"""
tools/postgres_tools.py — PostgreSQL-backed AML investigation tools.

Three deterministic tools backed by psycopg2:
  - txn_history_query       : last 90 days of transactions for an account
  - counterparty_risk_lookup: risk tier + flag reason from counterparty_risk table
  - round_trip_detector     : A→B→C→A cycle detection via WITH RECURSIVE CTE

All tools:
  - Accept a typed Pydantic input model.
  - Return ToolResult — never raise unhandled exceptions.
  - Use parameterised queries (%s) exclusively — no f-string SQL concatenation.
  - Check out / return connections to the module-level pool.
"""
from __future__ import annotations

import os
import structlog
from typing import Any
from urllib.parse import quote_plus

import psycopg2
import psycopg2.pool

from models.schemas import (
    ToolResult,
    TxnHistoryInput,
    CounterpartyRiskInput,
    RoundTripInput,
)

log = structlog.get_logger()

# ── Connection pool ──────────────────────────────────────────────────────────
# Initialised once at module import. Fails silently if DB unreachable;
# tools detect pool=None on entry and return an error ToolResult.

_pool: psycopg2.pool.SimpleConnectionPool | None = None


def _init_pool() -> psycopg2.pool.SimpleConnectionPool | None:
    try:
        user = quote_plus(os.environ["POSTGRES_USER"])
        password = quote_plus(os.environ["POSTGRES_PASSWORD"])
        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = os.environ.get("POSTGRES_PORT", "5432")
        db = os.environ["POSTGRES_DB"]
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"
        return psycopg2.pool.SimpleConnectionPool(1, 5, dsn)
    except Exception as exc:
        log.warning("postgres_pool.init_failed", error=str(exc))
        return None


_pool = _init_pool()


# ── txn_history_query ────────────────────────────────────────────────────────


def txn_history_query(inp: TxnHistoryInput) -> ToolResult:
    """
    Returns the last 90 days of transactions for inp.account_id.

    Data shape:
      {
        "account_id": str,
        "transactions": [{"txn_id", "amount", "counterparty", "timestamp"}, ...],
        "total_90d_count": int,
        "total_90d_volume_inr": float,
      }
    """
    if _pool is None:
        return ToolResult(
            success=False,
            tool_name="txn_history_query",
            error="DB connection error: pool unavailable",
        )

    conn = None
    try:
        conn = _pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT txn_id, counterparty_id, amount_inr, timestamp
                FROM transactions
                WHERE account_id = %s
                  AND timestamp >= NOW() - INTERVAL '90 days'
                ORDER BY timestamp DESC
                """,
                (inp.account_id,),
            )
            rows = cur.fetchall()

        transactions = [
            {
                "txn_id": row[0],
                "amount": float(row[2]),
                "counterparty": row[1],
                "timestamp": row[3].isoformat(),
            }
            for row in rows
        ]
        total_volume = sum(float(row[2]) for row in rows)

        log.info(
            "txn_history_query.ok",
            account_id=inp.account_id,
            row_count=len(rows),
        )
        return ToolResult(
            success=True,
            tool_name="txn_history_query",
            data={
                "account_id": inp.account_id,
                "transactions": transactions,
                "total_90d_count": len(rows),
                "total_90d_volume_inr": total_volume,
            },
        )

    except Exception as exc:
        log.error("txn_history_query.error", account_id=inp.account_id, error=str(exc))
        return ToolResult(
            success=False,
            tool_name="txn_history_query",
            error=f"DB connection error: {exc}",
        )
    finally:
        if conn is not None:
            _pool.putconn(conn)


# ── counterparty_risk_lookup ─────────────────────────────────────────────────


def counterparty_risk_lookup(inp: CounterpartyRiskInput) -> ToolResult:
    """
    Returns risk tier and flag reason for inp.account_id from counterparty_risk table.

    Returns risk_tier="unknown" / flag_reason="No record found" when account absent.

    Data shape:
      {"account_id": str, "risk_tier": str, "flag_reason": str | None}
    """
    if _pool is None:
        return ToolResult(
            success=False,
            tool_name="counterparty_risk_lookup",
            error="DB connection error: pool unavailable",
        )

    conn = None
    try:
        conn = _pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_id, risk_tier, flag_reason
                FROM counterparty_risk
                WHERE account_id = %s
                """,
                (inp.account_id,),
            )
            row = cur.fetchone()

        if row is None:
            log.info("counterparty_risk_lookup.not_found", account_id=inp.account_id)
            return ToolResult(
                success=True,
                tool_name="counterparty_risk_lookup",
                data={
                    "account_id": inp.account_id,
                    "risk_tier": "unknown",
                    "flag_reason": "No record found",
                },
            )

        log.info(
            "counterparty_risk_lookup.ok",
            account_id=inp.account_id,
            risk_tier=row[1],
        )
        return ToolResult(
            success=True,
            tool_name="counterparty_risk_lookup",
            data={
                "account_id": row[0],
                "risk_tier": row[1],
                "flag_reason": row[2],
            },
        )

    except Exception as exc:
        log.error(
            "counterparty_risk_lookup.error",
            account_id=inp.account_id,
            error=str(exc),
        )
        return ToolResult(
            success=False,
            tool_name="counterparty_risk_lookup",
            error=f"DB connection error: {exc}",
        )
    finally:
        if conn is not None:
            _pool.putconn(conn)


# ── round_trip_detector ──────────────────────────────────────────────────────


def round_trip_detector(inp: RoundTripInput) -> ToolResult:
    """
    Detects A→B→C→A round-trip cycles for inp.account_id within inp.window_hours.

    Uses a WITH RECURSIVE PostgreSQL CTE. Depth capped at 5 to prevent runaway
    recursion (T-03-02-03, T-03-02-04). Returns first cycle found, or
    cycle_detected=False if none.

    Data shape:
      {"cycle_detected": bool, "cycle_path": list[str] | None, "window_hours": int}
    """
    if _pool is None:
        return ToolResult(
            success=False,
            tool_name="round_trip_detector",
            error="DB connection error: pool unavailable",
        )

    conn = None
    try:
        conn = _pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE cycle_search AS (
                    -- Base: all transactions from the target account in the window
                    SELECT
                        t.account_id AS origin,
                        t.counterparty_id AS current_node,
                        ARRAY[t.account_id, t.counterparty_id] AS path,
                        1 AS depth
                    FROM transactions t
                    WHERE t.account_id = %s
                      AND t.timestamp >= NOW() - (%s * INTERVAL '1 hour')

                    UNION ALL

                    -- Recursive: follow counterparty's outgoing transactions
                    SELECT
                        cs.origin,
                        t.counterparty_id AS current_node,
                        cs.path || t.counterparty_id,
                        cs.depth + 1
                    FROM cycle_search cs
                    JOIN transactions t ON t.account_id = cs.current_node
                      AND t.timestamp >= NOW() - (%s * INTERVAL '1 hour')
                    WHERE
                        t.counterparty_id <> ALL(cs.path[2:])
                        AND cs.depth < 5
                )
                SELECT path
                FROM cycle_search
                WHERE current_node = origin
                  AND depth >= 2
                LIMIT 1
                """,
                (inp.account_id, inp.window_hours, inp.window_hours),
            )
            rows = cur.fetchall()

        if rows:
            cycle_path = list(rows[0][0])
            log.info(
                "round_trip_detector.cycle_found",
                account_id=inp.account_id,
                path=cycle_path,
            )
            return ToolResult(
                success=True,
                tool_name="round_trip_detector",
                data={
                    "cycle_detected": True,
                    "cycle_path": cycle_path,
                    "window_hours": inp.window_hours,
                },
            )

        log.info("round_trip_detector.no_cycle", account_id=inp.account_id)
        return ToolResult(
            success=True,
            tool_name="round_trip_detector",
            data={
                "cycle_detected": False,
                "cycle_path": None,
                "window_hours": inp.window_hours,
            },
        )

    except Exception as exc:
        log.error(
            "round_trip_detector.error",
            account_id=inp.account_id,
            error=str(exc),
        )
        return ToolResult(
            success=False,
            tool_name="round_trip_detector",
            error=f"DB connection error: {exc}",
        )
    finally:
        if conn is not None:
            _pool.putconn(conn)
