"""#1054 (record.pdhc stage 2): every history is append-only; ClinicalImpression.

Three things, all additive:

1. `clinical_impression` + `clinical_impression_history` -- the medical
   decision, same shape as every other per-type table, plus status,
   encounter_guid (indexed: "the decisions of this vårdkontakt"), date_at
   and performer_guid.
2. `version_written_at` on all eleven *_history tables: when the superseded
   version was itself written. Nullable, no default, no back-fill (NULL on
   old rows = not recorded, never a guess).
3. The append-only trigger of app/models/history_guard.ddl_for on all eleven
   *_history tables. #984 (e1f2a3b4c5d6) put it on condition and encounter;
   ddl_for is idempotent (CREATE OR REPLACE / DROP IF EXISTS / IF NOT
   EXISTS), so re-running it there changes nothing.

Measured before writing this: no code path or tool UPDATEs or DELETEs a
history row, so the triggers break nothing deployed. A purge of synthetic
data reaching history must set `cdr.allow_history_purge` (history_purge()).

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-11-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None

# Listed literally: a migration describes the schema at THIS revision.
TYPES = [
    "patient", "observation", "questionnaire_response", "condition",
    "medication_statement", "medication_request", "allergy_intolerance",
    "procedure", "encounter", "diagnostic_report", "clinical_impression",
]
#: Guarded by e1f2a3b4c5d6 already; downgrade leaves their triggers alone.
GUARDED_BEFORE = ("condition_history", "encounter_history")
CI = "clinical_impression"


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _tz():
    return sa.DateTime(timezone=True)


def _tail(history: bool) -> list:
    """Context (#769/#1014), review/card (#1044/#1049), then ClinicalImpression's own."""
    cols = [
        sa.Column("ingest_raw_guid", sa.String(36), nullable=True),
        sa.Column("transaction_guid", sa.String(36), nullable=True),
        sa.Column("care_plan_guid", sa.String(36), nullable=True),
        sa.Column("patient_org_guid", sa.String(36), nullable=True),
        sa.Column("data_origin", sa.String(32), nullable=True),
    ]
    if history:
        cols.append(sa.Column("version_written_at", _tz(), nullable=True))
    cols += [
        sa.Column("review_status", sa.String(16), nullable=True),
        sa.Column("review_reason", sa.Text, nullable=True),
        sa.Column("reviewed_by_user_guid", sa.String(36), nullable=True),
        sa.Column("reviewed_by_name", sa.String(255), nullable=True),
        sa.Column("reviewed_at", _tz(), nullable=True),
        sa.Column("endorses_guid", sa.String(36), nullable=True),
        sa.Column("card_section", sa.String(16), nullable=True),
        sa.Column("card_version", sa.String(36), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("encounter_guid", sa.String(36), nullable=True),
        sa.Column("date_at", _tz(), nullable=True),
        sa.Column("performer_guid", sa.String(36), nullable=True),
    ]
    return cols


def _create_clinical_impression(existing: set) -> None:
    if CI not in existing:
        op.create_table(
            CI,
            sa.Column("guid", sa.String(36), primary_key=True),
            sa.Column("patient_guid", sa.String(36), nullable=True),
            sa.Column("org_guid", sa.String(36), nullable=True),
            sa.Column("code_canonical", sa.String(512), nullable=True),
            sa.Column("effective_at", _tz(), nullable=True),
            sa.Column("raw_json", _json(), nullable=False),
            sa.Column("source", sa.String(64), nullable=True),
            sa.Column("source_request_id", sa.String(64), nullable=True),
            sa.Column("meta_tag", _json(), nullable=True),
            sa.Column("version_id", sa.Integer, nullable=False),
            sa.Column("sync_group_id", sa.String(36), nullable=True),
            sa.Column("mapping_version", sa.String(64), nullable=True),
            sa.Column("etag", sa.String(64), nullable=True),
            sa.Column("received_at", _tz(), nullable=False),
            sa.Column("created_at", _tz(), nullable=False),
            sa.Column("updated_at", _tz(), nullable=False),
            *_tail(history=False),
        )
        for name, cols in (
            ("pat_org_code_eff", ["patient_guid", "org_guid", "code_canonical", "effective_at"]),
            ("sync_group", ["sync_group_id"]), ("org", ["org_guid"]),
            ("code", ["code_canonical"]), ("ingest_raw", ["ingest_raw_guid"]),
            ("patient_org", ["patient_org_guid"]), ("review_status", ["review_status"]),
            ("endorses", ["endorses_guid"]), ("card_version", ["card_version"]),
            ("encounter_guid", ["encounter_guid"]),
        ):
            op.create_index(f"ix_{CI}_{name}", CI, cols)
    h = f"{CI}_history"
    if h not in existing:
        op.create_table(
            h,
            sa.Column("guid", sa.String(36), nullable=False),
            sa.Column("version_id", sa.Integer, nullable=False),
            sa.Column("patient_guid", sa.String(36), nullable=True),
            sa.Column("org_guid", sa.String(36), nullable=True),
            sa.Column("code_canonical", sa.String(512), nullable=True),
            sa.Column("effective_at", _tz(), nullable=True),
            sa.Column("raw_json", _json(), nullable=False),
            sa.Column("source", sa.String(64), nullable=True),
            sa.Column("source_request_id", sa.String(64), nullable=True),
            sa.Column("meta_tag", _json(), nullable=True),
            sa.Column("sync_group_id", sa.String(36), nullable=True),
            sa.Column("mapping_version", sa.String(64), nullable=True),
            sa.Column("etag", sa.String(64), nullable=True),
            sa.Column("superseded_at", _tz(), nullable=False),
            sa.Column("superseded_by_request_id", sa.String(64), nullable=True),
            *_tail(history=True),
            sa.PrimaryKeyConstraint("guid", "version_id"),
        )
        op.create_index(f"ix_{h}_guid", h, ["guid"])
        op.create_index(f"ix_{h}_org", h, ["org_guid"])


def upgrade():
    from app.models.history_guard import ddl_for
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    _create_clinical_impression(existing)
    insp = sa.inspect(bind)
    for t in TYPES:
        h = f"{t}_history"
        if h not in set(insp.get_table_names()):
            continue
        if "version_written_at" not in {c["name"] for c in insp.get_columns(h)}:
            op.add_column(h, sa.Column("version_written_at", _tz(), nullable=True))
        for stmt in ddl_for(h, bind.dialect.name):
            op.execute(sa.text(stmt))


def _drop_trigger(h: str, dialect: str) -> None:
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {h}_append_only_trg ON {h}"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {h}_append_only()"))
    elif dialect == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {h}_no_update"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {h}_no_delete"))


def downgrade():
    """Back to e6f7a8b9c0d1: condition/encounter stay guarded (#984);
    ClinicalImpression and its history are DROPPED -- take a pg_dump first."""
    bind = op.get_bind()
    dialect = bind.dialect.name
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    for t in TYPES:
        h = f"{t}_history"
        if h not in existing or t == CI:
            continue
        if h not in GUARDED_BEFORE:
            _drop_trigger(h, dialect)
        if "version_written_at" in {c["name"] for c in insp.get_columns(h)}:
            if dialect == "sqlite":
                # SQLite rebuilds the table to drop a column, and a trigger
                # on it would block the copy; re-add the #984 guard after.
                _drop_trigger(h, dialect)
                with op.batch_alter_table(h) as b:
                    b.drop_column("version_written_at")
                if h in GUARDED_BEFORE:
                    from app.models.history_guard import ddl_for
                    for stmt in ddl_for(h, dialect):
                        op.execute(sa.text(stmt))
            else:
                op.drop_column(h, "version_written_at")
    for t in (f"{CI}_history", CI):
        if t in existing:
            if t.endswith("_history"):
                _drop_trigger(t, dialect)
            op.drop_table(t)
