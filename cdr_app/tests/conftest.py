"""Shared fixtures for CDR tests."""
import pytest
from app import create_app, db as _db


@pytest.fixture(scope="session")
def app():
    """Create a test Flask app with SQLite."""
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "CAMBIO_DELIVERY_ENABLED": False,
        "GATEWAY_PDHC_SERVICE_KEY": "test-gateway-key",
        "TWOGATE_PDHC_SERVICE_KEY": "test-twogate-key",
    })
    with app.app_context():
        _db.create_all()
    yield app


@pytest.fixture(autouse=True)
def db_session(app):
    """Roll back after each test."""
    with app.app_context():
        yield _db
        _db.session.rollback()


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
