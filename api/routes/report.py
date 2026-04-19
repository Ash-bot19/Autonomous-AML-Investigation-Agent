"""GET /report/{investigation_id} — fetch compliance report."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import quote_plus

import psycopg2
import structlog
from fastapi import APIRouter, HTTPException

from models.schemas import ComplianceReport, EvidenceEntry

log = structlog.get_logger()
router = APIRouter()


def _get_conn():
    user = quote_plus(os.environ["POSTGRES_USER"])
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return psycopg2.connect(f"postgresql://{user}:{password}@{host}:{port}/{db}")


@router.get("/report/{investigation_id}", response_model=ComplianceReport)
async def get_report(investigation_id: str) -> ComplianceReport:
    """Return the full ComplianceReport for a resolved investigation."""
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT investigation_id, txn_id, verdict, confidence, finding,
                   evidence_chain, recommendation, narrative,
                   total_hops, total_cost_usd, resolved_at
            FROM compliance_reports
            WHERE investigation_id = %s
            LIMIT 1
            """,
            (investigation_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"no compliance report found for investigation {investigation_id!r}",
            )
        (
            inv_id, txn_id, verdict, confidence, finding,
            evidence_chain_raw, recommendation, narrative,
            total_hops, total_cost_usd, resolved_at,
        ) = row

        # evidence_chain stored as JSONB — psycopg2 returns it as dict/list already
        if isinstance(evidence_chain_raw, str):
            evidence_chain_raw = json.loads(evidence_chain_raw)
        evidence_entries = [EvidenceEntry(**e) for e in (evidence_chain_raw or [])]

        return ComplianceReport(
            investigation_id=str(inv_id),
            txn_id=txn_id,
            verdict=verdict,
            confidence=confidence,
            finding=finding,
            evidence_chain=evidence_entries,
            recommendation=recommendation,
            narrative=narrative,
            total_hops=total_hops,
            total_cost_usd=total_cost_usd,
            resolved_at=(
                resolved_at if isinstance(resolved_at, datetime) and resolved_at.tzinfo is not None
                else resolved_at.replace(tzinfo=timezone.utc) if isinstance(resolved_at, datetime)
                else datetime.fromisoformat(resolved_at).replace(tzinfo=timezone.utc) if isinstance(resolved_at, str)
                else datetime.now(timezone.utc)
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.error("api.report.error", investigation_id=investigation_id, error=str(exc))
        raise HTTPException(status_code=500, detail="internal error") from exc
    finally:
        if conn is not None:
            conn.close()
