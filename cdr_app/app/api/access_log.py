"""#1045 loggkontroll: ONE patient's access log from this CDR, for a PERSON.

    GET /api/v1/access-log/patients/<patient guid>
        ?from=<ISO instant>  ?to=<ISO instant, exclusive>  ?limit=<n>

Patientdatalagen 4 kap. 3 §: a care provider checks, systematically and
repeatedly, who has accessed a patient's records. dashboard.pdhc merges the
logs of the services holding the patient's data; this is the CDR's part:

  * `cdr_read_audit` — every patient-touching read (served, denied,
    break_glass), including reads services made on a user's behalf
    (`on_behalf_of_user_guid`, a recorded claim of that service);
  * `ingest_raw`     — every write that arrived (which service, when);
  * review decisions — who endorsed or rejected a patient's answer (#1044).

THE DOOR is the professional's own SSO bearer (`Authorization: Bearer`),
validated with sso.pdhc on every request, never cached — the reviews door
(app/api/reviews.py). A service key is NOT looked at here, so no machine can
read who looked at a patient: the loader's service-key path would make the
caller SU-equivalent and organisation-blind (`_service_blob`).

THE GATE (Rule 24, CLAUDE.md s11):
  * an SU admin reads any patient's log;
  * a professional reads it only when the patient has data in this CDR
    listed at one of their affiliated care units
    (`auth.patient_scope_clause`, THE predicate the clinical reads use:
    `patient_org_guid` in `affiliations[].care_unit_guid`);
  * anyone else, or a patient outside that scope: the SAME flat 403 as a
    patient this CDR does not hold, so probing learns nothing.

The read of the log is itself logged in `cdr_read_audit`
(resource_type `ReadAudit`), the refusal included (`outcome='denied'`).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from flask import Blueprint, current_app, g, jsonify, request

from app import db
from app.auth import patient_scope_clause, scope_org_guids
from app.models import IngestRaw, ReadAudit
from app.models.resources import _RESOURCE_MODELS
from app.services.read_audit import record_read_audit

bp = Blueprint("access_log", __name__)

DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000

_FORBIDDEN = {"error": "forbidden",
              "message": "no access log for that patient in your care units"}


def _err(status: int, error: str, message: str):
    return jsonify({"error": error, "message": message}), status


def _person():
    """(blob, None) for an authenticated SU or professional, else
    (None, response). AUTH_MODE=off yields the dev SU (local only)."""
    from app.auth import _DEV_BLOB, validate_sso_token

    if current_app.config.get("AUTH_MODE", "off") == "off":
        blob = dict(_DEV_BLOB)
    else:
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        if not token:
            return None, _err(401, "not_authenticated",
                              "send the operator's SSO token as a Bearer")
        blob = validate_sso_token(token)
        if not isinstance(blob, dict):
            return None, _err(401, "not_authenticated",
                              "the SSO token was not accepted")
    if blob.get("must_change_password"):
        return None, _err(403, "must_change_password",
                          "change your password in SSO first")
    if not blob.get("is_su_admin") and blob.get("user_type") != "professional":
        return None, _err(403, "not_professional",
                          "the access log is for care professionals")
    g.access_blob = blob          # for record_read_audit
    return blob, None


def patient_in_scope(blob: dict, patient_guid: str) -> bool:
    """SU: yes. A professional: the patient has a row in this CDR whose
    PATIENT organisation is one of the caller's affiliated care units. An
    empty scope matches nothing."""
    if blob.get("is_su_admin"):
        return True
    units = scope_org_guids(blob)
    if not units:
        return False
    for Model in _RESOURCE_MODELS.values():
        hit = (db.session.query(Model.guid)
               .filter(Model.patient_guid == patient_guid)
               .filter(patient_scope_clause(Model, units))
               .first())
        if hit is not None:
            return True
    return False


def _instant(name: str):
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None, None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, f"{name} is not an ISO-8601 instant: {raw!r}"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt, None


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _in_range(q, col, since, until):
    if since is not None:
        q = q.filter(col >= since)
    if until is not None:
        q = q.filter(col < until)
    return q


def _read_item(r: ReadAudit) -> dict:
    detail = r.detail if isinstance(r.detail, dict) else {}
    user = r.caller_user_guid or r.on_behalf_of_user_guid
    return {
        "id": f"read:{r.id}",
        "at": _iso(r.timestamp),
        "user_guid": user,
        "user_label": None,
        "actor_type": "service" if r.caller_service and not user else "user",
        "via_service": r.caller_service,
        "action": f"read {r.resource_type}" if r.resource_type else "read",
        "outcome": r.outcome or ("denied" if r.response_status in (401, 403) else "served"),
        "route": r.route,
        "resource_type": r.resource_type,
        "session_id": r.session_id,
        # The reader's care units, recorded only when the CDR authenticated
        # the person itself; NULL for a read a service made on their behalf.
        "actor_units": r.caller_org_guids if r.caller_user_guid else None,
        "basis": "break_glass" if r.outcome == "break_glass" else r.access_basis,
        "detail": {k: str(detail[k])[:500] for k in ("reason", "refusal_code", "action")
                   if detail.get(k) is not None},
    }


def _ingest_item(r: IngestRaw) -> dict:
    return {
        "id": f"ingest:{r.guid}",
        "at": _iso(r.received_at),
        "user_guid": None,
        "user_label": r.source_service,
        "actor_type": "service",
        "via_service": r.source_service,
        "action": f"write {r.source_type}" if r.source_type else "write",
        "outcome": "served",
        "route": None,
        "resource_type": r.source_type,
        "session_id": None,
        "actor_units": None,
        "basis": None,
        "detail": {},
    }


def _review_item(row, rtype: str) -> dict:
    return {
        "id": f"review:{row.guid}",
        "at": _iso(row.reviewed_at),
        "user_guid": row.reviewed_by_user_guid,
        "user_label": row.reviewed_by_name,
        "actor_type": "user",
        "via_service": None,
        "action": f"review {row.review_status}",
        "outcome": "served",
        "route": None,
        "resource_type": rtype,
        "session_id": None,
        "actor_units": None,
        "basis": None,
        "detail": {},
    }


@bp.get("/access-log/patients/<patient_guid>")
def patient_access_log(patient_guid: str):
    blob, refused = _person()
    if refused:
        return refused
    try:
        guid = str(uuid.UUID(patient_guid))
    except (ValueError, TypeError):
        return _err(400, "invalid_patient_guid", "the patient guid is not a GUID")
    since, err1 = _instant("from")
    until, err2 = _instant("to")
    if err1 or err2:
        return _err(400, "invalid_range", err1 or err2)
    try:
        limit = max(1, min(MAX_LIMIT, int(request.args.get("limit", DEFAULT_LIMIT))))
    except (TypeError, ValueError):
        return _err(400, "invalid_limit", "limit is not a number")

    if not patient_in_scope(blob, guid):
        record_read_audit(resource_type="ReadAudit", patient_guid=guid,
                          response_status=403, outcome="denied",
                          detail={"refusal_code": "OUT_OF_SCOPE",
                                  "action": "access-log"})
        return jsonify(_FORBIDDEN), 403

    items: list[dict] = []
    reads = _in_range(ReadAudit.query.filter(ReadAudit.patient_guid == guid),
                      ReadAudit.timestamp, since, until)
    items += [_read_item(r) for r in reads.order_by(ReadAudit.timestamp.asc())
              .limit(limit + 1).all()]
    ingests = _in_range(IngestRaw.query.filter(IngestRaw.patient_guid == guid),
                        IngestRaw.received_at, since, until)
    items += [_ingest_item(r) for r in ingests.order_by(IngestRaw.received_at.asc())
              .limit(limit + 1).all()]
    for rtype, Model in _RESOURCE_MODELS.items():
        q = _in_range(Model.query.filter(Model.patient_guid == guid,
                                         Model.reviewed_at.isnot(None)),
                      Model.reviewed_at, since, until)
        items += [_review_item(r, rtype) for r in q.limit(limit + 1).all()]

    items.sort(key=lambda i: (i["at"] or "", i["id"]))
    truncated = len(items) > limit
    items = items[:limit]
    record_read_audit(resource_type="ReadAudit", patient_guid=guid,
                      n_rows=len(items),
                      detail={"action": "access-log"})
    return jsonify({"service": "cdr.pdhc", "patient_guid": guid,
                    "from": _iso(since), "to": _iso(until), "items": items,
                    "total": None, "limit": limit, "truncated": truncated})
