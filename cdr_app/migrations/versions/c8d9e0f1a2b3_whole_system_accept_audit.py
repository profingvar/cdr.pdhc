"""cdr_audit_whole_system_accept — why a code was accepted, not just that it was

#872. plan.pdhc's ``$validate-code`` normally answers from a Concept or
ValueCatalog row for that exact code. For a CLASSIFICATION — a canonical lib
with ``whole_system_answer_space`` — it instead answers ``result: true`` for
ANY code in the system, because ~85 000 ICD-11 codes picked at the point of
care cannot be pre-enumerated. plan says so on the wire: a ``whole_system``
boolean and a message naming icd11.pdhc as the per-code existence checker.

Measured 2026-11-01: **nothing read that boolean.** Zero readers outside
plan.pdhc, and cdr's own parser carried the key into a dict nobody inspected.
So an ICD-11 diagnosis and a planned LOINC measurement were indistinguishable
in this repository's record of why each was accepted — not because anything
was let through wrongly, but because the basis was never written down.

This table is the mirror of ``cdr_audit_plan_miss``: same shape, same dedup,
read together. One says what plan refused; this one says what plan accepted
without being asked about the specific code.

Additive only. No existing table is touched, nothing is backfilled — rows
written before this migration have no recorded basis and cannot get one, which
is itself worth knowing when the table is first read.

Revision ID: c8d9e0f1a2b3
Revises: b6c7d8e9f0a1
Create Date: 2026-11-01

"""
from alembic import op
import sqlalchemy as sa


revision = "c8d9e0f1a2b3"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None

TABLE = "cdr_audit_whole_system_accept"


def upgrade():
    # Guarded so a re-run on a database that already has it is a no-op,
    # matching the idempotence the other cdr migrations keep.
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("guid", sa.String(36), primary_key=True),
        sa.Column("canonical_uri", sa.String(512), nullable=False),
        sa.Column("canonical_lib_name", sa.String(128), nullable=True),
        sa.Column("canonical_refnumber", sa.String(128), nullable=True),
        sa.Column("seen_count", sa.Integer, nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_request_id", sa.String(64), nullable=True),
        sa.UniqueConstraint("canonical_uri", name="uq_whole_system_canonical"),
    )


def downgrade():
    # The table is an audit record, not a dependency of any write path: the
    # canonicaliser only ever INSERTs here. Dropping it loses the basis for
    # every code already accepted, so it is not recoverable by re-running.
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)
