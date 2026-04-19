"""harden audit_log and escalation_queue

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-17

Adds a BEFORE UPDATE OR DELETE trigger on investigation_audit_log that unconditionally
raises an exception (AUDT-06). This enforces the append-only contract at the database
level, independent of application-layer enforcement.

The CHECK constraints on escalation_queue.status and escalation_queue.escalation_reason
and the CHECK constraint on investigation_audit_log.event_type are already present in
migrations 0001 and 0004. This migration does NOT redefine them.
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_log_no_modify() RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'investigation_audit_log is append-only — % not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        DROP TRIGGER IF EXISTS trg_audit_log_no_modify ON investigation_audit_log
    """)

    op.execute("""
        CREATE TRIGGER trg_audit_log_no_modify
            BEFORE UPDATE OR DELETE ON investigation_audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_no_modify()
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS trg_audit_log_no_modify ON investigation_audit_log
    """)

    op.execute("""
        DROP FUNCTION IF EXISTS audit_log_no_modify()
    """)
