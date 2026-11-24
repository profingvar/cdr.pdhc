"""#1054: record.pdhc's WRITE door -- Encounter and ClinicalImpression only.

    POST /api/v1/record/fhir/<Encounter|ClinicalImpression>          create
    PUT  /api/v1/record/fhir/<Encounter|ClinicalImpression>/<guid>   new version

record.pdhc (the journal, plans/record_pdhc_plan.md s4.1) creates the
vårdkontakt (Encounter) and the medical decision (ClinicalImpression) here.
Everything else it needs it READS through /api/v1/fhir/*, where its entry in
KNOWN_FHIR_SERVICES is read-only (#760 narrowing) and confined to that prefix.

WHY A DOOR OF ITS OWN and not a write entry in KNOWN_FHIR_SERVICES: that dict
gates POST/PUT/Bundle for EVERY type, and the blob it mints is SU-equivalent
and organisation-blind. A per-type allowlist bolted onto it would have to be
re-checked inside the Bundle dispatcher, the upsert-by-body-id path and the
dedup path, each a way to land on a row of another type or another writer.
Here the whole surface is two verbs on two types, nothing else is routable
(no DELETE, no Bundle), and every request must say WHO it acts for.

Per request, all required:
  X-Source-Service: record.pdhc  +  X-Service-Key: RECORD_PDHC_SERVICE_KEY
      (unset key = this door admits nobody; D1: set on cdr1 only)
  X-Operator-User-Guid  the professional record authenticated (a GUID)
  X-Patient-Org-Guid    the patient's care unit (Rule 24 scopes on it)
Optional: X-Operator-User-Role, X-Org-Guid (performer org), X-Request-Id.

Rules:
  * POST always CREATES. A body id that already exists is 409; the dedup
    path is never used, so a POST can never land on another writer's row
    (an icd11 Encounter with the same start, say).
  * PUT makes version n+1 of a row record.pdhc itself wrote, for the same
    patient, and requires If-Match (428 without it, 412 when stale). The
    old version moves to *_history, which is append-only (history_guard).
  * Each accepted write keeps the request as it arrived in ingest_raw (so
    it is in the patient's access log, #1045) and an audit_log row
    `record.write` naming the acting professional. Refusals after a valid
    key are audited as `record.write.refused`.
"""
from __future__ import annotations

import hmac
import logging
import uuid

from flask import Blueprint, current_app, g, jsonify, request

from app import db
from app.models import AuditLog, IngestRaw
from app.models.resources import live_model
from app.services.ingest_pipeline import safe_headers

log = logging.getLogger(__name__)

bp = Blueprint("record_write", __name__)

SOURCE = "record.pdhc"
CONFIG_KEY = "RECORD_PDHC_SERVICE_KEY"
#: The ONLY types record.pdhc may write (plan s4.1).
RECORD_WRITE_TYPES = frozenset({"Encounter", "ClinicalImpression"})

def _outcome(code: str, text: str) -> dict:
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": code,
                       "details": {"text": text}}]}


def _guid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def authenticate() -> tuple[bool, tuple | None]:
    """(True, None) for record.pdhc with its key, else (False, response)."""
    source = (request.headers.get("X-Source-Service") or "").strip()
    key = (request.headers.get("X-Service-Key") or "").strip()
    if not source or not key:
        return False, (jsonify(_outcome("login", "missing X-Source-Service or "
                                        "X-Service-Key")), 401)
    expected = current_app.config.get(CONFIG_KEY, "") or ""
    if source != SOURCE:
        return False, (jsonify(_outcome("forbidden", "only record.pdhc writes "
                                        "through /api/v1/record")), 403)
    if not expected:
        log.warning("record write refused: %s is unset on this CDR, so the "
                    "door admits nobody (#1054, D1: cdr1 only)", CONFIG_KEY)
        return False, (jsonify(_outcome("forbidden", "invalid service key")), 403)
    if not hmac.compare_digest(key.encode(), expected.encode()):
        return False, (jsonify(_outcome("forbidden", "invalid service key")), 403)
    return True, None


def _audit(event: str, *, user, patient, snapshot: dict) -> None:
    db.session.add(AuditLog(
        event_type=event, actor_guid=user, data_subject_guid=patient,
        source_service=SOURCE,
        correlation_id=request.headers.get("X-Correlation-Id"),
        ip_address=request.remote_addr, payload_snapshot=snapshot))


def _refuse(status: int, code: str, text: str, *, user=None, patient=None,
            resource_type=None, guid=None):
    """Audit a refusal made AFTER the key was accepted, then answer."""
    try:
        _audit("record.write.refused", user=user, patient=patient,
               snapshot={"resource_type": resource_type, "guid": guid,
                         "status": status, "reason": text[:300],
                         "method": request.method})
        db.session.commit()
    except Exception:  # noqa: BLE001 -- the refusal stands either way
        db.session.rollback()
        log.warning("record.write.refused audit failed", exc_info=True)
    return jsonify(_outcome(code, text)), status


def _patient_of(fhir: dict) -> str | None:
    ref = ((fhir.get("subject") or {}).get("reference") or "").strip()
    if not ref.startswith("Patient/"):
        return None
    return _guid(ref.split("/", 1)[1])


@bp.post("/fhir/<resource_type>")
def create(resource_type: str):
    return _write(resource_type, None)


@bp.put("/fhir/<resource_type>/<guid>")
def update(resource_type: str, guid: str):
    return _write(resource_type, guid)


def _write(resource_type: str, guid: str | None):
    ok, resp = authenticate()
    if not ok:
        return resp
    user = _guid(request.headers.get("X-Operator-User-Guid"))
    porg = _guid(request.headers.get("X-Patient-Org-Guid"))
    fhir = request.get_json(silent=True)
    patient = _patient_of(fhir) if isinstance(fhir, dict) else None
    kw = dict(user=user, patient=patient, resource_type=resource_type, guid=guid)

    if resource_type not in RECORD_WRITE_TYPES:
        return _refuse(403, "forbidden",
                       f"record.pdhc may write only "
                       f"{', '.join(sorted(RECORD_WRITE_TYPES))}", **kw)
    if not user:
        return _refuse(422, "required", "X-Operator-User-Guid (the acting "
                       "professional, a GUID) is required on every write", **kw)
    if not porg:
        return _refuse(422, "required", "X-Patient-Org-Guid (the patient's "
                       "care unit, a GUID) is required on every write", **kw)
    if not isinstance(fhir, dict):
        return _refuse(400, "structure", "the body must be a FHIR resource", **kw)
    if fhir.get("resourceType") != resource_type:
        return _refuse(400, "structure", "resourceType does not match the URL", **kw)
    if not patient:
        return _refuse(422, "required", "subject must reference Patient/<guid>", **kw)

    Live = live_model(resource_type)
    if guid is None:                                     # POST: create only
        new_id = fhir.get("id")
        if new_id is not None:
            new_id = _guid(new_id)
            if not new_id:
                return _refuse(400, "value", "id must be a GUID", **kw)
            if db.session.get(Live, new_id) is not None:
                return _refuse(409, "conflict", f"{resource_type}/{new_id} exists; "
                               "PUT a new version with If-Match", **kw)
        else:
            new_id = str(uuid.uuid4())
        target = new_id
        if_match = None
    else:                                                # PUT: version n+1
        target = _guid(guid)
        if not target:
            return _refuse(400, "value", "the id in the URL is not a GUID", **kw)
        row = db.session.get(Live, target)
        if row is None:
            return _refuse(404, "not-found", f"{resource_type}/{target} does not "
                           "exist; POST creates", **kw)
        if row.source != SOURCE:
            return _refuse(409, "conflict", f"{resource_type}/{target} was not "
                           "written by record.pdhc and is not versioned here", **kw)
        if row.patient_guid != patient:
            return _refuse(409, "conflict", "a new version cannot move the "
                           "resource to another patient", **kw)
        if_match = (request.headers.get("If-Match") or "").strip()
        if not if_match:
            return _refuse(428, "required", "If-Match with the current version's "
                           "ETag is required", **kw)
    kw["guid"] = target
    fhir = dict(fhir)
    fhir["id"] = target

    from app.api.fhir_write import _write_one
    from app.services.resource_writer import WriteContext

    # The whole-system assertion gate (#857/#874) reads the caller from here;
    # record.pdhc is named for no library, so a code it may not assert is
    # refused exactly as for any other service.
    g.access_blob = {"service_source": SOURCE, "user_type": "service",
                     "is_su_admin": False, "user_guid": None}
    operation = "create" if guid is None else "update"
    try:
        raw_payload = {"resource_type": resource_type, "operation": operation,
                       "fhir_resource": fhir, "on_behalf_of_user_guid": user,
                       "on_behalf_role": (request.headers.get("X-Operator-User-Role")
                                          or None),
                       "patient_org_guid": porg}
        raw = IngestRaw(source_service=SOURCE,
                        source_system_id=request.headers.get("X-Request-Id"),
                        patient_guid=patient, payload_json=raw_payload,
                        payload_hash=IngestRaw.compute_hash(raw_payload),
                        headers_json=safe_headers(request.headers),
                        source_type="record_fhir")
        db.session.add(raw)
        db.session.flush()
        ctx = WriteContext(
            org_guid=(request.headers.get("X-Org-Guid") or "").strip() or None,
            patient_org_guid=porg, source=SOURCE,
            source_request_id=request.headers.get("X-Request-Id"),
            if_match_etag=if_match, ingest_raw_guid=raw.guid,
            mapping_version=current_app.config.get("MAPPING_VERSION"))
        body, status, headers = _write_one(fhir, ctx, update_by_guid=target,
                                           via_record=True)
        if status >= 400:
            db.session.rollback()
            text = ((body.get("issue") or [{}])[0].get("details") or {}).get("text") \
                if isinstance(body, dict) else None
            _refuse(status, "invalid", text or "refused by the write path", **kw)
            return jsonify(body), status
        _audit("record.write", user=user, patient=patient, snapshot={
            "resource_type": resource_type, "guid": target,
            "version_id": int((body.get("meta") or {}).get("versionId") or 0),
            "operation": operation, "patient_org_guid": porg,
            "ingest_raw_guid": raw.guid,
            "on_behalf_role": raw_payload["on_behalf_role"]})
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    resp = jsonify(body)
    resp.status_code = status
    for k, v in headers.items():
        resp.headers[k] = v
    return resp
