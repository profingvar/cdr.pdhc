"""Shared fixtures for CDR tests."""
import pytest
from sqlalchemy.pool import StaticPool
from app import create_app, db as _db


@pytest.fixture(scope="session")
def app():
    """Create a test Flask app with SQLite."""
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        # A bare sqlite:///:memory: hands every connection its own private
        # database, so writes issued on one request are invisible to the read
        # on the next (#436: the "assert 0 == N" cluster). StaticPool +
        # check_same_thread=False pin a single shared in-memory connection for
        # the whole session so writes persist across requests.
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        },
        # Pin AUTH_MODE so the suite is hermetic — otherwise create_app reads
        # it from the ambient env (AUTH_MODE=sso inside the prod container),
        # sending every read to the SSO 302 path (#436: the "302" cluster).
        "AUTH_MODE": "off",
        "CAMBIO_DELIVERY_ENABLED": False,
        # sim.pdhc is a KNOWN_FHIR_SERVICES entry, so writes authenticated with
        # this source + key pass _service_key_outcome (#436: the write 403s that
        # emptied every read). gateway/twogate kept for other suites.
        "SIM_PDHC_SERVICE_KEY": "test-sim-key",
        "GATEWAY_PDHC_SERVICE_KEY": "test-gateway-key",
        "TWOGATE_PDHC_SERVICE_KEY": "test-twogate-key",
    })
    # Test-only header->blob shim. The read tests express Rule-24 scope via
    # X-Is-Admin / X-Org-Guids headers, but the production auth loader derives
    # scope from the SSO blob and ignores those headers (#436). Register a
    # before_request (runs AFTER the loader) that injects the equivalent blob
    # for header-scoped reads, so the REAL fhir_read._org_filter is still what's
    # under test. Write requests (X-Source-Service + X-Service-Key, singular
    # X-Org-Guid) match neither branch and keep the loader's service blob.
    from flask import g, request
    from app.auth import _blob_to_user

    @app.before_request
    def _test_scope_from_headers():
        if request.headers.get("X-Is-Admin") == "1":
            blob = {"user_type": "professional", "is_su_admin": True,
                    "effective_phases": ["analysis"], "organization_ids": []}
        elif request.headers.get("X-Aff-Role"):
            # Reform-shaped operator blob for #422 consent tests: one active
            # affiliation whose role drives analysis_purpose().
            projects = [p for p in
                        request.headers.get("X-Aff-Projects", "").split(",") if p]
            blob = {"user_type": "professional", "is_su_admin": False,
                    "effective_phases": ["analysis"],
                    "affiliations": [{
                        "affiliation_guid": "aff-t1",
                        "role": request.headers["X-Aff-Role"],
                        "care_unit_guid": request.headers.get(
                            "X-Aff-Org", "org-aaaa"),
                        "research_project_guids": projects,
                    }]}
        elif request.headers.get("X-Org-Guids"):
            orgs = [o for o in request.headers["X-Org-Guids"].split(",") if o]
            blob = {"user_type": "professional", "is_su_admin": False,
                    "effective_phases": ["analysis"], "organization_ids": orgs}
        else:
            return None
        g.access_blob = blob
        g.current_user = _blob_to_user(blob)
        return None

    with app.app_context():
        _db.create_all()
    yield app


@pytest.fixture(autouse=True)
def db_session(app):
    """Roll back after each test."""
    with app.app_context():
        yield _db
        _db.session.rollback()


@pytest.fixture(autouse=True)
def _consent_allow_all(monkeypatch):
    """#422 default: ips analysis-filter allows everyone, so the rest of the
    suite stays hermetic (no IPS_BASE_URL → the real call would 503 every
    operator-blob read, fail-closed by design). test_analysis_consent.py
    overrides this with its own verdicts."""
    from app.services import analysis_consent as ac
    monkeypatch.setattr(
        ac, "_analysis_filter",
        lambda guids, purpose, projects: {"allowed": list(guids),
                                          "excluded": []})


@pytest.fixture
def client(app):
    return app.test_client()


SAMPLE_FHIR_OBSERVATION = {
    "resourceType": "Observation",
    "status": "final",
    "code": {
        "coding": [{"system": "http://loinc.org", "code": "29463-7", "display": "Body weight"}],
        "text": "Body weight",
    },
    "valueQuantity": {"value": 85.2, "unit": "kg"},
    "effectiveDateTime": "2026-04-10T10:00:00Z",
}

SAMPLE_INGEST_BODY = {
    "patient_guid": "pat-001",
    "source_type": "fhir",
    "fhir_resource": SAMPLE_FHIR_OBSERVATION,
    "canonical": {
        "table": "health_observations",
        "metric": "body_weight_kg",
        "value": 85.2,
        "unit": "kg",
        "source_type": "fhir",
        "source_code": "29463-7",
        "concept_guid": "concept-weight-001",
        "effective_at": "2026-04-10T10:00:00Z",
    },
}
