"""
agent/audit_trail.py — Append-only audit-trail writer.

Every investigation state transition, tool call, trigger and resolution writes
exactly one row to investigation_audit_log. All writes are INSERTs — UPDATE and
DELETE are blocked by a DB trigger (migration 0007). Never raises; returns False
on any failure.

Event types (AUDT-01..AUDT-05):
  triggered    — investigation payload received (IDLE → INVESTIGATING)
  state_change — any state machine transition (INVESTIGATING → TOOL_CALLING, etc.)
  tool_call    — each deterministic tool call (TOOL_CALLING node)
  resolved     — investigation resolved with verdict (EVALUATING → RESOLVED)
  escalated    — investigation escalated to analyst queue (EVALUATING → ESCALATED)
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

log = structlog.get_logger()

_pool: psycopg2.pool.SimpleConnectionPool | None = None
_pool_init_attempted: bool = False
_pool_lock = threading.Lock()

VALID_EVENT_TYPES = frozenset({"triggered", "state_change", "tool_call", "resolved", "escalated"})


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
        log.warning("audit_trail.pool.init_failed", error=str(exc))
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


def write_audit_event(
    investigation_id: str,
    event_type: str,
    event_detail: Optional[dict[str, Any]] = None,
    state_from: Optional[str] = None,
    state_to: Optional[str] = None,
    cost_usd_delta: float = 0.0,
) -> bool:
    """Insert one row into investigation_audit_log.

    Returns True on success, False on any failure. Never raises.

    Parameters are always passed via psycopg2 %s placeholders — never via
    string concatenation (T-05-01-03 SQL injection mitigation).

    event_detail is serialised via json.dumps and cast to ::jsonb. Passing None
    produces an empty JSON object {}.
    """
    if event_type not in VALID_EVENT_TYPES:
        log.error(
            "audit_trail.invalid_event_type",
            investigation_id=investigation_id,
            event_type=event_type,
        )
        return False

    pool = _get_pool()
    if pool is None:
        log.warning(
            "audit_trail.pool_unavailable",
            investigation_id=investigation_id,
            event_type=event_type,
        )
        return False

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO investigation_audit_log
                    (investigation_id, event_type, event_detail, state_from, state_to, cost_usd_delta, created_at)
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, NOW())
                """,
                (
                    investigation_id,
                    event_type,
                    json.dumps(event_detail or {}),
                    state_from,
                    state_to,
                    cost_usd_delta,
                ),
            )
        conn.commit()
        log.info(
            "audit_trail.written",
            investigation_id=investigation_id,
            event_type=event_type,
            state_from=state_from,
            state_to=state_to,
        )
        return True
    except Exception as exc:
        log.error(
            "audit_trail.write_failed",
            investigation_id=investigation_id,
            event_type=event_type,
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
