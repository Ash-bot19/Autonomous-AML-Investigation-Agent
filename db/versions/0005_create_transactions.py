"""create transactions

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-16

Adds the transactions table used by:
  - txn_history_query (last 90 days for an account)
  - round_trip_detector (A->B->C->A cycle detection)
  - velocity_check seed data source
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            txn_id          TEXT PRIMARY KEY,
            account_id      TEXT NOT NULL,
            counterparty_id TEXT NOT NULL,
            amount_inr      NUMERIC(18, 2) NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_transactions_account_id
        ON transactions (account_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_transactions_timestamp
        ON transactions (timestamp)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_transactions_account_ts
        ON transactions (account_id, timestamp)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_transactions_account_ts")
    op.execute("DROP INDEX IF EXISTS idx_transactions_timestamp")
    op.execute("DROP INDEX IF EXISTS idx_transactions_account_id")
    op.execute("DROP TABLE IF EXISTS transactions")
