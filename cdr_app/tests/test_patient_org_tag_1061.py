"""#1061: on a read made with record.pdhc's key, every Observation carries the
patient's care unit at order time (observation.patient_org_guid, #768/#809) as
    meta.tag {system: urn:pdhc:patient-org-guid, code: <guid>}
so record can apply spärr per result without its vidimering tasks.

Only record's key sees it; another service key sees the stored body unchanged;
the stored raw_json is never modified; a row with no patient_org_guid gets no
tag; the PERFORMER org (org_guid) is never what is tagged.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

import pytest

from app import db
from app.api.fhir_read import PATIENT_ORG_TAG_SYSTEM
from app.models.history_guard import history_purge
from app.models.resources import history_model, live_model

RECORD_KEY = "test-record-key-1061"
RECORD = {"X-Source-Service": "record.pdhc", "X-Service-Key": RECORD_KEY,
          "X-Operator-User-Guid": "d0c70000-0000-4000-8000-000000001061"}
DASH_KEY = "test-dash-1061"
DASH = {"X-Source-Service": "dashboard.pdhc", "X-Service-Key": DASH_KEY}
UNIT = "aaaaaaaa-0000-4000-8000-000000001061"
PERFORMER = "eeeeeeee-0000-4000-8000-000000001061"
BODY = {"resourceType": "Observation", "status": "final",
        "meta": {"tag": [{"system": "urn:other", "code": "keep"}]},
        "code": {"text": "P-Glukos"}}


@pytest.fixture(autouse=True)
def _keys(app, monkeypatch):
    monkeypatch.setitem(app.config, "RECORD_PDHC_SERVICE_KEY", RECORD_KEY)
    monkeypatch.setitem(app.config, "DASHBOARD_PDHC_SERVICE_KEY", DASH_KEY)


@pytest.fixture(autouse=True)
def _wipe(app):
    def wipe():
        with app.app_context():
            db.session.rollback()
        with app.app_context(), history_purge(db.session):
            for t in ("observation_history", "observation", "cdr_read_audit"):
                db.session.execute(db.text(f"DELETE FROM {t}"))
            db.session.commit()
    wipe()
    yield
    wipe()


def _row(patient, unit=UNIT, version=1):
    M = live_model("Observation")
    row = M(guid=str(uuid.uuid4()), patient_guid=patient, patient_org_guid=unit,
            org_guid=PERFORMER, version_id=version, code_canonical="glucose",
            effective_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
            raw_json=copy.deepcopy(BODY))
    db.session.add(row)
    db.session.commit()
    return row.guid


def _tags(res):
    return [t for t in ((res.get("meta") or {}).get("tag") or [])
            if t.get("system") == PATIENT_ORG_TAG_SYSTEM]


def _search(client, patient, headers):
    r = client.get(f"/api/v1/fhir/Observation?patient={patient}", headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return [e["resource"] for e in r.get_json()["entry"]]


def test_record_key_search_carries_the_patient_unit(client):
    p = str(uuid.uuid4())
    _row(p)
    [res] = _search(client, p, RECORD)
    assert _tags(res) == [{"system": PATIENT_ORG_TAG_SYSTEM, "code": UNIT}]
    assert PERFORMER not in str(res)                       # never the performer
    assert {"system": "urn:other", "code": "keep"} in res["meta"]["tag"]   # others kept


def test_record_key_read_history_and_vread_carry_it(client):
    p = str(uuid.uuid4())
    gid = _row(p)
    r = client.get(f"/api/v1/fhir/Observation/{gid}", headers=RECORD)
    assert _tags(r.get_json())[0]["code"] == UNIT
    r = client.get(f"/api/v1/fhir/Observation/{gid}/_history", headers=RECORD)
    assert _tags(r.get_json()["entry"][0]["resource"])[0]["code"] == UNIT
    r = client.get(f"/api/v1/fhir/Observation/{gid}/_history/1", headers=RECORD)
    assert _tags(r.get_json())[0]["code"] == UNIT


def test_another_service_key_sees_the_stored_body_unchanged(client):
    p = str(uuid.uuid4())
    _row(p)
    [res] = _search(client, p, DASH)
    assert _tags(res) == []
    assert res == BODY


def test_the_stored_row_is_never_modified(client):
    p = str(uuid.uuid4())
    gid = _row(p)
    _search(client, p, RECORD)
    client.get(f"/api/v1/fhir/Observation/{gid}", headers=RECORD)
    db.session.expire_all()
    row = live_model("Observation").query.get(gid)
    assert row.raw_json == BODY


def test_a_row_without_patient_unit_gets_no_tag(client):
    p = str(uuid.uuid4())
    _row(p, unit=None)
    [res] = _search(client, p, RECORD)
    assert _tags(res) == []


def test_a_stale_tag_in_the_body_is_replaced_not_duplicated(client):
    p = str(uuid.uuid4())
    gid = _row(p)
    row = live_model("Observation").query.get(gid)
    body = copy.deepcopy(BODY)
    body["meta"]["tag"].append({"system": PATIENT_ORG_TAG_SYSTEM, "code": "forged"})
    row.raw_json = body
    db.session.commit()
    [res] = _search(client, p, RECORD)
    assert _tags(res) == [{"system": PATIENT_ORG_TAG_SYSTEM, "code": UNIT}]
