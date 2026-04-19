"""Query: escalation queue."""
from __future__ import annotations

import json
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


def get_escalation_queue(status_filter: str = "open", limit: int = 100) -> list[dict]:
    """Return escalated investigations with reason, confidence, and partial report evidence.

    partial_report is a JSONB dict with keys: evidence_chain (list), verdict, etc.
    Returns the evidence_chain list as a formatted string for display.
    """
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                investigation_id::text,
                txn_id,
                escalation_reason,
                confidence,
                partial_report,
                status,
                created_at
            FROM escalation_queue
            WHERE status = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (status_filter, limit),
        )
        cols = [
            "investigation_id",
            "txn_id",
            "escalation_reason",
            "confidence",
            "partial_report",
            "status",
            "created_at",
        ]
        rows = cur.fetchall()
        results = []
        for row in rows:
            d = dict(zip(cols, row))
            # partial_report: psycopg2 returns JSONB as dict already; normalise
            pr = d.get("partial_report")
            if isinstance(pr, str):
                pr = json.loads(pr)
            # Extract evidence chain for display — guard per-entry so one bad row
            # doesn't abort the whole result set.
            try:
                evidence = pr.get("evidence_chain", []) if pr and isinstance(pr, dict) else []
                d["evidence_summary"] = "; ".join(
                    f"hop {e.get('hop', '?')}: {e.get('finding', '')}"
                    for e in (evidence or [])
                    if isinstance(e, dict)
                ) or "no evidence gathered"
            except Exception:
                d["evidence_summary"] = "no evidence gathered"
            results.append(d)
        return results
    except Exception as exc:
        log.error("ui.query.get_escalation_queue.error", error=str(exc))
        return []
    finally:
        if conn is not None:
            conn.close()
