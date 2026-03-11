"""classify_alert node — extracts severity, service type, and affected resources.

Severity is determined in priority order — no naming convention required:
  1. Alarm name prefix (p1-/p2-/p3-/p4-)  — explicit team override
  2. reasonData JSON   (real AWS events)   — structured current/threshold
  3. reason text regex (test/partial payloads) — parse "datapoint [95.0] > (80.0)"
  4. LLM classification (final fallback)   — full context, reliable for any alarm

This means any alarm — regardless of naming convention — gets a meaningful
severity before investigation begins.
"""

import json
import re
import structlog
from pydantic import BaseModel, Field

from models.alert_state import AlertTriageState

log = structlog.get_logger(__name__)

# Metric namespace → service_type string used throughout the graph
_NAMESPACE_MAP: dict[str, str] = {
    "AWS/ECS": "ecs",
    "AWS/EC2": "ec2",
    "AWS/RDS": "rds",
    "AWS/Lambda": "lambda",
    "AWS/ApplicationELB": "alb",
    "AWS/NetworkELB": "alb",
    "AWS/ElastiCache": "elasticache",
    "AWS/ApiGateway": "apigateway",
    "AWS/AutoScaling": "autoscaling",
    "CWAgent": "ec2",          # CloudWatch Agent metrics are EC2-hosted
    "Custom/EC2": "ec2",       # Nginx process checks from CloudWatch Agent
}

_VALID_SEVERITIES = {"p1", "p2", "p3", "p4"}

# Metric name substrings that indicate an inherently critical signal.
# These boost the computed severity one level (p3→p2, p2→p1, p1 stays p1).
_CRITICAL_METRIC_PATTERNS = {
    "healthyhostcount", "unhealthyhostcount",
    "5xx", "error", "fault",
    "throttle", "replicationlag",
    "diskqueue", "burstbalance",
}

# Metric name substrings considered elevated even without a large breach ratio.
_ELEVATED_METRIC_PATTERNS = {
    "cpuutilization", "memoryutilization",
    "databaseconnections", "freeableMemory",
    "networkpacketslost",
}


def classify_alert(state: AlertTriageState) -> dict:
    """Classify the alert by severity and AWS service type."""
    detail = state["raw_payload"].get("detail", {})
    alarm_name = state["alarm_name"]

    severity = _extract_severity(alarm_name, detail)
    metric_namespace, metric_name, dimensions = _extract_metric_info(detail)
    service_type = _NAMESPACE_MAP.get(metric_namespace, "unknown")
    affected_resources = _build_affected_resources(dimensions=dimensions, account_id=state["account_id"])
    metadata = state["raw_payload"].get("customMetadata", {})

    log.info(
        "alert_classified",
        alert_id=state["alert_id"],
        severity=severity,
        service_type=service_type,
        metric_namespace=metric_namespace,
        dimensions=dimensions,
        metadata=metadata,
    )

    return {
        "severity": severity,
        "service_type": service_type,
        "metric_namespace": metric_namespace,
        "metric_name": metric_name,
        "dimensions": dimensions,
        "metadata": metadata,
        "affected_resources": affected_resources,
        "actions_taken": [
            f"[classify_alert] severity={severity}, service={service_type}, "
            f"namespace={metric_namespace}, dimensions={dimensions}"
        ],
    }


# ── Severity resolution ───────────────────────────────────────────────────────

def _extract_severity(alarm_name: str, detail: dict) -> str:
    """Resolve severity using name prefix first, then breach magnitude.

    Priority:
      1. p1/p2/p3/p4 prefix in the alarm name  → use it directly
      2. Breach ratio from CloudWatch reasonData → map to p1–p4
      3. Metric name patterns                   → fallback + critical boost
    """
    prefix = alarm_name.lower().split("-")[0]
    if prefix in _VALID_SEVERITIES:
        return prefix

    return _severity_from_breach(detail)


def _severity_from_breach(detail: dict) -> str:
    """Compute severity from breach magnitude — tries three sources in order.

    Layer 1 — reasonData JSON (present in all real AWS EventBridge events):
      state.reasonData = '{"recentDatapoints":[95.0],"threshold":80.0,...}'

    Layer 2 — reason text regex (test payloads / partial events):
      state.reason = "Threshold Crossed: 1 datapoint [95.0] was greater than the threshold (80.0)."

    Layer 3 — LLM classification using full alarm context as final fallback.

    Breach ratio tiers:
      >= 2.0  → p1   (double the threshold — critical)
      >= 1.5  → p2   (50% above — elevated)
      >= 1.0  → p3   (just crossed)
      <  1.0  → p4   (edge case)

    Critical metric names then boost one level (p3→p2, p2→p1).
    """
    current, threshold = _parse_breach_from_reason_data(detail)

    if current is None or threshold is None:
        current, threshold = _parse_breach_from_reason_text(detail)

    if current is not None and threshold is not None and threshold != 0:
        if current >= threshold:
            breach_ratio = current / threshold
        else:
            breach_ratio = threshold / max(current, 0.001)

        if breach_ratio >= 2.0:
            severity = "p1"
        elif breach_ratio >= 1.5:
            severity = "p2"
        elif breach_ratio >= 1.0:
            severity = "p3"
        else:
            severity = "p4"

        return _apply_critical_boost(severity, detail)

    # All structured sources exhausted — ask the LLM
    return _severity_from_llm(detail)


def _parse_breach_from_reason_data(detail: dict) -> tuple[float | None, float | None]:
    """Extract current value and threshold from state.reasonData JSON."""
    reason_data_raw = detail.get("state", {}).get("reasonData", "{}")
    try:
        reason_data = json.loads(reason_data_raw)
    except (ValueError, TypeError):
        return None, None

    threshold = reason_data.get("threshold")
    datapoints = reason_data.get("recentDatapoints", [])
    current = next((v for v in reversed(datapoints) if v is not None), None)

    if threshold is None or current is None:
        return None, None
    return float(current), float(threshold)


# Matches CloudWatch reason text like:
#   "Threshold Crossed: 1 datapoint [95.0] was greater than the threshold (80.0)."
#   "Threshold Crossed: 3 out of the last 5 datapoints [95.2 (highest), 88.1, 91.4] ..."
_REASON_TEXT_RE = re.compile(
    r"\[(?P<current>[\d.]+)"          # first value inside [...]
    r".*?\]"                           # rest of bracket
    r".*?"                             # words between
    r"threshold\s*\((?P<threshold>[\d.]+)\)",  # (80.0)
    re.IGNORECASE,
)


def _parse_breach_from_reason_text(detail: dict) -> tuple[float | None, float | None]:
    """Parse current value and threshold from the human-readable reason string."""
    reason = detail.get("state", {}).get("reason", "")
    match = _REASON_TEXT_RE.search(reason)
    if not match:
        return None, None
    try:
        return float(match.group("current")), float(match.group("threshold"))
    except (ValueError, TypeError):
        return None, None


class _SeverityClassification(BaseModel):
    severity: str = Field(description="One of: p1, p2, p3, p4")
    rationale: str = Field(description="One sentence explaining the classification")


def _severity_from_llm(detail: dict) -> str:
    """LLM fallback — classifies severity from full alarm context.

    Used when neither reasonData nor reason text contain structured numbers.
    Never raises — falls back to p3 if the LLM call fails.
    """
    from dotenv import load_dotenv
    load_dotenv()
    from utils.llm import get_bedrock_llm

    alarm_name = detail.get("alarmName", "unknown")
    state_value = detail.get("state", {}).get("value", "ALARM")
    reason = detail.get("state", {}).get("reason", "")
    metric_name = _get_metric_name(detail)
    namespace = detail.get("configuration", {}).get("metrics", [{}])[0].get(
        "metricStat", {}).get("metric", {}).get("namespace", "unknown")
    dimensions = detail.get("configuration", {}).get("metrics", [{}])[0].get(
        "metricStat", {}).get("metric", {}).get("dimensions", {})

    prompt = f"""Classify the severity of this AWS CloudWatch alarm.

Alarm name: {alarm_name}
State: {state_value}
Metric: {metric_name} ({namespace})
Dimensions: {dimensions}
Reason: {reason}

Severity levels:
- p1: Production outage or critical data/revenue impact — act immediately
- p2: Significant degradation — investigate within 30 minutes
- p3: Elevated concern — investigate within 2 hours
- p4: Low / informational — investigate during business hours

Respond with the severity (p1/p2/p3/p4) and a one-sentence rationale."""

    try:
        llm = get_bedrock_llm(structured_output_schema=_SeverityClassification)
        result = llm.invoke(prompt)
        severity = result.severity.lower().strip()
        if severity not in _VALID_SEVERITIES:
            severity = "p3"
        log.info("severity_from_llm", alarm_name=alarm_name, severity=severity,
                 rationale=result.rationale)
        return severity
    except Exception as e:
        log.warning("severity_llm_failed", error=str(e), fallback="p3")
        return "p3"


def _severity_from_metric_name(detail: dict) -> str:
    """Severity from metric name patterns alone (no breach data, no LLM)."""
    metric_name = _get_metric_name(detail).lower()

    if any(p in metric_name for p in _CRITICAL_METRIC_PATTERNS):
        return "p2"
    if any(p in metric_name for p in _ELEVATED_METRIC_PATTERNS):
        return "p3"
    return "p3"


def _apply_critical_boost(severity: str, detail: dict) -> str:
    """Promote severity one level for inherently critical metric names.

    e.g. even a small 5xx spike should be p2 not p3.
    p1 is never promoted further.
    """
    metric_name = _get_metric_name(detail).lower()
    if any(p in metric_name for p in _CRITICAL_METRIC_PATTERNS):
        return {"p4": "p3", "p3": "p2", "p2": "p1", "p1": "p1"}.get(severity, severity)
    return severity


def _get_metric_name(detail: dict) -> str:
    metrics = detail.get("configuration", {}).get("metrics", [])
    if not metrics:
        return ""
    return metrics[0].get("metricStat", {}).get("metric", {}).get("name", "")


# ── Metric info extraction ────────────────────────────────────────────────────

def _extract_metric_info(detail: dict) -> tuple[str, str, dict]:
    """Extract metric namespace, name, and dimensions from the alarm detail."""
    metrics = detail.get("configuration", {}).get("metrics", [])
    if not metrics:
        return "unknown", "unknown", {}

    first_metric = metrics[0].get("metricStat", {}).get("metric", {})
    namespace = first_metric.get("namespace", "unknown")
    name = first_metric.get("name", "unknown")
    dimensions = first_metric.get("dimensions", {})
    return namespace, name, dimensions


def _build_affected_resources(dimensions: dict, account_id: str) -> list[str]:
    """Build a human-readable list of affected resource identifiers."""
    if not dimensions:
        return [f"account:{account_id}"]
    return [f"{k}={v}" for k, v in dimensions.items()]
