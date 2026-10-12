"""#817: which sim generation run produced a row, on cdr1/cdr2/cdr3.

`source_service` already records that `sim.pdhc` WROTE a row. This records
WHICH RUN, which is a different and stronger claim:

* `source_service = sim.pdhc`  — identifies the **writer**
* `sim_run_id = sim-2026…-0000002a` — identifies the **generation**

cdr_6 has had this since it was built, and it is what makes a cohort there
purgeable as a unit (`DELETE /api/v1/runs/<id>`). A cohort promoted out of
cdr_6 into a working CDR kept its writer and lost its run identity, so there
was no way to say "these 300 rows came from that one run" — or to remove them
again without matching on effective dates and hoping.

## Why `clinical_context` and not `observation`

Because that is where the rest of the provenance already lives
(`contract_guid`, `plan_definition_guid`, `plan_activity_guid`,
`provider_org_guid`, `source_service`), and keeping one row's provenance in
one place is the thing #769 argued for. `observation.ingest_raw_guid` joins to
`clinical_context.ingest_raw_guid` — #769 records that column as THE join to
provenance — so "every observation from run X" remains a single join away.

## String(64), not String(36)

A sim run id is not a GUID. It is `sim-<ISO-timestamp>-<8 hex>`, e.g.
`sim-20261012T055851Z-00000005` — 29 characters today, and
`synthea-20260603T141456Z-fd45c6` is another live shape. 36 would fit both and
leaves no room for a longer prefix; 64 matches `source_service` beside it.

## Applied to cdr1, cdr2 and cdr3

Their application code diverges (cdr2/cdr3 lack cdr1's `clinical_read` module
and, until 2026-10-12, #665's `author_org_guid` declaration) but their SCHEMAS
are identical, verified column-by-column. One additive migration serves all
three.

Nullable, no backfill: rows written before this cannot be attributed to a run,
and guessing from timestamps would invent provenance. NULL correctly means
"not recorded" — and for anything gateway wrote, it means "not from sim",
which is the honest answer.

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-10-12
"""
from alembic import op
import sqlalchemy as sa

revision = "b6c7d8e9f0a1"
down_revision = "a5b6c7d8e9f0"
branch_labels = None
depends_on = None

TABLE = "clinical_context"
COLUMN = "sim_run_id"
INDEX = "ix_clinical_context_sim_run"


def upgrade():
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return
    if COLUMN not in {c["name"] for c in insp.get_columns(TABLE)}:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(64), nullable=True))
    # Indexed because the queries this column exists for are both lookups by
    # run: "show me that cohort" and "remove that cohort".
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
