"""
Demo seed data for AML Investigation Agent.

Three scenarios matching the Interview Demo Script in CLAUDE.md:
- Scenario 1: Suspicious investigation — velocity rule fires, resolved with 3 hops
- Scenario 2: Escalation path — ML score 0.81, max hops reached
- Scenario 3: Clean investigation — large transfer, clean history, 2 hops

Usage:
    python scripts/seed.py
"""

# WARNING: Do not run while an investigation is in progress.
# ZSET deletion (delete + zadd pattern below) creates a brief window of empty
# velocity data. Any velocity_check call during that window returns count=0.
# This is safe for demo seeding; never run against a live investigation workload.

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import psycopg2
import redis
import structlog
from dotenv import load_dotenv

from models.schemas import InvestigationPayload
from agent.runner import run_investigation

load_dotenv()

log = structlog.get_logger()


def _get_pg_conn():
    user = quote_plus(os.environ["POSTGRES_USER"])
    password = quote_plus(os.environ["POSTGRES_PASSWORD"])
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return psycopg2.connect(f"postgresql://{user}:{password}@{host}:{port}/{db}")


def _get_redis():
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
    )


def _already_seeded(investigation_id: str) -> bool:
    """Return True if this investigation_id already has a row in compliance_reports OR escalation_queue."""
    conn = _get_pg_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1 FROM compliance_reports WHERE investigation_id = %s
            UNION
            SELECT 1 FROM escalation_queue WHERE investigation_id = %s
            """,
            (investigation_id, investigation_id),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def seed_scenario_1() -> None:
    """
    Seed Scenario 1: Suspicious investigation (resolved, 3 hops).

    Account ACC_S1_001 with 17 transfers to high-risk counterparty ACC_S1_CP
    in under 2 hours — velocity rule fires.
    Agent hops: velocity_check -> txn_history_query -> round_trip_detector
    Expected verdict: suspicious, confidence 0.87
    """
    iid_s1 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "seed-scenario-1"))
    if _already_seeded(iid_s1):
        log.info("seed.already_seeded", scenario="scenario_1", investigation_id=iid_s1)
        return

    conn = _get_pg_conn()
    r = _get_redis()
    now = datetime.now(timezone.utc)

    cur = None
    try:
        cur = conn.cursor()

        # Counterparty risk: ACC_S1_CP is a high-risk shell company
        cur.execute(
            """
            INSERT INTO counterparty_risk (account_id, risk_tier, flag_reason)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            ("ACC_S1_CP", "high", "Shell company linked to 3 prior investigations"),
        )

        # 17 transactions to ACC_S1_CP within the last 2 hours, spaced ~6-7 min apart
        # Plus 1 historical transaction 90 days ago for history depth
        txns = []
        zset_entries: dict[str, float] = {}

        for i in range(17):
            minutes_ago = (i + 1) * 7  # 7 to 119 minutes ago
            ts = now - timedelta(minutes=minutes_ago)
            txn_id = f"TXN_S1_{i + 1:03d}"
            amount = 50000 + (i % 7) * 21428  # varies 50000-200000
            txns.append((txn_id, "ACC_S1_001", "ACC_S1_CP", amount, ts))
            zset_entries[txn_id] = ts.timestamp()

        # 1 historical transaction for 90-day count
        ts_hist = now - timedelta(days=90)
        txn_id_hist = "TXN_S1_HIST"
        txns.append((txn_id_hist, "ACC_S1_001", "ACC_S1_CP", 75000, ts_hist))
        zset_entries[txn_id_hist] = ts_hist.timestamp()

        cur.executemany(
            """
            INSERT INTO transactions (txn_id, account_id, counterparty_id, amount_inr, timestamp)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            txns,
        )
        conn.commit()

        # Redis ZSET: delete key first for idempotency, then zadd
        r.delete("velocity:ACC_S1_001")
        r.zadd("velocity:ACC_S1_001", zset_entries)

        log.info("seed_scenario_1.complete", txn_count=len(txns), zset_count=len(zset_entries))

    finally:
        if cur is not None:
            cur.close()
        conn.close()

    # Run investigation against the seeded data (DEMO-01)
    payload_s1 = InvestigationPayload(
        investigation_id=iid_s1,
        txn_id="TXN_S1_001",
        trigger_type="rule_engine",
        trigger_detail="velocity: 17 transactions to ACC_S1_CP in 2 hours exceeds threshold of 5",
    )
    log.info("seed_scenario_1.running_investigation", investigation_id=iid_s1)
    result_s1 = run_investigation(payload_s1)
    log.info(
        "seed_scenario_1.investigation_complete",
        status=result_s1.get("status"),
        investigation_id=result_s1.get("investigation_id"),
    )


def seed_scenario_2() -> None:
    """
    Seed Scenario 2: Escalation via max hops.

    ML score 0.81 triggers investigation on ACC_S2_001.
    4 different counterparties, no obvious pattern — forces all 4 tool hops.
    Agent hops: txn_history_query -> counterparty_risk_lookup -> watchlist_lookup -> velocity_check
    Exit: ESCALATED — max_hops exceeded (4 hops)
    """
    iid_s2 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "seed-scenario-2"))
    if _already_seeded(iid_s2):
        log.info("seed.already_seeded", scenario="scenario_2", investigation_id=iid_s2)
        return

    conn = _get_pg_conn()
    r = _get_redis()
    now = datetime.now(timezone.utc)

    cur = None
    try:
        cur = conn.cursor()

        # 4 medium-risk counterparties
        counterparties = [
            ("ACC_S2_CP1", "medium", "Flagged for monitoring"),
            ("ACC_S2_CP2", "medium", "Flagged for monitoring"),
            ("ACC_S2_CP3", "medium", "Flagged for monitoring"),
            ("ACC_S2_CP4", "medium", "Flagged for monitoring"),
        ]
        cur.executemany(
            """
            INSERT INTO counterparty_risk (account_id, risk_tier, flag_reason)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            counterparties,
        )

        # 4 transactions, one to each counterparty, spread over 6 hours ago
        txns = []
        zset_entries: dict[str, float] = {}
        amounts = [100000, 250000, 350000, 500000]
        for i in range(4):
            hours_ago = (i + 1) * 1.5  # 1.5 to 6 hours ago
            ts = now - timedelta(hours=hours_ago)
            txn_id = f"TXN_S2_{i + 1:03d}"
            cp = f"ACC_S2_CP{i + 1}"
            txns.append((txn_id, "ACC_S2_001", cp, amounts[i], ts))
            zset_entries[txn_id] = ts.timestamp()

        cur.executemany(
            """
            INSERT INTO transactions (txn_id, account_id, counterparty_id, amount_inr, timestamp)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            txns,
        )
        conn.commit()

        # Redis ZSET: delete for idempotency, then zadd
        r.delete("velocity:ACC_S2_001")
        r.zadd("velocity:ACC_S2_001", zset_entries)

        log.info("seed_scenario_2.complete", txn_count=len(txns), zset_count=len(zset_entries))

    finally:
        if cur is not None:
            cur.close()
        conn.close()

    # Run investigation against the seeded data (DEMO-02)
    payload_s2 = InvestigationPayload(
        investigation_id=iid_s2,
        txn_id="TXN_S2_001",
        trigger_type="ml_score",
        trigger_detail="ML score 0.81 exceeds threshold 0.75",
        risk_score=0.81,
    )
    log.info("seed_scenario_2.running_investigation", investigation_id=iid_s2)
    result_s2 = run_investigation(payload_s2)
    log.info(
        "seed_scenario_2.investigation_complete",
        status=result_s2.get("status"),
        investigation_id=result_s2.get("investigation_id"),
    )


def seed_scenario_3() -> None:
    """
    Seed Scenario 3: Clean investigation (resolved, 2 hops).

    Large transfer from ACC_S3_001 triggers rule engine (amount > 10 lakh).
    Account has clean history — single transaction to a low-risk counterparty.
    Agent hops: txn_history_query -> counterparty_risk_lookup
    Expected verdict: clean, confidence 0.91
    """
    iid_s3 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "seed-scenario-3"))
    if _already_seeded(iid_s3):
        log.info("seed.already_seeded", scenario="scenario_3", investigation_id=iid_s3)
        return

    conn = _get_pg_conn()
    r = _get_redis()
    now = datetime.now(timezone.utc)

    cur = None
    try:
        cur = conn.cursor()

        # Low-risk counterparty
        cur.execute(
            """
            INSERT INTO counterparty_risk (account_id, risk_tier, flag_reason)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            ("ACC_S3_CP", "low", None),
        )

        # Single large transaction (rule triggers on amount > ₹10,00,000)
        txn_id = "TXN_S3_001"
        ts = now
        cur.execute(
            """
            INSERT INTO transactions (txn_id, account_id, counterparty_id, amount_inr, timestamp)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (txn_id, "ACC_S3_001", "ACC_S3_CP", 1200000, ts),
        )
        conn.commit()

        # Redis ZSET: delete for idempotency, then zadd
        r.delete("velocity:ACC_S3_001")
        r.zadd("velocity:ACC_S3_001", {txn_id: ts.timestamp()})

        log.info("seed_scenario_3.complete", txn_count=1, zset_count=1)

    finally:
        if cur is not None:
            cur.close()
        conn.close()

    # Run investigation against the seeded data (DEMO-03)
    payload_s3 = InvestigationPayload(
        investigation_id=iid_s3,
        txn_id="TXN_S3_001",
        trigger_type="rule_engine",
        trigger_detail="amount: \u20b912,00,000 transfer exceeds threshold of \u20b910,00,000",
    )
    log.info("seed_scenario_3.running_investigation", investigation_id=iid_s3)
    result_s3 = run_investigation(payload_s3)
    log.info(
        "seed_scenario_3.investigation_complete",
        status=result_s3.get("status"),
        investigation_id=result_s3.get("investigation_id"),
    )


def main() -> None:
    log.info("seed.starting")
    seed_scenario_1()
    log.info("seed.scenario_complete", scenario=1, description="suspicious — 17 txns, high-risk counterparty")
    seed_scenario_2()
    log.info("seed.scenario_complete", scenario=2, description="max_hops — 4 medium-risk counterparties")
    seed_scenario_3()
    log.info("seed.scenario_complete", scenario=3, description="clean — 1 large transfer, low-risk counterparty")
    log.info(
        "seed.complete",
        scenario_1_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, "seed-scenario-1")),
        scenario_2_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, "seed-scenario-2")),
        scenario_3_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, "seed-scenario-3")),
        note="check compliance_reports and escalation_queue for results",
    )


if __name__ == "__main__":
    main()
