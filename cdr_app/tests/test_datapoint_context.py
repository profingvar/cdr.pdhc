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


# ---- #773: ingest actually WRITES the context onto the row ----------------

def test_ingest_stores_the_join_key_and_context_on_the_row(client, app, clean_rows):
    """#769 added the columns; this is what makes them real. Before #773,
    `ingest_raw_guid` was already a PARAMETER of build_live_observation_row —
    used only as a fallback for the row's guid and never stored — which is why
    clinical_context, keyed on it, could not be joined to this table."""
    from tests.conftest import SAMPLE_INGEST_BODY, SAMPLE_FHIR_OBSERVATION
    body = dict(SAMPLE_INGEST_BODY)
    # The live-row path requires fhir_resource["id"] — gateway always sets one
    # (build_live_observation_row falls back to ingest_raw_guid only when it is
    # absent), but the shared sample has none, so an ingest of it creates no
    # datapoint row at all.
    body["fhir_resource"] = dict(SAMPLE_FHIR_OBSERVATION, id=str(uuid.uuid4()))
    body["clinical_context"] = {
        "service_request_guid": "sr-773",
        "transaction_guid": "tx-773",
        "care_plan_guid": "cp-773",
        "provider_org_guid": "performer-773",
        "requesting_org_guid": "requester-773",
    }
    r = client.post("/api/v1/ingest", json=body, headers={
        "X-Service-Key": "test-gateway-key", "X-Source-Service": "gateway.pdhc"})
    assert r.status_code == 202, r.get_data(as_text=True)[:200]
    raw_guid = r.get_json()["ingest_raw_guid"]

    Obs = live_model("Observation")
    from app.models import IngestRaw
    with app.app_context():
        obs = db.session.query(Obs).filter_by(ingest_raw_guid=raw_guid).one()
        assert obs.transaction_guid == "tx-773"
        assert obs.care_plan_guid == "cp-773"
        # the one join that used to return 0 rows, now with real values
        joined = (db.session.query(Obs, IngestRaw)
                  .join(IngestRaw, IngestRaw.guid == Obs.ingest_raw_guid)
                  .filter(Obs.ingest_raw_guid == raw_guid).one())
        assert joined[1].guid == raw_guid

        # the row and the context table must agree, not diverge
        from app.models import ClinicalContext
        cc = db.session.query(ClinicalContext).filter_by(
            ingest_raw_guid=raw_guid).one()
        assert cc.transaction_guid == obs.transaction_guid
        assert cc.care_plan_guid == obs.care_plan_guid

        db.session.query(Obs).filter_by(ingest_raw_guid=raw_guid).delete()
        db.session.query(ClinicalContext).filter_by(
            ingest_raw_guid=raw_guid).delete()
        db.session.query(IngestRaw).filter_by(guid=raw_guid).delete()
        db.session.commit()


def test_ingest_STORES_patient_org_guid_from_the_service_resolved_context(
        client, app, clean_rows):
    """#809, and #775's conformance slot. Through the real HTTP route.

    This REPLACES `test_ingest_does_NOT_write_patient_org_guid_from_the_producer`,
    which asserted the opposite and was deliberate when written. Its premise
    does not survive contact with how the value actually travels, and the
    reasoning is recorded here rather than deleted with it.

    That test refused a value it called producer-asserted. The refusal was
    aimed at a real conflation (#768: the performer and the requester are not
    the patient's organisation, and both have their own columns). But cdr
    cannot distinguish the case it refused from the legitimate one, because
    the two are byte-identical on the wire: both arrive as
    `X-Source-Service: gateway.pdhc` carrying `clinical_context.patient_org_guid`.
    gateway FORWARDS the SR context (`dict(self._data)`) rather than cdr
    FETCHING it, so there is no provenance marker to tell them apart.

    Requiring `service_request_guid` alongside it was considered as a
    discriminator and rejected: sim's cohort rows carry `patient_org_guid` and
    no SR at all, so that rule would silently drop exactly the data the column
    exists to scope.

    What settles it: on this path EVERY sibling context field is already taken
    from the body by the same callers — `provider_org_guid`,
    `requesting_org_guid`, `contract_guid`, `plan_definition_guid`. A caller
    able to lie about the patient's organisation can already lie about the
    performer's and the requester's, and those are written. Refusing only this
    one bought nothing while leaving the Rule 24 column NULL on every row.

    The trust boundary is therefore the service key, which all three ingest
    routes enforce — not a per-field refusal.
    """
    from tests.conftest import SAMPLE_INGEST_BODY, SAMPLE_FHIR_OBSERVATION
    body = dict(SAMPLE_INGEST_BODY)
    # The live-row path requires fhir_resource["id"] — gateway always sets one.
    body["fhir_resource"] = dict(SAMPLE_FHIR_OBSERVATION, id=str(uuid.uuid4()))
    body["clinical_context"] = {
        "service_request_guid": "sr-809",
        "provider_org_guid": "performer-809",
        "requesting_org_guid": "requester-809",
        "patient_org_guid": "the-patients-ips-org-809",
    }
    r = client.post("/api/v1/ingest", json=body, headers={
        "X-Service-Key": "test-gateway-key", "X-Source-Service": "gateway.pdhc"})
    assert r.status_code == 202, r.get_data(as_text=True)[:200]
    raw_guid = r.get_json()["ingest_raw_guid"]

    Obs = live_model("Observation")
    with app.app_context():
        obs = db.session.query(Obs).filter_by(ingest_raw_guid=raw_guid).one()
        assert obs.patient_org_guid == "the-patients-ips-org-809", (
            "the Rule 24 scoping column is still NULL on an ingest whose SR "
            "context carried the patient's organisation")
        # ...and it is still the THIRD organisation, not a copy of either of
        # the other two. This is the part of #768 that remains true.
        assert obs.org_guid != obs.patient_org_guid
        assert obs.patient_org_guid != "requester-809"
        db.session.query(Obs).filter_by(ingest_raw_guid=raw_guid).delete()
        db.session.commit()


def test_an_absent_patient_org_guid_stays_NULL_and_is_never_inferred(
        client, app, clean_rows):
    """The half of the old test that IS still true, kept as its own case.

    The column fills FORWARD ONLY: ips's `patient_clinic_assignments` has
    `assigned_at` and no end timestamp, so asking ips later returns TODAY's
    answer for an old request. So when the context does not carry it, nothing
    may stand in — not `org_guid` (the performer), not `requesting_org_guid`,
    and not a lookup. NULL means "nobody recorded it", which is the honest
    answer and the one a Rule 24 filter must be able to rely on.
    """
    from tests.conftest import SAMPLE_INGEST_BODY, SAMPLE_FHIR_OBSERVATION
    body = dict(SAMPLE_INGEST_BODY)
    body["fhir_resource"] = dict(SAMPLE_FHIR_OBSERVATION, id=str(uuid.uuid4()))
    body["clinical_context"] = {
        "provider_org_guid": "performer-x",
        "requesting_org_guid": "requester-x",
        # patient_org_guid deliberately absent; org_guid must not fill it.
        "org_guid": "nor-this",
    }
    r = client.post("/api/v1/ingest", json=body, headers={
        "X-Service-Key": "test-gateway-key", "X-Source-Service": "gateway.pdhc"})
    assert r.status_code == 202
    raw_guid = r.get_json()["ingest_raw_guid"]
    Obs = live_model("Observation")
    with app.app_context():
        obs = db.session.query(Obs).filter_by(ingest_raw_guid=raw_guid).one()
        assert obs.patient_org_guid is None, (
            "an organisation was invented for a row whose context named none")
        db.session.query(Obs).filter_by(ingest_raw_guid=raw_guid).delete()
        db.session.commit()


def test_the_BACKFILL_cli_does_not_invent_an_organisation():
    """`flask backfill-live-observations` passes no context, so it must keep
    writing NULL. A backfill that resolved the organisation from ips today
    would stamp today's affiliation onto an old reading — the precise error
    the forward-only rule exists to prevent, applied retroactively to every
    historical row at once.
    """
    import inspect
    from app import auth as cdr_auth

    src = inspect.getsource(cdr_auth)
    i = src.index("def backfill_live_observations")
    body = src[i:i + 3000]
    assert "build_live_observation_row(" in body
    assert "context=" not in body, (
        "the backfill now passes a context, so it can write an organisation "
        "nobody recorded at the time")


def test_the_deprecated_aliases_still_land_on_the_row(client, app, clean_rows):
    """ClinicalContext accepts careplan_guid/activity_guid during the #294
    window (#302). The row must accept the same, or a producer mid-migration
    populates the context table and leaves the row empty — which is how the
    two could disagree."""
    from tests.conftest import SAMPLE_INGEST_BODY, SAMPLE_FHIR_OBSERVATION
    body = dict(SAMPLE_INGEST_BODY)
    # The live-row path requires fhir_resource["id"] — gateway always sets one
    # (build_live_observation_row falls back to ingest_raw_guid only when it is
    # absent), but the shared sample has none, so an ingest of it creates no
    # datapoint row at all.
    body["fhir_resource"] = dict(SAMPLE_FHIR_OBSERVATION, id=str(uuid.uuid4()))
    body["clinical_context"] = {"activity_guid": "tx-alias",
                                "careplan_guid": "cp-alias"}
    r = client.post("/api/v1/ingest", json=body, headers={
        "X-Service-Key": "test-gateway-key", "X-Source-Service": "gateway.pdhc"})
    assert r.status_code == 202
    raw_guid = r.get_json()["ingest_raw_guid"]
    Obs = live_model("Observation")
    with app.app_context():
        obs = db.session.query(Obs).filter_by(ingest_raw_guid=raw_guid).one()
        assert obs.transaction_guid == "tx-alias"
        assert obs.care_plan_guid == "cp-alias"
        db.session.query(Obs).filter_by(ingest_raw_guid=raw_guid).delete()
        db.session.commit()
