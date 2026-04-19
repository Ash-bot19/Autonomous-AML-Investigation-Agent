"""
tests/unit/test_static_kafka_tools.py — Unit tests for watchlist_lookup and kafka_lag_check.

watchlist tests: run without mocking (CSV at data/watchlist.csv is present).
kafka tests: inject mocks via tools.kafka_tools module attributes — no real broker required.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestWatchlistLookup(unittest.TestCase):
    """Tests for tools.static_tools.watchlist_lookup."""

    def setUp(self):
        from tools.static_tools import watchlist_lookup
        from models.schemas import WatchlistInput
        self.watchlist_lookup = watchlist_lookup
        self.WatchlistInput = WatchlistInput

    def test_exact_match_returns_true(self):
        """watchlist_lookup("Viktor Kovalenko") -> match=True, matched_entity set."""
        result = self.watchlist_lookup(self.WatchlistInput(entity_name="Viktor Kovalenko"))
        self.assertTrue(result.success)
        self.assertTrue(result.data["match"])
        self.assertEqual(result.data["matched_entity"], "Viktor Kovalenko")
        self.assertEqual(result.data["queried_name"], "Viktor Kovalenko")

    def test_normalised_match_returns_true(self):
        """watchlist_lookup("  VIKTOR KOVALENKO  ") normalises to same key -> match=True."""
        result = self.watchlist_lookup(self.WatchlistInput(entity_name="  VIKTOR KOVALENKO  "))
        self.assertTrue(result.success)
        self.assertTrue(result.data["match"])

    def test_unknown_name_returns_false(self):
        """watchlist_lookup("John Smith") -> match=False, matched_entity=None."""
        result = self.watchlist_lookup(self.WatchlistInput(entity_name="John Smith"))
        self.assertTrue(result.success)
        self.assertFalse(result.data["match"])
        self.assertIsNone(result.data["matched_entity"])
        self.assertEqual(result.data["queried_name"], "John Smith")

    def test_empty_string_does_not_raise(self):
        """Empty string input should not raise — returns ToolResult(success=True, match=False)."""
        result = self.watchlist_lookup(self.WatchlistInput(entity_name=""))
        self.assertTrue(result.success)
        self.assertFalse(result.data["match"])

    def test_other_watchlist_entity_matches(self):
        """Meridian Trade Holdings is also in the CSV — verify a second entity works."""
        result = self.watchlist_lookup(self.WatchlistInput(entity_name="Meridian Trade Holdings"))
        self.assertTrue(result.success)
        self.assertTrue(result.data["match"])
        self.assertEqual(result.data["matched_entity"], "Meridian Trade Holdings")


class TestKafkaLagCheck(unittest.TestCase):
    """
    Tests for tools.kafka_tools.kafka_lag_check.

    Injection pattern: set tools.kafka_tools.KafkaAdminClient / KafkaConsumer
    to mocks before each test, reset in tearDown. This bypasses the project's
    kafka/ directory shadowing the installed library.
    """

    def setUp(self):
        import tools.kafka_tools as kt
        from models.schemas import KafkaLagInput
        self.kt = kt
        self.kafka_lag_check = kt.kafka_lag_check
        self.KafkaLagInput = KafkaLagInput
        # Save originals
        self._orig_admin = kt.KafkaAdminClient
        self._orig_consumer = kt.KafkaConsumer

    def tearDown(self):
        # Restore originals so tests don't bleed into each other
        self.kt.KafkaAdminClient = self._orig_admin
        self.kt.KafkaConsumer = self._orig_consumer

    def _make_admin(self, committed_offsets=None):
        """Build a mock KafkaAdminClient that returns committed_offsets."""
        mock_admin_instance = MagicMock()
        mock_admin_instance.list_consumer_group_offsets.return_value = committed_offsets or {}
        mock_admin_cls = MagicMock(return_value=mock_admin_instance)
        return mock_admin_cls

    def test_returns_correct_shape_zero_lag(self):
        """
        With empty committed_offsets: lag=0, is_pipeline_delay=False.
        """
        self.kt.KafkaAdminClient = self._make_admin({})
        self.kt.KafkaConsumer = MagicMock()

        result = self.kafka_lag_check(self.KafkaLagInput())

        self.assertTrue(result.success)
        self.assertEqual(result.data["lag"], 0)
        self.assertFalse(result.data["is_pipeline_delay"])
        self.assertIn("consumer_group", result.data)

    def test_returns_pipeline_delay_true_when_lag_positive(self):
        """
        Committed offset=10, end offset=15 -> lag=5, is_pipeline_delay=True.
        """
        tp_key = "partition-0"  # use a plain string as stand-in for TopicPartition

        mock_offset_meta = MagicMock()
        mock_offset_meta.offset = 10

        self.kt.KafkaAdminClient = self._make_admin({tp_key: mock_offset_meta})

        mock_consumer_instance = MagicMock()
        mock_consumer_instance.end_offsets.return_value = {tp_key: 15}
        self.kt.KafkaConsumer = MagicMock(return_value=mock_consumer_instance)

        result = self.kafka_lag_check(self.KafkaLagInput())

        self.assertTrue(result.success)
        self.assertGreater(result.data["lag"], 0)
        self.assertTrue(result.data["is_pipeline_delay"])

    def test_returns_failure_when_admin_raises(self):
        """KafkaAdminClient constructor raises -> ToolResult(success=False)."""
        self.kt.KafkaAdminClient = MagicMock(side_effect=Exception("NoBrokersAvailable"))
        self.kt.KafkaConsumer = MagicMock()

        result = self.kafka_lag_check(self.KafkaLagInput())

        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)

    def test_result_has_required_keys_on_success(self):
        """Result data must have consumer_group, lag, is_pipeline_delay keys."""
        self.kt.KafkaAdminClient = self._make_admin({})
        self.kt.KafkaConsumer = MagicMock()

        result = self.kafka_lag_check(self.KafkaLagInput())

        self.assertTrue(result.success)
        self.assertIn("consumer_group", result.data)
        self.assertIn("lag", result.data)
        self.assertIn("is_pipeline_delay", result.data)

    def test_lag_is_int(self):
        """lag field must be an int (not float or str)."""
        self.kt.KafkaAdminClient = self._make_admin({})
        self.kt.KafkaConsumer = MagicMock()

        result = self.kafka_lag_check(self.KafkaLagInput())

        self.assertIsInstance(result.data["lag"], int)


if __name__ == "__main__":
    unittest.main()
