"""Query: open investigations from investigation_audit_log."""
from __future__ import annotations

import os
from urllib.parse import quote_plus

import psycopg2
import structlog

log = structlog.get_logger()


def _get_conn():
    user = quote_plus(os.environ["POSTGRES_USER"])
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return psycopg2.connect(f"postgresql://{user}:{password}@{host}:{port}/{db}")


def get_open_investigations(limit: int = 100) -> list[dict]:
    """Return recent investigations with latest status, trigger_type, and start time.

    Joins investigation_audit_log to get:
    - investigation_id (UUID as str)
    - txn_id (from event_detail->>'txn_id' on triggered event)
    - status (latest state_to)
    - trigger_type (from event_detail->>'trigger_type' on triggered event)
    - started_at (created_at of the 'triggered' event)
    """
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            WITH trigger_events AS (
                SELECT
                    investigation_id,
                    event_detail->>'txn_id'       AS txn_id,
                    event_detail->>'trigger_type' AS trigger_type,
                    created_at                    AS started_at
                FROM investigation_audit_log
                WHERE event_type = 'triggered'
            ),
            latest_status AS (
                SELECT DISTINCT ON (investigation_id)
                    investigation_id,
                    state_to AS status
                FROM investigation_audit_log
                WHERE state_to IS NOT NULL
                ORDER BY investigation_id, created_at DESC
            )
            SELECT
                t.investigation_id::text,
                t.txn_id,
                COALESCE(s.status, 'unknown') AS status,
                COALESCE(t.trigger_type, 'unknown') AS trigger_type,
                t.started_at
            FROM trigger_events t
            LEFT JOIN latest_status s ON t.investigation_id = s.investigation_id
            ORDER BY t.started_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        cols = ["investigation_id", "txn_id", "status", "trigger_type", "started_at"]
        rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as exc:
        log.error("ui.query.get_open_investigations.error", error=str(exc))
        return []
    finally:
        if conn is not None:
            conn.close()
