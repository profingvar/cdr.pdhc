"""Care-delivery read surface for the clinical dashboard (#468 / #462 D6).

The rebuilt dashboard.pdhc (#462) is a single-patient CLINICAL tool for the
treating organisation. It reads CDR1 under a CARE-DELIVERY legal basis
(vård: vårdrelation + spärr), NOT the analysis-consent basis (#422
``check_patient_allowed``) that the FHIR read path applies to human
operators. A patient may be treatable while having declined research; the
analysis gate would wrongly hide them from their own clinician.

Two facts about CDR1's existing auth make this endpoint's shape what it is:

  1. #422 already PASSES THROUGH for service-key callers — ``_operator_blob``
     returns None when ``service_source`` is set (analysis_consent.py). So a
     dashboard service-key read is already consent-bypassed. Good: that IS
     the care-delivery basis for consent.
  2. BUT the service blob is ``is_su_admin: True`` (auth._service_blob), so
     the shared ``fhir_read._org_filter`` short-circuits and returns
     everything — it does NOT honour ``X-Org-Guids``. For a care-delivery
     read that would leak every org's patients.

So these endpoints do their OWN explicit org scoping from ``X-Org-Guids`` /
``X-Is-Admin`` (the values the dashboard derives from the operator's
affiliations), and require the caller to be the ``dashboard.pdhc`` service
declaring ``X-Access-Purpose: care-delivery``. Spärr is enforced on the
dashboard side (operator #469 Q1); CDR-side spärr (defense-in-depth) is a
deferred follow-up.

Endpoints (mounted at /api/v1/clinical):
  GET /patients                 — org's patients that HAVE data (+ counts)
  GET /patient/<guid>/summary   — per-concept data counts for one patient
"""
from __future__ import annotations

import re
from datetime import datetime

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func

from app import db
from app.models.resources import live_model
from app.services.plan_client import PlanClient

bp = Blueprint("clinical_read", __name__)

CARE_DELIVERY_PURPOSE = "care-delivery"
CLINICAL_SERVICE = "dashboard.pdhc"
_DEFAULT_LIMIT = 500
_MAX_LIMIT = 2000
# Series can be dense (CGM ≈ 1 point / 5 min). Higher ceiling than the
# patient/summary lists; the dashboard downsamples for display.
_DEFAULT_SERIES_LIMIT = 10000
_MAX_SERIES_LIMIT = 50000


# ---------------------------------------------------------------------------
# Guards + scoping
# ---------------------------------------------------------------------------

def _care_delivery_guard():
    """Return an error ``(json, status)`` tuple, or None when the request is
    an authorised care-delivery read from the clinical dashboard."""
    if request.headers.get("X-Access-Purpose", "").strip() != CARE_DELIVERY_PURPOSE:
        return jsonify(error=(
            "these endpoints serve care-delivery reads only; set "
            "X-Access-Purpose: care-delivery")), 400
    blob = getattr(g, "access_blob", None) or {}
    if blob.get("service_source") != CLINICAL_SERVICE:
        return jsonify(error="care-delivery reads require the dashboard.pdhc "
                             "service identity"), 403
    return None


def _scope_orgs():
    """(is_admin, orgs) from the forwarded operator scope headers.

    ``X-Is-Admin: 1`` → admin operator, no org restriction (mirrors the
    existing dashboard/federation semantics). Otherwise the caller's
    affiliation care-unit guids from ``X-Org-Guids``.
    """
    if request.headers.get("X-Is-Admin") == "1":
        return True, []
    hdr = request.headers.get("X-Org-Guids", "").strip()
    orgs = [o.strip() for o in hdr.split(",") if o.strip()]
    return False, orgs


def _apply_org_scope(query, Model):
    """Apply care-delivery org scoping. Returns None when a non-admin caller
    has no orgs (Rule 24 → sees nothing)."""
    is_admin, orgs = _scope_orgs()
    if is_admin:
        return query
    if not orgs:
        return None
    return query.filter(Model.org_guid.in_(orgs))


def _limit() -> int:
    try:
        n = int(request.args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, n))


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _parse_iso(s: str | None):
    """Lenient ISO-8601 parse (accepts a trailing 'Z'); None on failure."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _series_limit() -> int:
    try:
        n = int(request.args.get("limit", _DEFAULT_SERIES_LIMIT))
    except (TypeError, ValueError):
        return _DEFAULT_SERIES_LIMIT
    return max(1, min(_MAX_SERIES_LIMIT, n))


# --- concept display resolution (#471/#462 item 5) ------------------------
#
# Live CDR1 data (2026-07-15): code_canonical is dominantly
# ``urn:pdhc:concept/<concept-guid>`` (Path B — the concept GUID is embedded
# as the last path segment). So a chart's human label is: parse the GUID out
# of code_canonical, then resolve its display via plan.pdhc CodeSystem/$lookup
# (cached in PlanClient). Cosmetic + FAIL-OPEN — a miss shows the raw code.

_CONCEPT_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I,
)
_plan_client_singleton: PlanClient | None = None


def _concept_guid_from_canonical(code: str | None) -> str | None:
    """Extract the plan Concept GUID embedded in a Path-B code_canonical
    (``urn:pdhc:concept/<guid>`` or ``.../api/v1/concepts/<guid>``). Returns
    None for termbank URIs, smoke codes, or anything without a GUID tail."""
    if not code:
        return None
    if code.startswith("urn:pdhc:concept/") or "/api/v1/concepts/" in code:
        tail = code.rsplit("/", 1)[-1]
        return tail if _CONCEPT_GUID_RE.match(tail) else None
    return None


def _plan_client() -> PlanClient:
    global _plan_client_singleton
    if _plan_client_singleton is None:
        _plan_client_singleton = PlanClient(
            base_url=current_app.config.get("PLAN_BASE_URL") or None)
    return _plan_client_singleton


def resolve_display(code: str | None) -> str | None:
    """code_canonical → human display via plan.pdhc, or None. Skips entirely
    when PLAN_BASE_URL is unconfigured (tests / plan-less envs) so no network
    is touched. Never raises (cosmetic)."""
    if not current_app.config.get("PLAN_BASE_URL"):
        return None
    guid = _concept_guid_from_canonical(code)
    if not guid:
        return None
    try:
        return _plan_client().lookup_display(guid)
    except Exception:  # noqa: BLE001 — display is cosmetic, never break a read
        return None


def _patient_name(pat) -> str:
    """Best-effort display name from a Patient row's ``names`` JSON (a list
    of FHIR HumanName dicts)."""
    if pat is None:
        return ""
    names = getattr(pat, "names", None)
    if not isinstance(names, list) or not names:
        return ""
    n = names[0] or {}
    if n.get("text"):
        return str(n["text"])
    given = " ".join(n.get("given") or [])
    family = n.get("family") or ""
    return f"{given} {family}".strip()


# ---------------------------------------------------------------------------
# GET /api/v1/clinical/patients — org's patients that have data
# ---------------------------------------------------------------------------

@bp.get("/patients")
def patients():
    err = _care_delivery_guard()
    if err:
        return err
    Obs = live_model("Observation")
    q = (db.session.query(
            Obs.patient_guid,
            func.count(Obs.guid),
            func.max(Obs.effective_at),
         )
         .filter(Obs.patient_guid.isnot(None)))
    q = _apply_org_scope(q, Obs)
    if q is None:
        return jsonify(patients=[], count=0), 200
    q = (q.group_by(Obs.patient_guid)
          .order_by(func.max(Obs.effective_at).desc().nullslast())
          .limit(_limit()))
    rows = q.all()

    Pat = live_model("Patient")
    guids = [r[0] for r in rows]
    pats = {}
    if guids:
        pats = {p.guid: p for p in
                db.session.query(Pat).filter(Pat.guid.in_(guids)).all()}

    out = []
    for pg, count, last in rows:
        pat = pats.get(pg)
        out.append({
            "patient_guid": pg,
            "name": _patient_name(pat),
            "birth_date": (pat.birth_date.isoformat()
                           if pat is not None and pat.birth_date else None),
            "observation_count": int(count or 0),
            "last_observed_at": _iso(last),
        })
    return jsonify(patients=out, count=len(out)), 200


# ---------------------------------------------------------------------------
# GET /api/v1/clinical/patient/<guid>/summary — per-concept data counts
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>/summary")
def patient_summary(guid):
    err = _care_delivery_guard()
    if err:
        return err
    Obs = live_model("Observation")
    q = (db.session.query(
            Obs.code_canonical,
            func.count(Obs.guid),
            func.min(Obs.effective_at),
            func.max(Obs.effective_at),
            func.max(Obs.value_unit),
         )
         .filter(Obs.patient_guid == guid))
    q = _apply_org_scope(q, Obs)
    if q is None:
        return jsonify(patient_guid=guid, parameters=[], count=0), 200
    q = (q.group_by(Obs.code_canonical)
          .order_by(func.count(Obs.guid).desc()))
    rows = q.all()

    out = [{
        "code": code,
        "display": resolve_display(code),   # human label via plan.pdhc (#471)
        "unit": unit,
        "count": int(count or 0),
        "first_observed_at": _iso(first),
        "last_observed_at": _iso(last),
    } for code, count, first, last, unit in rows]
    return jsonify(patient_guid=guid, parameters=out, count=len(out)), 200


# ---------------------------------------------------------------------------
# GET /api/v1/clinical/patient/<guid>/series — the actual data points
# ---------------------------------------------------------------------------

@bp.get("/patient/<guid>/series")
def patient_series(guid):
    """Time-series points for a patient, optionally filtered to specific
    concept codes and an effective-date window. Ordered oldest→newest.

    Each point carries ``org_guid`` so the dashboard can apply spärr
    (per-clinic blocks) on its side (operator #469 Q1) — CDR1 has already
    org-scoped to the caller's affiliation, but a patient can still block a
    clinic the caller is affiliated with.
    """
    err = _care_delivery_guard()
    if err:
        return err
    Obs = live_model("Observation")
    q = (db.session.query(
            Obs.code_canonical, Obs.effective_at, Obs.value_quantity,
            Obs.value_unit, Obs.value_string, Obs.org_guid,
         )
         .filter(Obs.patient_guid == guid))
    q = _apply_org_scope(q, Obs)
    if q is None:
        return jsonify(patient_guid=guid, points=[], count=0), 200

    codes = [c for c in request.args.getlist("code") if c]
    if codes:
        q = q.filter(Obs.code_canonical.in_(codes))
    frm = _parse_iso(request.args.get("from"))
    if frm is not None:
        q = q.filter(Obs.effective_at >= frm)
    to = _parse_iso(request.args.get("to"))
    if to is not None:
        q = q.filter(Obs.effective_at <= to)

    q = q.order_by(Obs.effective_at.asc()).limit(_series_limit())
    rows = q.all()

    points = [{
        "code": code,
        "at": _iso(at),
        "value": float(vq) if vq is not None else None,
        "unit": unit,
        "value_string": vs,
        "org_guid": org,
    } for code, at, vq, unit, vs, org in rows]
    return jsonify(patient_guid=guid, points=points, count=len(points)), 200
