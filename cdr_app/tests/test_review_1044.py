"""#1044: patient-reported EPS answers are stored untouched, reviewed by
care, and an approval writes a NEW endorsement row.

Every gate here is exercised through the real routes with AUTH_MODE=sso: the
only thing replaced is the HTTP call to sso.pdhc (`validate_sso_token`), which
answers with the blob sso would return for that token. A gate that only runs
under AUTH_MODE=off proves nothing.
"""
from __future__ import annotations

import importlib.util
import pathlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool

from app import create_app, db
from app.models import AuditLog, ClinicalContext, IngestRaw
from app.models.resources import RESOURCES, history_model, live_model
from app.services import review as rv

UNIT_A = "aaaaaaaa-0000-4000-8000-00000000000a"
UNIT_B = "bbbbbbbb-0000-4000-8000-00000000000b"
PROVIDER = "cccccccc-0000-4000-8000-00000000000c"   # the patient app's org
PATIENTAPP_KEY = "test-patientapp-key-1044"

GATEWAY = {"X-Source-Service": "gateway.pdhc", "X-Service-Key": "test-gateway-key"}
SIM = {"X-Source-Service": "sim.pdhc", "X-Service-Key": "test-sim-key"}
PATIENTAPP = {"X-Source-Service": "patientapp.pdhc", "X-Service-Key": PATIENTAPP_KEY}


def _aff(unit, role):
    return {"affiliation_guid": f"aff-{unit[:4]}-{role}", "care_unit_guid": unit,
            "care_unit_name": f"Unit {unit[:1].upper()}", "role": role,
            "role_guid": f"role-{role}", "is_admin": False}


def _prof(name, affs, **extra):
    return dict({"user_guid": str(uuid.uuid5(uuid.NAMESPACE_DNS, name)),
                 "email": f"{name}@example.test", "display_name": name,
                 "user_type": "professional", "is_su_admin": False,
                 "affiliations": affs}, **extra)


BLOBS = {
    "doctor-a": _prof("Doctor A", [_aff(UNIT_A, "doctor")]),
    "nurse-a": _prof("Nurse A", [_aff(UNIT_A, "nurse")]),
    "nurse-b": _prof("Nurse B", [_aff(UNIT_B, "nurse")]),
    "othercare-a": _prof("Other A", [_aff(UNIT_A, "other_care")]),
    "admin": _prof("Admin", [], is_su_admin=True),
    "admin-doctor-a": _prof("Admin Doc", [_aff(UNIT_A, "doctor")], is_su_admin=True),
    "mustchange": _prof("Must", [_aff(UNIT_A, "doctor")], must_change_password=True),
    "patient": {"user_guid": str(uuid.uuid4()), "user_type": "patient",
                "display_name": "Pat", "affiliations": []},
    # Not something sso issues; it is here so the user_type check is tested
    # on its own and not through "a patient happens to have no affiliation".
    "patient-forged": {"user_guid": str(uuid.uuid4()), "user_type": "patient",
                       "display_name": "Pat F",
                       "affiliations": [_aff(UNIT_A, "doctor")]},
}


def _bearer(name):
    return {"Authorization": f"Bearer {name}"}


@pytest.fixture(autouse=True)
def _sso_on(app, monkeypatch):
    """Auth ON for every test in this module."""
    from app import auth
    monkeypatch.setitem(app.config, "AUTH_MODE", "sso")
    monkeypatch.setitem(app.config, "PATIENTAPP_PDHC_SERVICE_KEY", PATIENTAPP_KEY)
    monkeypatch.setattr(auth, "validate_sso_token", lambda token: BLOBS.get(token))


@pytest.fixture(autouse=True)
def _isolate(app):
    from app.models.history_guard import history_purge
    def _wipe():
        with app.app_context():
            db.session.rollback()
        with app.app_context(), history_purge(db.session):   # #1054
            for table in ("clinical_context", "observation", "observation_history",
                          "fhir_resources", "cambio_delivery_log", "audit_log",
                          "dedupe_registry", "change_feed", "ingest_raw"):
                db.session.execute(db.text(f"DELETE FROM {table}"))
    _wipe()
    yield
    _wipe()


Obs = live_model("Observation")


def _body(*, unit=UNIT_A, patient=None, link="allergy.substance", value="Penicillin",
          review=True, origin="patient_generated", **fhir_extra):
    fhir = {"resourceType": "Observation", "status": "final",
            "id": str(uuid.uuid4()), "code": {"text": "Allergi mot"},
            "effectiveDateTime": "2026-11-20T08:00:00+00:00",
            "performer": [{"type": "Organization", "identifier": {"value": PROVIDER}}],
            "meta": {"tag": [{"system": "urn:pdhc:fhir:CodeSystem:data-origin",
                              "code": "patient-generated"}]},
            "extension": [{"url": "urn:pdhc:fhir:extension:data-origin",
                           "valueCode": "patient_generated"}]}
    if value is None:
        pass
    elif isinstance(value, bool):
        fhir["valueBoolean"] = value
    elif isinstance(value, (int, float)):
        fhir["valueQuantity"] = {"value": value, "unit": "st"}
    else:
        fhir["valueString"] = value
    fhir.update(fhir_extra)
    ctx = {"service_request_guid": str(uuid.uuid4()), "transaction_guid": link,
           "provider_org_guid": PROVIDER, "patient_org_guid": unit}
    if origin is not None:
        ctx["data_origin"] = origin
    if review is not None:
        ctx["review_required"] = review
    return {"patient_guid": patient or str(uuid.uuid4()), "source_type": "fhir",
            "fhir_resource": fhir, "clinical_context": ctx}


def _ingest(client, **kw):
    body = _body(**kw)
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 202, r.get_json()
    return body["fhir_resource"]["id"], body


def _row(guid):
    db.session.expire_all()
    return db.session.get(Obs, guid)


def _snapshot(guid, without=()):
    row = _row(guid)
    return {c.key: getattr(row, c.key) for c in inspect(Obs).columns
            if c.key not in without}


def _pending():
    db.session.expire_all()
    return Obs.query.filter_by(review_status="pending").count()


# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------

REVIEW_COLUMNS = ("review_status", "review_reason", "reviewed_by_user_guid",
                  "reviewed_by_name", "reviewed_at", "endorses_guid")


@pytest.mark.parametrize("fhir_name", [r[0] for r in RESOURCES])
def test_every_live_and_history_table_has_the_review_columns(fhir_name):
    for model in (live_model(fhir_name), history_model(fhir_name)):
        cols = {c.key: c for c in inspect(model).columns}
        for name in REVIEW_COLUMNS:
            assert name in cols, (model.__tablename__, name)
            assert cols[name].nullable and cols[name].default is None \
                and cols[name].server_default is None


def test_the_status_has_one_home_not_a_copy_on_clinical_context():
    assert not set(REVIEW_COLUMNS) & {c.key for c in inspect(ClinicalContext).columns}


MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"


def _migration():
    path = MIGRATIONS / "versions" / "d5e6f7a8b9c0_1044_review.py"
    spec = importlib.util.spec_from_file_location("m1044", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_is_the_single_head_on_top_of_1014():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    script = ScriptDirectory.from_config(cfg)
    # #1049 put e6f7a8b9c0d1 on top; this one stays in the single line.
    (head,) = script.get_heads()
    assert "d5e6f7a8b9c0" in {r.revision for r in script.walk_revisions(
        base="base", head=head)}
    mod = _migration()
    assert mod.down_revision == "c1d4e7f0a2b5"
    assert len(mod.revision) <= 32


def test_migration_on_a_POPULATED_table_is_additive_idempotent_and_reversible():
    """The row that was there keeps every value and reads NULL in the new
    columns; a second upgrade is a no-op; downgrade removes exactly them."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    mod = _migration()
    engine = sa.create_engine("sqlite://", poolclass=StaticPool)
    with engine.connect() as conn:
        for t in ("observation", "observation_history", "condition"):
            conn.execute(sa.text(
                f"create table {t} (guid varchar(36), value_string varchar(512), "
                f"data_origin varchar(32))"))
        conn.execute(sa.text("insert into observation values "
                             "('o-1', 'Penicillin', 'patient_generated')"))
        ops_ctx = MigrationContext.configure(conn)
        with Operations.context(ops_ctx):
            mod.upgrade()
            mod.upgrade()                       # idempotent
        insp = sa.inspect(conn)
        for t in ("observation", "observation_history", "condition"):
            names = {c["name"] for c in insp.get_columns(t)}
            assert set(REVIEW_COLUMNS) <= names, t
            assert all(c["nullable"] for c in insp.get_columns(t)
                       if c["name"] in REVIEW_COLUMNS)
        assert {i["name"] for i in insp.get_indexes("observation")} == {
            "ix_observation_review_status", "ix_observation_endorses"}
        assert not insp.get_indexes("observation_history")
        row = conn.execute(sa.text("select * from observation")).mappings().one()
        assert (row["guid"], row["value_string"], row["data_origin"]) == \
            ("o-1", "Penicillin", "patient_generated")
        assert all(row[c] is None for c in REVIEW_COLUMNS)
        with Operations.context(ops_ctx):
            mod.downgrade()
        insp = sa.inspect(conn)
        assert not set(REVIEW_COLUMNS) & {c["name"] for c in insp.get_columns("observation")}
        assert conn.execute(sa.text("select count(*) from observation")).scalar() == 1


# ---------------------------------------------------------------------------
# 2. Ingest
# ---------------------------------------------------------------------------

def test_review_required_stores_the_answer_pending_and_untouched(client):
    guid, body = _ingest(client)
    row = _row(guid)
    assert row.review_status == "pending"
    assert row.data_origin == "patient_generated"
    assert row.patient_org_guid == UNIT_A          # the gate's column
    assert row.org_guid == PROVIDER                # the performer, NOT a care unit
    assert row.value_string == "Penicillin" and row.raw_json == body["fhir_resource"]
    assert row.endorses_guid is None and row.reviewed_at is None
    raw = IngestRaw.query.filter_by(guid=row.ingest_raw_guid).one()
    assert raw.payload_json == body                # the submission, as received


def test_without_review_required_nothing_is_pending(client):
    for kw in ({"review": None}, {"review": False}):
        guid, _ = _ingest(client, **kw)
        assert _row(guid).review_status is None
    guid, _ = _ingest(client, review=None, origin=None)
    assert _row(guid).review_status is None and _row(guid).data_origin is None


@pytest.mark.parametrize("origin", [None, ""])
def test_review_required_WITHOUT_patient_generated_is_refused_by_name(client, origin):
    body = _body(origin=origin)
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 422, r.get_json()
    assert "review_required_without_patient_generated" in r.get_json()["errors"][0]
    assert IngestRaw.query.count() == 0 and Obs.query.count() == 0


@pytest.mark.parametrize("flag", ["true", 1, "yes", {"a": 1}])
def test_review_required_must_be_a_json_boolean(client, flag):
    r = client.post("/api/v1/ingest", json=_body(review=flag), headers=GATEWAY)
    assert r.status_code == 422
    assert "invalid_review_required" in r.get_json()["errors"][0]
    assert IngestRaw.query.count() == 0


def test_a_TOP_LEVEL_flag_beside_a_nested_context_is_not_dropped(client):
    """A nested context wins over flat keys; the flag must survive that."""
    body = _body(review=None)
    body["review_required"] = True
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 202
    assert _row(body["fhir_resource"]["id"]).review_status == "pending"


def test_the_flat_payload_shape_works_too(client):
    body = _body()
    body.update(body.pop("clinical_context"))
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 202
    assert _row(body["fhir_resource"]["id"]).review_status == "pending"


def test_review_required_needs_an_observation_that_will_get_a_row(client):
    body = _body()
    del body["fhir_resource"]["id"]
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 422
    assert "review_required_needs_observation" in r.get_json()["errors"][0]
    guid, first = _ingest(client)
    again = _body()
    again["fhir_resource"]["id"] = guid
    r = client.post("/api/v1/ingest", json=again, headers=GATEWAY)
    assert r.status_code == 422
    assert "review_required_guid_exists" in r.get_json()["errors"][0]
    assert _row(guid).raw_json == first["fhir_resource"]


# ---------------------------------------------------------------------------
# 3. The queue
# ---------------------------------------------------------------------------

def _guids(resp):
    assert resp.status_code == 200, resp.get_json()
    return {i["guid"] for i in resp.get_json()["items"]}


def test_list_is_scoped_to_the_callers_care_units(client):
    a, _ = _ingest(client, unit=UNIT_A)
    b, _ = _ingest(client, unit=UNIT_B)
    plain, _ = _ingest(client, review=None)          # not a review item
    assert _guids(client.get("/api/v1/reviews", headers=_bearer("doctor-a"))) == {a}
    assert _guids(client.get("/api/v1/reviews", headers=_bearer("nurse-b"))) == {b}
    r = client.get("/api/v1/reviews", headers=_bearer("admin"))
    assert _guids(r) == {a, b}
    assert all(i["can_decide"] is False for i in r.get_json()["items"])


def test_list_item_shape(client):
    guid, body = _ingest(client)
    item = client.get("/api/v1/reviews?status=pending",
                      headers=_bearer("doctor-a")).get_json()
    assert item["total"] == 1
    it = item["items"][0]
    for key in ("guid", "patient_guid", "section", "link_id", "label", "value",
                "unit", "recorded_at", "submitted_at", "review_status"):
        assert key in it, key
    assert (it["guid"], it["section"], it["link_id"], it["label"], it["value"],
            it["review_status"], it["can_decide"]) == (
        guid, "allergy", "allergy.substance", "Allergi mot", "Penicillin",
        "pending", True)
    assert it["patient_guid"] == body["patient_guid"]


def test_list_filters_by_patient_and_a_foreign_patient_shows_nothing(client):
    a, body_a = _ingest(client, unit=UNIT_A)
    _b, body_b = _ingest(client, unit=UNIT_B)
    h = _bearer("doctor-a")
    assert _guids(client.get(f"/api/v1/reviews?patient_guid={body_a['patient_guid']}",
                             headers=h)) == {a}
    r = client.get(f"/api/v1/reviews?patient_guid={body_b['patient_guid']}", headers=h)
    assert r.get_json() == {"items": [], "total": 0, "limit": 200, "truncated": False}


@pytest.mark.parametrize("who,status,error", [
    (None, 401, "not_authenticated"),
    ("no-such-token", 401, "not_authenticated"),
    ("patient", 403, "not_professional"),
    ("patient-forged", 403, "not_professional"),
    ("mustchange", 403, "must_change_password"),
    ("othercare-a", 403, "no_review_scope"),
])
def test_list_refusals(client, who, status, error):
    _ingest(client)
    r = client.get("/api/v1/reviews", headers=_bearer(who) if who else {})
    assert r.status_code == status
    assert r.get_json()["error"] == error
    assert "items" not in r.get_json()


@pytest.mark.parametrize("headers", [GATEWAY, SIM, PATIENTAPP])
def test_no_service_key_opens_the_professional_routes(client, headers):
    guid, _ = _ingest(client)
    assert client.get("/api/v1/reviews", headers=headers).status_code == 401
    r = client.post(f"/api/v1/reviews/{guid}/approve", json={}, headers=headers)
    assert r.status_code == 401
    r = client.post(f"/api/v1/reviews/{guid}/reject", json={"reason": "x"},
                    headers=headers)
    assert r.status_code == 401
    assert _row(guid).review_status == "pending"


# ---------------------------------------------------------------------------
# 4. Approve
# ---------------------------------------------------------------------------

def _endorsement(original_guid):
    db.session.expire_all()
    return Obs.query.filter_by(endorses_guid=original_guid).all()


def test_an_affiliated_doctor_approves_and_a_NEW_row_is_written(client):
    guid, body = _ingest(client)
    before = _snapshot(guid, without=rv.FLIP_COLUMNS)

    r = client.post(f"/api/v1/reviews/{guid}/approve", json={"note": "Bekräftat"},
                    headers=_bearer("doctor-a"))
    assert r.status_code == 201, r.get_json()
    out = r.get_json()
    assert out["amended"] is False

    # the original: only the review columns moved
    assert _snapshot(guid, without=rv.FLIP_COLUMNS) == before
    orig = _row(guid)
    doc = BLOBS["doctor-a"]
    assert (orig.review_status, orig.reviewed_by_user_guid, orig.reviewed_by_name) == \
        ("endorsed", doc["user_guid"], "Doctor A")
    assert orig.reviewed_at is not None and orig.review_reason is None
    assert orig.data_origin == "patient_generated" and orig.endorses_guid is None

    # the endorsement: a new row, care's, pointing at the original
    (end,) = _endorsement(guid)
    assert end.guid == out["endorsement_guid"] != guid
    assert end.data_origin is None and end.review_status is None
    assert (end.patient_guid, end.patient_org_guid, end.code_canonical,
            end.transaction_guid, end.value_string) == (
        orig.patient_guid, UNIT_A, orig.code_canonical, "allergy.substance",
        "Penicillin")
    assert end.effective_at == orig.effective_at
    assert end.org_guid == UNIT_A                       # the endorsing care unit
    assert (end.reviewed_by_user_guid, end.reviewed_by_name) == \
        (doc["user_guid"], "Doctor A")
    assert end.source == rv.REVIEW_SOURCE
    fhir = end.raw_json
    assert fhir["id"] == end.guid
    assert fhir["derivedFrom"][0]["identifier"]["value"] == guid
    assert fhir["performer"][1]["identifier"]["value"] == doc["user_guid"]
    assert fhir["note"][0]["text"] == "Bekräftat"
    assert "extension" not in fhir and "tag" not in (fhir.get("meta") or {})

    # its provenance, and the decision in the audit log
    ctx = ClinicalContext.query.filter_by(ingest_raw_guid=end.ingest_raw_guid).one()
    assert ctx.data_origin is None and ctx.author_org_guid == UNIT_A
    assert ctx.service_request_guid == body["clinical_context"]["service_request_guid"]
    audit = AuditLog.query.filter_by(event_type="review.approved").one()
    assert audit.actor_guid == doc["user_guid"]
    assert audit.data_subject_guid == orig.patient_guid
    assert audit.payload_snapshot["original_guid"] == guid
    assert audit.payload_snapshot["endorsement_guid"] == end.guid


def test_a_nurse_may_approve_too(client):
    guid, _ = _ingest(client)
    r = client.post(f"/api/v1/reviews/{guid}/approve", headers=_bearer("nurse-a"))
    assert r.status_code == 201


def test_amend_and_approve_changes_the_endorsement_never_the_original(client):
    guid, body = _ingest(client)
    before = _snapshot(guid, without=rv.FLIP_COLUMNS)
    r = client.post(f"/api/v1/reviews/{guid}/approve",
                    json={"value": "Penicillin V"}, headers=_bearer("doctor-a"))
    assert r.status_code == 201 and r.get_json()["amended"] is True
    assert _snapshot(guid, without=rv.FLIP_COLUMNS) == before
    assert _row(guid).value_string == "Penicillin"
    assert _row(guid).raw_json == body["fhir_resource"]
    (end,) = _endorsement(guid)
    assert end.value_string == "Penicillin V"
    assert end.raw_json["valueString"] == "Penicillin V"
    assert AuditLog.query.filter_by(event_type="review.approved").one() \
        .payload_snapshot["amended"] is True


def test_the_same_value_is_not_an_amendment(client):
    guid, _ = _ingest(client, value=3)
    r = client.post(f"/api/v1/reviews/{guid}/approve", json={"value": 3.0},
                    headers=_bearer("doctor-a"))
    assert r.status_code == 201 and r.get_json()["amended"] is False


@pytest.mark.parametrize("original,amended,code", [
    ("Penicillin", 5, "required"),
    ("Penicillin", "   ", "required"),
    ("Penicillin", "x" * 513, "out_of_range"),
    (3, "three", "wrong_type"),
    (3, True, "wrong_type"),
    (3, 10 ** 13, "out_of_range"),
    (True, "ja", "wrong_type"),
])
def test_an_amended_value_is_validated_like_an_ingested_one(client, original,
                                                           amended, code):
    guid, _ = _ingest(client, value=original)
    r = client.post(f"/api/v1/reviews/{guid}/approve", json={"value": amended},
                    headers=_bearer("doctor-a"))
    assert r.status_code == 422, r.get_json()
    assert (r.get_json()["error"], r.get_json()["code"]) == ("validation", code)
    assert _row(guid).review_status == "pending" and not _endorsement(guid)
    assert IngestRaw.query.count() == 1


def test_an_amended_number_lands_as_a_number(client):
    guid, _ = _ingest(client, value=3)
    r = client.post(f"/api/v1/reviews/{guid}/approve", json={"value": 4},
                    headers=_bearer("doctor-a"))
    assert r.status_code == 201
    (end,) = _endorsement(guid)
    assert float(end.value_quantity) == 4.0 and end.value_unit == "st"
    assert float(_row(guid).value_quantity) == 3.0


def test_approval_is_ONE_transaction(client, monkeypatch):
    """If the endorsement cannot be written, the original is not flipped."""
    from app.services import ingest_pipeline
    guid, _ = _ingest(client)

    def boom(*a, **k):
        raise RuntimeError("endorsement row could not be built")
    monkeypatch.setattr(ingest_pipeline, "build_live_observation_row", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/api/v1/reviews/{guid}/approve", headers=_bearer("doctor-a"))
    db.session.rollback()
    assert _row(guid).review_status == "pending"
    assert _row(guid).reviewed_by_user_guid is None
    assert not _endorsement(guid)
    assert IngestRaw.query.count() == 1
    assert AuditLog.query.filter_by(event_type="review.approved").count() == 0


# ---------------------------------------------------------------------------
# 5. Who may NOT decide
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action,payload", [("approve", {}),
                                            ("reject", {"reason": "fel"})])
def test_an_UNAFFILIATED_professional_cannot_decide_and_learns_nothing(
        client, action, payload):
    guid, body = _ingest(client, unit=UNIT_A)
    r = client.post(f"/api/v1/reviews/{guid}/{action}", json=payload,
                    headers=_bearer("nurse-b"))
    assert r.status_code == 403
    # the same answer as for a guid that does not exist at all
    ghost = client.post(f"/api/v1/reviews/{uuid.uuid4()}/{action}", json=payload,
                        headers=_bearer("nurse-b"))
    assert ghost.status_code == 403 and ghost.get_json() == r.get_json()
    text = r.get_data(as_text=True)
    for secret in (body["patient_guid"], "Penicillin", UNIT_A, "pending"):
        assert secret not in text
    assert _row(guid).review_status == "pending" and not _endorsement(guid)
    denied = AuditLog.query.filter_by(event_type="review.denied").all()
    assert {d.payload_snapshot["why"] for d in denied} == {"not_affiliated", "not_found"}
    assert all(d.data_subject_guid is None for d in denied)


def test_an_unaffiliated_caller_cannot_tell_decided_from_pending(client):
    """`already_decided` is information; it is given only inside the scope."""
    guid, _ = _ingest(client, unit=UNIT_A)
    assert client.post(f"/api/v1/reviews/{guid}/approve",
                       headers=_bearer("doctor-a")).status_code == 201
    r = client.post(f"/api/v1/reviews/{guid}/approve", headers=_bearer("nurse-b"))
    assert r.status_code == 403 and r.get_json()["error"] == "forbidden"


@pytest.mark.parametrize("action,payload", [("approve", {}),
                                            ("reject", {"reason": "fel"})])
def test_an_ADMIN_without_affiliation_may_list_but_not_decide(client, action, payload):
    guid, _ = _ingest(client, unit=UNIT_A)
    assert guid in _guids(client.get("/api/v1/reviews", headers=_bearer("admin")))
    r = client.post(f"/api/v1/reviews/{guid}/{action}", json=payload,
                    headers=_bearer("admin"))
    assert r.status_code == 403
    assert _row(guid).review_status == "pending" and not _endorsement(guid)


def test_an_admin_who_IS_affiliated_may_decide(client):
    guid, _ = _ingest(client, unit=UNIT_A)
    r = client.post(f"/api/v1/reviews/{guid}/approve", headers=_bearer("admin-doctor-a"))
    assert r.status_code == 201


def test_an_affiliation_with_another_role_cannot_decide(client):
    guid, _ = _ingest(client, unit=UNIT_A)
    r = client.post(f"/api/v1/reviews/{guid}/approve", headers=_bearer("othercare-a"))
    assert r.status_code == 403
    assert _row(guid).review_status == "pending"


@pytest.mark.parametrize("who", ["patient", "patient-forged"])
@pytest.mark.parametrize("action,payload", [("approve", {}),
                                            ("reject", {"reason": "fel"})])
def test_a_PATIENT_cannot_decide(client, who, action, payload):
    guid, _ = _ingest(client, unit=UNIT_A)
    r = client.post(f"/api/v1/reviews/{guid}/{action}", json=payload,
                    headers=_bearer(who))
    assert r.status_code == 403 and r.get_json()["error"] == "not_professional"
    assert _row(guid).review_status == "pending" and not _endorsement(guid)


def test_a_patient_blob_has_no_review_units_whatever_it_carries():
    assert rv.review_units(BLOBS["patient-forged"]) == {}
    assert rv.may_decide(BLOBS["patient-forged"], UNIT_A) is None


def test_must_change_password_blocks_a_decision(client):
    guid, _ = _ingest(client)
    r = client.post(f"/api/v1/reviews/{guid}/approve", headers=_bearer("mustchange"))
    assert r.status_code == 403 and r.get_json()["error"] == "must_change_password"
    assert _row(guid).review_status == "pending"


def test_a_row_with_NO_patient_organisation_is_decided_by_nobody(client):
    """Unknown organisation is not 'any organisation' (Rule 24)."""
    guid, _ = _ingest(client, unit=None)
    assert _row(guid).patient_org_guid is None
    for who in ("doctor-a", "admin", "admin-doctor-a"):
        r = client.post(f"/api/v1/reviews/{guid}/approve", headers=_bearer(who))
        assert r.status_code == 403, who
    assert guid not in _guids(client.get("/api/v1/reviews", headers=_bearer("doctor-a")))
    assert guid in _guids(client.get("/api/v1/reviews", headers=_bearer("admin")))
    assert _row(guid).review_status == "pending"


def test_a_row_that_is_not_a_review_item_cannot_be_decided(client):
    guid, _ = _ingest(client, review=None)
    r = client.post(f"/api/v1/reviews/{guid}/approve", headers=_bearer("doctor-a"))
    assert r.status_code == 403
    assert _row(guid).review_status is None and not _endorsement(guid)


# ---------------------------------------------------------------------------
# 6. Once
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("first,second", [
    ("approve", "approve"), ("approve", "reject"),
    ("reject", "approve"), ("reject", "reject")])
def test_a_second_decision_is_409(client, first, second):
    guid, _ = _ingest(client)
    payload = {"approve": {}, "reject": {"reason": "stämmer inte"}}
    h = _bearer("doctor-a")
    r1 = client.post(f"/api/v1/reviews/{guid}/{first}", json=payload[first], headers=h)
    assert r1.status_code in (200, 201)
    after_first = _snapshot(guid)
    r2 = client.post(f"/api/v1/reviews/{guid}/{second}", json=payload[second],
                     headers=_bearer("nurse-a"))
    assert r2.status_code == 409 and r2.get_json()["error"] == "already_decided"
    assert _snapshot(guid) == after_first
    assert len(_endorsement(guid)) == (1 if first == "approve" else 0)


def test_the_flip_itself_refuses_a_row_that_is_no_longer_pending(client):
    """The route's 409 check and the conditional UPDATE are two layers; this
    is the second one, which is what holds under two simultaneous requests."""
    guid, _ = _ingest(client)
    assert client.post(f"/api/v1/reviews/{guid}/reject", json={"reason": "nej"},
                       headers=_bearer("doctor-a")).status_code == 200
    stale = _row(guid)
    actor = {"user_guid": "u", "name": "n"}
    with pytest.raises(rv.AlreadyDecided):
        rv.approve(stale, actor=actor, unit=_aff(UNIT_A, "doctor"))
    assert _row(guid).review_status == "rejected" and not _endorsement(guid)


# ---------------------------------------------------------------------------
# 7. Reject
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [None, {}, {"reason": ""}, {"reason": "   "},
                                     {"reason": 7}])
def test_reject_without_a_reason_is_422(client, payload):
    guid, _ = _ingest(client)
    r = client.post(f"/api/v1/reviews/{guid}/reject", json=payload,
                    headers=_bearer("doctor-a"))
    assert r.status_code == 422 and r.get_json()["error"] == "reason_required"
    assert _row(guid).review_status == "pending"


def test_reject_stores_the_reason_and_writes_no_endorsement(client):
    guid, _ = _ingest(client)
    before = _snapshot(guid, without=rv.FLIP_COLUMNS)
    r = client.post(f"/api/v1/reviews/{guid}/reject",
                    json={"reason": " Stämmer inte med journalen "},
                    headers=_bearer("nurse-a"))
    assert r.status_code == 200 and r.get_json()["review_status"] == "rejected"
    assert _snapshot(guid, without=rv.FLIP_COLUMNS) == before
    row = _row(guid)
    assert (row.review_status, row.review_reason, row.reviewed_by_name) == \
        ("rejected", "Stämmer inte med journalen", "Nurse A")
    assert not _endorsement(guid) and Obs.query.count() == 1
    audit = AuditLog.query.filter_by(event_type="review.rejected").one()
    assert audit.payload_snapshot["original_guid"] == guid
    assert guid in _guids(client.get("/api/v1/reviews?status=rejected",
                                     headers=_bearer("doctor-a")))
    assert guid not in _guids(client.get("/api/v1/reviews", headers=_bearer("doctor-a")))


# ---------------------------------------------------------------------------
# 8. The generic FHIR write path cannot rewrite either row
# ---------------------------------------------------------------------------

def test_a_FHIR_PUT_cannot_overwrite_an_original_or_an_endorsement(client):
    guid, body = _ingest(client)
    forged = dict(body["fhir_resource"], valueString="Inget",
                  subject={"reference": f"Patient/{body['patient_guid']}"})
    r = client.put(f"/api/v1/fhir/Observation/{guid}", json=forged, headers=SIM)
    assert r.status_code == 409, r.get_json()
    assert _row(guid).value_string == "Penicillin" and _row(guid).version_id == 1

    out = client.post(f"/api/v1/reviews/{guid}/approve",
                      headers=_bearer("doctor-a")).get_json()
    end = out["endorsement_guid"]
    r = client.put(f"/api/v1/fhir/Observation/{end}",
                   json=dict(forged, id=end), headers=SIM)
    assert r.status_code == 409
    assert _row(end).value_string == "Penicillin"


# ---------------------------------------------------------------------------
# 9. The patient app's one route
# ---------------------------------------------------------------------------

def test_status_route_refuses_without_or_with_the_wrong_key(client, app, monkeypatch):
    _guid, body = _ingest(client)
    url = f"/api/v1/reviews/by-patient/{body['patient_guid']}"
    for headers in ({}, {"X-Source-Service": "patientapp.pdhc"},
                    {"X-Service-Key": PATIENTAPP_KEY},
                    dict(PATIENTAPP, **{"X-Service-Key": "wrong"}),
                    dict(PATIENTAPP, **{"X-Source-Service": "other.pdhc"}),
                    SIM, GATEWAY,                       # valid keys, wrong door
                    _bearer("doctor-a"), _bearer("admin")):
        r = client.get(url, headers=headers)
        assert r.status_code == 401, headers
        assert "items" not in r.get_json()
    # an UNSET key admits nobody, whatever is sent
    monkeypatch.setitem(app.config, "PATIENTAPP_PDHC_SERVICE_KEY", "")
    for key in (PATIENTAPP_KEY, "", " "):
        r = client.get(url, headers=dict(PATIENTAPP, **{"X-Service-Key": key}))
        assert r.status_code == 401


def test_status_route_shows_the_patient_what_became_of_their_answers(client):
    patient = str(uuid.uuid4())
    pending, _ = _ingest(client, patient=patient, link="medication.name", value="Alvedon")
    endorsed, _ = _ingest(client, patient=patient)
    rejected, _ = _ingest(client, patient=patient, link="history.year", value=1999)
    _ingest(client, patient=patient, review=None)       # not a review item
    _ingest(client)                                     # someone else's
    h = _bearer("doctor-a")
    client.post(f"/api/v1/reviews/{endorsed}/approve", json={"value": "Pc V"}, headers=h)
    client.post(f"/api/v1/reviews/{rejected}/reject", json={"reason": "Fel år"}, headers=h)

    r = client.get(f"/api/v1/reviews/by-patient/{patient}", headers=PATIENTAPP)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    items = {i["original_guid"]: i for i in r.get_json()["items"]}
    assert set(items) == {pending, endorsed, rejected}
    for it in items.values():
        assert set(it) == {"original_guid", "section", "summary", "status",
                           "reason", "decided_at"}
    assert (items[pending]["section"], items[pending]["summary"],
            items[pending]["status"], items[pending]["decided_at"]) == \
        ("medication", "Alvedon", "pending", None)
    assert (items[endorsed]["status"], items[endorsed]["reason"]) == ("endorsed", None)
    assert items[endorsed]["summary"] == "Penicillin"   # their answer, not care's
    assert items[endorsed]["decided_at"]
    assert (items[rejected]["status"], items[rejected]["reason"],
            items[rejected]["summary"]) == ("rejected", "Fel år", "1999 st")
    assert "Doctor A" not in r.get_data(as_text=True)
    assert "Pc V" not in r.get_data(as_text=True)

    other = client.get(f"/api/v1/reviews/by-patient/{uuid.uuid4()}", headers=PATIENTAPP)
    assert other.status_code == 200 and other.get_json() == {"items": []}


def test_status_route_shows_a_coded_answer_in_words(client):
    """#1044: the stored value of a coded answer is its code (`has`); the
    patient's status list shows the display that came with it."""
    patient = str(uuid.uuid4())
    coded, _ = _ingest(client, patient=patient, link="allergy.status",
                       value=None, valueCodeableConcept={
                           "coding": [{"code": "has", "display": "Ja, jag har allergier"}],
                           "text": "Ja, jag har allergier"})
    bare, _ = _ingest(client, patient=patient, link="vaccination.status",
                      value=None, valueCodeableConcept={"coding": [{"code": "none"}]})
    r = client.get(f"/api/v1/reviews/by-patient/{patient}", headers=PATIENTAPP)
    items = {i["original_guid"]: i["summary"] for i in r.get_json()["items"]}
    assert items[coded] == "Ja, jag har allergier"
    assert items[bare] == "none"        # no display sent: the code, never empty


def test_the_right_key_opens_NO_other_route(client):
    guid, body = _ingest(client)
    patient = body["patient_guid"]
    assert client.get(f"/api/v1/reviews/by-patient/{patient}",
                      headers=PATIENTAPP).status_code == 200      # the control
    probes = [
        ("get", "/api/v1/reviews", None),
        ("post", f"/api/v1/reviews/{guid}/approve", {}),
        ("post", f"/api/v1/reviews/{guid}/reject", {"reason": "x"}),
        ("post", f"/api/v1/reviews/by-patient/{patient}", {}),
        ("put", f"/api/v1/reviews/by-patient/{patient}", {}),
        ("delete", f"/api/v1/reviews/by-patient/{patient}", None),
        ("get", "/api/v1/fhir/Observation", None),
        ("get", f"/api/v1/fhir/Observation/{guid}", None),
        ("put", f"/api/v1/fhir/Observation/{guid}", body["fhir_resource"]),
        ("post", "/api/v1/fhir/Observation", body["fhir_resource"]),
        ("post", "/api/v1/ingest", _body()),
        ("get", f"/api/v1/observations/{guid}/provenance", None),
        ("get", "/api/v1/clinical/patients", None),
        ("get", f"/api/v1/clinical/patient/{patient}/series", None),
        ("get", "/api/v1/stats", None),
    ]
    for method, url, payload in probes:
        r = getattr(client, method)(url, json=payload, headers=dict(
            PATIENTAPP, **{"X-Access-Purpose": "care-delivery"}))
        assert r.status_code in (401, 403, 405), (method, url, r.status_code)
    assert _row(guid).review_status == "pending" and Obs.query.count() == 1


def test_patientapp_is_in_neither_of_the_other_allowlists():
    from app.auth import KNOWN_FHIR_SERVICES
    from app.api.auth import KNOWN_SERVICES
    from app.api.reviews import PATIENT_STATUS_SERVICES
    assert PATIENT_STATUS_SERVICES == {"patientapp.pdhc": "PATIENTAPP_PDHC_SERVICE_KEY"}
    assert "patientapp.pdhc" not in KNOWN_FHIR_SERVICES
    assert "patientapp.pdhc" not in KNOWN_SERVICES
    assert "PATIENTAPP_PDHC_SERVICE_KEY" not in KNOWN_FHIR_SERVICES.values()
    assert "PATIENTAPP_PDHC_SERVICE_KEY" not in KNOWN_SERVICES.values()


# ---------------------------------------------------------------------------
# 10. What readers and the EPS derivation see
# ---------------------------------------------------------------------------

DASH = {"X-Source-Service": "dashboard.pdhc", "X-Service-Key": "test-dash-1044",
        "X-Access-Purpose": "care-delivery", "X-Is-Admin": "1",
        "X-Admin-Read-Reason": "test"}


def _own_app(**cfg):
    """An app of its own (own in-memory database). Needed where the shared
    app's test-only header shim (conftest) would replace the service blob,
    and for the analysis role."""
    return create_app(dict({
        "TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"check_same_thread": False},
                                      "poolclass": StaticPool},
        "AUTH_MODE": "sso", "CAMBIO_DELIVERY_ENABLED": False,
        "GATEWAY_PDHC_SERVICE_KEY": "test-gateway-key",
        "DASHBOARD_PDHC_SERVICE_KEY": "test-dash-1044",
        "PATIENTAPP_PDHC_SERVICE_KEY": PATIENTAPP_KEY,
    }, **cfg))


@pytest.fixture
def clinical_app():
    app = _own_app()
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_clinical_reads_tell_original_from_endorsement(clinical_app):
    client = clinical_app.test_client()
    patient = str(uuid.uuid4())
    guid, _ = _ingest(client, patient=patient)
    plain, _ = _ingest(client, patient=patient, review=None, origin=None, value="x")
    end = client.post(f"/api/v1/reviews/{guid}/approve",
                      headers=_bearer("doctor-a")).get_json()["endorsement_guid"]

    r = client.get(f"/api/v1/clinical/patient/{patient}/series", headers=DASH)
    assert r.status_code == 200, r.get_json()
    pts = {p["observation_guid"]: p for p in r.get_json()["points"]}
    assert set(pts) == {guid, plain, end}
    assert (pts[guid]["data_origin"], pts[guid]["review_status"],
            pts[guid]["endorses_guid"]) == ("patient_generated", "endorsed", None)
    assert (pts[end]["data_origin"], pts[end]["review_status"],
            pts[end]["endorses_guid"], pts[end]["reviewed_by_name"]) == \
        (None, None, guid, "Doctor A")
    assert pts[end]["reviewed_by_user_guid"] == BLOBS["doctor-a"]["user_guid"]
    assert pts[end]["reviewed_at"]
    for key in ("review_status", "endorses_guid", "reviewed_by_user_guid",
                "reviewed_by_name", "reviewed_at"):
        assert key in pts[plain] and pts[plain][key] is None

    d = client.get(f"/api/v1/clinical/observation/{end}", headers=DASH).get_json()
    assert d["review"]["endorses_guid"] == guid
    assert d["review"]["reviewed_by_name"] == "Doctor A"
    assert d["context"]["data_origin"] is None and d["context_found"] is True
    d = client.get(f"/api/v1/clinical/observation/{guid}", headers=DASH).get_json()
    assert d["review"]["review_status"] == "endorsed"
    assert d["context"]["data_origin"] == "patient_generated"


def test_the_eps_export_holds_ONLY_endorsement_rows(client):
    patient = str(uuid.uuid4())
    endorsed, _ = _ingest(client, patient=patient)
    _ingest(client, patient=patient, link="medication.name", value="Alvedon")  # pending
    rejected, _ = _ingest(client, patient=patient, link="history.year", value=1999)
    _ingest(client, patient=patient, review=None)       # patient data, no review
    _ingest(client, patient=patient, review=None, origin=None)   # care's own row
    h = _bearer("doctor-a")
    end = client.post(f"/api/v1/reviews/{endorsed}/approve",
                      headers=h).get_json()["endorsement_guid"]
    client.post(f"/api/v1/reviews/{rejected}/reject", json={"reason": "nej"}, headers=h)

    rows = rv.export_endorsements(patient)
    assert [r["guid"] for r in rows] == [end]
    row = rows[0]
    assert row["endorses_guid"] == endorsed and row["data_origin"] is None
    # the shape eps.pdhc reads (contract Deviations, [ips])
    for key in ("cdr", "guid", "patient_guid", "link_id", "value", "data_origin",
                "endorses_guid", "reviewed_by_user_guid", "reviewed_by_name",
                "reviewed_at", "author_org_guid", "group", "review_status"):
        assert key in row, key
    assert (row["reviewed_by_user_guid"], row["reviewed_by_name"],
            row["author_org_guid"], row["section"], row["link_id"], row["value"]) == (
        BLOBS["doctor-a"]["user_guid"], "Doctor A", UNIT_A, "allergy",
        "allergy.substance", "Penicillin")
    assert row["reviewed_at"] and row["raw_json"]["id"] == end
    assert row["review_status"] is None and row["group"] is None
    assert rv.export_endorsements(str(uuid.uuid4())) == []
    assert [r["guid"] for r in rv.export_endorsements()] == [end]

    # a row carrying BOTH marks is left out, not trusted
    e = _row(end)
    e.data_origin = "patient_generated"
    db.session.commit()
    assert rv.export_endorsements(patient) == []


def test_the_export_cli(client, app):
    import json
    guid, body = _ingest(client)
    client.post(f"/api/v1/reviews/{guid}/approve", headers=_bearer("doctor-a"))
    res = app.test_cli_runner().invoke(
        args=["export-eps-endorsements", "--patient", body["patient_guid"]])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)
    assert len(rows) == 1 and rows[0]["endorses_guid"] == guid


# ---------------------------------------------------------------------------
# 11. The analysis repositories (cdr2/cdr3: CDR_CLINICAL_READ=false)
# ---------------------------------------------------------------------------

@pytest.fixture
def analysis_app():
    app = _own_app(CDR_CLINICAL_READ=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_an_ANALYSIS_cdr_has_no_review_surface_at_all(analysis_app):
    c = analysis_app.test_client()
    rules = {r.rule for r in analysis_app.url_map.iter_rules()}
    assert not any("/reviews" in r for r in rules)
    g = str(uuid.uuid4())
    for method, url, headers in (
            ("get", "/api/v1/reviews", _bearer("doctor-a")),
            ("post", f"/api/v1/reviews/{g}/approve", _bearer("doctor-a")),
            ("post", f"/api/v1/reviews/{g}/reject", _bearer("doctor-a")),
            ("get", f"/api/v1/reviews/by-patient/{g}", PATIENTAPP)):
        assert getattr(c, method)(url, headers=headers).status_code == 404, url


def test_an_ANALYSIS_cdr_refuses_review_required_by_name_and_takes_the_rest(analysis_app):
    c = analysis_app.test_client()
    r = c.post("/api/v1/ingest", json=_body(), headers=GATEWAY)
    assert r.status_code == 422
    assert "review_not_available_here" in r.get_json()["errors"][0]
    assert IngestRaw.query.count() == 0
    body = _body(review=None)                 # #1014 data without the flag: as before
    assert c.post("/api/v1/ingest", json=body, headers=GATEWAY).status_code == 202
    row = db.session.get(Obs, body["fhir_resource"]["id"])
    assert row.data_origin == "patient_generated" and row.review_status is None


# ---------------------------------------------------------------------------
# 12. The repeating-group instance travels with the answer
# ---------------------------------------------------------------------------

def test_group_is_kept_from_ingest_to_queue_to_endorsement_to_export(client):
    patient = str(uuid.uuid4())
    body = _body(patient=patient)
    body["clinical_context"]["group"] = "allergy.entry#2"
    body["clinical_context"]["resolved_context_json"] = {"kept": 1}
    assert client.post("/api/v1/ingest", json=body, headers=GATEWAY).status_code == 202
    guid = body["fhir_resource"]["id"]
    ctx = ClinicalContext.query.filter_by(
        ingest_raw_guid=_row(guid).ingest_raw_guid).one()
    assert ctx.resolved_context_json == {"kept": 1, "group": "allergy.entry#2"}

    h = _bearer("doctor-a")
    (item,) = client.get("/api/v1/reviews", headers=h).get_json()["items"]
    assert item["group"] == "allergy.entry#2"
    client.post(f"/api/v1/reviews/{guid}/approve", headers=h)
    (row,) = rv.export_endorsements(patient)
    assert row["group"] == "allergy.entry#2"


@pytest.mark.parametrize("group", [["a"], {"a": 1}, True, "x" * 65])
def test_an_unusable_group_is_refused_by_name(client, group):
    body = _body()
    body["clinical_context"]["group"] = group
    r = client.post("/api/v1/ingest", json=body, headers=GATEWAY)
    assert r.status_code == 422 and "invalid_group" in r.get_json()["errors"][0]
    assert IngestRaw.query.count() == 0


# ---------------------------------------------------------------------------
# 13. The care-delivery read scopes on the PATIENT's care unit (#1044)
# ---------------------------------------------------------------------------
#
# /api/v1/clinical/* is reached by dashboard.pdhc with its service key and
# the operator's affiliation care units in X-Org-Guids. Until this change it
# compared those to `org_guid` (the performer) only, so a patient-app answer
# (performer = the patient app's organisation) was invisible to the treating
# clinician. Own app: the shared one's header shim would replace the blob.

def _dash(*units, admin=False):
    h = {"X-Source-Service": "dashboard.pdhc", "X-Service-Key": "test-dash-1044",
         "X-Access-Purpose": "care-delivery"}
    if admin:
        h.update({"X-Is-Admin": "1", "X-Admin-Read-Reason": "test"})
    elif units:
        h["X-Org-Guids"] = ",".join(units)
    return h


def _series(client, patient, headers):
    r = client.get(f"/api/v1/clinical/patient/{patient}/series", headers=headers)
    assert r.status_code == 200, r.get_json()
    return {p["observation_guid"]: p for p in r.get_json()["points"]}


def _patients(client, headers):
    r = client.get("/api/v1/clinical/patients", headers=headers)
    assert r.status_code == 200, r.get_json()
    return {p["patient_guid"]: p for p in r.get_json()["patients"]}


@pytest.fixture
def seen(clinical_app):
    """One patient listed at UNIT_A: an endorsed patient-app answer (original
    + endorsement) and a pending one."""
    client = clinical_app.test_client()
    patient = str(uuid.uuid4())
    original, _ = _ingest(client, patient=patient)
    pending, _ = _ingest(client, patient=patient, link="medication.name",
                         value="Alvedon")
    end = client.post(f"/api/v1/reviews/{original}/approve",
                      headers=_bearer("doctor-a")).get_json()["endorsement_guid"]
    return client, patient, original, pending, end


def test_an_AFFILIATED_non_admin_sees_the_original_and_the_endorsement(seen):
    client, patient, original, pending, end = seen
    assert _row(original).org_guid == PROVIDER != UNIT_A   # performer is not the unit
    pts = _series(client, patient, _dash(UNIT_A))
    assert set(pts) == {original, pending, end}
    assert (pts[original]["data_origin"], pts[original]["review_status"],
            pts[original]["endorses_guid"]) == ("patient_generated", "endorsed", None)
    assert (pts[pending]["data_origin"], pts[pending]["review_status"]) == \
        ("patient_generated", "pending")
    assert (pts[end]["data_origin"], pts[end]["review_status"],
            pts[end]["endorses_guid"]) == (None, None, original)

    d = client.get(f"/api/v1/clinical/observation/{original}", headers=_dash(UNIT_A))
    assert d.status_code == 200
    d = d.get_json()
    assert d["context"]["data_origin"] == "patient_generated"
    assert d["review"]["review_status"] == "endorsed"
    assert d["review"]["reviewed_by_name"] == "Doctor A"
    assert d["value_string"] == "Penicillin"                # what the patient wrote
    d = client.get(f"/api/v1/clinical/observation/{end}", headers=_dash(UNIT_A)).get_json()
    assert d["review"]["endorses_guid"] == original and d["context"]["data_origin"] is None

    assert _patients(client, _dash(UNIT_A))[patient]["observation_count"] == 3
    s = client.get(f"/api/v1/clinical/patient/{patient}/summary",
                   headers=_dash(UNIT_A)).get_json()
    assert sum(p["count"] for p in s["parameters"]) == 3


def test_a_professional_affiliated_ELSEWHERE_sees_neither(seen):
    client, patient, original, pending, end = seen
    h = _dash(UNIT_B)
    assert _series(client, patient, h) == {}
    assert patient not in _patients(client, h)
    for guid in (original, pending, end):
        r = client.get(f"/api/v1/clinical/observation/{guid}", headers=h)
        assert r.status_code == 404 and r.get_json()["error"] == "not_found"
    s = client.get(f"/api/v1/clinical/patient/{patient}/summary", headers=h).get_json()
    assert s["parameters"] == []
    # and no scope at all is still nothing
    assert _series(client, patient, _dash()) == {}


def test_a_row_with_NO_patient_organisation_stays_out_of_the_care_unit_scope(clinical_app):
    """Unknown organisation is not 'my unit'. Such a row is reachable as
    before this change and no further: through its performer, or as admin."""
    client = clinical_app.test_client()
    patient = str(uuid.uuid4())
    guid, _ = _ingest(client, patient=patient, unit=None)
    assert _row(guid).patient_org_guid is None
    assert _series(client, patient, _dash(UNIT_A)) == {}
    assert _series(client, patient, _dash(UNIT_A, UNIT_B)) == {}
    assert set(_series(client, patient, _dash(PROVIDER))) == {guid}     # as before
    assert set(_series(client, patient, _dash(admin=True))) == {guid}


def test_what_was_visible_through_the_performer_is_still_visible(clinical_app):
    """The legacy clause: ordinary rows reached via org_guid keep being
    reached, whether or not they record a patient organisation."""
    client = clinical_app.test_client()
    patient = str(uuid.uuid4())
    for guid, patient_org in (("legacy-null", None), ("legacy-other", UNIT_B)):
        db.session.add(Obs(guid=guid, patient_guid=patient, org_guid=UNIT_A,
                           patient_org_guid=patient_org, raw_json={},
                           code_canonical="sys|x", status="final"))
    db.session.commit()
    assert set(_series(client, patient, _dash(UNIT_A))) == {"legacy-null", "legacy-other"}
    assert set(_series(client, patient, _dash(UNIT_B))) == {"legacy-other"}
    assert _series(client, patient, _dash(PROVIDER)) == {}


def test_nothing_changes_for_an_admin(seen):
    client, patient, original, pending, end = seen
    other = str(uuid.uuid4())
    b, _ = _ingest(client, patient=other, unit=UNIT_B)
    h = _dash(admin=True)
    assert set(_series(client, patient, h)) == {original, pending, end}
    assert set(_series(client, other, h)) == {b}
    assert set(_patients(client, h)) == {patient, other}
    # the attestation reason is still required for an admin-override read
    bare = dict(h)
    del bare["X-Admin-Read-Reason"]
    assert client.get("/api/v1/clinical/patients", headers=bare).status_code == 428


def test_the_care_delivery_door_itself_is_unchanged(seen):
    client, patient, *_ = seen
    url = f"/api/v1/clinical/patient/{patient}/series"
    assert client.get(url, headers=dict(GATEWAY, **{
        "X-Access-Purpose": "care-delivery", "X-Org-Guids": UNIT_A})).status_code == 403
    assert client.get(url, headers=dict(PATIENTAPP, **{
        "X-Access-Purpose": "care-delivery", "X-Org-Guids": UNIT_A})).status_code == 403
    h = _dash(UNIT_A)
    del h["X-Access-Purpose"]
    assert client.get(url, headers=h).status_code == 400


def test_an_analysis_cdr_still_has_no_clinical_surface(analysis_app):
    c = analysis_app.test_client()
    for url in ("/api/v1/clinical/patients",
                f"/api/v1/clinical/patient/{uuid.uuid4()}/series",
                f"/api/v1/clinical/observation/{uuid.uuid4()}"):
        assert c.get(url, headers=_dash(UNIT_A)).status_code == 404


def test_fhir_read_and_clinical_read_share_ONE_rule24_clause():
    """Both modules call auth.patient_scope_clause; neither spells the
    patient-organisation predicate itself. Asserted on the parse tree, so a
    comment quoting the old form cannot satisfy or trip it."""
    import ast
    import app.api.clinical_read as cr
    import app.api.fhir_read as fr
    from app import auth
    assert str(auth.patient_scope_clause(Obs, ["u"]).left) == "observation.patient_org_guid"
    for mod, fn_name in ((cr, "_apply_org_scope"), (fr, "_org_filter")):
        tree = ast.parse(pathlib.Path(mod.__file__).read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        assert any(getattr(c.func, "id", None) == "patient_scope_clause" for c in calls), fn_name
        own = [c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "in_"
               and getattr(c.func.value, "attr", None) == "patient_org_guid"]
        assert not own, fn_name
