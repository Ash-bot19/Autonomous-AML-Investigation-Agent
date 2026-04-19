"""
tests/unit/test_escalation_writer.py

Unit tests for agent/escalation_writer.py.
All tests run in-memory — no PostgreSQL required. psycopg2 is fully mocked.
Covers build_partial_report (3 tests) and write_escalation (9 tests).
"""
from __future__ import annotations

import json
import pytest
import psycopg2
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from models.schemas import InvestigationPayload
from agent.state import AgentState, InvestigationStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_mock_pool_and_conn():
    """Returns (mock_pool, mock_conn, mock_cursor) with context-manager support."""
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    return mock_pool, mock_conn, mock_cursor


_SENTINEL = object()  # Distinguishes "use default" from explicit None


def make_full_state(
    investigation_id: str = "inv-test-001",
    txn_id: str = "TXN_FULL",
    hop_count: int = 3,
    accumulated_cost_usd: float = 0.012,
    evidence_chain: list | None = None,
    evaluation=_SENTINEL,
    escalation_reason: str | None = "low_confidence",
) -> AgentState:
    """Build a full AgentState for build_partial_report tests.

    Pass evaluation=None explicitly to get an AgentState with evaluation=None.
    Omit evaluation to get the default evaluation dict (inconclusive, confidence=0.5).
    """
    if evidence_chain is None:
        evidence_chain = [
            {"hop": 1, "tool": "velocity_check", "finding": "high velocity", "significance": "high"}
        ]
    if evaluation is _SENTINEL:
        evaluation = {
            "confidence": 0.5,
            "verdict": "inconclusive",
            "finding": "Borderline case",
            "recommendation": "escalate_to_compliance",
            "narrative": "Insufficient evidence to conclude.",
            "should_continue": False,
        }
    payload = InvestigationPayload(
        investigation_id=investigation_id,
        txn_id=txn_id,
        trigger_type="ml_score",
        trigger_detail="risk_score=0.81",
        risk_score=0.81,
    )
    return AgentState(
        payload=payload,
        status=InvestigationStatus.EVALUATING,
        hop_count=hop_count,
        accumulated_cost_usd=accumulated_cost_usd,
        started_at=None,
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=evidence_chain,
        evaluation=evaluation,
        escalation_reason=escalation_reason,
        final_report=None,
    )


# ── Tests: build_partial_report ───────────────────────────────────────────────

class TestBuildPartialReport:

    def test_full_state_returns_all_keys(self):
        """Test 1: build_partial_report on full state returns dict with all expected keys."""
        from agent.escalation_writer import build_partial_report

        state = make_full_state()
        result = build_partial_report(state)

        required_keys = {
            "investigation_id", "txn_id", "hop_count", "accumulated_cost_usd",
            "evidence_chain", "verdict", "confidence", "finding",
            "recommendation", "narrative", "escalation_reason",
        }
        assert required_keys.issubset(result.keys()), (
            f"Missing keys: {required_keys - set(result.keys())}"
        )
        assert result["investigation_id"] == "inv-test-001"
        assert result["txn_id"] == "TXN_FULL"
        assert result["hop_count"] == 3
        assert result["accumulated_cost_usd"] == pytest.approx(0.012)
        assert len(result["evidence_chain"]) == 1
        assert result["verdict"] == "inconclusive"
        assert result["confidence"] == pytest.approx(0.5)

    def test_none_evaluation_returns_none_verdict_and_confidence(self):
        """Test 2: build_partial_report when evaluation is None — must not raise KeyError."""
        from agent.escalation_writer import build_partial_report

        state = make_full_state(evaluation=None)
        result = build_partial_report(state)

        assert result["verdict"] is None
        assert result["confidence"] is None

    def test_empty_evidence_chain_does_not_crash(self):
        """Test 3: build_partial_report when evidence_chain is [] — must not crash."""
        from agent.escalation_writer import build_partial_report

        state = make_full_state(evidence_chain=[])
        result = build_partial_report(state)

        assert result["evidence_chain"] == []


# ── Tests: write_escalation ───────────────────────────────────────────────────

class TestWriteEscalation:

    @patch("agent.escalation_writer._get_pool")
    def test_low_confidence_reason_inserts_correctly(self, mock_get_pool):
        """Test 4: write_escalation with 'low_confidence' inserts correct row."""
        from agent.escalation_writer import write_escalation

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_escalation("inv-1", "txn-1", "low_confidence", 0.4, {"foo": "bar"})

        assert result is True
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

        sql, params = mock_cursor.execute.call_args[0]
        assert "INSERT INTO escalation_queue" in sql
        assert params[0] == "inv-1"            # investigation_id
        assert params[1] == "txn-1"            # txn_id
        assert params[2] == "low_confidence"   # reason
        assert params[3] == pytest.approx(0.4) # confidence
        # params[4] is partial_report JSON string
        assert isinstance(params[4], str)
        parsed = json.loads(params[4])
        assert parsed["foo"] == "bar"

    @patch("agent.escalation_writer._get_pool")
    def test_max_hops_reason_accepted(self, mock_get_pool):
        """Test 5: write_escalation accepts 'max_hops' reason."""
        from agent.escalation_writer import write_escalation

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_escalation("inv-2", "txn-2", "max_hops", None, {})
        assert result is True
        sql, params = mock_cursor.execute.call_args[0]
        assert params[2] == "max_hops"

    @patch("agent.escalation_writer._get_pool")
    def test_timeout_reason_accepted(self, mock_get_pool):
        """Test 6: write_escalation accepts 'timeout' reason."""
        from agent.escalation_writer import write_escalation

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_escalation("inv-3", "txn-3", "timeout", None, {})
        assert result is True
        sql, params = mock_cursor.execute.call_args[0]
        assert params[2] == "timeout"

    @patch("agent.escalation_writer._get_pool")
    def test_cost_cap_reason_accepted(self, mock_get_pool):
        """Test 7: write_escalation accepts 'cost_cap' reason."""
        from agent.escalation_writer import write_escalation

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_escalation("inv-4", "txn-4", "cost_cap", None, {})
        assert result is True
        sql, params = mock_cursor.execute.call_args[0]
        assert params[2] == "cost_cap"

    @patch("agent.escalation_writer._get_pool")
    def test_empty_evidence_reason_accepted(self, mock_get_pool):
        """Test 8: write_escalation accepts 'empty_evidence' reason."""
        from agent.escalation_writer import write_escalation

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_escalation("inv-5", "txn-5", "empty_evidence", None, {})
        assert result is True
        sql, params = mock_cursor.execute.call_args[0]
        assert params[2] == "empty_evidence"

    @patch("agent.escalation_writer._get_pool")
    def test_invalid_reason_returns_false_no_insert(self, mock_get_pool):
        """Test 9: write_escalation rejects invalid reason — returns False, no insert attempted."""
        from agent.escalation_writer import write_escalation

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_escalation("inv-6", "txn-6", "other", None, {})

        assert result is False
        mock_cursor.execute.assert_not_called()

    @patch("agent.escalation_writer._get_pool")
    def test_pool_unavailable_returns_false_no_raise(self, mock_get_pool):
        """Test 10: write_escalation with pool=None returns False, does not raise."""
        from agent.escalation_writer import write_escalation

        mock_get_pool.return_value = None

        result = write_escalation("inv-7", "txn-7", "max_hops", None, {})

        assert result is False

    @patch("agent.escalation_writer._get_pool")
    def test_db_error_returns_false_and_rolls_back(self, mock_get_pool):
        """Test 11: write_escalation with cursor.execute raising psycopg2.Error returns False, rolls back."""
        from agent.escalation_writer import write_escalation

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_cursor.execute.side_effect = psycopg2.OperationalError("connection lost")
        mock_get_pool.return_value = mock_pool

        result = write_escalation("inv-8", "txn-8", "timeout", None, {})

        assert result is False
        mock_conn.rollback.assert_called_once()

    @patch("agent.escalation_writer._get_pool")
    def test_partial_report_serialised_as_json_with_jsonb_cast(self, mock_get_pool):
        """Test 12: partial_report is serialised via json.dumps with ::jsonb cast — not string-concatenated."""
        from agent.escalation_writer import write_escalation

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        partial = {"hop_count": 2, "verdict": "inconclusive", "confidence": 0.55}
        write_escalation("inv-9", "txn-9", "low_confidence", 0.55, partial)

        sql, params = mock_cursor.execute.call_args[0]
        # SQL must contain ::jsonb cast
        assert "::jsonb" in sql
        # params[4] must be a JSON string
        assert isinstance(params[4], str)
        parsed = json.loads(params[4])
        assert parsed["hop_count"] == 2
        assert parsed["verdict"] == "inconclusive"
