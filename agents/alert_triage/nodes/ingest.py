"""ingest_alert node — normalises the raw EventBridge CloudWatch alarm payload.

Produces: account_id, region, alert_id, alarm_name, alarm_arn,
          alarm_reason, alarm_timestamp from raw_payload.
"""

import time

import structlog

from models.alert_state import AlertTriageState

log = structlog.get_logger(__name__)


def ingest_alert(state: AlertTriageState) -> dict:
    """Parse the raw EventBridge payload and extract core alert fields.

    Handles both direct EventBridge events and SQS-wrapped events.
    Returns delta dict with base alert fields populated.
    """
    payload = state["raw_payload"]
    event = _unwrap_event(payload)

    account_id = event.get("account", "unknown")
    region = event.get("region", "unknown")
    detail = event.get("detail", {})
    alarm_name = detail.get("alarmName", "unknown-alarm")
    alarm_arn = detail.get("alarmArn", "")
    alarm_timestamp = detail.get("state", {}).get("timestamp", "")
    alarm_reason = detail.get("state", {}).get("reason", "")

    alert_id = _build_alert_id(account_id=account_id, alarm_name=alarm_name)

    log.info("alert_ingested", alert_id=alert_id, alarm_name=alarm_name,
             account_id=account_id, region=region)

    return {
        "account_id": account_id,
        "region": region,
        "alert_id": alert_id,
        "alarm_name": alarm_name,
        "alarm_arn": alarm_arn,
        "alarm_reason": alarm_reason,
        "alarm_timestamp": alarm_timestamp,
        "actions_taken": [f"[ingest_alert] Ingested alarm '{alarm_name}' from account {account_id}"],
    }


def _unwrap_event(payload: dict) -> dict:
    """Extract the EventBridge event from direct or SQS-wrapped payloads.

    SQS wraps EventBridge events inside Records[0].body (JSON string).
    Returns the EventBridge event dict.
    """
    if "Records" in payload:
        import json
        first_record = payload["Records"][0]
        body = first_record.get("body", "{}")
        return json.loads(body) if isinstance(body, str) else body
    return payload


def _build_alert_id(account_id: str, alarm_name: str) -> str:
    """Build a unique, time-scoped alert ID.

    Format: {account_id}-{alarm_name}-{epoch_seconds}
    Epoch ensures two firings of the same alarm are separate investigations.
    """
    epoch = int(time.time())
    safe_name = alarm_name.replace(" ", "-").lower()
    return f"{account_id}-{safe_name}-{epoch}"
