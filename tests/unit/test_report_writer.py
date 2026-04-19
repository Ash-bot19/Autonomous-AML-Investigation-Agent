"""
tests/unit/test_report_writer.py

Unit tests for agent/report_writer.py (Task 1) and node_resolved wiring (Task 2).
All tests run in-memory — no PostgreSQL required. psycopg2 is fully mocked.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch, call

from models.schemas import ComplianceReport, EvidenceEntry, InvestigationPayload
from agent.state import AgentState, InvestigationStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_evidence_entry(hop: int = 1, tool: str = "velocity_check") -> EvidenceEntry:
    return EvidenceEntry(
        hop=hop,
        tool=tool,
        finding="18 txns in 6h",
        significance="high",
    )


def make_compliance_report(
    investigation_id: str = "test-uuid-001",
    verdict: str = "suspicious",
    evidence_chain: list[EvidenceEntry] | None = None,
) -> ComplianceReport:
    if evidence_chain is None:
        evidence_chain = [make_evidence_entry()]
    return ComplianceReport(
        investigation_id=investigation_id,
        txn_id="TXN_001",
        verdict=verdict,
        confidence=0.85,
        finding="Unusual velocity pattern detected",
        evidence_chain=evidence_chain,
        recommendation="escalate_to_compliance",
        narrative="The transaction shows elevated velocity with 18 transfers in 6 hours.",
        total_hops=1,
        total_cost_usd=0.003,
    )


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


# ── Task 1: write_compliance_report ──────────────────────────────────────────

class TestWriteComplianceReport:

    @patch("agent.report_writer._get_pool")
    def test_write_success_returns_true(self, mock_get_pool):
        """Test 1: write_compliance_report returns True on successful INSERT."""
        from agent.report_writer import write_compliance_report

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        report = make_compliance_report()
        result = write_compliance_report(report)

        assert result is True
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    @patch("agent.report_writer._get_pool")
    def test_all_eleven_fields_in_params(self, mock_get_pool):
        """Test 2: All 11 fields are passed in the parameter tuple to cursor.execute."""
        from agent.report_writer import write_compliance_report

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        report = make_compliance_report(investigation_id="uuid-verify-fields")
        write_compliance_report(report)

        sql, params = mock_cursor.execute.call_args[0]
        assert "INSERT INTO compliance_reports" in sql
        assert len(params) == 11, f"Expected 11 params, got {len(params)}"
        # Verify key field positions
        assert params[0] == "uuid-verify-fields"   # investigation_id
        assert params[1] == "TXN_001"               # txn_id
        assert params[2] == "suspicious"             # verdict
        assert params[3] == pytest.approx(0.85)     # confidence

    @patch("agent.report_writer._get_pool")
    def test_evidence_chain_serialised_as_json(self, mock_get_pool):
        """Test 3: evidence_chain is serialised via json.dumps before passing to SQL."""
        from agent.report_writer import write_compliance_report

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        entry = EvidenceEntry(hop=1, tool="velocity_check", finding="high velocity", significance="high")
        report = make_compliance_report(evidence_chain=[entry])
        write_compliance_report(report)

        sql, params = mock_cursor.execute.call_args[0]
        # The 6th param (index 5) is evidence_chain — must be a JSON string
        evidence_param = params[5]
        assert isinstance(evidence_param, str), "evidence_chain must be JSON string"
        parsed = json.loads(evidence_param)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["hop"] == 1
        assert parsed[0]["tool"] == "velocity_check"

    @patch("agent.report_writer._get_pool")
    def test_pool_unavailable_returns_false_no_raise(self, mock_get_pool):
        """Test 4: When pool is None, returns False and does NOT raise."""
        from agent.report_writer import write_compliance_report

        mock_get_pool.return_value = None
        report = make_compliance_report()

        result = write_compliance_report(report)

        assert result is False

    @patch("agent.report_writer._get_pool")
    def test_db_error_returns_false_and_calls_rollback(self, mock_get_pool):
        """Test 5: When cursor.execute raises, returns False and calls conn.rollback()."""
        import psycopg2

        from agent.report_writer import write_compliance_report

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_cursor.execute.side_effect = psycopg2.IntegrityError("duplicate key violation")
        mock_get_pool.return_value = mock_pool

        report = make_compliance_report()
        result = write_compliance_report(report)

        assert result is False
        mock_conn.rollback.assert_called_once()

    @patch("agent.report_writer._get_pool")
    def test_sql_contains_on_conflict_idempotent(self, mock_get_pool):
        """Test 6: SQL contains ON CONFLICT (investigation_id) DO NOTHING — idempotent on duplicate."""
        from agent.report_writer import write_compliance_report

        mock_pool, mock_conn, mock_cursor = make_mock_pool_and_conn()
        mock_get_pool.return_value = mock_pool

        report = make_compliance_report()
        write_compliance_report(report)

        sql, _ = mock_cursor.execute.call_args[0]
        assert "ON CONFLICT" in sql
        assert "investigation_id" in sql
        assert "DO NOTHING" in sql


# ── Task 2: node_resolved wiring ─────────────────────────────────────────────

def make_resolved_state(
    investigation_id: str = "test-inv-001",
    txn_id: str = "TXN_001",
    verdict: str = "suspicious",
    confidence: float = 0.85,
    hop_count: int = 2,
    accumulated_cost: float = 0.012,
    state_evidence_chain: list | None = None,
) -> AgentState:
    """Build an AgentState that would arrive at node_resolved after evaluation."""
    if state_evidence_chain is None:
        state_evidence_chain = [
            {"hop": 1, "tool": "old_stale", "finding": "stale state entry", "significance": "low"}
        ]
    payload = InvestigationPayload(
        investigation_id=investigation_id,
        txn_id=txn_id,
        trigger_type="rule_engine",
        trigger_detail="velocity breach",
    )
    return AgentState(
        payload=payload,
        status=InvestigationStatus.EVALUATING,
        hop_count=hop_count,
        accumulated_cost_usd=accumulated_cost,
        started_at=None,
        tool_selection=None,
        last_tool_result=None,
        evidence_chain=state_evidence_chain,
        evaluation={
            "confidence": confidence,
            "verdict": verdict,
            "finding": "Unusual velocity pattern detected",
            "recommendation": "escalate_to_compliance",
            "narrative": "Evidence shows elevated velocity.",
            "should_continue": False,
        },
        escalation_reason=None,
        final_report=None,
    )


class TestNodeResolved:

    @patch("agent.graph.write_compliance_report")
    @patch("agent.graph.build_evidence_chain")
    def test_uses_db_evidence_chain_not_state(self, mock_build, mock_write):
        """Test 1: node_resolved uses build_evidence_chain output, NOT state.evidence_chain."""
        from agent.graph import node_resolved

        db_entries = [
            EvidenceEntry(hop=1, tool="velocity_check", finding="from db", significance="high"),
            EvidenceEntry(hop=2, tool="counterparty_risk_lookup", finding="risk high", significance="high"),
        ]
        mock_build.return_value = db_entries
        mock_write.return_value = True

        state = make_resolved_state(hop_count=2)
        result = node_resolved(state)

        assert result["status"] == InvestigationStatus.RESOLVED
        chain = result["final_report"]["evidence_chain"]
        assert len(chain) == 2
        # Must come from DB entries, not the stale state entry
        assert chain[0]["finding"] == "from db"
        assert chain[0]["tool"] == "velocity_check"

    @patch("agent.graph.write_compliance_report")
    @patch("agent.graph.build_evidence_chain")
    def test_write_compliance_report_called_once(self, mock_build, mock_write):
        """Test 2: node_resolved calls write_compliance_report exactly once with ComplianceReport."""
        from agent.graph import node_resolved
        from models.schemas import ComplianceReport

        mock_build.return_value = [
            EvidenceEntry(hop=1, tool="velocity_check", finding="high velocity", significance="high")
        ]
        mock_write.return_value = True

        state = make_resolved_state()
        node_resolved(state)

        mock_write.assert_called_once()
        call_arg = mock_write.call_args[0][0]
        assert isinstance(call_arg, ComplianceReport)

    @patch("agent.graph.write_compliance_report")
    @patch("agent.graph.build_evidence_chain")
    def test_db_write_failure_is_non_fatal(self, mock_build, mock_write):
        """Test 3: When write_compliance_report returns False, node_resolved still returns RESOLVED."""
        from agent.graph import node_resolved

        mock_build.return_value = [
            EvidenceEntry(hop=1, tool="velocity_check", finding="high velocity", significance="high")
        ]
        mock_write.return_value = False  # DB failure

        state = make_resolved_state()
        result = node_resolved(state)

        assert result["status"] == InvestigationStatus.RESOLVED
        assert result["final_report"] is not None

    @patch("agent.graph.write_compliance_report")
    @patch("agent.graph.build_evidence_chain")
    def test_total_cost_usd_from_state(self, mock_build, mock_write):
        """Test 4: total_cost_usd in final_report equals state accumulated_cost_usd."""
        from agent.graph import node_resolved

        mock_build.return_value = [
            EvidenceEntry(hop=1, tool="velocity_check", finding="x", significance="medium")
        ]
        mock_write.return_value = True

        state = make_resolved_state(accumulated_cost=0.031)
        result = node_resolved(state)

        assert result["final_report"]["total_cost_usd"] == pytest.approx(0.031)

    @patch("agent.graph.write_compliance_report")
    @patch("agent.graph.build_evidence_chain")
    def test_total_hops_from_state(self, mock_build, mock_write):
        """Test 5: total_hops in final_report equals state hop_count."""
        from agent.graph import node_resolved

        mock_build.return_value = [
            EvidenceEntry(hop=1, tool="velocity_check", finding="x", significance="medium"),
            EvidenceEntry(hop=2, tool="txn_history_query", finding="y", significance="medium"),
            EvidenceEntry(hop=3, tool="round_trip_detector", finding="z", significance="high"),
        ]
        mock_write.return_value = True

        state = make_resolved_state(hop_count=3, accumulated_cost=0.024)
        result = node_resolved(state)

        assert result["final_report"]["total_hops"] == 3

    @patch("agent.graph.write_compliance_report")
    @patch("agent.graph.build_evidence_chain")
    def test_empty_evidence_chain_forces_inconclusive(self, mock_build, mock_write):
        """Test 6: When build_evidence_chain returns [], verdict forced to 'inconclusive' regardless of evaluation."""
        from agent.graph import node_resolved

        mock_build.return_value = []  # pool down or no log rows
        mock_write.return_value = True

        # State says suspicious with high confidence — must be overridden
        state = make_resolved_state(verdict="suspicious", confidence=0.95)
        result = node_resolved(state)

        assert result["status"] == InvestigationStatus.RESOLVED
        assert result["final_report"]["verdict"] == "inconclusive", (
            "Empty evidence chain must force inconclusive regardless of LLM verdict"
        )
        assert result["final_report"]["recommendation"] == "escalate_to_compliance"
