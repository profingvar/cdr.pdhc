"""#1006 — an unknown performer organisation is stored as NULL, never invented.

cdr filled org_guid with "00000000-0000-0000-0000-000000000000" whenever the
producer named none; icd11's nine Conditions landed in cdr2 with it.
"""
from __future__ import annotations

import importlib.util
import pathlib

import sqlalchemy as sa

from app.models.resources import ChangeFeed, live_model
from app.services.ingest_pipeline import build_live_observation_row
from tests.test_fhir_write import (  # noqa: F401  (fixtures)
    _SERVICE_KEYS, _hba1c_body, clean_tables, fake_canon,
)

ZERO = "00000000-0000-0000-0000-000000000000"
ORG_TAG = "https://cdr.pdhc.se/CodeSystem/org"


def _post(client, headers_extra):
    headers = {"X-Source-Service": "sim.pdhc", "X-Service-Key": _SERVICE_KEYS["sim.pdhc"],
               "X-Request-Id": "req-1006"}
    headers.update(headers_extra)
    return client.post("/api/v1/fhir/Observation", json=_hba1c_body(), headers=headers)


def _org_tags(resource):
    return [s for s in ((resource.get("meta") or {}).get("security") or []) if s.get("system") == ORG_TAG]


# ----------------------------------------------------------------- write path

def test_a_write_WITHOUT_x_org_guid_stores_NULL_not_the_zero_guid(client, fake_canon):
    r = _post(client, {})
    assert r.status_code == 201, r.get_json()
    row, = live_model("Observation").query.all()
    assert row.org_guid is None
    feed, = ChangeFeed.query.all()
    assert feed.org_guid is None
    assert _org_tags(r.get_json()) == [], "no tag rather than one saying None"


def test_a_BLANK_header_is_unset(client, fake_canon):
    _post(client, {"X-Org-Guid": "   "})
    assert live_model("Observation").query.one().org_guid is None


def test_a_write_WITH_x_org_guid_stores_it_and_tags_it(client, fake_canon):
    r = _post(client, {"X-Org-Guid": "org-real"})
    assert live_model("Observation").query.one().org_guid == "org-real"
    assert [t["display"] for t in _org_tags(r.get_json())] == ["org-real"]


# ----------------------------------------------------------------- ingest path

def _obs(performer=None):
    o = {"resourceType": "Observation", "id": "obs-1006", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
         "effectiveDateTime": "2026-11-16T10:00:00Z", "valueQuantity": {"value": 6.4, "unit": "%"}}
    if performer:
        o["performer"] = [{"identifier": {"value": performer}}]
    return o


def test_ingest_WITHOUT_a_performer_builds_NULL(app):
    with app.app_context():
        row = build_live_observation_row(_obs(), patient_guid="p1", source_service="sim.pdhc")
        assert row.org_guid is None


def test_ingest_WITH_a_performer_keeps_it(app):
    with app.app_context():
        row = build_live_observation_row(_obs("org-perf"), patient_guid="p1", source_service="sim.pdhc")
        assert row.org_guid == "org-perf"


# ----------------------------------------------------------------- the migration

def _load_migration():
    p = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "d9e0f1a2b3c4_org_guid_nullable.py"
    spec = importlib.util.spec_from_file_location("mig_1006", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(mod, fn, conn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    mod.op = Operations(MigrationContext.configure(conn))
    getattr(mod, fn)()


def test_the_migration_nulls_ONLY_the_zero_guid_and_round_trips(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with eng.begin() as c:
        c.execute(sa.text("CREATE TABLE condition (guid TEXT PRIMARY KEY, org_guid VARCHAR(36) NOT NULL)"))
        c.execute(sa.text("CREATE TABLE change_feed (seq INTEGER PRIMARY KEY, org_guid VARCHAR(36) NOT NULL)"))
        c.execute(sa.text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, name TEXT)"))
        c.execute(sa.text("INSERT INTO condition VALUES ('a', :z), ('b', 'org-real')"), {"z": ZERO})
        c.execute(sa.text("INSERT INTO change_feed VALUES (1, :z), (2, 'org-real')"), {"z": ZERO})
    mod = _load_migration()
    with eng.begin() as c:
        _run(mod, "upgrade", c)
    with eng.connect() as c:
        assert c.execute(sa.text("SELECT guid, org_guid FROM condition ORDER BY guid")).all() == [("a", None), ("b", "org-real")]
        assert c.execute(sa.text("SELECT org_guid FROM change_feed ORDER BY seq")).scalars().all() == [None, "org-real"]
        cols = {col["name"]: col["nullable"] for col in sa.inspect(c).get_columns("condition")}
        assert cols["org_guid"] is True
    with eng.begin() as c:
        _run(mod, "downgrade", c)
    with eng.connect() as c:
        assert c.execute(sa.text("SELECT org_guid FROM condition ORDER BY guid")).scalars().all() == [ZERO, "org-real"]
        cols = {col["name"]: col["nullable"] for col in sa.inspect(c).get_columns("condition")}
        assert cols["org_guid"] is False
