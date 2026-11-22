"""#1044: review of patient-reported answers, and the endorsement row.

Six columns on every per-type live table and its history (the same set
c1d4e7f0a2b5 put data_origin on, minus clinical_context):

* review_status          NULL = not subject to review; pending | endorsed |
                         rejected, set on the ORIGINAL
* review_reason          text; required on a rejection
* reviewed_by_user_guid, reviewed_by_name, reviewed_at
                         who decided (on the original); the author (on the
                         endorsement row)
* endorses_guid          on the ENDORSEMENT row: the original's guid

clinical_context is deliberately NOT touched: the status changes once, and a
second copy of a value that changes is a copy that drifts. The untouched
submission is ingest_raw.payload_json, as for every other ingest.

Additive only. Every column is nullable with no default, so Postgres adds
them as metadata and rewrites no table; existing rows read NULL = "not
subject to review", which is true of all of them. The two indexes per live
table are built over those NULLs (a scan, no rewrite).

Revision ID: d5e6f7a8b9c0
Revises: c1d4e7f0a2b5
Create Date: 2026-11-22
"""
from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "c1d4e7f0a2b5"
branch_labels = None
depends_on = None

# Listed literally, as in c1d4e7f0a2b5: a migration describes the schema at
# THIS revision, not whatever the model says when it is next run.
TABLES = [
    "patient", "observation", "questionnaire_response", "condition",
    "medication_statement", "medication_request", "allergy_intolerance",
    "procedure", "encounter", "diagnostic_report",
]


def _columns():
    return [
        sa.Column("review_status", sa.String(16), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_by_user_guid", sa.String(36), nullable=True),
        sa.Column("reviewed_by_name", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("endorses_guid", sa.String(36), nullable=True),
    ]


def _indexes(table):
    return [(f"ix_{table}_review_status", "review_status"),
            (f"ix_{table}_endorses", "endorses_guid")]


def upgrade():
    insp = sa.inspect(op.get_bind())
    existing = set(insp.get_table_names())
    for live in TABLES:
        for table in (live, f"{live}_history"):
            if table not in existing:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for col in _columns():
                if col.name not in have:
                    op.add_column(table, col)
        if live in existing:
            have_ix = {i["name"] for i in insp.get_indexes(live)}
            for name, column in _indexes(live):
                if name not in have_ix:
                    op.create_index(name, live, [column])


def downgrade():
    insp = sa.inspect(op.get_bind())
    existing = set(insp.get_table_names())
    for live in TABLES:
        if live in existing:
            have_ix = {i["name"] for i in insp.get_indexes(live)}
            for name, _ in _indexes(live):
                if name in have_ix:
                    op.drop_index(name, table_name=live)
        for table in (live, f"{live}_history"):
            if table not in existing:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for col in _columns():
                if col.name in have:
                    op.drop_column(table, col.name)
