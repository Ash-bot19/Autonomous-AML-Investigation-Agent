"""
tests/unit/test_investigating_node.py

Unit tests for node_investigating (Task 1) and node_tool_calling (Task 2) after
real call_llm wiring (Plan 04-02). All tests patch agent.graph.call_llm and
agent.graph.dispatch_tool — zero real OpenAI or infrastructure calls.
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import patch, MagicMock

from models.schemas import (
    InvestigationPayload,
    ToolSelectionOutput,
    EvaluationOutput,
    ToolResult,
)
from agent.state import AgentState, InvestigationStatus
from agent.graph import (
    node_investigating,
    node_tool_calling,
    SYSTEM_PROMPT_TOOL_SELECTION,
    TOOL_DESCRIPTIONS,
)

VALID_TOOL_NAMES = list(TOOL_DESCRIPTIONS.keys())


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_payload(txn_id: str = "TXN_TEST_002") -> InvestigationPayload:
    return InvestigationPayload(
        txn_id=txn_id,
        trigger_type="rule_engine",
        trigger_detail="velocity breach: 18 txns in 2h",
        risk_score=0.82,
    )


def make_initial_state(payload: InvestigationPayload | None = None) -> AgentState:
    """Minimal state before node_investigating runs."""
    return AgentState(
        payload=payload or make_payload(),
        status=InvestigationStatus.IDLE,
        hop_count=0,
        accumulated_cost_usd=0.0,
        started_at=None,
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=[],
        evaluation=None,
        escalation_reason=None,
        final_report=None,
    )


def make_post_investigating_state(
    tool_name: str = "velocity_check",
    cost: float = 0.001,
    payload: InvestigationPayload | None = None,
) -> AgentState:
    """State that node_tool_calling receives after node_investigating ran."""
    p = payload or make_payload()
    return AgentState(
        payload=p,
        status=InvestigationStatus.INVESTIGATING,
        hop_count=0,
        accumulated_cost_usd=cost,
        started_at=time.time(),
        tool_selection={"tool_name": tool_name, "tool_input": {"account_id": p.txn_id}, "reasoning": "initial"},
        last_tool_result=None,
        evidence_chain=[],
        evaluation=None,
        escalation_reason=None,
        final_report=None,
    )


# ── Task 1: node_investigating ─────────────────────────────────────────────────

class TestNodeInvestigating:

    @patch("agent.graph.call_llm")
    def test_returns_selected_tool_and_accumulates_cost(self, mock_call_llm):
        """Test 1: LLM returns ToolSelectionOutput — result has correct tool_name and cost."""
        mock_call_llm.return_value = (
            ToolSelectionOutput(
                tool_name="velocity_check",
                tool_input={"account_id": "ACC_001"},
                reasoning="start with velocity since rule engine fired on velocity breach",
            ),
            0.001,
        )
        state = make_initial_state()
        result = node_investigating(state)

        assert result["tool_selection"]["tool_name"] == "velocity_check"
        assert result["accumulated_cost_usd"] == pytest.approx(0.001)
        assert result["status"] == InvestigationStatus.INVESTIGATING
        assert result["hop_count"] == 0

    @patch("agent.graph.call_llm")
    def test_system_prompt_contains_aml_investigation_and_all_tools(self, mock_call_llm):
        """Test 2: System prompt includes 'AML investigation' and all 6 tool names."""
        mock_call_llm.return_value = (
            ToolSelectionOutput(
                tool_name="velocity_check",
                tool_input={"account_id": "ACC_001"},
                reasoning="check velocity",
            ),
            0.001,
        )
        state = make_initial_state()
        node_investigating(state)

        # Verify call_llm was called with our system prompt constant
        call_args = mock_call_llm.call_args
        messages = call_args.kwargs.get("messages") or call_args.args[0]
        system_msg = next(m["content"] for m in messages if m["role"] == "system")

        assert "AML investigation" in system_msg
        for tool_name in VALID_TOOL_NAMES:
            assert tool_name in system_msg, f"Tool '{tool_name}' missing from system prompt"

    @patch("agent.graph.call_llm")
    def test_user_message_includes_payload_fields(self, mock_call_llm):
        """Test 3: User message includes txn_id, trigger_detail, and risk_score from payload."""
        mock_call_llm.return_value = (
            ToolSelectionOutput(
                tool_name="txn_history_query",
                tool_input={"account_id": "TXN_TEST_002"},
                reasoning="examine history",
            ),
            0.001,
        )
        payload = make_payload("TXN_TEST_002")
        state = make_initial_state(payload=payload)
        node_investigating(state)

        call_args = mock_call_llm.call_args
        messages = call_args.kwargs.get("messages") or call_args.args[0]
        user_msg = next(m["content"] for m in messages if m["role"] == "user")

        assert "TXN_TEST_002" in user_msg
        assert "velocity breach: 18 txns in 2h" in user_msg
        assert "0.82" in user_msg

    @patch("agent.graph.call_llm")
    def test_fallback_when_llm_returns_evaluation_output(self, mock_call_llm):
        """Test 4: When call_llm returns EvaluationOutput (API failure), node falls back to safe default."""
        mock_call_llm.return_value = (
            EvaluationOutput(
                confidence=0.0,
                verdict="inconclusive",
                finding="x",
                recommendation="escalate_to_compliance",
                narrative="y",
                should_continue=False,
            ),
            0.0,
        )
        state = make_initial_state()
        # Must not raise
        result = node_investigating(state)

        assert isinstance(result["tool_selection"], dict)
        assert result["tool_selection"]["tool_name"] in VALID_TOOL_NAMES
        assert result["tool_selection"]["reasoning"].startswith("fallback")
        assert result["accumulated_cost_usd"] == pytest.approx(0.0)

    @patch("agent.graph.call_llm")
    def test_started_at_and_hop_count_are_set(self, mock_call_llm):
        """Test 5: started_at is set and hop_count=0 regardless of LLM output (no regression)."""
        mock_call_llm.return_value = (
            ToolSelectionOutput(
                tool_name="watchlist_lookup",
                tool_input={"entity_name": "Some Entity"},
                reasoning="check watchlist",
            ),
            0.002,
        )
        state = make_initial_state()
        result = node_investigating(state)

        assert result["hop_count"] == 0
        assert result["started_at"] is not None
        assert isinstance(result["started_at"], float)


# ── Task 2: node_tool_calling ─────────────────────────────────────────────────

class TestNodeToolCalling:

    @patch("agent.graph.dispatch_tool")
    @patch("agent.graph.call_llm")
    def test_uses_llm_for_next_tool_selection(self, mock_call_llm, mock_dispatch):
        """Test 1: After tool execution, node_tool_calling calls call_llm to pick next tool."""
        mock_dispatch.return_value = ToolResult(
            success=True,
            tool_name="velocity_check",
            data={"account_id": "ACC_001", "windows": {"1h": {"count": 5, "volume_inr": 500000}}},
        )
        mock_call_llm.return_value = (
            ToolSelectionOutput(
                tool_name="counterparty_risk_lookup",
                tool_input={"account_id": "ACC_001"},
                reasoning="check counterparty after velocity breach",
            ),
            0.002,
        )
        state = make_post_investigating_state(tool_name="velocity_check", cost=0.001)
        result = node_tool_calling(state)

        assert result["tool_selection"]["tool_name"] == "counterparty_risk_lookup"

    @patch("agent.graph.dispatch_tool")
    @patch("agent.graph.call_llm")
    def test_cost_from_next_selection_is_added_to_accumulated(self, mock_call_llm, mock_dispatch):
        """Test 2: LLM cost for next-tool selection is added to accumulated_cost_usd (0.001 + 0.002 = 0.003)."""
        mock_dispatch.return_value = ToolResult(
            success=True,
            tool_name="velocity_check",
            data={"account_id": "ACC_001", "windows": {"1h": {"count": 3, "volume_inr": 300000}}},
        )
        mock_call_llm.return_value = (
            ToolSelectionOutput(
                tool_name="counterparty_risk_lookup",
                tool_input={"account_id": "ACC_001"},
                reasoning="next check",
            ),
            0.002,
        )
        state = make_post_investigating_state(cost=0.001)
        result = node_tool_calling(state)

        assert result["accumulated_cost_usd"] == pytest.approx(0.003)

    @patch("agent.graph.dispatch_tool")
    @patch("agent.graph.call_llm")
    def test_fallback_when_next_selection_llm_fails(self, mock_call_llm, mock_dispatch):
        """Test 3: When LLM returns EvaluationOutput fallback, node sets safe default tool_selection."""
        mock_dispatch.return_value = ToolResult(
            success=True,
            tool_name="velocity_check",
            data={"account_id": "ACC_001", "windows": {"1h": {"count": 2, "volume_inr": 200000}}},
        )
        mock_call_llm.return_value = (
            EvaluationOutput(
                confidence=0.0,
                verdict="inconclusive",
                finding="x",
                recommendation="escalate_to_compliance",
                narrative="y",
                should_continue=False,
            ),
            0.0,
        )
        state = make_post_investigating_state(cost=0.001)
        # Must not raise
        result = node_tool_calling(state)

        assert isinstance(result["tool_selection"], dict)
        assert result["tool_selection"]["tool_name"] in VALID_TOOL_NAMES
        assert result["tool_selection"]["reasoning"].startswith("fallback")

    @patch("agent.graph.dispatch_tool")
    @patch("agent.graph.call_llm")
    def test_hop_count_incremented_regardless_of_llm_outcome(self, mock_call_llm, mock_dispatch):
        """Test 4: hop_count is incremented from N to N+1 regardless of LLM outcome."""
        mock_dispatch.return_value = ToolResult(
            success=True,
            tool_name="velocity_check",
            data={"account_id": "ACC_001", "windows": {"1h": {"count": 1, "volume_inr": 100000}}},
        )
        mock_call_llm.return_value = (
            EvaluationOutput(
                confidence=0.0,
                verdict="inconclusive",
                finding="x",
                recommendation="escalate_to_compliance",
                narrative="y",
                should_continue=False,
            ),
            0.0,
        )
        state = make_post_investigating_state(cost=0.001)
        assert state["hop_count"] == 0
        result = node_tool_calling(state)
        assert result["hop_count"] == 1

    @patch("agent.graph.dispatch_tool")
    @patch("agent.graph.call_llm")
    def test_evidence_chain_appended_even_when_next_selection_fails(self, mock_call_llm, mock_dispatch):
        """Test 5: evidence_chain entry from the just-executed tool is appended even when next-selection LLM fails."""
        mock_dispatch.return_value = ToolResult(
            success=True,
            tool_name="velocity_check",
            data={"account_id": "ACC_001", "windows": {"1h": {"count": 7, "volume_inr": 700000}}},
        )
        mock_call_llm.return_value = (
            EvaluationOutput(
                confidence=0.0,
                verdict="inconclusive",
                finding="x",
                recommendation="escalate_to_compliance",
                narrative="y",
                should_continue=False,
            ),
            0.0,
        )
        state = make_post_investigating_state(cost=0.001)
        result = node_tool_calling(state)

        # The evidence from velocity_check must be in the chain
        assert len(result["evidence_chain"]) == 1
        entry = result["evidence_chain"][0]
        assert entry["tool"] == "velocity_check"
        assert entry["hop"] == 1
