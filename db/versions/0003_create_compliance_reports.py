"""create compliance_reports

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-15
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS compliance_reports (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            investigation_id  UUID NOT NULL UNIQUE,
            txn_id            TEXT NOT NULL,
            verdict           TEXT NOT NULL,
            confidence        FLOAT NOT NULL,
            finding           TEXT NOT NULL,
            evidence_chain    JSONB NOT NULL DEFAULT '[]',
            recommendation    TEXT NOT NULL,
            narrative         TEXT NOT NULL,
            total_hops        INT NOT NULL,
            total_cost_usd    FLOAT NOT NULL DEFAULT 0,
            resolved_at       TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_compliance_reports_investigation_id
        ON compliance_reports (investigation_id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_compliance_reports_investigation_id")
    op.execute("DROP TABLE IF EXISTS compliance_reports")
