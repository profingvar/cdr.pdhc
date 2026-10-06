"""#760 — a service key added for READS must not carry a write grant.

Membership in `KNOWN_FHIR_SERVICES` was the whole check, and the blob the loader
mints for a service caller is SU-equivalent and organisation-blind. So every
identity ever added so it could `GET /api/v1/fhir/Observation` was also allowed
to `POST /api/v1/fhir/<type>` and `PUT` over any resource in the repository.

These go through the loader via a real request, because the defect was never in
a predicate — it was in what the loader let past. A test that asserted on
`READ_ONLY_SERVICE_SOURCES` would pass against the broken code.
"""
import pytest

from app.auth import KNOWN_FHIR_SERVICES, READ_ONLY_SERVICE_SOURCES

KEY = "test-analyse-key-760"
SIM_KEY = "test-sim-key-760"


@pytest.fixture
def svc_app(app, monkeypatch):
    """The shared app with service keys installed for the duration of ONE test.

    `monkeypatch.setitem` and not assignment: the `app` fixture is
    session-scoped, and leaving a key behind arms a service identity for every
    later test in the run. Writing `app.config[...] = ""` directly is what
    403'd five innocent provenance tests under #732.
    """
    for k, v in (("ANALYSE_PDHC_SERVICE_KEY", KEY),
                 ("SIM_PDHC_SERVICE_KEY", SIM_KEY),
                 ("AUTH_MODE", "sso"),
                 ("CDR_READ_LOCKDOWN", False)):
        monkeypatch.setitem(app.config, k, v)
    return app


def _hdr(source, key):
    return {"X-Source-Service": source, "X-Service-Key": key}


def test_analyse_is_registered_read_only():
    """Guard against someone adding a reader to the dict and forgetting this
    list — the dict is what grants, this list is what narrows."""
    assert "analyse.pdhc" in KNOWN_FHIR_SERVICES
    assert "analyse.pdhc" in READ_ONLY_SERVICE_SOURCES


def test_dashboard_is_registered_read_only():
    """#762: all six of dashboard's fanout call sites pass method="GET", and
    cdr1_client has a single transport built on requests.get. Its two POSTs go
    to sso and ips, neither of which is a CDR."""
    assert "dashboard.pdhc" in KNOWN_FHIR_SERVICES
    assert "dashboard.pdhc" in READ_ONLY_SERVICE_SOURCES


def test_sim_and_gateway_are_NOT_read_only():
    """They write by design; narrowing them would break ingest."""
    assert "sim.pdhc" not in READ_ONLY_SERVICE_SOURCES
    assert "gateway.pdhc" not in READ_ONLY_SERVICE_SOURCES


def test_a_read_only_key_cannot_POST_fhir(svc_app):
    """The actual write surface: POST /api/v1/fhir/<type> creates a resource."""
    r = svc_app.test_client().post(
        "/api/v1/fhir/Observation", json={"resourceType": "Observation"},
        headers=_hdr("analyse.pdhc", KEY))
    assert r.status_code == 403
    assert "read-only" in r.get_json()["error"]


def test_a_read_only_key_cannot_PUT_over_a_resource(svc_app):
    r = svc_app.test_client().put(
        "/api/v1/fhir/Observation/abc", json={"resourceType": "Observation"},
        headers=_hdr("analyse.pdhc", KEY))
    assert r.status_code == 403
    assert "read-only" in r.get_json()["error"]


def test_a_read_only_key_cannot_POST_a_bundle(svc_app):
    r = svc_app.test_client().post(
        "/api/v1/fhir/Bundle", json={"resourceType": "Bundle"},
        headers=_hdr("analyse.pdhc", KEY))
    assert r.status_code == 403


def test_the_refusal_does_not_blame_the_credentials(svc_app):
    """The key IS valid; the verb is not. Sharing the generic 'Invalid service
    credentials' sends whoever hits this to rotate a key that was never the
    problem."""
    r = svc_app.test_client().post(
        "/api/v1/fhir/Observation", json={}, headers=_hdr("analyse.pdhc", KEY))
    assert "Invalid service credentials" not in r.get_data(as_text=True)


def test_a_read_only_key_still_READS(svc_app):
    """The whole point: narrowing must cost analyse nothing. Anything but 403
    means the loader let it through to the view."""
    r = svc_app.test_client().get(
        "/api/v1/fhir/Observation", headers=_hdr("analyse.pdhc", KEY))
    assert r.status_code != 403, r.get_data(as_text=True)[:200]


def test_a_WRITING_service_is_unaffected(svc_app):
    """sim.pdhc writes synthetic cohorts by design and must still be able to."""
    r = svc_app.test_client().post(
        "/api/v1/fhir/Observation", json={"resourceType": "Observation"},
        headers=_hdr("sim.pdhc", SIM_KEY))
    assert r.status_code != 403, r.get_data(as_text=True)[:200]


def test_a_bad_key_still_reports_bad_credentials(svc_app):
    """The new branch must not swallow the old one."""
    r = svc_app.test_client().get(
        "/api/v1/fhir/Observation", headers=_hdr("analyse.pdhc", "wrong"))
    assert r.status_code == 403
    assert "Invalid service credentials" in r.get_data(as_text=True)


def test_dashboards_key_cannot_write(svc_app):
    """It held a live FHIR-write grant on all three CDRs until #762."""
    import pytest
    from app.auth import KNOWN_FHIR_SERVICES
    svc_app.config["DASHBOARD_PDHC_SERVICE_KEY"] = "test-dash-key-762"
    c = svc_app.test_client()
    h = {"X-Source-Service": "dashboard.pdhc", "X-Service-Key": "test-dash-key-762"}
    for call in (lambda: c.post("/api/v1/fhir/Observation", json={}, headers=h),
                 lambda: c.put("/api/v1/fhir/Observation/x", json={}, headers=h),
                 lambda: c.post("/api/v1/fhir/Bundle", json={}, headers=h)):
        r = call()
        assert r.status_code == 403
        assert "read-only" in r.get_data(as_text=True)


def test_dashboard_still_READS(svc_app):
    """Narrowing must cost it nothing — it is an active reader."""
    svc_app.config["DASHBOARD_PDHC_SERVICE_KEY"] = "test-dash-key-762"
    r = svc_app.test_client().get("/api/v1/fhir/Observation", headers={
        "X-Source-Service": "dashboard.pdhc", "X-Service-Key": "test-dash-key-762"})
    assert r.status_code != 403, r.get_data(as_text=True)[:160]
