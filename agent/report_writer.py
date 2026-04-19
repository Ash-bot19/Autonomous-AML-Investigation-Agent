"""
agent/report_writer.py — Persists ComplianceReport to the compliance_reports table.

REPT-01 contract: every RESOLVED investigation writes one row containing all
report fields. Idempotent via ON CONFLICT (investigation_id) DO NOTHING — the
state machine should call this once per investigation, but retries from upstream
infrastructure must not produce duplicates.

DB write failure is non-fatal: the function returns False and logs the error;
the agent's in-memory final_report stays the source of truth for the current run.
Phase 5 adds a retry/dead-letter path.

REPT-04 invariant: this module never imports agent.llm_client. The evidence_chain
in the report is always sourced from build_evidence_chain() (tool_execution_log),
not derived from any LLM output.
"""
from __future__ import annotations

import json
import os
import threading
from urllib.parse import quote_plus

import psycopg2
import psycopg2.pool
import structlog

from models.schemas import ComplianceReport

log = structlog.get_logger()

_pool: psycopg2.pool.SimpleConnectionPool | None = None
_pool_init_attempted: bool = False
_pool_lock = threading.Lock()


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
        log.warning("report_writer.pool.init_failed", error=str(exc))
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


def write_compliance_report(report: ComplianceReport) -> bool:
    """
    INSERT one row into compliance_reports. Returns True on success, False on
    any failure. Never raises.

    Idempotent via ON CONFLICT (investigation_id) DO NOTHING — a duplicate
    investigation_id write is a silent no-op (returns True; no second row created).

    Parameters are always passed via psycopg2 %s placeholders — never via string
    concatenation (T-04-05-01 SQL injection mitigation).
    """
    pool = _get_pool()
    if pool is None:
        log.warning(
            "report_writer.pool_unavailable",
            investigation_id=report.investigation_id,
        )
        return False

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO compliance_reports (
                    investigation_id, txn_id, verdict, confidence, finding,
                    evidence_chain, recommendation, narrative, total_hops,
                    total_cost_usd, resolved_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (investigation_id) DO NOTHING
                """,
                (
                    report.investigation_id,
                    report.txn_id,
                    report.verdict,
                    report.confidence,
                    report.finding,
                    json.dumps([e.model_dump() for e in report.evidence_chain]),
                    report.recommendation,
                    report.narrative,
                    report.total_hops,
                    report.total_cost_usd,
                    report.resolved_at,
                ),
            )
            inserted = cur.rowcount == 1
        conn.commit()
        if inserted:
            log.info(
                "report_writer.written",
                investigation_id=report.investigation_id,
                verdict=report.verdict,
                total_hops=report.total_hops,
                total_cost_usd=report.total_cost_usd,
            )
        else:
            log.info(
                "report_writer.duplicate_skipped",
                investigation_id=report.investigation_id,
            )
        return True
    except Exception as exc:
        log.error(
            "report_writer.write_failed",
            investigation_id=report.investigation_id,
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
