"""openEHR composition by-GUID read endpoint.

Search endpoints (`GET /composition` with patient filter and
`GET /ehr/<patient>/compositions`) were moved to dashboard.pdhc's
analyse layer (`app/analyse/openehr.py`) in #292. cdr1 keeps only the
per-GUID storage-style lookup; multi-row queries belong to the
federation layer.
"""
from flask import Blueprint, jsonify
from app.models import OpenEhrComposition

bp = Blueprint("openehr", __name__)


@bp.get("/composition/<guid>")
def read_composition(guid):
    row = OpenEhrComposition.query.get(guid)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row.composition_json), 200
