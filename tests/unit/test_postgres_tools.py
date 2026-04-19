"""
tests/unit/test_postgres_tools.py

Unit tests for tools/postgres_tools.py.
All tests use unittest.mock to avoid requiring a real PostgreSQL connection.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from models.schemas import ToolResult, TxnHistoryInput, CounterpartyRiskInput, RoundTripInput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool_mock(fetchall_return=None, fetchone_return=None):
    """Return a MagicMock that looks like a psycopg2 SimpleConnectionPool."""
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = fetchall_return or []
    cursor_mock.fetchone.return_value = fetchone_return

    # Support `with conn.cursor() as cur:`
    cursor_mock.__enter__ = lambda s: cursor_mock
    cursor_mock.__exit__ = MagicMock(return_value=False)

    conn_mock = MagicMock()
    conn_mock.cursor.return_value = cursor_mock

    pool_mock = MagicMock()
    pool_mock.getconn.return_value = conn_mock

    return pool_mock, cursor_mock


# ---------------------------------------------------------------------------
# txn_history_query
# ---------------------------------------------------------------------------

class TestTxnHistoryQuery:
    def test_returns_correct_shape_with_rows(self):
        """txn_history_query returns data with account_id, transactions list,
        total_90d_count, total_90d_volume_inr when cursor returns 2 rows."""
        from datetime import datetime, timezone
        ts = datetime(2026, 4, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [
            ("TXN_A", "ACC_CP1", 150000.00, ts),
            ("TXN_B", "ACC_CP1", 200000.00, ts),
        ]
        pool_mock, _ = _make_pool_mock(fetchall_return=rows)

        with patch("tools.postgres_tools._pool", pool_mock):
            from tools.postgres_tools import txn_history_query
            result = txn_history_query(TxnHistoryInput(account_id="ACC_S1_001"))

        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.data is not None
        assert result.data["account_id"] == "ACC_S1_001"
        assert len(result.data["transactions"]) == 2
        assert result.data["total_90d_count"] == 2
        assert result.data["total_90d_volume_inr"] == 350000.00

        txn = result.data["transactions"][0]
        assert "txn_id" in txn
        assert "amount" in txn
        assert "counterparty" in txn
        assert "timestamp" in txn

    def test_returns_empty_list_when_no_rows(self):
        """txn_history_query returns empty transactions list for unknown account."""
        pool_mock, _ = _make_pool_mock(fetchall_return=[])

        with patch("tools.postgres_tools._pool", pool_mock):
            from tools.postgres_tools import txn_history_query
            result = txn_history_query(TxnHistoryInput(account_id="NONEXISTENT"))

        assert result.success is True
        assert result.data["transactions"] == []
        assert result.data["total_90d_count"] == 0
        assert result.data["total_90d_volume_inr"] == 0.0

    def test_returns_failure_when_pool_is_none(self):
        """txn_history_query returns ToolResult(success=False) when _pool is None."""
        with patch("tools.postgres_tools._pool", None):
            from tools.postgres_tools import txn_history_query
            result = txn_history_query(TxnHistoryInput(account_id="ANY"))

        assert result.success is False
        assert result.error is not None
        assert "pool" in result.error.lower() or "connection" in result.error.lower()


# ---------------------------------------------------------------------------
# counterparty_risk_lookup
# ---------------------------------------------------------------------------

class TestCounterpartyRiskLookup:
    def test_returns_risk_data_when_record_exists(self):
        """counterparty_risk_lookup returns correct tier + reason when row found."""
        pool_mock, cursor_mock = _make_pool_mock()
        cursor_mock.fetchone.return_value = ("ACC_S1_CP", "high", "Shell company linked to 3 prior investigations")

        with patch("tools.postgres_tools._pool", pool_mock):
            from tools.postgres_tools import counterparty_risk_lookup
            result = counterparty_risk_lookup(CounterpartyRiskInput(account_id="ACC_S1_CP"))

        assert result.success is True
        assert result.data["account_id"] == "ACC_S1_CP"
        assert result.data["risk_tier"] == "high"
        assert result.data["flag_reason"] is not None

    def test_returns_unknown_when_no_record(self):
        """counterparty_risk_lookup returns risk_tier=unknown when no row found."""
        pool_mock, cursor_mock = _make_pool_mock()
        cursor_mock.fetchone.return_value = None

        with patch("tools.postgres_tools._pool", pool_mock):
            from tools.postgres_tools import counterparty_risk_lookup
            result = counterparty_risk_lookup(CounterpartyRiskInput(account_id="MISSING_ACCOUNT"))

        assert result.success is True
        assert result.data["account_id"] == "MISSING_ACCOUNT"
        assert result.data["risk_tier"] == "unknown"
        assert result.data["flag_reason"] == "No record found"

    def test_returns_failure_when_pool_is_none(self):
        """counterparty_risk_lookup returns ToolResult(success=False) when pool is None."""
        with patch("tools.postgres_tools._pool", None):
            from tools.postgres_tools import counterparty_risk_lookup
            result = counterparty_risk_lookup(CounterpartyRiskInput(account_id="ANY"))

        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# round_trip_detector
# ---------------------------------------------------------------------------

class TestRoundTripDetector:
    def test_returns_no_cycle_when_no_rows(self):
        """round_trip_detector returns cycle_detected=False when CTE returns empty."""
        pool_mock, _ = _make_pool_mock(fetchall_return=[])

        with patch("tools.postgres_tools._pool", pool_mock):
            from tools.postgres_tools import round_trip_detector
            result = round_trip_detector(RoundTripInput(account_id="ACC_S3_001", window_hours=24))

        assert result.success is True
        assert result.data["cycle_detected"] is False
        assert result.data["cycle_path"] is None
        assert result.data["window_hours"] == 24

    def test_returns_cycle_when_row_found(self):
        """round_trip_detector returns cycle_detected=True with path when CTE finds a cycle."""
        cycle_path = ["ACC_A", "ACC_B", "ACC_C", "ACC_A"]
        pool_mock, _ = _make_pool_mock(fetchall_return=[(cycle_path,)])

        with patch("tools.postgres_tools._pool", pool_mock):
            from tools.postgres_tools import round_trip_detector
            result = round_trip_detector(RoundTripInput(account_id="ACC_A", window_hours=24))

        assert result.success is True
        assert result.data["cycle_detected"] is True
        assert result.data["cycle_path"] == cycle_path

    def test_returns_failure_when_pool_is_none(self):
        """round_trip_detector returns ToolResult(success=False) when pool is None."""
        with patch("tools.postgres_tools._pool", None):
            from tools.postgres_tools import round_trip_detector
            result = round_trip_detector(RoundTripInput(account_id="ANY"))

        assert result.success is False
        assert result.error is not None
