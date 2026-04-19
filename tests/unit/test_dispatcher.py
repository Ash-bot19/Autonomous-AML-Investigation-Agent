"""
tests/unit/test_dispatcher.py — Unit tests for tools/dispatcher.py dispatch_tool.

Strategy:
- Patch tools.dispatcher._log_pool to a MagicMock so DB writes don't fail.
- Patch individual tool functions at their source modules so lazy imports inside
  _route_tool pick up the mocks correctly.
- All tests require no running infrastructure.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch, call


class TestDispatchTool(unittest.TestCase):
    """Tests for tools.dispatcher.dispatch_tool."""

    def setUp(self):
        import tools.dispatcher as disp
        from models.schemas import ToolResult
        self.disp = disp
        self.dispatch_tool = disp.dispatch_tool
        self.ToolResult = ToolResult

        # Patch the log pool so DB writes don't attempt a real connection.
        self._pool_patcher = patch.object(disp, "_log_pool", MagicMock())
        self._pool_patcher.start()

    def tearDown(self):
        self._pool_patcher.stop()

    # ── Routing tests ─────────────────────────────────────────────────────────

    def test_routes_watchlist_lookup_and_returns_tool_result(self):
        """dispatch_tool("watchlist_lookup", ...) routes to watchlist_lookup and returns ToolResult."""
        fake_result = self.ToolResult(
            success=True,
            tool_name="watchlist_lookup",
            data={"queried_name": "Test", "match": False, "matched_entity": None},
        )
        with patch("tools.static_tools.watchlist_lookup", return_value=fake_result):
            result = self.dispatch_tool(
                "watchlist_lookup",
                {"entity_name": "Test"},
                "inv-001",
                1,
            )
        self.assertIsInstance(result, self.ToolResult)
        self.assertTrue(result.success)

    def test_routes_velocity_check_and_returns_tool_result(self):
        """dispatch_tool("velocity_check", ...) routes to velocity_check and returns ToolResult."""
        fake_result = self.ToolResult(
            success=True,
            tool_name="velocity_check",
            data={"account_id": "ACC_001", "windows": {"1h": {"count": 3, "volume_inr": 0.0},
                                                         "6h": {"count": 5, "volume_inr": 0.0},
                                                         "24h": {"count": 7, "volume_inr": 0.0}}},
        )
        with patch("tools.redis_tools.velocity_check", return_value=fake_result):
            result = self.dispatch_tool(
                "velocity_check",
                {"account_id": "ACC_001"},
                "inv-002",
                2,
            )
        self.assertIsInstance(result, self.ToolResult)
        self.assertTrue(result.success)
        self.assertEqual(result.data["account_id"], "ACC_001")

    def test_unknown_tool_returns_failure(self):
        """dispatch_tool("unknown_tool", ...) returns ToolResult(success=False) with 'Unknown tool' error."""
        result = self.dispatch_tool("unknown_tool", {}, "inv-001", 1)
        self.assertFalse(result.success)
        self.assertIn("Unknown tool", result.error)

    def test_all_six_tool_names_are_routed(self):
        """Each of the 6 valid tool names returns a ToolResult (not an unknown-tool error)."""
        tool_configs = {
            "txn_history_query": (
                "tools.postgres_tools.txn_history_query",
                {"account_id": "ACC_001"},
                {"account_id": "ACC_001", "transactions": [], "total_90d_count": 0, "total_90d_volume_inr": 0.0},
            ),
            "counterparty_risk_lookup": (
                "tools.postgres_tools.counterparty_risk_lookup",
                {"account_id": "ACC_001"},
                {"account_id": "ACC_001", "risk_tier": "low", "flag_reason": None},
            ),
            "round_trip_detector": (
                "tools.postgres_tools.round_trip_detector",
                {"account_id": "ACC_001"},
                {"cycle_detected": False, "cycle_path": None, "window_hours": 24},
            ),
            "velocity_check": (
                "tools.redis_tools.velocity_check",
                {"account_id": "ACC_001"},
                {"account_id": "ACC_001", "windows": {"1h": {"count": 0, "volume_inr": 0.0},
                                                        "6h": {"count": 0, "volume_inr": 0.0},
                                                        "24h": {"count": 0, "volume_inr": 0.0}}},
            ),
            "watchlist_lookup": (
                "tools.static_tools.watchlist_lookup",
                {"entity_name": "Test"},
                {"queried_name": "Test", "match": False, "matched_entity": None},
            ),
            "kafka_lag_check": (
                "tools.kafka_tools.kafka_lag_check",
                {},
                {"consumer_group": "aml-investigation-agent", "lag": 0, "is_pipeline_delay": False},
            ),
        }

        for tool_name, (patch_target, tool_input, data) in tool_configs.items():
            with self.subTest(tool_name=tool_name):
                fake_result = self.ToolResult(
                    success=True, tool_name=tool_name, data=data
                )
                with patch(patch_target, return_value=fake_result):
                    result = self.dispatch_tool(tool_name, tool_input, "inv-all", 1)
                self.assertIsInstance(result, self.ToolResult)
                # Must not be an "Unknown tool" error
                if not result.success:
                    self.assertNotIn("Unknown tool", result.error or "")

    # ── Logging tests ─────────────────────────────────────────────────────────

    def test_log_pool_getconn_called_after_dispatch(self):
        """After dispatch_tool completes, _log_pool.getconn() was called (log write attempted)."""
        import tools.dispatcher as disp
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.getconn.return_value = mock_conn

        fake_result = self.ToolResult(
            success=True,
            tool_name="watchlist_lookup",
            data={"queried_name": "T", "match": False, "matched_entity": None},
        )
        with patch.object(disp, "_log_pool", mock_pool), \
             patch("tools.static_tools.watchlist_lookup", return_value=fake_result):
            self.dispatch_tool("watchlist_lookup", {"entity_name": "T"}, "inv-log", 3)

        mock_pool.getconn.assert_called_once()

    def test_log_write_failure_does_not_affect_tool_result(self):
        """If _log_pool.getconn() raises, dispatch_tool still returns the correct ToolResult."""
        import tools.dispatcher as disp
        mock_pool = MagicMock()
        mock_pool.getconn.side_effect = Exception("DB down")

        fake_result = self.ToolResult(
            success=True,
            tool_name="watchlist_lookup",
            data={"queried_name": "T", "match": False, "matched_entity": None},
        )
        with patch.object(disp, "_log_pool", mock_pool), \
             patch("tools.static_tools.watchlist_lookup", return_value=fake_result):
            result = self.dispatch_tool("watchlist_lookup", {"entity_name": "T"}, "inv-nolog", 1)

        # Tool result is still returned despite log failure
        self.assertIsInstance(result, self.ToolResult)
        self.assertTrue(result.success)

    # ── Signature tests ───────────────────────────────────────────────────────

    def test_dispatch_tool_signature_has_four_params(self):
        """dispatch_tool must accept exactly (tool_name, tool_input, investigation_id, hop_number)."""
        import inspect
        sig = inspect.signature(self.dispatch_tool)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["tool_name", "tool_input", "investigation_id", "hop_number"])

    def test_never_raises_on_any_input(self):
        """dispatch_tool must not raise even for pathological input."""
        # None tool_input dict — _route_tool wraps in try/except
        try:
            result = self.dispatch_tool("txn_history_query", None, "inv-x", 0)
            self.assertIsInstance(result, self.ToolResult)
        except Exception as exc:
            self.fail(f"dispatch_tool raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main()
