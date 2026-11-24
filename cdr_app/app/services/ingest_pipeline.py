"""Ingest processing pipeline — raw → standard → canonical → delivery."""
import logging
from datetime import datetime, timezone
from flask import current_app, request as flask_request
from app import db
from app.models import (
    IngestRaw, FhirResource, HealthObservation,
    Activity, ClinicalContext, DedupeRegistry, AuditLog, CambioDeliveryLog,
)
from app.services.synthetic import synthetic_reason
from app.services import data_origin as _origin
from app.services import review as _review

logger = logging.getLogger(__name__)



#: The canonical clinical-context fields, as `plans/pdhc_data_shapes.md`
#: defines them. Named here so the flat and nested payload shapes cannot
#: drift apart silently.
_CONTEXT_KEYS = (
    "service_request_guid", "transaction_guid", "concept_guid",
    "plan_definition_guid", "plan_activity_guid", "care_plan_guid",
    "contract_guid", "requesting_org_guid", "provider_org_guid",
    "requester_user_guid", "author_org_guid", "patient_org_guid",
    "resolved_context_json", "source_service",
    # #821: which sim run produced the row. sim and cdr_6 emit it at the top
    # level like the rest; absent from this list it would be dropped from
    # every flat payload, which is the defect #820 existed to fix.
    "sim_run_id",
    # #1014: patient-generated or (NULL) healthcare. Listed so a flat payload
    # keeps it, for the same reason as sim_run_id above.
    "data_origin",
    # #1044: `true` = this patient-reported answer must be reviewed by care.
    # Not stored on clinical_context (the status lives on the row, once);
    # listed so the flat payload shape carries it like the nested one.
    "review_required",
    # #1049 (contract s7.4): the patient card's section and version. Stored
    # on the ROW (beside the review columns), not on clinical_context.
    "card_section", "card_version",
)
# #1044: `clinical_context.group` says which INSTANCE of a repeating group an
# answer belongs to (one allergy = substance + reaction + severity, three
# rows): any stable string per instance, kept in
# resolved_context_json["group"]. Honoured in the NESTED context only and
# deliberately not listed above: `group` is too ordinary a word to start
# reading off the top level of every producer's payload.

#: Wire aliases still accepted during the #294 deprecation window.
_CONTEXT_ALIASES = {
    "plandef_guid": "plan_definition_guid",
    "careplan_guid": "care_plan_guid",
    "activity_guid": "transaction_guid",
    "requester_org_guid": "requesting_org_guid",
}


def _clinical_context_from(body: dict) -> dict | None:
    """The clinical context, from EITHER payload shape (#820).

    This used to be `body.get("clinical_context")` and nothing else, so a
    producer that put the canonical fields at the TOP LEVEL got no
    `ClinicalContext` row at all — silently, because the insert is guarded by
    `if context:`.

    That is not a hypothetical: cdr_6's own ingest reads these keys at the top
    level, so a cohort transferred out of cdr_6 into cdr1/cdr2/cdr3 arrived
    with its contract, plandef, visit and transaction dropped, while the
    transfer reported `verified: true` (it verifies the row COUNT). Measured
    2026-10-11: 30 of 30 rows, every context field empty. One canonical
    context, two payload contracts, and the mismatch cost the annotation.

    Nested wins when present, because an explicit block is a deliberate
    statement; top-level keys are the fallback. Returns None when neither
    carries anything, so the `if context:` guard still means "nothing was
    said about this row".
    """
    nested = body.get("clinical_context")
    if isinstance(nested, dict) and any(nested.get(k) for k in _CONTEXT_KEYS):
        return nested
    flat = {}
    for key in _CONTEXT_KEYS:
        v = body.get(key)
        if v not in (None, ""):
            flat[key] = v
    for alias, canonical in _CONTEXT_ALIASES.items():
        v = body.get(alias)
        if v not in (None, "") and canonical not in flat:
            flat[canonical] = v
    if not flat:
        # Preserve a nested block that exists but is entirely empty, so a
        # caller that sent one explicitly is not second-guessed.
        return nested if isinstance(nested, dict) else None
    return flat


#: #1054: request headers NEVER copied into ingest_raw.headers_json. Until
#: 2026-11-24 the whole header set was stored, so every ingest_raw row held
#: the calling service's own X-Service-Key in clear (measured with a test
#: client: gateway's key, verbatim). Rows written before this fix still do.
SECRET_HEADERS = frozenset({"x-service-key", "authorization", "cookie",
                            "x-sso-client-secret", "proxy-authorization"})


def safe_headers(headers) -> dict | None:
    """The request headers minus credentials, for ingest_raw.headers_json."""
    if not headers:
        return None
    return {k: v for k, v in dict(headers).items()
            if k.lower() not in SECRET_HEADERS}


class IngestPipeline:

    @staticmethod
    def process(body, source_service, headers):
        patient_guid = body["patient_guid"]
        source_type = body.get("source_type", "fhir")

        # #682 (2026-11-20): cdr no longer stores, generates or accepts openEHR.
        # rosetta.pdhc renders openEHR from the canonical observation. Measured
        # before removal: no producer had ever sent `openehr_composition` to
        # cdr1, cdr2 or cdr3, so refusing it loses nothing and says so loudly
        # instead of silently storing only the raw payload.
        if "openehr_composition" in body:
            return {"status": "rejected", "errors": [
                "openehr_input_retired: send fhir_resource (+ canonical); "
                "openEHR is rendered by rosetta.pdhc"]}

        # #1014: refuse an unknown origin BEFORE anything is stored or
        # registered for dedupe, so a corrected resend is not a "duplicate".
        _ctx0 = _clinical_context_from(body) or {}
        _, origin_err = _origin.check(_ctx0.get("data_origin"))
        if origin_err:
            return {"status": "rejected", "errors": [origin_err]}

        # #1044: `review_required` (contract s3.2), refused by name before
        # anything is stored, for the same reason as the origin above. Read
        # from the context AND the top level: a nested context wins over flat
        # keys in `_clinical_context_from`, and a flag silently dropped there
        # would store the answer as never needing review.
        _flag = _ctx0.get("review_required")
        if _flag is None:
            _flag = body.get("review_required")
        review_required, review_err = _review.check_required(
            _flag, _ctx0.get("data_origin"))
        if not review_err and review_required:
            review_err = _review_target_error(body)
        if not review_err:
            review_err = _review.check_group(_ctx0.get("group"))
        # #1049: a card answer (card_section + card_version). Checked after
        # the review flag, because a card answer is always a reviewed one;
        # it needs no service_request_guid (v2 has no care order).
        card_section = card_version = None
        if not review_err:
            card_section, card_version, review_err = _review.check_card(
                _ctx0, review_required=review_required,
                patient_guid=patient_guid)
        if review_err:
            return {"status": "rejected", "errors": [review_err]}

        # #1054: an Observation's status from the source, and a report for an
        # observation already held (same id) becomes its NEXT VERSION. Both
        # decided here, before anything is stored, so a refused report leaves
        # no ingest_raw, no dedupe entry and no partial version behind.
        version_of, obs_err = observation_version_check(
            body.get("fhir_resource"), patient_guid=patient_guid,
            source_service=source_service)
        if obs_err:
            return obs_err

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
            headers_json=safe_headers(headers),   # #1054: never the key
            source_type=source_type,
        )
        db.session.add(raw)
        db.session.flush()

        # 3. Store/transform standard layer
        fhir_resource = body.get("fhir_resource")
        canonical = body.get("canonical")
        context = _clinical_context_from(body)

        fhir_row = None

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
                if existing_live is not None and version_of is not None:
                    # #1054: a correction / status change -> version n+1.
                    _new_observation_version(
                        existing_live, fhir_resource,
                        patient_guid=patient_guid,
                        source_service=source_service, raw=raw,
                        context=context)
                elif existing_live is None:
                    live_row = build_live_observation_row(
                        fhir_resource,
                        patient_guid=patient_guid,
                        source_service=source_service,
                        ingest_raw_guid=raw.guid,
                        received_at=raw.received_at,
                        context=context,
                    )
                    if live_row is not None and review_required:
                        # #1044: the ONLY place a row becomes pending.
                        live_row.review_status = _review.PENDING
                    if live_row is not None and card_version:
                        # #1049: latest wins. In THIS transaction, so the
                        # new version and the retirement of the older
                        # pending one commit together or not at all.
                        live_row.card_section = card_section
                        live_row.card_version = card_version
                        _review.supersede_older(
                            patient_guid=patient_guid, section=card_section,
                            version=card_version, at=raw.received_at)
                    if live_row is not None:
                        db.session.add(live_row)
                        db.session.flush()

        # 4. Store canonical
        concept_guid = None
        if canonical:
            concept_guid = canonical.get("concept_guid")
            _store_canonical(raw.guid, patient_guid, canonical, source_service,
                             received_at=raw.received_at)

        # 5. Store context — canonical field set (#302, +author_org #665).
        # Accept
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
                # #820: the VISIT. See the column's note in models.
                plan_activity_guid=context.get("plan_activity_guid"),
                care_plan_guid=(context.get("care_plan_guid")
                                or context.get("careplan_guid")),
                contract_guid=context.get("contract_guid"),
                requesting_org_guid=context.get("requesting_org_guid"),
                provider_org_guid=context.get("provider_org_guid"),
                # #665: declared by the submitter; NULL when not declared.
                # Deliberately NOT defaulted to provider_org_guid — a guess
                # written into the row becomes indistinguishable from a
                # declared fact. A consumer that wants a value can coalesce.
                author_org_guid=context.get("author_org_guid"),
                requester_user_guid=context.get("requester_user_guid"),
                received_at=raw.received_at,
                source_service=context.get("source_service") or source_service,
                # #821: the generation run, so a promoted cohort keeps its
                # run identity and not only its writer.
                sim_run_id=context.get("sim_run_id"),
                # #1014: validated above; NULL = healthcare by policy.
                data_origin=_origin.check(context.get("data_origin"))[0],
                resolved_context_json=_review.with_group(
                    context.get("resolved_context_json"), context.get("group")),
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
        # #1000: synthetic data never enters the Cambio queue. It used to be
        # queued exactly like real data — 8 056 sim rows sat "pending" behind
        # nothing but CAMBIO_DELIVERY_ENABLED=false. Recorded as "skipped"
        # with the reason, so the decision is visible rather than an absence.
        synthetic = synthetic_reason(
            source_service=source_service,
            context_source=(context or {}).get("source_service"),
            sim_run_id=(context or {}).get("sim_run_id"),
        )
        if synthetic:
            if fhir_row:
                db.session.add(CambioDeliveryLog(
                    ingest_raw_guid=raw.guid,
                    fhir_resource_guid=fhir_row.guid,
                    patient_guid=patient_guid,
                    delivery_type="fhir",
                    operator_session_id=op_sid,
                    status="skipped",
                    last_error=synthetic,
                ))
        elif has_concept_ref:
            if fhir_row:
                db.session.add(CambioDeliveryLog(
                    ingest_raw_guid=raw.guid,
                    fhir_resource_guid=fhir_row.guid,
                    patient_guid=patient_guid,
                    delivery_type="fhir",
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


# ---------------------------------------------------------------------------
# #1054: observation status and versions
# ---------------------------------------------------------------------------

#: The statuses a source may report (record.pdhc plan s5: preliminary,
#: final, corrected; amended and entered-in-error from FHIR's lifecycle).
OBSERVATION_STATUSES = ("preliminary", "final", "amended", "corrected",
                        "entered-in-error")
#: Order of a report's maturity. A new version may not go DOWN this list
#: (final -> preliminary), and nothing follows entered-in-error.
_STATUS_RANK = {"preliminary": 0, "final": 1, "amended": 1, "corrected": 1,
                "entered-in-error": 2}
VERSION_REFUSED = "conflict"


def _conflict(code: str, text: str) -> dict:
    return {"status": VERSION_REFUSED, "errors": [f"{code}: {text}"],
            "code": code}


def observation_version_check(fhir, *, patient_guid, source_service):
    """(existing live row to version, None) / (None, None) / (None, refusal).

    THE IDENTITY of "the same observation" is `Observation.id`: it is the
    live row's guid, and it was already the ingest's idempotence key (#295 --
    a second report with the same id used to be dropped silently, so a
    correction never reached the record). gateway sets it from the source's
    own id when the source gives one. `Observation.identifier` is not used:
    no producer sends it today, and two keys could disagree.

    A same-id report must be for the same patient and from the same source
    service, or it is an id collision rather than a new version (409). A
    report identical to the stored body is not a new version (None, None);
    the ingest keeps it as raw only, as before.
    """
    if not isinstance(fhir, dict) or fhir.get("resourceType") != "Observation":
        return None, None
    status = fhir.get("status")
    if status is not None and status not in OBSERVATION_STATUSES:
        return None, {"status": "rejected", "errors": [
            f"invalid_observation_status: {str(status)[:40]!r}; one of "
            f"{', '.join(OBSERVATION_STATUSES)}"]}
    oid = fhir.get("id")
    if not oid:
        return None, None
    from app.models.resources import live_model
    existing = live_model("Observation").query.filter_by(guid=oid).first()
    if existing is None:
        return None, None
    if _same_report(existing.raw_json, fhir):
        return None, None
    if existing.patient_guid != patient_guid:
        return None, _conflict("observation_id_conflict",
                               "this id belongs to another patient's observation")
    if (existing.source or None) != source_service:
        return None, _conflict("observation_source_conflict",
                               "this id was reported by another service; a new "
                               "version comes from the source of the first")
    if (existing.review_status is not None
            or existing.endorses_guid is not None):
        return None, _conflict("review_locked",
                               "a reviewed answer or endorsement is not "
                               "re-versioned by ingest (#1044)")
    if status is None:
        return None, {"status": "rejected", "errors": [
            "observation_status_required: a new version of an observation "
            "must state its status"]}
    old = existing.status
    if old == "entered-in-error":
        return None, _conflict("observation_entered_in_error",
                               "the observation is entered-in-error; nothing "
                               "follows it (report a new observation)")
    if old in _STATUS_RANK and _STATUS_RANK[status] < _STATUS_RANK[old]:
        return None, _conflict("status_regression",
                               f"{old} -> {status} is refused; a correction "
                               f"of a {old} result is 'corrected' or 'amended'")
    return existing, None


def _same_report(stored, incoming) -> bool:
    """The incoming body equals the stored one, ignoring the versionId this
    CDR stamps on versions >= 2."""
    def strip(b):
        b = dict(b or {})
        meta = dict(b.get("meta") or {})
        meta.pop("versionId", None)
        if meta:
            b["meta"] = meta
        else:
            b.pop("meta", None)
        return b
    return strip(stored) == strip(incoming)


#: Row context kept from the previous version when a correction omits it:
#: a correction does not move the observation out of its patient's unit.
_KEEP_IF_ABSENT = ("org_guid", "patient_org_guid", "transaction_guid",
                   "care_plan_guid", "data_origin")


def _new_observation_version(existing, fhir_resource, *, patient_guid,
                             source_service, raw, context):
    """Move the current version to observation_history (append-only) and
    make the live row version n+1. Never an in-place overwrite: the old
    version stays readable by vread."""
    from sqlalchemy import inspect as _inspect
    from app.models.resources import ChangeFeed, history_model, live_model
    Live, Hist = live_model("Observation"), history_model("Observation")
    hist_keys = {c.key for c in _inspect(Hist).columns}
    hist = {c.key: getattr(existing, c.key) for c in _inspect(Live).columns
            if c.key in hist_keys}
    now = datetime.now(timezone.utc)
    hist.update(superseded_at=now, superseded_by_request_id=raw.guid,
                version_written_at=existing.updated_at)
    db.session.add(Hist(**hist))

    new_version = (existing.version_id or 1) + 1
    body = dict(fhir_resource)
    body["meta"] = {**(body.get("meta") or {}), "versionId": str(new_version)}
    fresh = build_live_observation_row(
        body, patient_guid=patient_guid, source_service=source_service,
        ingest_raw_guid=raw.guid, received_at=raw.received_at, context=context)
    for col in _inspect(Live).columns:
        k = col.key
        if k in ("guid", "created_at"):
            continue
        val = getattr(fresh, k)
        if k in _KEEP_IF_ABSENT and val is None:
            continue
        setattr(existing, k, val)
    existing.version_id = new_version
    existing.etag = f'W/"{new_version}"'
    existing.updated_at = now
    db.session.flush()
    db.session.add(ChangeFeed(
        event_type="update", resource_type="Observation",
        resource_guid=existing.guid, patient_guid=patient_guid,
        org_guid=existing.org_guid, version_id=new_version,
        sync_group_id=existing.sync_group_id,
        code_canonical=existing.code_canonical,
        source_request_id=raw.guid,
        payload_summary={"operation": "update", "from_version": new_version - 1,
                         "status": existing.status}))


def _review_target_error(body) -> str | None:
    """Why this ingest cannot be put up for review, or None (#1044).

    The pending status lives on the live observation row, so an ingest that
    would not create one cannot be reviewed, and accepting it anyway would
    store a patient answer that silently never reaches the queue.
    """
    if not current_app.config.get("CDR_CLINICAL_READ", True):
        return ("review_not_available_here: this repository has no review "
                "surface (CDR_CLINICAL_READ=false); send answers that need "
                "review to the clinical repository")
    fhir = body.get("fhir_resource")
    if (not isinstance(fhir, dict) or fhir.get("resourceType") != "Observation"
            or not fhir.get("id")):
        return ("review_required_needs_observation: review_required applies "
                "to a FHIR Observation with an id")
    from app.models.resources import live_model
    LiveObs = live_model("Observation")
    if LiveObs.query.filter_by(guid=fhir["id"]).first() is not None:
        return ("review_required_guid_exists: an observation with this id is "
                "already stored; a new answer needs a new id")
    return None


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
    tests reference it verbatim.
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


# Legacy name preserved for back-compat with existing tests. New callers
# should use the explicit `_extract_primary_code_hint` name.
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


#: The PDHC concept system, in its CANONICAL spelling (#787).
_CONCEPT_SYSTEM_CANONICAL = "urn:pdhc:concept"
#: The same identifier, spelled as a plan.pdhc URL. **Legacy, read-only.**
#: gateway dual-emits both (`fhir_observation_builder.py`, the #294 alias
#: window), so this cannot be dropped from the accepted set yet — but it must
#: never be what gets WRITTEN when the canonical spelling is also present.
_CONCEPT_SYSTEM_LEGACY = "https://plan.pdhc.se/api/v1/concepts"


def _primary_canonical_uri(fhir_resource):
    """Return `<system>/<code>` for the preferred canonical coding, else None.

    Mirrors the shape resource_writer._primary_code_canonical produces so
    cdr_app/app/api/fhir_read.py:_filter_by_code can equality-match it.

    #787: the choice between the two PDHC spellings is now EXPLICIT, in two
    passes, instead of falling out of the producer's array order.

    It used to return the first coding whose system was in
    `_CANONICAL_CODE_SYSTEMS` — a membership test, so the tuple's order meant
    nothing and the winner was whichever coding the producer happened to put
    first. gateway puts `urn:pdhc:concept` before
    `https://plan.pdhc.se/api/v1/concepts`, so in practice the urn won and all
    40,941 rows measured on cdr1/2/3 on 2026-10-13 carry it (100%, zero of the
    other form).

    That was luck, not a decision, and the failure mode is quiet: reorder
    gateway's two codings and cdr starts writing the other spelling for the
    same concept. `fhir_read._filter_by_code` EQUALITY-matches this column, so
    every row written before the reorder would stop matching queries built
    after it. Same concept, two strings, no error.

    LOINC is deliberately left on the old footing. It is a real external code
    system rather than a second spelling of ours, so which of LOINC-vs-PDHC
    wins still follows the producer's array order, exactly as before — this
    change settles only the one-identifier-two-serialisations question #787
    asked about, and does not quietly re-rank anything else.
    """
    codings = (fhir_resource.get("code") or {}).get("coding") or []
    # Pass 1: anything but the legacy alias, in producer order.
    for coding in codings:
        sys = coding.get("system")
        if (sys in _CANONICAL_CODE_SYSTEMS
                and sys != _CONCEPT_SYSTEM_LEGACY and coding.get("code")):
            return f"{sys.rstrip('/')}/{coding['code']}"
    # Pass 2: the legacy spelling, only when nothing canonical was offered.
    for coding in codings:
        if coding.get("system") == _CONCEPT_SYSTEM_LEGACY and coding.get("code"):
            return f"{_CONCEPT_SYSTEM_LEGACY.rstrip('/')}/{coding['code']}"
    return None


def build_live_observation_row(fhir_resource, *, patient_guid, source_service,
                                ingest_raw_guid=None, received_at=None,
                                context=None):
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
        # #1049: a patient card answer's performer is the PATIENT. A patient
        # is never an organisation, so its guid must not land in org_guid.
        if (performer.get("type") == "Patient"
                or str(performer.get("reference") or "").startswith("Patient/")):
            continue
        ident = (performer.get("identifier") or {}).get("value")
        if ident:
            org_guid = ident
            break
    # #1006: no performer, no org_guid. It used to fall back to the all-zero
    # guid, an invented organisation that readers could not tell from a real one.

    # Value extraction — pick the first present *value* field.
    vq = fhir_resource.get("valueQuantity") or {}
    value_quantity = vq.get("value")
    value_unit = vq.get("unit") or vq.get("code")
    value_string = fhir_resource.get("valueString")
    value_code = ((fhir_resource.get("valueCodeableConcept") or {})
                  .get("coding") or [{}])[0].get("code")

    # #773/#769: the datapoint's own context, ON the row.
    #
    # ingest_raw_guid was already a parameter here — used only as a fallback for
    # the row's guid and never stored — so `clinical_context`, which keys on it,
    # could not be joined to this table at all. Storing it is the repair.
    #
    # The others are read from the context block the producer sends, with the
    # same legacy aliases ClinicalContext accepts (#302), so a producer mid-way
    # through the deprecation window populates the row and the context table
    # identically rather than one of them.
    ctx = context or {}
    return LiveObservation(
        guid=guid,
        patient_guid=patient_guid,
        org_guid=org_guid,
        ingest_raw_guid=ingest_raw_guid,
        transaction_guid=(ctx.get("transaction_guid")
                          or ctx.get("activity_guid")),
        care_plan_guid=(ctx.get("care_plan_guid")
                        or ctx.get("careplan_guid")),
        # The organisation the PATIENT was affiliated with in ips at request
        # time (#768) — NOT the performer (that is org_guid) and NOT the
        # requester (requesting_org_guid). Three different organisations.
        #
        # #809: this is read from the SR context, and the old comment that
        # refused it has been removed rather than edited, because its stated
        # reason expired. It said "Left NULL until #774 resolves it from ips at
        # request time" — #774 COMPLETED (39a58f1) and nothing replaced this
        # line, so the column was NULL on every row in cdr1/cdr2/cdr3.
        #
        # The worry the old comment encoded was right but aimed at a different
        # case. It refused a PRODUCER-ASSERTED organisation, and that refusal
        # still stands: writing whatever a producer called "the organisation"
        # here is exactly the conflation #768 documents. The value arriving
        # here is not that. request.pdhc resolves it from ips and SNAPSHOTS it
        # onto the service_request at creation; gateway forwards that SR
        # payload verbatim (`dict(self._data)`, which is why gateway has no
        # textual hit for the field). And every route that reaches this
        # function is @require_service_key — so the only callers are
        # gateway.pdhc and sim.pdhc, never an arbitrary producer.
        #
        # Deliberately NOT inferred from anything else when the context omits
        # it. The column fills FORWARD ONLY: ips's patient_clinic_assignments
        # has `assigned_at` and no end timestamp, so asking ips later returns
        # TODAY's answer for an old request. NULL correctly means "nobody
        # recorded it", and that is why `flask backfill-live-observations`
        # passes no context and still writes NULL — a historical row whose
        # affiliation was never captured must stay unscopable rather than
        # acquire a plausible guess.
        #
        # The generic FHIR write path (services/resource_writer.py) still
        # leaves this NULL on purpose; see the #809 response on the board.
        patient_org_guid=ctx.get("patient_org_guid"),
        # #1014: the same value as clinical_context, on the row the series
        # read uses. An invalid value never reaches here (refused in process).
        data_origin=_origin.check(ctx.get("data_origin"))[0],
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
