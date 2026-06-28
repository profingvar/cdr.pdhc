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

            # 3.5 Live observation row — #295. Mirrors the per-type FHIR
            # search index used by GET /api/v1/fhir/Observation. Without
            # this, the ingest path (gateway → cdr1) leaves the Live table
            # empty, and the federated analyse search (dashboard.pdhc
            # since #291) returns 0 entries. Idempotent: if a row with
            # the same guid already exists, skip.
            if (fhir_resource.get("resourceType") == "Observation"
                    and fhir_resource.get("id")):
                from app.models.resources import live_model
                LiveObs = live_model("Observation")
                existing_live = (LiveObs.query
                                 .filter_by(guid=fhir_resource["id"])
                                 .first()) if LiveObs is not None else None
                if existing_live is None:
                    live_row = build_live_observation_row(
                        fhir_resource,
                        patient_guid=patient_guid,
                        source_service=source_service,
                        ingest_raw_guid=raw.guid,
                    )
                    if live_row is not None:
                        db.session.add(live_row)
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


# ---------------------------------------------------------------------------
# #295 — populate the Live observation table during ingest
# ---------------------------------------------------------------------------

# Code systems whose codings count as a canonical reference. LOINC is the
# external standard; the urn:pdhc:concept + plan.pdhc URLs are the
# platform-canonical references gateway emits via
# gateway.pdhc/app/services/fhir_observation_builder.py.
_CANONICAL_CODE_SYSTEMS = (
    "http://loinc.org",
    "urn:pdhc:concept",
    "https://plan.pdhc.se/api/v1/concepts",
)


def _primary_canonical_uri(fhir_resource):
    """Return `<system>/<code>` for the first canonical-system coding, else None.

    Mirrors the shape resource_writer._primary_code_canonical produces so
    cdr_app/app/api/fhir_read.py:_filter_by_code can equality-match it.
    """
    for coding in ((fhir_resource.get("code") or {}).get("coding") or []):
        sys = coding.get("system")
        if sys in _CANONICAL_CODE_SYSTEMS and coding.get("code"):
            return f"{sys.rstrip('/')}/{coding['code']}"
    return None


def build_live_observation_row(fhir_resource, *, patient_guid, source_service,
                                ingest_raw_guid=None):
    """Build a Live Observation ORM row from a FHIR R5 Observation dict.

    Mirrors the column layout of `public.observation` (per migration 0001).
    Used by both the live ingest path (IngestPipeline.process step 3.5) and
    the one-shot `flask backfill-live-observations` CLI (#295).
    """
    from datetime import datetime, timezone
    from app.models.resources import live_model
    LiveObservation = live_model("Observation")
    if LiveObservation is None:
        return None

    now = datetime.now(timezone.utc)

    # guid: prefer the FHIR resource's own id (gateway always sets one);
    # fall back to ingest_raw_guid so a Live row always lands somewhere.
    guid = fhir_resource.get("id") or ingest_raw_guid

    code_canonical = _primary_canonical_uri(fhir_resource)
    effective_at = _parse_effective(fhir_resource.get("effectiveDateTime"))

    # Provider org from FHIR Observation.performer[0] (gateway always populates this).
    org_guid = None
    for performer in (fhir_resource.get("performer") or []):
        ident = (performer.get("identifier") or {}).get("value")
        if ident:
            org_guid = ident
            break
    org_guid = org_guid or "00000000-0000-0000-0000-000000000000"

    # Value extraction — pick the first present *value* field.
    vq = fhir_resource.get("valueQuantity") or {}
    value_quantity = vq.get("value")
    value_unit = vq.get("unit") or vq.get("code")
    value_string = fhir_resource.get("valueString")
    value_code = ((fhir_resource.get("valueCodeableConcept") or {})
                  .get("coding") or [{}])[0].get("code")

    return LiveObservation(
        guid=guid,
        patient_guid=patient_guid,
        org_guid=org_guid,
        code_canonical=code_canonical,
        effective_at=effective_at,
        raw_json=fhir_resource,
        source=source_service,
        meta_tag=(fhir_resource.get("meta") or {}).get("tag"),
        version_id=1,
        created_at=now,
        updated_at=now,
        value_quantity=value_quantity,
        value_unit=value_unit,
        value_string=value_string,
        value_code=value_code,
        status=fhir_resource.get("status"),
    )


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
