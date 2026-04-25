"""Canonical store query API — flat table access."""
from flask import Blueprint, request, jsonify
from app.models import HealthObservation, Activity

bp = Blueprint("canonical", __name__)

TABLE_MAP = {
    "health_observations": HealthObservation,
    "activities": Activity,
}


@bp.get("/<table_name>")
def query_table(table_name):
    model = TABLE_MAP.get(table_name)
    if not model:
        return jsonify({"error": f"unknown table: {table_name}"}), 404

    patient = request.args.get("patient_guid", "").strip()
    if not patient:
        return jsonify({"error": "patient_guid required"}), 400

    limit = min(int(request.args.get("limit", 100)), 500)
    offset = int(request.args.get("offset", 0))

    q = model.query.filter_by(patient_guid=patient)

    metric = request.args.get("metric", "").strip()
    if metric and hasattr(model, "metric"):
        q = q.filter_by(metric=metric)

    q = q.order_by(model.effective_at.desc()).offset(offset).limit(limit)
    rows = q.all()

    return jsonify({
        "table": table_name,
        "patient_guid": patient,
        "total": len(rows),
        "rows": [
            {
                "guid": r.guid,
                "value": float(r.value) if r.value is not None else None,
                "unit": getattr(r, "unit", None),
                "metric": getattr(r, "metric", None) or getattr(r, "activity_type", None),
                "effective_at": r.effective_at.isoformat() if r.effective_at else None,
                "source_service": r.source_service,
            }
            for r in rows
        ],
    }), 200
