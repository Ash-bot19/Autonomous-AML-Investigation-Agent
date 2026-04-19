"""
Benchmark script for AML Investigation Agent.

Fires N investigations through the full LangGraph stack (direct invocation, not HTTP),
records wall-clock latency, cost, hops, and escalation rate, then writes results to
docs/benchmarks.md.

Usage:
    python scripts/benchmark.py [--runs N]

Prerequisites: docker compose up -d (Postgres + Redis must be running), seed.py must have
already run so ACC_S1_001 / ACC_S2_001 / ACC_S3_001 account data exists.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

import psycopg2
from dotenv import load_dotenv

load_dotenv()

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.runner import run_investigation
from agent.state import InvestigationStatus
from models.schemas import InvestigationPayload

DOCS_DIR = ROOT / "docs"


def _pg():
    user = quote_plus(os.environ["POSTGRES_USER"])
    pw = quote_plus(os.environ["POSTGRES_PASSWORD"])
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return psycopg2.connect(f"postgresql://{user}:{pw}@{host}:{port}/{db}")


def _fetch_db_metrics(conn, investigation_id: str) -> dict:
    """Pull hops + cost from compliance_reports or escalation_queue + audit_log."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT total_hops, total_cost_usd FROM compliance_reports WHERE investigation_id = %s",
            (investigation_id,),
        )
        row = cur.fetchone()
        if row:
            return {"hops": row[0] or 0, "cost_usd": float(row[1] or 0.0), "escalated": False}

        # Escalated path — get hops + cost from audit log
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE event_type = 'tool_call'),
                COALESCE(SUM(cost_usd_delta), 0.0)
            FROM investigation_audit_log
            WHERE investigation_id = %s
            """,
            (investigation_id,),
        )
        row2 = cur.fetchone()
        return {
            "hops": int(row2[0] or 0),
            "cost_usd": float(row2[1] or 0.0),
            "escalated": True,
        }


def _make_payload(scenario: int) -> InvestigationPayload:
    iid = str(uuid.uuid4())
    if scenario == 1:
        return InvestigationPayload(
            investigation_id=iid,
            txn_id="TXN_S1_001",
            trigger_type="rule_engine",
            trigger_detail=(
                "velocity: 17 transactions to ACC_S1_CP in 2 hours exceeds threshold of 5"
                " — investigate account ACC_S1_001"
            ),
        )
    elif scenario == 2:
        return InvestigationPayload(
            investigation_id=iid,
            txn_id="TXN_S2_001",
            trigger_type="ml_score",
            trigger_detail="ML score 0.81 exceeds threshold 0.75 — investigate account ACC_S2_001",
            risk_score=0.81,
        )
    else:
        return InvestigationPayload(
            investigation_id=iid,
            txn_id="TXN_S3_001",
            trigger_type="rule_engine",
            trigger_detail=(
                "amount: \u20b912,00,000 transfer exceeds threshold of \u20b910,00,000"
                " — investigate account ACC_S3_001"
            ),
        )


def run_benchmark(n_runs: int = 10) -> None:
    print(f"\n{'=' * 60}")
    print(f"  AML Investigation Agent - Benchmark ({n_runs} runs)")
    print(f"{'=' * 60}\n")

    conn = _pg()

    latencies_ms: list[float] = []
    costs: list[float] = []
    hops_list: list[int] = []
    escalated_count = 0
    error_count = 0

    # Distribute runs across 3 scenarios (round-robin)
    scenarios = [1, 2, 3]

    for i in range(n_runs):
        scenario = scenarios[i % len(scenarios)]
        payload = _make_payload(scenario)
        label = f"run {i + 1:02d}/{n_runs} (S{scenario})"

        print(f"  {label} — investigation_id={payload.investigation_id[:8]}...", end="", flush=True)
        t0 = time.monotonic()
        try:
            result = run_investigation(payload)
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            print(f"  ERROR ({elapsed:.0f}ms): {exc}")
            error_count += 1
            continue

        elapsed_ms = (time.monotonic() - t0) * 1000
        status = result.get("status")

        if status in ("log_only", "already_investigating"):
            print(f"  SKIP ({status})")
            continue

        db = _fetch_db_metrics(conn, payload.investigation_id)
        latencies_ms.append(elapsed_ms)
        costs.append(db["cost_usd"])
        hops_list.append(db["hops"])
        if db["escalated"]:
            escalated_count += 1

        marker = "ESC" if db["escalated"] else "OK "
        print(
            f"  [{marker}] {elapsed_ms:6.0f}ms  hops={db['hops']}  cost=${db['cost_usd']:.4f}"
        )

    conn.close()

    if not latencies_ms:
        print("\n  No completed investigations — cannot compute metrics.")
        return

    n = len(latencies_ms)
    latencies_ms.sort()
    costs_sorted = sorted(costs)

    def pct(data: list[float], p: float) -> float:
        idx = int(len(data) * p / 100)
        idx = min(idx, len(data) - 1)
        return data[idx]

    p50_lat = pct(latencies_ms, 50)
    p95_lat = pct(latencies_ms, 95)
    p99_lat = pct(latencies_ms, 99)
    avg_lat = statistics.mean(latencies_ms)
    avg_cost = statistics.mean(costs)
    avg_hops = statistics.mean(hops_list)
    escalation_rate = escalated_count / n * 100

    print(f"\n{'-' * 60}")
    print(f"  Results ({n} completed, {error_count} errors, {n_runs} total)")
    print(f"{'-' * 60}")
    print(f"  Latency p50:          {p50_lat:8.0f} ms")
    print(f"  Latency p95:          {p95_lat:8.0f} ms")
    print(f"  Latency p99:          {p99_lat:8.0f} ms")
    print(f"  Latency avg:          {avg_lat:8.0f} ms")
    print(f"  Avg cost/investigation:  ${avg_cost:.4f}")
    print(f"  Avg hops/investigation:  {avg_hops:.2f}")
    print(f"  Escalation rate:         {escalation_rate:.0f}%  ({escalated_count}/{n})")
    print(f"{'-' * 60}\n")

    _write_benchmarks_md(
        n_runs=n_runs,
        n_completed=n,
        p50=p50_lat,
        p95=p95_lat,
        p99=p99_lat,
        avg_lat=avg_lat,
        avg_cost=avg_cost,
        avg_hops=avg_hops,
        escalation_rate=escalation_rate,
        escalated=escalated_count,
    )


def _write_benchmarks_md(
    *,
    n_runs: int,
    n_completed: int,
    p50: float,
    p95: float,
    p99: float,
    avg_lat: float,
    avg_cost: float,
    avg_hops: float,
    escalation_rate: float,
    escalated: int,
) -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    content = f"""# Benchmarks — Autonomous AML Investigation Agent
Run: {date.today()}, Hardware: WSL2 (10GB RAM, 8 cores), LLM: gpt-4o-mini

## Investigation Latency & Cost

| Metric | Value | Notes |
|---|---|---|
| p50 investigation latency | {p50:.0f} ms | wall-clock, direct graph invocation |
| p95 investigation latency | {p95:.0f} ms | includes LLM round-trips |
| p99 investigation latency | {p99:.0f} ms | worst-case in {n_completed}-run sample |
| Avg investigation latency | {avg_lat:.0f} ms | |
| Avg cost / investigation | ${avg_cost:.4f} | gpt-4o-mini, real OpenAI API |
| Avg hops / investigation | {avg_hops:.2f} | max 4 by design |
| Escalation rate | {escalation_rate:.0f}% | {escalated}/{n_completed} runs |

## Test Setup

- {n_runs} total runs, {n_completed} completed (round-robin across 3 scenarios)
- Scenario 1: velocity rule — 17 txns to high-risk counterparty (ACC_S1_001)
- Scenario 2: ML score 0.81 — 4 medium-risk counterparties (ACC_S2_001)
- Scenario 3: large transfer, clean account (ACC_S3_001)
- Direct `run_investigation()` call — no HTTP overhead
- Postgres + Redis running via Docker Compose (local WSL2)
- Seed data pre-loaded via `python scripts/seed.py`

## Scale Ceiling

| Limit | Value | Enforced by |
|---|---|---|
| Max hops before escalation | 4 | Hard limit in `agent/limits.py` |
| Cost cap per investigation | $0.05 | Hard limit — exits to ESCALATED |
| Investigation timeout | 30 s | Hard limit — exits to ESCALATED |
| Concurrency | Per-txn_id Redis mutex | Duplicate investigations blocked |
"""
    out = DOCS_DIR / "benchmarks.md"
    out.write_text(content, encoding="utf-8")
    print(f"  Wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AML agent benchmark")
    parser.add_argument(
        "--runs",
        type=int,
        default=10,
        help="Number of investigations to run (default: 10)",
    )
    args = parser.parse_args()
    run_benchmark(args.runs)
