"""
agent/evidence.py — Deterministic evidence-chain builder.

REPT-04 / AUDT-04 contract: the citation chain in compliance_reports MUST be
derived from tool_execution_log rows, never from LLM output. The LLM may
control verdict/recommendation/narrative (via EvaluationOutput) but the
evidence chain itself is locked to what the dispatcher actually executed.

Significance assignment is currently deterministic (success → medium, failure → low).
A future plan may wire LLM-assigned significance per-entry, but the source of
the entries themselves stays this module.
"""
from __future__ import annotations

import os
import threading
from typing import Any
from urllib.parse import quote_plus

import psycopg2
import psycopg2.extras
import psycopg2.pool
import structlog

from models.schemas import EvidenceEntry

log = structlog.get_logger()

# ── Connection pool (independent of dispatcher's pool to avoid import cycles) ──

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
        log.warning("evidence.pool.init_failed", error=str(exc))
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


# ── Tool-specific finding summarisers ─────────────────────────────────────────


def _summarise_txn_history(data: dict[str, Any]) -> str:
    return (
        f"{data.get('total_90d_count', 0)} txns in last 90d, "
        f"volume INR {data.get('total_90d_volume_inr', 0):,}"
    )


def _summarise_counterparty_risk(data: dict[str, Any]) -> str:
    return f"counterparty risk_tier={data.get('risk_tier')} ({data.get('flag_reason', 'no reason')})"


def _summarise_velocity(data: dict[str, Any]) -> str:
    windows = data.get("windows", {})
    parts = []
    for w in ("1h", "6h", "24h"):
        wd = windows.get(w, {}) or {}
        parts.append(f"{w}: count={wd.get('count', 0)}, volume_inr={wd.get('volume_inr', 0):,}")
    return "velocity " + "; ".join(parts)


def _summarise_watchlist(data: dict[str, Any]) -> str:
    if data.get("match"):
        return f"watchlist MATCH on {data.get('matched_entity')}"
    return f"watchlist no match for {data.get('queried_name')}"


def _summarise_round_trip(data: dict[str, Any]) -> str:
    if data.get("cycle_detected"):
        return f"round-trip detected: {data.get('cycle_path')} (window {data.get('window_hours')}h)"
    return f"no round-trip in {data.get('window_hours', 24)}h window"


def _summarise_kafka_lag(data: dict[str, Any]) -> str:
    lag = data.get("lag", 0)
    if data.get("is_pipeline_delay"):
        return f"kafka lag={lag} — pipeline delay suspected"
    return f"kafka lag={lag} — no pipeline delay"


_SUMMARISERS: dict[str, Any] = {
    "txn_history_query": _summarise_txn_history,
    "counterparty_risk_lookup": _summarise_counterparty_risk,
    "velocity_check": _summarise_velocity,
    "watchlist_lookup": _summarise_watchlist,
    "round_trip_detector": _summarise_round_trip,
    "kafka_lag_check": _summarise_kafka_lag,
}


def _summarise_tool_output(
    tool_name: str, tool_output: dict[str, Any] | None
) -> tuple[str, str]:
    """
    Returns (finding_text, significance) for a tool_execution_log row.

    significance is deterministic for now:
      - tool_output.success=True  → "medium"
      - tool_output.success=False → "low"
    """
    if not tool_output:
        return "no tool output recorded", "low"

    success = tool_output.get("success", False)
    significance = "medium" if success else "low"

    if not success:
        err = tool_output.get("error", "unknown error")
        return f"tool error: {err}", "low"

    data = tool_output.get("data") or {}
    summariser = _SUMMARISERS.get(tool_name)
    if summariser is None:
        return f"raw: {str(data)[:200]}", significance
    try:
        return summariser(data), significance
    except Exception as exc:
        log.warning("evidence.summariser_error", tool_name=tool_name, error=str(exc))
        return f"raw: {str(data)[:200]}", significance


# ── Public API ────────────────────────────────────────────────────────────────


def build_evidence_chain(investigation_id: str) -> list[EvidenceEntry]:
    """
    Read tool_execution_log for the given investigation_id and return an ordered
    list of EvidenceEntry. Never raises — returns [] on any failure.

    The evidence chain is sourced from logged tool calls ONLY (REPT-04/AUDT-04).
    LLM output is never consulted here.
    """
    pool = _get_pool()
    if pool is None:
        log.warning("evidence.pool_unavailable", investigation_id=investigation_id)
        return []

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT hop_number, tool_name, tool_output
                FROM tool_execution_log
                WHERE investigation_id = %s
                ORDER BY hop_number ASC
                """,
                (investigation_id,),
            )
            rows = cur.fetchall()

        entries: list[EvidenceEntry] = []
        for row in rows:
            finding, significance = _summarise_tool_output(row["tool_name"], row["tool_output"])
            entries.append(
                EvidenceEntry(
                    hop=row["hop_number"],
                    tool=row["tool_name"],
                    finding=finding,
                    significance=significance,
                )
            )
        log.info(
            "evidence.built",
            investigation_id=investigation_id,
            entry_count=len(entries),
        )
        return entries

    except Exception as exc:
        log.error(
            "evidence.query_failed",
            investigation_id=investigation_id,
            error=str(exc),
        )
        return []
    finally:
        if conn:
            try:
                pool.putconn(conn)
            except Exception:
                pass
