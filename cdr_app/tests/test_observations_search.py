"""Tests for /api/v1/observations (gateway analyse-pull proxy target).

Phase 3 of SSOT cutover (ticket #282).
"""
import uuid
from datetime import datetime, timezone

import pytest

from app import db
from app.models import IngestRaw, FhirResource


# Header bundle for the gateway service-key path.
GW_HEADERS = {
    "X-Source-Service": "gateway.pdhc",
    "X-Service-Key": "test-gateway-key",
}


def _make_observation(sr_guid, patient_guid, value=42.0, code="29463-7"):
    """Build a FHIR R5 Observation matching the shape gateway forwards."""
    return {
        "resourceType": "Observation",
        "id": str(uuid.uuid4()),
        "status": "final",
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": code,
                "display": "test",
            }],
        },
        "subject": {"reference": f"Patient/{patient_guid}"},
        "effectiveDateTime": "2026-05-01T10:00:00Z",
        "valueQuantity": {"value": value, "unit": "kg"},
        "basedOn": [{
            "reference": f"https://request.pdhc.se/api/v1/service-requests/{sr_guid}",
            "type": "ServiceRequest",
            "identifier": {"value": sr_guid},
        }],
    }


def _seed_observation(sr_guid, patient_guid, value=42.0):
    """Insert one IngestRaw + FhirResource row matching the gateway-forwarder
    shape. Returns the FhirResource row.
    """
    fhir_json = _make_observation(sr_guid, patient_guid, value)
    raw = IngestRaw(
        guid=str(uuid.uuid4()),
        source_service="gateway.pdhc",
        source_system_id=str(uuid.uuid4()),
        patient_guid=patient_guid,
        payload_json={"patient_guid": patient_guid, "fhir_resource": fhir_json},
        payload_hash=str(uuid.uuid4()),
    )
    db.session.add(raw)
    db.session.flush()
    res = FhirResource(
        guid=str(uuid.uuid4()),
        ingest_raw_guid=raw.guid,
        patient_guid=patient_guid,
        resource_type="Observation",
        resource_json=fhir_json,
        effective_at=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
        source_service="gateway.pdhc",
    )
    db.session.add(res)
    db.session.commit()
    return res


class TestObservationsSearchAuth:

    def test_requires_service_key_header(self, client):
        r = client.get("/api/v1/observations?service_request=sr-1")
        assert r.status_code == 401

    def test_rejects_unknown_source(self, client):
        r = client.get(
            "/api/v1/observations?service_request=sr-1",
            headers={"X-Source-Service": "stranger", "X-Service-Key": "x"},
        )
        assert r.status_code == 403

    def test_rejects_wrong_service_key(self, client):
        r = client.get(
            "/api/v1/observations?service_request=sr-1",
            headers={"X-Source-Service": "gateway.pdhc",
                     "X-Service-Key": "wrong"},
        )
        assert r.status_code == 403


class TestObservationsSearch:

    def test_requires_service_request_param(self, client):
        r = client.get("/api/v1/observations", headers=GW_HEADERS)
        assert r.status_code == 400
        body = r.get_json()
        assert "service_request" in body.get("error", "")

    def test_empty_when_no_match(self, client):
        r = client.get(
            "/api/v1/observations?service_request=sr-missing",
            headers=GW_HEADERS,
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["resourceType"] == "Bundle"
        assert body["type"] == "searchset"
        assert body["total"] == 0
        assert body["entry"] == []

    def test_finds_observation_by_service_request(self, client):
        sr = "sr-aaa"
        _seed_observation(sr, "pat-A", value=10.0)
        _seed_observation("sr-bbb", "pat-B", value=20.0)  # noise
        r = client.get(
            f"/api/v1/observations?service_request={sr}",
            headers=GW_HEADERS,
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["total"] == 1
        obs = body["entry"][0]["resource"]
        assert obs["resourceType"] == "Observation"
        assert obs["subject"]["reference"] == "Patient/pat-A"

    def test_repeated_service_request_unions(self, client):
        _seed_observation("sr-x1", "pat-1")
        _seed_observation("sr-x2", "pat-2")
        _seed_observation("sr-x3", "pat-3")
        r = client.get(
            "/api/v1/observations?service_request=sr-x1&service_request=sr-x2",
            headers=GW_HEADERS,
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["total"] == 2
        patients = sorted(e["resource"]["subject"]["reference"]
                          for e in body["entry"])
        assert patients == ["Patient/pat-1", "Patient/pat-2"]

    def test_patient_filter_narrows_result(self, client):
        _seed_observation("sr-shared", "pat-X", value=10.0)
        _seed_observation("sr-shared", "pat-Y", value=20.0)
        r = client.get(
            "/api/v1/observations?service_request=sr-shared&patient=pat-X",
            headers=GW_HEADERS,
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["total"] == 1
        assert body["entry"][0]["resource"]["subject"]["reference"] == "Patient/pat-X"
