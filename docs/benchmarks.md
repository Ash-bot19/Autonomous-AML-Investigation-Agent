# Benchmarks — Autonomous AML Investigation Agent
Run: 2026-04-18, Hardware: WSL2 (10GB RAM, 8 cores), LLM: gpt-4o-mini

## Investigation Latency & Cost

| Metric | Value | Notes |
|---|---|---|
| p50 investigation latency | 5688 ms | wall-clock, direct graph invocation |
| p95 investigation latency | 13109 ms | includes LLM round-trips |
| p99 investigation latency | 13109 ms | worst-case in 10-run sample |
| Avg investigation latency | 6494 ms | |
| Avg cost / investigation | $0.0003 | gpt-4o-mini, real OpenAI API |
| Avg hops / investigation | 1.00 | max 4 by design |
| Escalation rate | 0% | 0/10 runs |

## Test Setup

- 10 total runs, 10 completed (round-robin across 3 scenarios)
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
