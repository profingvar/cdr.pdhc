"""FHIR R5 read API and CapabilityStatement."""
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from app import db
from app.models import FhirResource

bp = Blueprint("fhir", __name__)


@bp.get("/metadata")
def capability_statement():
    return jsonify({
        "resourceType": "CapabilityStatement",
        "id": "cdr-pdhc",
        "status": "active",
        "kind": "instance",
        "date": datetime.now(timezone.utc).isoformat(),
        "fhirVersion": "5.0.0",
        "format": ["json"],
        "rest": [{
            "mode": "server",
            "resource": [
                {
                    "type": "Observation",
                    "interaction": [{"code": "read"}, {"code": "search-type"}],
                    "searchParam": [
                        {"name": "patient", "type": "reference"},
                        {"name": "code", "type": "token"},
                        {"name": "date", "type": "date"},
                        {"name": "_count", "type": "number"},
                    ],
                },
            ],
        }],
    }), 200


@bp.get("/Observation")
def search_observations():
    patient = request.args.get("patient", "").strip()
    code = request.args.get("code", "").strip()
    count = min(int(request.args.get("_count", 100)), 500)

    if not patient:
        return jsonify({"error": "patient parameter required"}), 400

    q = FhirResource.query.filter_by(patient_guid=patient, resource_type="Observation")
    if code:
        q = q.filter_by(loinc_code=code)
    q = q.order_by(FhirResource.effective_at.desc()).limit(count)
    rows = q.all()

    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(rows),
        "entry": [{"resource": r.resource_json} for r in rows],
    }
    return jsonify(bundle), 200


@bp.get("/Observation/<guid>")
def read_observation(guid):
    row = FhirResource.query.filter_by(guid=guid, resource_type="Observation").first()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row.resource_json), 200
