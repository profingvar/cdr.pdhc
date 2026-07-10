"""X1 (#407/#443) — read-audit rows on cdr's FHIR read surface."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app import db
from app.models import ReadAudit
from app.services import read_audit as ra
from tests.test_analysis_consent import (
    DOCTOR,
    RESEARCHER,
    _seed_two_patients,
    fake_canon,        # noqa: F401 — fixture reuse
    clean_tables,      # noqa: F401
)


@pytest.fixture(autouse=True)
def _clean_audit(app):
    with app.app_context():
        db.session.execute(db.text("DELETE FROM cdr_read_audit"))
        db.session.commit()
    yield


def _rows(app):
    with app.app_context():
        return db.session.query(ReadAudit).all()


def test_operator_search_writes_tuple_row(client, fake_canon, app):
    _seed_two_patients(client)
    r = client.get("/api/v1/fhir/Observation", headers=RESEARCHER)
    assert r.status_code == 200
    rows = _rows(app)
    assert len(rows) == 1
    row = rows[0]
    assert row.route == "GET /api/v1/fhir/<resource_type>"
    assert row.resource_type == "Observation"
    assert row.caller_service is None
    assert row.role_guid == "role-researcher"
    assert row.purpose == "research"
    assert row.access_basis == "research_consent"
    assert row.n_rows_returned == 2


def test_operator_instance_read_row(client, fake_canon, app):
    _seed_two_patients(client)
    from app.models.resources import live_model
    with app.app_context():
        obs = db.session.query(live_model("Observation")).first()
        guid, pat = obs.guid, obs.patient_guid
    r = client.get(f"/api/v1/fhir/Observation/{guid}", headers=DOCTOR)
    assert r.status_code == 200
    row = _rows(app)[0]
    assert row.patient_guid == pat
    assert (row.purpose, row.access_basis) == ("statistics", "same_unit")
    assert row.role_guid == "role-doctor"


def test_service_read_has_caller_but_null_tuple(client, fake_canon, app):
    _seed_two_patients(client)
    r = client.get("/api/v1/fhir/Observation",
                   headers={"X-Source-Service": "sim.pdhc",
                            "X-Service-Key": "test-sim-key"})
    assert r.status_code == 200
    row = _rows(app)[0]
    assert row.caller_service == "sim.pdhc"
    assert row.caller_user_guid is None
    assert row.role_guid is None and row.purpose is None
    assert row.access_basis is None


def test_admin_read_purpose_administration(client, fake_canon, app):
    _seed_two_patients(client)
    r = client.get("/api/v1/fhir/Observation", headers={"X-Is-Admin": "1"})
    assert r.status_code == 200
    row = _rows(app)[0]
    assert (row.purpose, row.access_basis) == ("administration", "su_admin")


def test_audit_failure_does_not_break_read(client, fake_canon, app):
    _seed_two_patients(client)
    with patch.object(ra, "ReadAudit", side_effect=RuntimeError("boom")):
        r = client.get("/api/v1/fhir/Observation", headers=DOCTOR)
    assert r.status_code == 200
    assert _rows(app) == []
