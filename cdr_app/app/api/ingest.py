"""Ingest API — receives observations from gateways."""
import logging
from flask import Blueprint, request, jsonify, g
from app import db
from app.models import IngestRaw, DedupeRegistry, AuditLog, FhirResource
from app.services.ingest_pipeline import IngestPipeline
from .auth import require_service_key

logger = logging.getLogger(__name__)
bp = Blueprint("ingest", __name__)


@bp.post("/ingest")
@require_service_key
def ingest_single():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON body"}), 400

    patient_guid = body.get("patient_guid")
    if not patient_guid:
        return jsonify({"error": "missing patient_guid"}), 400

    result = IngestPipeline.process(body, g.source_service, request.headers)

    status_code = {"accepted": 202, "duplicate": 200, "rejected": 422}.get(result["status"], 500)
    return jsonify(result), status_code


@bp.post("/ingest/batch")
@require_service_key
def ingest_batch():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON body"}), 400

    # Accept both {"items": [...]} and bare [...]
    items = body if isinstance(body, list) else body.get("items", [])
    if not isinstance(items, list):
        return jsonify({"error": "expected items array"}), 400
    if len(items) > 100:
        return jsonify({"error": "max 100 items per batch"}), 400

    results = []
    accepted = duplicate = rejected = 0
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("patient_guid"):
            results.append({"index": i, "status": "rejected", "errors": ["missing patient_guid"]})
            rejected += 1
            continue
        r = IngestPipeline.process(item, g.source_service, request.headers)
        r["index"] = i
        results.append(r)
        if r["status"] == "accepted":
            accepted += 1
        elif r["status"] == "duplicate":
            duplicate += 1
        else:
            rejected += 1

    return jsonify({
        "total": len(items),
        "accepted": accepted,
        "duplicate": duplicate,
        "rejected": rejected,
        "entries": results,
    }), 200



@bp.get("/ingest/by-source-id/<source_system_id>")
@require_service_key
def lookup_by_source_id(source_system_id):
    """Look up an ingest_raw row by the calling service's source_system_id.

    Source-service scoped: gateway.pdhc only sees gateway.pdhc rows,
    sim.pdhc only sees sim.pdhc rows. The source_system_id namespace
    is per-service.

    Used by gateway.pdhc/receipt_service.py to confirm a delivery
    landed when its local InboundObservation row has been deleted
    (SSOT phase 5).
    """
    raw = IngestRaw.query.filter_by(
        source_system_id=source_system_id,
        source_service=g.source_service,
    ).first()
    if not raw:
        return jsonify({"error": "not found"}), 404

    fhir_row = FhirResource.query.filter_by(ingest_raw_guid=raw.guid).first()
    return jsonify({
        "guid": raw.guid,
        "patient_guid": raw.patient_guid,
        "source_service": raw.source_service,
        "source_system_id": raw.source_system_id,
        "created_at": raw.created_at.isoformat() if raw.created_at else None,
        "received_at": raw.received_at.isoformat() if raw.received_at else None,
        "fhir_resource_guid": fhir_row.guid if fhir_row else None,
        "status": "stored",
    }), 200
