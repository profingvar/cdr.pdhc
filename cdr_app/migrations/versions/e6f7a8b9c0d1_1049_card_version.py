"""#1049: the patient card ("Mitt patientkort") answer's section and version.

Two columns on every per-type live table and its history, beside the #1044
review columns (the same TABLES as d5e6f7a8b9c0):

* card_section   allergy | medication | history | vaccination | contact
* card_version   the uuid the patient app minted for ONE send of ONE
                 section; decided as a whole (contract s7.4)

`review_status` gains the value `superseded` (a newer pending version of the
same section replaced this one). It is a String(16), so the value needs no
schema change; it is named here so the migration history says when it
arrived.

Additive only: both columns nullable with no default, so Postgres adds them
as metadata and rewrites no table; existing rows read NULL = "not a card
answer", which is true of all of them. One index per live table, on
card_version, built over those NULLs (a scan, no rewrite).

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-11-22
"""
from alembic import op
import sqlalchemy as sa

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None

# Listed literally: a migration describes the schema at THIS revision.
TABLES = [
    "patient", "observation", "questionnaire_response", "condition",
    "medication_statement", "medication_request", "allergy_intolerance",
    "procedure", "encounter", "diagnostic_report",
]


def _columns():
    return [
        sa.Column("card_section", sa.String(16), nullable=True),
        sa.Column("card_version", sa.String(36), nullable=True),
    ]


def _indexes(table):
    return [(f"ix_{table}_card_version", "card_version")]


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
