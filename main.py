"""Argos — FastAPI application.

Routes:
  POST /telegram-webhook    Interactive AWS assistant (existing Telegram bot)
  POST /alert-webhook       EventBridge CloudWatch alarm → alert triage graph
"""

import asyncio
import json

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.background import BackgroundTasks
from langchain.agents import create_agent
from langgraph.checkpoint.memory import MemorySaver

from agents.alert_triage.graph import (
    alert_triage_graph,
    build_initial_state,
    build_thread_config,
)
from tools.aws_boto import aws_boto_command_tool
from utils.formatting import markdown_to_telegram_html

load_dotenv()

import httpx
import os

log = structlog.get_logger(__name__)
app = FastAPI(title="Argos", version="1.0.0")

# ── Telegram interactive agent ────────────────────────────────────────────────

_telegram_agent = create_agent(
    model=os.environ.get("BEDROCK_MODEL_ID", "bedrock:global.anthropic.claude-sonnet-4-6"),
    tools=[aws_boto_command_tool],
    system_prompt=(
        "You are an AWS expert who can help users query and manage their AWS resources "
        "using boto3. Only use the provided tools and do not make up commands. "
        "Always ask for clarification if the user's request is ambiguous. "
        "Reply in plain text or simple Markdown (bold/italic/code, lists)."
    ),
    checkpointer=MemorySaver(),
)


@app.post("/telegram-webhook")
@app.post("/")
async def telegram_webhook(req: Request) -> dict:
    """Receive Telegram updates and respond via the interactive AWS agent."""
    try:
        data = await req.json()
        if "message" not in data:
            return {"ok": True}
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

        if not token:
            log.error("telegram_token_missing")
            return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}

        config = {"configurable": {"thread_id": str(chat_id)}}
        result = _telegram_agent.invoke(
            {"messages": [{"role": "user", "content": text}]},
            config=config,
        )
        reply = markdown_to_telegram_html(str(result["messages"][-1].content))

        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": reply, "parse_mode": "HTML"},
            )
        return {"ok": True}
    except Exception as e:
        log.error("telegram_webhook_error", error=str(e))
        return {"ok": False, "error": str(e)}


# ── Alert triage webhook ──────────────────────────────────────────────────────


@app.post("/alert-webhook")
async def alert_webhook(req: Request, background_tasks: BackgroundTasks) -> dict:
    """Receive EventBridge CloudWatch alarm events and trigger the triage graph.

    Returns 200 immediately. Triage runs as a background task so the
    API Gateway / EventBridge HTTP target does not time out.

    Expected payload: native CloudWatch Alarm State Change event from EventBridge,
    or an SQS Records wrapper containing one.
    """
    try:
        payload = await req.json()
    except Exception as e:
        log.error("alert_webhook_bad_payload", error=str(e))
        return {"ok": False, "error": "Invalid JSON payload"}

    event = _extract_cloudwatch_alarm_event(payload)
    if not event:
        log.warning("alert_webhook_unexpected_payload",
                    source=payload.get("source"), detail_type=payload.get("detail-type"))
        return {"ok": True, "status": "ignored", "reason": "Not a CloudWatch alarm event"}

    if not _is_cloudwatch_alarm_event(event):
        log.info(
            "alert_webhook_ignored_non_alarm_state",
            alarm_name=event.get("detail", {}).get("alarmName", "unknown"),
            state=event.get("detail", {}).get("state", {}).get("value", "unknown"),
        )
        return {"ok": True, "status": "ignored", "reason": "CloudWatch event state is not ALARM"}

    log.info("alert_webhook_received",
             alarm_name=event.get("detail", {}).get("alarmName", "unknown"),
             account=event.get("account", "unknown"))

    background_tasks.add_task(_run_triage, event)
    return {"ok": True, "status": "processing"}


def _extract_cloudwatch_alarm_event(payload: dict) -> dict | None:
    """Return a normalized CloudWatch Alarm State Change event from direct or SQS-wrapped payloads."""
    if not isinstance(payload, dict):
        return None

    if _is_cloudwatch_event_shape(payload):
        return payload

    records = payload.get("Records", [])
    if not isinstance(records, list):
        return None

    for record in records:
        body = record.get("body", {})
        event = _parse_json_maybe(body)
        if not isinstance(event, dict):
            continue

        # Handles SNS->SQS wrappers where the actual event is in Message.
        if "Message" in event:
            message = _parse_json_maybe(event.get("Message", {}))
            if isinstance(message, dict):
                event = message

        if _is_cloudwatch_event_shape(event):
            return event

    return None


def _parse_json_maybe(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _is_cloudwatch_event_shape(event: dict) -> bool:
    return (
        event.get("source") == "aws.cloudwatch"
        and event.get("detail-type") == "CloudWatch Alarm State Change"
        and isinstance(event.get("detail"), dict)
    )


def _is_cloudwatch_alarm_event(event: dict) -> bool:
    """Return True only for ALARM state transitions of CloudWatch Alarm State Change events."""
    return _is_cloudwatch_event_shape(event) and event.get("detail", {}).get("state", {}).get("value") == "ALARM"


def _run_triage(event: dict) -> None:
    """Invoke the alert triage graph synchronously (runs in background thread)."""
    import time
    from utils.deduplication import DuplicateAlertError, is_duplicate, mark_completed, mark_processing

    detail = event.get("detail", {})
    account_id = event.get("account", "unknown")
    alarm_name = detail.get("alarmName", "unknown")
    alarm_arn = detail.get("alarmArn", alarm_name)
    state_timestamp = detail.get("state", {}).get("timestamp", "")
    alert_id = f"{account_id}-{alarm_name.lower().replace(' ', '-')}-{int(time.time())}"

    # ── Deduplication check ───────────────────────────────────────────────────
    if is_duplicate(alarm_arn=alarm_arn, state_timestamp=state_timestamp, alert_id=alert_id):
        return  # logged inside is_duplicate

    try:
        mark_processing(alarm_arn=alarm_arn, state_timestamp=state_timestamp, alert_id=alert_id)
    except DuplicateAlertError:
        log.warning("alert_claimed_by_other_instance", alert_id=alert_id, alarm_name=alarm_name)
        return

    # ── Run graph ─────────────────────────────────────────────────────────────
    config = build_thread_config(account_id=account_id, alert_id=alert_id)
    initial_state = build_initial_state(raw_payload=event)

    log.info("triage_graph_starting", alert_id=alert_id, alarm_name=alarm_name)
    try:
        result = alert_triage_graph.invoke(initial_state, config=config)
        log.info(
            "triage_graph_complete",
            alert_id=alert_id,
            resolved=result.get("resolved", False),
            jira_key=result.get("jira_issue_key"),
            severity=result.get("severity"),
        )
        mark_completed(alarm_arn=alarm_arn, alert_id=alert_id)
    except Exception as e:
        log.error("triage_graph_failed", alert_id=alert_id, alarm_name=alarm_name, error=str(e))


# ── Health check ──────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    """Basic health check endpoint."""
    return {"status": "ok", "service": "argos"}
