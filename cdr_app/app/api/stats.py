"""Dashboard stats API."""
from flask import Blueprint, jsonify, request
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


@bp.get("/recent-observations")
def recent_observations():
    """Latest canonical observations, newest first — the CDR's own
    monitoring feed (line data). Distinct from the clinical read surface
    (/api/v1/clinical/*): this is an operator view of what is arriving in
    this repository, ordered by ingest time. NOTE: it does NOT apply
    spärr/org scoping — it is an operator monitor gated by the CDR's own
    SSO login, not a care-delivery clinical read.
    """
    from app.api.clinical_read import resolve_display  # display via plan.pdhc
    limit = request.args.get("limit", 50, type=int) or 50
    limit = max(1, min(limit, 200))
    Obs = live_model("Observation")
    rows = (Obs.query
            .order_by(Obs.received_at.desc(), Obs.created_at.desc())
            .limit(limit).all())
    disp_cache = {}

    def _disp(code):
        if code not in disp_cache:
            disp_cache[code] = resolve_display(code)
        return disp_cache[code]

    items = []
    for o in rows:
        code = getattr(o, "code_canonical", None)
        vq = getattr(o, "value_quantity", None)
        value = (float(vq) if vq is not None
                 else getattr(o, "value_string", None)
                 or getattr(o, "value_code", None))
        recv = getattr(o, "received_at", None)
        eff = getattr(o, "effective_at", None)
        items.append({
            "received_at": recv.isoformat() if recv else None,
            "effective_at": eff.isoformat() if eff else None,
            "patient_guid": getattr(o, "patient_guid", None),
            "code": code,
            "display": _disp(code),
            "value": value,
            "unit": getattr(o, "value_unit", None),
            "source": getattr(o, "source", None),
            "org_guid": getattr(o, "org_guid", None),
        })
    return jsonify({"items": items, "count": len(items)}), 200
