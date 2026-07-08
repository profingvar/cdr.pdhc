"""X2 operator-session propagation (#423) — cdr.pdhc (cdr1) adoption.

cdr1 completes the request -> gateway -> cdr1 -> Cambio chain:
  - synchronous ingest-context calls (plan/xlate canonicalisation) forward the
    operator session resolved from the inbound ingest request.
  - the async cdr1 -> Cambio hop captures the operator session on the
    CambioDeliveryLog row at ingest and the cambio_worker replays it.
"""
from unittest.mock import patch

from app import db
from app.services.session_headers import current_session_id, outbound_session_headers
from app.services.cambio_client import CambioClient
from app.services import cambio_worker
from app.services.ingest_pipeline import IngestPipeline
from app.models import CambioDeliveryLog, FhirResource

SID = "sess-cdr-1"

SAMPLE_FHIR = {
    "resourceType": "Observation",
    "status": "final",
    "code": {"coding": [{"system": "http://loinc.org", "code": "29463-7"}]},
    "valueQuantity": {"value": 85.2, "unit": "kg"},
    "effectiveDateTime": "2026-04-10T10:00:00Z",
}
BODY = {
    "patient_guid": "pat-x2-001",
    "source_type": "fhir",
    "fhir_resource": SAMPLE_FHIR,
    "canonical": {
        "table": "health_observations", "metric": "body_weight_kg",
        "value": 85.2, "unit": "kg", "source_type": "fhir",
        "source_code": "29463-7", "concept_guid": "concept-weight-001",
        "effective_at": "2026-04-10T10:00:00Z",
    },
}


def test_helper_resolves_and_gates(app):
    with app.test_request_context("/", headers={"X-Operator-Session-Id": SID}):
        assert current_session_id() == SID
        assert outbound_session_headers() == {"X-Operator-Session-Id": SID}
    with app.test_request_context("/"):
        assert outbound_session_headers() == {}


def test_cambio_headers_replay_explicit_sid(app):
    """The worker (no request context) replays a captured sid explicitly."""
    with app.app_context():
        with patch.object(CambioClient, "_get_token", return_value="tok"):
            h = CambioClient._headers(operator_session_id=SID)
    assert h.get("X-Operator-Session-Id") == SID


def test_ingest_captures_operator_session(app):
    """The operator session on the ingest request is captured on the
    CambioDeliveryLog row."""
    with app.app_context():
        IngestPipeline.process(BODY, "sim.pdhc", {"X-Operator-Session-Id": SID})
        logs = CambioDeliveryLog.query.filter_by(patient_guid="pat-x2-001").all()
        assert logs, "no delivery log created"
        assert all(l.operator_session_id == SID for l in logs)


def test_worker_replays_captured_session(app):
    """cambio_worker._deliver_one passes the captured sid to the Cambio call."""
    with app.app_context():
        fr = FhirResource(ingest_raw_guid="raw-1", patient_guid="pat-w",
                          resource_type="Observation", resource_json=SAMPLE_FHIR,
                          source_service="sim.pdhc")
        db.session.add(fr)
        db.session.flush()
        log = CambioDeliveryLog(ingest_raw_guid="raw-1", fhir_resource_guid=fr.guid,
                                patient_guid="pat-w", delivery_type="fhir",
                                status="pending", operator_session_id=SID)
        db.session.add(log)
        db.session.flush()
        seen = {}
        with patch.object(CambioClient, "ensure_patient", return_value=("cpat", "ehr")), \
             patch.object(CambioClient, "deliver_fhir_observation",
                          side_effect=lambda *a, **kw: (seen.update(kw), "cambio-id")[1]):
            cambio_worker._deliver_one(log)
        assert seen.get("operator_session_id") == SID
