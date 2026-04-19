"""
tests/integration/test_tools_integration.py

Integration tests for the 6 real tool implementations.
These tests require live PostgreSQL, Redis, and the seed data from scripts/seed.py.

Run with:
    python -m pytest tests/integration/ -m integration -v

Skip condition: tests skip if PostgreSQL or Redis is unreachable.
"""
from __future__ import annotations

import os
import pytest
import psycopg2
import redis as redis_lib
from unittest.mock import patch, MagicMock
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()


def _pg_available() -> bool:
    try:
        user = quote_plus(os.environ.get("POSTGRES_USER", ""))
        password = quote_plus(os.environ.get("POSTGRES_PASSWORD", ""))
        if not user or not password:
            return False
        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = os.environ.get("POSTGRES_PORT", "5432")
        db = os.environ.get("POSTGRES_DB", "")
        if not db:
            return False
        conn = psycopg2.connect(
            f"postgresql://{user}:{password}@{host}:{port}/{db}",
            connect_timeout=3,
        )
        conn.close()
        return True
    except Exception:
        return False


def _redis_available() -> bool:
    try:
        r = redis_lib.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            socket_connect_timeout=1,
        )
        r.ping()
        r.close()
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL not available — start with docker compose up -d postgres",
)
requires_redis = pytest.mark.skipif(
    not _redis_available(),
    reason="Redis not available — start with docker compose up -d redis",
)


# ── Test 1: txn_history_query — seeded account ACC_S1_001 ───────────────────


@pytest.mark.integration
@requires_pg
def test_txn_history_query_seeded_account():
    from tools.postgres_tools import txn_history_query
    from models.schemas import TxnHistoryInput, ToolResult

    result = txn_history_query(TxnHistoryInput(account_id="ACC_S1_001"))

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.data is not None
    assert result.data["account_id"] == "ACC_S1_001"
    assert isinstance(result.data["transactions"], list)
    assert len(result.data["transactions"]) >= 1, (
        "Seeded data must produce at least 1 transaction"
    )
    assert "total_90d_count" in result.data
    assert "total_90d_volume_inr" in result.data
    assert result.data["total_90d_count"] >= 1
    # Verify transaction dict shape
    txn = result.data["transactions"][0]
    assert "txn_id" in txn
    assert "amount" in txn
    assert "counterparty" in txn
    assert "timestamp" in txn


# ── Test 2: counterparty_risk_lookup — high-risk and clean counterparties ────


@pytest.mark.integration
@requires_pg
def test_counterparty_risk_lookup_high_risk():
    from tools.postgres_tools import counterparty_risk_lookup
    from models.schemas import CounterpartyRiskInput

    result = counterparty_risk_lookup(CounterpartyRiskInput(account_id="ACC_S1_CP"))

    assert result.success is True
    assert result.data["risk_tier"] == "high"
    assert result.data["account_id"] == "ACC_S1_CP"
    assert result.data["flag_reason"] is not None


@pytest.mark.integration
@requires_pg
def test_counterparty_risk_lookup_clean():
    from tools.postgres_tools import counterparty_risk_lookup
    from models.schemas import CounterpartyRiskInput

    result = counterparty_risk_lookup(CounterpartyRiskInput(account_id="ACC_S3_CP"))

    assert result.success is True
    assert result.data["risk_tier"] == "low"


# ── Test 3: velocity_check — seeded account ACC_S1_001 ──────────────────────


@pytest.mark.integration
@requires_redis
@requires_pg
def test_velocity_check_seeded_account():
    from tools.redis_tools import velocity_check
    from models.schemas import VelocityCheckInput

    result = velocity_check(VelocityCheckInput(account_id="ACC_S1_001"))

    assert result.success is True
    assert "windows" in result.data
    windows = result.data["windows"]
    assert "1h" in windows and "6h" in windows and "24h" in windows
    # Scenario 1 has 17 transactions within the last 2 hours (spaced 7-119 min apart)
    # At seed time, transactions 1-8 are within 56 min (8*7), all in 1h window
    assert windows["1h"]["count"] >= 1, (
        "Seeded velocity data must be visible in 1h window"
    )
    assert windows["6h"]["count"] >= windows["1h"]["count"], "6h count >= 1h count"
    assert windows["24h"]["count"] >= windows["6h"]["count"], "24h count >= 6h count"
    assert isinstance(windows["1h"]["volume_inr"], (int, float))
    assert windows["1h"]["volume_inr"] > 0, (
        "volume_inr=0 means PostgreSQL sub-query failed or seed data missing"
    )


# ── Test 4: watchlist_lookup — CSV-backed, no infra required ────────────────


@pytest.mark.integration
def test_watchlist_lookup_match():
    from tools.static_tools import watchlist_lookup
    from models.schemas import WatchlistInput

    result = watchlist_lookup(WatchlistInput(entity_name="Viktor Kovalenko"))

    assert result.success is True
    assert result.data["match"] is True
    assert result.data["matched_entity"] == "Viktor Kovalenko"
    assert result.data["queried_name"] == "Viktor Kovalenko"


@pytest.mark.integration
def test_watchlist_lookup_no_match():
    from tools.static_tools import watchlist_lookup
    from models.schemas import WatchlistInput

    result = watchlist_lookup(WatchlistInput(entity_name="John Smith"))

    assert result.success is True
    assert result.data["match"] is False
    assert result.data["matched_entity"] is None


# ── Test 5: dispatch_tool routing — mocks log pool, no DB write ─────────────


@pytest.mark.integration
def test_dispatch_tool_routing():
    from tools.dispatcher import dispatch_tool

    # Mock the log pool so we don't need DB for this test
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_pool.getconn.return_value = mock_conn
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)

    with patch("tools.dispatcher._log_pool", mock_pool):
        result = dispatch_tool(
            "watchlist_lookup",
            {"entity_name": "Viktor Kovalenko"},
            "test-inv-001",
            1,
        )

    assert result is not None
    assert result.success is True
    assert result.data["match"] is True


@pytest.mark.integration
def test_dispatch_tool_unknown_name():
    from tools.dispatcher import dispatch_tool

    with patch("tools.dispatcher._log_pool", MagicMock()):
        result = dispatch_tool("nonexistent_tool", {}, "test-inv-002", 1)

    assert result.success is False
    assert "Unknown tool" in result.error
