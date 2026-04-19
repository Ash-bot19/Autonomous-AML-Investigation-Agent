"""Queries: cost and operational metrics."""
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


def get_cost_metrics() -> dict:
    """Return cost per investigation list, running total, and average from compliance_reports.

    Returns:
        {
            "costs": [float, ...],        # one entry per resolved investigation
            "total_cost_usd": float,
            "avg_cost_usd": float,
            "investigation_count": int,
        }
    """
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT total_cost_usd
            FROM compliance_reports
            ORDER BY resolved_at DESC
            """
        )
        rows = cur.fetchall()
        costs = [float(r[0]) for r in rows if r[0] is not None]
        total = sum(costs)
        avg = total / len(costs) if costs else 0.0
        return {
            "costs": costs,
            "total_cost_usd": round(total, 6),
            "avg_cost_usd": round(avg, 6),
            "investigation_count": len(costs),
        }
    except Exception as exc:
        log.error("ui.query.get_cost_metrics.error", error=str(exc))
        return {
            "costs": [],
            "total_cost_usd": 0.0,
            "avg_cost_usd": 0.0,
            "investigation_count": 0,
        }
    finally:
        if conn is not None:
            conn.close()


def get_operational_metrics() -> dict:
    """Return investigation count, escalation rate, avg hops, avg latency.

    Returns:
        {
            "total_investigations": int,
            "total_escalations": int,
            "escalation_rate_pct": float,   # 0-100
            "avg_hops": float,
            "avg_latency_ms": float,        # avg tool latency from tool_execution_log
        }
    """
    conn = None
    try:
        conn = _get_conn()
        cur = conn.cursor()

        # investigation count: distinct investigation_ids with a 'triggered' event
        cur.execute(
            "SELECT COUNT(DISTINCT investigation_id) FROM investigation_audit_log WHERE event_type = 'triggered'"
        )
        total_inv = cur.fetchone()[0] or 0

        # escalation count: distinct investigation_ids with an 'escalated' event
        cur.execute(
            "SELECT COUNT(DISTINCT investigation_id) FROM investigation_audit_log WHERE event_type = 'escalated'"
        )
        total_esc = cur.fetchone()[0] or 0

        # avg hops from compliance_reports
        cur.execute("SELECT AVG(total_hops) FROM compliance_reports")
        avg_hops_row = cur.fetchone()
        avg_hops = (
            float(avg_hops_row[0])
            if avg_hops_row and avg_hops_row[0] is not None
            else 0.0
        )

        # avg tool latency from tool_execution_log
        cur.execute("SELECT AVG(latency_ms) FROM tool_execution_log")
        avg_lat_row = cur.fetchone()
        avg_latency = (
            float(avg_lat_row[0])
            if avg_lat_row and avg_lat_row[0] is not None
            else 0.0
        )

        esc_rate = (total_esc / total_inv * 100.0) if total_inv > 0 else 0.0

        return {
            "total_investigations": int(total_inv),
            "total_escalations": int(total_esc),
            "escalation_rate_pct": round(esc_rate, 1),
            "avg_hops": round(avg_hops, 2),
            "avg_latency_ms": round(avg_latency, 1),
        }
    except Exception as exc:
        log.error("ui.query.get_operational_metrics.error", error=str(exc))
        return {
            "total_investigations": 0,
            "total_escalations": 0,
            "escalation_rate_pct": 0.0,
            "avg_hops": 0.0,
            "avg_latency_ms": 0.0,
        }
    finally:
        if conn is not None:
            conn.close()
