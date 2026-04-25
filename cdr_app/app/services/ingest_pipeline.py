"""Ingest processing pipeline — raw → standard → canonical → delivery."""
import logging
from datetime import datetime, timezone
from flask import request as flask_request
from app import db
from app.models import (
    IngestRaw, FhirResource, OpenEhrComposition, HealthObservation,
    Activity, ClinicalContext, DedupeRegistry, AuditLog, CambioDeliveryLog,
)
from .transformer import FhirOpenEhrTransformer

logger = logging.getLogger(__name__)


class IngestPipeline:

    @staticmethod
    def process(body, source_service, headers):
        patient_guid = body["patient_guid"]
        source_type = body.get("source_type", "fhir")

        # 1. Deduplicate
        payload_hash = IngestRaw.compute_hash(body)
        existing = DedupeRegistry.query.filter_by(
            payload_hash=payload_hash, source_service=source_service
        ).first()
        if existing:
            existing.last_seen_at = datetime.now(timezone.utc)
            existing.hit_count += 1
            db.session.commit()
            return {"status": "duplicate", "dedupe_hash": payload_hash}

        # 2. Store raw
        raw = IngestRaw(
            source_service=source_service,
            source_system_id=body.get("source_system_id"),
            patient_guid=patient_guid,
            payload_json=body,
            payload_hash=payload_hash,
            headers_json=dict(headers) if headers else None,
            source_type=source_type,
        )
        db.session.add(raw)
        db.session.flush()

        # 3. Store/transform standard layer
        fhir_resource = body.get("fhir_resource")
        openehr_comp = body.get("openehr_composition")
        canonical = body.get("canonical")
        context = body.get("clinical_context")

        fhir_row = None
        openehr_row = None

        # Store FHIR resource
        if fhir_resource:
            loinc_code = _extract_loinc(fhir_resource)
            effective_at = _parse_effective(fhir_resource.get("effectiveDateTime"))
            fhir_row = FhirResource(
                ingest_raw_guid=raw.guid,
                patient_guid=patient_guid,
                resource_type=fhir_resource.get("resourceType", "Observation"),
                resource_json=fhir_resource,
                loinc_code=loinc_code,
                effective_at=effective_at,
                source_service=source_service,
            )
            db.session.add(fhir_row)
            db.session.flush()

        # Store or generate openEHR composition
        if openehr_comp:
            openehr_row = OpenEhrComposition(
                ingest_raw_guid=raw.guid,
                fhir_resource_guid=fhir_row.guid if fhir_row else None,
                patient_guid=patient_guid,
                archetype_id=openehr_comp.get("archetype_id"),
                composition_json=openehr_comp,
                effective_at=fhir_row.effective_at if fhir_row else None,
                source_service=source_service,
            )
            db.session.add(openehr_row)
            db.session.flush()
        elif fhir_resource:
            # Generate openEHR from FHIR
            generated = FhirOpenEhrTransformer.fhir_to_openehr(fhir_resource)
            if generated:
                openehr_row = OpenEhrComposition(
                    ingest_raw_guid=raw.guid,
                    fhir_resource_guid=fhir_row.guid,
                    patient_guid=patient_guid,
                    archetype_id=generated.get("archetype_id"),
                    composition_json=generated,
                    effective_at=fhir_row.effective_at,
                    source_service=source_service,
                )
                db.session.add(openehr_row)
                db.session.flush()

        # Generate FHIR from openEHR if FHIR not provided
        if not fhir_resource and openehr_comp:
            generated_fhir = FhirOpenEhrTransformer.openehr_to_fhir(openehr_comp, patient_guid)
            if generated_fhir:
                fhir_row = FhirResource(
                    ingest_raw_guid=raw.guid,
                    patient_guid=patient_guid,
                    resource_type="Observation",
                    resource_json=generated_fhir,
                    loinc_code=_extract_loinc(generated_fhir),
                    effective_at=_parse_effective(generated_fhir.get("effectiveDateTime")),
                    source_service=source_service,
                )
                db.session.add(fhir_row)
                db.session.flush()

        # 4. Store canonical
        concept_guid = None
        if canonical:
            concept_guid = canonical.get("concept_guid")
            _store_canonical(raw.guid, patient_guid, canonical, source_service)

        # 5. Store context
        if context:
            db.session.add(ClinicalContext(
                ingest_raw_guid=raw.guid,
                patient_guid=patient_guid,
                transaction_guid=context.get("transaction_guid"),
                careplan_guid=context.get("careplan_guid"),
                plandef_guid=context.get("plandef_guid"),
                resolved_context_json=context.get("resolved_context_json"),
                source_service=source_service,
            ))

        # 6. Dedupe registry
        db.session.add(DedupeRegistry(
            payload_hash=payload_hash,
            source_service=source_service,
            patient_guid=patient_guid,
        ))

        # 7. Enqueue Cambio delivery (if concept-mapped)
        has_concept_ref = bool(concept_guid or (fhir_row and fhir_row.loinc_code))
        if has_concept_ref:
            if fhir_row:
                db.session.add(CambioDeliveryLog(
                    ingest_raw_guid=raw.guid,
                    fhir_resource_guid=fhir_row.guid,
                    patient_guid=patient_guid,
                    delivery_type="fhir",
                    status="pending",
                ))
            if openehr_row:
                db.session.add(CambioDeliveryLog(
                    ingest_raw_guid=raw.guid,
                    openehr_comp_guid=openehr_row.guid,
                    patient_guid=patient_guid,
                    delivery_type="openehr",
                    status="pending",
                ))
        else:
            # Not eligible for Cambio — no concept mapping
            if fhir_row:
                db.session.add(CambioDeliveryLog(
                    ingest_raw_guid=raw.guid,
                    fhir_resource_guid=fhir_row.guid,
                    patient_guid=patient_guid,
                    delivery_type="fhir",
                    status="skipped",
                ))

        # 8. Audit
        db.session.add(AuditLog(
            event_type="ingest.accepted",
            actor_guid=source_service,
            data_subject_guid=patient_guid,
            source_service=source_service,
            correlation_id=flask_request.headers.get("X-Correlation-Id"),
            ip_address=flask_request.remote_addr,
            payload_snapshot={
                "ingest_raw_guid": raw.guid,
                "source_type": source_type,
                "payload_hash": payload_hash,
                "has_fhir": fhir_row is not None,
                "has_openehr": openehr_row is not None,
                "has_canonical": canonical is not None,
                "has_context": context is not None,
                "cambio_eligible": has_concept_ref,
            },
        ))

        db.session.commit()

        return {
            "status": "accepted",
            "ingest_raw_guid": raw.guid,
        }


def _extract_loinc(fhir_resource):
    for coding in ((fhir_resource.get("code") or {}).get("coding") or []):
        if coding.get("system") == "http://loinc.org":
            return coding.get("code")
    return None


def _parse_effective(dt_str):
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _store_canonical(raw_guid, patient_guid, canonical, source_service):
    table = canonical.get("table", "health_observations")
    effective_at = _parse_effective(canonical.get("effective_at"))

    if table == "health_observations":
        db.session.add(HealthObservation(
            ingest_raw_guid=raw_guid,
            patient_guid=patient_guid,
            metric=canonical.get("metric", "unknown"),
            value=canonical.get("value"),
            unit=canonical.get("unit"),
            source_type=canonical.get("source_type", "fhir"),
            source_code=canonical.get("source_code"),
            source_service=source_service,
            concept_guid=canonical.get("concept_guid"),
            effective_at=effective_at,
        ))
    elif table == "activities":
        db.session.add(Activity(
            ingest_raw_guid=raw_guid,
            patient_guid=patient_guid,
            activity_type=canonical.get("metric", "unknown"),
            value=canonical.get("value"),
            unit=canonical.get("unit"),
            source_type=canonical.get("source_type", "fhir"),
            source_code=canonical.get("source_code"),
            source_service=source_service,
            effective_at=effective_at,
        ))
