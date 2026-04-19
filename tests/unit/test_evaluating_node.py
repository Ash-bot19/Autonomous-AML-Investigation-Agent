"""
tests/unit/test_evaluating_node.py

Unit tests for evaluate_routing (Task 1) and node_evaluating (Task 2) after
real LLM wiring in Plan 04-03. All node_evaluating tests patch agent.graph.call_llm.
"""
from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest

from agent.limits import evaluate_routing, CONFIDENCE_THRESHOLD, MAX_TOOL_HOPS, COST_CAP_USD, INVESTIGATION_TIMEOUT_SECONDS
from agent.state import AgentState, InvestigationStatus
from models.schemas import EvaluationOutput, InvestigationPayload


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_payload(txn_id: str = "TXN_TEST_003") -> InvestigationPayload:
    return InvestigationPayload(
        txn_id=txn_id,
        trigger_type="rule_engine",
        trigger_detail="velocity breach",
        risk_score=0.82,
    )


def make_evidence_entry(hop: int = 1) -> dict:
    return {
        "hop": hop,
        "tool": "velocity_check",
        "finding": "23 transactions in 6 hours",
        "significance": "high",
    }


def make_eval_output(confidence: float = 0.85, should_continue: bool = False) -> EvaluationOutput:
    return EvaluationOutput(
        confidence=confidence,
        verdict="suspicious",
        finding="Velocity anomaly detected",
        recommendation="escalate_to_compliance",
        narrative="Evidence shows unusual transaction velocity.",
        should_continue=should_continue,
    )


# ── Task 1: evaluate_routing tests ────────────────────────────────────────────

def test_routing_should_continue_true_returns_tool_calling():
    state = {"payload": None, "evaluation": {"confidence": 0.95, "should_continue": True}}
    assert evaluate_routing(state) == "node_tool_calling"


def test_routing_should_continue_false_high_confidence_returns_resolved():
    state = {
        "payload": make_payload(),
        "evaluation": {"confidence": CONFIDENCE_THRESHOLD + 0.1, "should_continue": False},
    }
    assert evaluate_routing(state) == "node_resolved"


def test_routing_should_continue_false_low_confidence_returns_tool_calling():
    state = {
        "payload": make_payload(),
        "evaluation": {"confidence": CONFIDENCE_THRESHOLD - 0.1, "should_continue": False},
    }
    assert evaluate_routing(state) == "node_tool_calling"


def test_routing_no_evaluation_returns_tool_calling():
    state = {"payload": make_payload(), "evaluation": None}
    assert evaluate_routing(state) == "node_tool_calling"


# ── Task 2: node_evaluating tests ─────────────────────────────────────────────

def test_system_prompt_contains_verbatim_narrative_constraint():
    from agent.graph import SYSTEM_PROMPT_EVALUATION
    NARRATIVE_CONSTRAINT = (
        "You may only reference evidence from the tool results provided. "
        "Do not infer additional patterns not present in the data."
    )
    assert NARRATIVE_CONSTRAINT in SYSTEM_PROMPT_EVALUATION


def test_node_evaluating_calls_llm_and_accumulates_cost():
    from agent.graph import node_evaluating
    eval_out = make_eval_output(confidence=0.85, should_continue=False)
    state = AgentState(
        payload=make_payload(),
        status=InvestigationStatus.TOOL_CALLING,
        hop_count=1,
        accumulated_cost_usd=0.010,
        started_at=time.time(),
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=[make_evidence_entry()],
        evaluation=None,
        escalation_reason=None,
        final_report=None,
    )
    with patch("agent.graph.call_llm", return_value=(eval_out, 0.003)) as mock_llm:
        result = node_evaluating(state)
    mock_llm.assert_called_once()
    assert result["evaluation"]["confidence"] == 0.85
    assert abs(result["accumulated_cost_usd"] - 0.013) < 1e-9
    assert result["escalation_reason"] is None


def test_node_evaluating_user_message_includes_evidence_and_txn_id():
    from agent.graph import node_evaluating
    eval_out = make_eval_output(confidence=0.85)
    state = AgentState(
        payload=make_payload(txn_id="TXN_ACME_999"),
        status=InvestigationStatus.TOOL_CALLING,
        hop_count=2,
        accumulated_cost_usd=0.005,
        started_at=time.time(),
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=[make_evidence_entry(hop=1), make_evidence_entry(hop=2)],
        evaluation=None,
        escalation_reason=None,
        final_report=None,
    )
    with patch("agent.graph.call_llm", return_value=(eval_out, 0.002)) as mock_llm:
        node_evaluating(state)
    call_args = mock_llm.call_args
    messages = call_args.kwargs["messages"] if call_args.kwargs else call_args[1]["messages"]
    user_msg = next(m["content"] for m in messages if m["role"] == "user")
    assert "TXN_ACME_999" in user_msg
    assert "hop 1" in user_msg
    assert "hop 2" in user_msg


def test_node_evaluating_fallback_llm_triggers_low_confidence_escalation():
    from agent.graph import node_evaluating
    fallback = make_eval_output(confidence=0.0, should_continue=False)
    state = AgentState(
        payload=make_payload(),
        status=InvestigationStatus.TOOL_CALLING,
        hop_count=1,
        accumulated_cost_usd=0.0,
        started_at=time.time(),
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=[make_evidence_entry()],
        evaluation=None,
        escalation_reason=None,
        final_report=None,
    )
    with patch("agent.graph.call_llm", return_value=(fallback, 0.0)):
        result = node_evaluating(state)
    assert result["escalation_reason"] == "low_confidence"


def test_node_evaluating_empty_evidence_guard_fires_before_llm():
    from agent.graph import node_evaluating
    state = AgentState(
        payload=make_payload(),
        status=InvestigationStatus.TOOL_CALLING,
        hop_count=0,
        accumulated_cost_usd=0.0,
        started_at=time.time(),
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=[],
        evaluation=None,
        escalation_reason=None,
        final_report=None,
    )
    with patch("agent.graph.call_llm") as mock_llm:
        result = node_evaluating(state)
    assert mock_llm.call_count == 0
    assert result["escalation_reason"] == "empty_evidence"


def test_node_evaluating_max_hops_fires_before_llm():
    from agent.graph import node_evaluating
    state = AgentState(
        payload=make_payload(),
        status=InvestigationStatus.TOOL_CALLING,
        hop_count=MAX_TOOL_HOPS,
        accumulated_cost_usd=0.0,
        started_at=time.time(),
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=[make_evidence_entry()],
        evaluation=None,
        escalation_reason=None,
        final_report=None,
    )
    with patch("agent.graph.call_llm") as mock_llm:
        result = node_evaluating(state)
    assert mock_llm.call_count == 0
    assert result["escalation_reason"] == "max_hops"


def test_node_evaluating_cost_cap_fires_before_llm():
    from agent.graph import node_evaluating
    state = AgentState(
        payload=make_payload(),
        status=InvestigationStatus.TOOL_CALLING,
        hop_count=1,
        accumulated_cost_usd=COST_CAP_USD,
        started_at=time.time(),
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=[make_evidence_entry()],
        evaluation=None,
        escalation_reason=None,
        final_report=None,
    )
    with patch("agent.graph.call_llm") as mock_llm:
        result = node_evaluating(state)
    assert mock_llm.call_count == 0
    assert result["escalation_reason"] == "cost_cap"


def test_node_evaluating_timeout_fires_before_llm():
    from agent.graph import node_evaluating
    state = AgentState(
        payload=make_payload(),
        status=InvestigationStatus.TOOL_CALLING,
        hop_count=1,
        accumulated_cost_usd=0.0,
        started_at=time.time() - INVESTIGATION_TIMEOUT_SECONDS - 1,
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=[make_evidence_entry()],
        evaluation=None,
        escalation_reason=None,
        final_report=None,
    )
    with patch("agent.graph.call_llm") as mock_llm:
        result = node_evaluating(state)
    assert mock_llm.call_count == 0
    assert result["escalation_reason"] == "timeout"
