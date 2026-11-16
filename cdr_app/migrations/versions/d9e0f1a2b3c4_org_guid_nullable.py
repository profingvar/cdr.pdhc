"""org_guid may be NULL — no more invented all-zero organisation (#1006)

The FHIR write path (`fhir_write._extract_context`) and the ingest path
(`ingest_pipeline` live-row builder) filled `org_guid` with
"00000000-0000-0000-0000-000000000000" whenever the producer named no
performer organisation. An invented value cannot be told apart from a real
one: icd11.pdhc's nine ICD-11 Conditions landed in cdr2 with it on
2026-11-16 (#1005), and readers grouping on org_guid saw an organisation that
does not exist. Same rule as `patient_org_guid` (#818): unknown reads as NULL.

Measured before writing this (2026-11-16): 21 rows carried the zero guid —
cdr1 change_feed 1, patient 1; cdr2 condition 9, condition_history 1,
change_feed 10; cdr3 none. Every observation (75 668 across the three) has a
real org_guid and is untouched.

Upgrade, per table that has an `org_guid` column:
  1. drop NOT NULL;
  2. set the zero guid to NULL (operator decision: they were all invented).
Downgrade reverses both: NULL back to the zero guid, then NOT NULL — so the
schema and the values return exactly to what they were.

Tables are discovered by inspection, not listed: the per-resource live and
history tables are generated from a registry (`models.resources`), and a
hand-written list is exactly how one gets missed.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-11-16

"""
from alembic import op
import sqlalchemy as sa


revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None

ZERO = "00000000-0000-0000-0000-000000000000"
COLUMN = "org_guid"


def _tables_with_org_guid():
    insp = sa.inspect(op.get_bind())
    out = []
    for table in insp.get_table_names():
        for col in insp.get_columns(table):
            if col["name"] == COLUMN:
                out.append((table, col["nullable"]))
    return out


def upgrade():
    for table, nullable in _tables_with_org_guid():
        if not nullable:
            with op.batch_alter_table(table) as batch:
                batch.alter_column(COLUMN, existing_type=sa.String(36), nullable=True)
        op.execute(
            sa.text(f'UPDATE "{table}" SET {COLUMN} = NULL WHERE {COLUMN} = :zero')
            .bindparams(zero=ZERO))


def downgrade():
    for table, nullable in _tables_with_org_guid():
        op.execute(
            sa.text(f'UPDATE "{table}" SET {COLUMN} = :zero WHERE {COLUMN} IS NULL')
            .bindparams(zero=ZERO))
        if nullable:
            with op.batch_alter_table(table) as batch:
                batch.alter_column(COLUMN, existing_type=sa.String(36), nullable=False)
