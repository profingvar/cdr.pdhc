"""CDR database models — three-layer storage + Cambio delivery."""
import uuid
import hashlib
import json
from datetime import datetime, timezone
from app import db


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Layer 1 — Raw Store (immutable)
# ---------------------------------------------------------------------------

class IngestRaw(db.Model):
    __tablename__ = "ingest_raw"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    source_service = db.Column(db.String(64), nullable=False)
    source_system_id = db.Column(db.String(64), nullable=True)
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    payload_json = db.Column(db.JSON, nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False, index=True)
    headers_json = db.Column(db.JSON, nullable=True)
    source_type = db.Column(db.String(32), nullable=True)
    received_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)

    @staticmethod
    def compute_hash(payload):
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()


# ---------------------------------------------------------------------------
# Layer 2 — Standard Store (dual-format)
# ---------------------------------------------------------------------------

class FhirResource(db.Model):
    __tablename__ = "fhir_resources"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    ingest_raw_guid = db.Column(db.String(36), db.ForeignKey("ingest_raw.guid"), nullable=False)
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    resource_type = db.Column(db.String(64), nullable=False)
    resource_json = db.Column(db.JSON, nullable=False)
    loinc_code = db.Column(db.String(16), nullable=True, index=True)
    status = db.Column(db.String(32), default="final")
    effective_at = db.Column(db.DateTime(timezone=True), nullable=True)
    source_service = db.Column(db.String(64), nullable=True)
    # #294 RFC E1: three time concepts — `effective_at` is clinical
    # measurement time; `received_at` is when the platform first saw
    # the payload (= IngestRaw.received_at); `created_at` is when
    # THIS row was written. Default mirrors `created_at` for rows that
    # arrive without an explicit ingest-boundary timestamp.
    received_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        db.Index("ix_fhir_patient_type_eff", "patient_guid", "resource_type", effective_at.desc()),
    )


class OpenEhrComposition(db.Model):
    __tablename__ = "openehr_compositions"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    ingest_raw_guid = db.Column(db.String(36), db.ForeignKey("ingest_raw.guid"), nullable=False)
    fhir_resource_guid = db.Column(db.String(36), db.ForeignKey("fhir_resources.guid"), nullable=True)
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    archetype_id = db.Column(db.String(128), nullable=True)
    template_id = db.Column(db.String(128), default="generic")
    composition_json = db.Column(db.JSON, nullable=False)
    effective_at = db.Column(db.DateTime(timezone=True), nullable=True)
    source_service = db.Column(db.String(64), nullable=True)
    # #294 RFC E1: see FhirResource.received_at docstring.
    received_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        db.Index("ix_openehr_patient_arch_eff", "patient_guid", "archetype_id", effective_at.desc()),
    )


# ---------------------------------------------------------------------------
# Layer 3 — Canonical Store (query-optimized)
# ---------------------------------------------------------------------------

class HealthObservation(db.Model):
    __tablename__ = "health_observations"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    ingest_raw_guid = db.Column(db.String(36), db.ForeignKey("ingest_raw.guid"), nullable=False)
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    metric = db.Column(db.String(64), nullable=False)
    value = db.Column(db.Numeric(12, 4), nullable=True)
    unit = db.Column(db.String(32), nullable=True)
    source_type = db.Column(db.String(16), nullable=True)
    source_code = db.Column(db.String(32), nullable=True)
    source_service = db.Column(db.String(64), nullable=True)
    concept_guid = db.Column(db.String(36), nullable=True, index=True)
    effective_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # #294 RFC E1: see FhirResource.received_at docstring.
    received_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)


class Activity(db.Model):
    __tablename__ = "activities"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    ingest_raw_guid = db.Column(db.String(36), db.ForeignKey("ingest_raw.guid"), nullable=False)
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    activity_type = db.Column(db.String(64), nullable=False)
    value = db.Column(db.Numeric(12, 4), nullable=True)
    unit = db.Column(db.String(32), nullable=True)
    source_type = db.Column(db.String(16), nullable=True)
    source_code = db.Column(db.String(32), nullable=True)
    source_service = db.Column(db.String(64), nullable=True)
    effective_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # #294 RFC E1: see FhirResource.received_at docstring.
    received_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)


# ---------------------------------------------------------------------------
# Provenance and context
# ---------------------------------------------------------------------------

class ClinicalContext(db.Model):
    """Per-observation clinical context — the canonical 12-field record.

    Matches plans/pdhc_clinical_context_harmonisation_plan.md §3
    field-for-field. #302 widened this from 6 fields to the full 12 in
    2026-06-28, renaming the legacy aliases (careplan_guid →
    care_plan_guid; plandef_guid → plan_definition_guid).
    """
    __tablename__ = "clinical_context"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    ingest_raw_guid = db.Column(db.String(36), db.ForeignKey("ingest_raw.guid"), nullable=False)

    # --- canonical 12-field clinical context (plan §3) -----------------------
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    service_request_guid = db.Column(db.String(36), nullable=True, index=True)
    transaction_guid = db.Column(db.String(36), nullable=True)
    concept_guid = db.Column(db.String(36), nullable=True, index=True)
    plan_definition_guid = db.Column(db.String(36), nullable=True)
    care_plan_guid = db.Column(db.String(36), nullable=True)
    contract_guid = db.Column(db.String(36), nullable=True)
    requesting_org_guid = db.Column(db.String(36), nullable=True)
    provider_org_guid = db.Column(db.String(36), nullable=True, index=True)
    requester_user_guid = db.Column(db.String(36), nullable=True)
    received_at = db.Column(db.DateTime(timezone=True), nullable=True)
    source_service = db.Column(db.String(64), nullable=True)

    # --- bag-of-fields for unmapped extras + ops -----------------------------
    resolved_context_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class DedupeRegistry(db.Model):
    __tablename__ = "dedupe_registry"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    payload_hash = db.Column(db.String(64), nullable=False)
    source_service = db.Column(db.String(64), nullable=False)
    patient_guid = db.Column(db.String(36), nullable=False)
    first_seen_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    last_seen_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    hit_count = db.Column(db.Integer, default=1, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("payload_hash", "source_service", name="uq_dedupe_hash_service"),
    )


# ---------------------------------------------------------------------------
# Service keys
# ---------------------------------------------------------------------------

class ServiceKey(db.Model):
    __tablename__ = "service_keys"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    service_name = db.Column(db.String(64), nullable=False, unique=True)
    key_hash = db.Column(db.String(128), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# LOINC-to-archetype mapping
# ---------------------------------------------------------------------------

class LoincArchetypeMap(db.Model):
    __tablename__ = "loinc_archetype_map"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    loinc_code = db.Column(db.String(16), nullable=False, index=True)
    loinc_display = db.Column(db.String(128), nullable=True)
    archetype_id = db.Column(db.String(128), nullable=True)
    archetype_node_id = db.Column(db.String(32), nullable=True)
    canonical_metric = db.Column(db.String(64), nullable=True)
    canonical_unit = db.Column(db.String(32), nullable=True)
    canonical_table = db.Column(db.String(64), nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)


# ---------------------------------------------------------------------------
# Users (SSO-synced)
# ---------------------------------------------------------------------------

class User(db.Model):
    __tablename__ = "users"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    username = db.Column(db.String(128), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_su = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class AuditLog(db.Model):
    __tablename__ = "audit_log"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    event_type = db.Column(db.String(64), nullable=False, index=True)
    actor_guid = db.Column(db.String(36), nullable=True)
    data_subject_guid = db.Column(db.String(36), nullable=True)
    source_service = db.Column(db.String(64), nullable=True)
    correlation_id = db.Column(db.String(64), nullable=True)
    payload_snapshot = db.Column(db.JSON, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)


# ---------------------------------------------------------------------------
# Cambio CDR sandbox delivery
# ---------------------------------------------------------------------------

class CambioPatientMap(db.Model):
    __tablename__ = "cambio_patient_map"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    pdhc_patient_guid = db.Column(db.String(36), nullable=False, unique=True, index=True)
    cambio_patient_id = db.Column(db.String(128), nullable=True)
    cambio_ehr_id = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)


class CambioDeliveryLog(db.Model):
    __tablename__ = "cambio_delivery_log"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    ingest_raw_guid = db.Column(db.String(36), db.ForeignKey("ingest_raw.guid"), nullable=False)
    fhir_resource_guid = db.Column(db.String(36), db.ForeignKey("fhir_resources.guid"), nullable=True)
    openehr_comp_guid = db.Column(db.String(36), db.ForeignKey("openehr_compositions.guid"), nullable=True)
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    delivery_type = db.Column(db.String(16), nullable=False)  # 'fhir' | 'openehr'
    cambio_resource_id = db.Column(db.String(128), nullable=True)
    status = db.Column(db.String(32), default="pending", nullable=False)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    last_attempt_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    delivered_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # X2 (#423): the operator SSO session id captured from the inbound ingest
    # request (X-Operator-Session-Id, forwarded by gateway), replayed by the
    # cambio_worker as X-Operator-Session-Id on the cdr1 -> Cambio hop so the
    # chain-of-custody survives the async queue gap. Nullable = no operator
    # correlation (machine ingest).
    operator_session_id = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("ingest_raw_guid", "delivery_type", name="uq_delivery_raw_type"),
    )


# ---------------------------------------------------------------------------
# Phase-1 platform-plan tables (FHIR per-type + history + sync_group +
# cdr_audit_plan_miss + change_feed). Imported for side effect so SQLAlchemy
# registers the tables on db.metadata.
# ---------------------------------------------------------------------------
from app.models import resources as _resources  # noqa: E402, F401
from app.models.resources import (  # noqa: E402, F401
    SyncGroup,
    CdrAuditPlanMiss,
    ChangeFeed,
    live_model,
    history_model,
)


class ReadAudit(db.Model):
    """X1 (#407/#443) — one row per patient-touching FHIR read.

    cdr1..5 SoT counterpart of cdr_6's cdr_6_read_audit (M0 #412
    reference). Tuple columns per the emission contract in
    plans/pdhc_data_shapes.md §5: role_guid = the ACTIVE affiliation's
    role; purpose/access_basis are the closed enums. Machine reads
    (dashboard federation, sim) carry caller_service with NULL tuple —
    the sibling reader holds the operator context and logs the real
    purpose on its side.
    """
    __tablename__ = "cdr_read_audit"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=_now,
                          nullable=False, index=True)

    caller_service = db.Column(db.String(64), nullable=True, index=True)
    caller_user_guid = db.Column(db.String(36), nullable=True, index=True)
    caller_org_guids = db.Column(db.JSON, nullable=True)

    route = db.Column(db.String(200), nullable=False, index=True)
    resource_type = db.Column(db.String(64), nullable=True)
    patient_guid = db.Column(db.String(64), nullable=True, index=True)
    n_rows_returned = db.Column(db.Integer, nullable=True)
    response_status = db.Column(db.Integer, nullable=False, default=200)

    session_id = db.Column(db.String(128), nullable=True, index=True)
    role_guid = db.Column(db.String(64), nullable=True, index=True)
    purpose = db.Column(db.String(32), nullable=True)
    access_basis = db.Column(db.String(32), nullable=True)
