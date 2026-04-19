"""
agent/escalation_writer.py — Escalation queue writer.

Persists an escalation_queue row on every non-RESOLVED exit. Covers all 5 reason
codes (low_confidence, max_hops, timeout, cost_cap, empty_evidence) per
ESC-01..ESC-05. Every row captures a partial_report JSONB snapshot of the
AgentState at the moment of escalation (ESC-06). Never raises; returns False on
any failure.

The status column is always written as 'open' (ESC-07 — lifecycle transitions
are managed externally via the analyst queue UI, not the agent).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional
from urllib.parse import quote_plus

import psycopg2
import psycopg2.pool
import structlog

from agent.state import AgentState

log = structlog.get_logger()

_pool: psycopg2.pool.SimpleConnectionPool | None = None
_pool_init_attempted: bool = False
_pool_lock = threading.Lock()

VALID_REASONS = frozenset({"low_confidence", "max_hops", "timeout", "cost_cap", "empty_evidence"})


def _init_pool() -> psycopg2.pool.SimpleConnectionPool | None:
    try:
        user = quote_plus(os.environ["POSTGRES_USER"])
        password = quote_plus(os.environ["POSTGRES_PASSWORD"])
        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = os.environ.get("POSTGRES_PORT", "5432")
        db = os.environ["POSTGRES_DB"]
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"
        return psycopg2.pool.SimpleConnectionPool(1, 3, dsn)
    except Exception as exc:
        log.warning("escalation_writer.pool.init_failed", error=str(exc))
        return None


def _get_pool() -> psycopg2.pool.SimpleConnectionPool | None:
    global _pool, _pool_init_attempted
    if _pool is not None:          # fast path, no lock
        return _pool
    with _pool_lock:
        if _pool is not None:      # re-check inside lock
            return _pool
        if not _pool_init_attempted:
            _pool = _init_pool()
            _pool_init_attempted = True
    return _pool


def build_partial_report(state: AgentState) -> dict[str, Any]:
    """Snapshot AgentState into a JSONB-serialisable partial_report dict.

    Tolerant of missing fields — never raises. Captures:
      investigation_id, txn_id, hop_count, accumulated_cost_usd,
      evidence_chain (list), verdict, confidence, finding,
      recommendation, narrative, escalation_reason.

    Called immediately before writing the escalation row so the partial_report
    reflects the exact state at the moment of escalation exit.
    """
    try:
        payload = state.get("payload")
        investigation_id = payload.investigation_id if payload else None
        txn_id = payload.txn_id if payload else None
    except Exception:
        investigation_id = None
        txn_id = None

    try:
        evaluation = state.get("evaluation") or {}
        verdict = evaluation.get("verdict") if evaluation else None
        confidence = evaluation.get("confidence") if evaluation else None
        finding = evaluation.get("finding") if evaluation else None
        recommendation = evaluation.get("recommendation") if evaluation else None
        narrative = evaluation.get("narrative") if evaluation else None
    except Exception:
        verdict = None
        confidence = None
        finding = None
        recommendation = None
        narrative = None

    try:
        evidence_chain = state.get("evidence_chain") or []
    except Exception:
        evidence_chain = []

    try:
        hop_count = state.get("hop_count") or 0
        accumulated_cost_usd = state.get("accumulated_cost_usd") or 0.0
        escalation_reason = state.get("escalation_reason")
    except Exception:
        hop_count = 0
        accumulated_cost_usd = 0.0
        escalation_reason = None

    return {
        "investigation_id": investigation_id,
        "txn_id": txn_id,
        "hop_count": hop_count,
        "accumulated_cost_usd": accumulated_cost_usd,
        "evidence_chain": evidence_chain,
        "verdict": verdict,
        "confidence": confidence,
        "finding": finding,
        "recommendation": recommendation,
        "narrative": narrative,
        "escalation_reason": escalation_reason,
    }


def write_escalation(
    investigation_id: str,
    txn_id: str,
    reason: str,
    confidence: Optional[float],
    partial_report: dict[str, Any],
) -> bool:
    """Insert one row into escalation_queue.

    Returns True on success, False on any failure. Never raises.

    reason must be one of the 5 valid escalation reasons — application-layer
    defence in depth on top of the DB CHECK constraint (migration 0004).

    partial_report is serialised via json.dumps and cast to ::jsonb — never
    string-concatenated into SQL (T-05-01-03 SQL injection mitigation).

    status is always written as 'open' — lifecycle transitions happen externally.
    """
    if reason not in VALID_REASONS:
        log.error(
            "escalation_writer.invalid_reason",
            reason=reason,
            investigation_id=investigation_id,
        )
        return False

    pool = _get_pool()
    if pool is None:
        log.warning(
            "escalation_writer.pool_unavailable",
            investigation_id=investigation_id,
            reason=reason,
        )
        return False

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO escalation_queue
                    (investigation_id, txn_id, escalation_reason, confidence, partial_report, status, created_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, 'open', NOW())
                """,
                (
                    investigation_id,
                    txn_id,
                    reason,
                    confidence,
                    json.dumps(partial_report or {}),
                ),
            )
        conn.commit()
        log.info(
            "escalation_writer.written",
            investigation_id=investigation_id,
            reason=reason,
            confidence=confidence,
        )
        return True
    except Exception as exc:
        log.error(
            "escalation_writer.write_failed",
            investigation_id=investigation_id,
            reason=reason,
            error=str(exc),
        )
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            try:
                pool.putconn(conn)
            except Exception:
                pass
