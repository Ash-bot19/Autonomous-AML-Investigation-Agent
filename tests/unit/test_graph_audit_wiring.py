"""
tests/unit/test_graph_audit_wiring.py

Unit tests verifying audit trail and escalation queue writes are correctly wired
into all 5 node functions of agent/graph.py (Plan 05-02).

All DB calls are mocked — zero real Postgres or infrastructure required.
Patches target agent.graph.write_audit_event and agent.graph.write_escalation
(the names as imported into graph.py's namespace).

Coverage:
  Test 1: node_investigating — write_audit_event called with event_type='triggered'
  Test 2: node_investigating LLM fallback — write_audit_event still called with 'triggered'
  Test 3: node_tool_calling — write_audit_event called with event_type='tool_call'
  Test 4: node_tool_calling LLM fallback — write_audit_event still called with 'tool_call'
  Test 5: node_resolved — write_audit_event called with event_type='resolved'
  Test 6: node_escalated — write_audit_event called with event_type='escalated'
  Test 7: node_escalated — write_escalation called with reason + build_partial_report output
  Test 8: write_audit_event returning False does NOT crash node_investigating
  Test 9: write_escalation returning False does NOT crash node_escalated
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, call, patch

import pytest

from agent.state import AgentState, InvestigationStatus
from models.schemas import (
    EvaluationOutput,
    InvestigationPayload,
    ToolResult,
    ToolSelectionOutput,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_payload(txn_id: str = "TXN_AUDIT_001") -> InvestigationPayload:
    return InvestigationPayload(
        txn_id=txn_id,
        trigger_type="rule_engine",
        trigger_detail="amount > 1000000",
        risk_score=0.82,
    )


def make_base_state(
    payload: InvestigationPayload | None = None,
    status: str = InvestigationStatus.IDLE,
    hop_count: int = 0,
    evidence_chain: list | None = None,
    evaluation: dict | None = None,
    escalation_reason: str | None = None,
) -> AgentState:
    return AgentState(
        payload=payload or make_payload(),
        status=status,
        hop_count=hop_count,
        accumulated_cost_usd=0.0,
        started_at=None,
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=evidence_chain if evidence_chain is not None else [],
        evaluation=evaluation,
        escalation_reason=escalation_reason,
        final_report=None,
    )


def make_tool_selection() -> ToolSelectionOutput:
    return ToolSelectionOutput(
        tool_name="velocity_check",
        tool_input_json='{"account_id": "ACC_001"}',
        reasoning="check velocity after rule engine trigger",
    )


def make_evaluation_output(confidence: float = 0.85) -> EvaluationOutput:
    return EvaluationOutput(
        confidence=confidence,
        verdict="suspicious",
        finding="Velocity anomaly detected",
        recommendation="escalate_to_compliance",
        narrative="Evidence shows unusual transaction velocity.",
        should_continue=False,
    )


def make_tool_result() -> ToolResult:
    return ToolResult(
        success=True,
        tool_name="velocity_check",
        data={"windows": {"1h": {"count": 18, "volume_inr": 900000}}},
    )


def make_evidence_entry(hop: int = 1) -> dict:
    return {
        "hop": hop,
        "tool": "velocity_check",
        "finding": "18 transactions in 1 hour",
        "significance": "high",
    }


# ── Test 1: node_investigating — triggered audit row written ──────────────────

class TestNodeInvestigatingAudit:

    @patch("agent.graph.write_audit_event", return_value=True)
    @patch("agent.graph.call_llm")
    def test_triggered_audit_row_written_on_normal_path(self, mock_call_llm, mock_write_audit):
        """Test 1: node_investigating writes triggered audit row before LLM call."""
        from agent.graph import node_investigating

        mock_call_llm.return_value = (make_tool_selection(), 0.001)
        state = make_base_state()
        node_investigating(state)

        # Find the 'triggered' call among all write_audit_event calls
        triggered_calls = [
            c for c in mock_write_audit.call_args_list
            if c.kwargs.get("event_type") == "triggered"
            or (c.args and c.args[1] == "triggered")
        ]
        assert len(triggered_calls) == 1, (
            f"Expected 1 'triggered' audit call, got {len(triggered_calls)}"
        )
        triggered_call = triggered_calls[0]
        # state_to must be INVESTIGATING
        state_to = triggered_call.kwargs.get("state_to")
        assert state_to == InvestigationStatus.INVESTIGATING

    @patch("agent.graph.write_audit_event", return_value=True)
    @patch("agent.graph.call_llm")
    def test_triggered_audit_row_written_on_llm_fallback(self, mock_call_llm, mock_write_audit):
        """Test 2: node_investigating writes triggered audit row even on LLM fallback (escalation path)."""
        from agent.graph import node_investigating

        # LLM returns EvaluationOutput — triggers the fallback escalation branch
        mock_call_llm.return_value = (make_evaluation_output(confidence=0.0), 0.0)
        state = make_base_state()
        result = node_investigating(state)

        # The node should still have written the triggered row before reaching LLM
        triggered_calls = [
            c for c in mock_write_audit.call_args_list
            if c.kwargs.get("event_type") == "triggered"
            or (c.args and len(c.args) > 1 and c.args[1] == "triggered")
        ]
        assert len(triggered_calls) == 1
        # Node escalates on LLM fallback
        assert result["status"] == InvestigationStatus.ESCALATED


# ── Test 3: node_tool_calling — tool_call audit row written ──────────────────

class TestNodeToolCallingAudit:

    @patch("agent.graph.write_audit_event", return_value=True)
    @patch("agent.graph.dispatch_tool")
    @patch("agent.graph.call_llm")
    def test_tool_call_audit_row_written_on_normal_path(
        self, mock_call_llm, mock_dispatch, mock_write_audit
    ):
        """Test 3: node_tool_calling writes tool_call audit row after dispatch_tool returns."""
        from agent.graph import node_tool_calling

        mock_dispatch.return_value = make_tool_result()
        mock_call_llm.return_value = (make_tool_selection(), 0.002)

        state = make_base_state(
            status=InvestigationStatus.INVESTIGATING,
            hop_count=0,
        )
        state = {
            **state,
            "started_at": time.time(),
            "tool_selection": {
                "tool_name": "velocity_check",
                "tool_input_json": '{"account_id": "ACC_001"}',
                "reasoning": "check velocity",
            },
        }
        node_tool_calling(state)

        tool_call_calls = [
            c for c in mock_write_audit.call_args_list
            if c.kwargs.get("event_type") == "tool_call"
            or (c.args and len(c.args) > 1 and c.args[1] == "tool_call")
        ]
        assert len(tool_call_calls) == 1
        tc = tool_call_calls[0]
        event_detail = tc.kwargs.get("event_detail", {})
        assert event_detail.get("tool_name") == "velocity_check"

    @patch("agent.graph.write_audit_event", return_value=True)
    @patch("agent.graph.dispatch_tool")
    @patch("agent.graph.call_llm")
    def test_tool_call_audit_row_written_on_llm_fallback(
        self, mock_call_llm, mock_dispatch, mock_write_audit
    ):
        """Test 4: node_tool_calling writes tool_call audit row even when next-tool LLM fails."""
        from agent.graph import node_tool_calling

        mock_dispatch.return_value = make_tool_result()
        # LLM returns EvaluationOutput — triggers fallback branch
        mock_call_llm.return_value = (make_evaluation_output(confidence=0.0), 0.0)

        state = make_base_state(
            status=InvestigationStatus.INVESTIGATING,
            hop_count=0,
        )
        state = {
            **state,
            "started_at": time.time(),
            "tool_selection": {
                "tool_name": "velocity_check",
                "tool_input_json": '{"account_id": "ACC_001"}',
                "reasoning": "check velocity",
            },
        }
        node_tool_calling(state)

        tool_call_calls = [
            c for c in mock_write_audit.call_args_list
            if c.kwargs.get("event_type") == "tool_call"
            or (c.args and len(c.args) > 1 and c.args[1] == "tool_call")
        ]
        assert len(tool_call_calls) == 1


# ── Test 5: node_resolved — resolved audit row written ───────────────────────

class TestNodeResolvedAudit:

    @patch("agent.graph.write_audit_event", return_value=True)
    @patch("agent.graph.write_compliance_report", return_value=True)
    @patch("agent.graph.build_evidence_chain")
    def test_resolved_audit_row_written(
        self, mock_build_evidence, mock_write_report, mock_write_audit
    ):
        """Test 5: node_resolved writes resolved audit row after compliance_reports write."""
        from agent.graph import node_resolved

        mock_build_evidence.return_value = [make_evidence_entry()]

        state = make_base_state(
            status=InvestigationStatus.EVALUATING,
            hop_count=2,
            evidence_chain=[make_evidence_entry()],
            evaluation={
                "confidence": 0.87,
                "verdict": "suspicious",
                "finding": "Velocity anomaly detected",
                "recommendation": "escalate_to_compliance",
                "narrative": "Unusual velocity.",
                "should_continue": False,
            },
        )
        state = {**state, "accumulated_cost_usd": 0.031, "started_at": time.time()}

        node_resolved(state)

        resolved_calls = [
            c for c in mock_write_audit.call_args_list
            if c.kwargs.get("event_type") == "resolved"
            or (c.args and len(c.args) > 1 and c.args[1] == "resolved")
        ]
        assert len(resolved_calls) == 1
        rc = resolved_calls[0]
        assert rc.kwargs.get("state_from") == InvestigationStatus.EVALUATING
        assert rc.kwargs.get("state_to") == InvestigationStatus.RESOLVED
        event_detail = rc.kwargs.get("event_detail", {})
        assert "verdict" in event_detail


# ── Tests 6–9: node_escalated ─────────────────────────────────────────────────

class TestNodeEscalatedAudit:

    def _make_escalated_state(self, reason: str = "max_hops") -> dict:
        state = make_base_state(
            status=InvestigationStatus.EVALUATING,
            hop_count=4,
            evidence_chain=[make_evidence_entry()],
            evaluation={
                "confidence": 0.55,
                "verdict": "inconclusive",
                "finding": "Inconclusive evidence",
                "recommendation": "monitor",
                "narrative": "Insufficient evidence.",
                "should_continue": False,
            },
            escalation_reason=reason,
        )
        return {**state, "accumulated_cost_usd": 0.02, "started_at": time.time()}

    @patch("agent.graph.write_escalation", return_value=True)
    @patch("agent.graph.build_partial_report", return_value={"investigation_id": "test-id"})
    @patch("agent.graph.write_audit_event", return_value=True)
    def test_escalated_audit_row_written(
        self, mock_write_audit, mock_build_partial, mock_write_escalation
    ):
        """Test 6: node_escalated writes escalated audit row with correct event_type and states."""
        from agent.graph import node_escalated

        state = self._make_escalated_state("max_hops")
        node_escalated(state)

        escalated_calls = [
            c for c in mock_write_audit.call_args_list
            if c.kwargs.get("event_type") == "escalated"
            or (c.args and len(c.args) > 1 and c.args[1] == "escalated")
        ]
        assert len(escalated_calls) == 1
        ec = escalated_calls[0]
        assert ec.kwargs.get("state_from") == InvestigationStatus.EVALUATING
        assert ec.kwargs.get("state_to") == InvestigationStatus.ESCALATED
        event_detail = ec.kwargs.get("event_detail", {})
        assert event_detail.get("reason") == "max_hops"

    @patch("agent.graph.write_escalation", return_value=True)
    @patch("agent.graph.build_partial_report", return_value={"investigation_id": "test-id"})
    @patch("agent.graph.write_audit_event", return_value=True)
    def test_write_escalation_called_with_reason_and_partial_report(
        self, mock_write_audit, mock_build_partial, mock_write_escalation
    ):
        """Test 7: node_escalated calls write_escalation with reason=state.escalation_reason
        and partial_report=build_partial_report(state)."""
        from agent.graph import node_escalated

        state = self._make_escalated_state("low_confidence")
        node_escalated(state)

        mock_write_escalation.assert_called_once()
        call_kwargs = mock_write_escalation.call_args.kwargs
        assert call_kwargs["reason"] == "low_confidence"
        assert call_kwargs["partial_report"] == {"investigation_id": "test-id"}

    @patch("agent.graph.write_escalation", return_value=True)
    @patch("agent.graph.build_partial_report", return_value={})
    @patch("agent.graph.write_audit_event", return_value=False)  # DB failure
    def test_write_audit_event_failure_does_not_crash_node_investigating(
        self, mock_write_audit, mock_build_partial, mock_write_escalation
    ):
        """Test 8: write_audit_event returning False does not crash node_investigating."""
        from agent.graph import node_investigating

        with patch("agent.graph.call_llm", return_value=(make_tool_selection(), 0.001)):
            state = make_base_state()
            # Must not raise
            result = node_investigating(state)

        assert "status" in result

    @patch("agent.graph.write_escalation", return_value=False)  # DB failure
    @patch("agent.graph.build_partial_report", return_value={})
    @patch("agent.graph.write_audit_event", return_value=False)  # DB failure
    def test_write_escalation_failure_does_not_crash_node_escalated(
        self, mock_write_audit, mock_build_partial, mock_write_escalation
    ):
        """Test 9: write_escalation returning False does not crash node_escalated;
        node still returns status=ESCALATED."""
        from agent.graph import node_escalated

        state = self._make_escalated_state("timeout")
        # Must not raise
        result = node_escalated(state)

        assert result["status"] == InvestigationStatus.ESCALATED
