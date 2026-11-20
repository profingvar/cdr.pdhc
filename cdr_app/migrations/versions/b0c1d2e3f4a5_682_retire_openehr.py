"""#682: retire cdr's openEHR store -- rosetta.pdhc renders openEHR.

Revision ID: b0c1d2e3f4a5
Revises: a9b8c7d6e5f4
Create Date: 2026-11-20

Operator decision 2026-11-20 ("Clip now"): cdr keeps raw + FHIR + canonical;
openEHR is rendered from the canonical observation by rosetta.pdhc (#683).

Measured before this revision, on cdr1/cdr2/cdr3:
  * openehr_compositions: 4 059 / 52 131 / 19 509 rows, every one generated
    by cdr's transformer and mislabelled laboratory_test_result (#496);
  * loinc_archetype_map: 0 rows everywhere (never seeded, never queried);
  * cambio_delivery_log rows of type 'openehr': never delivered
    (CAMBIO_DELIVERY_ENABLED=false on all three);
  * ingest_raw: no producer ever sent an `openehr_composition`.

The data is derived and disposable; take a pg_dump of the three tables before
upgrading. downgrade() recreates the schema EMPTY -- restore the rows from
that dump if they are ever wanted back.
"""
from alembic import op
import sqlalchemy as sa

revision = "b0c1d2e3f4a5"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DELETE FROM cambio_delivery_log WHERE delivery_type = 'openehr'")
    # Dropping the column drops its FK to openehr_compositions with it.
    with op.batch_alter_table("cambio_delivery_log", schema=None) as batch_op:
        batch_op.drop_column("openehr_comp_guid")
    op.execute("DROP TABLE IF EXISTS openehr_compositions")
    op.execute("DROP TABLE IF EXISTS loinc_archetype_map")


def downgrade():
    op.create_table(
        "loinc_archetype_map",
        sa.Column("guid", sa.String(length=36), nullable=False),
        sa.Column("loinc_code", sa.String(length=16), nullable=False),
        sa.Column("loinc_display", sa.String(length=128), nullable=True),
        sa.Column("archetype_id", sa.String(length=128), nullable=True),
        sa.Column("archetype_node_id", sa.String(length=32), nullable=True),
        sa.Column("canonical_metric", sa.String(length=64), nullable=True),
        sa.Column("canonical_unit", sa.String(length=32), nullable=True),
        sa.Column("canonical_table", sa.String(length=64), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("guid"),
    )
    op.create_index("ix_loinc_archetype_map_loinc_code", "loinc_archetype_map", ["loinc_code"])
    op.create_table(
        "openehr_compositions",
        sa.Column("guid", sa.String(length=36), nullable=False),
        sa.Column("ingest_raw_guid", sa.String(length=36), nullable=False),
        sa.Column("fhir_resource_guid", sa.String(length=36), nullable=True),
        sa.Column("patient_guid", sa.String(length=36), nullable=False),
        sa.Column("archetype_id", sa.String(length=128), nullable=True),
        sa.Column("template_id", sa.String(length=128), nullable=True),
        sa.Column("composition_json", sa.JSON(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_service", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["fhir_resource_guid"], ["fhir_resources.guid"]),
        sa.ForeignKeyConstraint(["ingest_raw_guid"], ["ingest_raw.guid"]),
        sa.PrimaryKeyConstraint("guid"),
    )
    op.create_index("ix_openehr_compositions_patient_guid", "openehr_compositions", ["patient_guid"])
    op.create_index("ix_openehr_patient_arch_eff", "openehr_compositions",
                    ["patient_guid", "archetype_id", sa.literal_column("effective_at DESC")])
    with op.batch_alter_table("cambio_delivery_log", schema=None) as batch_op:
        batch_op.add_column(sa.Column("openehr_comp_guid", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("cambio_delivery_log_openehr_comp_guid_fkey",
                                    "openehr_compositions", ["openehr_comp_guid"], ["guid"])
