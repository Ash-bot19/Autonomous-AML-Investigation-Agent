"""create investigation_audit_log

Revision ID: 0001
Revises:
Create Date: 2026-04-15
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("""
        CREATE TABLE IF NOT EXISTS investigation_audit_log (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            investigation_id  UUID NOT NULL,
            event_type        TEXT NOT NULL CHECK (event_type IN ('triggered', 'state_change', 'tool_call', 'resolved', 'escalated')),
            event_detail      JSONB,
            state_from        TEXT,
            state_to          TEXT,
            cost_usd_delta    FLOAT DEFAULT 0,
            created_at        TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_log_investigation_id
        ON investigation_audit_log (investigation_id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_audit_log_investigation_id")
    op.execute("DROP TABLE IF EXISTS investigation_audit_log")
