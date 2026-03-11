"""notify_and_report node — creates Jira ticket, sends Telegram alert, SMS for P1.

This is always the final node — runs whether or not remediation succeeded.
"""

import structlog

from models.alert_state import AlertTriageState
from tools.jira import create_jira_issue
from tools.sns_notify import send_sms
from tools.telegram_notify import send_ops_alert

log = structlog.get_logger(__name__)


def notify_and_report(state: AlertTriageState) -> dict:
    """Send all notifications and create the incident report.

    Returns delta dict with jira_issue_key, sms_sent, telegram_sent, report.
    """
    alert_id = state["alert_id"]
    severity = state.get("severity", "p3")
    log.info("notify_and_report_started", alert_id=alert_id, severity=severity)

    jira_result = _create_ticket(state)
    jira_key = jira_result.get("issue_key")

    telegram_result = _send_telegram(state=state, jira_key=jira_key)
    sms_result = _send_sms_if_p1(state=state)

    report = _build_report(state=state, jira_key=jira_key)

    log.info("notify_and_report_complete", alert_id=alert_id, jira_key=jira_key,
             telegram_ok=telegram_result.get("status") == "ok",
             sms_sent=sms_result is not None)

    return {
        "jira_issue_key": jira_key,
        "telegram_sent": telegram_result.get("status") == "ok",
        "sms_sent": sms_result is not None and sms_result.get("status") == "ok",
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


def _send_telegram(state: AlertTriageState, jira_key: str | None) -> dict:
    """Send a formatted alert summary to the Telegram ops channel."""
    text = _build_telegram_message(state=state, jira_key=jira_key)
    try:
        return send_ops_alert(text=text, alert_id=state["alert_id"])
    except Exception as e:
        log.error("notify_telegram_failed", alert_id=state["alert_id"], error=str(e))
        return {"status": "error", "error": str(e)}


def _send_sms_if_p1(state: AlertTriageState) -> dict | None:
    """Send SMS to on-call only for P1 alerts. Returns None for non-P1."""
    if state.get("severity", "").lower() != "p1":
        return None
    message = (
        f"[ARGOS P1 ALERT] {state['alarm_name']}\n"
        f"Account: {state['account_id']} | {state['region']}\n"
        f"Root cause: {state.get('root_cause', 'Unknown')[:200]}\n"
        f"Resolved: {state.get('resolved', False)}\n"
        f"Alert ID: {state['alert_id']}"
    )
    try:
        return send_sms(message=message, alert_id=state["alert_id"])
    except Exception as e:
        log.error("notify_sms_failed", alert_id=state["alert_id"], error=str(e))
        return {"status": "error", "error": str(e)}


def _build_telegram_message(state: AlertTriageState, jira_key: str | None) -> str:
    """Build the Telegram ops alert message in Markdown."""
    resolved_label = "RESOLVED" if state.get("resolved") else "OPEN"
    severity = state.get("severity", "??").upper()
    jira_ref = f" | Jira: {jira_key}" if jira_key else ""
    factors = "\n".join(f"  - {f}" for f in state.get("contributing_factors", [])[:5]) or "  None"
    actions = "\n".join(f"  - {a}" for a in state.get("actions_taken", [])[-5:]) or "  None"

    return (
        f"**[{severity}] {resolved_label} — {state['alarm_name']}**\n\n"
        f"**Account:** `{state['account_id']}` | **Region:** `{state['region']}`\n"
        f"**Service:** {state.get('service_type', 'unknown').upper()}\n"
        f"**Alert ID:** `{state['alert_id']}`{jira_ref}\n\n"
        f"**Root Cause** _(confidence: {state.get('confidence', '?')})_\n"
        f"{state.get('root_cause', 'Unknown')}\n\n"
        f"**Contributing Factors**\n{factors}\n\n"
        f"**Actions Taken by Argos**\n{actions}\n"
    )


def _build_report(state: AlertTriageState, jira_key: str | None) -> dict:
    """Build the final structured report stored in state for audit purposes."""
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
    }
