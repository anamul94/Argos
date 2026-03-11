# Argos

AI-powered cloud operations agent for AWS L1/L2 operations. Receives CloudWatch alarms, investigates root causes, auto-remediates safely, creates Jira tickets, and notifies teams via Telegram and SMS — all autonomously.

Built on **LangGraph** + **Amazon Bedrock (Claude)** + **FastAPI**.

---

## Table of Contents

1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Alert Triage Agent](#alert-triage-agent)
   - [Graph Flow](#graph-flow)
   - [Node Reference](#node-reference)
   - [State Model](#state-model)
   - [Remediation Design](#remediation-design)
4. [AWS Service Coverage](#aws-service-coverage)
5. [Tool Layer](#tool-layer)
6. [Project Structure](#project-structure)
7. [Setup & Configuration](#setup--configuration)
8. [Alarm Naming Convention](#alarm-naming-convention)
9. [EventBridge Integration](#eventbridge-integration)
10. [Security Model](#security-model)

---

## Overview

Argos connects to your AWS environment and responds to CloudWatch alarms automatically. For each alert it:

1. Identifies the affected service and severity
2. Gathers evidence — metrics, logs, and live service health
3. Uses an LLM (Bedrock Claude) to determine the root cause
4. Decides whether a safe automated remediation is possible
5. Executes the remediation (ECS redeploy, scale out, reboot) if safe
6. Verifies recovery
7. Creates a Jira incident ticket and sends a full report to Telegram
8. Sends an SMS to on-call if P1

Everything is recorded in an immutable audit trail inside the LangGraph checkpoint store.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          AWS Account                                │
│                                                                     │
│  CloudWatch Alarm ──► EventBridge Rule ──► API Gateway / Lambda    │
│       (state=ALARM)        (filter)              │                  │
│                                                  │ POST             │
└──────────────────────────────────────────────────┼─────────────────┘
                                                   │
                                                   ▼
                                    ┌──────────────────────────┐
                                    │       FastAPI (Argos)     │
                                    │                           │
                                    │  POST /alert-webhook  ◄──┤
                                    │  POST /telegram-webhook   │
                                    │  GET  /health             │
                                    └────────────┬─────────────┘
                                                 │ background task
                                                 ▼
                                    ┌──────────────────────────┐
                                    │   Alert Triage LangGraph  │
                                    │                           │
                                    │  ingest_alert        →    │
                                    │  classify_alert      →    │
                                    │  investigate_agent   →    │
                                    │  remediate_agent     →    │
                                    │  notify_and_report        │
                                    └────────────┬─────────────┘
                                                 │
                          ┌──────────────────────┼──────────────────────┐
                          │                      │                      │
                          ▼                      ▼                      ▼
                   ┌────────────┐        ┌──────────────┐      ┌──────────────┐
                   │  Jira Cloud │        │   Telegram   │      │  SNS SMS     │
                   │  (ticket)   │        │  ops channel │      │  (P1 only)   │
                   └────────────┘        └──────────────┘      └──────────────┘
```

### Two Entry Points

| Route | Trigger | Handler |
|---|---|---|
| `POST /alert-webhook` | EventBridge CloudWatch alarm | Alert Triage LangGraph (background) |
| `POST /telegram-webhook` | Telegram user message | Interactive LangChain agent |

The Telegram bot is a conversational AWS assistant (ask questions, query resources). The alert webhook is the autonomous remediation pipeline.

---

## Alert Triage Agent

### Graph Flow

```
                    ┌─────────────┐
                    │ ingest_alert │  ◄── raw EventBridge payload
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │classify_alert│  severity from alarm name prefix
                    └──────┬──────┘  service_type from metric namespace
                           │
                    ┌──────▼─────────────────┐
                    │ investigate_agent      │  ReAct agent: fetches metrics, 
                    │ (TodoListMiddleware)   │  searches logs, synthesises RCA
                    └──────┬─────────────────┘
                           │
                    ┌──────▼─────────────────┐
                    │ remediate_agent        │  ReAct agent: plans remediation, 
                    │ (TodoListMiddleware)   │  executes action, verifies health
                    └──────┬─────────────────┘
                           │
                    ┌──────▼─────────────────┐
                    │  notify_and_report     │  Jira + Telegram + SMS (P1)
                    └────────────────────────┘
```

### Node Reference

| Node | Type | Responsibility |
|---|---|---|
| `ingest_alert` | Pure Python | Normalise EventBridge/SQS payload. Generate `alert_id`. |
| `classify_alert` | Pure Python | Parse `severity` from alarm name prefix. Map metric namespace to `service_type`. |
| `investigate_agent` | **ReAct Agent (Bedrock)** | Uses read-only AWS tools dynamically to find root cause. Outputs `root_cause`, `confidence`, `contributing_factors`, `reasoning`. |
| `remediate_agent` | **ReAct Agent (Bedrock)** | Formulates a remediation plan and attempts execution using safe AWS write tools. Verifies recovery via health checks. |
| `notify_and_report` | Pure Python + HTTP | Creates Jira ticket, sends Telegram summary, sends SMS if P1. |

### State Model

`AlertTriageState` is the only communication channel between nodes. Nodes never call each other directly.

```python
class AlertTriageState(TypedDict):
    # Set by ingest_alert
    raw_payload: dict
    account_id: str
    region: str
    alert_id: str          # {account_id}-{alarm_name}-{epoch}
    alarm_name: str
    alarm_reason: str

    # Set by classify_alert
    severity: str          # "p1" | "p2" | "p3" | "p4"
    service_type: str      # "ecs" | "ec2" | "rds" | "lambda" | "alb" | ...
    metric_namespace: str
    dimensions: dict       # e.g. {"ClusterName": "prod", "ServiceName": "api"}

    # Set by gather_evidence
    metric_data: dict
    service_health: dict
    recent_logs: list[str]
    alarm_history: list[dict]

    # Set by analyze_root_cause (LLM)
    root_cause: str
    confidence: str        # "high" | "medium" | "low"
    contributing_factors: list[str]

    # Set by decide_remediation (LLM)
    can_remediate: bool
    action_type: str       # e.g. "force_ecs_redeploy"

    # Set by attempt_remediation + verify_remediation
    resolved: bool

    # Set by notify_and_report
    jira_issue_key: str
    report: dict

    # Append-only audit trail (LangGraph reducer: list concat)
    llm_reasoning:        Annotated[list[str], operator.add]
    actions_taken:        Annotated[list[str], operator.add]
    node_errors:          Annotated[list[str], operator.add]
    remediation_results:  Annotated[list[dict], operator.add]
```

**Append-only fields** (`llm_reasoning`, `actions_taken`, `node_errors`, `remediation_results`) use LangGraph's `operator.add` reducer — nodes only append, never overwrite. This creates a tamper-evident log of everything the agent did.

### Thread Isolation

Every alert runs as an independent graph execution with its own isolated state:

```
thread_id = f"{account_id}#{alert_id}"
```

- Two concurrent P1s → two separate DynamoDB rows, zero state mixing
- Same alarm fires 2 hours later → new epoch → new `alert_id` → fresh investigation
- DynamoDB checkpointer persists full state forever → complete audit trail queryable by `thread_id`

### Remediation Design

**The LLM never touches resource IDs.** It only selects an action type from a closed menu:

```
force_ecs_redeploy    Force new ECS deployment — restarts tasks without downtime
scale_out_ecs         Increase ECS desired count +2 (hard cap: 10)
reboot_ec2            Reboot EC2 instance — non-destructive
reboot_rds            Reboot RDS instance — clears connection pool
scale_out_asg         Increase ASG desired capacity +2
reboot_elasticache    Reboot ElastiCache cluster nodes
no_action             No safe action available — manual review required
```

The `attempt_remediation` node then resolves the actual resource identifiers from `state["dimensions"]` (verified AWS data returned by `gather_evidence`) and constructs the boto3 call in code.

**What is intentionally excluded (v1):**
- Scale down / reduce capacity
- Terminate / stop instances
- Delete anything
- Modify DB parameter groups
- Any action on resources not identified from the alarm dimensions

---

## AWS Service Coverage

The `gather_evidence` node fetches service-specific health data based on the alarm's metric namespace:

| Metric Namespace | `service_type` | Evidence Collected |
|---|---|---|
| `AWS/ECS` | `ecs` | Service desired/running/pending counts, deployment status, running task ARNs |
| `AWS/EC2`, `CWAgent` | `ec2` | Instance state, instance status check, system status check |
| `AWS/RDS` | `rds` | DB instance status, engine version, Multi-AZ, recent error events |
| `AWS/Lambda` | `lambda` | Runtime, memory, timeout, state, reserved concurrency |
| `AWS/ApplicationELB`, `AWS/NetworkELB` | `alb` | Healthy / unhealthy target counts |
| `AWS/ElastiCache` | `elasticache` | Cluster status, engine, node statuses |
| `AWS/ApiGateway` | `apigateway` | Stage metrics only (no remediation in v1) |

In addition, for **all** service types:
- CloudWatch `get_metric_statistics` — last 30 minutes of the alarming metric
- CloudWatch `describe_alarm_history` — last 10 state change events
- CloudWatch Logs Insights query — last 15 minutes, filtered to `ERROR|WARN|Exception|FATAL`

---

## Tool Layer

All AWS API calls go through `tools/aws_boto.py` — a whitelisted boto3 dispatcher.

```
Service request → ALLOWED_COMMANDS whitelist check → boto3 client → response
                        │
                 blocked if not in list
                 (returns {"status": "blocked", ...})
```

The whitelist (`ALLOWED_COMMANDS`) is the single authoritative list of permitted API calls. Any call not on it is rejected before reaching AWS.

**Safe write commands included:**

| Service | Write Commands |
|---|---|
| `ecs` | `update_service` (force new deployment, scale out) |
| `ec2` | `reboot_instances` |
| `rds` | `reboot_db_instance` |
| `autoscaling` | `set_desired_capacity` (scale out only — enforced in remediate.py) |
| `elasticache` | `reboot_cache_cluster` |
| `sns` | `publish` (SMS) |

**Excluded by design:** `terminate_instances`, `delete_*`, `stop_instances`, any IAM mutation, any cross-account escalation.

---

## Project Structure

```
argos/
├── main.py                              FastAPI app (two routes + health)
│
├── models/
│   └── alert_state.py                   AlertTriageState TypedDict
│
├── utils/
│   ├── serialization.py                 make_json_safe() — boto3 response sanitiser
│   └── formatting.py                    markdown_to_telegram_html()
│
├── tools/
│   ├── aws_boto.py                      Whitelisted boto3 dispatcher + LangChain StructuredTool
│   ├── jira.py                          Jira Cloud REST — create/update incidents
│   ├── sns_notify.py                    SNS SMS for P1 on-call alerts
│   └── telegram_notify.py              Telegram ops channel notifications
│
└── agents/
    └── alert_triage/
        ├── graph.py                     Compiled LangGraph, thread_id helpers, checkpointer
        └── nodes/
            ├── ingest.py
            ├── classify.py
            ├── gather_evidence.py
            ├── analyze.py               Bedrock LLM → RootCauseAnalysis
            ├── remediate.py             decide + attempt + verify
            └── notify.py
```

**Dependency flow (enforced by architecture):**
```
main.py  →  agents/  →  tools/  →  models/
                     →  utils/
```
`tools/` and `utils/` never import from `agents/`. `models/` imports nothing internal.

---

## Setup & Configuration

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```bash
# AWS — use IAM role in Lambda/EC2, or set credentials for local dev
AWS_DEFAULT_REGION=ap-south-1

# Bedrock
BEDROCK_MODEL_ID=bedrock:global.anthropic.claude-sonnet-4-6

# Telegram
TELEGRAM_BOT_TOKEN=<your-bot-token>
TELEGRAM_OPS_CHAT_ID=<ops-group-chat-id>     # group chat for alert notifications

# Jira Cloud
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=ops@yourcompany.com
JIRA_API_TOKEN=<jira-api-token>
JIRA_PROJECT_KEY=OPS

# SNS SMS — P1 on-call number (E.164 format)
SNS_ONCALL_PHONE=+919876543210

# DynamoDB checkpoint store (optional — falls back to in-memory if not set)
# Install: pip install langgraph-checkpoint-dynamodb
DYNAMODB_CHECKPOINT_TABLE=argos-checkpoints
```

### 3. Run

```bash
# Development
uvicorn main:app --reload

# Production
uvicorn main:app --host 0.0.0.0 --port 8080
```

### 4. Register Telegram webhook

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://your-domain.com/telegram-webhook"}'
```

---

## Alarm Naming Convention

Argos parses severity directly from the CloudWatch alarm name. Alarms **must** follow this pattern:

```
{severity}-{service}-{resource-name}-{metric}-{condition}
```

| Example | Parsed severity | Parsed service |
|---|---|---|
| `p1-ecs-payment-service-cpu-high` | P1 | ECS |
| `p2-rds-main-db-connections-high` | P2 | RDS |
| `p3-lambda-order-processor-errors` | P3 | Lambda |
| `p1-alb-api-gateway-5xx-spike` | P1 | ALB |
| `my-alarm-without-prefix` | P3 (default) | from namespace |

---

## EventBridge Integration

### EventBridge Rule

Create a rule that captures all CloudWatch alarm state changes to ALARM:

```json
{
  "source": ["aws.cloudwatch"],
  "detail-type": ["CloudWatch Alarm State Change"],
  "detail": {
    "state": {
      "value": ["ALARM"]
    }
  }
}
```

**Target:** API Gateway endpoint → `POST /alert-webhook`
(or SQS queue → Lambda → `POST /alert-webhook` for buffering and deduplication)

### Payload Argos Receives

```json
{
  "version": "0",
  "source": "aws.cloudwatch",
  "detail-type": "CloudWatch Alarm State Change",
  "account": "123456789012",
  "region": "ap-south-1",
  "detail": {
    "alarmName": "p1-ecs-payment-service-cpu-high",
    "alarmArn": "arn:aws:cloudwatch:...",
    "state": {
      "value": "ALARM",
      "reason": "Threshold Crossed: 1 datapoint [92.3] was greater than [80.0].",
      "timestamp": "2024-01-15T10:30:00.000Z"
    },
    "configuration": {
      "metrics": [{
        "metricStat": {
          "metric": {
            "namespace": "AWS/ECS",
            "name": "CPUUtilization",
            "dimensions": {
              "ClusterName": "prod-cluster",
              "ServiceName": "payment-service"
            }
          }
        }
      }]
    }
  }
}
```

Argos also handles SQS-wrapped EventBridge events (when using SQS as a buffer target).

---

## Security Model

### Credentials
- All secrets loaded from environment variables at call time (never at import or module load)
- No credentials, tokens, or secrets in code, comments, or state
- `JIRA_API_TOKEN`, `TELEGRAM_BOT_TOKEN` loaded via `os.environ` at the point of use

### AWS Access
- All boto3 calls pass through the `ALLOWED_COMMANDS` whitelist — any unlisted call is blocked and returns `{"status": "blocked"}`
- Safe write commands only: restart/reboot/scale-out — no terminate, delete, or scale-down
- Resource IDs in remediation come from AWS API responses (alarm dimensions), never from LLM output

### LLM Output Handling
- `analyze_root_cause`: structured Pydantic output — no free-form command execution
- `decide_remediation`: LLM output is validated against `SAFE_ACTIONS` dict; any unlisted `action_type` is forced to `no_action`
- Resource identifiers are re-resolved from `state["dimensions"]` before any boto3 write call

### Input Validation
- Alert payload is validated structurally before graph invocation (`_is_cloudwatch_alarm_event`)
- All LLM calls are wrapped in try/except with typed safe fallbacks — the graph never crashes on LLM failure
