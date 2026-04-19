"""GET /status/{investigation_id} — investigation status lookup."""
from __future__ import annotations

import os
from urllib.parse import quote_plus

import psycopg2
import structlog
from fastapi import APIRouter, HTTPException

from models.schemas import StatusResponse

log = structlog.get_logger()
router = APIRouter()


def _get_conn():
    user = quote_plus(os.environ["POSTGRES_USER"])
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return psycopg2.connect(f"postgresql://{user}:{password}@{host}:{port}/{db}")


@router.get("/status/{investigation_id}", response_model=StatusResponse)
async def get_status(investigation_id: str) -> StatusResponse:
    """Return current status, hop count, and total cost for an investigation."""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        # Existence check first — avoids false 404 for in-progress investigations
        # that have only a 'triggered' event and no state_change row yet.
        cur.execute(
            "SELECT 1 FROM investigation_audit_log WHERE investigation_id = %s LIMIT 1",
            (investigation_id,),
        )
        if cur.fetchone() is None:
            raise HTTPException(
                status_code=404,
                detail=f"investigation {investigation_id!r} not found",
            )
        cur.execute(
            """
            SELECT
                COALESCE(
                    (SELECT state_to FROM investigation_audit_log
                     WHERE investigation_id = %s
                     ORDER BY created_at DESC LIMIT 1),
                    'unknown'
                ) AS latest_status,
                COUNT(*) FILTER (WHERE event_type = 'tool_call') AS hops,
                COALESCE(SUM(cost_usd_delta), 0.0) AS total_cost
            FROM investigation_audit_log
            WHERE investigation_id = %s
            """,
            (investigation_id, investigation_id),
        )
        row = cur.fetchone()
        latest_status, hops, total_cost = row
        return StatusResponse(
            investigation_id=investigation_id,
            status=latest_status or "unknown",
            hops=int(hops or 0),
            cost_usd=float(total_cost or 0.0),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.error("api.status.error", investigation_id=investigation_id, error=str(exc))
        raise HTTPException(status_code=500, detail="internal error") from exc
    finally:
        if conn is not None:
            conn.close()
