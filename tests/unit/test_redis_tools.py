"""
tests/unit/test_redis_tools.py

Unit tests for tools/redis_tools.py.
All tests mock _redis_client and tools.postgres_tools._pool to avoid
requiring real Redis or PostgreSQL connections.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from models.schemas import ToolResult, VelocityCheckInput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis_mock(window_txn_ids=None):
    """
    Return a MagicMock redis client whose pipeline returns configurable
    per-window txn_id lists.

    window_txn_ids: list of 3 lists — [1h_ids, 6h_ids, 24h_ids]
    """
    if window_txn_ids is None:
        window_txn_ids = [["TXN_A", "TXN_B"], ["TXN_A", "TXN_B", "TXN_C"], ["TXN_A", "TXN_B", "TXN_C", "TXN_D"]]

    pipe_mock = MagicMock()
    pipe_mock.execute.return_value = window_txn_ids
    # zrangebyscore returns the pipe itself (fluent API)
    pipe_mock.zrangebyscore.return_value = pipe_mock

    client_mock = MagicMock()
    client_mock.pipeline.return_value = pipe_mock

    return client_mock, pipe_mock


def _make_pool_mock_no_rows():
    """Return a pool mock whose cursor returns no rows (all volumes = 0)."""
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = []
    cursor_mock.__enter__ = lambda s: cursor_mock
    cursor_mock.__exit__ = MagicMock(return_value=False)

    conn_mock = MagicMock()
    conn_mock.cursor.return_value = cursor_mock

    pool_mock = MagicMock()
    pool_mock.getconn.return_value = conn_mock
    return pool_mock


# ---------------------------------------------------------------------------
# velocity_check — output shape
# ---------------------------------------------------------------------------

class TestVelocityCheck:
    def test_output_has_correct_window_keys(self):
        """velocity_check data.windows must contain keys '1h', '6h', '24h'."""
        redis_mock, _ = _make_redis_mock()
        pool_mock = _make_pool_mock_no_rows()

        with patch("tools.redis_tools._redis_client", redis_mock), \
             patch("tools.postgres_tools._pool", pool_mock):
            from tools.redis_tools import velocity_check
            result = velocity_check(VelocityCheckInput(account_id="ACC_S1_001"))

        assert result.success is True
        assert result.data is not None
        assert "windows" in result.data
        assert set(result.data["windows"].keys()) == {"1h", "6h", "24h"}

    def test_each_window_has_count_and_volume_inr(self):
        """Each window dict must have 'count' (int) and 'volume_inr' (float)."""
        redis_mock, _ = _make_redis_mock()
        pool_mock = _make_pool_mock_no_rows()

        with patch("tools.redis_tools._redis_client", redis_mock), \
             patch("tools.postgres_tools._pool", pool_mock):
            from tools.redis_tools import velocity_check
            result = velocity_check(VelocityCheckInput(account_id="ACC_S1_001"))

        for label in ("1h", "6h", "24h"):
            window = result.data["windows"][label]
            assert "count" in window
            assert "volume_inr" in window
            assert isinstance(window["count"], int)
            assert isinstance(window["volume_inr"], float)

    def test_counts_match_mocked_zset_results(self):
        """count values must match the number of txn_ids returned per window."""
        # 1h=2, 6h=3, 24h=4
        redis_mock, _ = _make_redis_mock()
        pool_mock = _make_pool_mock_no_rows()

        with patch("tools.redis_tools._redis_client", redis_mock), \
             patch("tools.postgres_tools._pool", pool_mock):
            from tools.redis_tools import velocity_check
            result = velocity_check(VelocityCheckInput(account_id="ACC_S1_001"))

        assert result.data["windows"]["1h"]["count"] == 2
        assert result.data["windows"]["6h"]["count"] == 3
        assert result.data["windows"]["24h"]["count"] == 4

    def test_account_id_present_in_data(self):
        """data must include account_id matching the input."""
        redis_mock, _ = _make_redis_mock()
        pool_mock = _make_pool_mock_no_rows()

        with patch("tools.redis_tools._redis_client", redis_mock), \
             patch("tools.postgres_tools._pool", pool_mock):
            from tools.redis_tools import velocity_check
            result = velocity_check(VelocityCheckInput(account_id="ACC_TEST"))

        assert result.data["account_id"] == "ACC_TEST"

    def test_empty_account_returns_zero_counts(self):
        """velocity_check for an account with no ZSET entries returns 0 counts."""
        redis_mock, _ = _make_redis_mock(window_txn_ids=[[], [], []])
        pool_mock = _make_pool_mock_no_rows()

        with patch("tools.redis_tools._redis_client", redis_mock), \
             patch("tools.postgres_tools._pool", pool_mock):
            from tools.redis_tools import velocity_check
            result = velocity_check(VelocityCheckInput(account_id="EMPTY_ACCOUNT"))

        assert result.success is True
        for label in ("1h", "6h", "24h"):
            assert result.data["windows"][label]["count"] == 0
            assert result.data["windows"][label]["volume_inr"] == 0.0

    def test_returns_failure_when_redis_client_is_none(self):
        """velocity_check returns ToolResult(success=False) when _redis_client is None."""
        with patch("tools.redis_tools._redis_client", None):
            from tools.redis_tools import velocity_check
            result = velocity_check(VelocityCheckInput(account_id="ANY"))

        assert result.success is False
        assert result.error is not None
        assert "redis" in result.error.lower() or "client" in result.error.lower()

    def test_pipeline_called_three_times(self):
        """velocity_check must issue exactly 3 zrangebyscore calls via pipeline."""
        redis_mock, pipe_mock = _make_redis_mock()
        pool_mock = _make_pool_mock_no_rows()

        with patch("tools.redis_tools._redis_client", redis_mock), \
             patch("tools.postgres_tools._pool", pool_mock):
            from tools.redis_tools import velocity_check
            velocity_check(VelocityCheckInput(account_id="ACC_S1_001"))

        assert pipe_mock.zrangebyscore.call_count == 3
