"""#769 — each datapoint carries its own context, ON the row.

The defect this prevents: `clinical_context` is keyed on `ingest_raw_guid` while
the live tables key on the FHIR resource id, so the two never joined —
`observation.guid = clinical_context.ingest_raw_guid` matched 0 rows while cdr1
held 7,415 transaction GUIDs belonging to no reachable datapoint.

A side table that cannot be joined is worse than a missing column, because it
looks populated. These tests assert on the ROW.
"""
from __future__ import annotations

import glob
import os
import re
import uuid

import pytest

from app.models import db
from app.models.resources import RESOURCES, live_model, history_model

CONTEXT_COLUMNS = ("ingest_raw_guid", "transaction_guid",
                   "care_plan_guid", "patient_org_guid")


@pytest.mark.parametrize("fhir_name", [r[0] for r in RESOURCES])
def test_every_live_table_carries_the_context_columns(fhir_name):
    """Common, not Observation-only. Per-type special-casing is what produces
    the congruency problems #777 catalogues."""
    model = live_model(fhir_name)
    for c in CONTEXT_COLUMNS:
        assert hasattr(model, c), f"{fhir_name} live table lacks {c}"


@pytest.mark.parametrize("fhir_name", [r[0] for r in RESOURCES])
def test_every_history_table_carries_them_too(fhir_name):
    """A versioned read must answer the same question as a current one."""
    model = history_model(fhir_name)
    for c in CONTEXT_COLUMNS:
        assert hasattr(model, c), f"{fhir_name} history table lacks {c}"


@pytest.fixture
def clean_rows(app):
    """Remove what these tests insert.

    The `app` fixture is session-scoped, so a row left behind is visible to
    every later test. `test_ingest.py::test_ingest_accepted` does
    `IngestRaw.query.first()` with no ordering and asserts on what comes back —
    an IngestRaw left here made it fail while passing in isolation. Same leak
    as #732 and #733; the third time is enough to make it a fixture."""
    yield
    from app.models import IngestRaw
    Obs = live_model("Observation")
    with app.app_context():
        db.session.query(Obs).filter(
            Obs.patient_guid.in_(["pat-769", "pat-sep", "pat-null"])).delete(
                synchronize_session=False)
        db.session.query(IngestRaw).filter(
            IngestRaw.patient_guid == "pat-769").delete(
                synchronize_session=False)
        db.session.commit()


def test_a_datapoint_reaches_its_context_in_ONE_join(app, clean_rows):
    """THE regression. Before #769 this join returned 0 rows for every
    datapoint on cdr1."""
    Obs = live_model("Observation")
    from app.models import IngestRaw
    raw_guid = str(uuid.uuid4())
    with app.app_context():
        db.session.add(IngestRaw(
            guid=raw_guid, source_service="test", patient_guid="pat-769",
            payload_json={"resourceType": "Observation"},
            payload_hash="h769"))
        db.session.add(Obs(
            guid=str(uuid.uuid4()), patient_guid="pat-769",
            org_guid="00000000-0000-0000-0000-000000000000",
            code_canonical="urn:pdhc:concept/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            raw_json={"resourceType": "Observation"},
            ingest_raw_guid=raw_guid, transaction_guid="tx-769",
            care_plan_guid="cp-769", patient_org_guid="org-769"))
        db.session.commit()

        row = (db.session.query(Obs, IngestRaw)
               .join(IngestRaw, IngestRaw.guid == Obs.ingest_raw_guid)
               .filter(Obs.patient_guid == "pat-769").one())
        obs, raw = row
        assert raw.guid == raw_guid              # one join, reached
        assert obs.transaction_guid == "tx-769"
        assert obs.care_plan_guid == "cp-769"
        assert obs.patient_org_guid == "org-769"


def test_patient_org_guid_is_NOT_the_performer(app, clean_rows):
    """org_guid is the performer (from Observation.performer[0]);
    patient_org_guid is the patient's own organisation. Two columns because
    they are two facts — conflating them is #768."""
    Obs = live_model("Observation")
    with app.app_context():
        o = Obs(guid=str(uuid.uuid4()), patient_guid="pat-sep",
                org_guid="performer-org", patient_org_guid="patient-org",
                raw_json={"resourceType": "Observation"})
        db.session.add(o)
        db.session.commit()
        got = db.session.query(Obs).filter_by(patient_guid="pat-sep").one()
        assert got.org_guid == "performer-org"
        assert got.patient_org_guid == "patient-org"
        assert got.org_guid != got.patient_org_guid


def test_the_context_columns_are_nullable_for_now(app, clean_rows):
    """patient_org_guid's resolution is #774, blocked on #778, so NOT NULL
    would reject every ingest today. This test is the reminder to tighten it:
    when #774 lands, this expectation should change deliberately."""
    Obs = live_model("Observation")
    with app.app_context():
        o = Obs(guid=str(uuid.uuid4()), patient_guid="pat-null",
                org_guid="o", raw_json={"resourceType": "Observation"})
        db.session.add(o)
        db.session.commit()          # must not raise
        got = db.session.query(Obs).filter_by(patient_guid="pat-null").one()
        assert got.ingest_raw_guid is None and got.patient_org_guid is None


def test_the_migration_graph_has_exactly_one_head():
    """A fork is invisible until a deploy applies only one branch. An earlier
    draft of #769's migration chained onto f4a5b6c7d8e9 — which looked like the
    head because a `grep | tail` listed files alphabetically instead of
    following the graph — and silently forked the chain."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    nodes = {}
    for f in glob.glob(os.path.join(here, "migrations", "versions", "*.py")):
        s = open(f).read()
        r = re.search(r'^revision = ["\'](\w+)["\']', s, re.M)
        d = re.search(r'^down_revision = ["\']?(\w+|None)["\']?', s, re.M)
        if r:
            nodes[r.group(1)] = d.group(1) if d else None
    downs = set(nodes.values())
    heads = [r for r in nodes if r not in downs]
    assert len(heads) == 1, f"migration graph forked: {heads}"
