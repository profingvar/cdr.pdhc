"""#1054 (record.pdhc stage 2, the CDR part).

1. every *_history table is append-only (ORM + DB trigger), incl. the new one;
2. ClinicalImpression: table, history, read / vread / _history, search by
   patient and encounter; written ONLY through /api/v1/record;
3. record.pdhc's narrow identity: reads /api/v1/fhir/* only, writes
   Encounter + ClinicalImpression only, always on behalf of a professional;
4. Observation status from the source; a same-id report is a NEW VERSION.

Grants are checked with `_service_key_outcome` inside a test_request_context
(never by issuing a write that could land). Route tests run against the
suite's in-memory SQLite only.
"""
from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

from app import db
from app.models import AuditLog, IngestRaw, ReadAudit
from app.models.history_guard import GUARDED, HistoryIsAppendOnly, history_purge
from app.models.resources import _HISTORY_MODELS, RESOURCES, history_model, live_model
from tests.test_fhir_write import clean_tables, fake_canon  # noqa: F401
from tests.test_review_1044 import _isolate  # noqa: F401

RECORD_KEY = "test-record-key-1054"
RECORD = {"X-Source-Service": "record.pdhc", "X-Service-Key": RECORD_KEY}
GATEWAY = {"X-Source-Service": "gateway.pdhc", "X-Service-Key": "test-gateway-key"}
SIM = {"X-Source-Service": "sim.pdhc", "X-Service-Key": "test-sim-key"}
DASH_KEY, ANALYSE_KEY = "test-dash-1054", "test-analyse-1054"
DOCTOR = "d0c70000-0000-4000-8000-000000001054"
UNIT = "aaaaaaaa-0000-4000-8000-00000000000a"
OTHER_UNIT = "bbbbbbbb-0000-4000-8000-00000000000b"
P = lambda: str(uuid.uuid4())   # noqa: E731 - a fresh synthetic patient guid


@pytest.fixture(autouse=True)
def _keys(app, monkeypatch):
    monkeypatch.setitem(app.config, "RECORD_PDHC_SERVICE_KEY", RECORD_KEY)
    monkeypatch.setitem(app.config, "DASHBOARD_PDHC_SERVICE_KEY", DASH_KEY)
    monkeypatch.setitem(app.config, "ANALYSE_PDHC_SERVICE_KEY", ANALYSE_KEY)


@pytest.fixture(autouse=True)
def _wipe_ci(app):
    def _wipe():
        with app.app_context():
            db.session.rollback()
        with app.app_context(), history_purge(db.session):
            for t in ("clinical_impression_history", "clinical_impression",
                      "encounter_history", "encounter", "cdr_read_audit"):
                db.session.execute(db.text(f"DELETE FROM {t}"))
    _wipe()
    yield
    _wipe()


def _rec(**extra):
    h = dict(RECORD, **{"X-Operator-User-Guid": DOCTOR, "X-Patient-Org-Guid": UNIT,
                        "X-Operator-User-Role": "doctor"})
    h.update(extra)
    return {k: v for k, v in h.items() if v is not None}


def _enc(patient, start="2026-11-24T09:00:00+00:00", **kw):
    return dict({"resourceType": "Encounter", "status": "in-progress",
                 "subject": {"reference": f"Patient/{patient}"},
                 "class": [{"coding": [{"code": "AMB"}]}],
                 "period": {"start": start}}, **kw)


def _ci(patient, encounter=None, **kw):
    b = {"resourceType": "ClinicalImpression", "status": "completed",
         "subject": {"reference": f"Patient/{patient}"},
         "effectiveDateTime": "2026-11-24T09:30:00+00:00",
         "date": "2026-11-24T09:35:00+00:00",
         "performer": {"reference": f"Practitioner/{DOCTOR}"},
         "summary": "Stabil, fortsatt egenvård"}
    if encounter:
        b["encounter"] = {"reference": f"Encounter/{encounter}"}
    b.update(kw)
    return b


# ---------------------------------------------------------------------------
# 1. History is append-only for EVERY type
# ---------------------------------------------------------------------------

def test_GUARDED_names_every_history_model_and_install_refuses_a_gap():
    assert set(GUARDED) == set(_HISTORY_MODELS) == {r[0] for r in RESOURCES}
    assert "ClinicalImpression" in GUARDED and len(GUARDED) == 11
    from app.models import history_guard
    with pytest.raises(RuntimeError, match="without the append-only guard"):
        history_guard.install({"Unlisted": object()})


@pytest.mark.parametrize("fhir_name", [r[0] for r in RESOURCES])
def test_every_history_table_has_both_triggers(app, fhir_name):
    t = history_model(fhir_name).__tablename__
    names = {r[0] for r in db.session.execute(db.text(
        "select name from sqlite_master where type='trigger'"))}
    assert {f"{t}_no_update", f"{t}_no_delete"} <= names


@pytest.mark.parametrize("fhir_name", [r[0] for r in RESOURCES])
def test_raw_sql_cannot_rewrite_any_history_table(app, fhir_name):
    H = history_model(fhir_name)
    t = H.__tablename__
    db.session.execute(sa.insert(H.__table__).values(
        guid=str(uuid.uuid4()), version_id=1, raw_json={}, superseded_at=sa.func.now()))
    db.session.commit()
    for stmt in (f"UPDATE {t} SET source = 'x'", f"DELETE FROM {t}"):
        with pytest.raises(sa.exc.DatabaseError, match="append-only"):
            db.session.execute(db.text(stmt))
        db.session.rollback()
    with history_purge(db.session):
        db.session.execute(db.text(f"DELETE FROM {t}"))


def test_the_ORM_refuses_an_observation_history_rewrite(app, client):
    pid = P()
    oid = _ingest_obs(client, pid, "final", 5.0)
    _ingest_obs(client, pid, "corrected", 5.5, oid=oid)
    row = history_model("Observation").query.filter_by(guid=oid).one()
    row.status = "rewritten"
    with pytest.raises(HistoryIsAppendOnly):
        db.session.flush()
    db.session.rollback()
    row = history_model("Observation").query.filter_by(guid=oid).one()
    db.session.delete(row)
    with pytest.raises(HistoryIsAppendOnly):
        db.session.flush()
    db.session.rollback()


# ---------------------------------------------------------------------------
# Migration f7a8b9c0d1e2 on SQLite (Postgres not available locally)
# ---------------------------------------------------------------------------

def _migration():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "migrations" / "versions" / \
        "f7a8b9c0d1e2_1054_record_stage2.py"
    spec = importlib.util.spec_from_file_location("m1054", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_is_the_single_head_on_top_of_1049():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from pathlib import Path
    cfg = Config()
    cfg.set_main_option("script_location",
                        str(Path(__file__).resolve().parents[1] / "migrations"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["f7a8b9c0d1e2"]
    mod = _migration()
    assert mod.down_revision == "e6f7a8b9c0d1" and len(mod.revision) <= 32


def test_migration_on_populated_tables_matches_the_models_and_reverses():
    """Schema at e6f7a8b9c0d1 = the models minus ClinicalImpression, minus
    version_written_at, triggers only on condition/encounter."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.pool import StaticPool
    from app.models.history_guard import ddl_for
    mod = _migration()
    engine = sa.create_engine("sqlite://", poolclass=StaticPool)
    md = sa.MetaData()
    tables = []
    for name, table, *_ in RESOURCES:
        if name == "ClinicalImpression":
            continue
        for M in (live_model(name), history_model(name)):
            tables.append(M.__table__.to_metadata(md))
    with engine.begin() as conn:
        for t in tables:
            t.create(conn)
        for name, table, *_ in RESOURCES:
            if name == "ClinicalImpression":
                continue
            h = f"{table}_history"
            conn.execute(sa.text(f"DROP TRIGGER IF EXISTS {h}_no_update"))
            conn.execute(sa.text(f"DROP TRIGGER IF EXISTS {h}_no_delete"))
            conn.execute(sa.text(f"ALTER TABLE {h} DROP COLUMN version_written_at"))
        for h in ("condition_history", "encounter_history"):
            for stmt in ddl_for(h, "sqlite"):
                conn.execute(sa.text(stmt))
        conn.execute(sa.text("insert into observation_history (guid, version_id, raw_json, "
                             "superseded_at) values ('o-1', 1, '{}', '2026-11-01')"))
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
            mod.upgrade()                                       # idempotent
        conn.commit()
        insp = sa.inspect(conn)
        for name in ("ClinicalImpression",):
            for M in (live_model(name), history_model(name)):
                want = {c.name: (c.nullable, c.primary_key) for c in M.__table__.columns}
                got = {c["name"]: (c["nullable"], False) for c in insp.get_columns(M.__tablename__)}
                pk = set(insp.get_pk_constraint(M.__tablename__)["constrained_columns"])
                got = {k: (n, k in pk) for k, (n, _) in got.items()}
                assert got == want, M.__tablename__
                assert {i["name"] for i in insp.get_indexes(M.__tablename__)} == \
                    {i.name for i in M.__table__.indexes}
        trig = {r[0] for r in conn.execute(sa.text(
            "select name from sqlite_master where type='trigger'"))}
        for name, table, *_ in RESOURCES:
            h = f"{table}_history"
            assert "version_written_at" in {c["name"] for c in insp.get_columns(h)}
            assert {f"{h}_no_update", f"{h}_no_delete"} <= trig
        assert conn.execute(sa.text("select guid from observation_history")).scalar() == "o-1"
        with pytest.raises(sa.exc.DatabaseError, match="append-only"):
            conn.execute(sa.text("DELETE FROM observation_history"))
        conn.rollback()
        with Operations.context(ctx):
            mod.downgrade()
        conn.commit()
        insp = sa.inspect(conn)
        assert "clinical_impression" not in insp.get_table_names()
        trig = {r[0] for r in conn.execute(sa.text(
            "select name from sqlite_master where type='trigger'"))}
        assert trig == {"condition_history_no_update", "condition_history_no_delete",
                        "encounter_history_no_update", "encounter_history_no_delete"}
        assert "version_written_at" not in {c["name"] for c in insp.get_columns("observation_history")}
        assert conn.execute(sa.text("select guid from observation_history")).scalar() == "o-1"


# ---------------------------------------------------------------------------
# 3. record.pdhc's grants, decided by _service_key_outcome (no write issued)
# ---------------------------------------------------------------------------

def _outcome(app, method, path, headers):
    from app.auth import _service_key_outcome
    with app.test_request_context(path, method=method, headers=headers):
        return _service_key_outcome(app)


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/fhir/Observation"), ("POST", "/api/v1/fhir/Condition"),
    ("PUT", "/api/v1/fhir/Observation/x"), ("POST", "/api/v1/fhir/Bundle"),
    ("POST", "/api/v1/fhir/Encounter"), ("DELETE", "/api/v1/fhir/Encounter/x"),
    ("PATCH", "/api/v1/fhir/Condition/x"),
])
def test_record_cannot_write_or_delete_through_the_fhir_surface(app, method, path):
    from app.auth import WRITE_REFUSED
    assert _outcome(app, method, path, RECORD) is WRITE_REFUSED


@pytest.mark.parametrize("path", ["/api/v1/fhir/Observation", "/api/v1/fhir/Encounter/x",
                                  "/api/v1/fhir/ClinicalImpression/x/_history/1"])
def test_record_reads_the_fhir_surface(app, path):
    assert _outcome(app, "GET", path, RECORD) is True


@pytest.mark.parametrize("path", ["/api/v1/clinical/patients", "/api/v1/stats",
                                  "/api/v1/canonical/x", "/api/v1/cambio/patient/x"])
def test_record_is_confined_to_the_fhir_prefix(app, path):
    assert _outcome(app, "GET", path, RECORD) is False


def test_an_unset_record_key_admits_nobody(app, monkeypatch, client):
    monkeypatch.setitem(app.config, "RECORD_PDHC_SERVICE_KEY", "")
    assert _outcome(app, "GET", "/api/v1/fhir/Encounter", RECORD) is False
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(P()), headers=_rec())
    assert r.status_code == 403
    assert live_model("Encounter").query.count() == 0


@pytest.mark.parametrize("who", ["dashboard.pdhc", "analyse.pdhc"])
def test_a_read_only_caller_still_cannot_write(app, client, who):
    from app.auth import WRITE_REFUSED
    hdr = {"X-Source-Service": who,
           "X-Service-Key": DASH_KEY if who == "dashboard.pdhc" else ANALYSE_KEY}
    assert _outcome(app, "POST", "/api/v1/fhir/Encounter", hdr) is WRITE_REFUSED
    assert _outcome(app, "GET", "/api/v1/fhir/Encounter", hdr) is True   # control
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(P()),
                    headers=dict(_rec(), **hdr))
    assert r.status_code == 403
    assert live_model("Encounter").query.count() == 0


def test_another_writer_key_does_not_open_the_record_door(client):
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(P()),
                    headers=dict(_rec(), **SIM))
    assert r.status_code == 403
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(P()),
                    headers=dict(_rec(), **{"X-Service-Key": "wrong"}))
    assert r.status_code == 403
    # record's own key presented under another service's name
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(P()),
                    headers=dict(_rec(), **{"X-Source-Service": "gateway.pdhc"}))
    assert r.status_code == 403
    assert live_model("Encounter").query.count() == 0


@pytest.mark.parametrize("rtype", ["Observation", "Condition", "Patient", "Procedure"])
def test_record_door_refuses_every_other_type(client, rtype):
    pid = P()
    body = {"resourceType": rtype, "status": "final", "subject": {"reference": f"Patient/{pid}"}}
    r = client.post(f"/api/v1/record/fhir/{rtype}", json=body, headers=_rec())
    assert r.status_code == 403
    assert live_model(rtype).query.count() == 0
    a = AuditLog.query.filter_by(event_type="record.write.refused").one()
    assert a.actor_guid == DOCTOR and a.payload_snapshot["resource_type"] == rtype


def test_record_door_has_no_delete(client):
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(P()), headers=_rec())
    gid = r.get_json()["id"]
    assert client.delete(f"/api/v1/record/fhir/Encounter/{gid}", headers=_rec()).status_code == 405
    assert client.delete(f"/api/v1/fhir/Encounter/{gid}", headers=RECORD).status_code == 403
    assert db.session.get(live_model("Encounter"), gid) is not None


@pytest.mark.parametrize("drop,code", [("X-Operator-User-Guid", 422),
                                       ("X-Patient-Org-Guid", 422)])
def test_every_write_names_the_professional_and_the_patient_unit(client, drop, code):
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(P()),
                    headers=_rec(**{drop: None}))
    assert r.status_code == code
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(P()),
                    headers=_rec(**{drop: "not-a-guid"}))
    assert r.status_code == code
    assert live_model("Encounter").query.count() == 0


def test_a_record_write_is_stored_audited_and_keeps_no_key(client):
    pid = P()
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(pid), headers=_rec())
    assert r.status_code == 201, r.get_json()
    gid = r.get_json()["id"]
    row = db.session.get(live_model("Encounter"), gid)
    assert (row.source, row.patient_guid, row.patient_org_guid) == ("record.pdhc", pid, UNIT)
    raw = db.session.get(IngestRaw, row.ingest_raw_guid)
    assert raw.payload_json["on_behalf_of_user_guid"] == DOCTOR
    assert raw.payload_json["on_behalf_role"] == "doctor"
    assert RECORD_KEY not in str(raw.headers_json)
    assert "X-Operator-User-Guid" in raw.headers_json            # control
    a = AuditLog.query.filter_by(event_type="record.write").one()
    assert a.actor_guid == DOCTOR and a.data_subject_guid == pid
    assert a.payload_snapshot["patient_org_guid"] == UNIT
    assert a.payload_snapshot["version_id"] == 1


def test_post_always_creates_never_lands_on_another_writers_row(client, fake_canon):
    pid = P()
    r = client.post("/api/v1/fhir/Encounter", json=_enc(pid), headers=SIM)
    assert r.status_code == 201
    sim_id = r.get_json()["id"]
    # same patient + same period.start: the generic dedup key would match
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(pid), headers=_rec())
    assert r.status_code == 201 and r.get_json()["id"] != sim_id
    assert db.session.get(live_model("Encounter"), sim_id).source == "sim.pdhc"
    # an explicit existing id is a conflict, not an update
    r = client.post("/api/v1/record/fhir/Encounter", json=_enc(pid, id=sim_id), headers=_rec())
    assert r.status_code == 409
    assert db.session.get(live_model("Encounter"), sim_id).version_id == 1


def test_put_versions_only_records_own_rows_with_if_match(client, fake_canon):
    pid = P()
    sim_id = client.post("/api/v1/fhir/Encounter", json=_enc(pid), headers=SIM).get_json()["id"]
    r = client.put(f"/api/v1/record/fhir/Encounter/{sim_id}", json=_enc(pid),
                   headers=_rec(**{"If-Match": 'W/"1"'}))
    assert r.status_code == 409                       # not record's row
    gid = client.post("/api/v1/record/fhir/Encounter", json=_enc(pid), headers=_rec()).get_json()["id"]
    done = _enc(pid, status="completed")
    assert client.put(f"/api/v1/record/fhir/Encounter/{gid}", json=done,
                      headers=_rec()).status_code == 428
    assert client.put(f"/api/v1/record/fhir/Encounter/{gid}", json=done,
                      headers=_rec(**{"If-Match": 'W/"7"'})).status_code == 412
    assert client.put(f"/api/v1/record/fhir/Encounter/{gid}", json=_enc(P()),
                      headers=_rec(**{"If-Match": 'W/"1"'})).status_code == 409
    r = client.put(f"/api/v1/record/fhir/Encounter/{gid}", json=done,
                   headers=_rec(**{"If-Match": 'W/"1"'}))
    assert r.status_code == 200 and r.headers["ETag"] == 'W/"2"'
    live = db.session.get(live_model("Encounter"), gid)
    hist = history_model("Encounter").query.filter_by(guid=gid).one()
    assert (live.version_id, live.status, hist.version_id, hist.status) == (2, "completed", 1, "in-progress")
    assert hist.version_written_at is not None
    assert client.put(f"/api/v1/record/fhir/Encounter/{P()}", json=done,
                      headers=_rec(**{"If-Match": 'W/"1"'})).status_code == 404


def test_record_reads_are_logged_on_behalf_of_the_professional(client):
    pid = P()
    client.post("/api/v1/record/fhir/Encounter", json=_enc(pid), headers=_rec())
    r = client.get(f"/api/v1/fhir/Encounter?patient={pid}",
                   headers=dict(RECORD, **{"X-Operator-User-Guid": DOCTOR}))
    assert r.status_code == 200 and r.get_json()["total"] == 1
    ra = ReadAudit.query.filter_by(patient_guid=pid).one()
    assert (ra.caller_service, ra.on_behalf_of_user_guid) == ("record.pdhc", DOCTOR)


def test_record_is_held_to_the_whole_system_gate(client):
    pid = P()
    body = _ci(pid, finding=[{"item": {"concept": {"coding": [
        {"system": "icd11-mms", "code": "5A11"}]}}}])
    r = client.post("/api/v1/record/fhir/ClinicalImpression", json=body, headers=_rec())
    assert r.status_code == 403
    assert live_model("ClinicalImpression").query.count() == 0


# ---------------------------------------------------------------------------
# 2. ClinicalImpression
# ---------------------------------------------------------------------------

def test_the_generic_fhir_path_refuses_clinical_impression(client, fake_canon):
    pid = P()
    assert client.post("/api/v1/fhir/ClinicalImpression", json=_ci(pid),
                       headers=SIM).status_code == 403
    bundle = {"resourceType": "Bundle", "type": "transaction",
              "entry": [{"resource": _ci(pid)}]}
    assert client.post("/api/v1/fhir/Bundle", json=bundle, headers=SIM).status_code == 403
    assert live_model("ClinicalImpression").query.count() == 0


def test_clinical_impression_read_vread_history_and_search(client):
    pid = P()
    enc = client.post("/api/v1/record/fhir/Encounter", json=_enc(pid), headers=_rec()).get_json()["id"]
    r = client.post("/api/v1/record/fhir/ClinicalImpression", json=_ci(pid, enc), headers=_rec())
    assert r.status_code == 201, r.get_json()
    cid = r.get_json()["id"]
    row = db.session.get(live_model("ClinicalImpression"), cid)
    assert (row.encounter_guid, row.performer_guid, row.status, row.patient_org_guid) == \
        (enc, DOCTOR, "completed", UNIT)
    assert row.effective_at is not None and row.date_at is not None
    client.post("/api/v1/record/fhir/ClinicalImpression", json=_ci(pid), headers=_rec())
    r = client.put(f"/api/v1/record/fhir/ClinicalImpression/{cid}",
                   json=_ci(pid, enc, summary="Rättad bedömning"),
                   headers=_rec(**{"If-Match": 'W/"1"'}))
    assert r.status_code == 200
    # read (AUTH_MODE=off -> dev SU on the loader)
    assert client.get(f"/api/v1/fhir/ClinicalImpression/{cid}").get_json()["summary"] == "Rättad bedömning"
    assert client.get(f"/api/v1/fhir/ClinicalImpression/{cid}/_history/1").get_json()["summary"] == \
        "Stabil, fortsatt egenvård"
    h = client.get(f"/api/v1/fhir/ClinicalImpression/{cid}/_history").get_json()
    assert h["total"] == 2
    assert client.get(f"/api/v1/fhir/ClinicalImpression?patient={pid}").get_json()["total"] == 2
    by_enc = client.get(f"/api/v1/fhir/ClinicalImpression?encounter=Encounter/{enc}").get_json()
    assert [e["resource"]["id"] for e in by_enc["entry"]] == [cid]
    assert client.get(f"/api/v1/fhir/ClinicalImpression?encounter={enc}").get_json()["total"] == 1
    # a filter the type cannot answer is refused, not ignored
    assert client.get(f"/api/v1/fhir/Observation?encounter={enc}").status_code == 400
    meta = client.get("/api/v1/fhir/metadata").get_json()
    ci = next(x for x in meta["rest"][0]["resource"] if x["type"] == "ClinicalImpression")
    assert {"create", "update"}.isdisjoint({i["code"] for i in ci["interaction"]})
    assert "encounter" in {p["name"] for p in ci["searchParam"]}


# ---------------------------------------------------------------------------
# 4. Observation status and versions on ingest
# ---------------------------------------------------------------------------

def _ingest_obs(client, pid, status, value, *, oid=None, headers=GATEWAY, unit=UNIT,
                expect=202, drop_status=False):
    oid = oid or str(uuid.uuid4())
    fhir = {"resourceType": "Observation", "id": oid, "status": status,
            "subject": {"reference": f"Patient/{pid}"},
            "code": {"text": "P-Glukos"},
            "effectiveDateTime": "2026-11-20T08:00:00+00:00",
            "valueQuantity": {"value": value, "unit": "mmol/L"}}
    if drop_status:
        fhir.pop("status")
    ctx = {"service_request_guid": str(uuid.uuid4())}
    if unit:
        ctx["patient_org_guid"] = unit
    body = {"patient_guid": pid, "source_type": "fhir",
            "source_system_id": str(uuid.uuid4()),
            "fhir_resource": fhir, "clinical_context": ctx}
    r = client.post("/api/v1/ingest", json=body, headers=headers)
    assert r.status_code == expect, r.get_json()
    return oid if expect < 300 else r.get_json()


def _obs(oid):
    db.session.expire_all()
    return db.session.get(live_model("Observation"), oid)


def _hist(oid):
    return history_model("Observation").query.filter_by(guid=oid) \
        .order_by(history_model("Observation").version_id).all()


@pytest.mark.parametrize("status", ["preliminary", "final", "amended", "corrected",
                                    "entered-in-error"])
def test_each_source_status_is_accepted(client, status):
    oid = _ingest_obs(client, P(), status, 6.1)
    assert _obs(oid).status == status


def test_an_unknown_status_is_refused_and_nothing_stored(client):
    before = IngestRaw.query.count()
    out = _ingest_obs(client, P(), "registered", 6.1, expect=422)
    assert "invalid_observation_status" in out["errors"][0]
    assert IngestRaw.query.count() == before


def test_a_correction_is_a_new_version_not_a_new_row_or_an_overwrite(client):
    pid = P()
    oid = _ingest_obs(client, pid, "final", 6.1)
    v1_written = _obs(oid).updated_at
    _ingest_obs(client, pid, "corrected", 6.4, oid=oid)
    live = _obs(oid)
    assert (live.version_id, live.status, float(live.value_quantity)) == (2, "corrected", 6.4)
    assert live.etag == 'W/"2"' and live.raw_json["meta"]["versionId"] == "2"
    assert live_model("Observation").query.filter_by(patient_guid=pid).count() == 1
    (h,) = _hist(oid)
    assert (h.version_id, h.status, float(h.value_quantity)) == (1, "final", 6.1)
    assert h.version_written_at is not None and h.version_written_at == v1_written
    assert h.ingest_raw_guid != live.ingest_raw_guid


def test_vread_serves_both_versions(client):
    pid = P()
    oid = _ingest_obs(client, pid, "preliminary", 6.1)
    _ingest_obs(client, pid, "final", 6.2, oid=oid)
    v1 = client.get(f"/api/v1/fhir/Observation/{oid}/_history/1").get_json()
    v2 = client.get(f"/api/v1/fhir/Observation/{oid}/_history/2").get_json()
    assert (v1["status"], v2["status"]) == ("preliminary", "final")
    assert client.get(f"/api/v1/fhir/Observation/{oid}/_history").get_json()["total"] == 2


@pytest.mark.parametrize("old,new", [("final", "preliminary"), ("corrected", "preliminary"),
                                     ("amended", "preliminary")])
def test_a_status_regression_is_a_409_and_stores_nothing(client, old, new):
    pid = P()
    oid = _ingest_obs(client, pid, old, 6.1)
    before = IngestRaw.query.count()
    out = _ingest_obs(client, pid, new, 6.0, oid=oid, expect=409)
    assert "status_regression" in out["errors"][0]
    assert IngestRaw.query.count() == before
    assert _obs(oid).version_id == 1 and _hist(oid) == []


def test_entered_in_error_keeps_the_history_and_ends_the_line(client):
    pid = P()
    oid = _ingest_obs(client, pid, "final", 6.1)
    _ingest_obs(client, pid, "entered-in-error", 6.1, oid=oid)
    assert _obs(oid).status == "entered-in-error" and len(_hist(oid)) == 1
    out = _ingest_obs(client, pid, "corrected", 6.3, oid=oid, expect=409)
    assert "observation_entered_in_error" in out["errors"][0]
    assert _obs(oid).version_id == 2


def test_an_identical_report_is_not_a_new_version(client):
    pid = P()
    oid = _ingest_obs(client, pid, "final", 6.1)
    _ingest_obs(client, pid, "final", 6.1, oid=oid)      # new source_system_id only
    assert _obs(oid).version_id == 1 and _hist(oid) == []


def test_a_same_id_report_for_another_patient_or_source_is_a_conflict(client):
    pid = P()
    oid = _ingest_obs(client, pid, "final", 6.1)
    assert "observation_id_conflict" in _ingest_obs(client, P(), "corrected", 6.2, oid=oid,
                                                     expect=409)["errors"][0]
    assert "observation_source_conflict" in _ingest_obs(
        client, pid, "corrected", 6.2, oid=oid, headers=SIM, expect=409)["errors"][0]
    assert _obs(oid).version_id == 1


def test_a_new_version_must_state_its_status(client):
    pid = P()
    oid = _ingest_obs(client, pid, "final", 6.1)
    out = _ingest_obs(client, pid, None, 6.2, oid=oid, drop_status=True, expect=422)
    assert "observation_status_required" in out["errors"][0]


def test_a_correction_without_a_unit_stays_in_the_patients_unit(client):
    pid = P()
    oid = _ingest_obs(client, pid, "final", 6.1, unit=UNIT)
    _ingest_obs(client, pid, "corrected", 6.2, oid=oid, unit=None)
    assert _obs(oid).patient_org_guid == UNIT


def test_ingest_raw_never_keeps_the_callers_key(client):
    oid = _ingest_obs(client, P(), "final", 6.1)
    raw = db.session.get(IngestRaw, _obs(oid).ingest_raw_guid)
    assert "test-gateway-key" not in str(raw.headers_json)
    assert raw.headers_json.get("X-Source-Service") == "gateway.pdhc"     # control
