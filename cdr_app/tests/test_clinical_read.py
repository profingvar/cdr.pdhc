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


def test_admin_sees_all_orgs(client):
    h = dict(BASE_H, **{"X-Is-Admin": "1"})
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
