"""create tool_execution_log

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-15
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS tool_execution_log (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            investigation_id  UUID NOT NULL,
            hop_number        INT NOT NULL,
            tool_name         TEXT NOT NULL,
            tool_input        JSONB,
            tool_output       JSONB,
            latency_ms        INT,
            called_at         TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_tool_log_investigation_id
        ON tool_execution_log (investigation_id)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tool_log_investigation_id")
    op.execute("DROP TABLE IF EXISTS tool_execution_log")
