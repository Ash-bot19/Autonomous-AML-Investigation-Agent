"""
tools/redis_tools.py — Redis-backed AML investigation tools.

Tool:
  - velocity_check: counts and INR volume for 1h, 6h, 24h transaction windows.

ZSET key format: velocity:{account_id}
  - Each member is a txn_id (str)
  - Score is UNIX timestamp (float)

Counts come from Redis ZRANGEBYSCORE (pipeline, 3 windows in one round-trip).
Volumes come from PostgreSQL transactions table for the matched txn_ids in the
24h window (largest superset). The 1h/6h volumes are derived from the subset of
those txn_ids that appear in their respective windows.

This cross-store join is intentional: seed.py writes ZSET members as bare
txn_ids, so the amount must be resolved via PostgreSQL. If the postgres pool is
unavailable, volume_inr falls back to 0 (counts are still accurate).

Never raises — all exceptions return ToolResult(success=False, error=...).
"""
from __future__ import annotations

import os
import time
import structlog
from typing import Any

import redis as redis_lib

from models.schemas import ToolResult, VelocityCheckInput

log = structlog.get_logger()

# ── Redis client ─────────────────────────────────────────────────────────────
# Initialised once at module import. Returns None silently on failure.

_redis_client: redis_lib.Redis | None = None


def _init_redis() -> redis_lib.Redis | None:
    try:
        return redis_lib.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True,
        )
    except Exception as exc:
        log.warning("redis_client.init_failed", error=str(exc))
        return None


_redis_client = _init_redis()


# ── velocity_check ───────────────────────────────────────────────────────────


def velocity_check(inp: VelocityCheckInput) -> ToolResult:
    """
    Returns transaction counts and INR volume for 1h, 6h, 24h windows.

    Counts: Redis ZRANGEBYSCORE on velocity:{account_id} ZSET (3-call pipeline).
    Volumes: PostgreSQL transactions table for txn_ids found in the 24h window.

    Data shape:
      {
        "account_id": str,
        "windows": {
          "1h":  {"count": int, "volume_inr": float},
          "6h":  {"count": int, "volume_inr": float},
          "24h": {"count": int, "volume_inr": float},
        }
      }
    """
    try:
        if _redis_client is None:
            return ToolResult(
                success=False,
                tool_name="velocity_check",
                error="Redis connection error: client not initialised",
            )

        now = time.time()
        window_specs = {
            "1h":  now - 3_600,
            "6h":  now - 21_600,
            "24h": now - 86_400,
        }

        key = f"velocity:{inp.account_id}"
        pipe = _redis_client.pipeline()
        for min_score in window_specs.values():
            pipe.zrangebyscore(key, min_score, now)
        results = pipe.execute()  # list of 3 lists of txn_ids

        txn_ids_by_window: dict[str, set[str]] = {
            label: set(ids)
            for label, ids in zip(window_specs.keys(), results)
        }

        # Resolve volumes from PostgreSQL for txn_ids in the 24h window
        all_txn_ids = txn_ids_by_window["24h"]
        volumes: dict[str, float] = {}

        if all_txn_ids:
            try:
                from tools.postgres_tools import _pool
                if _pool is not None:
                    conn = None
                    try:
                        conn = _pool.getconn()
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT txn_id, amount_inr FROM transactions WHERE txn_id = ANY(%s)",
                                (list(all_txn_ids),),
                            )
                            for row in cur.fetchall():
                                volumes[row[0]] = float(row[1])
                    except Exception as exc:
                        log.warning(
                            "velocity_check.volume_query_failed",
                            account_id=inp.account_id,
                            error=str(exc),
                        )
                    finally:
                        if conn is not None:
                            _pool.putconn(conn)
                else:
                    log.warning(
                        "velocity_check.volume_unavailable",
                        account_id=inp.account_id,
                        reason="postgres pool not initialised",
                    )
            except ImportError:
                log.warning("velocity_check.postgres_tools_unavailable")

        def window_stats(txn_ids: set[str]) -> dict[str, Any]:
            count = len(txn_ids)
            volume = sum(volumes.get(tid, 0.0) for tid in txn_ids)
            return {"count": count, "volume_inr": float(volume)}

        data: dict[str, Any] = {
            "account_id": inp.account_id,
            "windows": {
                label: window_stats(txn_ids_by_window[label])
                for label in window_specs
            },
        }

        log.info(
            "velocity_check.ok",
            account_id=inp.account_id,
            count_1h=data["windows"]["1h"]["count"],
            count_6h=data["windows"]["6h"]["count"],
            count_24h=data["windows"]["24h"]["count"],
        )
        return ToolResult(success=True, tool_name="velocity_check", data=data)

    except Exception as exc:
        log.error("velocity_check.error", account_id=inp.account_id, error=str(exc))
        return ToolResult(
            success=False,
            tool_name="velocity_check",
            error=str(exc),
        )
