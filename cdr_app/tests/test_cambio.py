"""Cambio delivery status API tests."""
from app import db
from app.models import CambioDeliveryLog, CambioPatientMap


def test_cambio_status(client, app):
    resp = client.get("/api/v1/cambio/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "pending" in data
    assert "total" in data
    assert "patients_mapped" in data


def test_cambio_patient_not_found(client):
    resp = client.get("/api/v1/cambio/patient/nonexistent")
    assert resp.status_code == 404


def test_cambio_patient_found(client, app):
    with app.app_context():
        db.session.add(CambioPatientMap(
            pdhc_patient_guid="pat-cambio-test",
            cambio_patient_id="cambio-123",
            cambio_ehr_id="ehr-456",
        ))
        db.session.commit()

    resp = client.get("/api/v1/cambio/patient/pat-cambio-test")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["cambio_patient_id"] == "cambio-123"
    assert data["cambio_ehr_id"] == "ehr-456"


def test_cambio_retry(client, app):
    resp = client.post("/api/v1/cambio/retry")
    assert resp.status_code == 200
    assert "retried" in resp.get_json()
