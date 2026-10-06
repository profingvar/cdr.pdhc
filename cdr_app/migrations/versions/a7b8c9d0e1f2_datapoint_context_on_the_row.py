"""#769 — put each datapoint's context ON the row.

`clinical_context` is keyed on `ingest_raw_guid`, while the per-type live
tables key on the FHIR resource id. The two could not be joined:

    observation.guid = clinical_context.ingest_raw_guid  ->      0 rows
    ingest_raw.guid  = clinical_context.ingest_raw_guid  ->  7,416 rows

So cdr1 held 7,415 transaction GUIDs belonging to no reachable datapoint. A side
table that cannot be joined is worse than a missing column, because it looks
populated. `ingest_raw_guid` on the row is the repair — the pipeline already had
that value in scope and discarded it.

All four columns are NULLABLE. The CDR contents are flushed alongside this
migration (#769, operator-authorised), so there are no legacy rows to excuse —
but `patient_org_guid` cannot be NOT NULL yet: its resolution path is #774,
blocked on #778, so requiring it would reject every ingest today. Tighten when
#775's conformance test can prove it is always set.

Revision ID: a7b8c9d0e1f2
Revises: b5c6d7e8f9a0
"""
from alembic import op
import sqlalchemy as sa

revision = "a7b8c9d0e1f2"
# b5c6d7e8f9a0 is the single head AND the revision live on cdr1,
# cdr2 and cdr3. An earlier draft chained onto f4a5b6c7d8e9 — which
# looked like the head because a `grep | tail` listed files
# alphabetically rather than following the graph — and that forked the
# chain in two.
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None

# The per-type tables, from app/models/resources.py RESOURCES. Listed literally
# rather than imported: a migration must describe the schema at THIS revision,
# not whatever the model happens to say when it is next run.
TABLES = [
    "patient", "observation", "questionnaire_response", "condition",
    "medication_statement", "medication_request", "allergy_intolerance",
    "procedure", "encounter", "diagnostic_report",
]

COLUMNS = [
    ("ingest_raw_guid", 36),
    ("transaction_guid", 36),
    ("care_plan_guid", 36),
    ("patient_org_guid", 36),
]


def _targets():
    for t in TABLES:
        yield t, True          # live table — gets indexes
        yield f"{t}_history", False


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    for table, is_live in _targets():
        if table not in existing:
            # cdr2/cdr3 are schema-identical to cdr1 today, but a migration
            # that assumes a table exists fails the whole deploy rather than
            # skipping one resource type.
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, length in COLUMNS:
            if name not in have:
                op.add_column(table, sa.Column(name, sa.String(length),
                                               nullable=True))
        if is_live:
            idx = {i["name"] for i in insp.get_indexes(table)}
            for col, suffix in (("ingest_raw_guid", "ingest_raw"),
                                ("patient_org_guid", "patient_org")):
                n = f"ix_{table}_{suffix}"
                if n not in idx:
                    op.create_index(n, table, [col])


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    for table, is_live in _targets():
        if table not in existing:
            continue
        if is_live:
            idx = {i["name"] for i in insp.get_indexes(table)}
            for suffix in ("ingest_raw", "patient_org"):
                n = f"ix_{table}_{suffix}"
                if n in idx:
                    op.drop_index(n, table_name=table)
        have = {c["name"] for c in insp.get_columns(table)}
        for name, _ in COLUMNS:
            if name in have:
                op.drop_column(table, name)
