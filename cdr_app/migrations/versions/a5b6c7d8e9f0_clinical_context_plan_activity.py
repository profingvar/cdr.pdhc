"""#820: the PlanDefinition ACTION (the visit) on clinical_context.

PDHC's plan model has three levels:

    PlanDefinition  ->  Activity  ->  Transaction
                        (the VISIT)   (one measurement in it)

`Activity` is FHIR `PlanDefinition.action`; in plan.pdhc it carries `title`,
`performer_type` and the whole timing/recurrence block, so it is the visit and
not a label. `clinical_context` already referenced the PlanDefinition and the
Transaction; the visit between them had nowhere to go, in **every** CDR —
cdr1 included. The operator's point: it should be part of all of them.

## Why the name is `plan_activity_guid`

cdr_6 already has a column called `activity_guid` and it is NOT this: it is the
deprecated #305 alias of `transaction_guid`, and cdr_6's ingest maps that
payload key ONTO `transaction_guid`. Reusing the name here would invite the
same key to mean two different things across the platform, and both are opaque
`String(36)` guids, so nothing downstream would flag the swap.

## Applied to cdr1, cdr2 and cdr3

The three run diverged application code (cdr2/cdr3 lack cdr1's
`clinical_read` module, and their `stats.py` is 28 lines against cdr1's 295 —
see `infra_cdr_codebase_is_not_one_codebase`), but their SCHEMAS are identical,
verified column-by-column on 2026-10-12. So one additive migration serves all
three.

Nullable, and no backfill: the field fills forward only for the same reason
`patient_org_guid` does — nothing records after the fact which visit produced
a reading, so a row written without it can never be corrected. Every existing
row keeps NULL, which correctly means "not recorded".

Revision ID: a5b6c7d8e9f0
Revises: a7b8c9d0e1f2
Create Date: 2026-10-12
"""
from alembic import op
import sqlalchemy as sa

revision = "a5b6c7d8e9f0"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None

TABLE = "clinical_context"
COLUMN = "plan_activity_guid"
INDEX = "ix_clinical_context_plan_activity"


def upgrade():
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return
    if COLUMN not in {c["name"] for c in insp.get_columns(TABLE)}:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(36), nullable=True))
    # Indexed because "every reading from visit X" is the query this column
    # exists to make answerable; grouping a cohort by visit is the point of
    # recording the visit at all.
    if INDEX not in {i["name"] for i in insp.get_indexes(TABLE)}:
        op.create_index(INDEX, TABLE, [COLUMN])


def downgrade():
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return
    if INDEX in {i["name"] for i in insp.get_indexes(TABLE)}:
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in {c["name"] for c in insp.get_columns(TABLE)}:
        op.drop_column(TABLE, COLUMN)
