"""Ingest pipeline and API tests."""
import json
from app import db
from app.models import IngestRaw, FhirResource, OpenEhrComposition, HealthObservation, DedupeRegistry, CambioDeliveryLog
from tests.conftest import SAMPLE_INGEST_BODY


def test_ingest_requires_auth(client):
    resp = client.post("/api/v1/ingest", json=SAMPLE_INGEST_BODY)
    assert resp.status_code == 401


def test_ingest_accepted(client, app):
    resp = client.post(
        "/api/v1/ingest",
        json=SAMPLE_INGEST_BODY,
        headers={
            "X-Service-Key": "test-gateway-key",
            "X-Source-Service": "gateway.pdhc",
        },
    )
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["status"] == "accepted"
    assert "ingest_raw_guid" in data

    with app.app_context():
        raw = IngestRaw.query.first()
        assert raw is not None
        assert raw.patient_guid == "pat-001"

        fhir = FhirResource.query.first()
        assert fhir is not None
        assert fhir.loinc_code == "29463-7"

        openehr = OpenEhrComposition.query.first()
        assert openehr is not None
        assert "body_weight" in openehr.archetype_id

        ho = HealthObservation.query.first()
        assert ho is not None
        assert ho.metric == "body_weight_kg"
        assert float(ho.value) == 85.2

        # Cambio delivery should be enqueued (has concept_guid)
        delivery = CambioDeliveryLog.query.filter_by(status="pending").all()
        assert len(delivery) >= 1


def test_ingest_dedup(client, app):
    headers = {
        "X-Service-Key": "test-gateway-key",
        "X-Source-Service": "gateway.pdhc",
    }
    # Use a unique body so it doesn't collide with other tests
    body = dict(SAMPLE_INGEST_BODY)
    body["patient_guid"] = "pat-dedup-test"

    resp1 = client.post("/api/v1/ingest", json=body, headers=headers)
    assert resp1.status_code == 202

    resp2 = client.post("/api/v1/ingest", json=body, headers=headers)
    assert resp2.status_code == 200
    assert resp2.get_json()["status"] == "duplicate"


def test_ingest_batch(client, app):
    body = {
        "items": [
            {**SAMPLE_INGEST_BODY, "patient_guid": "pat-batch-1"},
            {**SAMPLE_INGEST_BODY, "patient_guid": "pat-batch-2"},
        ]
    }
    resp = client.post(
        "/api/v1/ingest/batch",
        json=body,
        headers={
            "X-Service-Key": "test-gateway-key",
            "X-Source-Service": "gateway.pdhc",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 2
    assert data["accepted"] >= 1
