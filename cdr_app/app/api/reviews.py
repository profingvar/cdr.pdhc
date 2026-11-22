"""#1044: the review surface for patient-reported EPS answers (contract s4).

Mounted at /api/v1/reviews, on the CLINICAL repository only (the same role
flag as /api/v1/clinical, CDR_CLINICAL_READ): cdr2/cdr3 are analysis
repositories, hold no review queue, and do not have these routes at all.

TWO DOORS, and neither opens the other's routes:

  the professional's   GET  /api/v1/reviews
                       POST /api/v1/reviews/<guid>/approve
                       POST /api/v1/reviews/<guid>/reject
      `Authorization: Bearer <sso token>`, validated with sso.pdhc on every
      request (never cached). The dashboard forwards the OPERATOR'S OWN
      bearer, so the person deciding is the person sso authenticated. A
      service key is not looked at here: no machine identity may decide.

  #1049 (s7.4), the same door, one card version as a whole:
                       GET  /api/v1/reviews/cards
                       POST /api/v1/reviews/cards/<card_version>/approve
                       POST /api/v1/reviews/cards/<card_version>/reject
      The per-row routes above keep serving v1 answers (no card_version)
      and refuse a card answer, so a version is never decided piecemeal.

  the patient app's    GET  /api/v1/reviews/by-patient/<patient guid>
                       GET  /api/v1/reviews/card/by-patient/<patient guid>
      `X-Source-Service: patientapp.pdhc` + `X-Service-Key`, checked against
      PATIENT_STATUS_SERVICES below and nothing else. That name is in
      NEITHER of the CDR's two other allowlists (the ingest dict in
      app/api/auth.py, KNOWN_FHIR_SERVICES in app/auth.py), so the key opens
      this one GET and no other route. An unset key admits nobody.

The request loader treats /api/v1/reviews as a path that authenticates
itself (auth._public_path), exactly like ingest and the external read door:
its session-cookie + analysis-phase gate is the wrong question for both
doors.

Refusals a caller could use to probe are flat: a guid that does not exist, a
row that is not a review item and a row outside the caller's care units all
answer the SAME 403 body.
"""
from __future__ import annotations

import hmac
import logging

from flask import Blueprint, current_app, g, jsonify, request

from app import db
from app.models.resources import live_model
from app.services import review as rv
from app.services.read_audit import record_read_audit

logger = logging.getLogger(__name__)
bp = Blueprint("reviews", __name__)

#: The ONLY service identities admitted here, and only on the by-patient GET.
#: Not KNOWN_FHIR_SERVICES: that list grants the whole FHIR surface.
PATIENT_STATUS_SERVICES = {
    "patientapp.pdhc": "PATIENTAPP_PDHC_SERVICE_KEY",
}

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000

_FORBIDDEN_BODY = {"error": "forbidden",
                   "message": "no such review item in your care units"}


def _err(status: int, error: str, message: str, **extra):
    return jsonify(dict(error=error, message=message, **extra)), status


def _iso(dt):
    return dt.isoformat() if dt is not None else None


# ---------------------------------------------------------------------------
# Door 1: the professional
# ---------------------------------------------------------------------------

def _professional():
    """(blob, None) for an authenticated professional, else (None, response).

    AUTH_MODE=off (local development only, never deployed) yields the dev
    blob, which is an SU admin with no affiliation: it can list and cannot
    decide, so the decision gate is still the real one.
    """
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
    if blob.get("user_type") != "professional":
        return None, _err(403, "not_professional",
                          "reviews are for care professionals")
    g.access_blob = blob          # for the read audit, like every other read
    return blob, None


def _actor(blob: dict) -> dict:
    return {"user_guid": blob.get("user_guid"),
            "name": (blob.get("display_name") or blob.get("email")
                     or blob.get("user_guid") or "")[:255]}


def _item(row, endorsements: dict, groups: dict) -> dict:
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    _key, value, unit = rv.value_of(raw)
    link_id = rv.link_id_of(row)
    return {
        "guid": row.guid,
        "patient_guid": row.patient_guid,
        "patient_org_guid": row.patient_org_guid,
        "section": rv.section_of(link_id),
        "link_id": link_id,
        # Which instance of a repeating group (one allergy = several rows);
        # null when the producer sent none.
        "group": groups.get(row.ingest_raw_guid),
        "label": rv.label_of(raw),
        "code": row.code_canonical,
        "value": value,
        "unit": unit,
        "recorded_at": _iso(row.effective_at),
        "submitted_at": _iso(row.received_at),
        "data_origin": row.data_origin,
        "review_status": row.review_status,
        "review_reason": row.review_reason,
        "reviewed_by_user_guid": row.reviewed_by_user_guid,
        "reviewed_by_name": row.reviewed_by_name,
        "reviewed_at": _iso(row.reviewed_at),
        "endorsement_guid": endorsements.get(row.guid),
    }


@bp.get("")
def list_reviews():
    """The queue. `status` = pending (default) | endorsed | rejected | all.

    A deciding professional sees the originals of patients listed at their
    care units. An SU admin sees all (and can decide none of them unless
    also affiliated). Anyone else has no scope and is refused: an empty
    scope is never "everything".
    """
    blob, refused = _professional()
    if refused:
        return refused
    units = rv.review_units(blob)
    is_admin = bool(blob.get("is_su_admin"))
    if not units and not is_admin:
        return _err(403, "no_review_scope",
                    "you are not affiliated to a care unit as doctor or nurse")

    status = (request.args.get("status") or rv.PENDING).strip().lower()
    if status != "all" and status not in rv.STATUSES:
        return _err(400, "invalid_status",
                    "status must be pending, endorsed, rejected or all")
    try:
        limit = max(1, min(_MAX_LIMIT, int(request.args.get("limit", _DEFAULT_LIMIT))))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT

    Obs = live_model("Observation")
    # #1049: card answers have their own queue (/cards); v1 is the rest.
    q = Obs.query.filter(Obs.review_status.isnot(None),
                         Obs.card_version.is_(None))
    if status != "all":
        q = q.filter(Obs.review_status == status)
    if not is_admin:
        q = q.filter(Obs.patient_org_guid.in_(list(units)))
    patient = (request.args.get("patient_guid") or "").strip()
    if patient:
        q = q.filter(Obs.patient_guid == patient)
    total = q.count()
    rows = q.order_by(Obs.received_at.asc(), Obs.guid.asc()).limit(limit).all()
    endorsements = rv.endorsement_guids(
        [r.guid for r in rows if r.review_status == rv.ENDORSED])
    groups = rv.groups_of([r.ingest_raw_guid for r in rows])
    items = [_item(r, endorsements, groups) for r in rows]
    for it in items:
        it["can_decide"] = (it["review_status"] == rv.PENDING
                            and it["patient_org_guid"] in units)
    record_read_audit(resource_type="Observation", patient_guid=patient or None,
                      n_rows=len(items), detail={"review_status": status})
    return jsonify(items=items, total=total, limit=limit,
                   truncated=total > len(items)), 200


def _decidable(guid: str, action: str):
    """(blob, row, unit, None) when the caller may decide on `guid`, else
    (None, None, None, response). The scope check comes BEFORE the state
    check, so `already_decided` is told only to someone entitled to know."""
    blob, refused = _professional()
    if refused:
        return None, None, None, refused
    Obs = live_model("Observation")
    row = Obs.query.filter(Obs.guid == guid,
                           Obs.review_status.isnot(None)).first()
    unit = rv.may_decide(blob, row.patient_org_guid) if row is not None else None
    if unit is None:
        logger.warning("review %s refused: user %s on %s (%s)", action,
                       blob.get("user_guid"), guid,
                       "no such item" if row is None else "not affiliated")
        db.session.rollback()
        rv.audit_denied(actor=_actor(blob), guid=guid, action=action,
                        why="not_found" if row is None else "not_affiliated")
        return None, None, None, (jsonify(_FORBIDDEN_BODY), 403)
    if row.card_version is not None:
        # #1049: a card answer is decided with its whole version.
        return None, None, None, _err(
            409, "card_row",
            "this answer belongs to a card version; decide the version at "
            "/api/v1/reviews/cards/<card_version>",
            card_version=row.card_version)
    if row.review_status != rv.PENDING:
        return None, None, None, _err(
            409, "already_decided",
            f"this answer is already {row.review_status}")
    return blob, row, unit, None


@bp.post("/<guid>/approve")
def approve(guid):
    blob, row, unit, refused = _decidable(guid, "approve")
    if refused:
        return refused
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    note = body.get("note")
    if note is not None and (not isinstance(note, str)
                             or len(note) > rv.MAX_REASON):
        return _err(422, "validation", "note must be text of at most "
                                       f"{rv.MAX_REASON} characters", code="wrong_type")
    try:
        endorsement, amended = rv.approve(
            row, actor=_actor(blob), unit=unit, value=body.get("value"),
            note=(note or "").strip() or None)
    except rv.InvalidValue as exc:
        return _err(422, "validation", exc.message, code=exc.code)
    except rv.AlreadyDecided:
        return _err(409, "already_decided", "this answer is already decided")
    return jsonify(endorsement_guid=endorsement, amended=amended,
                   original_guid=guid, review_status=rv.ENDORSED), 201


@bp.post("/<guid>/reject")
def reject(guid):
    blob, row, unit, refused = _decidable(guid, "reject")
    if refused:
        return refused
    body = request.get_json(silent=True)
    reason = body.get("reason") if isinstance(body, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        return _err(422, "reason_required",
                    "a rejection needs a reason; the patient sees it")
    if len(reason) > rv.MAX_REASON:
        return _err(422, "validation", "reason must be at most "
                                       f"{rv.MAX_REASON} characters", code="out_of_range")
    try:
        rv.reject(row, actor=_actor(blob), unit=unit, reason=reason.strip())
    except rv.AlreadyDecided:
        return _err(409, "already_decided", "this answer is already decided")
    return jsonify(guid=guid, review_status=rv.REJECTED), 200


# ---------------------------------------------------------------------------
# Door 2: the patient app (status only)
# ---------------------------------------------------------------------------

def _patient_status_service() -> str | None:
    """The admitted service name, or None. Its own allowlist; an unset key
    admits nobody; constant-time compare."""
    source = request.headers.get("X-Source-Service", "").strip()
    key = request.headers.get("X-Service-Key", "").strip()
    cfg = PATIENT_STATUS_SERVICES.get(source)
    if not cfg or not key:
        return None
    expected = current_app.config.get(cfg, "") or ""
    if not expected:
        logger.warning("patient-status refused: %s is allowlisted but %s is "
                       "unset in this instance", source, cfg)
        return None
    if not hmac.compare_digest(key.encode(), expected.encode()):
        return None
    return source


@bp.get("/by-patient/<patient_guid>")
def by_patient(patient_guid):
    """What became of ONE patient's EPS answers (contract s4.3 / s2.3).

    Only that patient's reviewable originals, and only what they may see of
    them: their own answer as a short text, the status, the reason for a
    rejection and when it was decided. Not who decided, and not the value
    care endorsed instead.
    """
    service = _patient_status_service()
    if service is None:
        return _err(401, "invalid_service_credentials",
                    "this route needs the patient app's service key")
    g.access_blob = {"user_type": "service", "service_source": service}
    Obs = live_model("Observation")
    rows = (Obs.query
            .filter(Obs.patient_guid == patient_guid,
                    Obs.review_status.isnot(None),
                    Obs.card_version.is_(None))    # #1049: card has its own
            .order_by(Obs.received_at.asc(), Obs.guid.asc()).all())
    items = []
    for row in rows:
        raw = row.raw_json if isinstance(row.raw_json, dict) else {}
        items.append({
            "original_guid": row.guid,
            "section": rv.section_of(rv.link_id_of(row)),
            "summary": rv.summary_of(raw),
            "status": row.review_status,
            "reason": (row.review_reason
                       if row.review_status == rv.REJECTED else None),
            "decided_at": _iso(row.reviewed_at),
        })
    record_read_audit(resource_type="Observation", patient_guid=patient_guid,
                      n_rows=len(items), detail={"view": "patient_review_status"})
    resp = jsonify(items=items)
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200


# ---------------------------------------------------------------------------
# #1049: the patient card, one version at a time (contract s7.4)
# ---------------------------------------------------------------------------

def _card_row(row, groups: dict) -> dict:
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    _key, value, _unit = rv.value_of(raw)
    return {"guid": row.guid, "link_id": rv.link_id_of(row),
            "group": groups.get(row.ingest_raw_guid),
            "label": rv.label_of(raw), "value": value,
            "display": rv.display_of(raw)}


def _endorsed_block(patient_guid: str, section: str):
    """For the reviewer's diff: the currently endorsed version, with the
    values care ENDORSED (an amendment shows here). `original_guid` ties
    each endorsement row to the patient's answer it endorses."""
    cur = rv.endorsed_card(patient_guid, section)
    if cur is None:
        return None
    groups = rv.groups_of([r.ingest_raw_guid for r in cur["endorsements"]])
    rows = []
    for e in cur["endorsements"]:
        item = _card_row(e, groups)
        item["original_guid"] = e.endorses_guid
        rows.append(item)
    return {"card_version": cur["card_version"],
            "reviewed_at": _iso(cur["reviewed_at"]), "rows": rows}


@bp.get("/cards")
def list_cards():
    """Card versions. `status` = pending (default) | endorsed | rejected |
    superseded | all. Same gate and scope as the per-row queue."""
    blob, refused = _professional()
    if refused:
        return refused
    units = rv.review_units(blob)
    is_admin = bool(blob.get("is_su_admin"))
    if not units and not is_admin:
        return _err(403, "no_review_scope",
                    "you are not affiliated to a care unit as doctor or nurse")
    status = (request.args.get("status") or rv.PENDING).strip().lower()
    if status != "all" and status not in rv.CARD_STATUSES:
        return _err(400, "invalid_status", "status must be pending, endorsed, "
                                           "rejected, superseded or all")
    try:
        limit = max(1, min(_MAX_LIMIT, int(request.args.get("limit", _DEFAULT_LIMIT))))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT

    Obs = live_model("Observation")
    q = Obs.query.filter(Obs.card_version.isnot(None),
                         Obs.review_status.isnot(None))
    if status != "all":
        q = q.filter(Obs.review_status == status)
    if not is_admin:
        q = q.filter(Obs.patient_org_guid.in_(list(units)))
    patient = (request.args.get("patient_guid") or "").strip()
    if patient:
        q = q.filter(Obs.patient_guid == patient)
    versions = rv.card_versions(q.all())
    total = len(versions)
    versions = versions[:limit]
    endorsed_cache: dict = {}
    items = []
    for v in versions:
        key = (v["patient_guid"], v["section"])
        if key not in endorsed_cache:
            endorsed_cache[key] = _endorsed_block(*key)
        endorsed = endorsed_cache[key]
        items.append({
            "card_version": v["card_version"],
            "patient_guid": v["patient_guid"],
            "patient_org_guid": v["patient_org_guid"],
            "section": v["section"],
            "status": v["status"],
            "submitted_at": _iso(v["submitted_at"]),
            "decided_at": _iso(v["decided_at"]),
            "reason": v["reason"],
            "rows": [_card_row(r, v["groups"]) for r in v["originals"]],
            # The version currently in force, unless it is this one.
            "endorsed": (endorsed if endorsed is None
                         or endorsed["card_version"] != v["card_version"]
                         else None),
            "can_decide": (v["status"] == rv.PENDING
                           and v["patient_org_guid"] in units),
        })
    record_read_audit(resource_type="Observation", patient_guid=patient or None,
                      n_rows=sum(len(i["rows"]) for i in items),
                      detail={"view": "card_versions", "review_status": status})
    return jsonify(items=items, total=total, limit=limit,
                   truncated=total > len(items)), 200


def _card_decidable(card_version: str, action: str):
    """(blob, rows, unit, None) when the caller may decide this version,
    else (None, None, None, response). Scope BEFORE state, and an unknown
    version answers the same flat 403 as one outside the caller's units."""
    blob, refused = _professional()
    if refused:
        return None, None, None, refused
    rows = rv.card_rows(card_version)
    units_of_rows = {r.patient_org_guid for r in rows}
    unit = (rv.may_decide(blob, units_of_rows.pop())
            if len(units_of_rows) == 1 else None)
    if unit is None:
        logger.warning("card %s refused: user %s on %s (%s)", action,
                       blob.get("user_guid"), card_version,
                       "no such version" if not rows else "not affiliated")
        db.session.rollback()
        rv.audit_denied(actor=_actor(blob), guid=card_version, action=action,
                        why="not_found" if not rows else "not_affiliated")
        return None, None, None, (jsonify(_FORBIDDEN_BODY), 403)
    state = rv.card_state(rows)
    if state == rv.SUPERSEDED:
        return None, None, None, _err(
            409, "superseded", "a newer version of this section was sent; "
                               "decide that one")
    if state != rv.PENDING:
        return None, None, None, _err(
            409, "already_decided",
            f"this card version is already {state or 'decided'}")
    return blob, rows, unit, None


def _closed(exc):
    if isinstance(exc, rv.Superseded):
        return _err(409, "superseded", "a newer version of this section was sent")
    return _err(409, "already_decided", "this card version is already decided")


@bp.post("/cards/<card_version>/approve")
def approve_card(card_version):
    blob, rows, unit, refused = _card_decidable(card_version, "approve")
    if refused:
        return refused
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    note = body.get("note")
    if note is not None and (not isinstance(note, str)
                             or len(note) > rv.MAX_REASON):
        return _err(422, "validation", "note must be text of at most "
                                       f"{rv.MAX_REASON} characters", code="wrong_type")
    amend = body.get("amend")
    if amend is not None and not isinstance(amend, dict):
        return _err(422, "validation", "amend must map a row guid to its "
                                       "value", code="wrong_type")
    try:
        endorsed, amended, guids = rv.approve_card(
            rows, actor=_actor(blob), unit=unit, amend=amend,
            note=(note or "").strip() or None)
    except rv.InvalidValue as exc:
        return _err(422, "validation", exc.message, code=exc.code)
    except rv.AlreadyDecided as exc:
        return _closed(exc)
    return jsonify(endorsed=endorsed, amended=amended,
                   card_version=rows[0].card_version,
                   endorsement_guids=guids, review_status=rv.ENDORSED), 201


@bp.post("/cards/<card_version>/reject")
def reject_card(card_version):
    blob, rows, unit, refused = _card_decidable(card_version, "reject")
    if refused:
        return refused
    body = request.get_json(silent=True)
    reason = body.get("reason") if isinstance(body, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        return _err(422, "reason_required",
                    "a rejection needs a reason; the patient sees it")
    if len(reason) > rv.MAX_REASON:
        return _err(422, "validation", "reason must be at most "
                                       f"{rv.MAX_REASON} characters", code="out_of_range")
    try:
        n = rv.reject_card(rows, actor=_actor(blob), unit=unit,
                           reason=reason.strip())
    except rv.AlreadyDecided as exc:
        return _closed(exc)
    return jsonify(card_version=rows[0].card_version, rejected=n,
                   review_status=rv.REJECTED), 200


@bp.get("/card/by-patient/<patient_guid>")
def card_by_patient(patient_guid):
    """Mitt patientkort, for the patient app (contract s7.4): per section the
    LATEST version sent (not superseded) and the version currently endorsed.

    The patient's OWN answers only: both blocks are built from the
    originals, never from care's endorsement rows, so an amended value is
    not shown. Who decided is never returned; `reason` only on a rejection.
    """
    service = _patient_status_service()
    if service is None:
        return _err(401, "invalid_service_credentials",
                    "this route needs the patient app's service key")
    g.access_blob = {"user_type": "service", "service_source": service}
    Obs = live_model("Observation")
    rows = (Obs.query
            .filter(Obs.patient_guid == patient_guid,
                    Obs.card_version.isnot(None),
                    Obs.review_status.isnot(None))   # originals only
            .all())
    versions = rv.card_versions(rows)
    sections = {}
    n = 0
    for section in rv.SECTIONS:
        mine = [v for v in versions if v["section"] == section]
        live = [v for v in mine if v["status"] in
                (rv.PENDING, rv.ENDORSED, rv.REJECTED)]
        latest = live[-1] if live else None          # newest submission
        # The version in force: the endorsed one decided last (s7.2 orders
        # by reviewed_at too). An endorsed version always has decided_at.
        endorsed = max((v for v in mine if v["status"] == rv.ENDORSED),
                       key=lambda v: (v["decided_at"], v["card_version"]),
                       default=None)
        block = {"latest": None, "endorsed": None}
        if latest is not None:
            block["latest"] = {
                "card_version": latest["card_version"],
                "submitted_at": _iso(latest["submitted_at"]),
                "status": latest["status"],
                "reason": (latest["reason"]
                           if latest["status"] == rv.REJECTED else None),
                "decided_at": (_iso(latest["decided_at"])
                               if latest["status"] != rv.PENDING else None),
                "answers": [rv.answer_of(r, latest["groups"])
                            for r in latest["originals"]],
            }
            n += len(latest["originals"])
        if endorsed is not None:
            block["endorsed"] = {
                "card_version": endorsed["card_version"],
                "decided_at": _iso(endorsed["decided_at"]),
                "answers": [rv.answer_of(r, endorsed["groups"])
                            for r in endorsed["originals"]],
            }
        sections[section] = block
    record_read_audit(resource_type="Observation", patient_guid=patient_guid,
                      n_rows=n, detail={"view": "patient_card_status"})
    resp = jsonify(sections=sections)
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200
