"""#1049 (contract s7.4): "Mitt patientkort" answers in the CDR.

A card answer carries `card_section` + `card_version` in its clinical
context. A version is decided as a whole; a newer pending version of the same
section supersedes an older pending one, in the ingest's own transaction.

Every gate is exercised through the real routes with AUTH_MODE=sso: the only
thing replaced is the HTTP call to sso.pdhc (`validate_sso_token`), which
answers with the blob sso would return for that token. Service keys are the
real config values the routes compare against.
"""
from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool

from app import db
from app.models import AuditLog, ClinicalContext, IngestRaw
from app.models.resources import RESOURCES, history_model, live_model
from app.services import review as rv
from tests.test_review_1044 import (BLOBS, GATEWAY, PATIENTAPP, PATIENTAPP_KEY,
                                    SIM, UNIT_A, UNIT_B, _bearer, _own_app)

Obs = live_model("Observation")


@pytest.fixture(autouse=True)
def _sso_on(app, monkeypatch):
    from app import auth
    monkeypatch.setitem(app.config, "AUTH_MODE", "sso")
    monkeypatch.setitem(app.config, "PATIENTAPP_PDHC_SERVICE_KEY", PATIENTAPP_KEY)
    monkeypatch.setattr(auth, "validate_sso_token", lambda token: BLOBS.get(token))


@pytest.fixture(autouse=True)
def _isolate(app):
    def _wipe():
        with app.app_context():
            db.session.rollback()
            for table in ("clinical_context", "observation", "observation_history",
                          "fhir_resources", "cambio_delivery_log", "audit_log",
                          "dedupe_registry", "change_feed", "ingest_raw"):
                db.session.execute(db.text(f"DELETE FROM {table}"))
            db.session.commit()
    _wipe()
    yield
    _wipe()


def _card(*, patient, version, section="allergy", link=None, value="Penicillin",
          unit=UNIT_A, group=None, coded=None, **ctx_extra):
    """One extracted card answer as gateway's s7.3 path forwards it: no
    ServiceRequest, no care plan, the patient as performer."""
    link = link or f"{section}.substance"
    fhir = {"resourceType": "Observation", "status": "final",
            "id": str(uuid.uuid4()), "code": {"text": link},
            "effectiveDateTime": "2026-11-22T08:00:00+00:00",
            "subject": {"reference": f"Patient/{patient}"},
            "performer": [{"type": "Patient", "reference": f"Patient/{patient}",
                           "identifier": {"value": patient}}]}
    if coded:
        fhir["valueCodeableConcept"] = {"coding": [{"code": coded[0],
                                                    "display": coded[1]}]}
    elif isinstance(value, bool):
        fhir["valueBoolean"] = value
    elif isinstance(value, (int, float)):
        fhir["valueQuantity"] = {"value": value, "unit": "st"}
    else:
        fhir["valueString"] = value
    ctx = {"review_required": True, "data_origin": "patient_generated",
           "transaction_guid": link, "link_id": link, "patient_org_guid": unit,
           "source_service": "patientapp.pdhc",
           "card_section": section, "card_version": version}
    if group:
        ctx["group"] = group
    ctx.update(ctx_extra)
    return {"patient_guid": patient, "source_type": "fhir",
            "fhir_resource": fhir, "clinical_context": ctx}


def _send(client, **kw):
    body = _card(**kw)
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 202, r.get_json()
    return body["fhir_resource"]["id"]


def _version(client, patient, *answers, section="allergy", unit=UNIT_A):
    """Send one card version of `answers` [(link suffix, value)...]."""
    v = str(uuid.uuid4())
    guids = [_send(client, patient=patient, version=v, section=section,
                   link=f"{section}.{suffix}", value=value, unit=unit,
                   group=f"g{i}")
             for i, (suffix, value) in enumerate(answers)]
    return v, guids


def _row(guid):
    db.session.expire_all()
    return db.session.get(Obs, guid)


def _status(guids):
    db.session.expire_all()
    return [db.session.get(Obs, g).review_status for g in guids]


P = lambda: str(uuid.uuid4())   # noqa: E731 - a fresh synthetic patient guid


# ---------------------------------------------------------------------------
# 1. Schema and migration
# ---------------------------------------------------------------------------

CARD_COLUMNS = ("card_section", "card_version")


@pytest.mark.parametrize("fhir_name", [r[0] for r in RESOURCES])
def test_every_live_and_history_table_has_the_card_columns(fhir_name):
    for model in (live_model(fhir_name), history_model(fhir_name)):
        cols = {c.key: c for c in inspect(model).columns}
        for name in CARD_COLUMNS:
            assert name in cols, (model.__tablename__, name)
            assert cols[name].nullable and cols[name].default is None \
                and cols[name].server_default is None
    live = live_model(fhir_name).__table__
    assert f"ix_{live.name}_card_version" in {i.name for i in live.indexes}


MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"


def _migration():
    path = MIGRATIONS / "versions" / "e6f7a8b9c0d1_1049_card_version.py"
    spec = importlib.util.spec_from_file_location("m1049", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_is_the_single_head_on_top_of_1044():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["e6f7a8b9c0d1"]
    mod = _migration()
    assert mod.down_revision == "d5e6f7a8b9c0" and len(mod.revision) <= 32


def test_migration_on_a_POPULATED_table_is_additive_idempotent_and_reversible():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    mod = _migration()
    engine = sa.create_engine("sqlite://", poolclass=StaticPool)
    with engine.connect() as conn:
        for t in ("observation", "observation_history", "condition"):
            conn.execute(sa.text(f"create table {t} (guid varchar(36), "
                                 f"review_status varchar(16))"))
        conn.execute(sa.text("insert into observation values ('o-1', 'pending')"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
            mod.upgrade()                                  # idempotent
        insp = sa.inspect(conn)
        for t in ("observation", "observation_history", "condition"):
            assert set(CARD_COLUMNS) <= {c["name"] for c in insp.get_columns(t)}
        assert {i["name"] for i in insp.get_indexes("observation")} == {
            "ix_observation_card_version"}
        row = conn.execute(sa.text("select * from observation")).mappings().one()
        assert (row["guid"], row["review_status"]) == ("o-1", "pending")
        assert row["card_section"] is None and row["card_version"] is None
        with Operations.context(ctx):
            mod.downgrade()
        insp = sa.inspect(conn)
        assert not set(CARD_COLUMNS) & {c["name"] for c in insp.get_columns("observation")}
        assert conn.execute(sa.text("select count(*) from observation")).scalar() == 1


# ---------------------------------------------------------------------------
# 2. Ingest
# ---------------------------------------------------------------------------

def test_a_card_answer_WITHOUT_a_service_request_is_stored_pending(client):
    patient, v = P(), str(uuid.uuid4())
    body = _card(patient=patient, version=v.upper())       # case is normalised
    assert "service_request_guid" not in body["clinical_context"]
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 202, r.get_json()
    row = _row(body["fhir_resource"]["id"])
    assert (row.card_section, row.card_version, row.review_status,
            row.data_origin, row.patient_org_guid) == (
        "allergy", v, "pending", "patient_generated", UNIT_A)
    ctx = ClinicalContext.query.filter_by(ingest_raw_guid=row.ingest_raw_guid).one()
    assert ctx.service_request_guid is None and ctx.source_service == "patientapp.pdhc"


def test_the_PATIENT_as_performer_is_not_stored_as_an_organisation(client):
    patient = P()
    guid = _send(client, patient=patient, version=str(uuid.uuid4()))
    assert _row(guid).org_guid is None


def test_a_flat_card_payload_keeps_section_and_version(client):
    patient, v = P(), str(uuid.uuid4())
    body = _card(patient=patient, version=v)
    flat = dict(body, **body.pop("clinical_context"))
    r = client.post("/api/v1/ingest", json=flat, headers=GATEWAY)
    assert r.status_code == 202, r.get_json()
    assert _row(flat["fhir_resource"]["id"]).card_version == v


@pytest.mark.parametrize("change,error", [
    ({"card_version": None}, "incomplete_card"),
    ({"card_section": None}, "incomplete_card"),
    ({"card_section": "diagnosis", "transaction_guid": "diagnosis.x"},
     "invalid_card_section"),
    ({"card_version": "not-a-uuid"}, "invalid_card_version"),
    ({"card_version": 7}, "invalid_card_version"),
    ({"review_required": None}, "card_requires_review"),
    ({"transaction_guid": "medication.name"}, "card_section_mismatch"),
    ({"patient_org_guid": None}, "card_needs_patient_org"),
])
def test_an_unusable_card_answer_is_refused_by_name(client, change, error):
    body = _card(patient=P(), version=str(uuid.uuid4()))
    body["clinical_context"].update(change)
    body["clinical_context"] = {k: v for k, v in body["clinical_context"].items()
                                if v is not None}
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 422, r.get_json()
    assert r.get_json()["errors"][0].startswith(error), r.get_json()
    assert IngestRaw.query.count() == 0


@pytest.mark.parametrize("other", ["patient", "section", "unit"])
def test_a_card_version_belongs_to_ONE_patient_section_and_unit(client, other):
    patient, v = P(), str(uuid.uuid4())
    _send(client, patient=patient, version=v)
    kw = {"patient": patient, "version": v}
    if other == "patient":
        kw["patient"] = P()
    elif other == "section":
        kw["section"] = "medication"
    else:
        kw["unit"] = UNIT_B
    r = client.post("/api/v1/ingest", json=_card(**kw), headers=GATEWAY)
    assert r.status_code == 422
    assert r.get_json()["errors"][0].startswith("card_version_conflict")


def test_no_answer_joins_a_version_that_is_already_decided(client):
    patient = P()
    v, _ = _version(client, patient, ("substance", "Penicillin"))
    assert client.post(f"/api/v1/reviews/cards/{v}/approve",
                       headers=_bearer("doctor-a")).status_code == 201
    r = client.post("/api/v1/ingest", json=_card(patient=patient, version=v,
                                                 link="allergy.reaction"),
                    headers=GATEWAY)
    assert r.status_code == 422
    assert r.get_json()["errors"][0].startswith("card_version_closed")


# ---------------------------------------------------------------------------
# 3. Latest wins: supersede, in the ingest's transaction
# ---------------------------------------------------------------------------

def test_a_newer_version_supersedes_the_older_PENDING_one_only(client):
    patient = P()
    old_v, old = _version(client, patient, ("substance", "Penicillin"),
                          ("reaction", "Utslag"))
    end_v, end = _version(client, patient, ("substance", "Nötter"),
                          section="medication")      # another section: untouched
    other_patient_v, other = _version(client, P(), ("substance", "Gräs"))
    v1 = client.post("/api/v1/ingest", headers=GATEWAY, json=dict(
        _card(patient=patient, version=str(uuid.uuid4())),
        clinical_context={"review_required": True, "data_origin": "patient_generated",
                          "transaction_guid": "allergy.substance",
                          "patient_org_guid": UNIT_A}))     # a v1 row, no card
    assert v1.status_code == 202, v1.get_json()
    v1_guid = Obs.query.filter(Obs.card_version.is_(None)).one().guid

    new_v, new = _version(client, patient, ("substance", "Penicillin"))
    assert _status(old) == ["superseded", "superseded"]
    assert _status(new) == ["pending"]
    assert _status(end + other + [v1_guid]) == ["pending"] * 3
    for g in old:
        row = _row(g)
        assert row.reviewed_at is not None
        assert row.reviewed_by_user_guid is None and row.reviewed_by_name is None
        assert row.review_reason is None


def test_the_second_row_of_a_version_does_not_supersede_the_first(client):
    v, guids = _version(client, P(), ("substance", "A"), ("reaction", "B"),
                        ("severity", "C"))
    assert _status(guids) == ["pending"] * 3


def test_an_ENDORSED_version_is_untouched_by_a_newer_one(client):
    patient = P()
    v, guids = _version(client, patient, ("substance", "Penicillin"))
    client.post(f"/api/v1/reviews/cards/{v}/approve", headers=_bearer("doctor-a"))
    _version(client, patient, ("substance", "Inget"))
    assert _status(guids) == ["endorsed"]


def test_supersede_is_in_the_SAME_transaction_as_the_ingest(client, monkeypatch):
    patient = P()
    _v, old = _version(client, patient, ("substance", "Penicillin"))
    from app.services import ingest_pipeline as ip

    def boom(**_kw):
        raise RuntimeError("fails after the live row and the supersede")
    monkeypatch.setattr(ip, "synthetic_reason", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/v1/ingest", headers=GATEWAY,
                    json=_card(patient=patient, version=str(uuid.uuid4())))
    db.session.rollback()
    assert _status(old) == ["pending"]
    assert Obs.query.count() == 1


# ---------------------------------------------------------------------------
# 4. The section queue
# ---------------------------------------------------------------------------

def test_card_list_shape_with_the_endorsed_version_for_the_diff(client):
    patient = P()
    v1, g1 = _version(client, patient, ("substance", "Penicillin"), ("reaction", "Utslag"))
    h = _bearer("doctor-a")
    client.post(f"/api/v1/reviews/cards/{v1}/approve", headers=h,
                json={"amend": {g1[1]: "Nässelutslag"}})
    v2, g2 = _version(client, patient, ("substance", "Penicillin"))

    r = client.get("/api/v1/reviews/cards", headers=h)
    assert r.status_code == 200, r.get_json()
    (item,) = r.get_json()["items"]
    assert (item["card_version"], item["patient_guid"], item["section"],
            item["status"], item["can_decide"]) == (v2, patient, "allergy",
                                                    "pending", True)
    assert item["submitted_at"]
    assert item["rows"] == [{"guid": g2[0], "link_id": "allergy.substance",
                             "group": "g0", "label": "allergy.substance",
                             "value": "Penicillin", "display": "Penicillin"}]
    end = item["endorsed"]
    assert end["card_version"] == v1 and end["reviewed_at"]
    vals = {r["link_id"]: r["value"] for r in end["rows"]}
    assert vals == {"allergy.substance": "Penicillin",
                    "allergy.reaction": "Nässelutslag"}   # what care endorsed
    assert {r["original_guid"] for r in end["rows"]} == set(g1)


def test_card_list_is_scoped_and_v1_queue_holds_no_card_rows(client):
    va, _ = _version(client, P(), ("substance", "A"))
    vb, _ = _version(client, P(), ("substance", "B"), unit=UNIT_B)
    lists = {who: [i["card_version"] for i in client.get(
        "/api/v1/reviews/cards", headers=_bearer(who)).get_json()["items"]]
        for who in ("doctor-a", "nurse-b", "admin")}
    assert lists["doctor-a"] == [va] and lists["nurse-b"] == [vb]
    assert sorted(lists["admin"]) == sorted([va, vb])
    admin_items = client.get("/api/v1/reviews/cards",
                             headers=_bearer("admin")).get_json()["items"]
    assert not any(i["can_decide"] for i in admin_items)
    for who in ("doctor-a", "admin"):
        assert client.get("/api/v1/reviews?status=all",
                          headers=_bearer(who)).get_json()["items"] == []


@pytest.mark.parametrize("who,status,error", [
    (None, 401, "not_authenticated"), ("patient", 403, "not_professional"),
    ("mustchange", 403, "must_change_password"),
    ("othercare-a", 403, "no_review_scope")])
def test_card_list_refusals(client, who, status, error):
    r = client.get("/api/v1/reviews/cards", headers=_bearer(who) if who else {})
    assert r.status_code == status and r.get_json()["error"] == error


def test_card_list_status_filter(client):
    patient = P()
    old, _ = _version(client, patient, ("substance", "A"))
    new, _ = _version(client, patient, ("substance", "B"))
    h = _bearer("doctor-a")
    got = lambda s: [i["card_version"] for i in client.get(        # noqa: E731
        f"/api/v1/reviews/cards?status={s}", headers=h).get_json()["items"]]
    assert got("superseded") == [old] and got("pending") == [new]
    assert got("all") == [old, new]
    assert client.get("/api/v1/reviews/cards?status=x", headers=h).status_code == 400


# ---------------------------------------------------------------------------
# 5. Approve / reject a version
# ---------------------------------------------------------------------------

def test_approve_writes_one_endorsement_per_row_in_ONE_transaction(client):
    patient = P()
    v, guids = _version(client, patient, ("substance", "Penicillin"),
                        ("reaction", "Utslag"), ("severity", "Svår"))
    before = {g: _row(g).raw_json for g in guids}
    r = client.post(f"/api/v1/reviews/cards/{v}/approve", headers=_bearer("nurse-a"),
                    json={"amend": {guids[1]: "Nässelutslag", guids[2]: "Svår"},
                          "note": "Kontrollerat i telefon"})
    assert r.status_code == 201, r.get_json()
    out = r.get_json()
    assert (out["endorsed"], out["amended"]) == (3, 1)     # same value = no amendment
    assert _status(guids) == ["endorsed"] * 3
    db.session.expire_all()
    ends = Obs.query.filter(Obs.endorses_guid.isnot(None)).all()
    assert sorted(e.endorses_guid for e in ends) == sorted(guids)
    assert sorted(e.guid for e in ends) == sorted(out["endorsement_guids"])
    for e in ends:
        assert (e.card_section, e.card_version, e.data_origin, e.review_status) == (
            "allergy", v, None, None)
        assert e.org_guid == UNIT_A and e.reviewed_by_name == "Nurse A"
    by_orig = {e.endorses_guid: e for e in ends}
    assert by_orig[guids[1]].value_string == "Nässelutslag"
    assert {g: _row(g).raw_json for g in guids} == before   # originals untouched
    assert AuditLog.query.filter_by(event_type="review.card_approved").count() == 1


def test_approve_is_all_or_nothing(client, monkeypatch):
    v, guids = _version(client, P(), ("substance", "A"), ("reaction", "B"))
    real = rv._endorse
    calls = []

    def second_fails(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("the second row fails")
        return real(*a, **kw)
    monkeypatch.setattr(rv, "_endorse", second_fails)
    with pytest.raises(RuntimeError):
        client.post(f"/api/v1/reviews/cards/{v}/approve", headers=_bearer("doctor-a"))
    assert _status(guids) == ["pending", "pending"]
    assert Obs.query.filter(Obs.endorses_guid.isnot(None)).count() == 0


@pytest.mark.parametrize("amend,code", [
    ({"no-such-row": "x"}, "unknown_row"),
    ("not a dict", "wrong_type"),
    ("ROW0:", "required"),
])
def test_a_bad_amendment_is_refused_before_any_write(client, amend, code):
    v, guids = _version(client, P(), ("substance", "A"))
    if amend == "ROW0:":
        amend = {guids[0]: ""}
    r = client.post(f"/api/v1/reviews/cards/{v}/approve",
                    headers=_bearer("doctor-a"), json={"amend": amend})
    assert r.status_code == 422 and r.get_json()["code"] == code, r.get_json()
    assert _status(guids) == ["pending"]
    assert Obs.query.filter(Obs.endorses_guid.isnot(None)).count() == 0


def test_reject_marks_every_row_and_needs_a_reason(client):
    v, guids = _version(client, P(), ("substance", "A"), ("reaction", "B"))
    h = _bearer("doctor-a")
    for payload in (None, {}, {"reason": "  "}):
        assert client.post(f"/api/v1/reviews/cards/{v}/reject", headers=h,
                           json=payload).status_code == 422
    r = client.post(f"/api/v1/reviews/cards/{v}/reject", headers=h,
                    json={"reason": "Fel patient"})
    assert r.status_code == 200 and r.get_json()["rejected"] == 2
    assert _status(guids) == ["rejected", "rejected"]
    assert {_row(g).review_reason for g in guids} == {"Fel patient"}
    assert Obs.query.filter(Obs.endorses_guid.isnot(None)).count() == 0


@pytest.mark.parametrize("first,second", [("approve", "approve"),
                                          ("approve", "reject"),
                                          ("reject", "approve")])
def test_a_second_decision_is_409_already_decided(client, first, second):
    v, _ = _version(client, P(), ("substance", "A"))
    h = _bearer("doctor-a")
    assert client.post(f"/api/v1/reviews/cards/{v}/{first}", headers=h,
                       json={"reason": "x"}).status_code in (200, 201)
    r = client.post(f"/api/v1/reviews/cards/{v}/{second}", headers=h,
                    json={"reason": "x"})
    assert r.status_code == 409 and r.get_json()["error"] == "already_decided"


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_a_superseded_version_is_409_superseded(client, action):
    patient = P()
    old, guids = _version(client, patient, ("substance", "A"))
    _version(client, patient, ("substance", "B"))
    r = client.post(f"/api/v1/reviews/cards/{old}/{action}",
                    headers=_bearer("doctor-a"), json={"reason": "x"})
    assert r.status_code == 409 and r.get_json()["error"] == "superseded"
    assert _status(guids) == ["superseded"]


def test_a_version_superseded_AFTER_the_check_writes_nothing(client):
    """The race: the reviewer loaded the rows, then a newer version arrived."""
    patient = P()
    old, guids = _version(client, patient, ("substance", "A"))
    rows = rv.card_rows(old)
    _version(client, patient, ("substance", "B"))
    unit = rv.may_decide(BLOBS["doctor-a"], UNIT_A)
    actor = {"user_guid": BLOBS["doctor-a"]["user_guid"], "name": "Doctor A"}
    with client.application.test_request_context():
        with pytest.raises(rv.Superseded):
            rv.approve_card(rows, actor=actor, unit=unit)
        with pytest.raises(rv.Superseded):
            rv.reject_card(rows, actor=actor, unit=unit, reason="x")
    assert _status(guids) == ["superseded"]
    assert Obs.query.filter(Obs.endorses_guid.isnot(None)).count() == 0


# ---------------------------------------------------------------------------
# 6. Who may decide a version (s4.2's gate, unchanged)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("who", ["nurse-b", "admin", "othercare-a", "patient",
                                 "patient-forged"])
@pytest.mark.parametrize("action", ["approve", "reject"])
def test_only_an_affiliated_doctor_or_nurse_decides(client, who, action):
    v, guids = _version(client, P(), ("substance", "A"))
    r = client.post(f"/api/v1/reviews/cards/{v}/{action}", headers=_bearer(who),
                    json={"reason": "x"})
    assert r.status_code == 403, (who, r.get_json())
    assert _status(guids) == ["pending"]


def test_an_unknown_version_and_a_foreign_one_answer_the_SAME_403(client):
    v, _ = _version(client, P(), ("substance", "A"))
    h = _bearer("nurse-b")
    foreign = client.post(f"/api/v1/reviews/cards/{v}/approve", headers=h)
    unknown = client.post(f"/api/v1/reviews/cards/{uuid.uuid4()}/approve", headers=h)
    garbage = client.post("/api/v1/reviews/cards/nope/approve", headers=h)
    assert foreign.status_code == unknown.status_code == garbage.status_code == 403
    assert foreign.get_json() == unknown.get_json() == garbage.get_json()


@pytest.mark.parametrize("headers", [GATEWAY, SIM, PATIENTAPP, {}])
def test_no_service_key_decides_a_version(client, headers):
    v, guids = _version(client, P(), ("substance", "A"))
    for action in ("approve", "reject"):
        r = client.post(f"/api/v1/reviews/cards/{v}/{action}", headers=headers,
                        json={"reason": "x"})
        assert r.status_code == 401, r.get_json()
    assert client.get("/api/v1/reviews/cards", headers=headers).status_code == 401
    assert _status(guids) == ["pending"]


def test_the_per_row_routes_refuse_a_card_row_and_still_serve_v1(client):
    v, guids = _version(client, P(), ("substance", "A"))
    h = _bearer("doctor-a")
    for action in ("approve", "reject"):
        r = client.post(f"/api/v1/reviews/{guids[0]}/{action}", headers=h,
                        json={"reason": "x"})
        assert r.status_code == 409 and r.get_json()["error"] == "card_row"
        assert r.get_json()["card_version"] == v
    assert _status(guids) == ["pending"]
    # an off-unit caller still gets the flat 403 first, not the card hint
    assert client.post(f"/api/v1/reviews/{guids[0]}/approve",
                       headers=_bearer("nurse-b")).status_code == 403
    # a v1 answer (no card_version) is decided per row, as before
    body = _card(patient=P(), version=str(uuid.uuid4()))
    for k in ("card_section", "card_version"):
        body["clinical_context"].pop(k)
    assert client.post("/api/v1/ingest", json=body, headers=GATEWAY).status_code == 202
    assert client.post(f"/api/v1/reviews/{body['fhir_resource']['id']}/approve",
                       headers=h).status_code == 201


# ---------------------------------------------------------------------------
# 7. The patient app: Mitt patientkort
# ---------------------------------------------------------------------------

def _card_status(client, patient, headers=PATIENTAPP):
    return client.get(f"/api/v1/reviews/card/by-patient/{patient}", headers=headers)


def test_card_status_shape_own_answers_only_never_who_decided(client):
    patient = P()
    h = _bearer("doctor-a")
    v1, g1 = _version(client, patient, ("substance", "Penicillin"), ("reaction", "Utslag"))
    client.post(f"/api/v1/reviews/cards/{v1}/approve", headers=h,
                json={"amend": {g1[1]: "Nässelutslag"}})
    v2, _ = _version(client, patient, ("substance", "Penicillin"))
    vm, _ = _version(client, patient, ("name", "Alvedon"), section="medication")
    client.post(f"/api/v1/reviews/cards/{vm}/reject", headers=h,
                json={"reason": "Ange styrka"})
    _version(client, P(), ("substance", "Gräs"))           # someone else

    r = _card_status(client, patient)
    assert r.status_code == 200, r.get_json()
    assert r.headers["Cache-Control"] == "no-store"
    secs = r.get_json()["sections"]
    assert set(secs) == set(rv.SECTIONS)
    assert secs["history"] == {"latest": None, "endorsed": None}

    a = secs["allergy"]
    assert set(a["latest"]) == {"card_version", "submitted_at", "status", "reason",
                                "decided_at", "answers"}
    assert (a["latest"]["card_version"], a["latest"]["status"],
            a["latest"]["reason"], a["latest"]["decided_at"]) == (v2, "pending",
                                                                  None, None)
    assert a["latest"]["answers"] == [{"link_id": "allergy.substance",
                                       "group": "g0", "value": "Penicillin",
                                       "display": "Penicillin"}]
    assert set(a["endorsed"]) == {"card_version", "decided_at", "answers"}
    assert a["endorsed"]["card_version"] == v1 and a["endorsed"]["decided_at"]
    vals = {x["link_id"]: x["value"] for x in a["endorsed"]["answers"]}
    assert vals == {"allergy.substance": "Penicillin",
                    "allergy.reaction": "Utslag"}           # NOT care's amendment

    m = secs["medication"]["latest"]
    assert (m["status"], m["reason"]) == ("rejected", "Ange styrka") and m["decided_at"]
    assert secs["medication"]["endorsed"] is None

    text = r.get_data(as_text=True)
    for leak in ("Doctor A", BLOBS["doctor-a"]["user_guid"], "Nässelutslag",
                 "reviewed_by", "Gräs"):
        assert leak not in text, leak


def test_card_status_never_shows_a_superseded_version_as_latest(client):
    patient = P()
    _version(client, patient, ("substance", "Gammal"))
    new, _ = _version(client, patient, ("substance", "Ny"))
    latest = _card_status(client, patient).get_json()["sections"]["allergy"]["latest"]
    assert latest["card_version"] == new and latest["answers"][0]["value"] == "Ny"


def test_card_status_shows_a_coded_answer_in_words(client):
    patient = P()
    _send(client, patient=patient, version=str(uuid.uuid4()),
          link="allergy.severity", coded=("severe", "Svår"))
    (ans,) = _card_status(client, patient).get_json()["sections"]["allergy"]["latest"]["answers"]
    assert (ans["value"], ans["display"]) == ("severe", "Svår")


def test_card_status_door_is_the_patientapp_key_and_GET_only(client, app, monkeypatch):
    patient = P()
    _version(client, patient, ("substance", "A"))
    assert _card_status(client, patient).status_code == 200            # the control
    for headers in ({}, GATEWAY, SIM, _bearer("doctor-a"), _bearer("admin"),
                    dict(PATIENTAPP, **{"X-Service-Key": "wrong"}),
                    {"X-Source-Service": "dashboard.pdhc",
                     "X-Service-Key": PATIENTAPP_KEY}):
        r = _card_status(client, patient, headers)
        assert r.status_code == 401, headers
        assert r.get_json()["error"] == "invalid_service_credentials"
    for method in ("post", "put", "delete"):
        assert getattr(client, method)(f"/api/v1/reviews/card/by-patient/{patient}",
                                       headers=PATIENTAPP).status_code == 405
    monkeypatch.setitem(app.config, "PATIENTAPP_PDHC_SERVICE_KEY", "")
    assert _card_status(client, patient).status_code == 401           # unset: nobody


def test_the_patientapp_key_opens_no_card_decision(client):
    v, guids = _version(client, P(), ("substance", "A"))
    for action in ("approve", "reject"):
        assert client.post(f"/api/v1/reviews/cards/{v}/{action}", headers=PATIENTAPP,
                           json={"reason": "x"}).status_code == 401
    assert _status(guids) == ["pending"]


def test_the_v1_status_list_holds_no_card_rows(client):
    patient = P()
    _version(client, patient, ("substance", "A"))
    r = client.get(f"/api/v1/reviews/by-patient/{patient}", headers=PATIENTAPP)
    assert r.status_code == 200 and r.get_json()["items"] == []


# ---------------------------------------------------------------------------
# 8. The export for eps
# ---------------------------------------------------------------------------

def test_the_export_carries_card_section_version_and_reviewed_at(client, app):
    import json
    patient = P()
    v, guids = _version(client, patient, ("substance", "A"), ("reaction", "B"))
    client.post(f"/api/v1/reviews/cards/{v}/approve", headers=_bearer("doctor-a"))
    rows = rv.export_endorsements(patient)
    assert len(rows) == 2
    for row in rows:
        assert (row["card_section"], row["card_version"]) == ("allergy", v)
        assert row["reviewed_at"] and row["data_origin"] is None
    res = app.test_cli_runner().invoke(
        args=["export-eps-endorsements", "--patient", patient])
    assert res.exit_code == 0, res.output
    assert {r["card_version"] for r in json.loads(res.output)} == {v}


def test_the_exported_reviewed_at_carries_its_timezone(client, app):
    """#1049 e2e seam: eps's derivation REFUSES a reviewed_at without an
    offset (it orders card versions by it). The column is timestamptz and
    every write is UTC, but a backend that drops tzinfo (SQLite, this suite)
    handed the export a naive value, and the real eps aborted the run.
    Through the real CLI, as eps receives it."""
    import json
    from datetime import datetime
    patient = P()
    v, _ = _version(client, patient, ("substance", "A"))
    client.post(f"/api/v1/reviews/cards/{v}/approve", headers=_bearer("doctor-a"))
    res = app.test_cli_runner().invoke(
        args=["export-eps-endorsements", "--patient", patient])
    assert res.exit_code == 0, res.output
    (row,) = json.loads(res.output)
    at = datetime.fromisoformat(row["reviewed_at"])
    assert at.tzinfo is not None and at.utcoffset().total_seconds() == 0


# ---------------------------------------------------------------------------
# 9. The analysis repositories (cdr2/cdr3)
# ---------------------------------------------------------------------------

@pytest.fixture
def analysis_app():
    app = _own_app(CDR_CLINICAL_READ=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_an_ANALYSIS_cdr_refuses_a_card_answer_and_has_no_card_routes(analysis_app):
    c = analysis_app.test_client()
    r = c.post("/api/v1/ingest", headers=GATEWAY,
               json=_card(patient=P(), version=str(uuid.uuid4())))
    assert r.status_code == 422
    assert r.get_json()["errors"][0].startswith("review_not_available_here")
    v = str(uuid.uuid4())
    for method, url, headers in (
            ("get", "/api/v1/reviews/cards", _bearer("doctor-a")),
            ("post", f"/api/v1/reviews/cards/{v}/approve", _bearer("doctor-a")),
            ("post", f"/api/v1/reviews/cards/{v}/reject", _bearer("doctor-a")),
            ("get", f"/api/v1/reviews/card/by-patient/{v}", PATIENTAPP)):
        assert getattr(c, method)(url, headers=headers).status_code == 404, url
