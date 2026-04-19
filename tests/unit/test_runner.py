"""Unit tests for agent/runner.py — ML routing, Redis mutex, graph invocation.

Tests must not require live Redis or live Postgres.
All external dependencies (AMLGraph, Redis) are mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from models.schemas import InvestigationPayload


def _make_payload(**kwargs) -> InvestigationPayload:
    """Helper to build a minimal InvestigationPayload."""
    defaults = {
        "txn_id": "TXN-001",
        "trigger_type": "rule_engine",
        "trigger_detail": "amount_threshold",
        "risk_score": None,
    }
    defaults.update(kwargs)
    return InvestigationPayload(**defaults)


# ── ML routing and Redis mutex tests ─────────────────────────────────────────


class TestMLRouting:
    """Tests 1-7: ML score routing logic (_should_investigate / _apply_priority_flag)."""

    def test_ml_score_below_threshold_logs_and_skips(self):
        """Test 1: risk_score=0.60, trigger_type="ml_score" — no investigation, log_only returned."""
        payload = _make_payload(trigger_type="ml_score", risk_score=0.60)
        mock_graph = MagicMock()
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        mock_graph.invoke.assert_not_called()
        assert result["status"] == "log_only"
        assert result["txn_id"] == payload.txn_id

    def test_ml_score_exactly_0_75_skips(self):
        """Test 2: risk_score=0.75 — NOT > 0.75, so no investigation."""
        payload = _make_payload(trigger_type="ml_score", risk_score=0.75)
        mock_graph = MagicMock()
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        mock_graph.invoke.assert_not_called()
        assert result["status"] == "log_only"

    def test_ml_score_above_0_75_triggers(self):
        """Test 3: risk_score=0.76 — triggers investigation, no high_priority flag."""
        payload = _make_payload(trigger_type="ml_score", risk_score=0.76, trigger_detail="ml_trigger")
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"status": "RESOLVED"}
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        mock_graph.invoke.assert_called_once()
        # Check the payload passed — no high_priority in trigger_detail
        invoked_state = mock_graph.invoke.call_args[0][0]
        assert "high_priority" not in invoked_state["payload"].trigger_detail
        assert result["status"] == "RESOLVED"

    def test_ml_score_above_0_90_sets_high_priority(self):
        """Test 4: risk_score=0.91 — investigation triggered and 'high_priority' in trigger_detail."""
        payload = _make_payload(trigger_type="ml_score", risk_score=0.91, trigger_detail="ml_trigger")
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"status": "RESOLVED"}
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        mock_graph.invoke.assert_called_once()
        invoked_state = mock_graph.invoke.call_args[0][0]
        assert "high_priority" in invoked_state["payload"].trigger_detail

    def test_rule_engine_trigger_no_score_investigates(self):
        """Test 5: trigger_type="rule_engine", risk_score=None — always investigates."""
        payload = _make_payload(trigger_type="rule_engine", risk_score=None)
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"status": "RESOLVED"}
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        mock_graph.invoke.assert_called_once()

    def test_rule_engine_with_low_score_still_investigates(self):
        """Test 6: trigger_type="rule_engine", risk_score=0.60 — ML routing bypassed, investigates."""
        payload = _make_payload(trigger_type="rule_engine", risk_score=0.60)
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"status": "RESOLVED"}
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        mock_graph.invoke.assert_called_once()

    def test_both_trigger_type_with_low_score_investigates(self):
        """Test 7: trigger_type="both", risk_score=0.60 — 'both' bypasses ML score check."""
        payload = _make_payload(trigger_type="both", risk_score=0.60)
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"status": "ESCALATED"}
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        mock_graph.invoke.assert_called_once()


class TestRedisMutex:
    """Tests 8-11: Redis mutex acquisition, contention, and Redis unavailability."""

    def test_mutex_contention_blocks_investigation(self):
        """Test 8: Redis SET NX returns None (key exists) — returns already_investigating."""
        payload = _make_payload(trigger_type="rule_engine")
        mock_graph = MagicMock()
        mock_redis = MagicMock()
        mock_redis.set.return_value = None  # NX failed — key already exists

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        mock_graph.invoke.assert_not_called()
        assert result["status"] == "already_investigating"
        assert result["txn_id"] == payload.txn_id

    def test_mutex_acquired_and_released_on_success(self):
        """Test 9: mutex acquired, AMLGraph.invoke succeeds — DEL called after."""
        payload = _make_payload(trigger_type="rule_engine")
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"status": "RESOLVED"}
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            run_investigation(payload)

        mock_graph.invoke.assert_called_once()
        expected_key = f"mutex:investigation:{payload.txn_id}"
        mock_redis.delete.assert_called_once_with(expected_key)

    def test_mutex_released_on_graph_exception(self):
        """Test 10: mutex acquired, AMLGraph.invoke raises — DEL still called (finally block)."""
        payload = _make_payload(trigger_type="rule_engine")
        mock_graph = MagicMock()
        mock_graph.invoke.side_effect = RuntimeError("graph boom")
        mock_redis = MagicMock()
        mock_redis.set.return_value = True

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=mock_redis):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        expected_key = f"mutex:investigation:{payload.txn_id}"
        mock_redis.delete.assert_called_once_with(expected_key)
        assert result["status"] == "error"
        assert "graph boom" in result["error"]

    def test_redis_unavailable_runs_without_mutex(self):
        """Test 11: Redis unavailable — investigation proceeds without mutex (logs warning)."""
        payload = _make_payload(trigger_type="rule_engine")
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"status": "RESOLVED"}

        with patch("agent.runner.AMLGraph", mock_graph), \
             patch("agent.runner._get_redis", return_value=None):
            from agent.runner import run_investigation
            result = run_investigation(payload)

        mock_graph.invoke.assert_called_once()
        assert result["status"] == "RESOLVED"


# ── FastAPI endpoint tests ─────────────────────────────────────────────────────


class TestAPIEndpoints:
    """Tests 12-13: POST /investigate endpoint."""

    def test_post_investigate_returns_started(self):
        """Test 12: valid payload — returns 200 with investigation_id and status='started'."""
        from api.main import app
        client = TestClient(app)

        payload_data = {
            "txn_id": "TXN-API-001",
            "trigger_type": "rule_engine",
            "trigger_detail": "amount_threshold",
        }

        with patch("api.routes.investigate.run_investigation") as mock_run:
            mock_run.return_value = {"status": "RESOLVED"}
            response = client.post("/investigate", json=payload_data)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "started"
        assert "investigation_id" in body

    def test_post_investigate_missing_txn_id_returns_422(self):
        """Test 13: missing required field txn_id — returns 422 Unprocessable Entity."""
        from api.main import app
        client = TestClient(app)

        payload_data = {
            "trigger_type": "rule_engine",
            "trigger_detail": "amount_threshold",
            # txn_id is missing
        }

        response = client.post("/investigate", json=payload_data)
        assert response.status_code == 422
