# Autonomous AML Investigation Agent

A LangGraph-based compliance agent that autonomously investigates flagged transactions using real financial system tools, produces a traceable compliance report with a full evidence chain, and routes low-confidence cases to a human analyst queue.

Built as a portfolio project demonstrating: agentic reasoning loops, LangGraph state machines, tool orchestration, human-in-the-loop escalation, cost-per-investigation tracking, and immutable audit trails.

---

## What It Does

When a transaction is flagged — by a deterministic rule engine or an XGBoost ML risk score — the agent:

1. Runs a multi-hop investigation using 6 real tools (Postgres, Redis, Kafka, static watchlist)
2. Decides at each hop whether to call another tool or close the investigation
3. Produces a structured compliance report: verdict, confidence, evidence chain, narrative, cost
4. Escalates to a human analyst queue if confidence < 0.70, max hops (4) exceeded, 30s timeout, or $0.05 cost cap hit

The LLM reasons over tool results. It never replaces them. Hard limits are enforced by the state machine, not the LLM.

---

## Architecture

```
Rule Engine Triggers                        ML Score Trigger (XGBoost)
  amount > ₹10L                               score < 0.55   → ignore
  velocity > 5 txns/hr                        score 0.55–0.75 → log only
  round-trip A→B→C→A                         score > 0.75   → investigate
  counterparty on watchlist                   score > 0.90   → high priority
         │                                           │
         └─────────────────────┬─────────────────────┘
                               │ flagged-transactions (Kafka topic)
                               ▼
  ┌──────────────────────────────┐    ┌──────────────────────────────┐
  │  kafka/consumer.py           │    │  FastAPI POST /investigate    │  Direct API
  │  Kafka Consumer              │    │  async handler               │  for demo / CI
  └───────────────┬──────────────┘    └──────────────┬───────────────┘
                  └─────────────────┬────────────────┘
                                    │ InvestigationPayload
                                    ▼
                  ┌─────────────────────────────────────┐
                  │  agent/runner.py                     │  Redis mutex: mutex:investigation:{txn_id}
                  │  ML score routing + dispatch         │  Blocks duplicate investigations per txn
                  └─────────────────┬───────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────┐
│  LangGraph State Machine                                               │
│                                                                        │
│  IDLE ──▶ INVESTIGATING ──▶ TOOL_CALLING ──▶ EVALUATING ──▶ RESOLVED │
│                                                    │                   │
│                                                    └──▶ ESCALATED     │
│                                                                        │
│  LLM: tool selection · evidence evaluation · confidence scoring        │
│  Hard limits (state machine enforces — LLM cannot override):          │
│    max 4 hops · confidence ≥ 0.70 · 30s timeout · $0.05 cost cap     │
└──────────────────────────────────┬────────────────────────────────────┘
                                   │ dispatch_tool(tool_name, input)
                                   │ every call logged to tool_execution_log
                                   ▼
         ┌─────────────────────────────────────────────────────────┐
         │  Tool Belt — 6 tools                                    │  Deterministic Python
         │                                                         │  Never calls LLM internally
         ├──────────────────────────┬──────────────────────────────┤
         │  txn_history_query       │  Last 90 days of txns        │ ──▶ PostgreSQL
         │  counterparty_risk_lookup│  Risk tier + flag reason     │
         │  round_trip_detector     │  Recursive CTE — A→B→C→A    │
         ├──────────────────────────┼──────────────────────────────┤
         │  velocity_check          │  ZRANGEBYSCORE ×3 pipeline   │ ──▶ Redis + PostgreSQL
         │                          │  count + volume: 1h/6h/24h   │
         ├──────────────────────────┼──────────────────────────────┤
         │  watchlist_lookup        │  OFAC-style CSV, in-memory   │ ──▶ Static data
         ├──────────────────────────┼──────────────────────────────┤
         │  kafka_lag_check         │  Consumer group lag          │ ──▶ Kafka admin API
         └──────────────────────────┴──────────────────────────────┘
                                   │
                                   ▼
         ┌─────────────────────────────────────────────────────────┐
         │  PostgreSQL                                             │
         │  tool_execution_log   every hop · input · output · ms  │
         │  compliance_reports   verdict · evidence chain · cost   │
         │  escalation_queue     partial report · reason · status  │
         │  investigation_audit_log  append-only · no UPDATE/DEL  │
         │  transactions · counterparty_risk  (agent: read-only)  │
         └───────────────────────────┬─────────────────────────────┘
                                     │
              ┌──────────────────────┴─────────────────────┐
              ▼                                            ▼
    ┌─────────────────────────────┐    ┌─────────────────────────────┐
    │  Streamlit Dashboard        │    │  Prometheus                 │
    │  · Open investigations      │    │  /metrics endpoint          │
    │  · Compliance reports       │    │  investigation_count        │
    │  · Escalation queue         │    │  escalation_rate            │
    │  · Cost metrics             │    │  avg_cost_per_investigation │
    │  · Operational metrics      │    │  avg_hops_per_investigation │
    └─────────────────────────────┘    └─────────────────────────────┘
```

---

## Running the Project

### Services

| Service | Port | URL |
|---|---|---|
| PostgreSQL | 5432 | — |
| Redis | 6379 | — |
| Kafka | 9092 | — |
| FastAPI | 8000 | http://localhost:8000/docs |
| Streamlit UI | 8501 | http://localhost:8501 |
| Prometheus | 9090 | http://localhost:9090 |

### One-command startup

```bash
cp .env.example .env        # fill in OPENAI_API_KEY and DB credentials
docker compose up -d        # start Postgres, Redis, Kafka, Prometheus
python scripts/seed.py      # load demo data for all 3 scenarios
python scripts/start.py     # health-check infra, run migrations, start API + UI
```

`scripts/start.py` supervises all processes: retries infra up to 5 times, restarts crashed subprocesses once, and logs each service to `logs/<name>.log`.

### Individual service commands

```bash
# PostgreSQL (via Docker only)
docker compose up -d postgres

# Redis (via Docker only)
docker compose up -d redis

# Kafka (via Docker only)
docker compose up -d kafka

# Prometheus (via Docker only)
docker compose up -d prometheus

# FastAPI
python -m uvicorn api.main:app --reload --port 8000

# Streamlit UI
streamlit run ui/app.py --server.port 8501

# Kafka consumer (standalone)
python -m kafka.consumer
```

---

## Triggering an Investigation

### Via API

```bash
curl -X POST http://localhost:8000/investigate \
  -H "Content-Type: application/json" \
  -d '{
    "txn_id": "TXN_S1_001",
    "trigger_type": "rule_engine",
    "trigger_detail": "velocity_breach: 17 txns in 2h to ACC_S1_CP",
    "risk_score": null
  }'
```

```bash
# Check status
curl http://localhost:8000/status/{investigation_id}

# Get full report
curl http://localhost:8000/report/{investigation_id}
```

### Via Kafka

Produce a JSON message matching the `InvestigationPayload` schema to the `flagged-transactions` topic. The consumer picks it up and runs the investigation automatically.

---

## Demo Scenarios

Run `python scripts/seed.py` to load all three:

| Scenario | Trigger | Expected outcome |
|---|---|---|
| 1 — Suspicious | Velocity rule: 17 txns in 2h to high-risk counterparty | RESOLVED — suspicious, confidence ~0.87, recommend file_SAR |
| 2 — Escalation | ML score 0.81, 4 medium-risk counterparties | ESCALATED — max_hops exceeded, partial report in queue |
| 3 — Clean | Large transfer, clean history, low-risk counterparty | RESOLVED — clean, confidence ~0.91, recommend close_clean |

---

## Compliance Report Structure

Every resolved investigation produces:

```json
{
  "investigation_id": "uuid",
  "txn_id": "TXN_S1_001",
  "verdict": "suspicious",
  "confidence": 0.87,
  "finding": "23 transactions to high-risk counterparty in 6 hours",
  "evidence_chain": [
    { "hop": 1, "tool": "velocity_check", "finding": "...", "significance": "high" }
  ],
  "recommendation": "file_SAR",
  "narrative": "LLM-generated, grounded in evidence_chain only",
  "total_hops": 3,
  "total_cost_usd": 0.0003,
  "resolved_at": "2026-04-18T10:00:00Z"
}
```

The evidence chain is sourced exclusively from `tool_execution_log` — never from LLM memory.

---

## Hard Limits

| Limit | Value | On breach |
|---|---|---|
| Max tool hops | 4 | Force exit → ESCALATED |
| Confidence threshold | 0.70 | Below → ESCALATED |
| Investigation timeout | 30 seconds | → ESCALATED |
| Cost cap | $0.05 USD | → ESCALATED |

The agent escalates rather than guesses. The LLM cannot override hard limits.

---

## Benchmarks

Run: 2026-04-18 · Hardware: WSL2 (10GB RAM, 8 cores) · LLM: gpt-4o-mini

| Metric | Value |
|---|---|
| p50 investigation latency | 5.7s |
| p95 investigation latency | 13.1s |
| Avg cost / investigation | $0.0003 |
| Avg hops / investigation | 1.0 |
| Escalation rate | 0% (seeded data) |
| Cost cap | $0.05 hard limit |

Full results: [`docs/benchmarks.md`](docs/benchmarks.md)

---

## Project Structure

```
agent/          # LangGraph graph, state schema, hard limits, LLM client
tools/          # 6 deterministic tool implementations + dispatcher
models/         # Pydantic schemas, SQLAlchemy ORM models
db/             # Alembic migrations
api/            # FastAPI app — /investigate, /status, /report, /metrics
ui/             # Streamlit dashboard — 5 views
kafka/          # Kafka consumer for flagged transaction events
monitoring/     # prometheus.yml
scripts/        # start.py (supervisor), seed.py (demo data), benchmark.py
data/           # OFAC-style watchlist CSV
tests/          # unit/, integration/, e2e/
docs/           # benchmarks.md
```

---

## Environment Variables

Copy `.env.example` and fill in:

```
OPENAI_API_KEY=
POSTGRES_USER=
POSTGRES_PASSWORD=
POSTGRES_DB=
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
REDIS_HOST=localhost
REDIS_PORT=6379
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
KAFKA_CONSUMER_GROUP=aml-investigation-agent
KAFKA_TOPIC=flagged-transactions
```
