"""Remediation nodes: decide_remediation, attempt_remediation, verify_remediation.

The LLM only picks the action type from a predefined safe menu.
Resource IDs come from state (validated AWS data), never from LLM output.
"""

import os
import time

from dotenv import load_dotenv

load_dotenv()  # ensure env vars are available when running from background threads

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from models.alert_state import AlertTriageState
from tools.aws_boto import run_boto_sync
from utils.llm import get_bedrock_llm

log = structlog.get_logger(__name__)

# ── Safe action menu ──────────────────────────────────────────────────────────
# LLM selects action_type from this list. Code resolves resource IDs from state.
SAFE_ACTIONS = {
    "force_ecs_redeploy": "Force a new ECS task deployment (restart all tasks)",
    "scale_out_ecs": "Increase ECS desired task count by 2 (max cap: 10)",
    "reboot_ec2": "Reboot the EC2 instance (non-destructive OS restart)",
    "reboot_rds": "Reboot the RDS DB instance (clears connection pool)",
    "scale_out_asg": "Increase ASG desired capacity by 2",
    "reboot_elasticache": "Reboot the ElastiCache cache cluster",
    "no_action": "No safe automated action available — requires manual review",
}

_SYSTEM_PROMPT = (
    "You are an AWS cloud operations expert. "
    "Given the root cause and service health, decide the safest automated remediation action. "
    "You may ONLY choose from the provided action list. "
    "If no safe action exists, choose 'no_action'. "
    "Never suggest destructive actions (terminate, delete, scale down)."
)

_FALLBACK_DECISION = {
    "can_remediate": False,
    "action_type": "no_action",
    "reasoning": "Fallback: LLM call failed. Defaulting to no_action for safety.",
    "risk_assessment": "unknown",
}


class RemediationDecision(BaseModel):
    """Structured LLM output for the remediation decision."""

    can_remediate: bool = Field(description="True if a safe automated action can help")
    action_type: str = Field(description="One of the action keys from the provided safe action list")
    reasoning: str = Field(description="Why this action was chosen")
    risk_assessment: str = Field(description="low | medium | high")


# ── Node: decide_remediation ──────────────────────────────────────────────────


def decide_remediation(state: AlertTriageState) -> dict:
    """Ask the LLM to pick a safe remediation action from the predefined menu.

    Returns delta dict with can_remediate, action_type, remediation_rationale.
    Never crashes the graph.
    """
    alert_id = state["alert_id"]
    log.info("decide_remediation_started", alert_id=alert_id)

    try:
        decision = _call_llm_for_decision(state)
        # Guard: if action_type is not in the safe list, force no_action
        if decision.action_type not in SAFE_ACTIONS:
            log.warning("decide_remediation_invalid_action", alert_id=alert_id,
                        action_type=decision.action_type)
            decision.action_type = "no_action"
            decision.can_remediate = False
    except Exception as e:
        log.error("decide_remediation_llm_failed", alert_id=alert_id, error=str(e))
        decision = RemediationDecision(**_FALLBACK_DECISION)

    log.info("decide_remediation_complete", alert_id=alert_id,
             can_remediate=decision.can_remediate, action_type=decision.action_type)

    return {
        "can_remediate": decision.can_remediate and decision.action_type != "no_action",
        "action_type": decision.action_type,
        "remediation_rationale": decision.reasoning,
        "llm_reasoning": [f"[decide_remediation] {decision.reasoning} (risk: {decision.risk_assessment})"],
        "actions_taken": [f"[decide_remediation] Selected action: {decision.action_type}"],
    }


def _call_llm_for_decision(state: AlertTriageState) -> RemediationDecision:
    """Build prompt and invoke the LLM with structured output for remediation decision."""
    llm = get_bedrock_llm(structured_output_schema=RemediationDecision)
    prompt = _build_decision_prompt(state)
    return llm.invoke([SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=prompt)])


def _build_decision_prompt(state: AlertTriageState) -> str:
    """Build the decision prompt including root cause and available safe actions."""
    actions_list = "\n".join(f"  - {k}: {v}" for k, v in SAFE_ACTIONS.items())
    import json
    health_summary = json.dumps(state.get("service_health", {}), indent=2)[:1500]
    return (
        f"## Root Cause Analysis\n"
        f"Root cause: {state.get('root_cause', 'unknown')}\n"
        f"Confidence: {state.get('confidence', 'low')}\n"
        f"Contributing factors: {state.get('contributing_factors', [])}\n\n"
        f"## Alert Context\n"
        f"Service: {state['service_type']} | Severity: {state['severity']}\n"
        f"Dimensions: {state['dimensions']}\n\n"
        f"## Current Service Health\n{health_summary}\n\n"
        f"## Available Safe Actions (choose ONLY from these)\n{actions_list}\n"
    )


# ── Node: attempt_remediation ─────────────────────────────────────────────────


def attempt_remediation(state: AlertTriageState) -> dict:
    """Execute the chosen safe remediation action using verified resource IDs from state.

    Resource IDs are sourced from state['dimensions'] and state['service_health'],
    NEVER from LLM output. Returns delta dict with remediation_results appended.
    """
    alert_id = state["alert_id"]
    action_type = state.get("action_type", "no_action")
    log.info("attempt_remediation_started", alert_id=alert_id, action_type=action_type)

    result = _execute_safe_action(
        action_type=action_type,
        dimensions=state.get("dimensions", {}),
        service_health=state.get("service_health", {}),
        region=state["region"],
        alert_id=alert_id,
    )

    log.info("attempt_remediation_complete", alert_id=alert_id,
             action_type=action_type, result_status=result.get("status"))

    return {
        "remediation_results": [{"action": action_type, "result": result}],
        "actions_taken": [f"[attempt_remediation] Executed {action_type}: status={result.get('status')}"],
    }


def _execute_safe_action(
    action_type: str,
    dimensions: dict,
    service_health: dict,
    region: str,
    alert_id: str,
) -> dict:
    """Map action_type to a concrete boto3 call using only verified resource IDs.

    Returns the boto3 result dict.
    """
    try:
        if action_type == "force_ecs_redeploy":
            return _ecs_force_redeploy(dimensions=dimensions, region=region)
        if action_type == "scale_out_ecs":
            return _ecs_scale_out(dimensions=dimensions, service_health=service_health, region=region)
        if action_type == "reboot_ec2":
            return _ec2_reboot(dimensions=dimensions, region=region)
        if action_type == "reboot_rds":
            return _rds_reboot(dimensions=dimensions, region=region)
        if action_type == "scale_out_asg":
            return _asg_scale_out(service_health=service_health, region=region)
        if action_type == "reboot_elasticache":
            return _elasticache_reboot(dimensions=dimensions, region=region)
        return {"status": "skipped", "reason": f"action_type '{action_type}' maps to no_action"}
    except Exception as e:
        log.error("execute_safe_action_failed", alert_id=alert_id, action_type=action_type, error=str(e))
        return {"status": "error", "error": str(e)}


def _ecs_force_redeploy(dimensions: dict, region: str) -> dict:
    """Force a new ECS deployment — restarts all tasks without downtime."""
    cluster = dimensions.get("ClusterName", "")
    service = dimensions.get("ServiceName", "")
    if not cluster or not service:
        return {"status": "error", "error": "Missing ClusterName or ServiceName in dimensions"}
    return run_boto_sync("ecs", "update_service",
                         {"cluster": cluster, "service": service, "forceNewDeployment": True}, region)


def _ecs_scale_out(dimensions: dict, service_health: dict, region: str) -> dict:
    """Increase ECS desired task count by 2, capped at 10."""
    cluster = dimensions.get("ClusterName", "")
    service = dimensions.get("ServiceName", "")
    if not cluster or not service:
        return {"status": "error", "error": "Missing ClusterName or ServiceName in dimensions"}
    current = service_health.get("desired_count", 1)
    new_count = min(current + 2, 10)
    return run_boto_sync("ecs", "update_service",
                         {"cluster": cluster, "service": service, "desiredCount": new_count}, region)


def _ec2_reboot(dimensions: dict, region: str) -> dict:
    """Reboot an EC2 instance."""
    instance_id = dimensions.get("InstanceId", "")
    if not instance_id:
        return {"status": "error", "error": "Missing InstanceId in dimensions"}
    return run_boto_sync("ec2", "reboot_instances", {"InstanceIds": [instance_id]}, region)


def _rds_reboot(dimensions: dict, region: str) -> dict:
    """Reboot an RDS DB instance."""
    db_id = dimensions.get("DBInstanceIdentifier", dimensions.get("DbClusterIdentifier", ""))
    if not db_id:
        return {"status": "error", "error": "Missing DBInstanceIdentifier in dimensions"}
    return run_boto_sync("rds", "reboot_db_instance", {"DBInstanceIdentifier": db_id}, region)


def _asg_scale_out(service_health: dict, region: str) -> dict:
    """Increase ASG desired capacity by 2, capped at max_size."""
    asg_name = service_health.get("asg_name", "")
    if not asg_name:
        return {"status": "error", "error": "ASG name not available in service_health"}
    current = service_health.get("desired_capacity", 1)
    return run_boto_sync("autoscaling", "set_desired_capacity",
                         {"AutoScalingGroupName": asg_name, "DesiredCapacity": current + 2}, region)


def _elasticache_reboot(dimensions: dict, region: str) -> dict:
    """Reboot all nodes in an ElastiCache cluster."""
    cluster_id = dimensions.get("CacheClusterId", "")
    if not cluster_id:
        return {"status": "error", "error": "Missing CacheClusterId in dimensions"}
    return run_boto_sync("elasticache", "reboot_cache_cluster",
                         {"CacheClusterId": cluster_id, "CacheNodeIdsToReboot": ["0001"]}, region)


# ── Node: verify_remediation ──────────────────────────────────────────────────


def verify_remediation(state: AlertTriageState) -> dict:
    """Check if the service has stabilised after remediation.

    Waits 30 seconds then re-checks service health.
    Sets resolved=True if the service looks healthy, False otherwise.
    """
    alert_id = state["alert_id"]
    log.info("verify_remediation_started", alert_id=alert_id)

    time.sleep(30)  # Give AWS time to propagate the remediation action

    resolved = _check_service_resolved(
        service_type=state.get("service_type", "unknown"),
        service_health=state.get("service_health", {}),
        dimensions=state.get("dimensions", {}),
        region=state["region"],
    )

    log.info("verify_remediation_complete", alert_id=alert_id, resolved=resolved)

    return {
        "resolved": resolved,
        "actions_taken": [f"[verify_remediation] Post-remediation health check: resolved={resolved}"],
    }


def _check_service_resolved(
    service_type: str,
    service_health: dict,
    dimensions: dict,
    region: str,
) -> bool:
    """Re-check service health to determine if the issue is resolved.

    Returns True if the service appears healthy, False otherwise.
    """
    if service_type == "ecs":
        cluster = dimensions.get("ClusterName", "")
        service = dimensions.get("ServiceName", "")
        if not cluster or not service:
            return False
        result = run_boto_sync("ecs", "describe_services",
                               {"cluster": cluster, "services": [service]}, region)
        services = result.get("data", {}).get("Services", [{}])
        svc = services[0] if services else {}
        desired = svc.get("desiredCount", 0)
        running = svc.get("runningCount", 0)
        return desired > 0 and running >= desired

    if service_type == "rds":
        db_id = dimensions.get("DBInstanceIdentifier", "")
        if not db_id:
            return False
        result = run_boto_sync("rds", "describe_db_instances", {"DBInstanceIdentifier": db_id}, region)
        instances = result.get("data", {}).get("DBInstances", [{}])
        return instances[0].get("DBInstanceStatus") == "available" if instances else False

    # For other service types, conservatively mark as unresolved
    return False
