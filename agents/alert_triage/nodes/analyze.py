"""analyze_root_cause node — LLM-powered root cause analysis.

Uses Bedrock Claude with structured output. On failure returns a safe
fallback so the graph always continues.
"""

import json

from dotenv import load_dotenv

load_dotenv()  # ensure env vars are available when running from background threads

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from models.alert_state import AlertTriageState
from utils.llm import get_bedrock_llm

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are an AWS cloud operations expert specialising in incident response. "
    "Analyse the provided alert evidence and determine the root cause. "
    "Be precise. Focus only on facts present in the evidence — do not speculate. "
    "If evidence is insufficient, state that clearly with confidence=low."
)

_FALLBACK_ANALYSIS = {
    "root_cause": "Unable to determine root cause — LLM analysis failed",
    "confidence": "low",
    "contributing_factors": ["LLM analysis unavailable"],
    "reasoning": "Fallback: LLM call failed or returned unparseable output.",
}


class RootCauseAnalysis(BaseModel):
    """Structured LLM output for root cause analysis."""

    root_cause: str = Field(description="One-sentence description of the root cause")
    confidence: str = Field(description="Confidence level: 'high', 'medium', or 'low'")
    contributing_factors: list[str] = Field(description="List of contributing factors observed in the evidence")
    reasoning: str = Field(description="Step-by-step reasoning that led to this conclusion")


def analyze_root_cause(state: AlertTriageState) -> dict:
    """Call the LLM to analyse evidence and identify the root cause.

    Returns delta dict with root_cause, confidence, contributing_factors,
    and llm_reasoning appended. Never crashes the graph.
    """
    alert_id = state["alert_id"]
    log.info("analyze_root_cause_started", alert_id=alert_id)

    try:
        analysis = _call_llm(state)
    except Exception as e:
        log.error("analyze_root_cause_llm_failed", alert_id=alert_id, error=str(e))
        analysis = RootCauseAnalysis(**_FALLBACK_ANALYSIS)

    log.info("analyze_root_cause_complete", alert_id=alert_id,
             confidence=analysis.confidence, root_cause=analysis.root_cause[:80])

    return {
        "root_cause": analysis.root_cause,
        "confidence": analysis.confidence,
        "contributing_factors": analysis.contributing_factors,
        "llm_reasoning": [f"[analyze_root_cause] {analysis.reasoning}"],
        "actions_taken": [
            f"[analyze_root_cause] Root cause: {analysis.root_cause} (confidence: {analysis.confidence})"
        ],
    }


def _call_llm(state: AlertTriageState) -> RootCauseAnalysis:
    """Build the prompt, call Bedrock with structured output, and return the result."""
    llm = get_bedrock_llm(structured_output_schema=RootCauseAnalysis)
    prompt = _build_prompt(state)
    return llm.invoke([SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=prompt)])


def _build_prompt(state: AlertTriageState) -> str:
    """Build the evidence summary prompt for the LLM."""
    factors = "\n".join(f"  - {f}" for f in state.get("contributing_factors", []))
    logs_preview = "\n".join(state.get("recent_logs", [])[:30]) or "  No logs retrieved"
    metric_summary = json.dumps(state.get("metric_data", {}), indent=2)[:2000]
    health_summary = json.dumps(state.get("service_health", {}), indent=2)[:2000]
    history = json.dumps(state.get("alarm_history", []), indent=2)[:1000]

    return (
        f"## Alert Details\n"
        f"Alarm: {state['alarm_name']}\n"
        f"Severity: {state['severity']}\n"
        f"Service type: {state['service_type']}\n"
        f"Metric: {state['metric_name']} ({state['metric_namespace']})\n"
        f"Dimensions: {state['dimensions']}\n"
        f"Alarm reason: {state['alarm_reason']}\n\n"
        f"## Metric Data (last 30 min)\n{metric_summary}\n\n"
        f"## Service Health\n{health_summary}\n\n"
        f"## Recent Logs (last 15 min, errors/warnings only)\n{logs_preview}\n\n"
        f"## Alarm History\n{history}\n"
    )
