"""Dashboard stats API."""
from flask import Blueprint, jsonify
from app import db
from app.models import IngestRaw, FhirResource, OpenEhrComposition, HealthObservation, DedupeRegistry
from app.models.resources import RESOURCES, live_model

bp = Blueprint("stats", __name__)


@bp.get("/stats")
def stats():
    # Per-type FHIR live tables (Phase-1.2 ingest path).
    fhir_per_type = sum(live_model(name).query.count() for name, *_ in RESOURCES)
    # Patient count: prefer the per-type `patient` table; fall back to
    # legacy ingest_raw distinct-by-guid for older CDRs.
    patients = live_model("Patient").query.count() or (
        db.session.execute(
            db.text("SELECT COUNT(DISTINCT patient_guid) FROM ingest_raw")
        ).scalar() or 0
    )
    return jsonify({
        "ingest_raw": IngestRaw.query.count(),
        "fhir_resources": FhirResource.query.count() + fhir_per_type,
        "openehr_compositions": OpenEhrComposition.query.count(),
        "health_observations": HealthObservation.query.count(),
        "dedupe_registry": DedupeRegistry.query.count(),
        "patients": patients,
    }), 200
