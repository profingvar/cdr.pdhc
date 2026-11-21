"""#1014 — data_origin: patient-generated data must be visible as such.

Opt-in: 'patient_generated' or NULL (= produced/endorsed by healthcare).
Pinned here: it is stored in both places, absence stays NULL, any other
value is refused before anything is stored, and the professional's reads
return it.
"""
from __future__ import annotations

import uuid

import pytest

from app import db
from app.models import ClinicalContext, DedupeRegistry, IngestRaw
from app.models.resources import RESOURCES, live_model, history_model
from app.services import data_origin


@pytest.fixture(autouse=True)
def _isolate(app):
    def _wipe():
        with app.app_context():
            for table in ("clinical_context", "observation", "fhir_resources",
                          "health_observations", "activities",
                          "dedupe_registry", "ingest_raw"):
                db.session.execute(db.text(f"DELETE FROM {table}"))
            db.session.commit()
    _wipe()
    yield
    _wipe()


def _post(client, ctx=None, flat=None):
    from tests.test_fhir_read import WRITE_HEADERS
    body = {
        "patient_guid": str(uuid.uuid4()),
        "source_type": "fhir",
        "fhir_resource": {"resourceType": "Observation", "status": "final",
                          "id": str(uuid.uuid4()),
                          "code": {"text": "t"},
                          "valueQuantity": {"value": 5.5, "unit": "mmol/L"}},
    }
    if ctx is not None:
        body["clinical_context"] = ctx
    body.update(flat or {})
    return client.post("/api/v1/ingest", json=body, headers=WRITE_HEADERS), body


def test_check_contract():
    assert data_origin.check(None) == (None, None)
    assert data_origin.check("") == (None, None)
    assert data_origin.check("patient_generated") == ("patient_generated", None)
    for bad in ("patient-generated", "healthcare", "PATIENT_GENERATED", 1):
        v, err = data_origin.check(bad)
        assert v is None and err and "invalid_data_origin" in err


@pytest.mark.parametrize("fhir_name", [r[0] for r in RESOURCES])
def test_every_live_and_history_table_has_the_column(fhir_name):
    assert "data_origin" in live_model(fhir_name).__table__.columns
    assert "data_origin" in history_model(fhir_name).__table__.columns


def test_patient_generated_is_stored_on_context_and_row(app, client):
    r, body = _post(client, {"provider_org_guid": str(uuid.uuid4()),
                             "data_origin": "patient_generated"})
    assert r.status_code == 202, r.get_json()
    with app.app_context():
        ctx = ClinicalContext.query.filter_by(
            patient_guid=body["patient_guid"]).one()
        assert ctx.data_origin == "patient_generated"
        row = live_model("Observation").query.filter_by(
            guid=body["fhir_resource"]["id"]).one()
        assert row.data_origin == "patient_generated"


def test_flat_payload_keeps_it_too(app, client):
    r, body = _post(client, None, {"provider_org_guid": str(uuid.uuid4()),
                                   "data_origin": "patient_generated"})
    assert r.status_code == 202, r.get_json()
    with app.app_context():
        assert ClinicalContext.query.filter_by(
            patient_guid=body["patient_guid"]).one().data_origin == "patient_generated"


def test_absent_means_healthcare_and_stays_null(app, client):
    r, body = _post(client, {"provider_org_guid": str(uuid.uuid4())})
    assert r.status_code == 202, r.get_json()
    with app.app_context():
        assert ClinicalContext.query.filter_by(
            patient_guid=body["patient_guid"]).one().data_origin is None
        assert live_model("Observation").query.filter_by(
            guid=body["fhir_resource"]["id"]).one().data_origin is None


@pytest.mark.parametrize("bad", ["patient-generated", "healthcare", "PGD"])
def test_unknown_value_is_refused_and_nothing_is_stored(app, client, bad):
    r, body = _post(client, {"provider_org_guid": str(uuid.uuid4()),
                             "data_origin": bad})
    assert r.status_code == 422, r.get_json()
    assert "invalid_data_origin" in r.get_json()["errors"][0]
    with app.app_context():
        assert IngestRaw.query.filter_by(patient_guid=body["patient_guid"]).count() == 0
        assert DedupeRegistry.query.filter_by(patient_guid=body["patient_guid"]).count() == 0
