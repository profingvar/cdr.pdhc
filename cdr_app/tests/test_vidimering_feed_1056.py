"""#1056 (record.pdhc stage 4, the CDR part): what record's vidimering sync
reads from cdr1.

1. A NEW observation reaches the change feed. Before #1056 only a new VERSION
   did (`_new_observation_version`), so a first, final result from gateway
   never appeared in `GET /api/v1/fhir/events` and record could never make a
   task for it.
2. An observation's feed row names its status, the PATIENT's unit at order
   time (observation.patient_org_guid, #809 -- not org_guid, the performer,
   #768) and the orderer (clinical_context.requester_user_guid). Null when the
   producer sent none, never a guess.
3. record's read-only key reads the feed (organisation-blind service blob).
4. The clinical observation detail (dashboard's limit check) names the
   version and status it describes.

Synthetic data only.
"""
from __future__ import annotations

import uuid

import pytest

from app import db
from app.models.resources import ChangeFeed
from tests.test_fhir_write import clean_tables, fake_canon  # noqa: F401
from tests.test_record_stage2_1054 import _keys  # noqa: F401  (autouse: record key)
from tests.test_review_1044 import _isolate, clinical_app  # noqa: F401

RECORD = {"X-Source-Service": "record.pdhc", "X-Service-Key": "test-record-key-1054"}
GATEWAY = {"X-Source-Service": "gateway.pdhc", "X-Service-Key": "test-gateway-key"}
DASH = {"X-Source-Service": "dashboard.pdhc", "X-Service-Key": "test-dash-1044",
        "X-Access-Purpose": "care-delivery", "X-Is-Admin": "1",
        "X-Admin-Read-Reason": "test"}
UNIT = "aaaaaaaa-0000-4000-8000-00000000000a"
PERFORMER = "cccccccc-0000-4000-8000-00000000000c"
ORDERER = "d0c70000-0000-4000-8000-000000001056"


def _ingest(client, pid, status, value, *, oid=None, unit=UNIT, orderer=ORDERER, sr=None):
    oid = oid or str(uuid.uuid4())
    fhir = {"resourceType": "Observation", "id": oid, "status": status,
            "subject": {"reference": f"Patient/{pid}"}, "code": {"text": "P-Glukos"},
            "effectiveDateTime": "2026-11-20T08:00:00+00:00",
            "performer": [{"type": "Organization", "identifier": {"value": PERFORMER}}],
            "valueQuantity": {"value": value, "unit": "mmol/L"}}
    ctx = {"service_request_guid": sr or str(uuid.uuid4())}
    if unit:
        ctx["patient_org_guid"] = unit
    if orderer:
        ctx["requester_user_guid"] = orderer
    r = client.post("/api/v1/ingest", headers=GATEWAY, json={
        "patient_guid": pid, "source_type": "fhir", "source_system_id": str(uuid.uuid4()),
        "fhir_resource": fhir, "clinical_context": ctx})
    assert r.status_code == 202, r.get_json()
    return oid


def _events(client, since=0):
    r = client.get(f"/api/v1/fhir/events?since={since}&resource_type=Observation",
                   headers=RECORD)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["events"]


def test_a_new_observation_reaches_the_feed_with_its_patient_unit_and_orderer(client):
    pid = str(uuid.uuid4())
    oid = _ingest(client, pid, "final", 6.1)
    (e,) = [e for e in _events(client) if e["resource_guid"] == oid]
    assert (e["event_type"], e["version_id"], e["status"]) == ("create", 1, "final")
    assert e["patient_guid"] == pid
    assert e["patient_org_guid"] == UNIT          # the patient's unit ...
    assert e["org_guid"] == PERFORMER             # ... is not the performer
    assert e["requester_user_guid"] == ORDERER
    assert e["service_request_guid"] and e["source"] == "gateway.pdhc"


def test_a_correction_is_an_update_event_on_version_2_and_keeps_the_unit(client):
    pid = str(uuid.uuid4())
    oid = _ingest(client, pid, "final", 6.1)
    _ingest(client, pid, "corrected", 6.4, oid=oid, unit=None)
    evs = [e for e in _events(client) if e["resource_guid"] == oid]
    assert [(e["event_type"], e["version_id"], e["status"]) for e in evs] == [
        ("create", 1, "final"), ("update", 2, "corrected")]
    assert evs[1]["patient_org_guid"] == UNIT     # kept from v1 (#1054 rule)


def test_a_missing_orderer_or_unit_is_null_not_a_guess(client):
    pid = str(uuid.uuid4())
    oid = _ingest(client, pid, "final", 6.1, unit=None, orderer=None)
    (e,) = [e for e in _events(client) if e["resource_guid"] == oid]
    assert e["patient_org_guid"] is None and e["requester_user_guid"] is None
    assert e["org_guid"] == PERFORMER            # control: the performer is there


def test_an_identical_report_adds_no_event(client):
    pid = str(uuid.uuid4())
    oid = _ingest(client, pid, "final", 6.1)
    n = ChangeFeed.query.filter_by(resource_guid=oid).count()
    _ingest(client, pid, "final", 6.1, oid=oid)
    db.session.expire_all()
    assert ChangeFeed.query.filter_by(resource_guid=oid).count() == n == 1


def test_older_feed_rows_without_the_fields_read_as_null(client):
    db.session.add(ChangeFeed(event_type="update", resource_type="Observation",
                              resource_guid=str(uuid.uuid4()), patient_guid=str(uuid.uuid4()),
                              version_id=2, payload_summary={"operation": "update"}))
    db.session.commit()
    e = _events(client)[-1]
    assert e["status"] is None and e["patient_org_guid"] is None


def test_the_clinical_detail_names_its_version_and_status(clinical_app):
    client = clinical_app.test_client()
    pid = str(uuid.uuid4())
    oid = _ingest(client, pid, "final", 6.1)
    _ingest(client, pid, "amended", 6.3, oid=oid)
    d = client.get(f"/api/v1/clinical/observation/{oid}", headers=DASH).get_json()
    assert (d.get("version"), d.get("status"), d.get("value")) == (2, "amended", 6.3), d
