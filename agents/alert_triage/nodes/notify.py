"""notify_and_report node — creates Jira ticket, sends Telegram alert, SMS for P1.

This is always the final node — runs whether or not remediation succeeded.
"""

import re
import json

import structlog

from models.alert_state import AlertTriageState
from tools.jira import create_jira_issue
from tools.sns_notify import send_sms
from tools.telegram_notify import send_ops_alert
from utils.token_usage import build_node_usage, merge_node_usage

log = structlog.get_logger(__name__)

_NOT_FOUND_ERROR_CODES = {
    "InvalidInstanceID.NotFound",
    "DBInstanceNotFound",
    "CacheClusterNotFound",
    "AutoScalingGroupNotFound",
    "ServiceNotFoundException",
    "ClusterNotFoundException",
    "ResourceNotFoundException",
    "TargetGroupNotFound",
}
_NOT_FOUND_ERROR_HINTS = (
    "not found",
    "does not exist",
    "cannot be found",
    "resource not found",
    "deleted",
    "terminated",
)


def notify_and_report(state: AlertTriageState) -> dict:
    """Send all notifications and create the incident report.

    Returns delta dict with jira_issue_key, sms_sent, telegram_sent, report.
    """
    alert_id = state["alert_id"]
    severity = state.get("severity", "p3")
    log.info("notify_and_report_started", alert_id=alert_id, severity=severity)

    jira_result = _create_ticket(state)
    jira_key = jira_result.get("issue_key")

    notify_tool_calls_by_name = {
        "create_jira_issue": 1,
        "send_ops_alert": 1,
        "send_sms": 1 if severity.lower() == "p1" else 0,
    }
    token_usage_metadata = merge_node_usage(
        state.get("token_usage_metadata"),
        "notify_and_report",
        build_node_usage(
            model_name="none",
            tool_calls=sum(notify_tool_calls_by_name.values()),
            tool_calls_by_name=notify_tool_calls_by_name,
        ),
    )

    telegram_result = _send_telegram(state=state, jira_key=jira_key, token_usage_metadata=token_usage_metadata)
    sms_result = _send_sms_if_p1(state=state, token_usage_metadata=token_usage_metadata)

    report = _build_report(state=state, jira_key=jira_key, token_usage_metadata=token_usage_metadata)

    log.info("notify_and_report_complete", alert_id=alert_id, jira_key=jira_key,
             telegram_ok=telegram_result.get("status") == "ok",
             sms_sent=sms_result is not None)

    return {
        "jira_issue_key": jira_key,
        "telegram_sent": telegram_result.get("status") == "ok",
        "sms_sent": sms_result is not None and sms_result.get("status") == "ok",
        "token_usage_metadata": token_usage_metadata,
        "report": report,
        "actions_taken": [
            f"[notify_and_report] Jira: {jira_key or 'failed'} | "
            f"Telegram: {telegram_result.get('status')} | "
            f"SMS: {'sent' if sms_result else 'skipped'}"
        ],
    }


def _create_ticket(state: AlertTriageState) -> dict:
    """Create a Jira incident ticket from alert state. Returns result dict."""
    try:
        return create_jira_issue(
            alert_id=state["alert_id"],
            alarm_name=state["alarm_name"],
            severity=state.get("severity", "p3"),
            service_type=state.get("service_type", "unknown"),
            root_cause=state.get("root_cause", "Unknown"),
            contributing_factors=state.get("contributing_factors", []),
            actions_taken=state.get("actions_taken", []),
            resolved=state.get("resolved", False),
            account_id=state["account_id"],
            region=state["region"],
        )
    except Exception as e:
        log.error("notify_jira_failed", alert_id=state["alert_id"], error=str(e))
        return {"status": "error", "error": str(e)}


def _send_telegram(state: AlertTriageState, jira_key: str | None, token_usage_metadata: dict) -> dict:
    """Send a formatted alert summary to the Telegram ops channel."""
    text = _build_telegram_message(state=state, jira_key=jira_key, token_usage_metadata=token_usage_metadata)
    try:
        return send_ops_alert(text=text, alert_id=state["alert_id"])
    except Exception as e:
        log.error("notify_telegram_failed", alert_id=state["alert_id"], error=str(e))
        return {"status": "error", "error": str(e)}


def _send_sms_if_p1(state: AlertTriageState, token_usage_metadata: dict) -> dict | None:
    """Send SMS to on-call only for P1 alerts. Returns None for non-P1."""
    if state.get("severity", "").lower() != "p1":
        return None
    totals = token_usage_metadata.get("totals", {}) if isinstance(token_usage_metadata, dict) else {}
    per_node_summary = _build_token_usage_sms_line(token_usage_metadata)
    message = (
        f"[ARGOS P1 ALERT] {state['alarm_name']}\n"
        f"Account: {state['account_id']} | {state['region']}\n"
        f"Root cause: {state.get('root_cause', 'Unknown')[:200]}\n"
        f"Resolved: {state.get('resolved', False)}\n"
        f"Tokens in/out/total: {totals.get('input_tokens', 0)}/{totals.get('output_tokens', 0)}/{totals.get('total_tokens', 0)}\n"
        f"Token cost in/out/total USD: {totals.get('total_input_token_cost_usd', 0.0):.6f}/{totals.get('total_output_token_cost_usd', 0.0):.6f}/{totals.get('total_token_cost_usd', 0.0):.6f}\n"
        f"Tool calls: {totals.get('tool_calls', 0)}\n"
        f"Node usage: {per_node_summary}\n"
        f"Alert ID: {state['alert_id']}"
    )
    try:
        return send_sms(message=message, alert_id=state["alert_id"])
    except Exception as e:
        log.error("notify_sms_failed", alert_id=state["alert_id"], error=str(e))
        return {"status": "error", "error": str(e)}


def _build_telegram_message(state: AlertTriageState, jira_key: str | None, token_usage_metadata: dict) -> str:
    """Build the Telegram ops alert message in Markdown."""
    resolved_label = "RESOLVED" if state.get("resolved") else "OPEN"
    severity = state.get("severity", "??").upper()
    service = state.get("service_type", "unknown").upper()
    confidence = state.get("confidence", "?")
    root_cause = str(state.get("root_cause", "Unknown")).strip() or "Unknown"

    raw_factors = state.get("contributing_factors", [])[:4]
    factors = "\n".join(f"- {_limit_line(str(f), 140)}" for f in raw_factors) or "- None identified"

    raw_actions = state.get("actions_taken", [])[-4:]
    actions = "\n".join(f"- {_limit_line(_clean_action_text(str(a)), 160)}" for a in raw_actions) or "- None"
    jira_display = jira_key if jira_key else "not created"
    auto_status = "yes" if state.get("resolved") else "no"
    resource_issue = _detect_resource_missing_or_deleted(state)
    resource_note = (
        "Potential lifecycle issue: target resource appears missing/deleted. "
        "Check CloudTrail change events for delete/terminate actions."
    )

    header = (
        f"**ARGOS INCIDENT UPDATE**\n"
        f"`{severity} | {resolved_label} | {service}`\n"
        f"**Alarm:** {_limit_line(state['alarm_name'], 120)}\n\n"
        f"`Account : {state['account_id']}`\n"
        f"`Region  : {state['region']}`\n"
        f"`Alert ID: {state['alert_id']}`\n"
        f"`Jira    : {jira_display}`\n"
        f"`Auto fix: {auto_status}`\n\n"
        f"---\n\n"
    )
    body = (
        f"**Root Cause** (confidence: {confidence})\n"
        f"{_limit_line(root_cause, 500)}\n\n"
    )
    if resource_issue:
        body += f"**Resource Status Note**\n{resource_note}\n\n"
    body += (
        f"**Actions Taken by Argos**\n{actions}\n\n"
        f"**Contributing Factors**\n{factors}\n\n"
        f"**Token Usage Metadata**\n{_build_token_usage_markdown(token_usage_metadata)}\n\n"
        f"---\n"
    )
    return header + body


_ACTION_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")


def _clean_action_text(text: str) -> str:
    """Drop internal node-prefix tags like [notify_and_report] for cleaner user output."""
    return _ACTION_PREFIX_RE.sub("", text).strip()


def _limit_line(text: str, max_len: int) -> str:
    """Hard-limit long lines so Telegram messages stay readable."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _build_report(state: AlertTriageState, jira_key: str | None, token_usage_metadata: dict) -> dict:
    """Build the final structured report stored in state for audit purposes."""
    resource_issue = _detect_resource_missing_or_deleted(state)
    resource_evidence = _collect_resource_issue_evidence(state)
    return {
        "alert_id": state["alert_id"],
        "alarm_name": state["alarm_name"],
        "severity": state.get("severity"),
        "service_type": state.get("service_type"),
        "account_id": state["account_id"],
        "region": state["region"],
        "root_cause": state.get("root_cause"),
        "confidence": state.get("confidence"),
        "contributing_factors": state.get("contributing_factors", []),
        "can_remediate": state.get("can_remediate", False),
        "action_type": state.get("action_type", "no_action"),
        "resolved": state.get("resolved", False),
        "jira_issue_key": jira_key,
        "actions_taken": state.get("actions_taken", []),
        "node_errors": state.get("node_errors", []),
        "token_usage_metadata": token_usage_metadata,
        "resource_missing_or_deleted": resource_issue,
        "resource_issue_evidence": resource_evidence,
    }


def _build_token_usage_markdown(token_usage_metadata: dict) -> str:
    """Build a compact fixed-width table for Telegram-friendly token usage output."""
    if not isinstance(token_usage_metadata, dict):
        return "- No usage metadata available"

    totals = token_usage_metadata.get("totals", {}) if isinstance(token_usage_metadata.get("totals"), dict) else {}
    nodes = token_usage_metadata.get("nodes", {}) if isinstance(token_usage_metadata.get("nodes"), dict) else {}

    headers = ("Node", "Model", "In", "Out", "Total", "Tools", "In$", "Out$", "Tot$")
    rows: list[tuple[str, str, str, str, str, str, str, str, str]] = [
        (
            "TOTAL",
            "-",
            str(totals.get("input_tokens", 0)),
            str(totals.get("output_tokens", 0)),
            str(totals.get("total_tokens", 0)),
            str(totals.get("tool_calls", 0)),
            f"{totals.get('total_input_token_cost_usd', 0.0):.6f}",
            f"{totals.get('total_output_token_cost_usd', 0.0):.6f}",
            f"{totals.get('total_token_cost_usd', 0.0):.6f}",
        )
    ]

    for node_name in sorted(nodes.keys()):
        usage = nodes.get(node_name, {}) if isinstance(nodes.get(node_name), dict) else {}
        rows.append(
            (
                _truncate(node_name, 18),
                _truncate(str(usage.get("model_name", "unknown")), 24),
                str(usage.get("input_tokens", 0)),
                str(usage.get("output_tokens", 0)),
                str(usage.get("total_tokens", 0)),
                str(usage.get("tool_calls", 0)),
                f"{usage.get('input_token_cost_usd', 0.0):.6f}",
                f"{usage.get('output_token_cost_usd', 0.0):.6f}",
                f"{usage.get('total_token_cost_usd', 0.0):.6f}",
            )
        )

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            col_widths[i] = max(col_widths[i], len(value))

    def _fmt(row: tuple[str, str, str, str, str, str, str, str, str]) -> str:
        return " | ".join(value.ljust(col_widths[i]) for i, value in enumerate(row))

    separator = "-+-".join("-" * w for w in col_widths)
    table_lines = [_fmt(headers), separator] + [_fmt(r) for r in rows]
    return "```text\n" + "\n".join(table_lines) + "\n```"


def _build_token_usage_sms_line(token_usage_metadata: dict) -> str:
    """Build a compact single-line node usage summary for SMS."""
    if not isinstance(token_usage_metadata, dict):
        return "n/a"
    nodes = token_usage_metadata.get("nodes", {}) if isinstance(token_usage_metadata.get("nodes"), dict) else {}
    parts: list[str] = []
    for node_name in sorted(nodes.keys()):
        usage = nodes.get(node_name, {}) if isinstance(nodes.get(node_name), dict) else {}
        parts.append(
            f"{node_name}:{usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}/{usage.get('total_tokens', 0)}"
            f",t{usage.get('tool_calls', 0)},m={usage.get('model_name', 'unknown')}"
        )
    summary = " | ".join(parts)
    return _limit_line(summary, 600)


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def _detect_resource_missing_or_deleted(state: AlertTriageState) -> bool:
    """Return True if remediation/investigation evidence indicates a missing or deleted resource."""
    return bool(_collect_resource_issue_evidence(state))


def _collect_resource_issue_evidence(state: AlertTriageState) -> list[str]:
    """Collect compact evidence strings for missing/deleted resource scenarios."""
    evidence: list[str] = []

    # 1) Structured remediation tool results
    for item in state.get("remediation_results", []):
        result = _parse_possible_json(item.get("result"))
        if not isinstance(result, dict):
            continue
        if result.get("resource_missing_or_deleted"):
            evidence.append(f"{item.get('tool')}: resource_missing_or_deleted=true")
            continue
        error_code = str(result.get("error_code", "")).strip()
        error_text = str(result.get("error", "")).lower()
        if error_code in _NOT_FOUND_ERROR_CODES or any(h in error_text for h in _NOT_FOUND_ERROR_HINTS):
            evidence.append(f"{item.get('tool')}: {error_code or result.get('error', 'not_found_signal')}")

    # 2) Unstructured fields as fallback
    fallback_texts = [
        str(state.get("root_cause", "")),
        " | ".join(str(x) for x in state.get("actions_taken", [])),
        " | ".join(str(x) for x in state.get("node_errors", [])),
    ]
    fallback_blob = " | ".join(t.lower() for t in fallback_texts if t)
    if any(h in fallback_blob for h in _NOT_FOUND_ERROR_HINTS):
        evidence.append("text-signals: not_found/deleted/terminated keywords present")

    return evidence[:6]


def _parse_possible_json(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None
