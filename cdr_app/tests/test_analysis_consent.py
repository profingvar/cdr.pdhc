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
    PATIENT_ORG,
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

# #818 — a reader's affiliation names their CARE UNIT, which is the space
# patient_org_guid lives in (sso's affiliations[].care_unit_guid), not the
# performer space `ORG`. Scoped reads therefore have to ask for PATIENT_ORG.
DOCTOR = {"X-Aff-Role": "doctor", "X-Aff-Org": PATIENT_ORG}
RESEARCHER = {"X-Aff-Role": "Researcher", "X-Aff-Org": PATIENT_ORG,
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
               json={"resourceType": "Patient", "id": PAT_NO,
                         # #818: a row with no patient_org_guid is invisible
                         # to any scoped reader, so the gate under test is
                         # never reached — it 404s first.
                         "patient_org_guid": PATIENT_ORG},
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


# ---------------------------------------------------------------------------
# #730 — the Authorization header _analysis_filter actually sends
#
# Every test above patches `_analysis_filter` itself, so the header-building
# code in it was never exercised by any of them. That is why this defect
# survived: for a service-shaped read the request went out with NO
# Authorization header, ips answered 401, and the gate refused the whole read
# instead of filtering it. These tests patch `requests.post` one level lower
# and assert the header on the wire.
# ---------------------------------------------------------------------------

class _Resp:
    status_code = 200

    def __init__(self, allowed=(), excluded=()):
        self._b = {"allowed": list(allowed), "excluded": list(excluded)}

    def json(self):
        return self._b


# conftest's `_consent_allow_all` is AUTOUSE and replaces `_analysis_filter`
# with a lambda for the whole suite, so no test in this file can reach the real
# header-building code through the module attribute — which is precisely why
# #730 survived. Capture the real function at import time, before any fixture
# runs, and call that.
_REAL_ANALYSIS_FILTER = ac._analysis_filter


def test_the_autouse_stub_is_what_hid_this(app):
    """Guard the guard: if conftest ever stops stubbing `_analysis_filter`, the
    capture above becomes pointless indirection and should be removed. If it
    starts stubbing something else, these tests go quiet again."""
    assert ac._analysis_filter is not _REAL_ANALYSIS_FILTER, (
        "conftest no longer stubs _analysis_filter — simplify these tests")


def _capture_post(app, *, api_key=None, sso_token=None, allowed=("p1",)):
    """Call the REAL _analysis_filter and return the headers it sent."""
    seen = {}

    def _post(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers") or {}
        return _Resp(allowed=allowed)

    prev_key = app.config.get("IPS_API_KEY")
    prev_base = app.config.get("IPS_BASE_URL")
    app.config["IPS_API_KEY"] = api_key
    app.config["IPS_BASE_URL"] = "https://ips.example"
    try:
        with app.test_request_context("/api/v1/fhir/Observation"):
            if sso_token is not None:
                from flask import session
                session["sso_token"] = sso_token
            with patch("app.services.analysis_consent.requests.post",
                       side_effect=_post):
                _REAL_ANALYSIS_FILTER(["p1"], "care", [])
    finally:
        app.config["IPS_API_KEY"] = prev_key
        app.config["IPS_BASE_URL"] = prev_base
    return seen


def test_a_service_shaped_read_sends_its_own_ApiKey(app):
    """No operator session + a configured key => Authorization: ApiKey <key>.

    This is the whole fix. gateway, analyse and sim all reach the consent gate
    without a session, and before #730 they sent nothing.
    """
    seen = _capture_post(app, api_key="k-cdr1-secret")
    assert seen["headers"].get("Authorization") == "ApiKey k-cdr1-secret"
    assert seen["url"].endswith("/api/v1/patients/analysis-filter")


def test_an_operator_session_still_sends_its_bearer(app):
    """The operator path is unchanged — the ApiKey is a fallback, not a
    replacement. A service key standing in for a human would lose the
    per-reader purpose the #422 model derives from their affiliation."""
    seen = _capture_post(app, api_key="k-cdr1-secret", sso_token="tok-abc")
    assert seen["headers"].get("Authorization") == "Bearer tok-abc"


def test_the_request_is_NEVER_sent_unauthenticated(app):
    """The regression guard, stated as the invariant rather than as a case.

    ips's require_auth reads ONLY the Authorization header and silently ignores
    X-API-Key, so an absent header is indistinguishable from a bad credential —
    it is a 401 either way, and that 401 was being reported as an ips outage.
    """
    for kwargs in ({"api_key": "k"}, {"api_key": "k", "sso_token": "t"}):
        seen = _capture_post(app, **kwargs)
        auth = seen["headers"].get("Authorization", "")
        assert auth, f"no Authorization header for {kwargs}"
        assert auth.startswith(("ApiKey ", "Bearer ")), auth


def test_a_missing_key_fails_closed_and_blames_the_credential_not_ips(app):
    """Still fails closed with no key — that is correct. What changes is that
    the message names the missing piece, instead of a bare 401 surfacing as
    IpsUnreachable and reading like the sibling being down."""
    def _post(url, **kw):           # must not be reached
        raise AssertionError("ips was called without a credential")

    with app.test_request_context("/api/v1/fhir/Observation"):
        prev_key = app.config.get("IPS_API_KEY")
        prev_base = app.config.get("IPS_BASE_URL")
        app.config["IPS_API_KEY"] = ""
        app.config["IPS_BASE_URL"] = "https://ips.example"
        try:
            with patch("app.services.analysis_consent.requests.post",
                       side_effect=_post):
                with pytest.raises(ac.IpsUnreachable) as ei:
                    _REAL_ANALYSIS_FILTER(["p1"], "care", [])
        finally:
            app.config["IPS_API_KEY"] = prev_key
            app.config["IPS_BASE_URL"] = prev_base

    msg = str(ei.value)
    assert "IPS_API_KEY" in msg
    assert "NOT an ips outage" in msg


def test_the_session_header_does_not_clobber_the_credential(app):
    """outbound_session_headers() is merged AFTER the Authorization header, so
    a future field named Authorization there would silently un-authenticate
    every one of these calls."""
    with patch("app.services.analysis_consent.outbound_session_headers"
               if hasattr(ac, "outbound_session_headers") else
               "app.services.session_headers.outbound_session_headers",
               return_value={"X-Operator-Session-Id": "sid-1"}):
        seen = _capture_post(app, api_key="k-cdr1-secret")
    assert seen["headers"].get("Authorization") == "ApiKey k-cdr1-secret"
    assert seen["headers"].get("X-Operator-Session-Id") == "sid-1"
