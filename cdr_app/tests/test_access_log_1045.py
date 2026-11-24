"""#1045 loggkontroll: one patient's access log from the CDR, for a PERSON.

    GET /api/v1/access-log/patients/<guid>

AUTH_MODE=sso through the real route; only `validate_sso_token` (the HTTP
call to sso.pdhc) is replaced, as in test_review_1044.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.models import ReadAudit
from app.models.resources import live_model
from tests.test_review_1044 import (  # noqa: F401 — fixtures and helpers
    BLOBS, GATEWAY, UNIT_A, UNIT_B, _bearer, _ingest, _isolate, _sso_on)

Obs = live_model("Observation")
NOBODY = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _wipe_read_audit(app):
    from app.models.history_guard import history_purge
    def _wipe():
        with app.app_context():
            db.session.rollback()
            with history_purge(db.session):
                db.session.execute(db.text("DELETE FROM cdr_read_audit"))
    _wipe()
    yield
    _wipe()


def L(guid, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return f"/api/v1/access-log/patients/{guid}" + (f"?{qs}" if qs else "")


def _log(guid):
    db.session.expire_all()
    return ReadAudit.query.filter_by(patient_guid=guid, resource_type="ReadAudit") \
        .order_by(ReadAudit.id).all()


@pytest.fixture
def patient(client):
    """A patient with one answer in the CDR, listed at UNIT_A."""
    pid = str(uuid.uuid4())
    _ingest(client, patient=pid, unit=UNIT_A)
    return pid


def test_affiliated_professional_reads_the_log(client, patient):
    db.session.add(ReadAudit(caller_service="icd11.pdhc", route="GET /api/v1/fhir/Condition",
                             resource_type="Condition", patient_guid=patient,
                             response_status=200, outcome="break_glass",
                             on_behalf_of_user_guid="11111111-1111-4111-8111-111111111111",
                             detail={"reason": "akut", "recorded_via": "event"}))
    db.session.commit()
    r = client.get(L(patient), headers=_bearer("nurse-a"))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["service"] == "cdr.pdhc" and body["patient_guid"] == patient
    actions = [i["action"] for i in body["items"]]
    assert "write fhir" in actions and "read Condition" in actions
    bg = next(i for i in body["items"] if i["action"] == "read Condition")
    assert bg["basis"] == "break_glass" and bg["outcome"] == "break_glass"
    assert bg["user_guid"] == "11111111-1111-4111-8111-111111111111"
    assert bg["via_service"] == "icd11.pdhc"
    assert bg["detail"] == {"reason": "akut"}
    ats = [i["at"] for i in body["items"]]
    assert ats == sorted(ats)


def test_the_read_of_the_log_is_logged(client, patient):
    assert _log(patient) == []
    client.get(L(patient), headers=_bearer("nurse-a"))
    rows = _log(patient)
    assert len(rows) == 1 and rows[0].outcome == "served"
    assert rows[0].caller_user_guid == BLOBS["nurse-a"]["user_guid"]


def test_other_unit_is_the_same_403_as_a_patient_not_held_and_is_logged(client, patient):
    other = client.get(L(patient), headers=_bearer("nurse-b"))
    nobody = client.get(L(NOBODY), headers=_bearer("nurse-b"))
    assert other.status_code == nobody.status_code == 403
    assert other.get_json() == nobody.get_json()
    rows = _log(patient)
    assert len(rows) == 1 and rows[0].outcome == "denied"
    assert rows[0].response_status == 403


def test_su_reads_any_patient(client, patient):
    assert client.get(L(patient), headers=_bearer("admin")).status_code == 200


def test_a_service_key_is_not_a_door(client, patient):
    # gateway's key is valid on the loader's path; here it opens nothing
    r = client.get(L(patient), headers=GATEWAY)
    assert r.status_code == 401
    assert r.get_json()["error"] == "not_authenticated"


def test_patient_must_change_and_unknown_token_are_refused(client, patient):
    assert client.get(L(patient), headers=_bearer("patient")).status_code == 403
    # a patient blob carrying an affiliation is still not a professional
    forged = client.get(L(patient), headers=_bearer("patient-forged"))
    assert forged.status_code == 403
    assert forged.get_json()["error"] == "not_professional"
    assert client.get(L(patient), headers=_bearer("mustchange")).status_code == 403
    assert client.get(L(patient), headers=_bearer("nope")).status_code == 401
    assert client.get(L(patient)).status_code == 401


def test_a_review_decision_is_in_the_log(client, patient):
    row = Obs.query.filter_by(patient_guid=patient).first()
    row.review_status, row.reviewed_by_user_guid = "endorsed", BLOBS["doctor-a"]["user_guid"]
    row.reviewed_by_name, row.reviewed_at = "Doctor A", datetime.now(timezone.utc)
    db.session.commit()
    body = client.get(L(patient), headers=_bearer("nurse-a")).get_json()
    rv = [i for i in body["items"] if i["action"] == "review endorsed"]
    assert len(rv) == 1 and rv[0]["user_label"] == "Doctor A"


def test_range_limit_and_bad_input(client, patient):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    body = client.get(L(patient, **{"from": future}), headers=_bearer("admin")).get_json()
    assert body["items"] == []
    body = client.get(L(patient, to=future), headers=_bearer("admin")).get_json()
    assert len(body["items"]) >= 1
    db.session.add(ReadAudit(caller_service="dashboard.pdhc", route="GET x",
                             patient_guid=patient, response_status=200))
    db.session.commit()
    body = client.get(L(patient, limit=1), headers=_bearer("admin")).get_json()
    assert len(body["items"]) == 1 and body["truncated"] is True
    assert client.get(L("not-a-guid"), headers=_bearer("admin")).status_code == 400
    assert client.get(L(patient, to="igår"), headers=_bearer("admin")).status_code == 400
