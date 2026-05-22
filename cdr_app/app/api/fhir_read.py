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
import math
import os
import statistics
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import and_, or_

from app import db
from app.models.resources import (
    ChangeFeed,
    history_model,
    live_model,
)


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
    org_ids = blob.get("organization_ids")
    if isinstance(org_ids, list) and org_ids:
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
        resp = jsonify(live_row.raw_json)
        resp.headers["ETag"] = live_row.etag or f'W/"{vid}"'
        return resp, 200

    hist_row = _org_filter(
        db.session.query(Hist).filter(Hist.guid == guid, Hist.version_id == vid),
        Hist,
    ).one_or_none()
    if hist_row is None:
        return jsonify(_operation_outcome("error", "not-found", "version not found")), 404
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
    return _bundle_searchset(rows, resource_type)


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
# GET /Observation/$stats    — numeric-summary stats (§1.3.b–c)
# ---------------------------------------------------------------------------

@bp.get("/Observation/$stats")
@bp.get("/Observation/%24stats")
def observation_stats():
    """Return ``{n, min, max, mean, sd, p25, p50, p75, histogram[]}`` over
    Observation.value_quantity rows matching the search constraints.

    Ticket #116: the original implementation pulled every matching row
    into Python via `q.all()` and computed stats locally. On the
    seeded demo CDRs (2M+ Observations) this took ~4 s even when the
    result was empty, because SQLAlchemy still materialised every
    column of every row. We now push the aggregation into the database
    (Postgres) using `percentile_cont` + `width_bucket`, so n=0 returns
    in low double-digit milliseconds. SQLite falls back to the Python
    path so the existing unit tests don't need a Postgres fixture.
    """
    Obs = live_model("Observation")
    q = _org_filter(db.session.query(Obs.value_quantity), Obs)
    q = q.filter(Obs.value_quantity.isnot(None))

    code = request.args.get("code")
    if code:
        q = _filter_by_code(q, Obs, code)
    org = request.args.get("org")
    if org:
        q = q.filter(Obs.org_guid == org)
    for date_arg in request.args.getlist("date"):
        q = _apply_date_filter(q, Obs, date_arg)
    buckets = max(1, min(int(request.args.get("buckets", 20)), 100))

    bind = db.session.get_bind()
    dialect = bind.dialect.name if bind is not None else "sqlite"

    if dialect == "postgresql":
        return _stats_postgres(q, buckets)
    return _stats_python(q, buckets)


def _empty_stats_response():
    return jsonify(_stats_parameters(
        n=0, min_=None, max_=None, mean=None, sd=None,
        p25=None, p50=None, p75=None, histogram=[],
    )), 200


def _stats_postgres(filtered_query, buckets):
    """Single-round-trip aggregation against Postgres.

    Folds the filtered query into a subquery so the WHERE clause runs
    inside the aggregation. Avoids materialising rows in Python and
    avoids loading the full SQLAlchemy ORM identity map.
    """
    from sqlalchemy import select, func

    subq = filtered_query.subquery()
    val = subq.c.value_quantity

    agg = db.session.execute(
        select(
            func.count(val),
            func.min(val),
            func.max(val),
            func.avg(val),
            func.stddev_pop(val),
            func.percentile_cont(0.25).within_group(val.asc()),
            func.percentile_cont(0.5).within_group(val.asc()),
            func.percentile_cont(0.75).within_group(val.asc()),
        ).select_from(subq)
    ).one()

    n, mn, mx, mean, sd, p25, p50, p75 = agg
    n = int(n or 0)
    if n == 0:
        return _empty_stats_response()

    mn = float(mn)
    mx = float(mx)
    mean = float(mean)
    sd = float(sd or 0.0)
    p25 = float(p25)
    p50 = float(p50)
    p75 = float(p75)

    if mn == mx:
        histogram = [{"low": mn, "high": mx, "count": n}]
    else:
        width = (mx - mn) / buckets
        # width_bucket(value, lo, hi, count) returns 1..count for values
        # in [lo, hi), 0 below lo, and count+1 at or above hi. We bump
        # the upper bound by a tiny epsilon so the maximum value falls
        # into the last bucket — matches the Python implementation.
        upper = mx + 1e-9
        bucket_q = (
            select(
                func.width_bucket(val, mn, upper, buckets).label("b"),
                func.count(val),
            )
            .select_from(subq)
            .group_by("b")
            .order_by("b")
        )
        counts = {int(row.b): int(row[1]) for row in db.session.execute(bucket_q)}
        edges = [mn + i * width for i in range(buckets + 1)]
        edges[-1] = upper
        histogram = [
            {
                "low": edges[i],
                "high": edges[i + 1],
                # width_bucket returns 1-based bucket indices for in-range values
                "count": counts.get(i + 1, 0),
            }
            for i in range(buckets)
        ]

    return jsonify(_stats_parameters(
        n=n, min_=mn, max_=mx, mean=mean, sd=sd,
        p25=p25, p50=p50, p75=p75, histogram=histogram,
    )), 200


def _stats_python(filtered_query, buckets):
    """Original implementation. Retained so SQLite-based tests pass."""
    rows = filtered_query.all()
    # `rows` are 1-tuples since we selected just value_quantity
    values = [float(r[0]) for r in rows if r[0] is not None]

    if not values:
        return _empty_stats_response()

    values_sorted = sorted(values)
    n = len(values_sorted)
    mn, mx = values_sorted[0], values_sorted[-1]
    mean = statistics.fmean(values_sorted)
    sd = statistics.pstdev(values_sorted) if n >= 2 else 0.0
    p25 = _percentile(values_sorted, 25)
    p50 = _percentile(values_sorted, 50)
    p75 = _percentile(values_sorted, 75)
    histogram = _histogram(values_sorted, buckets)

    return jsonify(_stats_parameters(
        n=n, min_=mn, max_=mx, mean=mean, sd=sd,
        p25=p25, p50=p50, p75=p75, histogram=histogram,
    )), 200


def _percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list."""
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (p / 100.0) * (len(sorted_values) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (idx - lo)


def _histogram(sorted_values: list[float], buckets: int) -> list[dict]:
    mn, mx = sorted_values[0], sorted_values[-1]
    if mn == mx:
        return [{"low": mn, "high": mx, "count": len(sorted_values)}]
    width = (mx - mn) / buckets
    out = []
    edges = [mn + i * width for i in range(buckets + 1)]
    edges[-1] = mx + 1e-9  # ensure max value lands in last bucket
    counts = [0] * buckets
    for v in sorted_values:
        idx = min(int((v - mn) / width), buckets - 1)
        counts[idx] += 1
    for i in range(buckets):
        out.append({
            "low": edges[i],
            "high": edges[i + 1],
            "count": counts[i],
        })
    return out


def _stats_parameters(*, n, min_, max_, mean, sd, p25, p50, p75, histogram) -> dict:
    parts = [{"name": "n", "valueInteger": n}]
    if min_ is not None: parts.append({"name": "min", "valueDecimal": min_})
    if max_ is not None: parts.append({"name": "max", "valueDecimal": max_})
    if mean is not None: parts.append({"name": "mean", "valueDecimal": mean})
    if sd is not None: parts.append({"name": "sd", "valueDecimal": sd})
    if p25 is not None: parts.append({"name": "p25", "valueDecimal": p25})
    if p50 is not None: parts.append({"name": "p50", "valueDecimal": p50})
    if p75 is not None: parts.append({"name": "p75", "valueDecimal": p75})
    parts.append({
        "name": "histogram",
        "part": [{"name": f"bucket_{i}", "valueString":
                  f"[{b['low']},{b['high']}):{b['count']}"}
                 for i, b in enumerate(histogram)],
    })
    return {"resourceType": "Parameters", "parameter": parts}


# ---------------------------------------------------------------------------
# Terminology shims (§1.3.j)
# ---------------------------------------------------------------------------

@bp.post("/CodeSystem/$lookup")
@bp.post("/CodeSystem/%24lookup")
def codesystem_lookup():
    """Proxy to termbank.pdhc's ``GET /CodeSystem/<system>/<code>``.

    Body or query params: ``system`` (canonical_lib_name like 'loinc') and
    ``code``. We forward to termbank and return the FHIR Parameters
    response verbatim.
    """
    body = request.get_json(silent=True) or {}
    params = {p["name"]: p for p in body.get("parameter") or []}
    system = (params.get("system", {}).get("valueString")
              or request.args.get("system"))
    code = (params.get("code", {}).get("valueString")
            or request.args.get("code"))
    if not system or not code:
        return jsonify(_operation_outcome(
            "error", "required", "system and code are required",
        )), 400

    base = current_app.config.get("TERMBANK_BASE_URL") \
        or os.environ.get("TERMBANK_BASE_URL", "http://127.0.0.1:9012")
    url = f"{base.rstrip('/')}/CodeSystem/{system}/{code}"
    try:
        resp = requests.get(url, timeout=5.0)
    except requests.RequestException as e:
        return jsonify(_operation_outcome(
            "error", "transient", f"termbank unreachable: {e}",
        )), 503
    if resp.status_code == 200:
        return jsonify(resp.json()), 200
    if resp.status_code == 404:
        return jsonify(_operation_outcome(
            "error", "not-found", f"{system}|{code} not in termbank",
        )), 404
    return jsonify(_operation_outcome(
        "error", "exception", f"termbank returned {resp.status_code}",
    )), 502


@bp.post("/ConceptMap/$translate")
@bp.post("/ConceptMap/%24translate")
def conceptmap_translate():
    """Proxy to xlate.pdhc's ``POST /translate``."""
    body = request.get_json(silent=True) or {}
    params = {p["name"]: p for p in body.get("parameter") or []}
    system = (params.get("system", {}).get("valueString")
              or request.args.get("system"))
    code = (params.get("code", {}).get("valueString")
            or request.args.get("code"))
    if not system or not code:
        return jsonify(_operation_outcome(
            "error", "required", "system and code are required",
        )), 400

    base = current_app.config.get("XLATE_BASE_URL") \
        or os.environ.get("XLATE_BASE_URL", "http://127.0.0.1:9017")
    url = f"{base.rstrip('/')}/translate"
    try:
        resp = requests.post(url, json={"system": system, "code": code}, timeout=5.0)
    except requests.RequestException as e:
        return jsonify(_operation_outcome(
            "error", "transient", f"xlate unreachable: {e}",
        )), 503
    if resp.status_code in (200, 422):
        return jsonify(resp.json()), resp.status_code
    return jsonify(_operation_outcome(
        "error", "exception", f"xlate returned {resp.status_code}",
    )), 502


@bp.post("/ValueSet/$validate-code")
@bp.post("/ValueSet/%24validate-code")
def valueset_validate_code():
    """Proxy to plan.pdhc's ``GET /api/v1/ValueSet/$validate-code``."""
    body = request.get_json(silent=True) or {}
    params = {p["name"]: p for p in body.get("parameter") or []}
    system = (params.get("system", {}).get("valueString")
              or request.args.get("system"))
    code = (params.get("code", {}).get("valueString")
            or request.args.get("code"))
    if not system or not code:
        return jsonify(_operation_outcome(
            "error", "required", "system and code are required",
        )), 400

    base = current_app.config.get("PLAN_BASE_URL") \
        or os.environ.get("PLAN_BASE_URL", "http://127.0.0.1:9030")
    url = f"{base.rstrip('/')}/api/v1/ValueSet/$validate-code"
    try:
        resp = requests.get(url, params={"system": system, "code": code}, timeout=5.0)
    except requests.RequestException as e:
        return jsonify(_operation_outcome(
            "error", "transient", f"plan unreachable: {e}",
        )), 503
    if resp.status_code == 200:
        return jsonify(resp.json()), 200
    return jsonify(_operation_outcome(
        "error", "exception", f"plan returned {resp.status_code}",
    )), 502


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
        org_ids = blob.get("organization_ids") or []
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
