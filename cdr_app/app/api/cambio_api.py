"""Cambio CDR sandbox delivery status API."""
from flask import Blueprint, jsonify
from app import db
from app.models import CambioDeliveryLog, CambioPatientMap

bp = Blueprint("cambio", __name__)


@bp.get("/status")
def delivery_status():
    counts = {}
    for status in ("pending", "delivered", "failed", "skipped"):
        counts[status] = CambioDeliveryLog.query.filter_by(status=status).count()
    counts["total"] = sum(counts.values())
    counts["patients_mapped"] = CambioPatientMap.query.count()
    return jsonify(counts), 200


@bp.get("/patient/<pdhc_patient_guid>")
def patient_mapping(pdhc_patient_guid):
    mapping = CambioPatientMap.query.filter_by(pdhc_patient_guid=pdhc_patient_guid).first()
    if not mapping:
        return jsonify({"error": "no Cambio mapping for this patient"}), 404

    deliveries = (
        CambioDeliveryLog.query
        .filter_by(patient_guid=pdhc_patient_guid)
        .order_by(CambioDeliveryLog.created_at.desc())
        .limit(50)
        .all()
    )

    return jsonify({
        "pdhc_patient_guid": mapping.pdhc_patient_guid,
        "cambio_patient_id": mapping.cambio_patient_id,
        "cambio_ehr_id": mapping.cambio_ehr_id,
        "created_at": mapping.created_at.isoformat() if mapping.created_at else None,
        "deliveries": [
            {
                "guid": d.guid,
                "delivery_type": d.delivery_type,
                "status": d.status,
                "cambio_resource_id": d.cambio_resource_id,
                "attempt_count": d.attempt_count,
                "last_error": d.last_error,
                "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
            }
            for d in deliveries
        ],
    }), 200


@bp.post("/retry")
def retry_failed():
    updated = (
        CambioDeliveryLog.query
        .filter_by(status="failed")
        .update({"status": "pending", "attempt_count": 0, "last_error": None})
    )
    db.session.commit()
    return jsonify({"retried": updated}), 200
