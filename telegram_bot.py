from fastapi import FastAPI, Request
from dotenv import load_dotenv
import os
import httpx
import asyncio
import json
import re
import html
import datetime as dt
from decimal import Decimal
import boto3
import botocore
from botocore.exceptions import ClientError, BotoCoreError
from botocore.config import Config
from pydantic import BaseModel, Field
from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

load_dotenv()
app = FastAPI()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ── Constants ────────────────────────────────────────────────────────────────

ALLOWED_COMMANDS = {
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
        "update_service",
        "describe_clusters",
    },
    "ec2": {
        "describe_instances",
        "describe_instance_status",
        "describe_vpcs",
        "describe_subnets",
    },
    "elasticloadbalancing": {
        "describe_target_health",
        "describe_load_balancers",
        "describe_listeners",
    },
    "rds": {"describe_db_instances", "describe_events", "describe_db_clusters"},
    "cloudwatch": {
        "get_metric_data",
        "describe_alarms",
        "get_metric_statistics",
        "list_metrics",
    },
    "autoscaling": {
        "describe_auto_scaling_groups",
        "set_desired_capacity",
        "describe_policies",
    },
    "ssm": {"send_command", "get_command_invocation", "describe_instance_information"},
    "lambda": {"get_function", "get_function_concurrency", "list_functions", "invoke"},
    "codedeploy": {"list_deployments", "get_deployment", "list_applications"},
    "cloudformation": {"describe_stacks", "describe_stack_events", "list_stacks"},
    "sns": {"publish", "list_topics", "list_subscriptions"},
    "sqs": {"send_message", "receive_message", "get_queue_attributes"},
    "s3": {"list_buckets", "list_objects_v2", "get_object", "put_object"},
}

AWS_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "adaptive"},
    connect_timeout=10,
    read_timeout=30,
)

PAGINATED_COMMANDS = {
    "describe_instances",
    "describe_services",
    "list_tasks",
    "describe_db_instances",
    "describe_auto_scaling_groups",
    "list_functions",
    "describe_stacks",
    "filter_log_events",
    "list_objects_v2",
    "list_buckets",
    "describe_load_balancers",
    "describe_clusters",
}


# ── Pydantic schema ──────────────────────────────────────────────────────────


class AWSBotoInput(BaseModel):
    service: str = Field(description="AWS service name (e.g., 'ec2', 'logs', 'lambda')")
    command: str = Field(description="Boto3 client method (e.g., 'describe_instances')")
    args: dict = Field(
        default_factory=dict, description="Arguments dict for the boto3 method"
    )
    region: str = Field(default="ap-south-1", description="AWS region name")


# ── Sync boto3 core logic ────────────────────────────────────────────────────


def run_boto_sync(service: str, command: str, args: dict, region: str) -> dict:
    """Blocking boto3 execution — runs inside a thread pool executor."""

    # 1. Input validation
    if not isinstance(service, str) or not service:
        return {"status": "error", "error": "Service must be a non-empty string"}
    if not isinstance(command, str) or not command:
        return {"status": "error", "error": "Command must be a non-empty string"}
    if not isinstance(args, dict):
        return {"status": "error", "error": "Args must be a dictionary"}

    # 2. Whitelist check
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

    # 3. Build client
    try:
        client = boto3.client(service, region_name=region, config=AWS_CONFIG)
    except Exception as e:
        return {
            "status": "error",
            "stage": "client_init",
            "error": str(e),
            "error_type": type(e).__name__,
        }

    # 4. Validate method exists
    if not hasattr(client, command):
        return {
            "status": "error",
            "error": f"Command '{command}' not found on {service} client",
        }

    method = getattr(client, command)

    # 5. Execute (paginated or regular)
    try:
        if command in PAGINATED_COMMANDS:
            try:
                paginator = client.get_paginator(command)
                merged = {}
                for page in paginator.paginate(**args):
                    page.pop("ResponseMetadata", None)
                    for key, value in page.items():
                        if isinstance(value, list):
                            merged.setdefault(key, []).extend(value)
                        elif isinstance(value, dict) and key in merged:
                            merged[key].update(value)
                        else:
                            merged[key] = value
                safe_merged = _make_json_safe(merged)
                return {
                    "status": "ok",
                    "data": safe_merged,
                    "paginated": True,
                    "service": service,
                    "command": command,
                    "region": region,
                }
            except botocore.exceptions.OperationNotPageableError:
                pass  # fall through to regular call
            except Exception:
                pass  # paginator failed, fall through

        # Regular (non-paginated) call
        response = method(**args)
        response.pop("ResponseMetadata", None)

        safe_response = _make_json_safe(response)
        response_size = len(json.dumps(safe_response))
        if response_size > 100_000:
            return {
                "status": "ok",
                "data": {"truncated": True, "keys": list(safe_response.keys())},
                "warning": f"Response too large ({response_size} bytes), truncated",
                "service": service,
                "command": command,
            }

        return {
            "status": "ok",
            "data": safe_response,
            "paginated": False,
            "service": service,
            "command": command,
            "region": region,
        }

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_msg = e.response["Error"]["Message"]
        friendly_errors = {
            "AccessDenied": "Permission denied. Check IAM policies.",
            "InvalidParameterValue": "Invalid parameter provided.",
            "ResourceNotFound": "The requested resource was not found.",
            "Throttling": "AWS rate limit hit. Please retry.",
            "InvalidInstanceID.NotFound": "EC2 instance not found.",
            "NoSuchBucket": "S3 bucket does not exist.",
        }
        return {
            "status": "aws_error",
            "error_code": error_code,
            "error": error_msg,
            "friendly_message": friendly_errors.get(
                error_code, "AWS API error occurred"
            ),
            "service": service,
            "command": command,
        }
    except BotoCoreError as e:
        return {
            "status": "botocore_error",
            "error": str(e),
            "error_type": type(e).__name__,
            "service": service,
            "command": command,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__,
            "service": service,
            "command": command,
            "stage": "execution",
        }


def _make_json_safe(obj: object) -> object:
    """Convert common non-JSON types (e.g., datetime) to JSON-safe values."""
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, set):
        return [_make_json_safe(v) for v in obj]
    return obj


# ── Async wrapper (required by create_agent) ────────────────────────────────


async def _aws_boto_command_tool(
    service: str,
    command: str,
    args: dict,
    region: str = "ap-south-1",
) -> dict:
    """Async wrapper that offloads blocking boto3 I/O to a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: run_boto_sync(service, command, args, region),
    )


# ── LangChain StructuredTool ─────────────────────────────────────────────────

aws_boto_command_tool = StructuredTool.from_function(
    func=lambda service, command, args, region="ap-south-1": run_boto_sync(
        service, command, args, region
    ),
    coroutine=_aws_boto_command_tool,
    name="aws_boto_command_tool",
    description=(
        "Execute AWS API commands via Boto3. "
        "Provide the service (e.g. 'ec2'), command (e.g. 'describe_instances'), "
        "an args dict, and optionally a region. "
        "Only whitelisted read/write operations are permitted."
    ),
    args_schema=AWSBotoInput,
)


# ── Agent ────────────────────────────────────────────────────────────────────

aws_boto_agent = create_agent(
    model="bedrock:global.anthropic.claude-sonnet-4-6",
    tools=[aws_boto_command_tool],
    system_prompt=(
        "You are an AWS expert who can help users query and manage their AWS resources "
        "using boto3. Only use the provided tools and do not make up commands. "
        "Always ask for clarification if the user's request is ambiguous. "
        "Reply in plain text or simple Markdown (bold/italic/code, lists)."
    ),
    checkpointer=InMemorySaver(),
)

CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

def markdown_to_telegram_html(text: str) -> str:
    """Convert Markdown to Telegram-safe HTML (parse_mode='HTML')."""
    if not text:
        return text

    # ── 1. Stash code blocks BEFORE any escaping ────────────────────────────
    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _stash_block(m: re.Match) -> str:
        code_blocks.append(m.group(1).strip())
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    def _stash_inline(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = CODE_BLOCK_RE.sub(_stash_block, text)
    text = INLINE_CODE_RE.sub(_stash_inline, text)

    # ── 2. Escape HTML special chars in plain text ──────────────────────────
    text = html.escape(text)

    # ── 3. Inline formatting ────────────────────────────────────────────────
    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)
    # Italic: *text* or _text_  (single, not double)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<i>\1</i>", text)
    # Strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    # Links: [label](url)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)

    # ── 4. Block-level: headers → bold lines ────────────────────────────────
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # ── 5. Lists → clean bullet lines ───────────────────────────────────────
    # Unordered: -, *, +
    text = re.sub(r"^[\-\*\+]\s+(.+)$", r"• \1", text, flags=re.MULTILINE)
    # Ordered: 1. 2. etc  →  keep as-is (already readable)

    # ── 6. Horizontal rules → blank line ────────────────────────────────────
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # ── 7. Restore inline code ───────────────────────────────────────────────
    for i, code in enumerate(inline_codes):
        text = text.replace(
            f"\x00INLINE{i}\x00",
            f"<code>{html.escape(code)}</code>",
        )

    # ── 8. Restore code blocks ───────────────────────────────────────────────
    for i, code in enumerate(code_blocks):
        text = text.replace(
            f"\x00CODEBLOCK{i}\x00",
            f"<pre><code>{html.escape(code)}</code></pre>",
        )

    # ── 9. Collapse 3+ blank lines → max 2 ──────────────────────────────────
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()

@app.post("/")
@app.post("/telegram-webhook")
async def telegram_webhook(req: Request):
    try:
        data = await req.json()
        # print(data)

        if "message" in data:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"].get("text", "")

            if not TELEGRAM_BOT_TOKEN:
                raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

            config = {"configurable": {"thread_id": str(chat_id)}}
            result = aws_boto_agent.invoke(
                {"messages": [{"role": "user", "content": text}]},
                config=config,
            )
            reply_text = markdown_to_telegram_html(
                str(result["messages"][-1].content)
            )

            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": chat_id, 
                        "text": reply_text,
                        "parse_mode": "HTML"
                    },
                )

        return {"ok": True}
    except Exception as e:
        print(f"Error: {e}")
        return {"ok": False, "error": str(e)}
