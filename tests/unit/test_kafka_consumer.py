"""Unit tests for kafka/consumer.py — deserialization, routing, error handling.

Tests call _process_message directly to avoid needing a live Kafka broker.
KafkaConsumer is mocked; run_investigation is patched to prevent graph invocation.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


class TestProcessMessage:
    """Tests 1-4: _process_message deserialization, validation, and dispatch."""

    def test_valid_message_dispatches_to_run_investigation(self):
        """Test 1: valid JSON — InvestigationPayload deserialized and run_investigation called."""
        raw = json.dumps({
            "txn_id": "T1",
            "trigger_type": "rule_engine",
            "trigger_detail": "amount",
            "risk_score": 0.82,
        }).encode("utf-8")

        mock_run = MagicMock(return_value={"status": "RESOLVED"})
        from kafka.consumer import _process_message
        _process_message(raw, dispatch_fn=mock_run)

        mock_run.assert_called_once()
        called_payload = mock_run.call_args[0][0]
        assert called_payload.txn_id == "T1"

    def test_malformed_json_skips_run_investigation(self):
        """Test 2: malformed JSON — run_investigation NOT called; error logged."""
        raw = b"not-json"

        with patch("kafka.consumer.run_investigation") as mock_run, \
             patch("kafka.consumer.log") as mock_log:
            from kafka.consumer import _process_message
            _process_message(raw)

        mock_run.assert_not_called()
        # Verify the deserialize error was logged
        mock_log.error.assert_called_once()
        call_args = mock_log.error.call_args[0]
        assert call_args[0] == "kafka.consumer.deserialize_error"

    def test_invalid_payload_schema_skips_run_investigation(self):
        """Test 3: valid JSON but invalid InvestigationPayload — run_investigation NOT called."""
        raw = json.dumps({
            "txn_id": "T2",
            "trigger_type": "unknown",  # not in Literal["rule_engine", "ml_score", "both"]
            "trigger_detail": "test",
        }).encode("utf-8")

        with patch("kafka.consumer.run_investigation") as mock_run, \
             patch("kafka.consumer.log") as mock_log:
            from kafka.consumer import _process_message
            _process_message(raw)

        mock_run.assert_not_called()
        mock_log.error.assert_called_once()
        call_args = mock_log.error.call_args[0]
        assert call_args[0] == "kafka.consumer.payload_validation_error"

    def test_run_investigation_exception_does_not_crash_consumer(self):
        """Test 4: run_investigation raises — consumer does NOT crash; error logged."""
        raw = json.dumps({
            "txn_id": "T3",
            "trigger_type": "rule_engine",
            "trigger_detail": "amount",
        }).encode("utf-8")

        mock_run = MagicMock(side_effect=RuntimeError("graph exploded"))
        with patch("kafka.consumer.log") as mock_log:
            from kafka.consumer import _process_message
            # Should NOT raise
            _process_message(raw, dispatch_fn=mock_run)

        mock_log.error.assert_called_once()
        call_args = mock_log.error.call_args[0]
        assert call_args[0] == "kafka.consumer.run_investigation_error"
