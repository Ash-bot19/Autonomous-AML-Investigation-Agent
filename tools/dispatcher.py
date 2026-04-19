"""
tools/dispatcher.py — Unified tool dispatcher with execution logging.

Single entry point used by agent/graph.py. Routes tool_name to the correct
real tool implementation, logs every call to tool_execution_log, and never raises.

Routing table uses explicit if/elif — no dynamic dispatch via globals() or
getattr. Unknown tool names return ToolResult(success=False) without executing
any function (T-03-03-01 mitigation).

tool_execution_log INSERT uses parameterised psycopg2 with json.dumps
serialisation — no string concatenation in SQL (T-03-03-02 mitigation).

Log write failure is non-fatal: the tool result is returned regardless.
"""
from __future__ import annotations

import json
import os
import time
import structlog
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

import psycopg2
import psycopg2.pool

from models.schemas import (
    ToolResult,
    TxnHistoryInput,
    CounterpartyRiskInput,
    VelocityCheckInput,
    WatchlistInput,
    RoundTripInput,
    KafkaLagInput,
)

log = structlog.get_logger()

# ── Logging pool ─────────────────────────────────────────────────────────────
# Separate pool from postgres_tools._pool to avoid cross-import cycles.
# Smaller (max 3 conns) — only used for INSERT into tool_execution_log.

_log_pool: psycopg2.pool.SimpleConnectionPool | None = None
_log_pool_init_attempted: bool = False


def _init_log_pool() -> psycopg2.pool.SimpleConnectionPool | None:
    try:
        user = quote_plus(os.environ["POSTGRES_USER"])
        password = quote_plus(os.environ["POSTGRES_PASSWORD"])
        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = os.environ.get("POSTGRES_PORT", "5432")
        db = os.environ["POSTGRES_DB"]
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"
        return psycopg2.pool.SimpleConnectionPool(1, 3, dsn)
    except Exception as exc:
        log.warning("dispatcher.log_pool.init_failed", error=str(exc))
        return None


def _get_log_pool() -> psycopg2.pool.SimpleConnectionPool | None:
    """Lazy-init the log pool on first use. Safe to call repeatedly.

    Tests may patch _log_pool directly — this function checks the global
    so patched values are picked up without needing to patch _get_log_pool.
    """
    global _log_pool, _log_pool_init_attempted
    if _log_pool is not None:
        return _log_pool
    if not _log_pool_init_attempted:
        _log_pool = _init_log_pool()
        _log_pool_init_attempted = True
    return _log_pool


# ── Tool execution logging ────────────────────────────────────────────────────


def _write_tool_log(
    investigation_id: str,
    hop_number: int,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any],
    latency_ms: int,
) -> None:
    """
    Insert one row into tool_execution_log. Non-fatal — logs warning on failure.
    Uses parameterised psycopg2 INSERT; JSON serialised via json.dumps.
    """
    pool = _get_log_pool()
    if pool is None:
        log.warning("dispatcher.log_pool.unavailable", tool_name=tool_name)
        return
    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tool_execution_log
                    (investigation_id, hop_number, tool_name, tool_input,
                     tool_output, latency_ms, called_at)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                """,
                (
                    investigation_id,
                    hop_number,
                    tool_name,
                    json.dumps(tool_input),
                    json.dumps(tool_output),
                    latency_ms,
                    datetime.now(timezone.utc),
                ),
            )
        conn.commit()
        log.debug(
            "dispatcher.log_written",
            tool_name=tool_name,
            investigation_id=investigation_id,
            hop=hop_number,
        )
    except Exception as exc:
        log.warning("dispatcher.log_write_failed", tool_name=tool_name, error=str(exc))
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            pool.putconn(conn)


# ── Tool routing table ────────────────────────────────────────────────────────


def _route_tool(tool_name: str, tool_input: dict[str, Any]) -> ToolResult:
    """
    Instantiate the correct Pydantic input model and call the correct tool.

    Explicit if/elif routing — no dynamic globals()/getattr dispatch.
    Unknown tool_name returns ToolResult(success=False) without executing any
    arbitrary function (T-03-03-01).

    Tool modules are imported lazily inside each branch to avoid circular import
    issues at module load time and to allow test mocking at the source module.
    """
    try:
        if tool_name == "txn_history_query":
            from tools.postgres_tools import txn_history_query
            return txn_history_query(TxnHistoryInput(**tool_input))

        elif tool_name == "counterparty_risk_lookup":
            from tools.postgres_tools import counterparty_risk_lookup
            return counterparty_risk_lookup(CounterpartyRiskInput(**tool_input))

        elif tool_name == "round_trip_detector":
            from tools.postgres_tools import round_trip_detector
            return round_trip_detector(RoundTripInput(**tool_input))

        elif tool_name == "velocity_check":
            from tools.redis_tools import velocity_check
            return velocity_check(VelocityCheckInput(**tool_input))

        elif tool_name == "watchlist_lookup":
            from tools.static_tools import watchlist_lookup
            return watchlist_lookup(WatchlistInput(**tool_input))

        elif tool_name == "kafka_lag_check":
            from tools.kafka_tools import kafka_lag_check
            return kafka_lag_check(KafkaLagInput(**(tool_input or {})))

        else:
            log.warning("dispatcher.unknown_tool", tool_name=tool_name)
            return ToolResult(
                success=False,
                tool_name=tool_name,
                error=f"Unknown tool: {tool_name}",
            )

    except Exception as exc:
        log.error("dispatcher.route_error", tool_name=tool_name, error=str(exc))
        return ToolResult(
            success=False,
            tool_name=tool_name,
            error=f"Routing error: {exc}",
        )


# ── dispatch_tool — public API ────────────────────────────────────────────────


def dispatch_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    investigation_id: str,
    hop_number: int,
) -> ToolResult:
    """
    Execute a tool by name and return a structured ToolResult.

    Logs every call to tool_execution_log (investigation_id, hop_number,
    tool_name, tool_input, tool_output, latency_ms, called_at).
    Log write failure is non-fatal — tool result is returned regardless.
    Never raises.

    Args:
        tool_name:        One of the 6 valid tool names.
        tool_input:       Dict of tool-specific parameters.
        investigation_id: UUID string of the current investigation.
        hop_number:       Current hop count from state.
    """
    start_ms = time.monotonic()
    result: ToolResult | None = None

    try:
        result = _route_tool(tool_name, tool_input or {})
        return result

    except Exception as exc:
        # Outer safety net — _route_tool should never raise but belt-and-suspenders.
        log.error("dispatch_tool.outer_error", tool_name=tool_name, error=str(exc))
        result = ToolResult(success=False, tool_name=tool_name, error=str(exc))
        return result

    finally:
        latency_ms = int((time.monotonic() - start_ms) * 1000)
        output_dict: dict[str, Any] = (
            result.model_dump() if result is not None else {"error": "no result"}
        )
        _write_tool_log(
            investigation_id=investigation_id,
            hop_number=hop_number,
            tool_name=tool_name,
            tool_input=tool_input or {},
            tool_output=output_dict,
            latency_ms=latency_ms,
        )
        log.info(
            "dispatch_tool.complete",
            tool_name=tool_name,
            investigation_id=investigation_id,
            hop=hop_number,
            latency_ms=latency_ms,
            success=result.success if result is not None else False,
        )
