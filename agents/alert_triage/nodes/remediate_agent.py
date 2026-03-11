"""remediate_agent node — ReAct agent with TodoListMiddleware for multi-step remediation.

Replaces decide_remediation + attempt_remediation + verify_remediation with an
autonomous loop that can:
  - Plan multiple remediation steps upfront (via write_todos)
  - Try action → verify → try next action if not resolved
  - Escalate to no_action cleanly when nothing safe is available

Middleware stack:
  - TodoListMiddleware  : plan before executing, track step completion
  - ToolCallLimitMiddleware(run_limit=10) : hard cap (plan + actions + verifies)

Safety invariant: all write tools are closures capturing resource IDs from
state['dimensions'] (AWS-validated). The LLM picks WHICH action — it cannot
inject arbitrary resource IDs.
"""

import json
import time

import structlog
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from models.alert_state import AlertTriageState
from tools.aws_boto import run_boto_sync
from utils.llm import get_active_model_name, get_bedrock_llm
from utils.token_usage import (
    build_node_usage,
    count_tool_calls_by_name,
    extract_token_usage_from_messages,
    merge_node_usage,
)

load_dotenv()

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are an AWS cloud operations engineer performing automated remediation.

Use the write_todos tool to plan your remediation steps before executing anything.

## Remediation rules
- Always prefer the least disruptive action first (redeploy before reboot).
- After EVERY remediation action, call verify_service_health to check if it worked.
- If the first action does not resolve the issue, proceed to the next step in your plan.
- If no safe action can help, call no_action and explain why manual intervention is needed.
- Never skip verification after an action."""

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
    "no such",
    "deleted",
    "terminated",
)


def remediate_agent(state: AlertTriageState) -> dict:
    """ReAct remediation agent with TodoListMiddleware for structured multi-step execution."""
    alert_id = state["alert_id"]
    fallback_model = get_active_model_name()
    log.info("remediate_agent_started", alert_id=alert_id, service=state.get("service_type"))

    remediation_tools = _build_remediation_tools(state)
    llm = get_bedrock_llm()

    agent = create_agent(
        model=llm,
        tools=remediation_tools,
        system_prompt=_SYSTEM_PROMPT,
        middleware=[
            TodoListMiddleware(),
            ToolCallLimitMiddleware(run_limit=10),
        ],
    )

    try:
        result = agent.invoke({"messages": [HumanMessage(content=_build_initial_message(state))]})
        messages = result.get("messages", [])
        tool_calls_made, tool_calls_by_name = count_tool_calls_by_name(
            _extract_tool_results(messages, include_todos=True)
        )
        input_tokens, output_tokens, total_tokens, model_name = extract_token_usage_from_messages(
            messages,
            fallback_model_name=fallback_model,
        )
        node_usage = build_node_usage(
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            tool_calls=tool_calls_made,
            tool_calls_by_name=tool_calls_by_name,
        )
        tool_results = _extract_tool_results(messages)
        resolved = _determine_resolved(tool_results)
        actions = _extract_actions_taken(tool_results)
        final_summary = _get_final_summary(messages)

        log.info("remediate_agent_complete", alert_id=alert_id,
                 resolved=resolved, tool_calls=tool_calls_made,
                 input_tokens=input_tokens, output_tokens=output_tokens, model_name=model_name)
    except Exception as e:
        log.error("remediate_agent_failed", alert_id=alert_id, error=str(e))
        resolved = False
        actions = [f"Agent error: {str(e)[:200]}"]
        tool_results = []
        final_summary = f"Remediation agent failed: {str(e)}"
        node_usage = build_node_usage(model_name=fallback_model)

    token_usage_metadata = merge_node_usage(
        state.get("token_usage_metadata"),
        "remediate_agent",
        node_usage,
    )

    return {
        "resolved": resolved,
        "can_remediate": True,
        "action_type": _summarise_action_type(tool_results),
        "remediation_results": tool_results,
        "token_usage_metadata": token_usage_metadata,
        "actions_taken": [f"[remediate_agent] {a}" for a in actions],
        "llm_reasoning": [f"[remediate_agent] {final_summary[:500]}"],
    }


def _build_initial_message(state: AlertTriageState) -> str:
    health_summary = json.dumps(state.get("service_health", {}), indent=2)[:800]
    return (
        f"## Alert Requiring Remediation\n"
        f"Alarm: {state['alarm_name']}\n"
        f"Service: {state.get('service_type', 'unknown')} | Severity: {state.get('severity', 'p3')}\n"
        f"Dimensions: {json.dumps(state.get('dimensions', {}))}\n\n"
        f"## Root Cause\n"
        f"Root cause: {state.get('root_cause', 'unknown')}\n"
        f"Confidence: {state.get('confidence', 'low')}\n"
        f"Contributing factors: {state.get('contributing_factors', [])}\n\n"
        f"## Current Service Health\n{health_summary}\n\n"
        f"Write your remediation plan, then execute it step by step."
    )


def _extract_tool_results(messages: list, include_todos: bool = False) -> list[dict]:
    return [
        {"tool": m.name, "result": m.content}
        for m in messages
        if isinstance(m, ToolMessage) and (include_todos or m.name != "write_todos")
    ]


def _determine_resolved(tool_results: list[dict]) -> bool:
    """Check if the last verify_service_health call reported resolved=True."""
    for tr in reversed(tool_results):
        if tr["tool"] == "verify_service_health":
            try:
                return json.loads(tr["result"]).get("resolved", False)
            except Exception:
                pass
    return False


def _extract_actions_taken(tool_results: list[dict]) -> list[str]:
    actions = []
    for tr in tool_results:
        raw = str(tr["result"])
        parsed = _parse_result_json(raw)
        if parsed and parsed.get("resource_missing_or_deleted"):
            code = parsed.get("error_code", "unknown")
            actions.append(f"{tr['tool']}: resource not found or deleted ({code})")
        else:
            actions.append(f"{tr['tool']}: {raw[:120]}")
    return actions


def _get_final_summary(messages: list) -> str:
    return next(
        (str(m.content)[:500] for m in reversed(messages)
         if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None)),
        "No summary available",
    )


def _summarise_action_type(tool_results: list[dict]) -> str:
    """Return a comma-separated string of the write actions the agent took."""
    write_tools = {"force_ecs_redeploy", "scale_out_ecs", "reboot_ec2_instance",
                   "reboot_rds_instance", "scale_out_asg", "reboot_elasticache_cluster", "no_action"}
    actions = [tr["tool"] for tr in tool_results if tr["tool"] in write_tools]
    return ", ".join(actions) if actions else "no_action"


def _parse_result_json(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _is_resource_missing_or_deleted(result: dict) -> bool:
    error_code = str(result.get("error_code", "")).strip()
    error_text = str(result.get("error", "")).lower()
    if error_code in _NOT_FOUND_ERROR_CODES:
        return True
    return any(hint in error_text for hint in _NOT_FOUND_ERROR_HINTS)


# ── Remediation tools (closures — resource IDs from state dimensions only) ────

def _build_remediation_tools(state: AlertTriageState) -> list:
    """Build remediation tools as closures with resource IDs from state['dimensions'].

    The LLM decides WHICH tool to call. Resource IDs are resolved here from
    AWS-validated alarm dimensions — never from LLM output.
    """
    dimensions = state.get("dimensions", {})
    service_health = state.get("service_health", {})
    region = state["region"]
    service_type = state.get("service_type", "unknown")

    def _action_response(action: str, boto_result: dict, **details) -> str:
        payload: dict = {"action": action, "status": boto_result.get("status"), **details}
        if boto_result.get("status") != "ok":
            payload["error_code"] = boto_result.get("error_code")
            payload["error"] = boto_result.get("error")
            if _is_resource_missing_or_deleted(boto_result):
                payload["resource_missing_or_deleted"] = True
        return json.dumps(payload, default=str)

    @tool
    def force_ecs_redeploy() -> str:
        """Force a new ECS deployment — restarts all tasks without downtime.
        Best first action for: high CPU, memory leaks, task crashes, code bugs.
        """
        cluster = dimensions.get("ClusterName", "")
        service = dimensions.get("ServiceName", "")
        if not cluster or not service:
            return json.dumps({"status": "error", "error": "ClusterName or ServiceName not in alarm dimensions"})
        result = run_boto_sync(
            "ecs", "update_service",
            {"cluster": cluster, "service": service, "forceNewDeployment": True},
            region,
        )
        return _action_response("force_ecs_redeploy", result, cluster=cluster, service=service)

    @tool
    def scale_out_ecs() -> str:
        """Increase ECS desired task count by 2 (max 10).
        Use when running_count < desired_count or load is too high for current task count.
        """
        cluster = dimensions.get("ClusterName", "")
        service = dimensions.get("ServiceName", "")
        if not cluster or not service:
            return json.dumps({"status": "error", "error": "ClusterName or ServiceName not in alarm dimensions"})
        current = service_health.get("desired_count", 1)
        new_count = min(current + 2, 10)
        result = run_boto_sync(
            "ecs", "update_service",
            {"cluster": cluster, "service": service, "desiredCount": new_count},
            region,
        )
        return _action_response("scale_out_ecs", result, old_count=current, new_count=new_count)

    @tool
    def start_ec2_instance() -> str:
        """Start a STOPPED EC2 instance.
        Use when: instance_state=stopped (e.g. process crash caused shutdown, manual stop, auto-stop policy).
        Do NOT use for running instances — use reboot_ec2_instance for those.
        """
        instance_id = dimensions.get("InstanceId", "")
        if not instance_id:
            return json.dumps({"status": "error", "error": "InstanceId not in alarm dimensions"})
        result = run_boto_sync("ec2", "start_instances", {"InstanceIds": [instance_id]}, region)
        return _action_response("start_ec2", result, instance_id=instance_id)

    @tool
    def reboot_ec2_instance() -> str:
        """Reboot a RUNNING EC2 instance (non-destructive OS restart).
        Use for: high CPU, memory exhaustion, hung processes, OS-level issues.
        Do NOT use for stopped instances — use start_ec2_instance for those.
        """
        instance_id = dimensions.get("InstanceId", "")
        if not instance_id:
            return json.dumps({"status": "error", "error": "InstanceId not in alarm dimensions"})
        result = run_boto_sync("ec2", "reboot_instances", {"InstanceIds": [instance_id]}, region)
        return _action_response("reboot_ec2", result, instance_id=instance_id)

    @tool
    def reboot_rds_instance() -> str:
        """Reboot the RDS DB instance — clears the connection pool.
        Use for: connection exhaustion, connection pool saturation, DB unresponsive.
        """
        db_id = dimensions.get("DBInstanceIdentifier", dimensions.get("DbClusterIdentifier", ""))
        if not db_id:
            return json.dumps({"status": "error", "error": "DBInstanceIdentifier not in alarm dimensions"})
        result = run_boto_sync("rds", "reboot_db_instance", {"DBInstanceIdentifier": db_id}, region)
        return _action_response("reboot_rds", result, db_id=db_id)

    @tool
    def scale_out_asg() -> str:
        """Increase AutoScaling Group desired capacity by 2.
        Use for EC2 high load when horizontal scaling is appropriate.
        """
        asg_name = service_health.get("asg_name", "")
        if not asg_name:
            return json.dumps({"status": "error", "error": "ASG name not available in service health"})
        current = service_health.get("desired_capacity", 1)
        result = run_boto_sync(
            "autoscaling", "set_desired_capacity",
            {"AutoScalingGroupName": asg_name, "DesiredCapacity": current + 2},
            region,
        )
        return _action_response("scale_out_asg", result, asg=asg_name, new_capacity=current + 2)

    @tool
    def reboot_elasticache_cluster() -> str:
        """Reboot the ElastiCache cluster.
        Use for: cache corruption, connection issues, cluster unresponsive.
        """
        cluster_id = dimensions.get("CacheClusterId", "")
        if not cluster_id:
            return json.dumps({"status": "error", "error": "CacheClusterId not in alarm dimensions"})
        result = run_boto_sync(
            "elasticache", "reboot_cache_cluster",
            {"CacheClusterId": cluster_id, "CacheNodeIdsToReboot": ["0001"]},
            region,
        )
        return _action_response("reboot_elasticache", result, cluster_id=cluster_id)

    @tool
    def verify_service_health() -> str:
        """Check current service health to see if the remediation worked.

        ALWAYS call this after each remediation action. Waits 30 seconds first
        to give AWS time to propagate the change.
        Returns: {"resolved": true/false, "health": {...current state...}}
        """
        time.sleep(30)
        resolved = False
        health = {}

        if service_type == "ecs":
            cluster = dimensions.get("ClusterName", "")
            service_name = dimensions.get("ServiceName", "")
            if cluster and service_name:
                result = run_boto_sync("ecs", "describe_services",
                                       {"cluster": cluster, "services": [service_name]}, region)
                if result.get("status") != "ok":
                    health = {
                        "status": result.get("status"),
                        "error_code": result.get("error_code"),
                        "error": result.get("error"),
                    }
                    if _is_resource_missing_or_deleted(result):
                        health["resource_missing_or_deleted"] = True
                    return json.dumps({"resolved": False, "health": health}, default=str)
                svc = (result.get("data", {}).get("Services") or [{}])[0]
                desired = svc.get("desiredCount", 0)
                running = svc.get("runningCount", 0)
                health = {"desired": desired, "running": running, "pending": svc.get("pendingCount", 0)}
                resolved = desired > 0 and running >= desired

        elif service_type == "rds":
            db_id = dimensions.get("DBInstanceIdentifier", "")
            if db_id:
                result = run_boto_sync("rds", "describe_db_instances",
                                       {"DBInstanceIdentifier": db_id}, region)
                if result.get("status") != "ok":
                    health = {
                        "status": result.get("status"),
                        "error_code": result.get("error_code"),
                        "error": result.get("error"),
                    }
                    if _is_resource_missing_or_deleted(result):
                        health["resource_missing_or_deleted"] = True
                    return json.dumps({"resolved": False, "health": health}, default=str)
                instances = result.get("data", {}).get("DBInstances") or [{}]
                status = instances[0].get("DBInstanceStatus", "unknown")
                health = {"status": status}
                resolved = status == "available"

        elif service_type == "ec2":
            instance_id = dimensions.get("InstanceId", "")
            if instance_id:
                result = run_boto_sync("ec2", "describe_instance_status",
                                       {"InstanceIds": [instance_id], "IncludeAllInstances": True}, region)
                if result.get("status") != "ok":
                    health = {
                        "status": result.get("status"),
                        "error_code": result.get("error_code"),
                        "error": result.get("error"),
                    }
                    if _is_resource_missing_or_deleted(result):
                        health["resource_missing_or_deleted"] = True
                    return json.dumps({"resolved": False, "health": health}, default=str)
                statuses = result.get("data", {}).get("InstanceStatuses") or [{}]
                state_info = statuses[0]
                instance_state = state_info.get("InstanceState", {}).get("Name", "unknown")
                health = {
                    "instance_state": instance_state,
                    "instance_status": state_info.get("InstanceStatus", {}).get("Status", "unknown"),
                }
                resolved = instance_state == "running"

        elif service_type == "elasticache":
            cluster_id = dimensions.get("CacheClusterId", "")
            if cluster_id:
                result = run_boto_sync("elasticache", "describe_cache_clusters",
                                       {"CacheClusterId": cluster_id, "ShowCacheNodeInfo": True}, region)
                if result.get("status") != "ok":
                    health = {
                        "status": result.get("status"),
                        "error_code": result.get("error_code"),
                        "error": result.get("error"),
                    }
                    if _is_resource_missing_or_deleted(result):
                        health["resource_missing_or_deleted"] = True
                    return json.dumps({"resolved": False, "health": health}, default=str)
                cluster = (result.get("data", {}).get("CacheClusters") or [{}])[0]
                cluster_status = cluster.get("CacheClusterStatus", "unknown")
                health = {"status": cluster_status}
                resolved = cluster_status == "available"

        return json.dumps({"resolved": resolved, "health": health}, default=str)

    @tool
    def no_action() -> str:
        """Use when no safe automated action can resolve this issue.
        Call this when manual investigation or intervention is required.
        """
        return json.dumps({"action": "no_action", "resolved": False, "requires_manual": True})

    # Provide only the tools relevant to the detected service type
    always_available = [verify_service_health, no_action]
    service_tools = []

    if dimensions.get("ClusterName") and dimensions.get("ServiceName"):
        service_tools += [force_ecs_redeploy, scale_out_ecs]
    if dimensions.get("InstanceId"):
        service_tools += [start_ec2_instance, reboot_ec2_instance, scale_out_asg]
    if dimensions.get("DBInstanceIdentifier") or dimensions.get("DbClusterIdentifier"):
        service_tools += [reboot_rds_instance]
    if dimensions.get("CacheClusterId"):
        service_tools += [reboot_elasticache_cluster]

    # If no service-specific tools matched (unknown dimensions), offer all of them
    if not service_tools:
        service_tools = [force_ecs_redeploy, scale_out_ecs, start_ec2_instance,
                         reboot_ec2_instance, reboot_rds_instance, scale_out_asg,
                         reboot_elasticache_cluster]

    return service_tools + always_available
