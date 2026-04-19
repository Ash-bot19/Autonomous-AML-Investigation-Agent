"""
tests/unit/test_evidence_builder.py — Unit tests for agent/evidence.py

All Postgres calls are mocked via unittest.mock.patch.
Tests do NOT require a running database.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.evidence import build_evidence_chain


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_pool(rows: list[dict]) -> MagicMock:
    """Build a mock pool whose conn.cursor().fetchall() returns rows."""
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall.return_value = rows

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn
    return mock_pool


# ── Test 1: Happy path — ordered EvidenceEntry list ──────────────────────────


@patch("agent.evidence._get_pool")
def test_happy_path_returns_ordered_entries(mock_get_pool):
    rows = [
        {
            "hop_number": 1,
            "tool_name": "velocity_check",
            "tool_output": {
                "success": True,
                "tool_name": "velocity_check",
                "data": {
                    "account_id": "ACC-001",
                    "windows": {
                        "1h": {"count": 3, "volume_inr": 1000},
                        "6h": {"count": 18, "volume_inr": 5000},
                        "24h": {"count": 23, "volume_inr": 7000},
                    },
                },
            },
        },
        {
            "hop_number": 2,
            "tool_name": "txn_history_query",
            "tool_output": {
                "success": True,
                "tool_name": "txn_history_query",
                "data": {
                    "account_id": "ACC-001",
                    "total_90d_count": 12,
                    "total_90d_volume_inr": 2100000,
                },
            },
        },
        {
            "hop_number": 3,
            "tool_name": "watchlist_lookup",
            "tool_output": {
                "success": True,
                "tool_name": "watchlist_lookup",
                "data": {
                    "queried_name": "John Doe",
                    "match": False,
                    "matched_entity": None,
                },
            },
        },
    ]
    mock_get_pool.return_value = _make_pool(rows)

    result = build_evidence_chain("test-investigation-uuid")

    assert len(result) == 3
    assert result[0].hop == 1
    assert result[0].tool == "velocity_check"
    assert result[1].hop == 2
    assert result[1].tool == "txn_history_query"
    assert result[2].hop == 3
    assert result[2].tool == "watchlist_lookup"


# ── Test 2: Finding text contains tool-specific fields ───────────────────────


@patch("agent.evidence._get_pool")
def test_finding_text_per_tool_type(mock_get_pool):
    rows = [
        {
            "hop_number": 1,
            "tool_name": "velocity_check",
            "tool_output": {
                "success": True,
                "tool_name": "velocity_check",
                "data": {
                    "windows": {
                        "1h": {"count": 5, "volume_inr": 200000},
                        "6h": {"count": 12, "volume_inr": 500000},
                        "24h": {"count": 20, "volume_inr": 900000},
                    }
                },
            },
        },
        {
            "hop_number": 2,
            "tool_name": "counterparty_risk_lookup",
            "tool_output": {
                "success": True,
                "tool_name": "counterparty_risk_lookup",
                "data": {
                    "account_id": "ACC-002",
                    "risk_tier": "high",
                    "flag_reason": "suspicious_activity",
                },
            },
        },
        {
            "hop_number": 3,
            "tool_name": "round_trip_detector",
            "tool_output": {
                "success": True,
                "tool_name": "round_trip_detector",
                "data": {
                    "cycle_detected": True,
                    "cycle_path": "A→B→C→A",
                    "window_hours": 24,
                },
            },
        },
    ]
    mock_get_pool.return_value = _make_pool(rows)

    result = build_evidence_chain("test-uuid-2")

    # velocity_check finding must mention all three windows
    assert "1h:" in result[0].finding
    assert "6h:" in result[0].finding
    assert "24h:" in result[0].finding

    # counterparty_risk_lookup finding must mention risk_tier
    assert "high" in result[1].finding

    # round_trip_detector finding must mention cycle path
    assert "A→B→C→A" in result[2].finding


# ── Test 3: Significance — medium on success, low on failure ─────────────────


@patch("agent.evidence._get_pool")
def test_significance_based_on_success_flag(mock_get_pool):
    rows = [
        {
            "hop_number": 1,
            "tool_name": "txn_history_query",
            "tool_output": {
                "success": True,
                "tool_name": "txn_history_query",
                "data": {"total_90d_count": 5, "total_90d_volume_inr": 100000},
            },
        },
        {
            "hop_number": 2,
            "tool_name": "kafka_lag_check",
            "tool_output": {
                "success": False,
                "tool_name": "kafka_lag_check",
                "error": "Kafka broker unavailable",
            },
        },
    ]
    mock_get_pool.return_value = _make_pool(rows)

    result = build_evidence_chain("test-uuid-3")

    assert result[0].significance == "medium"  # success=True
    assert result[1].significance == "low"  # success=False


# ── Test 4: Empty result — zero rows returns empty list ───────────────────────


@patch("agent.evidence._get_pool")
def test_empty_rows_returns_empty_list(mock_get_pool):
    mock_get_pool.return_value = _make_pool([])

    result = build_evidence_chain("test-uuid-empty")

    assert result == []


# ── Test 5: Pool unavailable — returns [] and does not raise ─────────────────


@patch("agent.evidence._get_pool")
def test_pool_unavailable_returns_empty_list(mock_get_pool):
    mock_get_pool.return_value = None

    result = build_evidence_chain("test-uuid-no-pool")

    assert result == []


# ── Test 6: SQL query raises — returns [] and does not raise ─────────────────


@patch("agent.evidence._get_pool")
def test_sql_query_raises_returns_empty_list(mock_get_pool):
    import psycopg2

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.execute.side_effect = psycopg2.Error("connection lost")

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn
    mock_get_pool.return_value = mock_pool

    result = build_evidence_chain("test-uuid-sql-error")

    assert result == []


# ── Test 7: Unknown tool — produces entry with raw fallback, does not crash ───


@patch("agent.evidence._get_pool")
def test_unknown_tool_produces_raw_fallback_entry(mock_get_pool):
    rows = [
        {
            "hop_number": 1,
            "tool_name": "unknown_future_tool",
            "tool_output": {
                "success": True,
                "tool_name": "unknown_future_tool",
                "data": {"some_field": "some_value"},
            },
        },
    ]
    mock_get_pool.return_value = _make_pool(rows)

    result = build_evidence_chain("test-uuid-unknown")

    assert len(result) == 1
    assert result[0].tool == "unknown_future_tool"
    assert result[0].finding.startswith("raw:")
    assert result[0].significance == "medium"
