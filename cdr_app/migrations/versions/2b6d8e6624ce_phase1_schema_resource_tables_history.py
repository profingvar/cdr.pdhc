"""Phase 1 schema — FHIR per-type tables + *_history + sync_group + plan-miss audit + change_feed.

Adds the platform-plan §1.1 schema. The legacy generic ``fhir_resources``
table stays in place for backwards compat with the pre-platform ingest
path; new ingest writes into the per-type tables.

Per-type tables created (one live + one history each):

    patient, observation, questionnaire_response, condition,
    medication_statement, medication_request, allergy_intolerance,
    procedure, encounter, diagnostic_report

Cross-cutting tables created:

    sync_group, cdr_audit_plan_miss, change_feed

Revision ID: 2b6d8e6624ce
Revises: 8aa2748e0139
Create Date: 2026-04-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "2b6d8e6624ce"
down_revision = "8aa2748e0139"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Layout shared across resources
# ---------------------------------------------------------------------------

# (table_name, fhir_resource_type, extra_live_columns_factory, extra_history_columns_factory,
#  patient_guid_nullable)
RESOURCES = [
    ("patient", "Patient", "patient", True),
    ("observation", "Observation", "observation", False),
    ("questionnaire_response", "QuestionnaireResponse", "qr", False),
    ("condition", "Condition", "condition", False),
    ("medication_statement", "MedicationStatement", "medstmt", False),
    ("medication_request", "MedicationRequest", "medreq", False),
    ("allergy_intolerance", "AllergyIntolerance", "allergy", False),
    ("procedure", "Procedure", "procedure", False),
    ("encounter", "Encounter", "encounter", False),
    ("diagnostic_report", "DiagnosticReport", "dxreport", False),
]


def _common_live(patient_guid_nullable: bool) -> list:
    return [
        sa.Column("guid", sa.String(36), primary_key=True),
        sa.Column("patient_guid", sa.String(36), nullable=patient_guid_nullable),
        sa.Column("org_guid", sa.String(36), nullable=False),
        sa.Column("code_canonical", sa.String(512), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("source_request_id", sa.String(64), nullable=True),
        sa.Column("meta_tag", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("version_id", sa.Integer, nullable=False),
        sa.Column("sync_group_id", sa.String(36), nullable=True),
        sa.Column("mapping_version", sa.String(64), nullable=True),
        sa.Column("etag", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _common_history() -> list:
    return [
        sa.Column("guid", sa.String(36), nullable=False),
        sa.Column("version_id", sa.Integer, nullable=False),
        sa.Column("patient_guid", sa.String(36), nullable=True),
        sa.Column("org_guid", sa.String(36), nullable=False),
        sa.Column("code_canonical", sa.String(512), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("source_request_id", sa.String(64), nullable=True),
        sa.Column("meta_tag", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sync_group_id", sa.String(36), nullable=True),
        sa.Column("mapping_version", sa.String(64), nullable=True),
        sa.Column("etag", sa.String(64), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_by_request_id", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("guid", "version_id"),
    ]


def _extra_live(kind: str) -> list:
    if kind == "patient":
        return [
            sa.Column("identifiers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("names", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("gender", sa.String(16), nullable=True),
            sa.Column("birth_date", sa.Date, nullable=True),
            sa.Column("active", sa.Boolean, nullable=False),
        ]
    if kind == "observation":
        return [
            sa.Column("value_quantity", sa.Numeric(18, 6), nullable=True),
            sa.Column("value_unit", sa.String(64), nullable=True),
            sa.Column("value_string", sa.String(512), nullable=True),
            sa.Column("value_code", sa.String(128), nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
        ]
    if kind == "qr":
        return [
            sa.Column("questionnaire_canonical", sa.String(512), nullable=True),
            sa.Column("authored_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
        ]
    if kind == "condition":
        return [
            sa.Column("onset_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("abatement_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("clinical_status", sa.String(32), nullable=True),
            sa.Column("verification_status", sa.String(32), nullable=True),
        ]
    if kind == "medstmt":
        return [
            sa.Column("medication_canonical", sa.String(512), nullable=True),
            sa.Column("effective_period_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("effective_period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
        ]
    if kind == "medreq":
        return [
            sa.Column("medication_canonical", sa.String(512), nullable=True),
            sa.Column("authored_on", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
            sa.Column("intent", sa.String(32), nullable=True),
        ]
    if kind == "allergy":
        return [
            sa.Column("type", sa.String(32), nullable=True),
            sa.Column("category", sa.String(32), nullable=True),
            sa.Column("criticality", sa.String(32), nullable=True),
            sa.Column("clinical_status", sa.String(32), nullable=True),
        ]
    if kind == "procedure":
        return [
            sa.Column("performed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
        ]
    if kind == "encounter":
        return [
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
            sa.Column("class_code", sa.String(32), nullable=True),
        ]
    if kind == "dxreport":
        return [
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(32), nullable=True),
            sa.Column("conclusion", sa.Text, nullable=True),
        ]
    return []


def _extra_history(kind: str) -> list:
    cols = _extra_live(kind)
    # In history table, all extras are nullable (a row's prior version may
    # have lacked the value that's now non-null).
    out = []
    for c in cols:
        out.append(sa.Column(c.name, c.type, nullable=True))
    return out


# ---------------------------------------------------------------------------
# Upgrade / downgrade
# ---------------------------------------------------------------------------

def upgrade():
    # ---- per-type live + history tables ----
    for table_name, _fhir, kind, patient_nullable in RESOURCES:
        op.create_table(
            table_name,
            *_common_live(patient_nullable),
            *_extra_live(kind),
        )
        op.create_index(
            f"ix_{table_name}_pat_org_code_eff",
            table_name,
            ["patient_guid", "org_guid", "code_canonical", "effective_at"],
            unique=False,
        )
        op.create_index(f"ix_{table_name}_sync_group", table_name, ["sync_group_id"])
        op.create_index(f"ix_{table_name}_org", table_name, ["org_guid"])
        op.create_index(f"ix_{table_name}_code", table_name, ["code_canonical"])

        hist_table = f"{table_name}_history"
        op.create_table(
            hist_table,
            *_common_history(),
            *_extra_history(kind),
        )
        op.create_index(f"ix_{hist_table}_guid", hist_table, ["guid"])
        op.create_index(f"ix_{hist_table}_org", hist_table, ["org_guid"])

    # ---- sync_group ----
    op.create_table(
        "sync_group",
        sa.Column("sync_group_id", sa.String(36), primary_key=True),
        sa.Column("origin_api", sa.String(16), nullable=False),
        sa.Column("composition_guid", sa.String(36), nullable=True),
        sa.Column("fhir_resource_guids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mapping_version", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ---- cdr_audit_plan_miss ----
    op.create_table(
        "cdr_audit_plan_miss",
        sa.Column("guid", sa.String(36), primary_key=True),
        sa.Column("canonical_uri", sa.String(512), nullable=False),
        sa.Column("canonical_lib_name", sa.String(128), nullable=True),
        sa.Column("canonical_refnumber", sa.String(128), nullable=True),
        sa.Column("seen_count", sa.Integer, nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_request_id", sa.String(64), nullable=True),
        sa.UniqueConstraint("canonical_uri", name="uq_plan_miss_canonical"),
    )

    # ---- change_feed ----
    op.create_table(
        "change_feed",
        sa.Column("seq", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_guid", sa.String(36), nullable=False),
        sa.Column("patient_guid", sa.String(36), nullable=True),
        sa.Column("org_guid", sa.String(36), nullable=False),
        sa.Column("version_id", sa.Integer, nullable=False),
        sa.Column("sync_group_id", sa.String(36), nullable=True),
        sa.Column("code_canonical", sa.String(512), nullable=True),
        sa.Column("source_request_id", sa.String(64), nullable=True),
        sa.Column("payload_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_change_feed_seq", "change_feed", ["seq"])
    op.create_index("ix_change_feed_type", "change_feed", ["resource_type"])
    op.create_index("ix_change_feed_org", "change_feed", ["org_guid"])
    op.create_index("ix_change_feed_pat", "change_feed", ["patient_guid"])
    op.create_index("ix_change_feed_occurred", "change_feed", ["occurred_at"])


def downgrade():
    op.drop_index("ix_change_feed_occurred", table_name="change_feed")
    op.drop_index("ix_change_feed_pat", table_name="change_feed")
    op.drop_index("ix_change_feed_org", table_name="change_feed")
    op.drop_index("ix_change_feed_type", table_name="change_feed")
    op.drop_index("ix_change_feed_seq", table_name="change_feed")
    op.drop_table("change_feed")

    op.drop_table("cdr_audit_plan_miss")
    op.drop_table("sync_group")

    for table_name, _fhir, _kind, _patient_nullable in RESOURCES:
        hist_table = f"{table_name}_history"
        op.drop_index(f"ix_{hist_table}_org", table_name=hist_table)
        op.drop_index(f"ix_{hist_table}_guid", table_name=hist_table)
        op.drop_table(hist_table)

        op.drop_index(f"ix_{table_name}_code", table_name=table_name)
        op.drop_index(f"ix_{table_name}_org", table_name=table_name)
        op.drop_index(f"ix_{table_name}_sync_group", table_name=table_name)
        op.drop_index(f"ix_{table_name}_pat_org_code_eff", table_name=table_name)
        op.drop_table(table_name)
