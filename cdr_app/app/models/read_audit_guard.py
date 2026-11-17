"""#984: the CDR's read log (`cdr_read_audit`) is APPEND-ONLY.

Operator decision 2026-11-17 (ticket #984): `cdr_read_audit` is THE ONE access
log for diagnoses -- icd11.pdhc's own `read_audit` is no longer written. A log
that the application can rewrite is not evidence of who read what, so it gets
the same three layers as the register's history (app/models/history_guard.py):

  1. no code path mutates a row (measured 2026-11-17: `record_read_audit` and
     `POST /api/v1/read-audit/events` only ever INSERT);
  2. a SQLAlchemy event raises on UPDATE or DELETE of a `ReadAudit` instance;
  3. a database trigger on both backends, which holds against raw SQL.

No escape of its own. The ONE escape on this platform is `history_purge()`
(a deliberate purge of SYNTHETIC data, its own transaction, asked for by
name) and it is shared rather than duplicated: the same flag opens DELETE here
for the length of that purge and nowhere else. UPDATE is refused even then.
No production code calls `history_purge()` on this table; the test suite uses
it to empty the table between tests.
"""
from __future__ import annotations

from sqlalchemy import event, text

from app.models.history_guard import _purging, ddl_for

TABLE = "cdr_read_audit"


class ReadAuditIsAppendOnly(RuntimeError):
    pass


def install(cls) -> None:
    """Attach the ORM guard and the create_all DDL to the ReadAudit model."""

    @event.listens_for(cls, "before_update", propagate=True)
    def _no_update(mapper, connection, target):
        raise ReadAuditIsAppendOnly(f"{TABLE} is append-only (#984): UPDATE refused")

    @event.listens_for(cls, "before_delete", propagate=True)
    def _no_delete(mapper, connection, target):
        if not _purging.get():
            raise ReadAuditIsAppendOnly(f"{TABLE} is append-only (#984): DELETE refused")

    @event.listens_for(cls.__table__, "after_create")
    def _ddl(target, connection, **kw):
        for stmt in ddl_for(target.name, connection.dialect.name):
            connection.execute(text(stmt))
