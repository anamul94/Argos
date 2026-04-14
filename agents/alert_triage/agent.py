"""Alert Triage Deep Agent runner with a single agent doing investigation and remediation.

Flow:
  1. ingest_alert
  2. classify_alert
  3. prefetch current service health
  4. run one Deep Agent with investigation + remediation tools
  5. notify_and_report

This removes the nested investigate/remediate agents and keeps one top-level
Deep Agent responsible for the alert triage decision-making loop.
"""

from __future__ import annotations

import json
import os

import structlog
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from agents.alert_triage.nodes.classify import classify_alert
from agents.alert_triage.nodes.gather_evidence import _dispatch_service_health
from agents.alert_triage.nodes.ingest import ingest_alert
from agents.alert_triage.nodes.investigate_agent import _build_investigation_tools
from agents.alert_triage.nodes.notify import notify_and_report
from agents.alert_triage.nodes.remediate_agent import (
    _build_remediation_tools,
    _determine_resolved,
    _extract_actions_taken,
    _get_final_summary,
    _summarise_action_type,
)
from utils.llm import get_active_model_name, get_llm
from utils.token_usage import (
    build_node_usage,
    count_tool_calls_by_name,
    empty_token_usage_metadata,
    extract_token_usage_from_messages,
    merge_node_usage,
)

log = structlog.get_logger(__name__)

_APPEND_ONLY_FIELDS = {
    "llm_reasoning",
    "actions_taken",
    "node_errors",
    "remediation_results",
}

_REMEDIATION_TOOL_NAMES = {
    "force_ecs_redeploy",
    "scale_out_ecs",
    "start_ec2_instance",
    "reboot_ec2_instance",
    "reboot_rds_instance",
    "scale_out_asg",
    "reboot_elasticache_cluster",
    "verify_service_health",
    "no_action",
}

_SYSTEM_PROMPT = """You are the Argos Alert Triage Agent.

You are the single agent responsible for:
- investigating the CloudWatch alarm
- gathering AWS evidence with tools
- performing safe remediation when appropriate
- verifying whether the issue is resolved
- recording the final triage decision exactly once

Rules:
- Use only the provided AWS investigation/remediation tools plus `record_triage_decision`.
- Do not use filesystem tools.
- Do not delegate to subagents.
- You may use `write_todos` briefly if it helps, but do not spend time planning for its own sake.
- Always investigate before remediating.
- If you execute a remediation action, always call `verify_service_health` afterwards.
- If no safe remediation is appropriate, use `no_action`.
- Call `record_triage_decision` exactly once at the end with your final conclusion.
"""


def _build_checkpointer():
    """Return a DynamoDB checkpointer if configured, else fall back to in-memory."""
    table_name = os.environ.get("DYNAMODB_CHECKPOINT_TABLE", "")
    if table_name:
        from langgraph_dynamodb_checkpoint import DynamoDBSaver

        ttl_days = int(os.environ.get("DYNAMODB_CHECKPOINT_TTL_DAYS", "90"))
        log.info("checkpointer_using_dynamodb", table=table_name, ttl_days=ttl_days)
        return DynamoDBSaver(
            table_name=table_name,
            ttl_seconds=ttl_days * 86400,
        )

    log.info("checkpointer_using_memory")
    return MemorySaver()


checkpointer = _build_checkpointer()


class _AlertTriageSession:
    """Own the mutable alert state for a single deep-agent invocation."""

    def __init__(self, initial_state: dict):
        self.state = dict(initial_state)

    def invoke(self, config: dict | None = None) -> dict:
        """Run the full workflow through one Deep Agent and return final state."""
        self._apply_delta(ingest_alert(self.state))
        self._apply_delta(classify_alert(self.state))
        self._prime_service_health()

        investigation_tools = _build_investigation_tools(self.state["region"])
        remediation_tools = _build_remediation_tools(self.state)

        @tool
        def record_triage_decision(
            root_cause: str,
            confidence: str,
            contributing_factors_json: str,
            action_type: str,
            can_remediate: bool,
            resolved: bool,
            summary: str,
        ) -> str:
            """Record the final triage outcome after investigation/remediation is complete.

            contributing_factors_json must be a JSON array of strings.
            confidence must be high, medium, or low.
            action_type should summarize the remediation path taken, or no_action.
            """
            try:
                parsed = json.loads(contributing_factors_json)
                contributing_factors = [str(item) for item in parsed] if isinstance(parsed, list) else []
            except Exception:
                contributing_factors = []

            normalized_confidence = str(confidence).strip().lower()
            if normalized_confidence not in {"high", "medium", "low"}:
                normalized_confidence = "low"

            self._apply_delta(
                {
                    "root_cause": root_cause.strip() or "Unknown",
                    "confidence": normalized_confidence,
                    "contributing_factors": contributing_factors,
                    "action_type": action_type.strip() or "no_action",
                    "can_remediate": bool(can_remediate),
                    "resolved": bool(resolved),
                    "llm_reasoning": [f"[alert_triage_agent] {summary[:500]}"],
                    "actions_taken": [
                        f"[alert_triage_agent] Final decision recorded: "
                        f"action={action_type or 'no_action'}, resolved={bool(resolved)}"
                    ],
                }
            )
            return json.dumps(
                {
                    "status": "ok",
                    "recorded": True,
                    "resolved": self.state.get("resolved", False),
                },
                default=str,
            )

        agent = create_deep_agent(
            name="alert-triage-agent",
            model=get_llm(),
            tools=[*investigation_tools, *remediation_tools, record_triage_decision],
            system_prompt=_SYSTEM_PROMPT,
            checkpointer=checkpointer,
        )

        log.info(
            "alert_triage_deep_agent_started",
            alert_id=self.state["alert_id"],
            service=self.state.get("service_type"),
            severity=self.state.get("severity"),
        )
        fallback_model = get_active_model_name()
        result = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": self._build_initial_message(),
                    }
                ]
            },
            config=config or {},
        )
        messages = result.get("messages", [])
        self._finalize_from_messages(messages=messages, fallback_model=fallback_model)
        self._apply_delta(notify_and_report(self.state))
        log.info(
            "alert_triage_deep_agent_complete",
            alert_id=self.state["alert_id"],
            resolved=self.state.get("resolved", False),
            action_type=self.state.get("action_type", "no_action"),
        )
        return self.state

    def _build_initial_message(self) -> str:
        context = {
            "alarm_name": self.state["alarm_name"],
            "alarm_reason": self.state["alarm_reason"],
            "severity": self.state["severity"],
            "service_type": self.state["service_type"],
            "metric_name": self.state["metric_name"],
            "metric_namespace": self.state["metric_namespace"],
            "dimensions": self.state["dimensions"],
            "account_id": self.state["account_id"],
            "region": self.state["region"],
            "metadata": self.state.get("metadata", {}),
            "current_service_health": self.state.get("service_health", {}),
        }
        return (
            "Investigate and remediate this CloudWatch alarm end-to-end.\n\n"
            f"{json.dumps(context, indent=2)}\n\n"
            "When you are done, call record_triage_decision exactly once."
        )

    def _prime_service_health(self) -> None:
        """Fetch current service health before the Deep Agent begins."""
        try:
            health = _dispatch_service_health(self.state, self.state["region"])
        except Exception as e:
            log.warning("service_health_prefetch_failed", alert_id=self.state["alert_id"], error=str(e))
            health = {"error": str(e)}
        self.state["service_health"] = health

    def _finalize_from_messages(self, messages: list, fallback_model: str) -> None:
        """Merge token usage and derive fallback fields from the Deep Agent run."""
        tool_results = [
            {"tool": msg.name, "result": msg.content}
            for msg in messages
            if isinstance(msg, ToolMessage) and msg.name != "record_triage_decision"
        ]
        self.state["tool_outputs"] = tool_results
        tool_calls_made, tool_calls_by_name = count_tool_calls_by_name(tool_results)
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
        self.state["token_usage_metadata"] = merge_node_usage(
            self.state.get("token_usage_metadata"),
            "alert_triage_agent",
            node_usage,
        )

        remediation_results = [item for item in tool_results if item["tool"] in _REMEDIATION_TOOL_NAMES]
        if remediation_results:
            self._apply_delta({"remediation_results": remediation_results})

        if "resolved" not in self.state:
            self.state["resolved"] = _determine_resolved(remediation_results)

        if not self.state.get("action_type"):
            self.state["action_type"] = _summarise_action_type(remediation_results)

        if "can_remediate" not in self.state:
            self.state["can_remediate"] = bool(
                self.state.get("action_type") and self.state.get("action_type") != "no_action"
            )

        if not self.state.get("root_cause"):
            final_summary = _get_final_summary(messages)
            self.state["root_cause"] = final_summary[:200] if final_summary else "Unknown"
            self.state["confidence"] = "low"
            self.state["contributing_factors"] = ["Final triage decision tool was not called"]
            self._apply_delta(
                {"llm_reasoning": [f"[alert_triage_agent] {final_summary[:500] or 'No summary available'}"]}
            )

        remediation_actions = _extract_actions_taken(remediation_results)
        if remediation_actions:
            self._apply_delta(
                {"actions_taken": [f"[alert_triage_agent] {action}" for action in remediation_actions]}
            )

        last_ai_summary = next(
            (
                str(msg.content)[:500]
                for msg in reversed(messages)
                if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None)
            ),
            "",
        )
        if last_ai_summary:
            self._apply_delta({"llm_reasoning": [f"[alert_triage_agent] {last_ai_summary}"]})

    def _apply_delta(self, delta: dict) -> None:
        """Apply a node delta using append-only semantics for audit fields."""
        for key, value in delta.items():
            if key in _APPEND_ONLY_FIELDS and isinstance(value, list):
                existing = self.state.get(key, [])
                if not isinstance(existing, list):
                    existing = []
                self.state[key] = [*existing, *value]
                continue
            self.state[key] = value


class AlertTriageDeepAgentRunner:
    """Compatibility wrapper exposing an `.invoke()` API like the old graph."""

    def invoke(self, initial_state: dict, config: dict | None = None) -> dict:
        session = _AlertTriageSession(initial_state=initial_state)
        return session.invoke(config=config)


alert_triage_agent = AlertTriageDeepAgentRunner()
alert_triage_graph = alert_triage_agent


def build_thread_config(account_id: str, alert_id: str) -> dict:
    """Build the config dict with an isolated thread_id."""
    thread_id = f"{account_id}#{alert_id}"
    return {"configurable": {"thread_id": thread_id}}


def build_initial_state(raw_payload: dict) -> dict:
    """Build the initial state dict required to invoke the alert triage agent."""
    return {
        "raw_payload": raw_payload,
        "token_usage_metadata": empty_token_usage_metadata(),
        "llm_reasoning": [],
        "actions_taken": [],
        "node_errors": [],
        "remediation_results": [],
    }
