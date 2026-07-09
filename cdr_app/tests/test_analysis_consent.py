"""#422 — analysis consent enforcement on cdr's FHIR read surface.

Purpose derivation, the per-reader boundary (operator blob enforces,
machine/service context passes through), search-result filtering, the
single-read 403 with the ips reason, and the fail-closed 503 path.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app import db
from app.models.resources import live_model
from app.services import analysis_consent as ac
from tests.test_fhir_read import (  # reuse seeding helpers
    WRITE_HEADERS,
    _FakeCanonicaliser,
    _hba1c,
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
        for table in ("observation_history", "observation",
                      "patient_history", "patient",
                      "change_feed", "sync_group", "cdr_audit_plan_miss"):
            db.session.execute(db.text(f"DELETE FROM {table}"))
        db.session.commit()
    yield

ORG = "org-aaaa"
PAT_OK = "pat-consent-ok-1"
PAT_NO = "pat-consent-no-1"

DOCTOR = {"X-Aff-Role": "doctor", "X-Aff-Org": ORG}
RESEARCHER = {"X-Aff-Role": "Researcher", "X-Aff-Org": ORG,
              "X-Aff-Projects": "proj-1"}


def _seed_two_patients(client):
    for pat, val in ((PAT_OK, 6.1), (PAT_NO, 7.3)):
        r = client.post("/api/v1/fhir/Observation",
                        json=_hba1c(value=val, patient=pat),
                        headers=WRITE_HEADERS)
        assert r.status_code == 201, r.get_json()


def _verdict_excluding(pat, reason="ehds_opt_out"):
    def _f(guids, purpose, projects):
        return {"allowed": [g for g in guids if g != pat],
                "excluded": [{"patient_guid": pat, "reason": reason}]}
    return _f


# ---------------------------------------------------------------------------
# purpose mapping
# ---------------------------------------------------------------------------

def test_purpose_researcher_uses_active_affiliation_projects():
    blob = {"active_affiliation_guid": "a2", "affiliations": [
        {"affiliation_guid": "a1", "role": "nurse"},
        {"affiliation_guid": "a2", "role": "Researcher",
         "research_project_guids": ["p1"]}]}
    assert ac.analysis_purpose(blob) == ("research", ["p1"])


def test_purpose_quality_registry():
    blob = {"affiliations": [{"affiliation_guid": "a", "role": "Registrator"}]}
    assert ac.analysis_purpose(blob) == ("quality_registry", [])


def test_purpose_default_statistics():
    blob = {"affiliations": [{"affiliation_guid": "a", "role": "doctor"}]}
    assert ac.analysis_purpose(blob) == ("statistics", [])


# ---------------------------------------------------------------------------
# search filtering (operator context)
# ---------------------------------------------------------------------------

def test_search_drops_excluded_patients(client, fake_canon):
    _seed_two_patients(client)
    with patch.object(ac, "_analysis_filter",
                      side_effect=_verdict_excluding(PAT_NO)):
        r = client.get("/api/v1/fhir/Observation", headers=DOCTOR)
    assert r.status_code == 200
    subjects = {e["resource"]["subject"]["reference"]
                for e in r.get_json().get("entry") or []}
    assert f"Patient/{PAT_OK}" in subjects
    assert f"Patient/{PAT_NO}" not in subjects


def test_researcher_purpose_passes_projects(client, fake_canon):
    _seed_two_patients(client)
    seen = {}

    def _spy(guids, purpose, projects):
        seen.update({"purpose": purpose, "projects": projects})
        return {"allowed": list(guids), "excluded": []}

    with patch.object(ac, "_analysis_filter", side_effect=_spy):
        r = client.get("/api/v1/fhir/Observation", headers=RESEARCHER)
    assert r.status_code == 200
    assert seen == {"purpose": "research", "projects": ["proj-1"]}


# ---------------------------------------------------------------------------
# single reads
# ---------------------------------------------------------------------------

def test_read_instance_403_with_reason(client, fake_canon):
    _seed_two_patients(client)
    Live = live_model("Observation")
    with client.application.app_context():
        guid = (db.session.query(Live)
                .filter(Live.patient_guid == PAT_NO).first().guid)
    with patch.object(ac, "_analysis_filter",
                      side_effect=_verdict_excluding(PAT_NO)):
        r = client.get(f"/api/v1/fhir/Observation/{guid}", headers=DOCTOR)
    assert r.status_code == 403
    assert b"ehds_opt_out" in r.data


def test_everything_gated(client, fake_canon):
    client.put(f"/api/v1/fhir/Patient/{PAT_NO}",
               json={"resourceType": "Patient", "id": PAT_NO},
               headers=WRITE_HEADERS)
    with patch.object(ac, "_analysis_filter",
                      side_effect=_verdict_excluding(PAT_NO)):
        r = client.get(f"/api/v1/fhir/Patient/{PAT_NO}/$everything",
                       headers=DOCTOR)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# per-reader boundary + fail-closed
# ---------------------------------------------------------------------------

def test_service_key_read_bypasses_consent(client, fake_canon):
    """Machine reads are the SIBLING reader's context (dashboard enforces
    on its side, #415) — no ips call is made here."""
    _seed_two_patients(client)
    with patch.object(ac, "_analysis_filter",
                      side_effect=AssertionError("must not be called")):
        r = client.get("/api/v1/fhir/Observation",
                       headers={"X-Source-Service": "sim.pdhc",
                                "X-Service-Key": "test-sim-key"})
    assert r.status_code == 200
    assert (r.get_json().get("total") or len(r.get_json().get("entry") or [])) >= 2


def test_admin_read_bypasses_consent(client, fake_canon):
    _seed_two_patients(client)
    with patch.object(ac, "_analysis_filter",
                      side_effect=AssertionError("must not be called")):
        r = client.get("/api/v1/fhir/Observation", headers={"X-Is-Admin": "1"})
    assert r.status_code == 200


def test_operator_read_fails_closed_when_ips_down(client, fake_canon):
    _seed_two_patients(client)

    def _boom(*a, **kw):
        raise ac.IpsUnreachable("down")

    with patch.object(ac, "_analysis_filter", side_effect=_boom):
        r = client.get("/api/v1/fhir/Observation", headers=DOCTOR)
    assert r.status_code == 503
