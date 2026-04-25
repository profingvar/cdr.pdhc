"""openEHR composition query API."""
from flask import Blueprint, request, jsonify
from app.models import OpenEhrComposition

bp = Blueprint("openehr", __name__)


@bp.get("/composition")
def search_compositions():
    patient = request.args.get("patient_guid", "").strip()
    archetype = request.args.get("archetype_id", "").strip()

    if not patient:
        return jsonify({"error": "patient_guid parameter required"}), 400

    q = OpenEhrComposition.query.filter_by(patient_guid=patient)
    if archetype:
        q = q.filter_by(archetype_id=archetype)
    q = q.order_by(OpenEhrComposition.effective_at.desc()).limit(100)
    rows = q.all()

    return jsonify({
        "total": len(rows),
        "compositions": [
            {
                "guid": r.guid,
                "archetype_id": r.archetype_id,
                "effective_at": r.effective_at.isoformat() if r.effective_at else None,
                "composition": r.composition_json,
            }
            for r in rows
        ],
    }), 200


@bp.get("/composition/<guid>")
def read_composition(guid):
    row = OpenEhrComposition.query.get(guid)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row.composition_json), 200


@bp.get("/ehr/<patient_guid>/compositions")
def patient_compositions(patient_guid):
    rows = (
        OpenEhrComposition.query
        .filter_by(patient_guid=patient_guid)
        .order_by(OpenEhrComposition.effective_at.desc())
        .limit(200)
        .all()
    )
    return jsonify({
        "patient_guid": patient_guid,
        "total": len(rows),
        "compositions": [
            {
                "guid": r.guid,
                "archetype_id": r.archetype_id,
                "effective_at": r.effective_at.isoformat() if r.effective_at else None,
                "composition": r.composition_json,
            }
            for r in rows
        ],
    }), 200
