# CLAUDE.md

This file provides guidance to Claude Code (`claude.ai/code`) when working with code in this repository.

## Repository Purpose

**Argos** — An AI-powered cloud operations agent for AWS L1/L2 operations.
Built on LangGraph and Amazon Bedrock. Receives CloudWatch alarms via EventBridge, investigates root causes, auto-remediates safely, creates Jira tickets, and notifies via Telegram and SMS.

## Running

```bash
pip install -r requirements.txt
uvicorn main:app --reload          # development
uvicorn main:app --host 0.0.0.0 --port 8080  # production
```

## Required Environment Variables

```bash
# AWS
AWS_DEFAULT_REGION=ap-south-1

# Bedrock
BEDROCK_MODEL_ID=bedrock:global.anthropic.claude-sonnet-4-6

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_OPS_CHAT_ID=...        # ops group chat for alert notifications

# Jira Cloud
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=...
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=OPS

# SNS SMS (P1 only)
SNS_ONCALL_PHONE=+91...         # E.164 format

# DynamoDB checkpointer (optional — falls back to in-memory)
DYNAMODB_CHECKPOINT_TABLE=argos-checkpoints
```

## Project Structure

```
main.py                             # FastAPI app — two routes
models/alert_state.py               # AlertTriageState TypedDict (single source of truth)
utils/serialization.py              # make_json_safe() — boto3 response sanitiser
utils/formatting.py                 # markdown_to_telegram_html()
tools/
  aws_boto.py                       # Whitelisted boto3 dispatcher + LangChain StructuredTool
  jira.py                           # Jira Cloud REST API (create/update issues)
  sns_notify.py                     # SNS SMS for P1 on-call alerts
  telegram_notify.py                # Telegram ops channel notifications
agents/alert_triage/
  graph.py                          # Compiled LangGraph + thread ID helpers
  nodes/
    ingest.py                       # Normalise EventBridge payload
    classify.py                     # Extract severity (alarm prefix) + service type (namespace)
      gather_evidence.py              # Deleted (merged into investigate_agent)
    analyze.py                      # Deleted (merged into investigate_agent)
    investigate_agent.py            # ReAct LLM Agent for root cause analysis
    remediate_agent.py              # ReAct LLM Agent for planning, executing, verifying
    notify.py                       # Jira + Telegram + SNS SMS
```

---

## Alert Triage Graph Flow

```
EventBridge (CloudWatch Alarm state=ALARM)
  → POST /alert-webhook  (returns 200 immediately, runs triage in background)
    → ingest_alert       normalise payload, generate alert_id
    → classify_alert     severity from alarm name prefix, service_type from namespace
    → investigate_agent  ReAct agent (TodoListMiddleware) runs read-only AWS tools (logs, metrics, health) and creates RootCauseAnalysis
    → remediate_agent    ReAct agent (TodoListMiddleware) plans remediation, runs safe AWS actions from menu, verifies health
    → notify_and_report  Jira ticket + Telegram ops channel + SNS SMS (P1 only)
```

**Thread isolation:** `thread_id = f"{account_id}#{alert_id}"` — each alarm firing gets a unique ID, concurrent alerts never mix state.

**Safe remediation only (v1):** `force_ecs_redeploy`, `scale_out_ecs` (+2 tasks, cap 10), `reboot_ec2`, `reboot_rds`, `scale_out_asg` (+2). No scale-down, no terminate, no delete.

**LLM never touches resource IDs:** `remediate_agent` LLM picks an action type tool. The tools resolve resource IDs exclusively from `state["dimensions"]` (verified AWS data).

## AWS Service Routing

Alarm metric namespace → `service_type` → evidence tools used:

| Namespace | service_type | Evidence fetched |
|---|---|---|
| `AWS/ECS` | `ecs` | describe_services, list_tasks |
| `AWS/EC2` / `CWAgent` | `ec2` | describe_instance_status |
| `AWS/RDS` | `rds` | describe_db_instances, describe_events |
| `AWS/Lambda` | `lambda` | get_function, get_function_concurrency |
| `AWS/ApplicationELB` | `alb` | describe_target_health |
| `AWS/ElastiCache` | `elasticache` | describe_cache_clusters |

## Alarm Naming Convention

Alarms must be named `{severity}-{service}-{resource}-{metric}`:
```
p1-ecs-payment-service-cpu-high
p2-rds-main-db-connections-high
p3-lambda-order-processor-errors-spike
```
Severity is parsed from the prefix. Missing prefix defaults to `p3`.

## FastAPI Routes

| Route | Purpose |
|---|---|
| `POST /alert-webhook` | EventBridge CloudWatch alarm → triage graph (background task) |
| `POST /telegram-webhook` or `POST /` | Interactive Telegram bot (existing) |
| `GET /health` | Health check |

## Coding Rules

### General Principles

- **SOLID**: Every class and function has one reason to change. Depend on abstractions, not concretions. Extend via new modules, not by modifying existing ones.
- **DRY**: If the same logic appears twice, it becomes a shared utility. No exceptions.
- **KISS**: Prefer the simplest solution that correctly solves the problem. Do not over-engineer. Add complexity only when the requirement demands it.
- **Modular**: Every file is independently importable and testable. No circular imports. No mega-files. If a file exceeds 300 lines, it needs to be split.
- **Clean Architecture**: Dependencies flow inward only. `tools` and `memory` never import from `agents`. `agents` never import from `lambda_handler`. `models` imports from nothing inside the project.
  > `lambda_handler` → `agents` → `tools` / `memory` → `models`

### Never Guess

- **Never hardcode any value** that could change between environments, tenants, or deployments. Every configurable value must come from one of:
  - Environment variables (`os.environ`)
  - AWS Secrets Manager (credentials, tokens, API keys)
  - AWS SSM Parameter Store (non-secret config)
  - DynamoDB tenant registry (per-tenant config)
- **Never assume a default region**. Region must always be explicit from `TenantContext` or environment variable. No fallback to `us-east-1` silently.
- **Never assume a resource exists**. Always validate with a describe/get call before acting on any ARN, ID, or name provided by the LLM or alert payload.
- **Never trust LLM output for resource identifiers**. Always re-resolve resource IDs from AWS APIs. The LLM can hallucinate ARNs, cluster names, and instance IDs.
- **Never use `*` as a default** in IAM session policies or boto3 filters. Be explicit about what you are scoping.

### Naming Conventions

- **Files**: `snake_case.py`
- **Classes**: `PascalCase`
- **Functions / variables**: `snake_case`
- **Constants**: `UPPER_SNAKE_CASE`
- **LangGraph nodes**: `snake_case` verb phrases (e.g., `ingest_alert`, `classify_severity`)
- **Tools**: `snake_case` with service prefix (e.g., `ecs_force_redeploy`, `slack_post_message`)
- **No abbreviations** unless universally understood (`arn`, `id`, `ttl`, `kb`). Write `tenant_id` not `tid`. Write `alert_id` not `aid`.

### Functions

- Maximum **one responsibility per function**. If the docstring needs "and", split it.
- Maximum **30 lines** of logic per function. Longer functions must be decomposed.
- All functions must have **type hints** on every parameter and return value.
- All public functions must have a **docstring** explaining what it does, what it returns, and any side effects.
- Functions must **never return None silently** on failure. Return a typed error dict or raise an explicit exception.
- **No positional-only arguments** for functions with more than 2 parameters. Use keyword arguments.

```python
# ❌ Wrong
def process(a, b, c, d):
    ...

# ✅ Correct
def classify_alert(
    alarm_name: str,
    tenant: TenantContext,
    evidence: EvidenceBundle,
    confidence_threshold: float,
) -> ClassificationResult:
    ...
```

### Error Handling

- **Every boto3 call** must be wrapped in try/except catching `ClientError`, `BotoCoreError`, and `Exception` separately.
- **Every LLM call** must be wrapped in try/except. On failure, return a safe fallback — never crash the graph.
- **Errors are returned, not raised**, from tool functions. Tools return `{"status": "error", "error": str(e)}` so the graph can continue.
- **Agents and nodes may raise** — LangGraph catches and checkpoints on failure.
- **Never use bare `except:`**. Always catch specific exception types.
- **Never swallow exceptions silently**. Log every caught exception with `structlog` before returning the error dict.

```python
# ❌ Wrong
try:
    response = client.describe_services(...)
except:
    return {}

# ✅ Correct
try:
    response = client.describe_services(cluster=cluster, services=[service])
except ClientError as e:
    log.error(
        "ecs_describe_failed",
        error_code=e.response["Error"]["Code"],
        error=e.response["Error"]["Message"],
        cluster=cluster,
        service=service,
    )
    return {"status": "aws_error", "error": e.response["Error"]["Message"]}
except Exception as e:
    log.error("ecs_describe_unexpected", error=str(e))
    return {"status": "error", "error": str(e)}
```

### Logging

- Use `structlog` exclusively. Never use `print()` or the stdlib `logging` directly.
- Every log entry must include **contextual fields**: `tenant_id`, `alert_id`, `node` or `tool` name at minimum.
- Log levels: `log.debug` for trace data, `log.info` for state transitions, `log.warning` for recoverable issues, `log.error` for failures.
- **Never log secrets, credentials, tokens, or full payloads** containing PII. Log IDs and status codes, not variable contents.

```python
# ❌ Wrong
print(f"Processing alert {alert_id}")
log.info(f"Credentials: {creds}")

# ✅ Correct
log.info("node_started", node="ingest_alert", alert_id=alert_id, tenant_id=tenant.tenant_id)
log.info("sts_credentials_refreshed", tenant_id=tenant.tenant_id, role_arn=tenant.aws_role_arn)
```

### State Management

- **State is the only communication channel between nodes**. Nodes never call each other directly.
- **Never mutate state in place**. Always return a new dict with only the fields being updated. LangGraph merges it correctly.
- **Append-only fields** (`llm_reasoning`, `actions_taken`, `node_errors`) must only ever be appended to, never replaced.
- **Sensitive values** (credentials, tokens) must never be written to state. Store only ARNs, IDs, and non-secret references.

```python
# ❌ Wrong — mutating state
state["root_cause"] = "disk full"
state["llm_reasoning"] = ["new reason"]  # wipes previous reasoning

# ✅ Correct — returning delta
return {
    "root_cause": "disk full",
    "llm_reasoning": ["[synthesize] Root cause identified: disk full on /dev/xvda"],
}
```

---

## Security Rules

### Credentials & Secrets

- **Zero credentials in code**. No API keys, tokens, passwords, or secrets anywhere in source files, comments, or docstrings. No exceptions.
- **Zero credentials in environment variables** for sensitive values. Secrets Manager is the only accepted store for tokens and passwords.
- **Zero credentials in state**. Never write STS credentials, API tokens, or secrets into `AlertTriageState` or any DynamoDB record.
- **Zero credentials in logs**. `structlog` formatters must redact any field containing `token`, `key`, `secret`, `password`, `credential`.
- All secrets are loaded **lazily at call time** from Secrets Manager, not at module load or Lambda cold start.

```python
# ❌ Wrong
SLACK_TOKEN = "xoxb-123456"
os.environ["JIRA_TOKEN"] = "abc123"

# ✅ Correct
def _get_slack_token() -> str:
    return _get_secret("argos/notification-secrets")["SLACK_BOT_TOKEN"]
```

### IAM & Least Privilege

- The Argos Lambda execution role has **read-only AWS access** by default.
- Write permissions (ECS update, ASG scale) are granted only on the **cross-account spoke role**, scoped to specific resource ARN patterns.
- Every STS `AssumeRole` session includes an **inline session policy** restricting permissions to exactly what that investigation needs.
- **No `*` resources** in any policy statement written or referenced in this project.
- IAM role ARNs are loaded from DynamoDB tenant registry — never hardcoded.

### Input Validation

- **Every alert payload is untrusted input**. Validate structure with Pydantic before any field is accessed.
- **Every LLM output is untrusted**. Parse with `json.loads` in try/except. Validate against expected schema before use.
- **Every resource identifier from the LLM** must be re-verified against AWS APIs before being passed to a remediation action.
- **Whitelist over blacklist** for all AWS API access. If a command is not explicitly in `ALLOWED_COMMANDS`, it is blocked — no exceptions granted at runtime.

### Data Handling

- **No customer data in logs**. Log metadata (IDs, counts, statuses), not content.
- **No PII in DynamoDB records**. Store event IDs and ARNs, not usernames, email addresses, or IP addresses unless operationally required.
- **DynamoDB encryption at rest** must be enabled on all Argos tables (KMS CMK).
- **S3 audit logs** must have server-side encryption and bucket policies blocking public access.
- **All data in transit** uses TLS. Never construct HTTP (non-TLS) endpoints for webhooks or callbacks.

### Dependency Security

- Pin all dependencies to **exact versions** in `requirements.txt`. No `>=`, no `~=`, no unpinned packages.
- Run `pip audit` before every deployment. Any high/critical CVE blocks the deploy.
- Use only packages from the approved list. Adding a new dependency requires explicit review — document the reason in `requirements.txt` as a comment.

```text
# requirements.txt
langgraph==0.2.28          # LangGraph state machine
langchain==0.3.7           # LangChain core
langchain-aws==0.2.6       # Bedrock + boto3 integration
boto3==1.35.36             # AWS SDK — pin to tested version
pydantic==2.9.2            # State validation
structlog==24.4.0          # Structured logging
tenacity==9.0.0            # Retry logic
```

### Webhook Security

- All inbound webhooks (PagerDuty, OpsGenie, Slack) must validate **request signatures** before processing the payload.
- Slack: validate `X-Slack-Signature` using HMAC-SHA256.
- PagerDuty: validate webhook `v3` signature header.
- OpsGenie: validate shared secret in header.
- Reject any request that fails signature validation with HTTP 401. Log the attempt with source IP.

```python
# ❌ Wrong — process without validation
body = json.loads(event["body"])
return handle_webhook(body)

# ✅ Correct — validate first
if not verify_slack_signature(event):
    log.warning("invalid_slack_signature", source_ip=event["requestContext"]["identity"]["sourceIp"])
    return {"statusCode": 401, "body": "Unauthorized"}
    
body = json.loads(event["body"])
return handle_webhook(body)
```