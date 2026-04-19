"""
tests/unit/test_audit_trail.py

Unit tests for agent/audit_trail.py.
All tests run in-memory — no PostgreSQL required. psycopg2 is fully mocked.
Covers all 5 event types, pool failure, DB error, and JSON serialisation.
"""
from __future__ import annotations

import json
import pytest
import psycopg2
from unittest.mock import MagicMock, patch


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


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestWriteAuditEvent:

    @patch("agent.audit_trail._get_pool")
    def test_triggered_event_inserts_correctly(self, mock_get_pool):
        """Test 1: 'triggered' event inserts one row with correct fields."""
        from agent.audit_trail import write_audit_event

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_audit_event(
            "inv-1",
            "triggered",
            event_detail={"trigger_type": "ml_score"},
            state_from=None,
            state_to="INVESTIGATING",
        )

        assert result is True
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

        sql, params = mock_cursor.execute.call_args[0]
        assert "INSERT INTO investigation_audit_log" in sql
        assert params[0] == "inv-1"           # investigation_id
        assert params[1] == "triggered"        # event_type
        # params[2] is event_detail JSON string
        assert params[3] is None               # state_from
        assert params[4] == "INVESTIGATING"    # state_to
        assert params[5] == 0.0               # cost_usd_delta default

    @patch("agent.audit_trail._get_pool")
    def test_state_change_event_both_state_columns(self, mock_get_pool):
        """Test 2: 'state_change' event sets both state_from and state_to."""
        from agent.audit_trail import write_audit_event

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_audit_event(
            "inv-1",
            "state_change",
            state_from="INVESTIGATING",
            state_to="TOOL_CALLING",
        )

        assert result is True
        sql, params = mock_cursor.execute.call_args[0]
        assert "INSERT INTO investigation_audit_log" in sql
        assert params[1] == "state_change"
        assert params[3] == "INVESTIGATING"
        assert params[4] == "TOOL_CALLING"

    @patch("agent.audit_trail._get_pool")
    def test_tool_call_event_with_cost_delta(self, mock_get_pool):
        """Test 3: 'tool_call' event preserves cost_usd_delta."""
        from agent.audit_trail import write_audit_event

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_audit_event(
            "inv-1",
            "tool_call",
            event_detail={"tool_name": "velocity_check", "hop": 1},
            cost_usd_delta=0.0012,
        )

        assert result is True
        sql, params = mock_cursor.execute.call_args[0]
        assert params[1] == "tool_call"
        assert params[5] == pytest.approx(0.0012)

    @patch("agent.audit_trail._get_pool")
    def test_resolved_event_succeeds(self, mock_get_pool):
        """Test 4: 'resolved' event inserts without error."""
        from agent.audit_trail import write_audit_event

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_audit_event(
            "inv-1",
            "resolved",
            event_detail={"verdict": "clean"},
            state_from="EVALUATING",
            state_to="RESOLVED",
        )

        assert result is True
        sql, params = mock_cursor.execute.call_args[0]
        assert params[1] == "resolved"
        assert params[3] == "EVALUATING"
        assert params[4] == "RESOLVED"

    @patch("agent.audit_trail._get_pool")
    def test_escalated_event_succeeds(self, mock_get_pool):
        """Test 5: 'escalated' event inserts without error."""
        from agent.audit_trail import write_audit_event

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        result = write_audit_event(
            "inv-1",
            "escalated",
            event_detail={"reason": "max_hops"},
            state_from="EVALUATING",
            state_to="ESCALATED",
        )

        assert result is True
        sql, params = mock_cursor.execute.call_args[0]
        assert params[1] == "escalated"

    @patch("agent.audit_trail._get_pool")
    def test_pool_unavailable_returns_false_no_raise(self, mock_get_pool):
        """Test 6: When _get_pool() returns None, write_audit_event returns False and does not raise."""
        from agent.audit_trail import write_audit_event

        mock_get_pool.return_value = None

        result = write_audit_event("inv-1", "triggered")

        assert result is False

    @patch("agent.audit_trail._get_pool")
    def test_db_error_returns_false_and_rolls_back(self, mock_get_pool):
        """Test 7: When cursor.execute raises psycopg2.Error, returns False and calls rollback."""
        from agent.audit_trail import write_audit_event

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_cursor.execute.side_effect = psycopg2.OperationalError("connection lost")
        mock_get_pool.return_value = mock_pool

        result = write_audit_event("inv-1", "tool_call")

        assert result is False
        mock_conn.rollback.assert_called_once()

    @patch("agent.audit_trail._get_pool")
    def test_event_detail_serialised_as_json_string(self, mock_get_pool):
        """Test 8: event_detail is serialised via json.dumps — never a raw dict passed to SQL."""
        from agent.audit_trail import write_audit_event

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        detail = {"tool_name": "velocity_check", "hop": 2}
        write_audit_event("inv-1", "tool_call", event_detail=detail)

        sql, params = mock_cursor.execute.call_args[0]
        # params[2] is event_detail — must be a JSON string, not a dict
        event_detail_param = params[2]
        assert isinstance(event_detail_param, str), "event_detail must be JSON string"
        parsed = json.loads(event_detail_param)
        assert parsed["tool_name"] == "velocity_check"
        assert parsed["hop"] == 2
