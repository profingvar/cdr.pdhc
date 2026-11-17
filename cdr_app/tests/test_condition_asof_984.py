"""#984: the CDR answers "what did this diagnosis say on date X", replayed in
CLINICAL time from its version history -- icd11.pdhc's state_at, now in the
one register."""
from __future__ import annotations

from app.models import db
from app.services import condition_asof as asof
from tests.test_fhir_write import _SERVICE_KEYS, clean_tables, fake_canon  # noqa: F401

P = "462c1906-d09f-415b-80af-e1b094b54451"
ORG = "11111111-2222-4333-8444-555555555555"
HDR = {"X-Source-Service": "sim.pdhc", "X-Service-Key": _SERVICE_KEYS["sim.pdhc"],
       "X-Patient-Org-Guid": ORG, "X-Is-Admin": "true"}
CID = "c0000000-0000-4000-8000-000000000984"


def _v(valid_from, status, code="38341003", note=None):
    r = {"resourceType": "Condition", "id": CID, "subject": {"reference": f"Patient/{P}"},
         "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                        "code": status}]},
         "code": {"coding": [{"system": "https://termbank.pdhc.se/CodeSystem/snomed", "code": code}]},
         "extension": [{"url": asof.VALID_FROM_EXT, "valueDateTime": valid_from}]}
    if note:
        r["note"] = [{"text": note}]
    return r


def _write(client, body):
    r = client.put(f"/api/v1/fhir/Condition/{CID}", json=body, headers=HDR)
    assert r.status_code in (200, 201), r.get_json()


def _seed(client):
    _write(client, _v("2025-01-01T00:00:00+00:00", "active", note="v1"))
    # written LATER, but a clinician states it was true from 2024-06:
    _write(client, _v("2024-06-01T00:00:00+00:00", "active", code="44054006", note="v2 backdated"))
    _write(client, _v("2026-01-01T00:00:00+00:00", "resolved", note="v3"))


def _at(client, date):
    return client.get(f"/api/v1/fhir/Condition/{CID}/$at?date={date}", headers=HDR)


def test_the_replay_is_in_CLINICAL_time_not_write_order(app, client, fake_canon):
    with app.app_context():
        _seed(client)
        r = _at(client, "2024-07-01")
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["note"][0]["text"] == "v2 backdated"    # written 2nd, valid first
        assert r.headers["X-CDR-Valid-From-Source"] == "extension"
        assert _at(client, "2025-06-01").get_json()["note"][0]["text"] == "v1"
        assert _at(client, "2026-02-01").get_json()["note"][0]["text"] == "v3"
        assert _at(client, "2026-02-01").get_json()["clinicalStatus"]["coding"][0]["code"] == "resolved"


def test_before_any_assertion_it_was_NOT_YET_asserted(app, client, fake_canon):
    with app.app_context():
        _seed(client)
        r = _at(client, "2024-01-01")
        assert r.status_code == 404 and "not yet asserted" in r.get_data(as_text=True)


def test_a_tie_on_valid_from_is_broken_by_the_order_the_statements_were_made():
    same = "2025-01-01T00:00:00+00:00"
    vs = [(2, _v(same, "resolved", note="second")), (1, _v(same, "active", note="first"))]
    vid, res, _ = asof.state_at(vs, asof.parse_instant("2025-02-01"))
    assert vid == 2 and res["note"][0]["text"] == "second"


def test_a_bare_date_includes_that_whole_day_and_a_zone_less_time_is_refused():
    assert asof.state_at([(1, _v("2025-01-01T15:00:00+00:00", "active"))],
                         asof.parse_instant("2025-01-01")) is not None
    import pytest
    with pytest.raises(ValueError):
        asof.parse_instant("2025-01-01T10:00:00")


def test_without_the_extension_recordedDate_then_onset_and_never_a_silent_date():
    assert asof.valid_from({"recordedDate": "2025-03-01"})[1] == "recordedDate"
    assert asof.valid_from({"onsetDateTime": "2025-03-01"})[1] == "onsetDateTime"
    assert asof.valid_from({})[1] == "none"


def test_the_patient_list_at_a_date_and_the_read_is_LOGGED(app, client, fake_canon):
    from app.models import ReadAudit as CdrReadAudit
    with app.app_context():
        _seed(client)
        n0 = CdrReadAudit.query.count()
        b = client.get(f"/api/v1/fhir/Condition/$at?patient={P}&date=2025-06-01", headers=HDR).get_json()
        assert b["total"] == 1 and b["entry"][0]["resource"]["note"][0]["text"] == "v1"
        assert b["entry"][0]["fullUrl"].startswith(f"Condition/{CID}/_history/")
        assert client.get(f"/api/v1/fhir/Condition/$at?patient={P}&date=2023-01-01",
                          headers=HDR).get_json()["total"] == 0
        assert CdrReadAudit.query.count() > n0




def test_missing_date_or_patient_is_400(app, client, fake_canon):
    with app.app_context():
        assert client.get(f"/api/v1/fhir/Condition/{CID}/$at", headers=HDR).status_code == 400
        assert client.get("/api/v1/fhir/Condition/$at?date=2025-01-01", headers=HDR).status_code == 400
