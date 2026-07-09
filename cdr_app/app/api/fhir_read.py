"""FHIR R5 read / search / vread / $everything / $stats / terminology shims.

Implements platform-plan §1.3. Endpoints (all under /api/v1/fhir/):

  GET  /<Type>                            — Search
  GET  /<Type>/<guid>                     — read instance
  GET  /<Type>/<guid>/_history            — version list
  GET  /<Type>/<guid>/_history/<vid>      — vread specific snapshot
  GET  /Patient/<guid>/$everything        — patient compartment Bundle
  GET  /Observation/$stats                — numeric summary stats
  POST /CodeSystem/$lookup                — proxy to termbank.pdhc
  POST /ConceptMap/$translate             — proxy to xlate.pdhc
  POST /ValueSet/$validate-code           — proxy to plan.pdhc
  GET  /events                            — change_feed long-poll (§1.5)

Search supports the demonstrator subset called out in §1.3.a:
patient, code, date (ge/le), category, _tag, _has, _include,
_revinclude, plus chained search ``patient.identifier=…``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import and_, or_

from app import db
from app.auth import scope_org_guids
from app.models.resources import (
    ChangeFeed,
    history_model,
    live_model,
)
from app.services.analysis_consent import (
    check_patient_allowed,
    consent_allowed_guids,
)


def _row_patient_guid(row) -> str | None:
    """Patient rows identify themselves; every other type carries
    patient_guid (§1.1.b common columns)."""
    return getattr(row, "patient_guid", None) or getattr(row, "guid", None)


log = logging.getLogger(__name__)


bp = Blueprint("fhir_read", __name__)


_RESOURCE_TYPES = [
    "Patient", "Observation", "QuestionnaireResponse", "Condition",
    "MedicationStatement", "MedicationRequest", "AllergyIntolerance",
    "Procedure", "Encounter", "DiagnosticReport",
]


def _operation_outcome(severity: str, code: str, text: str) -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{
            "severity": severity,
            "code": code,
            "details": {"text": text},
        }],
    }


def _org_filter(query, Live, *, request_args=None):
    """Apply Rule 24 org-scoping.

    Authoritative source is the SSO / service-key access blob loaded by
    ``app.auth.install_request_loader`` into ``g.access_blob``:
        - ``is_su_admin = True``  → bypass scoping (admin sees all).
        - ``organization_ids``    → scope to that set.
    Legacy ``X-Is-Admin`` / ``X-Org-Guids`` headers are still honoured
    as a fallback for local-dev callers and the older test fixtures.
    """
    blob = getattr(g, "access_blob", None) or {}
    if blob.get("is_su_admin"):
        return query
    # M0 #416: Zone-1 scope from affiliations[] (dual-read fallback to
    # organization_ids), shared with auth._blob_to_user.
    org_ids = scope_org_guids(blob)
    if org_ids:
        return query.filter(Live.org_guid.in_(org_ids))

    # Legacy header path
    if request.headers.get("X-Is-Admin") == "1":
        return query
    org_guids_hdr = request.headers.get("X-Org-Guids", "").strip()
    if org_guids_hdr:
        orgs = [s.strip() for s in org_guids_hdr.split(",") if s.strip()]
        return query.filter(Live.org_guid.in_(orgs))

    # Nothing to scope by — non-admin user with no orgs sees nothing.
    return query.filter(Live.org_guid == "__no_org__")


# ---------------------------------------------------------------------------
# GET /<Type>/<guid>  — instance read (§1.3 / FHIR read interaction)
# ---------------------------------------------------------------------------

@bp.get("/<resource_type>/<guid>")
def read_instance(resource_type: str, guid: str):
    if resource_type not in _RESOURCE_TYPES:
        return jsonify(_operation_outcome(
            "error", "not-supported",
            f"Resource type '{resource_type}' is not handled by cdr.pdhc.",
        )), 404

    Live = live_model(resource_type)
    q = db.session.query(Live).filter(Live.guid == guid)
    q = _org_filter(q, Live)
    row = q.one_or_none()
    if row is None:
        return jsonify(_operation_outcome("error", "not-found", "resource not found")), 404
    check_patient_allowed(_row_patient_guid(row))  # #422
    resp = jsonify(row.raw_json)
    resp.headers["ETag"] = row.etag or f'W/"{row.version_id}"'
    return resp, 200


# ---------------------------------------------------------------------------
# GET /<Type>/<guid>/_history  — version list (§1.3.h)
# ---------------------------------------------------------------------------

@bp.get("/<resource_type>/<guid>/_history")
def history_list(resource_type: str, guid: str):
    if resource_type not in _RESOURCE_TYPES:
        return jsonify(_operation_outcome(
            "error", "not-supported", "unsupported resource type",
        )), 404

    Live = live_model(resource_type)
    Hist = history_model(resource_type)

    live_q = _org_filter(db.session.query(Live).filter(Live.guid == guid), Live)
    live_row = live_q.one_or_none()
    hist_rows = (
        _org_filter(db.session.query(Hist).filter(Hist.guid == guid), Hist)
        .order_by(Hist.version_id.desc())
        .all()
    )
    if live_row is None and not hist_rows:
        return jsonify(_operation_outcome("error", "not-found", "resource not found")), 404
    check_patient_allowed(_row_patient_guid(live_row or hist_rows[0]))  # #422

    entries = []
    if live_row is not None:
        entries.append({
            "fullUrl": f"{resource_type}/{guid}/_history/{live_row.version_id}",
            "resource": live_row.raw_json,
        })
    for r in hist_rows:
        entries.append({
            "fullUrl": f"{resource_type}/{guid}/_history/{r.version_id}",
            "resource": r.raw_json,
        })
    bundle = {
        "resourceType": "Bundle",
        "type": "history",
        "timestamp": _now_iso(),
        "total": len(entries),
        "entry": entries,
    }
    return jsonify(bundle), 200


# ---------------------------------------------------------------------------
# GET /<Type>/<guid>/_history/<vid>  — vread (§1.3.h)
# ---------------------------------------------------------------------------

@bp.get("/<resource_type>/<guid>/_history/<int:vid>")
def vread(resource_type: str, guid: str, vid: int):
    if resource_type not in _RESOURCE_TYPES:
        return jsonify(_operation_outcome(
            "error", "not-supported", "unsupported resource type",
        )), 404

    Live = live_model(resource_type)
    Hist = history_model(resource_type)

    live_row = _org_filter(
        db.session.query(Live).filter(Live.guid == guid),
        Live,
    ).one_or_none()
    if live_row is not None and live_row.version_id == vid:
        check_patient_allowed(_row_patient_guid(live_row))  # #422
        resp = jsonify(live_row.raw_json)
        resp.headers["ETag"] = live_row.etag or f'W/"{vid}"'
        return resp, 200

    hist_row = _org_filter(
        db.session.query(Hist).filter(Hist.guid == guid, Hist.version_id == vid),
        Hist,
    ).one_or_none()
    if hist_row is None:
        return jsonify(_operation_outcome("error", "not-found", "version not found")), 404
    check_patient_allowed(_row_patient_guid(hist_row))  # #422
    resp = jsonify(hist_row.raw_json)
    resp.headers["ETag"] = hist_row.etag or f'W/"{vid}"'
    return resp, 200


# ---------------------------------------------------------------------------
# GET /<Type>  — search (§1.3.a, §1.3.f, §1.3.g)
# ---------------------------------------------------------------------------

@bp.get("/<resource_type>")
def search(resource_type: str):
    if resource_type not in _RESOURCE_TYPES:
        return jsonify(_operation_outcome(
            "error", "not-supported", "unsupported resource type",
        )), 404

    Live = live_model(resource_type)

    q = _org_filter(db.session.query(Live), Live)

    # ----- chained search: patient.identifier=... ------------------------
    chained_pat_ident = request.args.get("patient.identifier") \
                        or request.args.get("subject.identifier")
    if chained_pat_ident:
        Patient = live_model("Patient")
        # Match against any identifier value in the JSONB array.
        # SQLite JSONB-ish: we filter in Python after fetching candidates.
        candidate_pats = _filter_patients_by_identifier(Patient, chained_pat_ident)
        pat_guids = [p.guid for p in candidate_pats]
        if not pat_guids:
            q = q.filter(False)
        else:
            q = q.filter(Live.patient_guid.in_(pat_guids))

    # ----- _has reverse-chain (§1.3.d) -----------------------------------
    # Syntax: _has:<TargetType>:<refField>:<targetParam>=<value>
    # Example on Patient: _has:Observation:patient:code=4548-4 →
    #   Patients who have an Observation with code=4548-4 referencing them.
    for has_arg in [k for k in request.args.keys() if k.startswith("_has:")]:
        q = _apply_has_filter(q, Live, resource_type, has_arg, request.args.get(has_arg))

    # ----- direct params -------------------------------------------------
    patient = request.args.get("patient") or request.args.get("subject")
    if patient:
        # Allow "Patient/<guid>" or bare "<guid>".
        if "/" in patient:
            patient = patient.rsplit("/", 1)[-1]
        if hasattr(Live, "patient_guid"):
            q = q.filter(Live.patient_guid == patient)
        else:
            q = q.filter(Live.guid == patient)

    code = request.args.get("code")
    if code:
        q = _filter_by_code(q, Live, code)

    # date=ge2024-01-01 / date=le2024-12-31  (FHIR prefix syntax)
    for date_arg in request.args.getlist("date"):
        q = _apply_date_filter(q, Live, date_arg)

    # _id
    for id_arg in request.args.getlist("_id"):
        q = q.filter(Live.guid == id_arg)

    # _tag — looks at meta_tag JSON array for {system, code} matches.
    tag_arg = request.args.get("_tag")
    if tag_arg:
        # JSONB arrays — use the raw column. Fallback to Python filter on SQLite.
        # Postgres-specific search would be `meta_tag @> '[{"code": "..."}]'` but
        # we keep it dialect-portable: pull a candidate set then filter in Python.
        rows = q.limit(1000).all()
        rows = [r for r in rows if _has_tag(r.meta_tag or [], tag_arg)]
        rows = _consent_filter(rows)  # #422
        return _bundle_searchset(rows, resource_type, with_includes=False)

    # _count + sort (default to most recent first by effective_at).
    # Cap raised from 500 → 30000 (2026-04-28 F1 data-shape) so the
    # nurse AGP can read a full 90-day CGM-raw window (~26k points)
    # in one fetch. Gunicorn's 120s timeout + nginx proxy_buffering
    # off (Block B') comfortably handle 30k-row responses.
    count = min(int(request.args.get("_count", 100)), 30000)
    if hasattr(Live, "effective_at"):
        q = q.order_by(Live.effective_at.desc().nullslast())
    q = q.limit(count)

    rows = q.all()
    rows = _consent_filter(rows)  # #422
    return _bundle_searchset(rows, resource_type)


def _consent_filter(rows: list) -> list:
    """Drop rows whose patient is excluded by the operator's role-derived
    purpose (#422). One batched ips call per request; pass-through for
    machine/SU contexts (consent_allowed_guids handles the gating)."""
    guids = {_row_patient_guid(r) for r in rows if _row_patient_guid(r)}
    if not guids:
        return rows
    allowed = consent_allowed_guids(guids)
    if allowed == guids:
        return rows
    return [r for r in rows if _row_patient_guid(r) in allowed]


def _filter_patients_by_identifier(Patient, ident_value: str):
    """Find patient rows whose identifiers JSONB array contains a match.

    ``ident_value`` is either ``"<system>|<value>"`` or just ``"<value>"``.
    """
    parts = ident_value.split("|", 1)
    sys, val = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])

    # Pull a bounded candidate set and filter in Python — keeps the code
    # dialect-portable. Volumes are small enough for the demo set; a
    # Postgres-specific JSONB containment query is the obvious upgrade.
    rows = db.session.query(Patient).limit(2000).all()
    out = []
    for r in rows:
        for ident in (r.identifiers or []):
            if val and ident.get("value") != val:
                continue
            if sys and ident.get("system") != sys:
                continue
            out.append(r)
            break
    return out


def _filter_by_code(q, Live, code_arg: str):
    """``code`` may be ``system|code``, ``url|code``, or just ``code``.

    When the caller supplies the system, the equality match against the
    indexed `code_canonical` column is enough — adding a `LIKE '%/<code>'`
    OR-fallback forces a sequential scan on the per-type tables (2M+
    rows on the demo CDRs), which made `$stats` take 4 s+ and timed out
    the dashboard's 2 s fanout. The bare-code path keeps the LIKE since
    no equality form is available."""
    parts = code_arg.split("|", 1)
    if len(parts) == 2:
        system, code = parts
        canonical_with_system = f"{system.rstrip('/')}/{code}"
        return q.filter(Live.code_canonical == canonical_with_system)
    return q.filter(Live.code_canonical.like(f"%/{parts[0]}"))


def _apply_has_filter(q, Live, resource_type: str, has_key: str, value: str):
    """``_has:<Type>:<refField>:<param>=<value>`` reverse-chain.

    Currently supports ``_has:<Type>:patient:code=<code>`` over per-type
    tables that have ``patient_guid`` and ``code_canonical``. Other
    combinations fall through unchanged.
    """
    parts = has_key.split(":")
    if len(parts) != 4:
        return q
    _, target_type, ref_field, param = parts
    if target_type not in _RESOURCE_TYPES:
        return q
    if ref_field != "patient":
        return q
    Target = live_model(target_type)
    if not hasattr(Target, "patient_guid"):
        return q

    inner = db.session.query(Target.patient_guid).distinct()
    if param == "code" and value:
        inner = _filter_by_code(inner, Target, value)
    else:
        return q
    pat_guids = [r[0] for r in inner.all() if r[0] is not None]
    if not pat_guids:
        return q.filter(False)
    if resource_type == "Patient":
        return q.filter(Live.guid.in_(pat_guids))
    if hasattr(Live, "patient_guid"):
        return q.filter(Live.patient_guid.in_(pat_guids))
    return q


def _apply_date_filter(q, Live, date_arg: str):
    """Honour FHIR prefix syntax: gt / ge / lt / le / eq."""
    prefix = date_arg[:2]
    raw = date_arg[2:] if prefix in ("gt", "ge", "lt", "le", "eq") else date_arg
    if prefix not in ("gt", "ge", "lt", "le", "eq"):
        prefix = "eq"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return q
    col = getattr(Live, "effective_at", None)
    if col is None:
        return q
    if prefix == "gt":
        return q.filter(col > dt)
    if prefix == "ge":
        return q.filter(col >= dt)
    if prefix == "lt":
        return q.filter(col < dt)
    if prefix == "le":
        return q.filter(col <= dt)
    return q.filter(col == dt)


def _has_tag(tags: list, tag_arg: str) -> bool:
    """``tag_arg`` is ``"system|code"`` or just ``"code"``."""
    parts = tag_arg.split("|", 1)
    sys, code = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
    for t in tags:
        if code and t.get("code") != code:
            continue
        if sys and t.get("system") != sys:
            continue
        return True
    return False


def _bundle_searchset(rows, resource_type: str, *, with_includes: bool = True) -> Any:
    """Build a FHIR searchset Bundle from a list of live ORM rows.

    Honours ``_include`` / ``_revinclude`` (§1.3.g) when requested.
    """
    entries = [{"resource": r.raw_json,
                "fullUrl": f"{resource_type}/{r.guid}",
                "search": {"mode": "match"}}
               for r in rows]

    if with_includes:
        included_pats: dict[str, dict] = {}
        included_obs: dict[str, dict] = {}

        for inc in request.args.getlist("_include"):
            # _include=Observation:patient style — one supported variant
            target = _parse_include(inc, resource_type)
            if target == "patient" and hasattr(rows[0] if rows else None, "patient_guid"):
                Patient = live_model("Patient")
                pat_guids = list({r.patient_guid for r in rows if r.patient_guid})
                if pat_guids:
                    pats = (db.session.query(Patient)
                            .filter(Patient.guid.in_(pat_guids))
                            .all())
                    for p in pats:
                        included_pats[p.guid] = p.raw_json

        for inc in request.args.getlist("_revinclude"):
            # _revinclude=Observation:patient — when searching Patient
            target = _parse_revinclude(inc)
            if resource_type == "Patient" and target == "Observation":
                Obs = live_model("Observation")
                pat_guids = [r.guid for r in rows]
                if pat_guids:
                    obs = (db.session.query(Obs)
                           .filter(Obs.patient_guid.in_(pat_guids))
                           .limit(500)
                           .all())
                    for o in obs:
                        included_obs[o.guid] = o.raw_json

        for guid, body in included_pats.items():
            entries.append({"resource": body, "fullUrl": f"Patient/{guid}",
                            "search": {"mode": "include"}})
        for guid, body in included_obs.items():
            entries.append({"resource": body, "fullUrl": f"Observation/{guid}",
                            "search": {"mode": "include"}})

    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "timestamp": _now_iso(),
        "total": len([e for e in entries if e.get("search", {}).get("mode") == "match"]),
        "entry": entries,
    }
    return jsonify(bundle), 200


def _parse_include(inc: str, current_type: str) -> str | None:
    """``_include=Observation:patient`` → ``"patient"`` if current_type matches."""
    if ":" not in inc:
        return None
    src, ref = inc.split(":", 1)
    if src != current_type:
        return None
    return ref


def _parse_revinclude(rev: str) -> str | None:
    """``_revinclude=Observation:patient`` → ``"Observation"``."""
    if ":" not in rev:
        return None
    return rev.split(":", 1)[0]


# ---------------------------------------------------------------------------
# GET /Patient/<guid>/$everything   — patient compartment (§1.3.e)
# ---------------------------------------------------------------------------

@bp.get("/Patient/<guid>/$everything")
@bp.get("/Patient/<guid>/%24everything")
def patient_everything(guid: str):
    Patient = live_model("Patient")
    pat_q = _org_filter(db.session.query(Patient).filter(Patient.guid == guid), Patient)
    patient = pat_q.one_or_none()
    if patient is None:
        return jsonify(_operation_outcome("error", "not-found", "Patient not found")), 404
    check_patient_allowed(guid)  # #422 — gates the whole compartment

    since = request.args.get("_since")
    types_filter = set((request.args.get("_type") or "").split(",")) - {""}
    count_cap = min(int(request.args.get("_count", 1000)), 5000)

    entries = [{
        "fullUrl": f"Patient/{guid}",
        "resource": patient.raw_json,
        "search": {"mode": "match"},
    }]

    other_types = [t for t in _RESOURCE_TYPES if t != "Patient"]
    for rt in other_types:
        if types_filter and rt not in types_filter:
            continue
        Live = live_model(rt)
        q = _org_filter(db.session.query(Live).filter(Live.patient_guid == guid), Live)
        if since:
            try:
                dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                q = q.filter(Live.updated_at >= dt)
            except ValueError:
                pass
        for r in q.limit(count_cap).all():
            entries.append({
                "fullUrl": f"{rt}/{r.guid}",
                "resource": r.raw_json,
                "search": {"mode": "match"},
            })

    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "timestamp": _now_iso(),
        "total": len(entries),
        "entry": entries,
    }
    return jsonify(bundle), 200


# ---------------------------------------------------------------------------
# $stats / $agp moved to dashboard.pdhc app/analyse/aggregations.py
# (phase 3 of the CDR1/Analyse split, ticket #289). cdr1 is now pure
# storage; aggregation runs in the analyse layer over raw Observations
# fetched via GET /api/v1/fhir/Observation.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /events  — change_feed long-poll (§1.5)
# ---------------------------------------------------------------------------

@bp.get("/events")
def events():
    """Return change_feed events newer than the given ``since`` seq.

    Designed for short-poll usage (no SSE / websockets here): client passes
    ``?since=<seq>`` and gets the next batch up to ``_count`` rows. A
    ``next_since`` field tells the client what to pass next time.
    """
    since = int(request.args.get("since", 0))
    count = min(int(request.args.get("_count", 100)), 1000)
    type_filter = request.args.get("resource_type")

    q = db.session.query(ChangeFeed).filter(ChangeFeed.seq > since)
    if type_filter:
        q = q.filter(ChangeFeed.resource_type == type_filter)

    blob = getattr(g, "access_blob", None) or {}
    if not blob.get("is_su_admin") and request.headers.get("X-Is-Admin") != "1":
        org_ids = scope_org_guids(blob)  # M0 #416: Zone-1 affiliations scope
        if not org_ids:
            org_ids = [
                s.strip() for s in request.headers.get("X-Org-Guids", "").split(",")
                if s.strip()
            ]
        if org_ids:
            q = q.filter(ChangeFeed.org_guid.in_(org_ids))
        else:
            q = q.filter(False)
    q = q.order_by(ChangeFeed.seq.asc()).limit(count)
    rows = q.all()

    next_since = rows[-1].seq if rows else since
    return jsonify({
        "events": [{
            "seq": r.seq,
            "event_type": r.event_type,
            "resource_type": r.resource_type,
            "resource_guid": r.resource_guid,
            "patient_guid": r.patient_guid,
            "org_guid": r.org_guid,
            "version_id": r.version_id,
            "sync_group_id": r.sync_group_id,
            "code_canonical": r.code_canonical,
            "source_request_id": r.source_request_id,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        } for r in rows],
        "next_since": next_since,
        "count": len(rows),
    }), 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
