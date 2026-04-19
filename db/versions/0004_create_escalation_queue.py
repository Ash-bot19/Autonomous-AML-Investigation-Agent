"""create escalation_queue

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-15
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS escalation_queue (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            investigation_id  UUID NOT NULL,
            txn_id            TEXT NOT NULL,
            escalation_reason TEXT NOT NULL CHECK (escalation_reason IN ('low_confidence', 'max_hops', 'timeout', 'cost_cap', 'empty_evidence')),
            confidence        FLOAT,
            partial_report    JSONB,
            analyst_id        TEXT,
            status            TEXT DEFAULT 'open' CHECK (status IN ('open', 'assigned', 'resolved')),
            created_at        TIMESTAMPTZ DEFAULT NOW(),
            resolved_at       TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_escalation_queue_investigation_id
        ON escalation_queue (investigation_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_escalation_queue_status
        ON escalation_queue (status)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_escalation_queue_status")
    op.execute("DROP INDEX IF EXISTS idx_escalation_queue_investigation_id")
    op.execute("DROP TABLE IF EXISTS escalation_queue")
