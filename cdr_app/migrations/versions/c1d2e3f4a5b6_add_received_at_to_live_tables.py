"""#294 RFC E1: add received_at to all Live tables, backfill from created_at.

Three time concepts now distinguished consistently across the live
layer:

  - effective_at — when the measurement actually occurred (clinical).
  - received_at  — when the platform first saw the payload (= IngestRaw.received_at).
  - created_at   — when this specific row was written.

Before this migration, the cdr.pdhc Live tables only carried
``effective_at`` + ``created_at``. The canonical clinical-context
schema (plans/pdhc_clinical_context_harmonisation_plan.md §3) lists
``received_at`` as a required field. This migration closes that gap.

Tables updated:

  - fhir_resources, openehr_compositions, health_observations, activities
    (Layer 2 + Layer 3, defined in app.models.__init__)
  - per-type FHIR live tables: patient, observation, questionnaire_response,
    condition, medication_statement, medication_request, allergy_intolerance,
    procedure, encounter, diagnostic_report
    (defined dynamically via app.models.resources._common_live_columns)

For each table the column is added nullable, backfilled from
``created_at`` (rows already on disk had no separate ingest-boundary
timestamp; ``created_at`` is the best available proxy), then altered
to NOT NULL with default now().

History tables intentionally do NOT get received_at — they track
revisions of the row, not ingest events.

Revision ID: c1d2e3f4a5b6
Revises: 2b6d8e6624ce
Create Date: 2026-06-28
"""
from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "2b6d8e6624ce"
branch_labels = None
depends_on = None


LIVE_TABLES = [
    # Layer 2 + Layer 3 (app.models.__init__)
    "fhir_resources",
    "openehr_compositions",
    "health_observations",
    "activities",
    # Per-type FHIR live tables (app.models.resources)
    "patient",
    "observation",
    "questionnaire_response",
    "condition",
    "medication_statement",
    "medication_request",
    "allergy_intolerance",
    "procedure",
    "encounter",
    "diagnostic_report",
]


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for table in LIVE_TABLES:
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "received_at" in cols:
            continue
        op.add_column(
            table,
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.execute(
            f"UPDATE {table} SET received_at = created_at "
            f"WHERE received_at IS NULL"
        )
        op.alter_column(
            table, "received_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for table in LIVE_TABLES:
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "received_at" not in cols:
            continue
        op.drop_column(table, "received_at")
