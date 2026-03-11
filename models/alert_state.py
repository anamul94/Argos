"""AlertTriageState — the single source of truth passed between all graph nodes.

Dependency rule: imports nothing from inside this project.
"""

import operator
from typing import Annotated, Optional
from typing_extensions import TypedDict


class AlertTriageState(TypedDict):
    # ── Input (populated by ingest_alert) ────────────────────────────────────
    raw_payload: dict
    account_id: str
    region: str
    alert_id: str
    alarm_name: str
    alarm_arn: str
    alarm_reason: str
    alarm_timestamp: str
    metadata: dict
    token_usage_metadata: dict

    # ── Classification (populated by classify_alert) ──────────────────────────
    severity: str                    # "p1" | "p2" | "p3" | "p4"
    service_type: str                # "ecs" | "ec2" | "rds" | "lambda" | "alb" | "unknown"
    metric_namespace: str
    metric_name: str
    dimensions: dict                 # e.g. {"ClusterName": "prod", "ServiceName": "api"}
    affected_resources: list[str]

    # ── Evidence (populated by gather_evidence) ───────────────────────────────
    metric_data: dict
    service_health: dict
    recent_logs: list[str]
    alarm_history: list[dict]

    # ── Analysis (populated by analyze_root_cause) ───────────────────────────
    root_cause: str
    confidence: str                  # "high" | "medium" | "low"
    contributing_factors: list[str]

    # ── Remediation decision (populated by decide_remediation) ───────────────
    can_remediate: bool
    action_type: str                 # e.g. "force_ecs_redeploy" | "no_action"
    remediation_rationale: str

    # ── Remediation result (populated by attempt_remediation) ────────────────
    resolved: bool

    # ── Notifications (populated by notify_and_report) ───────────────────────
    jira_issue_key: Optional[str]
    sms_sent: bool
    telegram_sent: bool
    report: dict

    # ── Append-only audit trail (reducer: list concat) ────────────────────────
    llm_reasoning: Annotated[list[str], operator.add]
    actions_taken: Annotated[list[str], operator.add]
    node_errors: Annotated[list[str], operator.add]
    remediation_results: Annotated[list[dict], operator.add]
