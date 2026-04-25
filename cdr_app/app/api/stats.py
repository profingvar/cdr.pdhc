"""Dashboard stats API."""
from flask import Blueprint, jsonify
from app import db
from app.models import IngestRaw, FhirResource, OpenEhrComposition, HealthObservation, DedupeRegistry

bp = Blueprint("stats", __name__)


@bp.get("/stats")
def stats():
    return jsonify({
        "ingest_raw": IngestRaw.query.count(),
        "fhir_resources": FhirResource.query.count(),
        "openehr_compositions": OpenEhrComposition.query.count(),
        "health_observations": HealthObservation.query.count(),
        "dedupe_registry": DedupeRegistry.query.count(),
        "patients": db.session.execute(
            db.text("SELECT COUNT(DISTINCT patient_guid) FROM ingest_raw")
        ).scalar() or 0,
    }), 200
