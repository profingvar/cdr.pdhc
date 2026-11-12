"""Care-delivery read surface for the clinical dashboard (#468 / #462 D6).

Uses a module-local ``app`` fixture (NOT the conftest one) so the
dashboard service identity survives — the conftest test-shim rewrites
``g.access_blob`` into an operator blob whenever ``X-Org-Guids`` is
present, which would strip ``service_source`` and defeat the point.
"""
from datetime import datetime, timezone, date

import pytest
from sqlalchemy.pool import StaticPool

from app import create_app, db as _db
from app.models.resources import live_model

DASH_KEY = "test-dash-key"
BASE_H = {
    "X-Source-Service": "dashboard.pdhc",
    "X-Service-Key": DASH_KEY,
    "X-Access-Purpose": "care-delivery",
}


@pytest.fixture
def app():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        },
        "AUTH_MODE": "off",
        "CAMBIO_DELIVERY_ENABLED": False,
        "DASHBOARD_PDHC_SERVICE_KEY": DASH_KEY,
        "SIM_PDHC_SERVICE_KEY": "test-sim-key",
    })
    with app.app_context():
        _db.create_all()
        _seed()
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _dt(day):
    return datetime(2026, 4, day, 10, 0, tzinfo=timezone.utc)


def _seed():
    Obs = live_model("Observation")
    Pat = live_model("Patient")
    n = 0

    def obs(patient, org, code, day, unit="mmol/L"):
        nonlocal n
        n += 1
        return Obs(guid=f"o{n}", patient_guid=patient, org_guid=org,
                   code_canonical=code, value_unit=unit, effective_at=_dt(day),
                   status="final", raw_json={})

    _db.session.add_all([
        Pat(guid="pat-a1", org_guid="org-a", raw_json={}, active=True,
            names=[{"family": "Ek", "given": ["Anna"]}], birth_date=date(1980, 1, 2)),
        Pat(guid="pat-a2", org_guid="org-a", raw_json={}, active=True,
            names=[{"text": "Bo Berg"}]),
        Pat(guid="pat-b1", org_guid="org-b", raw_json={}, active=True,
            names=[{"family": "Carlsson"}]),
    ])
    _db.session.add_all([
        # pat-a1: weight x3 (newest day 12), glucose x1
        obs("pat-a1", "org-a", "sys|weight", 5),
        obs("pat-a1", "org-a", "sys|weight", 8),
        obs("pat-a1", "org-a", "sys|weight", 12),
        obs("pat-a1", "org-a", "sys|glucose", 6),
        # pat-a2: glucose x2 (newest day 3)
        obs("pat-a2", "org-a", "sys|glucose", 2),
        obs("pat-a2", "org-a", "sys|glucose", 3),
        # pat-b1: in a DIFFERENT org
        obs("pat-b1", "org-b", "sys|weight", 9),
    ])
    _db.session.commit()


# --- guards ---------------------------------------------------------------

def test_missing_purpose_header_400(client):
    h = {k: v for k, v in BASE_H.items() if k != "X-Access-Purpose"}
    h["X-Org-Guids"] = "org-a"
    assert client.get("/api/v1/clinical/patients", headers=h).status_code == 400


def test_non_dashboard_service_403(client):
    h = dict(BASE_H)
    h["X-Source-Service"] = "sim.pdhc"
    h["X-Service-Key"] = "test-sim-key"
    h["X-Org-Guids"] = "org-a"
    assert client.get("/api/v1/clinical/patients", headers=h).status_code == 403


def test_non_admin_no_orgs_empty(client):
    r = client.get("/api/v1/clinical/patients", headers=BASE_H)  # no X-Org-Guids
    assert r.status_code == 200
    assert r.get_json() == {"patients": [], "count": 0}


# --- patient index --------------------------------------------------------

def test_patients_org_scoped_with_names_and_counts(client):
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    r = client.get("/api/v1/clinical/patients", headers=h)
    assert r.status_code == 200
    body = r.get_json()
    guids = [p["patient_guid"] for p in body["patients"]]
    assert set(guids) == {"pat-a1", "pat-a2"}  # org-b excluded
    a1 = next(p for p in body["patients"] if p["patient_guid"] == "pat-a1")
    assert a1["name"] == "Anna Ek"
    assert a1["birth_date"] == "1980-01-02"
    assert a1["observation_count"] == 4
    # most-recent-activity first: pat-a1 (day 12) before pat-a2 (day 3)
    assert guids[0] == "pat-a1"
    a2 = next(p for p in body["patients"] if p["patient_guid"] == "pat-a2")
    assert a2["name"] == "Bo Berg"


def test_admin_read_without_reason_is_gated(client):
    # #575: an admin-override read must carry an attestation reason.
    h = dict(BASE_H, **{"X-Is-Admin": "1"})
    r = client.get("/api/v1/clinical/patients", headers=h)
    assert r.status_code == 428
    assert r.get_json()["code"] == "ADMIN_READ_REASON_REQUIRED"


def test_admin_sees_all_orgs(client):
    h = dict(BASE_H, **{"X-Is-Admin": "1", "X-Admin-Read-Reason": "test attest"})
    r = client.get("/api/v1/clinical/patients", headers=h)
    guids = {p["patient_guid"] for p in r.get_json()["patients"]}
    assert guids == {"pat-a1", "pat-a2", "pat-b1"}


# --- per-patient summary --------------------------------------------------

def test_patient_summary_counts_desc(client):
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    r = client.get("/api/v1/clinical/patient/pat-a1/summary", headers=h)
    assert r.status_code == 200
    body = r.get_json()
    assert body["patient_guid"] == "pat-a1"
    params = body["parameters"]
    # weight (3) before glucose (1) — sorted by count desc
    assert [p["code"] for p in params] == ["sys|weight", "sys|glucose"]
    w = params[0]
    assert w["count"] == 3 and w["unit"] == "mmol/L"
    assert w["first_observed_at"].startswith("2026-04-05")
    assert w["last_observed_at"].startswith("2026-04-12")


def test_patient_summary_org_scope_blocks_cross_org(client):
    # pat-b1 lives in org-b; a caller scoped to org-a sees no data for them.
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    r = client.get("/api/v1/clinical/patient/pat-b1/summary", headers=h)
    assert r.status_code == 200
    assert r.get_json() == {"patient_guid": "pat-b1", "parameters": [], "count": 0}


# --- series ---------------------------------------------------------------

def test_series_ordered_with_value_and_org(client):
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    r = client.get("/api/v1/clinical/patient/pat-a1/series?code=sys|weight",
                   headers=h)
    assert r.status_code == 200
    body = r.get_json()
    pts = body["points"]
    assert [p["at"][:10] for p in pts] == [
        "2026-04-05", "2026-04-08", "2026-04-12"]  # oldest → newest
    assert all(p["code"] == "sys|weight" for p in pts)
    assert all(p["org_guid"] == "org-a" for p in pts)  # for spärr on the client
    assert pts[0]["unit"] == "mmol/L"


def test_series_code_and_date_filter(client):
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    # window excludes day 5; only day 8 + 12 weight remain
    r = client.get(
        "/api/v1/clinical/patient/pat-a1/series"
        "?code=sys|weight&from=2026-04-07T00:00:00Z", headers=h)
    days = [p["at"][:10] for p in r.get_json()["points"]]
    assert days == ["2026-04-08", "2026-04-12"]


def test_series_org_scope_blocks_cross_org(client):
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    r = client.get("/api/v1/clinical/patient/pat-b1/series", headers=h)
    body = r.get_json()
    # The SECURITY property, asserted as such. This used to compare the whole
    # body for equality and so failed when #983 added order/limit/truncated/
    # next_cursor — a failure about the payload's SHAPE, not about scoping.
    # What matters is that another organisation's patient yields nothing, and
    # that nothing slipped past a limit either.
    assert body["patient_guid"] == "pat-b1"
    assert body["points"] == []
    assert body["count"] == 0
    assert body["truncated"] is False
    assert body["next_cursor"] is None


def test_series_requires_care_delivery_guard(client):
    h = {k: v for k, v in BASE_H.items() if k != "X-Access-Purpose"}
    h["X-Org-Guids"] = "org-a"
    r = client.get("/api/v1/clinical/patient/pat-a1/series", headers=h)
    assert r.status_code == 400


# --- concept display resolution (#471) ------------------------------------

def test_concept_guid_from_canonical():
    from app.api.clinical_read import _concept_guid_from_canonical as parse
    g = "1c34a590-fc2d-430c-92e6-d123b95fe392"
    assert parse("urn:pdhc:concept/" + g) == g
    assert parse("https://plan.pdhc.se/api/v1/concepts/" + g) == g
    # non-guid tails, termbank URIs, empties → None
    assert parse("urn:pdhc:concept/smoke-c-296") is None
    assert parse("https://termbank.pdhc.se/CodeSystem/loinc/4548-4") is None
    assert parse("") is None
    assert parse(None) is None


def test_lookup_display_parses_and_fails_open(monkeypatch):
    from app.services.plan_client import PlanClient, PlanUnreachable

    class _Resp:
        status_code = 200
        def json(self):
            return {"parameter": [
                {"name": "display", "valueString": "HbA1c"},
                {"name": "name", "valueString": "plan"}]}

    pc = PlanClient(base_url="http://plan.test")
    monkeypatch.setattr(pc, "_get_with_retry", lambda *a, **k: _Resp())
    assert pc.lookup_display("g1") == "HbA1c"

    pc2 = PlanClient(base_url="http://plan.test")
    monkeypatch.setattr(pc2, "_get_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(PlanUnreachable("x")))
    assert pc2.lookup_display("g1") is None      # fail open


def test_summary_includes_display(client, monkeypatch):
    import app.api.clinical_read as cr
    monkeypatch.setattr(cr, "resolve_display",
                        lambda code: "Weight" if "weight" in (code or "") else None)
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    params = client.get(
        "/api/v1/clinical/patient/pat-a1/summary", headers=h).get_json()["parameters"]
    assert next(p for p in params if p["code"] == "sys|weight")["display"] == "Weight"
    assert next(p for p in params if p["code"] == "sys|glucose")["display"] is None


def test_summary_display_none_when_plan_unconfigured(client):
    client.application.config["PLAN_BASE_URL"] = ""   # no plan → skip resolution
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    params = client.get(
        "/api/v1/clinical/patient/pat-a1/summary", headers=h).get_json()["parameters"]
    assert params and all(p["display"] is None for p in params)


# --- #983: identity + per-datapoint provenance on every point --------------
#
# Without the observation's own guid a consumer cannot ADDRESS a datapoint:
# no cursor tiebreak, no selection, nothing to look a detail up by. The
# dashboard's event list and provenance panel were blocked on this one field
# while the column sat populated in the table.

def test_every_point_carries_its_own_observation_guid(client):
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    r = client.get("/api/v1/clinical/patient/pat-a1/series?code=sys|weight",
                   headers=h)
    pts = r.get_json()["points"]
    guids = [p["observation_guid"] for p in pts]
    assert all(guids), "a point without an identity cannot be addressed at all"
    assert len(set(guids)) == len(guids), "identities must be unique per point"
    # it is the row's own guid, not something derived
    assert set(guids) <= {"o1", "o2", "o3"}, guids


def test_the_provenance_KEYS_are_present_even_when_unset(client):
    """A field absent from the payload is indistinguishable from one the
    consumer forgot to read.

    `care_plan_guid` is unpopulated on cdr1 today — 0 of 1 949 rows — and is
    projected anyway, so a consumer can render "not recorded" and keep the
    gap VISIBLE. That is the plan's step 18 principle applied to the API.
    """
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    pts = client.get("/api/v1/clinical/patient/pat-a1/series?code=sys|weight",
                     headers=h).get_json()["points"]
    for p in pts:
        for key in ("transaction_guid", "ingest_raw_guid", "care_plan_guid"):
            assert key in p, f"{key} is absent from the payload entirely"
        assert p["care_plan_guid"] is None     # unset here, but PRESENT


def test_provenance_VALUES_survive_the_projection(app, client):
    """The fixture leaves them unset, so a test that only read the fixture
    would pass against a projection that hard-coded None."""
    Obs = live_model("Observation")
    with app.app_context():
        _db.session.add(Obs(
            guid="o-prov", patient_guid="pat-a1", org_guid="org-a",
            code_canonical="sys|probe", value_unit="kg",
            effective_at=_dt(20), status="final", raw_json={},
            transaction_guid="txn-1", ingest_raw_guid="ing-1",
            care_plan_guid="cp-1",
        ))
        _db.session.commit()

    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    pts = client.get("/api/v1/clinical/patient/pat-a1/series?code=sys|probe",
                     headers=h).get_json()["points"]
    assert len(pts) == 1
    p = pts[0]
    assert p["observation_guid"] == "o-prov"
    assert p["transaction_guid"] == "txn-1"
    assert p["ingest_raw_guid"] == "ing-1"
    assert p["care_plan_guid"] == "cp-1"


def test_the_existing_fields_are_UNCHANGED(client):
    """The control for a projection edit: adding four keys must not move,
    rename or drop any of the six that were already there."""
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    p = client.get("/api/v1/clinical/patient/pat-a1/series?code=sys|weight",
                   headers=h).get_json()["points"][0]
    for key in ("code", "at", "value", "unit", "value_string", "org_guid"):
        assert key in p, f"{key} disappeared from the projection"
    assert p["code"] == "sys|weight"
    assert p["org_guid"] == "org-a"
    assert p["unit"] == "mmol/L"


# --- #983 part 2: order, cursor, visible truncation ------------------------

H_A = dict(BASE_H, **{"X-Org-Guids": "org-a"})
WEIGHT = "/api/v1/clinical/patient/pat-a1/series?code=sys|weight"


def _walk(client, url, limit):
    """Follow cursors to the end; bounded so a looping cursor FAILS."""
    seen, cursor = [], None
    for _ in range(50):
        u = f"{url}&limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(u, headers=H_A).get_json()
        seen += [p["observation_guid"] for p in body["points"]]
        cursor = body["next_cursor"]
        if not cursor:
            return seen
    raise AssertionError("cursor never terminated — the walk loops")


def test_the_DEFAULT_order_is_still_ascending(client):
    """Existing consumers rely on oldest->newest. #983 is opt-in."""
    body = client.get(WEIGHT, headers=H_A).get_json()
    assert body["order"] == "asc"
    assert [p["at"][:10] for p in body["points"]] == [
        "2026-04-05", "2026-04-08", "2026-04-12"]
    assert "undated_count" not in body       # the old path is unchanged


def test_desc_returns_NEWEST_first(client):
    body = client.get(WEIGHT + "&order=desc", headers=H_A).get_json()
    assert [p["at"][:10] for p in body["points"]] == [
        "2026-04-12", "2026-04-08", "2026-04-05"]


def test_THE_ORIGINAL_DEFECT_the_first_page_holds_the_newest_point(client):
    """`ORDER BY effective_at ASC LIMIT n` returned the OLDEST n. Asked for
    newest-first with a limit, the first page must contain the newest."""
    body = client.get(WEIGHT + "&order=desc&limit=1", headers=H_A).get_json()
    assert [p["at"][:10] for p in body["points"]] == ["2026-04-12"]
    assert body["truncated"] is True
    assert body["next_cursor"]


def test_truncation_is_REPORTED_and_the_cursor_reaches_every_row(client):
    first = client.get(WEIGHT + "&limit=2", headers=H_A).get_json()
    assert first["truncated"] is True and first["limit"] == 2
    assert first["count"] == 2
    assert sorted(_walk(client, WEIGHT, 2)) == ["o1", "o2", "o3"]


def test_an_untruncated_page_says_so(client):
    body = client.get(WEIGHT, headers=H_A).get_json()
    assert body["truncated"] is False
    assert body["next_cursor"] is None


def test_TIED_timestamps_are_neither_skipped_nor_repeated(app, client):
    """`effective_at` is not unique — two (patient, effective_at) pairs
    repeat on cdr1 today. Page size 1 puts every boundary INSIDE the tied
    group, which is the only arrangement where a timestamp-only cursor is
    visibly wrong."""
    Obs = live_model("Observation")
    with app.app_context():
        for gid in ("t-a", "t-b", "t-c"):
            _db.session.add(Obs(guid=gid, patient_guid="pat-a1",
                                org_guid="org-a", code_canonical="sys|tied",
                                value_unit="kg", effective_at=_dt(15),
                                status="final", raw_json={}))
        _db.session.commit()
    url = "/api/v1/clinical/patient/pat-a1/series?code=sys|tied"
    for order in ("asc", "desc"):
        got = _walk(client, f"{url}&order={order}", 1)
        assert sorted(got) == ["t-a", "t-b", "t-c"], (order, got)
        assert len(got) == len(set(got)), f"{order}: a row was repeated"


def test_a_malformed_cursor_is_REFUSED_not_treated_as_page_one(client):
    for bad in ("not-a-cursor", "e30", "%%%"):
        r = client.get(WEIGHT + f"&cursor={bad}", headers=H_A)
        assert r.status_code == 400, bad
        assert r.get_json()["error"] == "invalid_cursor"


def test_a_cursor_from_the_OTHER_direction_is_refused(client):
    """Resuming a descending walk with an ascending predicate returns the
    wrong rows silently."""
    asc = client.get(WEIGHT + "&limit=1", headers=H_A).get_json()["next_cursor"]
    r = client.get(WEIGHT + f"&order=desc&limit=1&cursor={asc}", headers=H_A)
    assert r.status_code == 400
    assert "order" in r.get_json()["message"]


def test_an_unknown_order_is_refused_by_name(client):
    r = client.get(WEIGHT + "&order=sideways", headers=H_A)
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_order"


def test_UNDATED_rows_are_excluded_from_paging_and_COUNTED(app, client):
    """A row with no effective time cannot be placed in a time-ordered walk.
    Excluding it silently would make the history look complete."""
    Obs = live_model("Observation")
    with app.app_context():
        _db.session.add(Obs(guid="u-1", patient_guid="pat-a1", org_guid="org-a",
                            code_canonical="sys|weight", value_unit="kg",
                            effective_at=None, status="final", raw_json={}))
        _db.session.commit()
    body = client.get(WEIGHT + "&order=desc", headers=H_A).get_json()
    assert body["undated_count"] == 1
    assert "u-1" not in [p["observation_guid"] for p in body["points"]]


# --- #983 detail: GET /api/v1/clinical/observation/<guid> -----------------

DETAIL = "/api/v1/clinical/observation/{}"


def _seed_detail(app, *, with_context=True, raw=None, guid="d-1",
                 org="org-a", patient="pat-a1"):
    from app.models import ClinicalContext
    Obs = live_model("Observation")
    with app.app_context():
        _db.session.add(Obs(
            guid=guid, patient_guid=patient, org_guid=org,
            code_canonical="sys|weight", value_quantity=71.5,
            value_unit="kg", effective_at=_dt(20), status="final",
            raw_json=raw if raw is not None else {"resourceType": "Observation"},
            ingest_raw_guid=f"ing-{guid}", transaction_guid="txn-row",
        ))
        if with_context:
            _db.session.add(ClinicalContext(
                ingest_raw_guid=f"ing-{guid}", patient_guid=patient,
                plan_definition_guid="pd-1", contract_guid="ct-1",
                provider_org_guid="prov-1", service_request_guid="sr-1",
                transaction_guid="txn-ctx",
            ))
        _db.session.commit()


def test_detail_returns_the_datapoint_its_PATIENT_and_its_context(app, client):
    """`patient_guid` is what lets the dashboard re-gate on the observation's
    OWN patient instead of on the caller's word for it."""
    _seed_detail(app)
    r = client.get(DETAIL.format("d-1"), headers=H_A)
    assert r.status_code == 200
    b = r.get_json()
    assert b["observation_guid"] == "d-1"
    assert b["patient_guid"] == "pat-a1"
    assert b["value"] == 71.5 and b["unit"] == "kg"
    assert b["context_found"] is True
    c = b["context"]
    assert c["plan_definition_guid"] == "pd-1"
    assert c["contract_guid"] == "ct-1"
    assert c["service_request_guid"] == "sr-1"
    # the row's own column wins over the side table (#769)
    assert c["transaction_guid"] == "txn-row"


def test_every_context_field_is_PRESENT_even_with_no_context_row(app, client):
    """"Not recorded" must be renderable — so the key must exist."""
    _seed_detail(app, with_context=False, guid="d-2")
    b = client.get(DETAIL.format("d-2"), headers=H_A).get_json()
    assert b["context_found"] is False
    for k in ("service_request_guid", "plan_definition_guid", "contract_guid",
              "provider_org_guid", "author_org_guid", "care_plan_guid"):
        assert k in b["context"], k
    assert b["context"]["plan_definition_guid"] is None


def test_gaps_are_filled_from_the_RAW_resource_and_SAID_so(app, client):
    raw = {"resourceType": "Observation",
           "basedOn": [{"type": "ServiceRequest",
                        "identifier": {"value": "sr-raw"}}]}
    _seed_detail(app, with_context=False, guid="d-3", raw=raw)
    b = client.get(DETAIL.format("d-3"), headers=H_A).get_json()
    assert b["context"]["service_request_guid"] == "sr-raw"
    assert "service_request_guid" in b["context_filled_from_raw"]


def test_author_org_gets_NO_fallback_from_performer(app, client):
    """#665: performer currently carries the SUBMITTER, so filling author from
    it would recreate the author/submitter conflation."""
    raw = {"resourceType": "Observation",
           "performer": [{"identifier": {"value": "submitter-org"}}]}
    _seed_detail(app, with_context=False, guid="d-4", raw=raw)
    b = client.get(DETAIL.format("d-4"), headers=H_A).get_json()
    assert b["context"]["author_org_guid"] is None


def test_another_organisations_observation_is_a_FLAT_404(app, client):
    """Same answer as "does not exist", so probing a guid learns nothing."""
    _seed_detail(app, guid="d-b", org="org-b", patient="pat-b1")
    other = client.get(DETAIL.format("d-b"), headers=H_A)
    missing = client.get(DETAIL.format("no-such-guid"), headers=H_A)
    assert other.status_code == missing.status_code == 404
    assert other.get_json() == missing.get_json()


def test_detail_requires_the_care_delivery_guard(app, client):
    _seed_detail(app, guid="d-5")
    h = {k: v for k, v in H_A.items() if k != "X-Access-Purpose"}
    assert client.get(DETAIL.format("d-5"), headers=h).status_code == 400



def test_series_TRANSACTION_filter_is_applied_and_ECHOED(app, client):
    """#977: the dashboard's plan filter. The echo is how a caller tells
    "filtered" from "an older cdr ignored the parameter"."""
    Obs = live_model("Observation")
    with app.app_context():
        for g, tx in (("o-t1", "txn-A"), ("o-t2", "txn-B"), ("o-t3", "txn-A")):
            _db.session.add(Obs(
                guid=g, patient_guid="pat-a1", org_guid="org-a",
                code_canonical="sys|txprobe", value_unit="kg",
                effective_at=_dt(20), status="final", raw_json={},
                transaction_guid=tx))
        _db.session.commit()
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    body = client.get("/api/v1/clinical/patient/pat-a1/series"
                      "?code=sys|txprobe&transaction=txn-A&order=desc",
                      headers=h).get_json()
    assert sorted(p["observation_guid"] for p in body["points"]) == ["o-t1", "o-t3"]
    assert body["transaction_filter"] is True


def test_series_without_a_transaction_filter_does_not_claim_one(client):
    h = dict(BASE_H, **{"X-Org-Guids": "org-a"})
    body = client.get("/api/v1/clinical/patient/pat-a1/series", headers=h).get_json()
    assert "transaction_filter" not in body
