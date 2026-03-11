"""gather_evidence node — collects CloudWatch metrics, logs, and service health.

Routes service-specific health checks based on state['service_type'].
All boto3 calls go through run_boto_sync from tools/aws_boto.py.
"""

import time

import structlog

from models.alert_state import AlertTriageState
from tools.aws_boto import run_boto_sync

log = structlog.get_logger(__name__)

# CloudWatch Logs Insights: how long to poll for results (seconds)
_LOG_QUERY_TIMEOUT = 30
_LOG_QUERY_POLL_INTERVAL = 2


def gather_evidence(state: AlertTriageState) -> dict:
    """Collect all available evidence for the alert.

    Returns delta dict with metric_data, alarm_history, recent_logs,
    and service_health populated.
    """
    region = state["region"]
    service_type = state["service_type"]
    alert_id = state["alert_id"]

    log.info("gather_evidence_started", alert_id=alert_id, service_type=service_type)

    metric_data = _fetch_metric_data(state=state, region=region)
    alarm_history = _fetch_alarm_history(alarm_name=state["alarm_name"], region=region)
    recent_logs = _fetch_recent_logs(state=state, region=region)
    service_health = _dispatch_service_health(state=state, region=region)

    log.info("gather_evidence_complete", alert_id=alert_id,
             log_lines=len(recent_logs), service_health_keys=list(service_health.keys()))

    return {
        "metric_data": metric_data,
        "alarm_history": alarm_history,
        "recent_logs": recent_logs,
        "service_health": service_health,
        "actions_taken": [
            f"[gather_evidence] Collected metrics, {len(recent_logs)} log lines, "
            f"and {service_type} service health"
        ],
    }


# ── Metric & alarm history ────────────────────────────────────────────────────


def _fetch_metric_data(state: AlertTriageState, region: str) -> dict:
    """Fetch the last 30 minutes of metric data for the alarming metric."""
    end_time = int(time.time())
    start_time = end_time - 1800  # 30 minutes

    result = run_boto_sync(
        service="cloudwatch",
        command="get_metric_statistics",
        args={
            "Namespace": state["metric_namespace"],
            "MetricName": state["metric_name"],
            "Dimensions": [{"Name": k, "Value": v} for k, v in state["dimensions"].items()],
            "StartTime": start_time,
            "EndTime": end_time,
            "Period": 300,
            "Statistics": ["Average", "Maximum", "Minimum"],
        },
        region=region,
    )
    return result.get("data", {}) if result.get("status") == "ok" else {"error": result.get("error")}


def _fetch_alarm_history(alarm_name: str, region: str) -> list[dict]:
    """Fetch the last 10 state-change history entries for the alarm."""
    result = run_boto_sync(
        service="cloudwatch",
        command="describe_alarm_history",
        args={"AlarmName": alarm_name, "HistoryItemType": "StateUpdate", "MaxRecords": 10},
        region=region,
    )
    if result.get("status") != "ok":
        return []
    items = result.get("data", {}).get("AlarmHistoryItems", [])
    return [{"timestamp": i.get("Timestamp"), "summary": i.get("HistorySummary")} for i in items]


# ── Log fetching ──────────────────────────────────────────────────────────────


def _fetch_recent_logs(state: AlertTriageState, region: str) -> list[str]:
    """Query CloudWatch Logs Insights for recent errors related to the service.

    Tries common log group name patterns. Returns up to 50 log lines.
    """
    log_groups = _candidate_log_groups(
        service_type=state["service_type"],
        dimensions=state["dimensions"],
    )
    end_time = int(time.time())
    start_time = end_time - 900  # 15 minutes

    for group in log_groups:
        lines = _run_logs_insights_query(
            log_group=group,
            start_time=start_time,
            end_time=end_time,
            region=region,
            alert_id=state["alert_id"],
        )
        if lines:
            return lines[:50]
    return []


def _candidate_log_groups(service_type: str, dimensions: dict) -> list[str]:
    """Return candidate CloudWatch log group names for the service type."""
    service_name = dimensions.get("ServiceName", dimensions.get("FunctionName", ""))
    cluster_name = dimensions.get("ClusterName", "")
    db_id = dimensions.get("DBInstanceIdentifier", dimensions.get("DbClusterIdentifier", ""))
    instance_id = dimensions.get("InstanceId", "")

    patterns: dict[str, list[str]] = {
        "ecs": [f"/ecs/{service_name}", f"/aws/ecs/{cluster_name}/{service_name}"],
        "lambda": [f"/aws/lambda/{service_name}"],
        "rds": [f"/aws/rds/instance/{db_id}/error", f"/aws/rds/cluster/{db_id}/error"],
        "ec2": [f"/var/log/syslog/{instance_id}", f"/var/log/messages/{instance_id}"],
        "alb": [f"/aws/elasticloadbalancing/app/{service_name}"],
    }
    return [g for g in patterns.get(service_type, []) if g and not g.endswith("/")]


def _run_logs_insights_query(
    log_group: str,
    start_time: int,
    end_time: int,
    region: str,
    alert_id: str,
) -> list[str]:
    """Start a Logs Insights query and poll until complete.

    Returns list of formatted log lines, or empty list on failure.
    """
    start_result = run_boto_sync(
        service="logs",
        command="start_query",
        args={
            "logGroupName": log_group,
            "startTime": start_time,
            "endTime": end_time,
            "queryString": (
                "fields @timestamp, @message, @logStream "
                "| filter @message like /ERROR|WARN|Exception|error|warn|FATAL/ "
                "| sort @timestamp desc | limit 50"
            ),
            "limit": 50,
        },
        region=region,
    )
    if start_result.get("status") != "ok":
        return []

    query_id = start_result.get("data", {}).get("queryId", "")
    if not query_id:
        return []

    return _poll_query_results(query_id=query_id, region=region, alert_id=alert_id)


def _poll_query_results(query_id: str, region: str, alert_id: str) -> list[str]:
    """Poll Logs Insights for query results until Complete or timeout."""
    deadline = time.time() + _LOG_QUERY_TIMEOUT
    while time.time() < deadline:
        result = run_boto_sync(
            service="logs",
            command="get_query_results",
            args={"queryId": query_id},
            region=region,
        )
        if result.get("status") != "ok":
            return []
        data = result.get("data", {})
        if data.get("status") == "Complete":
            return [
                " | ".join(f["value"] for f in row if f.get("field") != "@ptr")
                for row in data.get("results", [])
            ]
        time.sleep(_LOG_QUERY_POLL_INTERVAL)
    log.warning("logs_insights_query_timeout", alert_id=alert_id, query_id=query_id)
    return []


# ── Service-specific health checks ───────────────────────────────────────────


def _dispatch_service_health(state: AlertTriageState, region: str) -> dict:
    """Route to the correct service health checker based on service_type."""
    dispatch = {
        "ecs": _fetch_ecs_health,
        "ec2": _fetch_ec2_health,
        "rds": _fetch_rds_health,
        "lambda": _fetch_lambda_health,
        "alb": _fetch_alb_health,
        "elasticache": _fetch_elasticache_health,
    }
    handler = dispatch.get(state["service_type"])
    if not handler:
        return {}
    try:
        return handler(dimensions=state["dimensions"], region=region)
    except Exception as e:
        log.error("service_health_fetch_failed", service_type=state["service_type"],
                  alert_id=state["alert_id"], error=str(e))
        return {"error": str(e)}


def _fetch_ecs_health(dimensions: dict, region: str) -> dict:
    """Fetch ECS service status, running/desired task counts, and recent stopped tasks."""
    cluster = dimensions.get("ClusterName", "")
    service = dimensions.get("ServiceName", "")
    if not cluster or not service:
        return {}

    svc_result = run_boto_sync("ecs", "describe_services",
                               {"cluster": cluster, "services": [service]}, region)
    tasks_result = run_boto_sync("ecs", "list_tasks",
                                 {"cluster": cluster, "serviceName": service, "desiredStatus": "RUNNING"}, region)

    services = svc_result.get("data", {}).get("Services", [{}])
    svc = services[0] if services else {}
    return {
        "desired_count": svc.get("desiredCount", 0),
        "running_count": svc.get("runningCount", 0),
        "pending_count": svc.get("pendingCount", 0),
        "status": svc.get("status", "unknown"),
        "deployment_status": [d.get("status") for d in svc.get("deployments", [])],
        "running_task_arns": tasks_result.get("data", {}).get("taskArns", []),
    }


def _fetch_ec2_health(dimensions: dict, region: str) -> dict:
    """Fetch EC2 instance status and ASG details if applicable."""
    instance_id = dimensions.get("InstanceId", "")
    if not instance_id:
        return {}

    status_result = run_boto_sync("ec2", "describe_instance_status",
                                  {"InstanceIds": [instance_id], "IncludeAllInstances": True}, region)
    statuses = status_result.get("data", {}).get("InstanceStatuses", [{}])
    status = statuses[0] if statuses else {}
    return {
        "instance_state": status.get("InstanceState", {}).get("Name", "unknown"),
        "instance_status": status.get("InstanceStatus", {}).get("Status", "unknown"),
        "system_status": status.get("SystemStatus", {}).get("Status", "unknown"),
    }


def _fetch_rds_health(dimensions: dict, region: str) -> dict:
    """Fetch RDS instance status, engine version, and recent error events."""
    db_id = dimensions.get("DBInstanceIdentifier", dimensions.get("DbClusterIdentifier", ""))
    if not db_id:
        return {}

    db_result = run_boto_sync("rds", "describe_db_instances", {"DBInstanceIdentifier": db_id}, region)
    instances = db_result.get("data", {}).get("DBInstances", [{}])
    db = instances[0] if instances else {}

    events_result = run_boto_sync("rds", "describe_events",
                                  {"SourceIdentifier": db_id, "SourceType": "db-instance", "Duration": 60}, region)

    return {
        "db_status": db.get("DBInstanceStatus", "unknown"),
        "engine": db.get("Engine", "unknown"),
        "engine_version": db.get("EngineVersion", "unknown"),
        "multi_az": db.get("MultiAZ", False),
        "recent_events": [e.get("Message") for e in events_result.get("data", {}).get("Events", [])[:5]],
    }


def _fetch_lambda_health(dimensions: dict, region: str) -> dict:
    """Fetch Lambda function configuration and concurrency limits."""
    function_name = dimensions.get("FunctionName", "")
    if not function_name:
        return {}

    fn_result = run_boto_sync("lambda", "get_function", {"FunctionName": function_name}, region)
    conc_result = run_boto_sync("lambda", "get_function_concurrency", {"FunctionName": function_name}, region)

    fn = fn_result.get("data", {}).get("Configuration", {})
    return {
        "runtime": fn.get("Runtime", "unknown"),
        "memory_size": fn.get("MemorySize", 0),
        "timeout": fn.get("Timeout", 0),
        "last_modified": fn.get("LastModified", "unknown"),
        "state": fn.get("State", "unknown"),
        "reserved_concurrency": conc_result.get("data", {}).get("ReservedConcurrentExecutions"),
    }


def _fetch_alb_health(dimensions: dict, region: str) -> dict:
    """Fetch ALB target group health and listener counts."""
    load_balancer = dimensions.get("LoadBalancer", "")
    target_group = dimensions.get("TargetGroup", "")
    if not target_group:
        return {"load_balancer": load_balancer}

    tg_arn = f"arn:aws:elasticloadbalancing:{target_group}"
    health_result = run_boto_sync("elasticloadbalancing", "describe_target_health",
                                  {"TargetGroupArn": tg_arn}, region)
    healths = health_result.get("data", {}).get("TargetHealthDescriptions", [])
    healthy = sum(1 for h in healths if h.get("TargetHealth", {}).get("State") == "healthy")
    return {
        "total_targets": len(healths),
        "healthy_targets": healthy,
        "unhealthy_targets": len(healths) - healthy,
    }


def _fetch_elasticache_health(dimensions: dict, region: str) -> dict:
    """Fetch ElastiCache cluster status and node health."""
    cluster_id = dimensions.get("CacheClusterId", dimensions.get("ReplicationGroupId", ""))
    if not cluster_id:
        return {}

    result = run_boto_sync("elasticache", "describe_cache_clusters",
                           {"CacheClusterId": cluster_id, "ShowCacheNodeInfo": True}, region)
    clusters = result.get("data", {}).get("CacheClusters", [{}])
    cluster = clusters[0] if clusters else {}
    return {
        "cluster_status": cluster.get("CacheClusterStatus", "unknown"),
        "engine": cluster.get("Engine", "unknown"),
        "num_cache_nodes": cluster.get("NumCacheNodes", 0),
        "node_statuses": [n.get("CacheNodeStatus") for n in cluster.get("CacheNodes", [])],
    }
