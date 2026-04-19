"""
agent/limits.py — Hard limit enforcement for the AML investigation state machine.

All functions are pure (read-only on state, no side effects).
The LLM cannot call or override these functions — they are wired directly
into the LangGraph conditional edge, which runs before any LLM invocation.
"""
from __future__ import annotations

import time
from typing import Optional

import structlog

from agent.state import AgentState

log = structlog.get_logger()

MAX_TOOL_HOPS: int = 4
CONFIDENCE_THRESHOLD: float = 0.70
INVESTIGATION_TIMEOUT_SECONDS: float = 30.0
COST_CAP_USD: float = 0.05


def check_max_hops(state: AgentState) -> Optional[str]:
    """Returns 'max_hops' escalation reason if hop_count >= MAX_TOOL_HOPS, else None."""
    hop_count = state.get("hop_count", 0)
    if hop_count >= MAX_TOOL_HOPS:
        log.warning("limit.max_hops", hop_count=hop_count, limit=MAX_TOOL_HOPS)
        return "max_hops"
    return None


def check_confidence(state: AgentState) -> Optional[str]:
    """Returns 'low_confidence' if evaluation.confidence < CONFIDENCE_THRESHOLD, else None.
    If evaluation is None (not yet set), returns None — limit not applicable yet.
    """
    evaluation = state.get("evaluation")
    if evaluation is None:
        return None
    confidence = round(evaluation.get("confidence", 0.0), 4)
    if confidence < CONFIDENCE_THRESHOLD:
        log.warning(
            "limit.low_confidence",
            confidence=confidence,
            threshold=CONFIDENCE_THRESHOLD,
        )
        return "low_confidence"
    return None


def check_timeout(state: AgentState) -> Optional[str]:
    """Returns 'timeout' if wall-clock time since started_at > INVESTIGATION_TIMEOUT_SECONDS, else None."""
    started_at = state.get("started_at")
    if started_at is None:
        return None
    elapsed = time.time() - started_at
    if elapsed > INVESTIGATION_TIMEOUT_SECONDS:
        log.warning(
            "limit.timeout",
            elapsed_seconds=elapsed,
            limit=INVESTIGATION_TIMEOUT_SECONDS,
        )
        return "timeout"
    return None


def check_cost_cap(state: AgentState) -> Optional[str]:
    """Returns 'cost_cap' if accumulated_cost_usd >= COST_CAP_USD, else None."""
    cost = state.get("accumulated_cost_usd", 0.0)
    if cost >= COST_CAP_USD:
        log.warning("limit.cost_cap", cost_usd=cost, cap=COST_CAP_USD)
        return "cost_cap"
    return None


def evaluate_routing(state: AgentState) -> str:
    """
    Called by route_after_evaluating in graph.py when escalation_reason is None.

    Hard limits are already checked and persisted by node_evaluating — this
    function must NOT re-run them. Decides between node_resolved and
    node_tool_calling based on the LLM's should_continue flag.

    LLM contract:
      - should_continue=True  → call another tool (back to TOOL_CALLING)
      - should_continue=False AND confidence >= CONFIDENCE_THRESHOLD → RESOLVED
      - should_continue=False AND confidence <  CONFIDENCE_THRESHOLD → TOOL_CALLING
        (next hop's check_confidence will trigger ESCALATED via low_confidence)

    Returns one of: "node_tool_calling" | "node_resolved"
    """
    payload = state.get("payload")
    investigation_id = payload.investigation_id if payload else "unknown"

    evaluation = state.get("evaluation")
    if evaluation is None:
        log.info("routing.no_evaluation_yet", investigation_id=investigation_id)
        return "node_tool_calling"

    should_continue = evaluation.get("should_continue", False)
    confidence = round(evaluation.get("confidence", 0.0), 4)

    if should_continue:
        log.info(
            "routing.llm_requested_more_evidence",
            investigation_id=investigation_id,
            confidence=confidence,
        )
        return "node_tool_calling"

    if confidence >= CONFIDENCE_THRESHOLD:
        log.info("routing.resolved", investigation_id=investigation_id, confidence=confidence)
        return "node_resolved"

    log.info(
        "routing.another_tool_low_confidence",
        investigation_id=investigation_id,
        confidence=confidence,
    )
    return "node_tool_calling"
