"""#984: `POST /api/v1/read-audit/events` -- a refusal that never reached the CDR.

Operator decision 2026-11-17: `cdr_read_audit` is THE ONE access log for
diagnoses. A read that a client service REFUSED (patient access denied) never
reaches the CDR's read surface, so without this endpoint it would be in no log
the platform treats as authoritative. icd11.pdhc posts each such refusal here.

What it is and is not:
  - service key only, and only a source in `read_audit.EVENT_SERVICES`
    (403 for any other service, and for an operator session);
  - APPEND-ONLY: one INSERT into `cdr_read_audit`, nothing else; there is no
    GET/PUT/DELETE on this path, and the table itself refuses UPDATE/DELETE
    (app/models/read_audit_guard.py);
  - NEVER a read of patient data: no repository table is queried;
  - validated strictly, every refusal 400 by NAME (`code`); `detail` is a
    closed set of metadata keys, so clinical content has nowhere to go.

Body:
    {"patient_guid": "...", "user_guid": "...", "outcome": "denied"|"break_glass",
     "route": "GET /api/v1/episodes/<guid>/history", "detail": {...}}

`user_guid` is a recorded claim of the calling service, like the forwarded
`X-Operator-Session-Id` (stored as `session_id`).
"""
from __future__ import annotations

import logging
import re

from flask import Blueprint, g, jsonify, request

from app import db
from app.models import ReadAudit
from app.services.read_audit import EVENT_SERVICES, MAX_REASON, USER_GUID_MAX

log = logging.getLogger(__name__)
bp = Blueprint("read_audit_events", __name__)

EVENT_OUTCOMES = ("denied", "break_glass")
_FIELDS = {"patient_guid", "user_guid", "outcome", "route", "detail"}
_IDENT = re.compile(r"^[A-Za-z0-9._:-]+$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
#: detail key -> max length of its string value. Metadata only.
_DETAIL_KEYS = {
    "reason": MAX_REASON,     # the break-glass reason the user typed
    "as_of": 64,              # an ISO instant
    "scope": 64,              # "unrestricted" / "organisation"
    "refusal_code": 64,       # e.g. PATIENT_ACCESS_DENIED
    "action": 64,             # e.g. history / assert / diagnosis-review
    "resource": 128,          # e.g. Condition/<guid>, ServiceRequest/<guid>
}


class _Bad(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _err(code: str, message: str, status: int):
    return jsonify({"error": message, "code": code}), status


def _ident(body: dict, name: str, max_len: int) -> str:
    v = body.get(name)
    if not isinstance(v, str) or not v.strip():
        raise _Bad(f"{name.upper()}_REQUIRED", f"{name} is required (a non-empty string)")
    v = v.strip()
    if len(v) > max_len or not _IDENT.match(v):
        raise _Bad(f"{name.upper()}_INVALID",
                   f"{name} must be an identifier of at most {max_len} characters")
    return v


def _validate(body) -> dict:
    if not isinstance(body, dict):
        raise _Bad("BODY_NOT_OBJECT", "the body must be a JSON object")
    unknown = sorted(set(body) - _FIELDS)
    if unknown:
        raise _Bad("UNKNOWN_FIELD", f"unknown field(s): {', '.join(unknown)}")
    out = {
        "patient_guid": _ident(body, "patient_guid", 64),
        "user_guid": _ident(body, "user_guid", USER_GUID_MAX),
    }
    outcome = body.get("outcome")
    if outcome not in EVENT_OUTCOMES:
        raise _Bad("OUTCOME_INVALID",
                   f"outcome must be one of {', '.join(EVENT_OUTCOMES)} "
                   f"(a served read is logged by the read itself)")
    out["outcome"] = outcome
    route = body.get("route")
    if not isinstance(route, str) or not route.strip():
        raise _Bad("ROUTE_REQUIRED", "route is required (a non-empty string)")
    if len(route) > 200 or any(c in route for c in "\r\n\t"):
        raise _Bad("ROUTE_INVALID", "route must be one line of at most 200 characters")
    out["route"] = route.strip()

    detail = body.get("detail", {})
    if detail is None:
        detail = {}
    if not isinstance(detail, dict):
        raise _Bad("DETAIL_NOT_OBJECT", "detail must be a JSON object")
    bad_keys = sorted(set(detail) - set(_DETAIL_KEYS))
    if bad_keys:
        raise _Bad("DETAIL_UNKNOWN_KEY",
                   f"detail may carry only {', '.join(sorted(_DETAIL_KEYS))}; "
                   f"got {', '.join(bad_keys)} (never clinical content)")
    clean = {}
    for k, v in detail.items():
        if v is None:
            continue
        if not isinstance(v, str):
            raise _Bad("DETAIL_VALUE_INVALID", f"detail.{k} must be a string")
        if len(v) > _DETAIL_KEYS[k]:
            raise _Bad("DETAIL_VALUE_TOO_LONG",
                       f"detail.{k} is longer than {_DETAIL_KEYS[k]} characters")
        clean[k] = v
    if "refusal_code" in clean and not _CODE.match(clean["refusal_code"]):
        raise _Bad("DETAIL_VALUE_INVALID", "detail.refusal_code must be an UPPER_SNAKE name")
    if outcome == "break_glass" and not (clean.get("reason") or "").strip():
        raise _Bad("REASON_REQUIRED", "a break_glass event must carry detail.reason")
    clean["recorded_via"] = "event"
    out["detail"] = clean
    return out


@bp.post("/read-audit/events")
def post_event():
    blob = getattr(g, "access_blob", None) or {}
    source = blob.get("service_source") if isinstance(blob, dict) else None
    if not source:
        return _err("SERVICE_KEY_REQUIRED",
                    "read-audit events are accepted from a service key only", 403)
    if source not in EVENT_SERVICES:
        log.warning("read-audit event REFUSED from service %r -- not in "
                    "EVENT_SERVICES (#984)", source)
        return _err("SERVICE_NOT_PERMITTED",
                    f"service {source!r} may not post read-audit events", 403)
    try:
        ev = _validate(request.get_json(silent=True))
    except _Bad as exc:
        return _err(exc.code, exc.message, 400)

    sid = request.headers.get("X-Operator-Session-Id")
    row = ReadAudit(
        caller_service=source,
        on_behalf_of_user_guid=ev["user_guid"],
        route=ev["route"],
        patient_guid=ev["patient_guid"],
        response_status=403 if ev["outcome"] == "denied" else 200,
        outcome=ev["outcome"],
        detail=ev["detail"],
        session_id=sid[:128] if sid else None,
    )
    try:
        db.session.add(row)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 -- named, never swallowed here
        db.session.rollback()
        log.error("read-audit event write FAILED (%s): %s", source, exc)
        return _err("AUDIT_WRITE_FAILED", "the event could not be written", 503)
    return jsonify({"id": row.id, "outcome": row.outcome}), 201
