"""Observation search API used by gateway.pdhc's analyse-pull proxy.

Phase 3 of the cdr1 SSOT cutover (ticket #282, plan §7). Gateway no
longer reads from its own inbound_observations table — it proxies the
analyse-pull endpoint through to cdr1, which is the source of truth.

Endpoint
========
``GET /api/v1/observations?service_request=<sr_guid>&...``

  - ``service_request`` (repeatable, required): filter to Observations
    whose ``basedOn[*].identifier.value`` matches one of the given
    service-request GUIDs. Gateway pre-computes this list by resolving
    its own contract/SR mappings before calling us.
  - ``patient`` (optional): additional patient_guid filter.

Auth: ``@require_service_key`` — gateway must send
``X-Source-Service: gateway.pdhc`` + matching ``X-Service-Key``. cdr1
trusts gateway's SSO + phase-gate + contract-scope decision; we just
return the data for the SRs gateway asks about.

Returns a FHIR R5 ``Bundle`` of type ``searchset`` whose entries are
the verbatim FHIR Observation resources gateway sent us via the
forwarder (insert-then-send, ticket #280). The bundle's ``total`` is
the number of matched observations.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from app import db
from app.models import FhirResource
from .auth import require_service_key


logger = logging.getLogger(__name__)
bp = Blueprint("observations_search", __name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_bundle():
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "timestamp": _now_iso(),
        "total": 0,
        "entry": [],
    }


def _resource_belongs_to_sr(resource_json: dict, sr_set: set[str]) -> bool:
    """True if any ``basedOn[*].identifier.value`` matches sr_set.

    Falls back to scanning ``basedOn[*].reference`` for ``/<guid>``
    suffix in case the identifier wasn't carried (defence in depth).
    """
    if not resource_json:
        return False
    for ref in resource_json.get("basedOn", []) or []:
        ident = (ref.get("identifier") or {}).get("value")
        if ident and ident in sr_set:
            return True
        ref_url = ref.get("reference") or ""
        if ref_url:
            tail = ref_url.rsplit("/", 1)[-1]
            if tail in sr_set:
                return True
    return False


@bp.get("/observations")
@require_service_key
def search_observations():
    sr_guids = [s.strip() for s in request.args.getlist("service_request")
                if s.strip()]
    if not sr_guids:
        return jsonify({
            "error": "service_request query parameter is required "
                     "(repeatable)",
        }), 400

    patient = (request.args.get("patient") or "").strip() or None

    # First-pass DB filter: type=Observation + optional patient_guid.
    # The SR filter then runs in Python against resource_json.basedOn —
    # FhirResource has no service_request column. Volume is bounded
    # (~7060 rows on cdr1 today; orders of magnitude larger and the
    # right fix is a denormalised column + index, out of scope here).
    q = (
        db.session.query(FhirResource)
        .filter(FhirResource.resource_type == "Observation")
    )
    if patient:
        q = q.filter(FhirResource.patient_guid == patient)
    # Stable ordering — match gateway's previous behaviour.
    q = q.order_by(FhirResource.effective_at.asc().nullslast())

    sr_set = set(sr_guids)
    matched = []
    for row in q.yield_per(500):
        if _resource_belongs_to_sr(row.resource_json or {}, sr_set):
            matched.append(row.resource_json)

    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "timestamp": _now_iso(),
        "total": len(matched),
        "entry": [{"resource": r} for r in matched],
    }
    return jsonify(bundle), 200
