"""External read door: de-identified patient data under a time-limited grant.

Operator decision 2026-11-19. An SU admin issues a READ GRANT in sso.pdhc to
an external partner: valid at most 48 hours, bound to chosen patient
organisations and chosen CDRs. The partner sends it as
`Authorization: Bearer rg.<guid>.<secret>` to

    GET /external/v1/patients            the patients the grant covers
    GET /external/v1/patients/<guid>     one patient: IPS summary + datapoints

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
column), then ips's research consent verdict (#422) -- a patient who opted out
of secondary use is not served.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

import requests
from flask import Blueprint, current_app, g, jsonify, request

from app import db
from app.models.resources import live_model

log = logging.getLogger(__name__)

bp = Blueprint("external_read", __name__)

#: Every patient-bearing type. Patient itself is deliberately absent.
DATA_TYPES = ("Observation", "QuestionnaireResponse", "Condition",
              "MedicationStatement", "MedicationRequest", "AllergyIntolerance",
              "Procedure", "Encounter", "DiagnosticReport")
#: The secondary-use purpose ips judges consent against.
PURPOSE = "research"
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
        verdict = _analysis_filter(sorted(guids), PURPOSE, [])
    except IpsUnreachable as e:
        raise _Refuse(503, "consent_unavailable",
                      f"Consent could not be checked, so nothing is served: {e}")
    return guids & set(verdict.get("allowed") or [])


def _audit(*, patient_guid=None, n_rows=None):
    from app.services.read_audit import record_read_audit
    record_read_audit(resource_type="External", patient_guid=patient_guid, n_rows=n_rows,
                      detail={"grant_guid": g.read_grant.get("grant_guid"),
                              "partner_guid": g.read_grant.get("partner_guid"),
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
    base = (current_app.config.get("IPS_BASE_URL") or os.environ.get("IPS_BASE_URL", "")).rstrip("/")
    key = current_app.config.get("IPS_API_KEY") or os.environ.get("IPS_API_KEY", "")
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
                    "count": len(guids), "truncated": truncated, "patients": guids})


@bp.get("/patients/<guid>")
def get_patient(guid: str):
    orgs = _orgs()
    try:
        in_scope = guid in _consented({guid} & _patients_in_orgs_for(guid, orgs))
    except _Refuse as e:
        return jsonify(e.body), e.status
    if not in_scope:
        _audit(patient_guid=guid, n_rows=0)
        # Same answer whether the patient is absent, outside the grant or
        # opted out: the grant learns nothing about patients it does not cover.
        return jsonify({"error": "not_found", "message": "No such patient under this read key"}), 404
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
    points, first = _with_days(points)
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
