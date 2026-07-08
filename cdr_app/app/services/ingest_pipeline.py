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

        # X2 (#423): capture the operator session forwarded on the ingest
        # request (X-Operator-Session-Id, set by gateway) so the async
        # cambio_worker can replay it on the cdr1 -> Cambio hop.
        op_sid = None
        if headers:
            op_sid = (headers.get("X-Operator-Session-Id") or None)
            if op_sid:
                op_sid = str(op_sid)[:128]

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
            primary_code = _extract_primary_code_hint(fhir_resource)
            # `loinc_code` column is String(16). LOINC codes fit; PDHC
            # concept GUIDs (36 chars) do not. Only persist a value
            # here when it fits — otherwise the flush blows up.
            loinc_code = primary_code if _loinc_column_writable(primary_code) else None
            effective_at = _parse_effective(fhir_resource.get("effectiveDateTime"))
            fhir_row = FhirResource(
                ingest_raw_guid=raw.guid,
                patient_guid=patient_guid,
                resource_type=fhir_resource.get("resourceType", "Observation"),
                resource_json=fhir_resource,
                loinc_code=loinc_code,
                effective_at=effective_at,
                source_service=source_service,
                received_at=raw.received_at,
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
                        received_at=raw.received_at,
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
                received_at=raw.received_at,
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
                    received_at=raw.received_at,
                )
                db.session.add(openehr_row)
                db.session.flush()

        # Generate FHIR from openEHR if FHIR not provided
        if not fhir_resource and openehr_comp:
            generated_fhir = FhirOpenEhrTransformer.openehr_to_fhir(openehr_comp, patient_guid)
            if generated_fhir:
                gen_primary_code = _extract_primary_code_hint(generated_fhir)
                gen_loinc_code = (
                    gen_primary_code if _loinc_column_writable(gen_primary_code)
                    else None
                )
                fhir_row = FhirResource(
                    ingest_raw_guid=raw.guid,
                    patient_guid=patient_guid,
                    resource_type="Observation",
                    resource_json=generated_fhir,
                    loinc_code=gen_loinc_code,
                    effective_at=_parse_effective(generated_fhir.get("effectiveDateTime")),
                    source_service=source_service,
                    received_at=raw.received_at,
                )
                db.session.add(fhir_row)
                db.session.flush()

        # 4. Store canonical
        concept_guid = None
        if canonical:
            concept_guid = canonical.get("concept_guid")
            _store_canonical(raw.guid, patient_guid, canonical, source_service,
                             received_at=raw.received_at)

        # 5. Store context — canonical 12-field set (#302). Accept
        # legacy alias keys (careplan_guid, plandef_guid) from
        # producers during the deprecation window.
        if context:
            db.session.add(ClinicalContext(
                ingest_raw_guid=raw.guid,
                patient_guid=patient_guid,
                service_request_guid=context.get("service_request_guid"),
                transaction_guid=context.get("transaction_guid"),
                concept_guid=context.get("concept_guid") or concept_guid,
                plan_definition_guid=(context.get("plan_definition_guid")
                                      or context.get("plandef_guid")),
                care_plan_guid=(context.get("care_plan_guid")
                                or context.get("careplan_guid")),
                contract_guid=context.get("contract_guid"),
                requesting_org_guid=context.get("requesting_org_guid"),
                provider_org_guid=context.get("provider_org_guid"),
                requester_user_guid=context.get("requester_user_guid"),
                received_at=raw.received_at,
                source_service=context.get("source_service") or source_service,
                resolved_context_json=context.get("resolved_context_json"),
            ))

        # 6. Dedupe registry
        db.session.add(DedupeRegistry(
            payload_hash=payload_hash,
            source_service=source_service,
            patient_guid=patient_guid,
        ))

        # 7. Enqueue Cambio delivery (if concept-mapped).
        # A concept ref is present when:
        #   - the ingest body's `canonical.concept_guid` field is set, OR
        #   - the FHIR resource carries a LOINC coding (fits the
        #     `loinc_code` column), OR
        #   - the FHIR resource carries a PDHC-scoped concept coding
        #     (urn:pdhc:concept / plan.pdhc concept URL — matched via
        #     _primary_canonical_uri, which is what
        #     LiveObservation.code_canonical is also keyed on).
        # Pre-fix, the third branch was missing: gateway.pdhc forwards
        # every Observation with `urn:pdhc:concept` codings and no
        # `canonical.concept_guid` in the wrapping payload, so Cambio
        # delivery was never enqueued for real production traffic.
        has_concept_ref = bool(
            concept_guid
            or (fhir_row and fhir_row.loinc_code)
            or (fhir_row and _primary_canonical_uri(fhir_row.resource_json))
        )
        if has_concept_ref:
            if fhir_row:
                db.session.add(CambioDeliveryLog(
                    ingest_raw_guid=raw.guid,
                    fhir_resource_guid=fhir_row.guid,
                    patient_guid=patient_guid,
                    delivery_type="fhir",
                    operator_session_id=op_sid,
                    status="pending",
                ))
            if openehr_row:
                db.session.add(CambioDeliveryLog(
                    ingest_raw_guid=raw.guid,
                    openehr_comp_guid=openehr_row.guid,
                    patient_guid=patient_guid,
                    delivery_type="openehr",
                    operator_session_id=op_sid,
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
                    operator_session_id=op_sid,
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


def _extract_primary_code_hint(fhir_resource):
    """Return the primary code from any canonical coding system.

    Prefers LOINC (external standard). Falls back to PDHC-scoped
    concept codings (`urn:pdhc:concept` or the legacy plan.pdhc
    concept URL) — gateway.pdhc emits these on every forwarded
    Observation. Prior to this change (pre-#296 followup 2026-07-02)
    the function only read `http://loinc.org`, so every forwarded
    row had a NULL `loinc_code` and the `has_concept_ref` gate
    below wouldn't fire for PDHC-scoped observations.

    Returns the raw `code` field. When the source is a PDHC concept
    that's a 36-char GUID; when it's LOINC it's a short LOINC code.
    Callers writing into the `FhirResource.loinc_code` String(16)
    column must filter to values that fit — see the ingest site.

    Kept exported under the old name `_extract_loinc` too because
    tests and downstream call sites (`_extract_loinc(generated_fhir)`
    from the openEHR→FHIR path) reference it verbatim.
    """
    codings = ((fhir_resource.get("code") or {}).get("coding") or [])
    # LOINC first — it's the external-standard preference.
    for coding in codings:
        if coding.get("system") == "http://loinc.org":
            return coding.get("code")
    # PDHC-scoped fallback covers everything gateway.pdhc forwards.
    for coding in codings:
        if coding.get("system") in (
            "urn:pdhc:concept",
            "https://plan.pdhc.se/api/v1/concepts",
        ):
            return coding.get("code")
    return None


# Legacy name preserved for back-compat with existing tests and the
# openEHR-generated-FHIR ingest branch. New callers should use the
# explicit `_extract_primary_code_hint` name.
_extract_loinc = _extract_primary_code_hint


# `loinc_code` column is `db.String(16)` (see models.__init__.FhirResource).
# LOINC codes fit; a PDHC concept GUID (36 chars) does not. Callers that
# write into that column must filter with this helper first — otherwise
# the flush raises a DataError.
_LOINC_CODE_COLUMN_MAX = 16


def _loinc_column_writable(code):
    """True if `code` fits `FhirResource.loinc_code` String(16)."""
    return bool(code) and len(code) <= _LOINC_CODE_COLUMN_MAX


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
                                ingest_raw_guid=None, received_at=None):
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
        # #294 RFC E1: received_at = ingest boundary (IngestRaw.received_at).
        # Fall back to `now` for backfill callers without an IngestRaw row.
        received_at=received_at or now,
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


def _store_canonical(raw_guid, patient_guid, canonical, source_service,
                     received_at=None):
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
            received_at=received_at or datetime.now(timezone.utc),
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
            received_at=received_at or datetime.now(timezone.utc),
        ))
