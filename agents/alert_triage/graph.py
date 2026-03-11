"""Alert Triage LangGraph — compiled graph with DynamoDB checkpointer.

Dependency rule: imports only from agents/alert_triage/nodes/*.
The graph itself is stateless — all data lives in AlertTriageState.
"""

import os

import structlog
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agents.alert_triage.nodes.classify import classify_alert
from agents.alert_triage.nodes.ingest import ingest_alert
from agents.alert_triage.nodes.investigate_agent import investigate_agent
from agents.alert_triage.nodes.notify import notify_and_report
from agents.alert_triage.nodes.remediate_agent import remediate_agent
from models.alert_state import AlertTriageState

log = structlog.get_logger(__name__)


def _build_graph() -> StateGraph:
    """Construct the alert triage StateGraph.

    Graph flow:
      ingest_alert → classify_alert → investigate_agent → remediate_agent → notify_and_report

    investigate_agent (ReAct + TodoListMiddleware):
      Replaces gather_evidence + analyze_root_cause.
      Autonomously collects evidence, follows cross-service leads, produces RCA.

    remediate_agent (ReAct + TodoListMiddleware):
      Replaces decide_remediation + attempt_remediation + verify_remediation.
      Plans remediation steps, executes, verifies, retries if needed.
    """
    graph = StateGraph(AlertTriageState)

    graph.add_node("ingest_alert", ingest_alert)
    graph.add_node("classify_alert", classify_alert)
    graph.add_node("investigate_agent", investigate_agent)
    graph.add_node("remediate_agent", remediate_agent)
    graph.add_node("notify_and_report", notify_and_report)

    graph.set_entry_point("ingest_alert")
    graph.add_edge("ingest_alert", "classify_alert")
    graph.add_edge("classify_alert", "investigate_agent")
    graph.add_edge("investigate_agent", "remediate_agent")
    graph.add_edge("remediate_agent", "notify_and_report")
    graph.add_edge("notify_and_report", END)

    return graph


def _build_checkpointer():
    """Return a DynamoDB checkpointer if configured, else fall back to in-memory.

    Set DYNAMODB_CHECKPOINT_TABLE in .env to enable DynamoDB persistence.
    The table is created automatically on first use — no manual setup needed.
    TTL is set to 90 days so old alert records expire automatically.
    """
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


# Compiled graph — module-level singleton, imported by main.py
checkpointer = _build_checkpointer()
alert_triage_graph = _build_graph().compile(checkpointer=checkpointer)


def build_thread_config(account_id: str, alert_id: str) -> dict:
    """Build the LangGraph config dict with an isolated thread_id.

    Format: account_id#alert_id — ensures no cross-account or cross-alert mixing.
    """
    thread_id = f"{account_id}#{alert_id}"
    return {"configurable": {"thread_id": thread_id}}


def build_initial_state(raw_payload: dict) -> dict:
    """Build the initial state dict required to invoke the graph.

    Append-only fields must be initialised as empty lists.
    """
    return {
        "raw_payload": raw_payload,
        "llm_reasoning": [],
        "actions_taken": [],
        "node_errors": [],
        "remediation_results": [],
    }
