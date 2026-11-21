"""#1014: data_origin — patient-generated or (NULL) healthcare.

A patient app is coming, and patient-generated data has a different legal
status: the professional must be able to see it. One column, in two places:

* `clinical_context.data_origin`: where the rest of the provenance lives
  (#821's argument), indexed.
* every per-type live table and its history: on the row, as #769 did, so
  the series read the professional sees needs no join. History mirrors live
  so a versioned read answers the same question as a current one.

Opt-in (operator 2026-11-21): the only value is 'patient_generated'; NULL
means produced or endorsed by healthcare. Nullable, no backfill. Every
existing row IS healthcare data, because no patient app has ever submitted.

Additive only. cdr1/cdr2/cdr3 were all at b0c1d2e3f4a5 when this was written
(checked 2026-11-21).

Revision ID: c1d4e7f0a2b5
Revises: b0c1d2e3f4a5
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa

revision = "c1d4e7f0a2b5"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None

COLUMN = "data_origin"
CTX = "clinical_context"
CTX_INDEX = "ix_clinical_context_data_origin"

# Listed literally, as in a7b8c9d0e1f2: a migration describes the schema at
# THIS revision, not whatever the model says when it is next run.
TABLES = [
    "patient", "observation", "questionnaire_response", "condition",
    "medication_statement", "medication_request", "allergy_intolerance",
    "procedure", "encounter", "diagnostic_report",
]


def _targets():
    yield CTX
    for t in TABLES:
        yield t
        yield f"{t}_history"


def upgrade():
    insp = sa.inspect(op.get_bind())
    existing = set(insp.get_table_names())
    for table in _targets():
        if table not in existing:
            continue
        if COLUMN not in {c["name"] for c in insp.get_columns(table)}:
            op.add_column(table, sa.Column(COLUMN, sa.String(32), nullable=True))
    if CTX in existing and CTX_INDEX not in {i["name"] for i in insp.get_indexes(CTX)}:
        op.create_index(CTX_INDEX, CTX, [COLUMN])


def downgrade():
    insp = sa.inspect(op.get_bind())
    existing = set(insp.get_table_names())
    if CTX in existing and CTX_INDEX in {i["name"] for i in insp.get_indexes(CTX)}:
        op.drop_index(CTX_INDEX, table_name=CTX)
    for table in _targets():
        if table in existing and COLUMN in {c["name"] for c in insp.get_columns(table)}:
            op.drop_column(table, COLUMN)
