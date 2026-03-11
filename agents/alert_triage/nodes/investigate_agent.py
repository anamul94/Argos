"""investigate_agent node — ReAct agent with TodoListMiddleware for dynamic investigation.

Replaces gather_evidence + analyze_root_cause with an autonomous loop that:
  1. Writes an investigation plan (todo list) before executing any steps
  2. Calls AWS investigation tools dynamically based on what evidence reveals
  3. Follows cross-service leads (e.g., ECS logs showing DB errors → checks RDS)
  4. Synthesises a structured RootCauseAnalysis at the end

Middleware stack:
  - TodoListMiddleware  : gives the agent a write_todos tool + planning guidance
  - ToolCallLimitMiddleware(run_limit=15) : hard cap to prevent runaway loops

All tools are read-only AWS operations — no writes happen here.
"""

import json
import time

import structlog
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from models.alert_state import AlertTriageState
from tools.aws_boto import run_boto_sync
from utils.llm import get_bedrock_llm

load_dotenv()

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are an AWS cloud operations expert investigating a CloudWatch alarm.

Use the write_todos tool to plan your investigation before executing any steps.

## Investigation rules
- Always check the primary service first (use dimensions from the alarm context).
- Always search recent logs for the affected service. 
  - If `LogGroup` is provided in the Context metadata, you MUST use that exact name.
  - If no metadata is provided, use the `list_log_groups` tool with a prefix like `/argos/`, `/aws/ecs/` or `/aws/lambda/` to discover the exact name securely. Do not guess.
- If logs or metrics point to a dependency issue (e.g., "connection refused to postgres"),
  add a new todo and investigate that service too.
- Stop when you can state the root cause with confidence.
- End with a clear summary: root cause, confidence (high/medium/low), contributing factors."""


class RootCauseAnalysis(BaseModel):
    root_cause: str = Field(description="One sentence root cause")
    confidence: str = Field(description="high | medium | low")
    contributing_factors: list[str] = Field(description="Observed contributing factors from evidence")
    reasoning: str = Field(description="Step-by-step reasoning from evidence to conclusion")


def investigate_agent(state: AlertTriageState) -> dict:
    """ReAct investigation agent with TodoListMiddleware for structured planning."""
    alert_id = state["alert_id"]
    region = state["region"]
    log.info("investigate_agent_started", alert_id=alert_id, service=state["service_type"])

    investigation_tools = _build_investigation_tools(region)
    llm = get_bedrock_llm()

    agent = create_agent(
        model=llm,
        tools=investigation_tools,
        system_prompt=_SYSTEM_PROMPT,
        middleware=[
            TodoListMiddleware(),
            ToolCallLimitMiddleware(run_limit=15),
        ],
    )

    try:
        result = agent.invoke({"messages": [HumanMessage(content=_build_initial_message(state))]})
        messages = result.get("messages", [])
        tool_calls_made = sum(1 for m in messages if hasattr(m, "tool_calls") and m.tool_calls)
        analysis = _synthesise_rca(state, messages)
        log.info("investigate_agent_complete", alert_id=alert_id,
                 confidence=analysis.confidence, tool_calls=tool_calls_made)
    except Exception as e:
        log.error("investigate_agent_failed", alert_id=alert_id, error=str(e))
        analysis = RootCauseAnalysis(
            root_cause="Investigation failed — unable to determine root cause",
            confidence="low",
            contributing_factors=[f"Agent error: {str(e)[:200]}"],
            reasoning="Investigation agent encountered an unexpected error.",
        )
        tool_calls_made = 0

    return {
        "root_cause": analysis.root_cause,
        "confidence": analysis.confidence,
        "contributing_factors": analysis.contributing_factors,
        "llm_reasoning": [f"[investigate_agent] {analysis.reasoning}"],
        "actions_taken": [
            f"[investigate_agent] {tool_calls_made} tool calls — "
            f"root cause: {analysis.root_cause} (confidence: {analysis.confidence})"
        ],
    }


def _build_initial_message(state: AlertTriageState) -> str:
    # Only pass what the agent actually needs to know about the alarm initially.
    # We DO NOT pass raw_payload (too big) or alarm_history (agent can fetch this if needed).
    context = {
        "alarm_name": state["alarm_name"],
        "alarm_reason": state["alarm_reason"],
        "severity": state["severity"],
        "service_type": state["service_type"],
        "metric_name": state["metric_name"],
        "metric_namespace": state["metric_namespace"],
        "dimensions": state["dimensions"],
        "account_id": state["account_id"],
        "region": state["region"],
        "metadata": state.get("metadata", {}),
    }
    return (
        f"## CloudWatch Alarm Fired\n"
        f"Here is the alarm context:\n"
        f"{json.dumps(context, indent=2)}\n\n"
        f"Write your investigation plan, then execute it."
    )


def _synthesise_rca(state: AlertTriageState, messages: list) -> RootCauseAnalysis:
    """Call LLM with structured output to extract a clean RCA from the agent conversation."""
    observations = [
        f"[{m.name}]: {str(m.content)[:500]}"
        for m in messages if isinstance(m, ToolMessage) and m.name != "write_todos"
    ]
    final_summary = next(
        (str(m.content)[:2000] for m in reversed(messages)
         if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None)),
        ""
    )
    prompt = (
        f"Extract a structured root cause analysis from this AWS investigation.\n\n"
        f"Alarm: {state['alarm_name']} | Service: {state['service_type']} | Metric: {state['metric_name']}\n\n"
        f"Tool results collected:\n" + "\n".join(observations[:20]) + "\n\n"
        f"Agent conclusion:\n{final_summary}\n\n"
        f"Provide root_cause (one sentence), confidence (high/medium/low), "
        f"contributing_factors (list), and reasoning (step-by-step)."
    )
    try:
        llm = get_bedrock_llm(structured_output_schema=RootCauseAnalysis)
        return llm.invoke(prompt)
    except Exception as e:
        log.warning("rca_synthesis_failed", error=str(e))
        return RootCauseAnalysis(
            root_cause=final_summary[:200] if final_summary else "Unknown",
            confidence="low",
            contributing_factors=["RCA synthesis failed"],
            reasoning=final_summary[:500] if final_summary else "No agent summary available",
        )


# ── Investigation tools (all read-only AWS operations) ────────────────────────

def _build_investigation_tools(region: str) -> list:
    """Build LangChain tools for read-only AWS investigation."""

    @tool
    def get_metric_data(namespace: str, metric_name: str, dimensions_json: str, minutes_back: int = 30) -> str:
        """Fetch CloudWatch metric statistics (Average/Max/Min) for any metric.

        dimensions_json: JSON string e.g. '{"ClusterName":"prod","ServiceName":"api"}'
        minutes_back: how far back to look (default 30 minutes)
        """
        try:
            dims = json.loads(dimensions_json) if dimensions_json else {}
        except Exception:
            dims = {}
        end_time = int(time.time())
        result = run_boto_sync(
            "cloudwatch", "get_metric_statistics",
            {
                "Namespace": namespace, "MetricName": metric_name,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                "StartTime": end_time - minutes_back * 60, "EndTime": end_time,
                "Period": 300, "Statistics": ["Average", "Maximum", "Minimum"],
            },
            region,
        )
        return json.dumps(result.get("data", result.get("error", "no data")), default=str)[:2000]

    @tool
    def get_alarm_history(alarm_name: str) -> str:
        """Fetch CloudWatch alarm state-change history (last 10 entries).

        Useful for detecting flapping alarms or recurring issues.
        """
        result = run_boto_sync(
            "cloudwatch", "describe_alarm_history",
            {"AlarmName": alarm_name, "HistoryItemType": "StateUpdate", "MaxRecords": 10},
            region,
        )
        items = result.get("data", {}).get("AlarmHistoryItems", [])
        return json.dumps(
            [{"timestamp": i.get("Timestamp"), "summary": i.get("HistorySummary")} for i in items],
            default=str
        )[:1500]

    @tool
    def list_log_groups(prefix: str = "") -> str:
        """List available CloudWatch Log Groups.
        
        Use this when you don't know the exact log group name for a service.
        Provide a prefix like '/aws/ecs/', '/aws/lambda/', or '/ec2/' to filter.
        """
        args = {}
        if prefix:
            args["logGroupNamePrefix"] = prefix
        result = run_boto_sync("logs", "describe_log_groups", args, region)
        if result.get("status") != "ok":
            return f"Failed to list log groups: {result.get('error', 'unknown error')}"
        groups = [g.get("logGroupName") for g in result.get("data", {}).get("logGroups", [])]
        if not groups:
            return f"No log groups found matching prefix '{prefix}'"
        return "\n".join(groups[:50])

    @tool
    def search_logs(log_group: str, keywords: str = "ERROR|WARN|Exception|FATAL", minutes_back: int = 15) -> str:
        """Search CloudWatch Logs Insights for recent errors or warnings.

        log_group: the log group name, e.g. '/ecs/my-service' or '/aws/lambda/fn-name'
        keywords: pipe-separated filter patterns (default: errors and warnings)
        minutes_back: how far back to search (default 15 minutes)
        """
        end_time = int(time.time())
        start_result = run_boto_sync(
            "logs", "start_query",
            {
                "logGroupName": log_group,
                "startTime": end_time - minutes_back * 60, "endTime": end_time,
                "queryString": (
                    f"fields @timestamp, @message "
                    f"| filter @message like /{keywords}/ "
                    f"| sort @timestamp desc | limit 30"
                ),
                "limit": 30,
            },
            region,
        )
        if start_result.get("status") != "ok":
            return f"Log query failed for {log_group}: {start_result.get('error', 'unknown')}"
        query_id = start_result.get("data", {}).get("queryId", "")
        if not query_id:
            return "No query ID returned"
        deadline = time.time() + 30
        while time.time() < deadline:
            r = run_boto_sync("logs", "get_query_results", {"queryId": query_id}, region)
            if r.get("status") == "ok" and r.get("data", {}).get("status") == "Complete":
                lines = [
                    " | ".join(f["value"] for f in row if f.get("field") != "@ptr")
                    for row in r.get("data", {}).get("results", [])
                ]
                return "\n".join(lines[:30]) or "No matching log entries found"
            time.sleep(2)
        return "Log query timed out"

    @tool
    def check_ecs_health(cluster_name: str, service_name: str) -> str:
        """Get ECS service health: desired/running/pending task counts and deployment status."""
        result = run_boto_sync(
            "ecs", "describe_services",
            {"cluster": cluster_name, "services": [service_name]},
            region,
        )
        svc = (result.get("data", {}).get("Services") or [{}])[0]
        return json.dumps({
            "desired": svc.get("desiredCount", 0),
            "running": svc.get("runningCount", 0),
            "pending": svc.get("pendingCount", 0),
            "status": svc.get("status", "unknown"),
            "deployments": [
                {"status": d.get("status"), "running": d.get("runningCount")}
                for d in svc.get("deployments", [])
            ],
        }, default=str)

    @tool
    def check_ec2_health(instance_id: str) -> str:
        """Get EC2 instance state, instance status check, and system status check."""
        result = run_boto_sync(
            "ec2", "describe_instance_status",
            {"InstanceIds": [instance_id], "IncludeAllInstances": True},
            region,
        )
        status = (result.get("data", {}).get("InstanceStatuses") or [{}])[0]
        return json.dumps({
            "instance_state": status.get("InstanceState", {}).get("Name", "unknown"),
            "instance_status": status.get("InstanceStatus", {}).get("Status", "unknown"),
            "system_status": status.get("SystemStatus", {}).get("Status", "unknown"),
        }, default=str)

    @tool
    def check_rds_health(db_instance_id: str) -> str:
        """Get RDS DB instance status, engine version, and recent events.

        Use this when logs show database connection errors or timeouts.
        """
        db_result = run_boto_sync(
            "rds", "describe_db_instances",
            {"DBInstanceIdentifier": db_instance_id},
            region,
        )
        db = (db_result.get("data", {}).get("DBInstances") or [{}])[0]
        events = run_boto_sync(
            "rds", "describe_events",
            {"SourceIdentifier": db_instance_id, "SourceType": "db-instance", "Duration": 60},
            region,
        )
        return json.dumps({
            "status": db.get("DBInstanceStatus", "unknown"),
            "engine": f"{db.get('Engine', '?')} {db.get('EngineVersion', '?')}",
            "multi_az": db.get("MultiAZ", False),
            "recent_events": [
                e.get("Message") for e in (events.get("data", {}).get("Events") or [])[:5]
            ],
        }, default=str)

    @tool
    def check_lambda_health(function_name: str) -> str:
        """Get Lambda function state, runtime, memory, timeout, and reserved concurrency."""
        fn_result = run_boto_sync("lambda", "get_function", {"FunctionName": function_name}, region)
        conc = run_boto_sync("lambda", "get_function_concurrency", {"FunctionName": function_name}, region)
        fn = fn_result.get("data", {}).get("Configuration", {})
        return json.dumps({
            "state": fn.get("State", "unknown"),
            "runtime": fn.get("Runtime", "unknown"),
            "memory_mb": fn.get("MemorySize", 0),
            "timeout_s": fn.get("Timeout", 0),
            "reserved_concurrency": conc.get("data", {}).get("ReservedConcurrentExecutions"),
        }, default=str)

    @tool
    def check_alb_health(target_group_arn: str) -> str:
        """Get ALB target group health: healthy vs unhealthy counts and failure reasons."""
        result = run_boto_sync(
            "elasticloadbalancing", "describe_target_health",
            {"TargetGroupArn": target_group_arn},
            region,
        )
        healths = result.get("data", {}).get("TargetHealthDescriptions", [])
        healthy = sum(1 for h in healths if h.get("TargetHealth", {}).get("State") == "healthy")
        return json.dumps({
            "total": len(healths), "healthy": healthy, "unhealthy": len(healths) - healthy,
            "details": [
                {"state": h.get("TargetHealth", {}).get("State"),
                 "reason": h.get("TargetHealth", {}).get("Reason")}
                for h in healths[:5]
            ],
        }, default=str)

    @tool
    def check_elasticache_health(cluster_id: str) -> str:
        """Get ElastiCache cluster status and per-node health."""
        result = run_boto_sync(
            "elasticache", "describe_cache_clusters",
            {"CacheClusterId": cluster_id, "ShowCacheNodeInfo": True},
            region,
        )
        cluster = (result.get("data", {}).get("CacheClusters") or [{}])[0]
        return json.dumps({
            "status": cluster.get("CacheClusterStatus", "unknown"),
            "engine": f"{cluster.get('Engine', '?')} {cluster.get('EngineVersion', '?')}",
            "num_nodes": cluster.get("NumCacheNodes", 0),
            "node_statuses": [n.get("CacheNodeStatus") for n in cluster.get("CacheNodes", [])],
        }, default=str)

    return [
        get_metric_data,
        get_alarm_history,
        list_log_groups,
        search_logs,
        check_ecs_health,
        check_ec2_health,
        check_rds_health,
        check_lambda_health,
        check_alb_health,
        check_elasticache_health,
    ]
