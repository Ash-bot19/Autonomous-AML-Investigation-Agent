"""
tests/unit/test_state_machine.py

Unit tests for the AML investigation state machine (Phase 2).
All tests run in-memory — no PostgreSQL, Redis, or Kafka required.
"""
import time
import pytest
from unittest.mock import patch
from models.schemas import EvaluationOutput, InvestigationPayload, ToolSelectionOutput, ToolResult
from agent.state import AgentState, InvestigationStatus
from agent.graph import AMLGraph, node_evaluating
from agent.limits import (
    MAX_TOOL_HOPS, CONFIDENCE_THRESHOLD, COST_CAP_USD,
    INVESTIGATION_TIMEOUT_SECONDS, evaluate_routing,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_payload(txn_id: str = "TXN_TEST_001") -> InvestigationPayload:
    return InvestigationPayload(
        txn_id=txn_id,
        trigger_type="rule_engine",
        trigger_detail="velocity breach: 18 txns in 2h",
    )


def make_state(
    *,
    hop_count: int = 1,
    accumulated_cost_usd: float = 0.01,
    started_at: float | None = None,
    evidence_chain: list | None = None,
    evaluation: dict | None = None,
    escalation_reason: str | None = None,
    payload: InvestigationPayload | None = None,
) -> AgentState:
    """Build a minimal AgentState for unit testing — all fields present."""
    if evidence_chain is None:
        evidence_chain = [
            {"hop": 1, "tool": "velocity_check", "finding": "18 txns in 6h", "significance": "high"}
        ]
    if evaluation is None:
        evaluation = {
            "confidence": 0.85,
            "verdict": "suspicious",
            "finding": "Unusual velocity pattern",
            "recommendation": "monitor",
            "narrative": "Mock narrative.",
        }
    return AgentState(
        payload=payload or make_payload(),
        status=InvestigationStatus.EVALUATING,
        hop_count=hop_count,
        accumulated_cost_usd=accumulated_cost_usd,
        started_at=started_at if started_at is not None else time.time(),
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=evidence_chain,
        evaluation=evaluation,
        escalation_reason=escalation_reason,
        final_report=None,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_happy_path():
    """Full graph traversal: IDLE → INVESTIGATING → TOOL_CALLING → EVALUATING → RESOLVED.
    call_llm and dispatch_tool are patched so no real OpenAI or infrastructure calls occur.
    node_evaluating injects a mock evaluation (confidence=0.85) when evaluation=None.
    """
    payload = make_payload("TXN_HAPPY_001")
    initial = AgentState(
        payload=payload,
        status=InvestigationStatus.IDLE,
        hop_count=0,
        accumulated_cost_usd=0.0,
        started_at=None,
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=[],
        evaluation=None,  # node_evaluating will inject mock evaluation
        escalation_reason=None,
        final_report=None,
    )

    deterministic_selection = ToolSelectionOutput(
        tool_name="velocity_check",
        tool_input={"account_id": "TXN_HAPPY_001"},
        reasoning="check velocity",
    )
    deterministic_evaluation = EvaluationOutput(
        confidence=0.85,
        verdict="suspicious",
        finding="Velocity anomaly detected",
        recommendation="escalate_to_compliance",
        narrative="Evidence shows unusual transaction velocity.",
        should_continue=False,
    )
    mock_tool_result = ToolResult(
        success=True,
        tool_name="velocity_check",
        data={"account_id": "TXN_HAPPY_001", "windows": {"1h": {"count": 5, "volume_inr": 500000}}},
    )

    def call_llm_side_effect(messages, response_model):
        if response_model is EvaluationOutput:
            return (deterministic_evaluation, 0.002)
        return (deterministic_selection, 0.001)

    with patch("agent.graph.call_llm", side_effect=call_llm_side_effect), \
         patch("agent.graph.dispatch_tool", return_value=mock_tool_result):
        result = AMLGraph.invoke(initial)

    assert result["status"] == InvestigationStatus.RESOLVED, (
        f"Expected RESOLVED, got {result['status']}. escalation_reason={result.get('escalation_reason')}"
    )
    assert result["final_report"] is not None, "final_report must be set on RESOLVED"
    verdict = result["final_report"]["verdict"]
    assert verdict in {"suspicious", "clean", "inconclusive"}, f"Invalid verdict: {verdict}"
    assert result["hop_count"] >= 1, "At least one tool hop must have occurred"


def test_max_hops():
    """hop_count == MAX_TOOL_HOPS at EVALUATING forces ESCALATED with reason 'max_hops'."""
    state = make_state(hop_count=MAX_TOOL_HOPS)
    result = node_evaluating(state)
    assert result["escalation_reason"] == "max_hops", (
        f"Expected 'max_hops', got '{result['escalation_reason']}'"
    )
    # Route should confirm ESCALATED path
    assert result["escalation_reason"] is not None


def test_low_confidence():
    """confidence < CONFIDENCE_THRESHOLD forces ESCALATED with reason 'low_confidence'."""
    state = make_state(
        hop_count=1,
        evaluation={
            "confidence": 0.65,  # below 0.70 threshold
            "verdict": "suspicious",
            "finding": "Borderline pattern",
            "recommendation": "monitor",
            "narrative": "Low confidence finding.",
        },
    )
    result = node_evaluating(state)
    assert result["escalation_reason"] == "low_confidence", (
        f"Expected 'low_confidence', got '{result['escalation_reason']}'"
    )


def test_timeout():
    """Wall-clock elapsed > INVESTIGATION_TIMEOUT_SECONDS forces ESCALATED with reason 'timeout'."""
    # Set started_at to 61 seconds ago — exceeds 30s limit
    past_start = time.time() - (INVESTIGATION_TIMEOUT_SECONDS + 31.0)
    state = make_state(hop_count=1, started_at=past_start)
    result = node_evaluating(state)
    assert result["escalation_reason"] == "timeout", (
        f"Expected 'timeout', got '{result['escalation_reason']}'"
    )


def test_cost_cap():
    """accumulated_cost_usd > COST_CAP_USD forces ESCALATED with reason 'cost_cap'."""
    state = make_state(
        hop_count=1,
        accumulated_cost_usd=COST_CAP_USD + 0.01,  # 0.06 > 0.05
    )
    result = node_evaluating(state)
    assert result["escalation_reason"] == "cost_cap", (
        f"Expected 'cost_cap', got '{result['escalation_reason']}'"
    )


def test_empty_evidence():
    """Empty evidence_chain forces ESCALATED with reason 'empty_evidence' — even if confidence=0.99."""
    state = make_state(
        hop_count=1,
        evidence_chain=[],  # EMPTY — SM-07 trigger
        evaluation={
            "confidence": 0.99,  # very high confidence — must be ignored
            "verdict": "clean",
            "finding": "All clear",
            "recommendation": "close_clean",
            "narrative": "High confidence but no evidence.",
        },
    )
    result = node_evaluating(state)
    assert result["escalation_reason"] == "empty_evidence", (
        f"Expected 'empty_evidence', got '{result['escalation_reason']}'. "
        "SM-07: confidence cannot override empty evidence guard."
    )
