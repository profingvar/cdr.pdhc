"""#984: condition_history and encounter_history are append-only.

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
Create Date: 2026-11-17

The CDR is the ONE diagnosis register (operator, 2026-11-17), replacing
icd11.pdhc's append-only assertion log. The trigger is the database layer of
app/models/history_guard.py; the only way past it is `history_purge()`, which
sets `cdr.allow_history_purge` for its own transaction and still never allows
an UPDATE.
"""
from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None

TABLES = ("condition_history", "encounter_history")


def upgrade():
    from app.models.history_guard import ddl_for
    dialect = op.get_bind().dialect.name
    for t in TABLES:
        for stmt in ddl_for(t, dialect):
            op.execute(sa.text(stmt))


def downgrade():
    dialect = op.get_bind().dialect.name
    for t in TABLES:
        if dialect == "postgresql":
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {t}_append_only_trg ON {t}"))
            op.execute(sa.text(f"DROP FUNCTION IF EXISTS {t}_append_only()"))
        elif dialect == "sqlite":
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {t}_no_update"))
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {t}_no_delete"))
