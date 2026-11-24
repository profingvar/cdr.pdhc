"""#1059: GET /api/v1/patient-extract/<guid> -- everything this CDR holds
about one patient, every version (plans/record_pdhc_extract_contract.md).

AUTH_MODE=sso through the real route; only `validate_sso_token` (the HTTP
call to sso.pdhc) is replaced, as in test_access_log_1045.
"""
from __future__ import annotations

import uuid

import pytest

from app import db
from app.models import ReadAudit
from app.models.history_guard import history_purge
from app.models.resources import history_model, live_model
from tests.test_review_1044 import (  # noqa: F401 -- fixtures and helpers
    BLOBS, GATEWAY, UNIT_A, UNIT_B, _bearer, _ingest, _isolate, _sso_on)

Obs = live_model("Observation")
NOBODY = str(uuid.uuid4())
DOCTOR = "d0c70000-0000-4000-8000-000000001059"
RECORD_KEY = "test-record-key-1059"
RECORD = {"X-Source-Service": "record.pdhc", "X-Service-Key": RECORD_KEY,
          "X-Operator-User-Guid": DOCTOR, "X-Patient-Org-Guid": UNIT_A,
          "X-Operator-User-Role": "doctor"}


@pytest.fixture(autouse=True)
def _wipe(app, monkeypatch):
    monkeypatch.setitem(app.config, "RECORD_PDHC_SERVICE_KEY", RECORD_KEY)

    def wipe():
        with app.app_context():
            db.session.rollback()
        with app.app_context(), history_purge(db.session):
            for t in ("cdr_read_audit", "encounter_history", "encounter",
                      "clinical_impression_history", "clinical_impression",
                      "condition_history", "condition", "patient"):
                db.session.execute(db.text(f"DELETE FROM {t}"))
    wipe()
    yield
    wipe()


def X(guid):
    return f"/api/v1/patient-extract/{guid}"


def _obs_version(client, patient, oid, status, value):
    body = {"patient_guid": patient, "source_type": "fhir",
            "source_system_id": str(uuid.uuid4()),
            "fhir_resource": {"resourceType": "Observation", "id": oid, "status": status,
                              "subject": {"reference": f"Patient/{patient}"},
                              "code": {"text": "P-Glukos"},
                              "effectiveDateTime": "2026-11-20T08:00:00+00:00",
                              "valueQuantity": {"value": value, "unit": "mmol/L"}},
            "clinical_context": {"patient_org_guid": UNIT_A}}
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 202, r.get_json()


@pytest.fixture
def patient(client):
    """A patient at UNIT_A with: a reviewed answer + its endorsement, a lab
    value in two versions, an Encounter and a ClinicalImpression from record
    (the latter in two versions), and a Patient row carrying a personnummer."""
    pid = str(uuid.uuid4())
    answer, _ = _ingest(client, patient=pid, unit=UNIT_A)
    r = client.post(f"/api/v1/reviews/{answer}/approve", headers=_bearer("doctor-a"),
                    json={})
    assert r.status_code in (200, 201), r.get_json()
    lab = str(uuid.uuid4())
    _obs_version(client, pid, lab, "preliminary", 6.1)
    _obs_version(client, pid, lab, "final", 6.4)
    enc = client.post("/api/v1/record/fhir/Encounter", headers=RECORD, json={
        "resourceType": "Encounter", "status": "in-progress",
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": "2026-11-24T09:00:00+00:00"}})
    assert enc.status_code == 201, enc.get_json()
    ci = {"resourceType": "ClinicalImpression", "status": "completed",
          "subject": {"reference": f"Patient/{pid}"},
          "encounter": {"reference": f"Encounter/{enc.get_json()['id']}"},
          "effectiveDateTime": "2026-11-24T09:30:00+00:00", "summary": "Bedömning 1"}
    cid = client.post("/api/v1/record/fhir/ClinicalImpression", headers=RECORD,
                      json=ci).get_json()["id"]
    r = client.put(f"/api/v1/record/fhir/ClinicalImpression/{cid}",
                   headers=dict(RECORD, **{"If-Match": 'W/"1"'}),
                   json=dict(ci, summary="Bedömning 2"))
    assert r.status_code == 200, r.get_json()
    r = client.put(f"/api/v1/record/fhir/ClinicalImpression/{cid}",
                   headers=dict(RECORD, **{"If-Match": 'W/"2"'}),
                   json=dict(ci, summary="Bedömning 3"))
    assert r.status_code == 200, r.get_json()
    db.session.add(live_model("Patient")(
        guid=pid, raw_json={"resourceType": "Patient", "id": pid,
                            "identifier": [{"value": "191212121212"}]},
        identifiers=[{"value": "191212121212"}], version_id=1))
    db.session.commit()
    return {"pid": pid, "answer": answer, "lab": lab, "ci": cid}


def _section(body, key):
    return next((s for s in body["sections"] if s["key"] == key), None)


def _direct_counts(pid):
    """Distinct resources per type in live UNION history, straight from the DB."""
    out = {}
    for t in ("Encounter", "ClinicalImpression", "Observation", "QuestionnaireResponse",
              "Condition", "MedicationStatement", "MedicationRequest",
              "AllergyIntolerance", "Procedure", "DiagnosticReport"):
        L, H = live_model(t), history_model(t)
        ids = {g for (g,) in db.session.query(L.guid).filter(L.patient_guid == pid)}
        ids |= {g for (g,) in db.session.query(H.guid).filter(H.patient_guid == pid)}
        endorse = {g for (g,) in db.session.query(L.guid).filter(
            L.patient_guid == pid, L.endorses_guid.isnot(None))}
        out[t] = (len(ids - endorse), len(endorse))
    return out


def test_su_gets_every_resource_and_the_counts_match_the_database(client, patient):
    r = client.get(X(patient["pid"]), headers=_bearer("admin"))
    assert r.status_code == 200, r.get_json()
    b = r.get_json()
    assert b["schema"] == "pdhc.patient-extract/1" and b["service"] == "cdr1.pdhc"
    assert b["patient_guid"] == patient["pid"] and b["complete"] is True
    direct = _direct_counts(patient["pid"])
    by_key = {s["key"]: s for s in b["sections"]}
    assert by_key["observations"]["count"] == direct["Observation"][0] == 2
    assert by_key["endorsements"]["count"] == sum(e for _, e in direct.values()) == 1
    assert by_key["encounters"]["count"] == direct["Encounter"][0] == 1
    assert by_key["clinical_impressions"]["count"] == direct["ClinicalImpression"][0] == 1
    for s in b["sections"]:
        assert s["count"] == len(s["items"])
    assert all(s["count"] > 0 for s in b["sections"])
    assert [s["key"] for s in b["sections"]] == [
        "encounters", "clinical_impressions", "observations", "endorsements"]


def test_every_version_oldest_first_with_both_times(client, patient):
    b = client.get(X(patient["pid"]), headers=_bearer("admin")).get_json()
    lab = next(i for i in _section(b, "observations")["items"] if i["id"] == patient["lab"])
    assert [v["version"] for v in lab["versions"]] == [1, 2]
    assert [v["status"] for v in lab["versions"]] == ["preliminary", "final"]
    assert lab["current_status"] == "final"
    assert lab["versions"][0]["fhir"]["valueQuantity"]["value"] == 6.1
    assert lab["versions"][1]["summary_sv"] == "P-Glukos 6,4 mmol/L"
    for v in lab["versions"]:
        assert v["event_time"].startswith("2026-11-20T08:00:00")
        assert v["recorded_at"] is not None
        assert v["author"]["service"] == "gateway.pdhc"
    ci = _section(b, "clinical_impressions")["items"][0]
    assert [v["version"] for v in ci["versions"]] == [1, 2, 3]
    assert [v["fhir"]["summary"] for v in ci["versions"]] == [
        "Bedömning 1", "Bedömning 2", "Bedömning 3"]
    assert ci["versions"][0]["author"] == {"guid": DOCTOR, "name": None, "role": "doctor",
                                           "service": "record.pdhc"}
    end = _section(b, "endorsements")["items"][0]
    assert end["versions"][0]["author"]["guid"] == BLOBS["doctor-a"]["user_guid"]
    assert patient["answer"] in {i["id"] for i in _section(b, "observations")["items"]}


def test_no_patient_demographics_and_no_personnummer(client, patient):
    r = client.get(X(patient["pid"]), headers=_bearer("admin"))
    assert "191212121212" not in r.get_data(as_text=True)
    assert all(i["resource_type"] != "Patient"
               for s in r.get_json()["sections"] for i in s["items"])


def test_affiliated_professional_reads_and_it_is_logged(client, patient):
    r = client.get(X(patient["pid"]), headers=_bearer("nurse-a"))
    assert r.status_code == 200
    rows = ReadAudit.query.filter_by(patient_guid=patient["pid"],
                                     resource_type="PatientExtract").all()
    assert len(rows) == 1 and rows[0].outcome == "served"
    assert rows[0].detail["action"] == "patient_extract"
    assert rows[0].caller_user_guid == BLOBS["nurse-a"]["user_guid"]
    assert rows[0].n_rows_returned == sum(len(i["versions"]) for s in r.get_json()["sections"]
                                          for i in s["items"])


def test_other_unit_gets_the_same_refusal_as_a_patient_not_held_and_it_is_logged(client, patient):
    other = client.get(X(patient["pid"]), headers=_bearer("nurse-b"))
    nobody = client.get(X(NOBODY), headers=_bearer("nurse-b"))
    assert other.status_code == nobody.status_code == 403
    assert other.get_json() == nobody.get_json()
    assert "sections" not in other.get_json()
    rows = ReadAudit.query.filter_by(patient_guid=patient["pid"],
                                     resource_type="PatientExtract").all()
    assert len(rows) == 1 and rows[0].outcome == "denied"
    assert rows[0].detail["action"] == "patient_extract"


def test_su_on_a_patient_with_nothing_here_gets_an_empty_complete_extract(client):
    b = client.get(X(NOBODY), headers=_bearer("admin")).get_json()
    assert b["sections"] == [] and b["complete"] is True
    assert any("holds no clinical data" in n for n in b["notes"])


def test_a_service_key_is_not_a_door(client, patient):
    for hdr in (GATEWAY, {k: v for k, v in RECORD.items()
                          if k in ("X-Source-Service", "X-Service-Key")}):
        r = client.get(X(patient["pid"]), headers=hdr)
        assert r.status_code == 401


def test_patients_and_must_change_are_refused(client, patient):
    assert client.get(X(patient["pid"]), headers=_bearer("patient")).status_code == 403
    assert client.get(X(patient["pid"]), headers=_bearer("mustchange")).status_code == 403
    assert client.get(X(patient["pid"]), headers=_bearer("nope")).status_code == 401
    assert client.get(X("not-a-guid"), headers=_bearer("admin")).status_code == 400


# ---------------------------------------------------------------- D9 gate
# The extract is a stricter question than the access log: SU, or a doctor or
# nurse affiliated to a unit holding the patient. Every other professional
# gets exactly the refusal of a patient not held here.

from tests.test_review_1044 import _aff, _prof  # noqa: E402


def _refusal(client, who):
    r = client.get(X(NOBODY), headers=_bearer(who))
    assert r.status_code == 403
    return r.get_json()


def test_d9_doctor_and_nurse_at_the_unit_get_it(client, patient):
    for who in ("doctor-a", "nurse-a", "admin"):
        assert client.get(X(patient["pid"]), headers=_bearer(who)).status_code == 200, who


def test_d9_other_role_at_the_unit_is_the_same_refusal_and_is_logged(client, patient):
    r = client.get(X(patient["pid"]), headers=_bearer("othercare-a"))
    assert r.status_code == 403
    assert r.get_json() == _refusal(client, "othercare-a") == _refusal(client, "nurse-b")
    rows = ReadAudit.query.filter_by(patient_guid=patient["pid"],
                                     resource_type="PatientExtract").all()
    assert len(rows) == 1 and rows[0].outcome == "denied"
    assert rows[0].detail == {"refusal_code": "OUT_OF_SCOPE", "action": "patient_extract"}


def test_d9_does_not_narrow_the_access_log(client, patient):
    # The same caller the extract refuses still reads the access log (#1045).
    r = client.get(f"/api/v1/access-log/patients/{patient['pid']}",
                   headers=_bearer("othercare-a"))
    assert r.status_code == 200, r.get_json()


def test_d9_the_role_must_be_held_at_the_patients_unit(client, patient, monkeypatch):
    # doctor elsewhere, other_care here: the doctor role does not carry over.
    monkeypatch.setitem(BLOBS, "split", _prof(
        "Split", [_aff(UNIT_B, "doctor"), _aff(UNIT_A, "other_care")]))
    r = client.get(X(patient["pid"]), headers=_bearer("split"))
    assert r.status_code == 403 and r.get_json() == _refusal(client, "nurse-b")


def test_d9_an_ended_affiliation_does_not_count(client, patient, monkeypatch):
    ended = dict(_aff(UNIT_A, "doctor"), status="ended")
    monkeypatch.setitem(BLOBS, "ended", _prof("Ended", [ended]))
    assert client.get(X(patient["pid"]), headers=_bearer("ended")).status_code == 403
