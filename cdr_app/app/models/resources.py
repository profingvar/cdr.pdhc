"""FHIR per-resource-type tables for the CDR canonical store.

Implements platform-plan execution §1.1: each FHIR resource type the CDR
holds gets its own table, plus a matching ``*_history`` table for
versioning, plus the cross-cutting ``sync_group``, ``cdr_audit_plan_miss``,
and ``change_feed`` tables.

Common columns on every per-type live table (§1.1.b):

    guid, patient_guid, org_guid, code_canonical, effective_at, raw_json,
    source, source_request_id, meta_tag, version_id, sync_group_id,
    mapping_version, etag, created_at, updated_at

History tables carry the same columns plus ``superseded_at`` and the
``superseded_by_request_id`` of the write that pushed the row to history.

These tables are additive — the legacy ``fhir_resources`` generic table
(see ``app/models/__init__.py``) stays in place for backwards compat
with the pre-platform-plan ingest path. New ingest (Phase 1.2) will
write into the per-type tables; the generic table is read-only legacy.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Text
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

from app import db


# Dialect-aware JSON: JSONB on Postgres (production), JSON on SQLite (tests).
# Lets us keep one ORM definition that works in both environments without
# the in-memory SQLite test fixture choking on a Postgres-only type.
JSONType = JSON().with_variant(_PG_JSONB(astext_type=Text()), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Common column factories — used by both live and history tables
# ---------------------------------------------------------------------------

def _common_live_columns() -> list:
    """Columns shared by every live FHIR per-type table."""
    return [
        db.Column("guid", db.String(36), primary_key=True, default=_uuid),
        db.Column("patient_guid", db.String(36), nullable=True),  # null on Patient
        db.Column("org_guid", db.String(36), nullable=False),
        db.Column("code_canonical", db.String(512), nullable=True),
        db.Column("effective_at", db.DateTime(timezone=True), nullable=True),
        db.Column("raw_json", JSONType, nullable=False),
        db.Column("source", db.String(64), nullable=True),
        db.Column("source_request_id", db.String(64), nullable=True),
        db.Column("meta_tag", JSONType, nullable=True),
        db.Column("version_id", db.Integer, nullable=False, default=1),
        db.Column("sync_group_id", db.String(36), nullable=True),
        db.Column("mapping_version", db.String(64), nullable=True),
        db.Column("etag", db.String(64), nullable=True),
        db.Column("created_at", db.DateTime(timezone=True), default=_now, nullable=False),
        db.Column("updated_at", db.DateTime(timezone=True), default=_now, onupdate=_now, nullable=False),
    ]


def _common_history_columns() -> list:
    """Columns shared by every *_history table.

    Note: ``guid`` is NOT a primary key in the history table — multiple
    versions of the same resource share a guid, distinguished by
    ``version_id``. PK is the composite ``(guid, version_id)``.
    """
    return [
        db.Column("guid", db.String(36), nullable=False),
        db.Column("version_id", db.Integer, nullable=False),
        db.Column("patient_guid", db.String(36), nullable=True),
        db.Column("org_guid", db.String(36), nullable=False),
        db.Column("code_canonical", db.String(512), nullable=True),
        db.Column("effective_at", db.DateTime(timezone=True), nullable=True),
        db.Column("raw_json", JSONType, nullable=False),
        db.Column("source", db.String(64), nullable=True),
        db.Column("source_request_id", db.String(64), nullable=True),
        db.Column("meta_tag", JSONType, nullable=True),
        db.Column("sync_group_id", db.String(36), nullable=True),
        db.Column("mapping_version", db.String(64), nullable=True),
        db.Column("etag", db.String(64), nullable=True),
        db.Column("superseded_at", db.DateTime(timezone=True), default=_now, nullable=False),
        db.Column("superseded_by_request_id", db.String(64), nullable=True),
        db.PrimaryKeyConstraint("guid", "version_id", name=None),
    ]


# Resource types managed by this module. Each entry produces:
#   - a live table  ``<table>``
#   - a history table ``<table>_history``
#   - the per-type extra columns (if any)
#
# Tuple shape: (singular_name, table_name, extra_live_columns, extra_history_columns)
RESOURCES = [
    (
        "Patient",
        "patient",
        # Patient is FHIR-shaped: identifier[], name[], gender, birthDate, active.
        # Stored on top of the common columns. Patient has no patient_guid
        # (it IS the patient); patient_guid is left null and the resource's
        # own guid acts as the patient identifier elsewhere.
        [
            db.Column("identifiers", JSONType, nullable=True),
            db.Column("names", JSONType, nullable=True),
            db.Column("gender", db.String(16), nullable=True),
            db.Column("birth_date", db.Date, nullable=True),
            db.Column("active", db.Boolean, nullable=False, default=True),
        ],
        [
            db.Column("identifiers", JSONType, nullable=True),
            db.Column("names", JSONType, nullable=True),
            db.Column("gender", db.String(16), nullable=True),
            db.Column("birth_date", db.Date, nullable=True),
            db.Column("active", db.Boolean, nullable=True),
        ],
    ),
    (
        "Observation",
        "observation",
        [
            db.Column("value_quantity", db.Numeric(18, 6), nullable=True),
            db.Column("value_unit", db.String(64), nullable=True),
            db.Column("value_string", db.String(512), nullable=True),
            db.Column("value_code", db.String(128), nullable=True),
            db.Column("status", db.String(32), nullable=True),
        ],
        [
            db.Column("value_quantity", db.Numeric(18, 6), nullable=True),
            db.Column("value_unit", db.String(64), nullable=True),
            db.Column("value_string", db.String(512), nullable=True),
            db.Column("value_code", db.String(128), nullable=True),
            db.Column("status", db.String(32), nullable=True),
        ],
    ),
    (
        "QuestionnaireResponse",
        "questionnaire_response",
        [
            db.Column("questionnaire_canonical", db.String(512), nullable=True),
            db.Column("authored_at", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
        ],
        [
            db.Column("questionnaire_canonical", db.String(512), nullable=True),
            db.Column("authored_at", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
        ],
    ),
    (
        "Condition",
        "condition",
        [
            db.Column("onset_at", db.DateTime(timezone=True), nullable=True),
            db.Column("abatement_at", db.DateTime(timezone=True), nullable=True),
            db.Column("clinical_status", db.String(32), nullable=True),
            db.Column("verification_status", db.String(32), nullable=True),
        ],
        [
            db.Column("onset_at", db.DateTime(timezone=True), nullable=True),
            db.Column("abatement_at", db.DateTime(timezone=True), nullable=True),
            db.Column("clinical_status", db.String(32), nullable=True),
            db.Column("verification_status", db.String(32), nullable=True),
        ],
    ),
    (
        "MedicationStatement",
        "medication_statement",
        [
            db.Column("medication_canonical", db.String(512), nullable=True),
            db.Column("effective_period_start", db.DateTime(timezone=True), nullable=True),
            db.Column("effective_period_end", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
        ],
        [
            db.Column("medication_canonical", db.String(512), nullable=True),
            db.Column("effective_period_start", db.DateTime(timezone=True), nullable=True),
            db.Column("effective_period_end", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
        ],
    ),
    (
        "MedicationRequest",
        "medication_request",
        [
            db.Column("medication_canonical", db.String(512), nullable=True),
            db.Column("authored_on", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
            db.Column("intent", db.String(32), nullable=True),
        ],
        [
            db.Column("medication_canonical", db.String(512), nullable=True),
            db.Column("authored_on", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
            db.Column("intent", db.String(32), nullable=True),
        ],
    ),
    (
        "AllergyIntolerance",
        "allergy_intolerance",
        [
            db.Column("type", db.String(32), nullable=True),
            db.Column("category", db.String(32), nullable=True),
            db.Column("criticality", db.String(32), nullable=True),
            db.Column("clinical_status", db.String(32), nullable=True),
        ],
        [
            db.Column("type", db.String(32), nullable=True),
            db.Column("category", db.String(32), nullable=True),
            db.Column("criticality", db.String(32), nullable=True),
            db.Column("clinical_status", db.String(32), nullable=True),
        ],
    ),
    (
        "Procedure",
        "procedure",
        [
            db.Column("performed_at", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
        ],
        [
            db.Column("performed_at", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
        ],
    ),
    (
        "Encounter",
        "encounter",
        [
            db.Column("period_start", db.DateTime(timezone=True), nullable=True),
            db.Column("period_end", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
            db.Column("class_code", db.String(32), nullable=True),
        ],
        [
            db.Column("period_start", db.DateTime(timezone=True), nullable=True),
            db.Column("period_end", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
            db.Column("class_code", db.String(32), nullable=True),
        ],
    ),
    (
        "DiagnosticReport",
        "diagnostic_report",
        [
            db.Column("issued_at", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
            db.Column("conclusion", db.Text, nullable=True),
        ],
        [
            db.Column("issued_at", db.DateTime(timezone=True), nullable=True),
            db.Column("status", db.String(32), nullable=True),
            db.Column("conclusion", db.Text, nullable=True),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Build SQLAlchemy ORM classes dynamically from the RESOURCES table.
# We could declare 9 ORM classes by hand, but the boilerplate is
# substantial and identical. Building them through ``type()`` keeps the
# table list (above) the single source of truth.
# ---------------------------------------------------------------------------

_RESOURCE_MODELS: dict[str, type] = {}
_HISTORY_MODELS: dict[str, type] = {}


def _build_models():
    for fhir_name, table_name, extra_live, extra_hist in RESOURCES:
        # ----- live table ----------------------------------------------------
        live_cols = _common_live_columns() + list(extra_live)
        live_cols.append(
            db.Index(
                f"ix_{table_name}_pat_org_code_eff",
                "patient_guid", "org_guid", "code_canonical", "effective_at",
            )
        )
        live_cols.append(db.Index(f"ix_{table_name}_sync_group", "sync_group_id"))
        live_cols.append(db.Index(f"ix_{table_name}_org", "org_guid"))
        live_cols.append(db.Index(f"ix_{table_name}_code", "code_canonical"))

        live_cls = type(
            fhir_name,
            (db.Model,),
            {
                "__tablename__": table_name,
                "__table_args__": tuple(c for c in live_cols if not isinstance(c, db.Column)),
                **{
                    c.name: c
                    for c in live_cols
                    if isinstance(c, db.Column)
                },
            },
        )
        _RESOURCE_MODELS[fhir_name] = live_cls
        globals()[fhir_name] = live_cls

        # ----- history table -------------------------------------------------
        hist_table = f"{table_name}_history"
        hist_cols = _common_history_columns() + list(extra_hist)
        hist_cols.append(db.Index(f"ix_{hist_table}_guid", "guid"))
        hist_cols.append(db.Index(f"ix_{hist_table}_org", "org_guid"))

        hist_cls_name = f"{fhir_name}History"
        hist_cls = type(
            hist_cls_name,
            (db.Model,),
            {
                "__tablename__": hist_table,
                "__table_args__": tuple(c for c in hist_cols if not isinstance(c, db.Column)),
                **{
                    c.name: c
                    for c in hist_cols
                    if isinstance(c, db.Column)
                },
            },
        )
        _HISTORY_MODELS[fhir_name] = hist_cls
        globals()[hist_cls_name] = hist_cls


_build_models()


def live_model(fhir_resource_type: str):
    """Return the ORM class for a FHIR resource type string ('Observation' etc)."""
    return _RESOURCE_MODELS.get(fhir_resource_type)


def history_model(fhir_resource_type: str):
    return _HISTORY_MODELS.get(fhir_resource_type)


# ---------------------------------------------------------------------------
# Cross-cutting: sync_group, cdr_audit_plan_miss, change_feed
# ---------------------------------------------------------------------------

class SyncGroup(db.Model):
    """Links FHIR and openEHR representations of the same clinical fact.

    One row per logical clinical fact. The openEHR side may be a placeholder
    today (bidirectional mapping deferred); the ID is minted now so the
    wiring is ready. Per execution plan §1.1.g.
    """
    __tablename__ = "sync_group"

    sync_group_id = db.Column(db.String(36), primary_key=True, default=_uuid)
    origin_api = db.Column(db.String(16), nullable=False)  # 'fhir' | 'openehr'
    composition_guid = db.Column(db.String(36), nullable=True)
    fhir_resource_guids = db.Column(JSONType, nullable=True)  # array of guids
    mapping_version = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)


class CdrAuditPlanMiss(db.Model):
    """A canonical URI returned by xlate.pdhc that is not referenced from
    any active plan.pdhc Concept/ValueCatalog. Bookkept here so the
    workgroup can review and adopt out-of-band. Per execution plan
    §1.2.d.ii.
    """
    __tablename__ = "cdr_audit_plan_miss"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    canonical_uri = db.Column(db.String(512), nullable=False, unique=True)
    canonical_lib_name = db.Column(db.String(128), nullable=True)
    canonical_refnumber = db.Column(db.String(128), nullable=True)
    seen_count = db.Column(db.Integer, nullable=False, default=1)
    first_seen_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    last_seen_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    last_request_id = db.Column(db.String(64), nullable=True)


class ChangeFeed(db.Model):
    """Append-only event log emitted on every CDR write.

    The event backbone other services (dashboard, simulator, sibling CDRs)
    can subscribe to. Per execution plan §1.5.
    """
    __tablename__ = "change_feed"

    seq = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    event_type = db.Column(db.String(32), nullable=False)  # 'create' | 'update' | 'delete'
    resource_type = db.Column(db.String(64), nullable=False)
    resource_guid = db.Column(db.String(36), nullable=False)
    patient_guid = db.Column(db.String(36), nullable=True)
    org_guid = db.Column(db.String(36), nullable=False)
    version_id = db.Column(db.Integer, nullable=False)
    sync_group_id = db.Column(db.String(36), nullable=True)
    code_canonical = db.Column(db.String(512), nullable=True)
    source_request_id = db.Column(db.String(64), nullable=True)
    payload_summary = db.Column(JSONType, nullable=True)
    occurred_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        db.Index("ix_change_feed_seq", "seq"),
        db.Index("ix_change_feed_type", "resource_type"),
        db.Index("ix_change_feed_org", "org_guid"),
        db.Index("ix_change_feed_pat", "patient_guid"),
        db.Index("ix_change_feed_occurred", "occurred_at"),
    )
