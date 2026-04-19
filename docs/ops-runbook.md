# AML Agent — Ops Runbook

Commands used to start, verify, and manage services. Accumulated per phase for Notion export.

---

## Phase 1 — Infrastructure & Schema

### Start the stack

```bash
docker compose up -d
docker compose ps          # verify all containers: healthy
```

### Stop the stack

```bash
docker compose down
```

### Health checks (individual services)

```bash
# PostgreSQL
docker exec aml-postgres pg_isready -U aml_user -d aml_db

# Redis
docker exec aml-redis redis-cli ping

# Kafka (list topics — works when broker is ready)
docker exec aml-kafka kafka-topics.sh --bootstrap-server localhost:9092 --list
```

### Migrations

```bash
# Apply all pending migrations
python -m alembic upgrade head

# Check current revision
python -m alembic current

# Full history
python -m alembic history

# Roll back everything (dev only)
python -m alembic downgrade base
```

### Verify tables in PostgreSQL

```bash
docker exec aml-postgres psql -U aml_user -d aml_db -c \
  "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;"
```

Expected output:
```
    table_name
----------------------------
 alembic_version
 compliance_reports
 escalation_queue
 investigation_audit_log
 tool_execution_log
```

### Supervisor startup

```bash
python scripts/start.py
```

Expected output:
```
[INFO]  Starting AML Investigation Agent
[OK]    postgres   — healthy
[OK]    redis      — healthy
[OK]    kafka      — healthy
[OK]    migrations — applied
[OK]    api        — started (exited 0)
[OK]    ui         — started (exited 0)
```

### Seed + data validation

```bash
# Run seed (stubs in Phase 1 — no-op)
python scripts/seed.py

# Validate watchlist CSV
python -c "import csv; rows=list(csv.DictReader(open('data/watchlist.csv'))); assert len(rows)==5; print('OK —', len(rows), 'rows')"
```

### Directory & gitignore checks

```bash
# Verify directory structure
find . -type d | grep -v ".git" | grep -v "__pycache__" | grep -v ".planning" | sort

# Verify __init__.py files
find . -name "__init__.py" | grep -v ".git" | grep -v "__pycache__" | sort

# Confirm logs/ is gitignored
git check-ignore -v logs/

# Confirm .env is gitignored
git check-ignore -v .env
```

### Logs (created by start.py at runtime)

```bash
cat logs/migration.log    # alembic upgrade output
cat logs/api.log          # api subprocess output
cat logs/ui.log           # ui subprocess output
```

---

*Add Phase 2 commands below after Phase 2 completes.*
