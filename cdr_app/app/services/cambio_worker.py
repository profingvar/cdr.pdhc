"""Background delivery worker — processes pending Cambio deliveries with retry."""
import logging
import time
from datetime import datetime, timezone
from app import db
from app.models import CambioDeliveryLog, FhirResource, OpenEhrComposition
from .cambio_client import CambioClient

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BASE_BACKOFF = 10  # seconds


def run_delivery_cycle(app):
    """Process all pending deliveries. Called from scheduler or CLI."""
    with app.app_context():
        if not app.config.get("CAMBIO_DELIVERY_ENABLED"):
            return 0

        pending = (
            CambioDeliveryLog.query
            .filter_by(status="pending")
            .order_by(CambioDeliveryLog.created_at.asc())
            .limit(50)
            .all()
        )

        delivered = 0
        for log in pending:
            try:
                _deliver_one(log)
                delivered += 1
            except Exception as e:
                logger.error("Delivery %s failed: %s", log.guid, e)
                _mark_failed(log, str(e))

        db.session.commit()
        if delivered:
            logger.info("Delivery cycle: %d/%d succeeded", delivered, len(pending))
        return delivered


def _deliver_one(log):
    """Attempt delivery of a single log entry."""
    now = datetime.now(timezone.utc)

    # Check retry backoff
    if log.attempt_count > 0 and log.last_attempt_at:
        backoff = BASE_BACKOFF * (2 ** (log.attempt_count - 1))
        elapsed = (now - log.last_attempt_at).total_seconds()
        if elapsed < backoff:
            return  # Not yet time to retry

    log.attempt_count += 1
    log.last_attempt_at = now

    # Ensure patient exists in Cambio
    cambio_patient_id, ehr_id = CambioClient.ensure_patient(log.patient_guid)

    if log.delivery_type == "fhir":
        if not log.fhir_resource_guid:
            _mark_failed(log, "No FHIR resource linked")
            return
        fhir_row = FhirResource.query.get(log.fhir_resource_guid)
        if not fhir_row:
            _mark_failed(log, "FHIR resource not found")
            return

        cambio_id = CambioClient.deliver_fhir_observation(
            fhir_row.resource_json, cambio_patient_id
        )
        log.cambio_resource_id = cambio_id
        log.status = "delivered"
        log.delivered_at = now
        log.last_error = None

    elif log.delivery_type == "openehr":
        if not ehr_id:
            _mark_failed(log, "No Cambio EHR ID available")
            return
        if not log.openehr_comp_guid:
            _mark_failed(log, "No openEHR composition linked")
            return
        comp_row = OpenEhrComposition.query.get(log.openehr_comp_guid)
        if not comp_row:
            _mark_failed(log, "openEHR composition not found")
            return

        cambio_uid = CambioClient.deliver_openehr_composition(
            comp_row.composition_json, ehr_id
        )
        log.cambio_resource_id = cambio_uid
        log.status = "delivered"
        log.delivered_at = now
        log.last_error = None

    else:
        _mark_failed(log, f"Unknown delivery type: {log.delivery_type}")


def _mark_failed(log, error_msg):
    """Mark delivery as failed, or permanently failed after max attempts."""
    log.last_error = error_msg[:500]
    if log.attempt_count >= MAX_ATTEMPTS:
        log.status = "failed"
        logger.warning("Delivery %s permanently failed after %d attempts: %s", log.guid, log.attempt_count, error_msg)
    else:
        log.status = "pending"  # Will be retried on next cycle
        logger.info("Delivery %s attempt %d failed, will retry: %s", log.guid, log.attempt_count, error_msg)
