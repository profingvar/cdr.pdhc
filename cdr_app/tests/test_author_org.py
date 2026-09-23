"""#665 — clinical_context.author_org_guid: who CREATED an observation.

The platform had three org roles and a field for only two of them: who
ORDERED it (requesting_org_guid), who SUBMITTED it (provider_org_guid), and
nothing for who AUTHORED it. Worse, gateway stamps the authenticated
submitter into FHIR `performer`, which in FHIR means the author — so the
submitter was recorded AS the author.

These tests pin the two decisions in analyse.pdhc ADR-0006: no defaulting to
provider_org_guid, and no falling back to `performer`.
"""
from __future__ import annotations

import uuid

import pytest

from app import db
from app.models import ClinicalContext, IngestRaw


@pytest.fixture(autouse=True)
def _isolate(app):
    """Leave the ingest tables as we found them.

    tests/test_ingest.py asserts on ``IngestRaw.query.first()``, which is only
    meaningful when that table holds one row. These tests create ingest rows,
    so they clean up both before and after — this file neither inherits
    another test's rows nor leaks its own. Children before parents:
    clinical_context references ingest_raw.
    """
    def _wipe():
        with app.app_context():
            for table in ("clinical_context", "fhir_resources",
                          "openehr_compositions", "health_observations",
                          "activities", "dedupe_registry", "ingest_raw"):
                db.session.execute(db.text(f"DELETE FROM {table}"))
            db.session.commit()
    _wipe()
    yield
    _wipe()


def _mk(app, **ctx):
    with app.app_context():
        pat = str(uuid.uuid4())
        raw = IngestRaw(guid=str(uuid.uuid4()), payload_json={},
                        payload_hash=uuid.uuid4().hex,
                        patient_guid=pat, source_service="test")
        db.session.add(raw)
        db.session.flush()
        row = ClinicalContext(ingest_raw_guid=raw.guid,
                              patient_guid=pat, **ctx)
        db.session.add(row)
        db.session.commit()
        return row.guid


class TestColumn:

    def test_author_org_is_stored_and_read_back(self, app):
        org = str(uuid.uuid4())
        guid = _mk(app, author_org_guid=org)
        with app.app_context():
            assert db.session.get(ClinicalContext, guid).author_org_guid == org

    def test_it_is_nullable_and_defaults_to_none(self, app):
        """NULL means UNKNOWN. It must never quietly become the provider."""
        provider = str(uuid.uuid4())
        guid = _mk(app, provider_org_guid=provider)
        with app.app_context():
            row = db.session.get(ClinicalContext, guid)
            assert row.author_org_guid is None
            assert row.provider_org_guid == provider

    def test_author_is_independent_of_the_other_two_orgs(self, app):
        """The case the field exists for: a third party authored the data."""
        requester, provider, author = (str(uuid.uuid4()) for _ in range(3))
        guid = _mk(app, requesting_org_guid=requester,
                   provider_org_guid=provider, author_org_guid=author)
        with app.app_context():
            row = db.session.get(ClinicalContext, guid)
            assert len({row.requesting_org_guid, row.provider_org_guid,
                        row.author_org_guid}) == 3


class TestIngest:

    def _post(self, client, ctx):
        from tests.test_fhir_read import WRITE_HEADERS
        return client.post("/api/v1/ingest", json={
            "patient_guid": str(uuid.uuid4()),
            "source_type": "fhir",
            "fhir_resource": {"resourceType": "Observation", "status": "final",
                              "code": {"text": "t"}},
            "clinical_context": ctx,
        }, headers=WRITE_HEADERS)

    def test_declared_author_is_persisted(self, app, client):
        author = str(uuid.uuid4())
        r = self._post(client, {"provider_org_guid": str(uuid.uuid4()),
                                "author_org_guid": author})
        assert r.status_code in (200, 201, 202), r.get_json()
        with app.app_context():
            assert ClinicalContext.query.filter_by(
                author_org_guid=author).count() == 1

    def test_absent_author_stays_null_and_is_not_defaulted(self, app, client):
        """ADR-0006: a guess written into the row becomes indistinguishable
        from a value the submitter actually declared."""
        provider = str(uuid.uuid4())
        r = self._post(client, {"provider_org_guid": provider})
        assert r.status_code in (200, 201, 202), r.get_json()
        with app.app_context():
            row = ClinicalContext.query.filter_by(
                provider_org_guid=provider).first()
            assert row is not None
            assert row.author_org_guid is None
