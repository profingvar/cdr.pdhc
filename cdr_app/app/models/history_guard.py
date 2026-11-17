"""#984: the CDR's diagnosis history is APPEND-ONLY.

Operator decision 2026-11-17: the CDR is the ONE register for diagnoses,
including their log. icd11.pdhc's append-only assertion log (#873) is retired
into the CDR's version history -- so that history must be at least as hard to
rewrite as the log it replaces. Enforced in three places, as in icd11 #873:

  1. no code path mutates a history row (measured 2026-11-17: none does);
  2. a SQLAlchemy event raises on UPDATE or DELETE of a guarded history class;
  3. a database trigger on both backends, which holds against raw SQL.

THE ONE ESCAPE is a deliberate purge of SYNTHETIC data (generations are purged
as a unit -- cdr_6, 2026-10-09). It must be asked for by name, inside
`history_purge()`, and it is the only way past all three layers.

Scope: the register's tables only (Condition, and Encounter, which carries the
primary/secondary diagnosis per encounter). Widening it to every resource is a
separate decision; nothing in #984 needs it.
"""
from __future__ import annotations

import contextlib
import contextvars

from sqlalchemy import event, text

GUARDED = ("Condition", "Encounter")
PG_FLAG = "cdr.allow_history_purge"
SQLITE_FLAG_TABLE = "cdr_history_purge_flag"

_purging = contextvars.ContextVar("cdr_history_purge", default=False)


class HistoryIsAppendOnly(RuntimeError):
    pass


@contextlib.contextmanager
def history_purge(session):
    """Allow DELETING guarded history rows, for a purge of synthetic data.

    The purge is ITS OWN TRANSACTION: it commits on a clean exit and rolls
    back on an error. The permission cannot outlive it -- on Postgres it is a
    SET LOCAL; on SQLite it is a flag row removed before that same commit (a
    first version let the caller commit, which saved the flag and left the
    guard open; caught by test_the_permission_does_not_outlive_the_purge).
    UPDATE stays refused even inside a purge.
    """
    dialect = session.get_bind().dialect.name
    token = _purging.set(True)
    try:
        if dialect == "postgresql":
            session.execute(text(f"SET LOCAL {PG_FLAG} = 'on'"))
        elif dialect == "sqlite":
            # A trigger on a main table cannot read the temp schema, so the
            # flag is a row in a main-schema table.
            session.execute(text(f"CREATE TABLE IF NOT EXISTS {SQLITE_FLAG_TABLE} (x INTEGER)"))
            session.execute(text(f"INSERT INTO {SQLITE_FLAG_TABLE} VALUES (1)"))
        yield
        if dialect == "sqlite":
            session.execute(text(f"DELETE FROM {SQLITE_FLAG_TABLE}"))
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        _purging.reset(token)


def _pg_ddl(table: str) -> list[str]:
    fn = f"{table}_append_only"
    return [
        f"""CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF coalesce(current_setting('{PG_FLAG}', true), '') = 'on' AND TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION '{table} is append-only (#984): % refused', TG_OP;
END $$""",
        f"DROP TRIGGER IF EXISTS {fn}_trg ON {table}",
        f"CREATE TRIGGER {fn}_trg BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {fn}()",
    ]


def _sqlite_ddl(table: str) -> list[str]:
    return [
        f"CREATE TABLE IF NOT EXISTS {SQLITE_FLAG_TABLE} (x INTEGER)",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
BEGIN SELECT RAISE(ABORT, '{table} is append-only (#984): UPDATE refused'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
WHEN NOT EXISTS (SELECT 1 FROM {SQLITE_FLAG_TABLE})
BEGIN SELECT RAISE(ABORT, '{table} is append-only (#984): DELETE refused'); END""",
    ]


def ddl_for(table: str, dialect: str) -> list[str]:
    return _pg_ddl(table) if dialect == "postgresql" else _sqlite_ddl(table) if dialect == "sqlite" else []


def install(history_models: dict) -> None:
    """Attach the ORM guard and the create_all DDL to each guarded class."""
    for name in GUARDED:
        cls = history_models[name]
        table = cls.__table__

        @event.listens_for(cls, "before_update", propagate=True)
        def _no_update(mapper, connection, target, _t=table.name):
            raise HistoryIsAppendOnly(f"{_t} is append-only (#984): UPDATE refused")

        @event.listens_for(cls, "before_delete", propagate=True)
        def _no_delete(mapper, connection, target, _t=table.name):
            if not _purging.get():
                raise HistoryIsAppendOnly(f"{_t} is append-only (#984): DELETE refused")

        @event.listens_for(table, "after_create")
        def _ddl(target, connection, **kw):
            for stmt in ddl_for(target.name, connection.dialect.name):
                connection.execute(text(stmt))
