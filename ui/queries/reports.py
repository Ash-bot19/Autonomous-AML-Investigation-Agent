"""Query: resolved compliance reports."""
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


def get_compliance_reports(limit: int = 50) -> list[dict]:
    """Return resolved compliance reports ordered by most recent."""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                investigation_id::text,
                txn_id,
                verdict,
                confidence,
                finding,
                evidence_chain,
                recommendation,
                narrative,
                total_hops,
                total_cost_usd,
                resolved_at
            FROM compliance_reports
            ORDER BY resolved_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        cols = [
            "investigation_id", "txn_id", "verdict", "confidence",
            "finding", "evidence_chain", "recommendation", "narrative",
            "total_hops", "total_cost_usd", "resolved_at",
        ]
        rows = cur.fetchall()
        results = []
        for row in rows:
            d = dict(zip(cols, row))
            ec = d.get("evidence_chain")
            if isinstance(ec, str):
                ec = json.loads(ec)
            d["evidence_chain"] = ec or []
            if d.get("resolved_at"):
                d["resolved_at"] = str(d["resolved_at"])
            results.append(d)
        return results
    except Exception as exc:
        log.error("ui.query.get_compliance_reports.error", error=str(exc))
        return []
    finally:
        if conn is not None:
            conn.close()
