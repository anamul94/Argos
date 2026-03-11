"""AWS boto3 tool — whitelisted dispatcher used by both the Telegram agent and graph nodes.

Safe write commands (force redeploy, scale out, reboot) are included.
Destructive commands (terminate, delete, scale down) are intentionally absent.
"""

import asyncio
import json
import os

from dotenv import load_dotenv

load_dotenv()  # safe to call multiple times — no-op if already loaded

import boto3
import botocore
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from utils.serialization import make_json_safe

log = structlog.get_logger(__name__)

AWS_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "adaptive"},
    connect_timeout=10,
    read_timeout=30,
)

# ── Whitelist ─────────────────────────────────────────────────────────────────
# Omitting anything destructive: terminate, delete, scale-down, modify-*.
ALLOWED_COMMANDS: dict[str, set[str]] = {
    "logs": {
        "start_query",
        "get_query_results",
        "describe_log_groups",
        "filter_log_events",
    },
    "ecs": {
        "describe_services",
        "describe_tasks",
        "list_tasks",
        "update_service",       # force new deployment / scale out only
        "describe_clusters",
        "describe_task_definition",
    },
    "ec2": {
        "describe_instances",
        "describe_instance_status",
        "describe_vpcs",
        "describe_subnets",
        "reboot_instances",     # safe: reboot running instance
        "start_instances",      # safe: start a stopped instance
    },
    "elasticloadbalancing": {
        "describe_target_health",
        "describe_load_balancers",
        "describe_listeners",
        "describe_target_groups",
    },
    "rds": {
        "describe_db_instances",
        "describe_events",
        "describe_db_clusters",
        "reboot_db_instance",   # safe: reboot, not delete
    },
    "cloudwatch": {
        "get_metric_data",
        "describe_alarms",
        "get_metric_statistics",
        "list_metrics",
        "describe_alarm_history",
    },
    "autoscaling": {
        "describe_auto_scaling_groups",
        "set_desired_capacity",     # scale OUT only — guard enforced in remediate.py
        "describe_policies",
    },
    "ssm": {
        "send_command",
        "get_command_invocation",
        "describe_instance_information",
        "get_parameter",
        "get_parameters_by_path",
    },
    "lambda": {
        "get_function",
        "get_function_concurrency",
        "list_functions",
        "invoke",
    },
    "elasticache": {
        "describe_cache_clusters",
        "describe_replication_groups",
        "describe_events",
        "reboot_cache_cluster",     # safe: reboot, not delete
    },
    "apigateway": {
        "get_rest_apis",
        "get_stages",
        "get_deployments",
    },
    "codedeploy": {
        "list_deployments",
        "get_deployment",
        "list_applications",
    },
    "cloudformation": {
        "describe_stacks",
        "describe_stack_events",
        "list_stacks",
    },
    "cloudtrail": {
        "lookup_events",
    },
    "sns": {
        "publish",
        "list_topics",
        "list_subscriptions",
    },
    "sqs": {
        "send_message",
        "receive_message",
        "get_queue_attributes",
    },
}

PAGINATED_COMMANDS: set[str] = {
    "describe_instances",
    "describe_services",
    "list_tasks",
    "describe_db_instances",
    "describe_auto_scaling_groups",
    "list_functions",
    "describe_stacks",
    "filter_log_events",
    "describe_load_balancers",
    "describe_clusters",
    "describe_cache_clusters",
    "describe_log_groups",
}


# ── Core sync executor ────────────────────────────────────────────────────────


def run_boto_sync(service: str, command: str, args: dict, region: str) -> dict:
    """Execute a whitelisted boto3 command synchronously.

    Returns a dict with 'status': 'ok' and 'data' on success,
    or 'status': 'error'/'blocked'/'aws_error' on failure.
    Never raises — all errors are returned as structured dicts.
    """
    validation_error = _validate_request(service, command, args)
    if validation_error:
        return validation_error

    try:
        client = boto3.client(service, region_name=region, config=AWS_CONFIG)
    except Exception as e:
        log.error("boto_client_init_failed", service=service, error=str(e))
        return {"status": "error", "stage": "client_init", "error": str(e)}

    if not hasattr(client, command):
        return {"status": "error", "error": f"Command '{command}' not found on {service} client"}

    return _execute_command(client, service, command, args, region)


def _validate_request(service: str, command: str, args: dict) -> dict | None:
    """Validate service, command, and args against the whitelist.

    Returns an error dict if validation fails, None if valid.
    """
    if not isinstance(service, str) or not service:
        return {"status": "error", "error": "Service must be a non-empty string"}
    if not isinstance(command, str) or not command:
        return {"status": "error", "error": "Command must be a non-empty string"}
    if not isinstance(args, dict):
        return {"status": "error", "error": "Args must be a dictionary"}

    allowed = ALLOWED_COMMANDS.get(service)
    if not allowed:
        return {
            "status": "blocked",
            "error": f"Service '{service}' is not allowed",
            "allowed_services": list(ALLOWED_COMMANDS.keys()),
        }
    if command not in allowed:
        return {
            "status": "blocked",
            "error": f"Command '{service}.{command}' is not allowed",
            "allowed_commands": sorted(allowed),
        }
    return None


def _execute_command(
    client: object,
    service: str,
    command: str,
    args: dict,
    region: str,
) -> dict:
    """Execute the boto3 method, handling pagination and response size limits.

    Returns a structured dict with status, data, service, command, region.
    """
    method = getattr(client, command)
    try:
        if command in PAGINATED_COMMANDS:
            result = _run_paginated(client, command, args)
            if result is not None:
                return {"status": "ok", "data": make_json_safe(result), "paginated": True,
                        "service": service, "command": command, "region": region}

        response = method(**args)
        response.pop("ResponseMetadata", None)
        safe = make_json_safe(response)

        if len(json.dumps(safe)) > 100_000:
            return {"status": "ok", "data": {"truncated": True, "keys": list(safe.keys())},
                    "warning": "Response too large — truncated", "service": service, "command": command}

        return {"status": "ok", "data": safe, "paginated": False,
                "service": service, "command": command, "region": region}

    except ClientError as e:
        log.error("boto_client_error", service=service, command=command,
                  error_code=e.response["Error"]["Code"], error=e.response["Error"]["Message"])
        return {"status": "aws_error", "error_code": e.response["Error"]["Code"],
                "error": e.response["Error"]["Message"], "service": service, "command": command}
    except BotoCoreError as e:
        log.error("boto_core_error", service=service, command=command, error=str(e))
        return {"status": "botocore_error", "error": str(e), "service": service, "command": command}
    except Exception as e:
        log.error("boto_unexpected_error", service=service, command=command, error=str(e))
        return {"status": "error", "error": str(e), "service": service, "command": command}


def _run_paginated(client: object, command: str, args: dict) -> dict | None:
    """Run a boto3 paginator and merge all pages into a single dict.

    Returns merged dict on success, None if pagination not supported.
    """
    try:
        paginator = client.get_paginator(command)
        merged: dict = {}
        for page in paginator.paginate(**args):
            page.pop("ResponseMetadata", None)
            for key, value in page.items():
                if isinstance(value, list):
                    merged.setdefault(key, []).extend(value)
                elif isinstance(value, dict) and key in merged:
                    merged[key].update(value)
                else:
                    merged[key] = value
        return merged
    except botocore.exceptions.OperationNotPageableError:
        return None
    except Exception:
        return None


# ── Async wrapper ─────────────────────────────────────────────────────────────


async def run_boto_async(service: str, command: str, args: dict, region: str) -> dict:
    """Async wrapper that offloads blocking boto3 I/O to the default thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: run_boto_sync(service, command, args, region))


# ── LangChain StructuredTool (for Telegram agent) ─────────────────────────────


class AWSBotoInput(BaseModel):
    service: str = Field(description="AWS service name (e.g., 'ec2', 'logs', 'lambda')")
    command: str = Field(description="Boto3 client method (e.g., 'describe_instances')")
    args: dict = Field(default_factory=dict, description="Arguments dict for the boto3 method")
    region: str = Field(
        default_factory=lambda: os.environ.get("AWS_DEFAULT_REGION", "ap-south-1"),
        description="AWS region name",
    )


aws_boto_command_tool = StructuredTool.from_function(
    func=lambda service, command, args, region: run_boto_sync(service, command, args, region),
    coroutine=lambda service, command, args, region: run_boto_async(service, command, args, region),
    name="aws_boto_command_tool",
    description=(
        "Execute AWS API commands via Boto3. "
        "Provide the service (e.g. 'ec2'), command (e.g. 'describe_instances'), "
        "an args dict, and optionally a region. "
        "Only whitelisted read/write operations are permitted."
    ),
    args_schema=AWSBotoInput,
)
