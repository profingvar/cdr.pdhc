"""#984: cdr_read_audit is THE ONE access log for diagnoses.

Revision ID: a9b8c7d6e5f4
Revises: e1f2a3b4c5d6
Create Date: 2026-11-17

Operator decision 2026-11-17 (ticket #984). Additive, all nullable:

  on_behalf_of_user_guid  String(36), indexed -- the end user a service in
                          read_audit.ON_BEHALF_SERVICES states it acts for
                          (a recorded claim, like the forwarded session id)
  outcome                 String(16): served / denied / break_glass. Rows
                          written before this revision stay NULL = served.
  detail                  JSON: reason, as_of, scope, refusal_code -- never
                          clinical content.

and the table becomes APPEND-ONLY at the database layer: the same trigger
DDL as condition_history (app/models/history_guard.ddl_for), so UPDATE is
always refused and DELETE only inside `history_purge()`.
"""
from alembic import op
import sqlalchemy as sa

revision = "a9b8c7d6e5f4"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None

TABLE = "cdr_read_audit"
INDEX = "ix_cdr_read_audit_on_behalf_of_user_guid"


def _drop_triggers(dialect):
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {TABLE}_append_only_trg ON {TABLE}"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {TABLE}_append_only()"))
    elif dialect == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {TABLE}_no_update"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {TABLE}_no_delete"))


def upgrade():
    with op.batch_alter_table(TABLE) as b:
        b.add_column(sa.Column("on_behalf_of_user_guid", sa.String(36), nullable=True))
        b.add_column(sa.Column("outcome", sa.String(16), nullable=True))
        b.add_column(sa.Column("detail", sa.JSON(), nullable=True))
    op.create_index(INDEX, TABLE, ["on_behalf_of_user_guid"])

    from app.models.history_guard import ddl_for
    for stmt in ddl_for(TABLE, op.get_bind().dialect.name):
        op.execute(sa.text(stmt))


def downgrade():
    # Triggers first: on SQLite a batch drop recreates the table.
    _drop_triggers(op.get_bind().dialect.name)
    op.drop_index(INDEX, table_name=TABLE)
    with op.batch_alter_table(TABLE) as b:
        b.drop_column("detail")
        b.drop_column("outcome")
        b.drop_column("on_behalf_of_user_guid")
