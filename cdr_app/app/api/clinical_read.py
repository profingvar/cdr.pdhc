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

import base64
import binascii
import json
import re
from datetime import datetime

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import and_, func, or_

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
    # #575 (#212 re-home): an admin-OVERRIDE read (X-Is-Admin:1 bypasses org
    # scoping and returns every patient) is break-glass — it MUST carry an
    # attestation reason (X-Admin-Read-Reason). Enforced at the data layer so
    # no client can bypass the dashboard's attestation prompt. The reason
    # itself is recorded in the dashboard's audit (event_type=admin_override).
    if request.headers.get("X-Is-Admin") == "1":
        if not (request.headers.get("X-Admin-Read-Reason") or "").strip():
            return jsonify(
                error="admin-override read requires an attestation reason",
                code="ADMIN_READ_REASON_REQUIRED",
            ), 428
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

# --- #983 part 2: ordering, a keyset cursor, and VISIBLE truncation --------
#
# The series used to be `ORDER BY effective_at ASC LIMIT 10000`, so a patient
# with more than ten thousand points got the OLDEST ten thousand — silently,
# and with no way to page past them. That is the size the patient dashboard's
# Gate 1 and Gate 4 test at. No cdr1 patient is near it today (max 650 rows,
# measured 2026-11-11), so the defect is latent rather than live.
#
# Three changes, and the first is the constraint the others work around:
#
# 1. THE DEFAULT ORDER STAYS ASCENDING. This endpoint has consumers that rely
#    on oldest->newest — `test_series_ordered_with_value_and_org` asserts it,
#    and dashboard's existing charts page reads it. Flipping the default would
#    break them to serve a new one. `?order=desc` is opt-in.
# 2. `guid` IS ALWAYS THE TIEBREAK. `effective_at` is not unique — two
#    (patient, effective_at) pairs repeat on cdr1 today — and a cursor on a
#    non-unique key either skips rows or repeats them at a page boundary.
# 3. TRUNCATION IS REPORTED. One extra row is fetched; if it exists the
#    response says `truncated: true` and carries a cursor, so a consumer can
#    say "showing N of M" instead of presenting a cut history as complete.

_CURSOR_VERSION = 1
_ORDERS = ("asc", "desc")


class InvalidCursor(ValueError):
    """A cursor that cannot be honoured. Never silently means "page one":
    an infinite list that quietly restarts re-serves the same rows forever
    with nothing to say the walk broke."""


def _encode_cursor(order: str, at, obs_guid: str) -> str:
    payload = {"v": _CURSOR_VERSION, "o": order,
               "e": at.isoformat(), "g": obs_guid}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str, order: str):
    """-> (effective_at, guid). Raises InvalidCursor on anything doubtful,
    INCLUDING a cursor minted for the other direction: resuming a descending
    walk with an ascending predicate silently returns the wrong rows."""
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + pad))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor("cursor is not decodable") from exc
    if not isinstance(data, dict) or data.get("v") != _CURSOR_VERSION:
        raise InvalidCursor("cursor version is not supported")
    if data.get("o") != order:
        raise InvalidCursor(
            f"cursor was minted for order={data.get('o')!r}, not {order!r}")
    guid, e = data.get("g"), data.get("e")
    if not isinstance(guid, str) or not guid or not isinstance(e, str):
        raise InvalidCursor("cursor is missing its position")
    try:
        at = datetime.fromisoformat(e)
    except ValueError as exc:
        raise InvalidCursor("cursor timestamp is not ISO-8601") from exc
    return at, guid


def _bad_request(code: str, message: str):
    return jsonify(error=code, message=message), 400


@bp.get("/patient/<guid>/series")
def patient_series(guid):
    """Time-series points for a patient, optionally filtered to specific
    concept codes and an effective-date window. Ordered oldest→newest by
    default; `?order=desc` and `?cursor=` page newest-first (#983).

    Each point carries ``org_guid`` so the dashboard can apply spärr
    (per-clinic blocks) on its side (operator #469 Q1) — CDR1 has already
    org-scoped to the caller's affiliation, but a patient can still block a
    clinic the caller is affiliated with.
    """
    err = _care_delivery_guard()
    if err:
        return err
    Obs = live_model("Observation")
    # #983 — identity and per-datapoint provenance travel WITH the point.
    #
    # The four context columns live ON THIS ROW by #769's repair, not in
    # `clinical_context`: that side table is keyed on `ingest_raw_guid` while
    # this table's `guid` is the FHIR resource id, so a join on the resource
    # id matched 0 rows while the side table looked populated. They are
    # columns here, so this costs one SELECT and no join at all.
    #
    # `guid` is the row identity a consumer needs in order to ADDRESS a
    # datapoint: without it there is no cursor tiebreak, no selection, and
    # nothing to look a detail up by. `care_plan_guid` is projected although
    # it is unpopulated today (0 of 1 949 on cdr1) — a field absent from the
    # payload is indistinguishable from one the consumer forgot to read, and
    # a consumer that renders it as "not recorded" keeps the gap visible.
    q = (db.session.query(
            Obs.code_canonical, Obs.effective_at, Obs.value_quantity,
            Obs.value_unit, Obs.value_string, Obs.org_guid,
            Obs.guid, Obs.transaction_guid, Obs.ingest_raw_guid,
            Obs.care_plan_guid,
         )
         .filter(Obs.patient_guid == guid))
    q = _apply_org_scope(q, Obs)
    if q is None:
        # Same shape as the full path: a consumer branches on `truncated` and
        # `next_cursor`, and two shapes for one endpoint is how a client ends
        # up reading a missing key as "false" on one path only.
        return jsonify(patient_guid=guid, points=[], count=0,
                       truncated=False, next_cursor=None), 200

    codes = [c for c in request.args.getlist("code") if c]
    if codes:
        q = q.filter(Obs.code_canonical.in_(codes))
    frm = _parse_iso(request.args.get("from"))
    if frm is not None:
        q = q.filter(Obs.effective_at >= frm)
    to = _parse_iso(request.args.get("to"))
    if to is not None:
        q = q.filter(Obs.effective_at <= to)
    # The dashboard's plan filter (#977): observations belonging to a plan's
    # transactions. ECHOED in the body as `transaction_filter`, because a cdr
    # without this filter would ignore the parameter and answer with the
    # whole history — the caller must be able to tell "filtered" from
    # "ignored" without trusting that the server is new enough.
    transactions = [t for t in request.args.getlist("transaction") if t]
    if transactions:
        q = q.filter(Obs.transaction_guid.in_(transactions))

    order = (request.args.get("order") or "asc").lower()
    if order not in _ORDERS:
        return _bad_request("invalid_order",
                            f"order must be one of {', '.join(_ORDERS)}")
    cursor = request.args.get("cursor")
    paged = order == "desc" or bool(cursor)

    undated = None
    if paged:
        # A row with no effective time cannot be placed in a time-ordered
        # walk. Those rows are EXCLUDED from the paged path and COUNTED, so
        # the omission is visible rather than silent. 0 on cdr1 today; the
        # column is nullable, so it is handled rather than assumed away.
        undated = q.filter(Obs.effective_at.is_(None)).count()
        q = q.filter(Obs.effective_at.isnot(None))

    if cursor:
        try:
            c_at, c_guid = _decode_cursor(cursor, order)
        except InvalidCursor as exc:
            return _bad_request("invalid_cursor", str(exc))
        if order == "desc":
            q = q.filter(or_(Obs.effective_at < c_at,
                             and_(Obs.effective_at == c_at, Obs.guid < c_guid)))
        else:
            q = q.filter(or_(Obs.effective_at > c_at,
                             and_(Obs.effective_at == c_at, Obs.guid > c_guid)))

    if order == "desc":
        q = q.order_by(Obs.effective_at.desc(), Obs.guid.desc())
    else:
        q = q.order_by(Obs.effective_at.asc(), Obs.guid.asc())

    limit = _series_limit()
    rows = q.limit(limit + 1).all()
    truncated = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if truncated and rows:
        last = rows[-1]
        if last[1] is not None:            # effective_at
            next_cursor = _encode_cursor(order, last[1], last[6])

    points = [{
        "code": code,
        "at": _iso(at),
        "value": float(vq) if vq is not None else None,
        "unit": unit,
        "value_string": vs,
        "org_guid": org,
        # Named `observation_guid`, not `guid`: this payload already carries
        # an org guid and a transaction guid, and a bare `guid` in that
        # company reads as "which one?".
        "observation_guid": obs_guid,
        "transaction_guid": txn,
        "ingest_raw_guid": ingest,
        "care_plan_guid": cp,
    } for code, at, vq, unit, vs, org, obs_guid, txn, ingest, cp in rows]
    body = dict(patient_guid=guid, points=points, count=len(points),
                order=order, limit=limit, truncated=truncated,
                next_cursor=next_cursor)
    if undated is not None:
        body["undated_count"] = undated
    if transactions:
        body["transaction_filter"] = True
    return jsonify(body), 200


# ---------------------------------------------------------------------------
# GET /api/v1/clinical/observation/<guid> — ONE datapoint and its provenance
# ---------------------------------------------------------------------------
#
# For the patient dashboard's detail panel (#983, step 18 of its plan).
#
# WHY NOT THE EXISTING `/observations/<guid>/provenance`. Measured on cdr1
# 2026-11-11: that route is keyed on `FhirResource.guid`, and
# `observation.guid` matches `fhir_resources.guid` for 0 of 1 949 rows — the
# two stores use DIFFERENT guids for the same datapoint, the id-space split
# #769 describes. Every observation_guid a clinical read hands out would 404
# there. It is also guarded by the narrow ingest/provenance service list,
# which the dashboard is deliberately not on.
#
# WHY NOT THE FHIR READ. `fhir_read`'s blob is SU-equivalent and
# organisation-blind; reading a care datapoint through it would step round
# this module's own Rule 24 scoping.
#
# So: the same guard and the same org scope as the series, looked up by the
# live table's guid, with context joined on `ingest_raw_guid` — exactly 1:1
# against both `clinical_context` and `fhir_resources` on cdr1. The raw FHIR is
# on the row already (`raw_json`, populated on 1 949 of 1 949).
#
# The fallbacks into the raw resource REUSE provenance.py's helpers rather
# than re-implementing them, and inherit its #665 rule: author_org_guid gets
# NO fallback, because FHIR `performer` currently carries the SUBMITTER.

_CONTEXT_FIELDS = (
    "service_request_guid", "plan_definition_guid", "plan_activity_guid",
    "contract_guid", "requesting_org_guid", "provider_org_guid",
    "author_org_guid", "care_plan_guid", "transaction_guid", "concept_guid",
    "requester_user_guid", "source_service", "sim_run_id",
)


@bp.get("/observation/<guid>")
def observation_detail(guid):
    """One observation with every provenance field, present or null.

    A missing value is returned as an explicit null, never omitted: the
    dashboard renders "not recorded" so the context gaps stay VISIBLE, and a
    key absent from the payload is indistinguishable from one the consumer
    forgot to read.

    404 for "does not exist" AND for "outside your organisations", with the
    same body, so probing an observation guid learns nothing.
    """
    err = _care_delivery_guard()
    if err:
        return err
    from app.models import ClinicalContext
    from .provenance import (_basedon_guid, _extension_ref_guid,
                             _EXT_CONTRACT, _EXT_REQUESTING_ORG)

    Obs = live_model("Observation")
    q = _apply_org_scope(Obs.query.filter(Obs.guid == guid), Obs)
    row = q.first() if q is not None else None
    if row is None:
        return jsonify(error="not_found",
                       message="no such observation in your scope"), 404

    ctx = None
    if row.ingest_raw_guid:
        ctx = (ClinicalContext.query
               .filter_by(ingest_raw_guid=row.ingest_raw_guid).first())

    context = {k: (getattr(ctx, k, None) if ctx is not None else None)
               for k in _CONTEXT_FIELDS}
    # The row's own columns win over the side table: #769 put them there.
    for k in ("transaction_guid", "care_plan_guid"):
        if getattr(row, k, None):
            context[k] = getattr(row, k)

    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    filled_from_raw = []
    for key, value in (
        ("service_request_guid", _basedon_guid(raw, "ServiceRequest")),
        ("plan_definition_guid", _basedon_guid(raw, "PlanDefinition")),
        ("contract_guid", _extension_ref_guid(raw, _EXT_CONTRACT)),
        ("requesting_org_guid", _extension_ref_guid(raw, _EXT_REQUESTING_ORG)),
    ):
        if not context.get(key) and value:
            context[key] = value
            filled_from_raw.append(key)
    # author_org_guid: deliberately NO fallback (#665).

    return jsonify({
        "observation_guid": row.guid,
        "patient_guid": row.patient_guid,
        "code": row.code_canonical,
        "at": _iso(row.effective_at),
        "received_at": _iso(row.received_at),
        "value": float(row.value_quantity) if row.value_quantity is not None else None,
        "unit": row.value_unit,
        "value_string": row.value_string,
        "org_guid": row.org_guid,
        "ingest_raw_guid": row.ingest_raw_guid,
        "context": context,
        "context_found": ctx is not None,
        # Which values came from the FHIR resource rather than the structured
        # columns — a reader auditing provenance should know the difference.
        "context_filled_from_raw": filled_from_raw,
        "raw_fhir": raw or None,
    }), 200
