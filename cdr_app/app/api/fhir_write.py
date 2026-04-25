"""FHIR write API for the CDR — platform-plan §1.2.

Endpoints:

  - ``POST /api/v1/fhir/Bundle`` — transaction-bundle endpoint, per-entry
    dispatch (§1.2.a, §1.3.i).
  - ``POST /api/v1/fhir/<Type>`` — single-resource write (§1.2.b).
  - ``PUT /api/v1/fhir/<Type>/<guid>`` — update (with optimistic
    concurrency via ``If-Match``).

Every write goes through:

    canonicalise → dedup-lookup → insert-or-update-with-history →
    sync_group → mapping_version → change_feed.

Errors:
  - 422 + ``xlate_miss`` OperationOutcome   (§1.2.d.i)
  - 422 + ``plan_miss`` OperationOutcome    (§1.2.d.ii)
  - 412 Precondition Failed on stale If-Match (§1.2.h)
  - 503 + ``transient`` OperationOutcome   when xlate or plan unreachable
  - 400 / 404 / 405 for malformed bodies / unknown types / wrong verb
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request

from app.services.canonicalisation import (
    Canonicaliser,
    operation_outcome_plan_miss,
    operation_outcome_transient,
    operation_outcome_xlate_miss,
    record_plan_miss,
)
from app.services.plan_client import PlanClient
from app.services.resource_writer import (
    EtagMismatch,
    IntegerPatientReference,
    UnknownResourceType,
    WriteContext,
    write_resource,
)
from app.services.xlate_client import XlateClient
from app import db


log = logging.getLogger(__name__)


bp = Blueprint("fhir_write", __name__)


# ---------------------------------------------------------------------------
# Lazy singletons — one xlate / plan client per app
# ---------------------------------------------------------------------------

def _get_canonicaliser() -> Canonicaliser:
    if not hasattr(current_app, "_canonicaliser"):
        xlate = XlateClient()
        plan = PlanClient()
        current_app._canonicaliser = Canonicaliser(xlate=xlate, plan=plan)
    return current_app._canonicaliser


# ---------------------------------------------------------------------------
# Request → WriteContext extractor
# ---------------------------------------------------------------------------

def _extract_context() -> WriteContext:
    return WriteContext(
        org_guid=(request.headers.get("X-Org-Guid")
                  or request.args.get("org_guid")
                  or "00000000-0000-0000-0000-000000000000"),
        source=request.headers.get("X-Source-Service") or "unknown",
        source_request_id=request.headers.get("X-Request-Id"),
        sim_run_id=request.headers.get("X-Sim-Run-Id"),
        if_match_etag=request.headers.get("If-Match"),
        mapping_version=current_app.config.get("MAPPING_VERSION") or os.environ.get("MAPPING_VERSION"),
    )


def _operation_outcome(severity: str, code: str, text: str) -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{
            "severity": severity,
            "code": code,
            "details": {"text": text},
        }],
    }


# ---------------------------------------------------------------------------
# Single-resource write helpers
# ---------------------------------------------------------------------------

def _write_one(fhir: dict, ctx: WriteContext, *,
               resource_id: str | None = None,
               update_by_guid: str | None = None) -> tuple[dict, int, dict]:
    """Run the canonicalisation + write path for one resource.

    Returns ``(body, http_status, response_headers)``.
    """
    rt = fhir.get("resourceType")
    if not rt:
        return _operation_outcome(
            "error", "structure", "resource missing resourceType"
        ), 400, {}

    canon = _get_canonicaliser()
    result = canon.canonicalise(fhir)

    if result.status == "transient":
        return operation_outcome_transient(
            result.transient_reason or "upstream service unreachable"
        ), 503, {}

    if result.status == "xlate_miss":
        return operation_outcome_xlate_miss(result.misses), 422, {}

    if result.status == "plan_miss":
        # Bookkeep each plan_miss canonical (§1.2.d.ii). Caller commits.
        for miss in result.misses:
            if miss.kind == "plan_miss":
                record_plan_miss(db.session, miss, request_id=ctx.source_request_id)
        return operation_outcome_plan_miss(result.misses), 422, {}

    # status == ok
    rewritten = result.rewritten or fhir
    ctx.primary_canonical_uri = result.primary_canonical_uri
    try:
        outcome = write_resource(
            rewritten, ctx,
            resource_id=resource_id,
            update_by_guid=update_by_guid,
        )
    except IntegerPatientReference as e:
        return _operation_outcome("error", "value", str(e)), 422, {}
    except UnknownResourceType as e:
        return _operation_outcome("error", "not-supported", str(e)), 422, {}
    except EtagMismatch as e:
        return _operation_outcome("error", "conflict", str(e)), 412, {}

    headers = {
        "ETag": outcome.etag,
        "Location": f"/api/v1/fhir/{outcome.location}",
    }
    status = 201 if outcome.operation == "created" else 200
    return outcome.resource, status, headers


# ---------------------------------------------------------------------------
# POST /api/v1/fhir/<Type>
# ---------------------------------------------------------------------------

@bp.post("/<resource_type>")
def post_resource(resource_type: str):
    """Create a new resource of ``resource_type``.

    Body must be a single FHIR resource whose ``resourceType`` matches
    the URL path.
    """
    fhir = request.get_json(silent=True)
    if not isinstance(fhir, dict):
        return jsonify(_operation_outcome(
            "error", "structure", "request body must be a FHIR resource (JSON object)"
        )), 400

    if fhir.get("resourceType") != resource_type:
        return jsonify(_operation_outcome(
            "error",
            "structure",
            f"resourceType '{fhir.get('resourceType')}' "
            f"does not match URL path '/{resource_type}'",
        )), 400

    ctx = _extract_context()
    try:
        body, status, headers = _write_one(fhir, ctx)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    resp = jsonify(body)
    resp.status_code = status
    for k, v in headers.items():
        resp.headers[k] = v
    return resp


# ---------------------------------------------------------------------------
# PUT /api/v1/fhir/<Type>/<guid>  — update with optimistic concurrency
# ---------------------------------------------------------------------------

@bp.put("/<resource_type>/<guid>")
def put_resource(resource_type: str, guid: str):
    """Update an existing resource. Requires ``If-Match`` for the current
    version's ETag (§1.2.h)."""
    fhir = request.get_json(silent=True)
    if not isinstance(fhir, dict):
        return jsonify(_operation_outcome(
            "error", "structure", "request body must be a FHIR resource"
        )), 400

    if fhir.get("resourceType") != resource_type:
        return jsonify(_operation_outcome(
            "error", "structure", "resourceType / URL mismatch"
        )), 400

    fhir["id"] = guid
    ctx = _extract_context()
    try:
        body, status, headers = _write_one(fhir, ctx, update_by_guid=guid)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    resp = jsonify(body)
    resp.status_code = status
    for k, v in headers.items():
        resp.headers[k] = v
    return resp


# ---------------------------------------------------------------------------
# POST /api/v1/fhir/Bundle  — transaction / batch dispatch
# ---------------------------------------------------------------------------

@bp.post("/Bundle")
def post_bundle():
    """Per-entry dispatch of a transaction or batch Bundle.

    For ``type=transaction``: atomic — any per-entry failure rolls back
    the whole bundle.
    For ``type=batch``: per-entry error reporting; successful entries
    persist, failed entries get an OperationOutcome in their slot.
    """
    bundle = request.get_json(silent=True)
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        return jsonify(_operation_outcome(
            "error", "structure", "request body must be a FHIR Bundle"
        )), 400

    btype = bundle.get("type", "batch")
    if btype not in ("transaction", "batch"):
        return jsonify(_operation_outcome(
            "error",
            "value",
            f"Bundle.type '{btype}' must be 'transaction' or 'batch'",
        )), 400

    ctx = _extract_context()
    entries = bundle.get("entry") or []

    response_entries: list[dict] = []
    if btype == "transaction":
        # Run each entry; on first failure, rollback the whole bundle.
        try:
            for i, entry in enumerate(entries):
                fhir = entry.get("resource")
                if not isinstance(fhir, dict):
                    raise _BundleEntryError(
                        index=i, status=400,
                        body=_operation_outcome(
                            "error", "structure",
                            f"entry[{i}].resource must be a FHIR resource",
                        ),
                    )
                body, status, headers = _write_one(fhir, ctx)
                if status >= 400:
                    raise _BundleEntryError(index=i, status=status, body=body)
                response_entries.append({
                    "response": {
                        "status": str(status),
                        "location": headers.get("Location"),
                        "etag": headers.get("ETag"),
                    },
                    "resource": body,
                })
            db.session.commit()
        except _BundleEntryError as e:
            db.session.rollback()
            return jsonify({
                "resourceType": "Bundle",
                "type": "transaction-response",
                "issue_at_entry": e.index,
                "issue": e.body,
            }), e.status
    else:
        # Batch: per-entry commits; failed entries don't poison successful ones.
        for i, entry in enumerate(entries):
            fhir = entry.get("resource")
            if not isinstance(fhir, dict):
                response_entries.append({
                    "response": {
                        "status": "400",
                        "outcome": _operation_outcome(
                            "error", "structure",
                            f"entry[{i}].resource must be a FHIR resource",
                        ),
                    }
                })
                continue
            try:
                body, status, headers = _write_one(fhir, ctx)
                db.session.commit()
            except Exception:
                db.session.rollback()
                raise
            response_entries.append({
                "response": {
                    "status": str(status),
                    "location": headers.get("Location"),
                    "etag": headers.get("ETag"),
                    "outcome": body if status >= 400 else None,
                },
                "resource": body if status < 400 else None,
            })

    return jsonify({
        "resourceType": "Bundle",
        "type": f"{btype}-response",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry": response_entries,
    }), 200


class _BundleEntryError(Exception):
    def __init__(self, *, index: int, status: int, body: dict):
        self.index = index
        self.status = status
        self.body = body
