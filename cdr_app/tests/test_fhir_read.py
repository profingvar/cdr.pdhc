"""Phase 1.3 — read / search / vread / $everything / terminology shims.

Covers the §1.5 unit-test list items related to the query surface:

  - everything, chained_search, include, terminology_shims
  - history_on_update + vread (the history side; write side covered in
    test_fhir_write.py)

$stats / $agp aggregations were moved to dashboard.pdhc analyse layer
in phase 3 of the CDR1/Analyse split (ticket #289). Their coverage is
in dashboard.pdhc/app/tests/test_aggregations.py.

Org-scoping by ``X-Org-Guids`` header is exercised so we know the
Rule-24 filter is wired.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app import db
from app.models.resources import (
    ChangeFeed,
    history_model,
    live_model,
)
from app.services.canonicalisation import CanonicalisationResult


# ---------------------------------------------------------------------------
# Fixtures (same fake canonicaliser pattern as test_fhir_write.py)
# ---------------------------------------------------------------------------

class _FakeCanonicaliser:
    def canonicalise(self, fhir):
        primary = None
        for path in ("code", "valueCodeableConcept", "medicationCodeableConcept"):
            cc = fhir.get(path)
            if cc and cc.get("coding"):
                c0 = cc["coding"][0]
                if c0.get("system", "").startswith("https://termbank.pdhc.se/"):
                    primary = f"{c0['system'].rstrip('/')}/{c0['code']}"
                    break
        return CanonicalisationResult(
            status="ok", rewritten=fhir, primary_canonical_uri=primary
        )


@pytest.fixture
def fake_canon(app):
    fake = _FakeCanonicaliser()
    app._canonicaliser = fake
    yield fake
    if hasattr(app, "_canonicaliser"):
        delattr(app, "_canonicaliser")


@pytest.fixture(autouse=True)
def clean_tables(app):
    with app.app_context():
        for table in (
            "observation_history", "observation",
            "patient_history", "patient",
            "questionnaire_response_history", "questionnaire_response",
            "condition_history", "condition",
            "encounter_history", "encounter",
            "change_feed", "sync_group", "cdr_audit_plan_miss",
        ):
            db.session.execute(db.text(f"DELETE FROM {table}"))
        db.session.commit()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ORG = "org-aaaa"
ADMIN_HEADERS = {"X-Is-Admin": "1"}
ORG_HEADERS = {"X-Org-Guids": ORG}
WRITE_HEADERS = {"X-Org-Guid": ORG, "X-Source-Service": "test"}

PATIENT_GUID = "pat-1111-2222-3333-4444"


def _post(client, path, body, *, request_id=None):
    headers = dict(WRITE_HEADERS)
    if request_id:
        headers["X-Request-Id"] = request_id
    return client.post(path, json=body, headers=headers)


def _hba1c(value=6.4, eff="2026-04-01T10:00:00Z", patient=PATIENT_GUID, code="4548-4"):
    return {
        "resourceType": "Observation",
        "status": "final",
        "subject": {"reference": f"Patient/{patient}"},
        "code": {
            "coding": [{
                "system": "https://termbank.pdhc.se/CodeSystem/loinc",
                "code": code,
            }],
        },
        "effectiveDateTime": eff,
        "valueQuantity": {"value": value, "unit": "%", "code": "%"},
    }


def _create_patient(client, guid=PATIENT_GUID, ident_value="19121212-1212"):
    """Create a patient with the requested GUID (PUT-by-id) so cross-resource
    references resolve. POST would mint a new server-side id."""
    return client.put(
        f"/api/v1/fhir/Patient/{guid}",
        json=_patient_body(guid=guid, ident_value=ident_value),
        headers=WRITE_HEADERS,
    )


def _patient_body(guid=PATIENT_GUID, ident_value="19121212-1212"):
    return {
        "resourceType": "Patient",
        "id": guid,
        "active": True,
        "identifier": [{
            "system": "urn:oid:1.2.752.129.2.1.3.1",
            "value": ident_value,
        }],
        "name": [{"family": "Test", "given": ["Pat"]}],
        "gender": "female",
        "birthDate": "1955-04-15",
    }


def _condition_body(patient=PATIENT_GUID, code="44054006",
                    onset="2020-01-15"):
    """Minimal Condition body."""
    return {
        "resourceType": "Condition",
        "subject": {"reference": f"Patient/{patient}"},
        "code": {
            "coding": [{
                "system": "https://termbank.pdhc.se/CodeSystem/snomed",
                "code": code,
                "display": "Type 2 diabetes mellitus",
            }],
        },
        "onsetDateTime": onset,
        "clinicalStatus": {"coding": [{"code": "active"}]},
    }


# ---------------------------------------------------------------------------
# Read instance + ETag
# ---------------------------------------------------------------------------

def test_read_returns_resource_with_etag(client, fake_canon):
    r = _post(client, "/api/v1/fhir/Observation", _hba1c())
    guid = r.get_json()["id"]
    resp = client.get(f"/api/v1/fhir/Observation/{guid}", headers=ORG_HEADERS)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["id"] == guid
    assert resp.headers["ETag"].startswith('W/"1"')


def test_read_404_for_missing(client, fake_canon):
    resp = client.get("/api/v1/fhir/Observation/nonexistent", headers=ORG_HEADERS)
    assert resp.status_code == 404


def test_read_org_scoped_non_admin(client, fake_canon):
    """Rule 24: a non-admin from a different org sees nothing."""
    r = _post(client, "/api/v1/fhir/Observation", _hba1c())
    guid = r.get_json()["id"]
    resp = client.get(
        f"/api/v1/fhir/Observation/{guid}",
        headers={"X-Org-Guids": "different-org"},
    )
    assert resp.status_code == 404


def test_admin_sees_cross_org(client, fake_canon):
    r = _post(client, "/api/v1/fhir/Observation", _hba1c())
    guid = r.get_json()["id"]
    resp = client.get(
        f"/api/v1/fhir/Observation/{guid}", headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_by_patient(client, fake_canon):
    _post(client, "/api/v1/fhir/Observation", _hba1c())
    _post(client, "/api/v1/fhir/Observation",
          _hba1c(value=7.2, eff="2026-04-15T10:00:00Z"))
    resp = client.get(
        f"/api/v1/fhir/Observation?patient={PATIENT_GUID}",
        headers=ORG_HEADERS,
    )
    assert resp.status_code == 200
    bundle = resp.get_json()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "searchset"
    assert bundle["total"] == 2


def test_search_by_code_with_system(client, fake_canon):
    _post(client, "/api/v1/fhir/Observation", _hba1c(code="4548-4"))
    _post(client, "/api/v1/fhir/Observation",
          _hba1c(code="29463-7", eff="2026-04-02T10:00:00Z"))
    resp = client.get(
        "/api/v1/fhir/Observation?"
        "code=https://termbank.pdhc.se/CodeSystem/loinc|4548-4",
        headers=ORG_HEADERS,
    )
    assert resp.status_code == 200
    bundle = resp.get_json()
    assert bundle["total"] == 1
    assert bundle["entry"][0]["resource"]["code"]["coding"][0]["code"] == "4548-4"


def test_search_by_date_range(client, fake_canon):
    for i, eff in enumerate([
        "2026-01-15T10:00:00Z",
        "2026-02-15T10:00:00Z",
        "2026-03-15T10:00:00Z",
    ]):
        _post(client, "/api/v1/fhir/Observation", _hba1c(value=6.0 + i, eff=eff))

    resp = client.get(
        "/api/v1/fhir/Observation?date=ge2026-02-01&date=lt2026-03-15",
        headers=ORG_HEADERS,
    )
    bundle = resp.get_json()
    assert bundle["total"] == 1


def test_chained_search_patient_identifier(client, fake_canon):
    # Use PUT-by-id so the patient's GUID matches PATIENT_GUID in the
    # Observation reference. POST mints a new id (FHIR "create"
    # semantics) which would orphan the chain.
    client.put(
        f"/api/v1/fhir/Patient/{PATIENT_GUID}",
        json=_patient_body(ident_value="12345"),
        headers=WRITE_HEADERS,
    )
    _post(client, "/api/v1/fhir/Observation", _hba1c())
    resp = client.get(
        "/api/v1/fhir/Observation?patient.identifier=12345",
        headers=ORG_HEADERS,
    )
    bundle = resp.get_json()
    assert bundle["total"] == 1


def test_include_observation_patient(client, fake_canon):
    _create_patient(client)
    _post(client, "/api/v1/fhir/Observation", _hba1c())
    resp = client.get(
        f"/api/v1/fhir/Observation?patient={PATIENT_GUID}"
        "&_include=Observation:patient",
        headers=ORG_HEADERS,
    )
    bundle = resp.get_json()
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert "Observation" in types
    assert "Patient" in types


def test_revinclude_patient_observation(client, fake_canon):
    _create_patient(client)
    _post(client, "/api/v1/fhir/Observation", _hba1c())
    resp = client.get(
        f"/api/v1/fhir/Patient?_id={PATIENT_GUID}"
        "&_revinclude=Observation:patient",
        headers=ORG_HEADERS,
    )
    bundle = resp.get_json()
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert "Patient" in types
    assert "Observation" in types


def test_has_reverse_chain(client, fake_canon):
    """Patients who have an Observation with a given code."""
    _create_patient(client)
    _create_patient(client, guid="other-pat-9999", ident_value="other")
    _post(client, "/api/v1/fhir/Observation", _hba1c())  # PATIENT_GUID has 4548-4
    resp = client.get(
        "/api/v1/fhir/Patient?_has:Observation:patient:code=4548-4",
        headers=ORG_HEADERS,
    )
    bundle = resp.get_json()
    assert bundle["total"] == 1
    assert bundle["entry"][0]["resource"]["id"] == PATIENT_GUID


# ---------------------------------------------------------------------------
# vread + history
# ---------------------------------------------------------------------------

def test_history_list_after_update(client, fake_canon):
    r1 = _post(client, "/api/v1/fhir/Observation", _hba1c(value=6.4))
    guid = r1.get_json()["id"]
    new_body = _hba1c(value=7.0)
    new_body["id"] = guid
    client.put(
        f"/api/v1/fhir/Observation/{guid}",
        json=new_body,
        headers={**WRITE_HEADERS, "If-Match": r1.headers["ETag"]},
    )
    resp = client.get(
        f"/api/v1/fhir/Observation/{guid}/_history", headers=ORG_HEADERS,
    )
    assert resp.status_code == 200
    bundle = resp.get_json()
    assert bundle["type"] == "history"
    assert bundle["total"] == 2  # current live + 1 history


def test_vread_specific_version(client, fake_canon):
    r1 = _post(client, "/api/v1/fhir/Observation", _hba1c(value=6.4))
    guid = r1.get_json()["id"]
    new_body = _hba1c(value=7.0)
    new_body["id"] = guid
    client.put(
        f"/api/v1/fhir/Observation/{guid}",
        json=new_body,
        headers={**WRITE_HEADERS, "If-Match": r1.headers["ETag"]},
    )
    # Read v1 (now in history)
    resp = client.get(
        f"/api/v1/fhir/Observation/{guid}/_history/1", headers=ORG_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["valueQuantity"]["value"] == 6.4
    # Read v2 (live)
    resp2 = client.get(
        f"/api/v1/fhir/Observation/{guid}/_history/2", headers=ORG_HEADERS,
    )
    assert resp2.get_json()["valueQuantity"]["value"] == 7.0


def test_vread_404_for_unknown_version(client, fake_canon):
    r1 = _post(client, "/api/v1/fhir/Observation", _hba1c())
    guid = r1.get_json()["id"]
    resp = client.get(
        f"/api/v1/fhir/Observation/{guid}/_history/99",
        headers=ORG_HEADERS,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# $everything
# ---------------------------------------------------------------------------

def test_everything_returns_all_for_patient(client, fake_canon):
    _create_patient(client)
    _post(client, "/api/v1/fhir/Observation", _hba1c())
    _post(client, "/api/v1/fhir/Condition", _condition_body())
    resp = client.get(
        f"/api/v1/fhir/Patient/{PATIENT_GUID}/$everything",
        headers=ORG_HEADERS,
    )
    bundle = resp.get_json()
    types = sorted({e["resource"]["resourceType"] for e in bundle["entry"]})
    assert types == ["Condition", "Observation", "Patient"]


def test_everything_respects_org_scope(client, fake_canon):
    _create_patient(client)
    _post(client, "/api/v1/fhir/Observation", _hba1c())
    resp = client.get(
        f"/api/v1/fhir/Patient/{PATIENT_GUID}/$everything",
        headers={"X-Org-Guids": "different-org"},
    )
    assert resp.status_code == 404


def test_everything_type_filter(client, fake_canon):
    _create_patient(client)
    _post(client, "/api/v1/fhir/Observation", _hba1c())
    _post(client, "/api/v1/fhir/Condition", _condition_body())
    resp = client.get(
        f"/api/v1/fhir/Patient/{PATIENT_GUID}/$everything?_type=Observation",
        headers=ORG_HEADERS,
    )
    types = {e["resource"]["resourceType"] for e in resp.get_json()["entry"]}
    assert types == {"Patient", "Observation"}


# ---------------------------------------------------------------------------
# Terminology shims
# ---------------------------------------------------------------------------

def test_codesystem_lookup_proxy(client, fake_canon):
    fake_resp = type("R", (), {
        "status_code": 200,
        "json": lambda self: {
            "resourceType": "Parameters",
            "parameter": [
                {"name": "name", "valueString": "loinc"},
                {"name": "display", "valueString": "Hemoglobin A1c"},
            ],
        },
    })()
    with patch("app.api.fhir_read.requests.get", return_value=fake_resp):
        resp = client.post(
            "/api/v1/fhir/CodeSystem/$lookup",
            json={"resourceType": "Parameters", "parameter": [
                {"name": "system", "valueString": "loinc"},
                {"name": "code", "valueString": "4548-4"},
            ]},
            headers=ORG_HEADERS,
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["resourceType"] == "Parameters"


def test_conceptmap_translate_proxy(client, fake_canon):
    fake_resp = type("R", (), {
        "status_code": 200,
        "json": lambda self: {
            "resourceType": "Parameters",
            "parameter": [
                {"name": "result", "valueBoolean": True},
                {"name": "match", "valueCoding":
                    {"system": "loinc", "code": "4548-4"}},
            ],
        },
    })()
    with patch("app.api.fhir_read.requests.post", return_value=fake_resp):
        resp = client.post(
            "/api/v1/fhir/ConceptMap/$translate",
            json={"resourceType": "Parameters", "parameter": [
                {"name": "system", "valueString": "http://loinc.org"},
                {"name": "code", "valueString": "4548-4"},
            ]},
            headers=ORG_HEADERS,
        )
    assert resp.status_code == 200


def test_validate_code_proxy(client, fake_canon):
    fake_resp = type("R", (), {
        "status_code": 200,
        "json": lambda self: {
            "resourceType": "Parameters",
            "parameter": [
                {"name": "result", "valueBoolean": True},
                {"name": "ref_via", "valueString": "Concept"},
            ],
        },
    })()
    with patch("app.api.fhir_read.requests.get", return_value=fake_resp):
        resp = client.post(
            "/api/v1/fhir/ValueSet/$validate-code",
            json={"resourceType": "Parameters", "parameter": [
                {"name": "system", "valueString": "loinc"},
                {"name": "code", "valueString": "4548-4"},
            ]},
            headers=ORG_HEADERS,
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /events  (change_feed long-poll)
# ---------------------------------------------------------------------------

def test_events_returns_change_feed_rows(client, fake_canon):
    _post(client, "/api/v1/fhir/Observation", _hba1c())
    _post(client, "/api/v1/fhir/Observation",
          _hba1c(value=7.0, eff="2026-04-15T10:00:00Z"))
    resp = client.get("/api/v1/fhir/events?since=0", headers=ORG_HEADERS)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 2
    assert all(e["resource_type"] == "Observation" for e in body["events"])


def test_events_since_advances(client, fake_canon):
    _post(client, "/api/v1/fhir/Observation", _hba1c())
    r1 = client.get("/api/v1/fhir/events?since=0", headers=ORG_HEADERS).get_json()
    next_since = r1["next_since"]

    _post(client, "/api/v1/fhir/Observation",
          _hba1c(value=7.0, eff="2026-04-15T10:00:00Z"))
    r2 = client.get(
        f"/api/v1/fhir/events?since={next_since}", headers=ORG_HEADERS,
    ).get_json()
    assert r2["count"] == 1
