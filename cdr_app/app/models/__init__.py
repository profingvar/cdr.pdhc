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
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)


# ---------------------------------------------------------------------------
# Provenance and context
# ---------------------------------------------------------------------------

class ClinicalContext(db.Model):
    __tablename__ = "clinical_context"

    guid = db.Column(db.String(36), primary_key=True, default=_uuid)
    ingest_raw_guid = db.Column(db.String(36), db.ForeignKey("ingest_raw.guid"), nullable=False)
    patient_guid = db.Column(db.String(36), nullable=False, index=True)
    transaction_guid = db.Column(db.String(36), nullable=True)
    careplan_guid = db.Column(db.String(36), nullable=True)
    plandef_guid = db.Column(db.String(36), nullable=True)
    resolved_context_json = db.Column(db.JSON, nullable=True)
    source_service = db.Column(db.String(64), nullable=True)
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
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("ingest_raw_guid", "delivery_type", name="uq_delivery_raw_type"),
    )
