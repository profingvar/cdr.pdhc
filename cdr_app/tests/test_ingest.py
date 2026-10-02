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


def test_lookup_by_source_id_hit(client, app):
    """SSOT phase 2 (#281): gateway can look up its own ingest_raw by
    the source_system_id it provided at insert time."""
    body = dict(SAMPLE_INGEST_BODY)
    body["patient_guid"] = "pat-lookup-hit"
    body["source_system_id"] = "gateway-obs-guid-aaa"
    headers = {
        "X-Service-Key": "test-gateway-key",
        "X-Source-Service": "gateway.pdhc",
    }
    resp = client.post("/api/v1/ingest", json=body, headers=headers)
    assert resp.status_code == 202

    resp = client.get(
        "/api/v1/ingest/by-source-id/gateway-obs-guid-aaa",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["source_system_id"] == "gateway-obs-guid-aaa"
    assert data["source_service"] == "gateway.pdhc"
    assert data["patient_guid"] == "pat-lookup-hit"
    assert data["status"] == "stored"
    assert data["guid"]  # ingest_raw.guid
    assert data["fhir_resource_guid"]  # FHIR layer also stored


def test_lookup_by_source_id_miss(client):
    resp = client.get(
        "/api/v1/ingest/by-source-id/never-seen-id-xyz",
        headers={
            "X-Service-Key": "test-gateway-key",
            "X-Source-Service": "gateway.pdhc",
        },
    )
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "not found"


def test_lookup_by_source_id_cross_service_isolation(client, app):
    """A source_system_id stored by one source_service must not be visible
    to another. Same id from sim.pdhc and gateway.pdhc are distinct rows
    in distinct namespaces."""
    # sim.pdhc inserts a row (proxy for "another service").
    # #732: this used to use 2gate.pdhc, an identity that was never built and
    # has now been removed from KNOWN_SERVICES. The test is about namespace
    # isolation between two DISTINCT source services, so any real second
    # service serves — it never needed that one specifically.
    body = dict(SAMPLE_INGEST_BODY)
    body["patient_guid"] = "pat-cross-1"
    body["source_system_id"] = "shared-id-001"
    resp = client.post(
        "/api/v1/ingest",
        json=body,
        headers={
            "X-Service-Key": "test-sim-key",
            "X-Source-Service": "sim.pdhc",
        },
    )
    assert resp.status_code == 202

    # gateway.pdhc looking up the same id should miss — different
    # source_service namespace.
    resp = client.get(
        "/api/v1/ingest/by-source-id/shared-id-001",
        headers={
            "X-Service-Key": "test-gateway-key",
            "X-Source-Service": "gateway.pdhc",
        },
    )
    assert resp.status_code == 404


def test_lookup_by_source_id_requires_auth(client):
    resp = client.get("/api/v1/ingest/by-source-id/anything")
    assert resp.status_code == 401


# --- #732: the 2gate.pdhc ingest identity is gone -------------------------

def test_2gate_is_not_an_ingest_identity():
    """A write identity for a service that was never built.

    It sat on the ingest path of five production CDRs and was safe only because
    TWOGATE_PDHC_SERVICE_KEY happened to be empty in every instance — the
    `not expected` branch refusing it. Protection by accident, one populated
    .env away from being a live write key with no owner.
    """
    from app.api.auth import KNOWN_SERVICES
    assert "2gate.pdhc" not in KNOWN_SERVICES
    assert set(KNOWN_SERVICES) == {"gateway.pdhc", "sim.pdhc"}


def test_an_allowlisted_caller_with_no_configured_key_is_refused(
        client, app, monkeypatch):
    """And says so in the log, rather than looking like a wrong key.

    This is the condition that hid 2gate.pdhc: refused either way, but
    indistinguishable from an entry nobody had tried yet.

    NOTE the monkeypatch. The `app` fixture is scope="session", so a plain
    `app.config[...] = ""` here leaked for the rest of the run and 403'd five
    provenance tests that were entirely innocent. monkeypatch.setitem restores
    it at teardown. Mutating a session-scoped fixture without undoing it is a
    test that breaks other tests.
    """
    monkeypatch.setitem(app.config, "GATEWAY_PDHC_SERVICE_KEY", "")
    resp = client.post(
        "/api/v1/ingest",
        json=dict(SAMPLE_INGEST_BODY),
        headers={"X-Service-Key": "anything",
                 "X-Source-Service": "gateway.pdhc"},
    )
    assert resp.status_code == 403
    assert resp.get_json() == {"error": "invalid service key"}


def test_an_unknown_source_service_is_refused(client):
    resp = client.post(
        "/api/v1/ingest",
        json=dict(SAMPLE_INGEST_BODY),
        headers={"X-Service-Key": "anything",
                 "X-Source-Service": "2gate.pdhc"},
    )
    assert resp.status_code == 403
    assert "unknown source service" in resp.get_json()["error"]
