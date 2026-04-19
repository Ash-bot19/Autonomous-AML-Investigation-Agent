"""create counterparty_risk

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-16

Adds the counterparty_risk table used by counterparty_risk_lookup tool.
risk_tier has a DB-level CHECK constraint enforcing exactly (low|medium|high).
Always queried by PK (account_id) — no additional indexes needed.
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS counterparty_risk (
            account_id  TEXT PRIMARY KEY,
            risk_tier   TEXT NOT NULL CHECK (risk_tier IN ('low', 'medium', 'high')),
            flag_reason TEXT
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS counterparty_risk")
