"""External read door: de-identified patient data under a time-limited grant.

Operator decision 2026-11-19. An SU admin issues a READ GRANT in sso.pdhc to
an external partner: valid at most 48 hours, bound to chosen patient
organisations and chosen CDRs. The partner sends it as
`Authorization: Bearer rg.<guid>.<secret>` to

    GET /external/v1/patients            the patients the grant covers
    GET /external/v1/patients/<guid>     one patient: IPS summary + datapoints
    GET /external/v1/patients/<guid>/$everything
                                         the same datapoints as a FHIR R5
                                         searchset Bundle (no Patient resource)

On EVERY request this CDR asks sso whether the key may be used here and now
(`/api/internal/read-grants/validate`, with this CDR's own SSO client pair),
so a revoke or an expiry takes effect immediately. Nothing is cached.

What the partner gets, by the operator's choice:
  * the REAL patient GUID (so data can be joined across CDRs). That makes this
    PSEUDONYMISED data, still personal data under GDPR -- not anonymous;
  * from ips: sex, birth year, diagnoses, medications (coded only);
  * every datapoint in this CDR for that patient: type, code, value, unit,
    the exact time, and `day` = whole days since the patient's first datapoint
    in this CDR (both ways of describing time).

Never: the Patient resource, a personnummer, a name, an address, raw_json, or
free-text notes. Fields are picked out, not filtered away, so a new column or
JSON field stays out until someone adds it here on purpose.

Scope, in order: the grant's organisations (on `patient_org_guid`, the Rule 24
column), then ips's consent verdict (#422) for the grant's OWN purpose and
research projects, chosen per key by the SU admin. (The first version asked
for purpose "research" with no projects, which ips answers with nobody.)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

import requests
from flask import Blueprint, current_app, g, jsonify, request

from app import db
from app.models.resources import live_model
from app.services.eps_settings import eps_setting

log = logging.getLogger(__name__)

bp = Blueprint("external_read", __name__)

#: Every patient-bearing type. Patient itself is deliberately absent.
DATA_TYPES = ("Observation", "QuestionnaireResponse", "Condition",
              "MedicationStatement", "MedicationRequest", "AllergyIntolerance",
              "Procedure", "Encounter", "DiagnosticReport")
MAX_PATIENTS = 5000
TIMEOUT = 8


class _Refuse(Exception):
    def __init__(self, status, error, message, **extra):
        super().__init__(message)
        self.status, self.body = status, {"error": error, "message": message, **extra}


def cdr_id() -> str:
    """This CDR's name in a grant. cdr1 sets no CDR_INSTANCE; cdr2/cdr3 set
    `cdr_pdhc_2` / `cdr_pdhc_3` (deploy/stamp.sh)."""
    inst = (os.environ.get("CDR_INSTANCE") or "").strip()
    if not inst or inst == "cdr_pdhc":
        return "cdr1"
    tail = inst.rsplit("_", 1)[-1]
    return f"cdr{tail}" if tail.isdigit() else inst


# ------------------------------------------------------------------- the grant

def _validate_grant() -> dict:
    auth = request.headers.get("Authorization", "")
    key = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not key.startswith("rg."):
        raise _Refuse(401, "unauthenticated", "Send a read key as 'Authorization: Bearer rg.…'")
    base = (current_app.config.get("SSO_BASE_URL") or "").rstrip("/")
    cid = current_app.config.get("SSO_CLIENT_ID") or ""
    sec = current_app.config.get("SSO_CLIENT_SECRET") or ""
    if not (base and cid and sec):
        raise _Refuse(503, "not_configured", "This CDR cannot check read keys (SSO not configured)")
    try:
        r = requests.post(f"{base}/api/internal/read-grants/validate",
                          json={"key": key, "cdr": cdr_id()},
                          headers={"X-SSO-Client-Id": cid, "X-SSO-Client-Secret": sec},
                          timeout=TIMEOUT)
    except requests.RequestException as e:
        log.warning("external read: sso unreachable: %s", e)
        raise _Refuse(503, "sso_unavailable", "The read key could not be checked; try again")
    if r.status_code == 200:
        return r.json()
    if r.status_code == 401:
        raise _Refuse(401, "invalid_key", "Unknown or wrong read key")
    if r.status_code == 403:
        reason = (r.json() or {}).get("reason") if r.headers.get("content-type", "").startswith("application/json") else None
        if reason:
            raise _Refuse(403, "forbidden", f"Read key refused: {reason}", reason=reason)
    log.error("external read: sso validate answered %s", r.status_code)
    raise _Refuse(503, "sso_error", "The read key could not be checked")


@bp.before_request
def _gate():
    try:
        grant = _validate_grant()
    except _Refuse as e:
        return jsonify(e.body), e.status
    if not grant.get("purpose_code"):
        return jsonify({"error": "forbidden", "message": "Read key has no purpose",
                        "reason": "no_purpose"}), 403
    g.read_grant = grant
    # Read-audit attribution: the caller is this grant, not a user or service.
    g.access_blob = {"service_source": f"read-grant:{grant['grant_guid']}"[:64],
                     "is_su_admin": False}
    return None


# ------------------------------------------------------------------- scope

def _orgs() -> list[str]:
    return list(g.read_grant.get("org_guids") or [])


def _patients_in_orgs() -> set[str]:
    orgs = _orgs()
    out: set[str] = set()
    if not orgs:
        return out
    for t in DATA_TYPES:
        M = live_model(t)
        if M is None:
            continue
        rows = (db.session.query(M.patient_guid)
                .filter(M.patient_org_guid.in_(orgs), M.patient_guid.isnot(None))
                .distinct().all())
        out.update(r[0] for r in rows)
    return out


def _consented(guids: set[str]) -> set[str]:
    """ips's research-consent verdict. Fails CLOSED: no verdict, no data."""
    if not guids:
        return set()
    from app.services.analysis_consent import IpsUnreachable, _analysis_filter
    try:
        verdict = _analysis_filter(sorted(guids), g.read_grant["purpose_code"],
                                   list(g.read_grant.get("research_project_guids") or []))
    except IpsUnreachable as e:
        raise _Refuse(503, "consent_unavailable",
                      f"Consent could not be checked, so nothing is served: {e}")
    return guids & set(verdict.get("allowed") or [])


def _audit(*, patient_guid=None, n_rows=None):
    from app.services.read_audit import record_read_audit
    record_read_audit(resource_type="External", patient_guid=patient_guid, n_rows=n_rows,
                      detail={"grant_guid": g.read_grant.get("grant_guid"),
                              "partner_guid": g.read_grant.get("partner_guid"),
                              "purpose": g.read_grant.get("purpose_code"),
                              "cdr": cdr_id()})


# ------------------------------------------------------------------- shaping

def _num(v):
    return float(v) if v is not None else None


def _codes(cc) -> list[str]:
    return [c.get("code") for c in ((cc or {}).get("coding") or [])
            if isinstance(c, dict) and c.get("code")]


def _datapoint(t: str, row) -> dict | None:
    raw = row.raw_json or {}
    if "entered-in-error" in _codes(raw.get("verificationStatus")) or raw.get("status") == "entered-in-error":
        return None
    d = {"resource_type": t, "guid": row.guid, "code": row.code_canonical,
         "effective_at": row.effective_at.isoformat() if row.effective_at else None}
    for col, key in (("value_quantity", "value"), ("value_unit", "unit"),
                     ("value_code", "value_code"), ("value_string", "value_string")):
        v = getattr(row, col, None)
        if v is not None:
            d[key] = _num(v) if col == "value_quantity" else v
    status = raw.get("status")
    if isinstance(status, str):
        d["status"] = status
    if t == "Condition":
        cs = _codes(raw.get("clinicalStatus"))
        if cs:
            d["clinical_status"] = cs[0]
    for col in ("care_plan_guid", "transaction_guid"):
        v = getattr(row, col, None)
        if v:
            d[col] = v
    return d


def _with_days(points: list[dict]) -> tuple[list[dict], str | None]:
    times = [p["effective_at"] for p in points if p["effective_at"]]
    if not times:
        return points, None
    first = min(datetime.fromisoformat(x) for x in times)
    for p in points:
        if p["effective_at"]:
            p["day"] = (datetime.fromisoformat(p["effective_at"]).date() - first.date()).days
    points.sort(key=lambda p: (p["effective_at"] or "", p["resource_type"], p["guid"]))
    return points, first.isoformat()


def _ips_summary(patient_guid: str) -> tuple[dict | None, str]:
    # #1024: EPS_* first, IPS_* as the deprecated fallback (eps_settings).
    base = eps_setting("BASE_URL").rstrip("/")
    key = eps_setting("API_KEY")
    if not (base and key):
        return None, "not_configured"
    try:
        r = requests.post(f"{base}/api/v1/patients/external-summary",
                          json={"patient_guids": [patient_guid],
                                "grant_guid": g.read_grant.get("grant_guid")},
                          headers={"Authorization": f"ApiKey {key}"}, timeout=TIMEOUT)
    except requests.RequestException:
        return None, "unavailable"
    if r.status_code != 200:
        log.warning("external read: ips external-summary answered %s", r.status_code)
        return None, f"ips_http_{r.status_code}"
    pts = (r.json() or {}).get("patients") or []
    return (pts[0] if pts else None), ("ok" if pts else "not_in_ips")


# ------------------------------------------------------------------- routes

@bp.get("/patients")
def list_patients():
    try:
        guids = sorted(_consented(_patients_in_orgs()))
    except _Refuse as e:
        return jsonify(e.body), e.status
    truncated = len(guids) > MAX_PATIENTS
    guids = guids[:MAX_PATIENTS]
    _audit(n_rows=len(guids))
    return jsonify({"cdr": cdr_id(), "expires_at": g.read_grant.get("expires_at"),
                    "purpose": g.read_grant.get("purpose_code"),
                    "count": len(guids), "truncated": truncated, "patients": guids})


def _patient_points(guid: str) -> tuple[list[dict], str | None] | None:
    """The patient's datapoints under this grant, or None when the grant does
    not cover the patient. Raises _Refuse when scope cannot be decided."""
    orgs = _orgs()
    if guid not in _consented({guid} & _patients_in_orgs_for(guid, orgs)):
        return None
    points = []
    for t in DATA_TYPES:
        M = live_model(t)
        if M is None:
            continue
        for row in (db.session.query(M)
                    .filter(M.patient_guid == guid, M.patient_org_guid.in_(orgs)).all()):
            p = _datapoint(t, row)
            if p:
                points.append(p)
    return _with_days(points)


@bp.get("/patients/<guid>")
def get_patient(guid: str):
    try:
        found = _patient_points(guid)
    except _Refuse as e:
        return jsonify(e.body), e.status
    if found is None:
        _audit(patient_guid=guid, n_rows=0)
        # Same answer whether the patient is absent, outside the grant or
        # opted out: the grant learns nothing about patients it does not cover.
        return jsonify({"error": "not_found", "message": "No such patient under this read key"}), 404
    points, first = found
    ips, ips_status = _ips_summary(guid)
    _audit(patient_guid=guid, n_rows=len(points))
    return jsonify({"cdr": cdr_id(), "patient_guid": guid,
                    "ips": ips, "ips_status": ips_status,
                    "first_datapoint_at": first,
                    "day_definition": "whole days since first_datapoint_at (day 0)",
                    "datapoints": points})


def _patients_in_orgs_for(guid: str, orgs: list[str]) -> set[str]:
    """{guid} if any of its rows sits in one of the grant's organisations."""
    for t in DATA_TYPES:
        M = live_model(t)
        if M is None:
            continue
        hit = (db.session.query(M.guid)
               .filter(M.patient_guid == guid, M.patient_org_guid.in_(orgs)).first())
        if hit:
            return {guid}
    return set()


# ------------------------------------------------------------------- FHIR view
#
# 2026-11-20 (operator): the same data as /patients/<guid>, as a FHIR R5
# searchset Bundle, for partners whose tools read FHIR.
#
# Built ONLY from the picked-out datapoints above (_datapoint), never from
# raw_json, so it can carry nothing the JSON view does not: no Patient
# resource, no name, no personnummer, no notes. Each resource points at
# `Patient/<guid>` (the pseudonymous GUID the JSON view already gives) without
# the Patient resource itself. Sex, birth year, diagnoses and medications from
# ips stay in the JSON view.

FHIR_JSON = "application/fhir+json"
DAY_EXT = "https://pdhc.se/fhir/StructureDefinition/days-since-first-datapoint"
TAG_SYSTEM = "https://pdhc.se/CodeSystem/external-read"
_COND_CLINICAL = "http://terminology.hl7.org/CodeSystem/condition-clinical"
_PDHC_CONCEPT = "urn:pdhc:concept"


def _coding(code: str | None) -> dict | None:
    """`urn:pdhc:concept/<guid>` -> system urn:pdhc:concept + code <guid>;
    `<system>|<code>` -> that pair; anything else -> code only."""
    if not code:
        return None
    if code.startswith(_PDHC_CONCEPT + "/"):
        return {"coding": [{"system": _PDHC_CONCEPT, "code": code[len(_PDHC_CONCEPT) + 1:]}]}
    if "|" in code:
        system, _, c = code.partition("|")
        return {"coding": [{"system": system, "code": c}]}
    return {"coding": [{"code": code}]}


def _fhir_resource(p: dict, patient_ref: dict) -> dict:
    t, when, cc = p["resource_type"], p.get("effective_at"), _coding(p.get("code"))
    r: dict = {"resourceType": t, "id": p["guid"]}
    status = p.get("status")
    if t == "Observation":
        r["status"] = status or "final"
        if cc:
            r["code"] = cc
        r["subject"] = patient_ref
        if when:
            r["effectiveDateTime"] = when
        if p.get("care_plan_guid"):
            r["basedOn"] = [{"reference": f"CarePlan/{p['care_plan_guid']}"}]
        if "value" in p:
            q = {"value": p["value"]}
            if p.get("unit"):
                q["unit"] = p["unit"]
            r["valueQuantity"] = q
        elif p.get("value_code"):
            r["valueCodeableConcept"] = {"coding": [{"code": p["value_code"]}]}
        elif p.get("value_string") is not None:
            r["valueString"] = p["value_string"]
    elif t == "Condition":
        if p.get("clinical_status"):
            r["clinicalStatus"] = {"coding": [{"system": _COND_CLINICAL,
                                               "code": p["clinical_status"]}]}
        if cc:
            r["code"] = cc
        r["subject"] = patient_ref
        if when:
            r["onsetDateTime"] = when
    elif t in ("MedicationStatement", "MedicationRequest"):
        r["status"] = status or ("recorded" if t == "MedicationStatement" else "unknown")
        if t == "MedicationRequest":
            r["intent"] = "order"
        if cc:
            r["medication"] = {"concept": cc}
        r["subject"] = patient_ref
        if when:
            r["effectiveDateTime" if t == "MedicationStatement" else "authoredOn"] = when
    elif t == "AllergyIntolerance":
        if cc:
            r["code"] = cc
        r["patient"] = patient_ref
        if when:
            r["recordedDate"] = when
    elif t == "Encounter":
        r["status"] = status or "unknown"
        if cc:
            r["type"] = [cc]
        r["subject"] = patient_ref
        if when:
            r["actualPeriod"] = {"start": when}
    elif t == "QuestionnaireResponse":
        r["status"] = status or "completed"
        if p.get("code"):
            r["questionnaire"] = p["code"]
        r["subject"] = patient_ref
        if when:
            r["authored"] = when
    else:  # Procedure, DiagnosticReport
        r["status"] = status or "unknown"
        if cc:
            r["code"] = cc
        r["subject"] = patient_ref
        if when:
            r["occurrenceDateTime" if t == "Procedure" else "effectiveDateTime"] = when
    if "day" in p:
        r["extension"] = [{"url": DAY_EXT, "valueInteger": p["day"]}]
    return r


def _fhir(body: dict, status: int = 200):
    resp = jsonify(body)
    resp.status_code = status
    resp.mimetype = FHIR_JSON
    return resp


def _outcome(code: str, text: str) -> dict:
    return {"resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": code, "diagnostics": text}]}


@bp.get("/patients/<guid>/$everything")
@bp.get("/patients/<guid>/%24everything")
def get_patient_fhir(guid: str):
    try:
        found = _patient_points(guid)
    except _Refuse as e:
        return _fhir(_outcome("exception", e.body.get("message", "")), e.status)
    if found is None:
        _audit(patient_guid=guid, n_rows=0)
        return _fhir(_outcome("not-found", "No such patient under this read key"), 404)
    points, _first = found
    types = set((request.args.get("_type") or "").split(",")) - {""}
    if types:
        points = [p for p in points if p["resource_type"] in types]
    ref = {"reference": f"Patient/{guid}"}
    entries = [{"fullUrl": f"{p['resource_type']}/{p['guid']}",
                "resource": _fhir_resource(p, ref),
                "search": {"mode": "match"}} for p in points]
    _audit(patient_guid=guid, n_rows=len(entries))
    return _fhir({
        "resourceType": "Bundle",
        "type": "searchset",
        "timestamp": datetime.now().astimezone().isoformat(),
        "meta": {"tag": [{"system": TAG_SYSTEM, "code": "pseudonymised",
                          "display": f"read grant {g.read_grant.get('grant_guid')} on {cdr_id()}"}]},
        "total": len(entries),
        "link": [{"relation": "self", "url": request.url}],
        "entry": entries,
    })
