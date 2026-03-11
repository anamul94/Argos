"""Alert deduplication — prevents duplicate triage runs.

Handles two scenarios:
  1. Duplicate delivery — same EventBridge event delivered twice (at-least-once).
     Dedup key: alarm_arn + state_timestamp (unique per state change event).

  2. Alarm flapping — alarm fires, recovers, fires again within cooldown window.
     Dedup key: alarm_arn checked against a cooldown TTL (default 15 min).

Uses a single DynamoDB table (argos-dedup) with TTL-based expiry.
If DynamoDB is unavailable, fails OPEN (allows processing) to avoid
dropping real alerts.
"""

import os
import time

import boto3
import structlog
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger(__name__)

_TABLE_NAME = lambda: os.environ.get("DYNAMODB_DEDUP_TABLE", "argos-dedup")
_REGION = lambda: os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")

# How long (seconds) to suppress re-alarms for the same alarm after a triage run.
# Prevents flapping: alarm fires → resolves → fires again within this window.
_FLAP_COOLDOWN_SECONDS = int(os.environ.get("ALARM_FLAP_COOLDOWN_SECONDS", str(15 * 60)))


def is_duplicate(alarm_arn: str, state_timestamp: str, alert_id: str) -> bool:
    """Return True if this alarm event should be skipped.

    Checks two conditions:
      1. Exact duplicate: same alarm_arn + state_timestamp already processed.
      2. Flapping: same alarm_arn processed within the cooldown window.

    Fails open (returns False) if DynamoDB is unreachable.
    """
    exact_key = f"exact#{alarm_arn}#{state_timestamp}"
    flap_key = f"flap#{alarm_arn}"

    try:
        table = _get_table()

        if _key_exists(table, exact_key):
            log.warning("alert_duplicate_delivery_skipped",
                        alert_id=alert_id, alarm_arn=alarm_arn,
                        state_timestamp=state_timestamp)
            return True

        if _key_exists(table, flap_key):
            log.warning("alert_flapping_cooldown_skipped",
                        alert_id=alert_id, alarm_arn=alarm_arn,
                        cooldown_seconds=_FLAP_COOLDOWN_SECONDS)
            return True

        return False

    except Exception as e:
        log.error("dedup_check_failed_open", alert_id=alert_id, error=str(e))
        return False  # fail open — never drop a real alert


def mark_processing(alarm_arn: str, state_timestamp: str, alert_id: str) -> None:
    """Record this alarm event as being processed.

    Uses a conditional write so only one concurrent process wins.
    Raises DuplicateAlertError if another process beat us to it.
    """
    exact_key = f"exact#{alarm_arn}#{state_timestamp}"
    flap_key = f"flap#{alarm_arn}"
    now = int(time.time())

    try:
        table = _get_table()
        _put_with_ttl(
            table=table,
            dedup_key=exact_key,
            ttl=now + 3600,          # exact dedup: 1 hour is plenty
            alert_id=alert_id,
            conditional=True,        # raises if already exists
        )
        _put_with_ttl(
            table=table,
            dedup_key=flap_key,
            ttl=now + _FLAP_COOLDOWN_SECONDS,
            alert_id=alert_id,
            conditional=False,       # overwrite — reset the cooldown window
        )
        log.debug("dedup_marked_processing", alert_id=alert_id, alarm_arn=alarm_arn)

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise DuplicateAlertError(
                f"Alert {alert_id} already being processed by another instance"
            ) from e
        log.error("dedup_mark_failed", alert_id=alert_id, error=str(e))


def mark_completed(alarm_arn: str, alert_id: str) -> None:
    """Reset the flapping cooldown after a successful triage.

    Keeps the cooldown active so rapid re-alarms are still suppressed,
    but updates the alert_id so logs show the latest run.
    """
    flap_key = f"flap#{alarm_arn}"
    try:
        table = _get_table()
        _put_with_ttl(
            table=table,
            dedup_key=flap_key,
            ttl=int(time.time()) + _FLAP_COOLDOWN_SECONDS,
            alert_id=alert_id,
            conditional=False,
        )
    except Exception as e:
        log.error("dedup_mark_completed_failed", alert_id=alert_id, error=str(e))


# ── DynamoDB helpers ──────────────────────────────────────────────────────────


class DuplicateAlertError(Exception):
    """Raised when a concurrent process already claimed this alert."""


def _get_table():
    """Return the DynamoDB Table resource, creating the table if it doesn't exist."""
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=_REGION(),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    table = dynamodb.Table(_TABLE_NAME())
    _ensure_table_exists(dynamodb)
    return table


def _ensure_table_exists(dynamodb) -> None:
    """Create the dedup table if it doesn't exist. No-op if it already exists."""
    client = dynamodb.meta.client
    try:
        client.describe_table(TableName=_TABLE_NAME())
    except client.exceptions.ResourceNotFoundException:
        log.info("dedup_table_creating", table=_TABLE_NAME())
        client.create_table(
            TableName=_TABLE_NAME(),
            KeySchema=[{"AttributeName": "dedup_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "dedup_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=_TABLE_NAME())
        # Enable TTL so expired records are cleaned up automatically
        client.update_time_to_live(
            TableName=_TABLE_NAME(),
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
        )
        log.info("dedup_table_ready", table=_TABLE_NAME())


def _key_exists(table, dedup_key: str) -> bool:
    """Return True if the dedup_key exists and has not expired."""
    response = table.get_item(
        Key={"dedup_key": dedup_key},
        ProjectionExpression="dedup_key, ttl",
    )
    item = response.get("Item")
    if not item:
        return False
    # DynamoDB TTL expiry is eventual — check manually to be precise
    return int(item.get("ttl", 0)) > int(time.time())


def _put_with_ttl(
    table,
    dedup_key: str,
    ttl: int,
    alert_id: str,
    conditional: bool,
) -> None:
    """Write a dedup record with TTL. Uses conditional write if requested."""
    kwargs: dict = {
        "Item": {"dedup_key": dedup_key, "alert_id": alert_id, "ttl": ttl},
    }
    if conditional:
        kwargs["ConditionExpression"] = "attribute_not_exists(dedup_key)"
    table.put_item(**kwargs)
