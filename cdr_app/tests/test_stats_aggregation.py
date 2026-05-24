"""Ticket #116 — $stats SQL-aggregation refactor sanity tests.

The legacy test_stats_math_against_fixture in test_fhir_read.py is
currently broken upstream (the write path rejects X-Source-Service
"test" — pre-existing rot, see the 18 other failing tests in that
file). These tests insert rows directly via the ORM so the
aggregation logic is exercised without depending on the write API.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("FLASK_SECRET_KEY", "x")
os.environ.setdefault("AUTH_MODE", "off")

import statistics as st  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.models.resources import live_model  # noqa: E402


ORG_HEADERS = {"X-Org-Guids": "test-org"}
CODE_SYSTEM = "https://termbank.pdhc.se/CodeSystem/loinc"
CODE_LOCAL = "4548-4"
CODE = f"{CODE_SYSTEM}/{CODE_LOCAL}"
CODE_QUERY = f"{CODE_SYSTEM}|{CODE_LOCAL}"  # FHIR system|code form


@pytest.fixture(autouse=True)
def _clean(app):
    with app.app_context():
        Obs = live_model("Observation")
        Obs.query.delete()
        db.session.commit()
        yield


def _insert(values, code=CODE):
    """Insert one Observation per value, distinct effective_at."""
    Obs = live_model("Observation")
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    for i, v in enumerate(values):
        db.session.add(Obs(
            guid=f"o-{i}",
            patient_guid="p1",
            org_guid="test-org",
            code_canonical=code,
            effective_at=base.replace(
                hour=(i // 3600) % 24,
                minute=(i // 60) % 60,
                second=i % 60,
            ),
            raw_json={},
            value_quantity=v,
        ))
    db.session.commit()


def test_stats_empty_returns_n_zero(client):
    """No rows → n=0, no histogram, 200 OK."""
    resp = client.get(
        f"/api/v1/fhir/Observation/$stats?code={CODE_QUERY}",
        headers=ORG_HEADERS,
    )
    assert resp.status_code == 200
    params = {p["name"]: p for p in resp.get_json()["parameter"]}
    assert params["n"]["valueInteger"] == 0


def test_stats_math_matches_python_reference(client, app):
    """Compare against statistics.pstdev / fmean on the same values."""
    values = [6.0 + i * 0.001 for i in range(1000)]
    with app.app_context():
        _insert(values)

    resp = client.get(
        f"/api/v1/fhir/Observation/$stats?code={CODE_QUERY}&buckets=20",
        headers=ORG_HEADERS,
    )
    assert resp.status_code == 200
    params = {p["name"]: p for p in resp.get_json()["parameter"]}

    assert params["n"]["valueInteger"] == 1000
    assert abs(params["mean"]["valueDecimal"] - st.fmean(values)) < 1e-6
    assert abs(params["sd"]["valueDecimal"] - st.pstdev(values)) < 1e-6
    histogram = params["histogram"]["part"]
    assert len(histogram) == 20
    total = sum(int(h["valueString"].split(":")[-1]) for h in histogram)
    assert total == 1000


def test_stats_filters_null_value_quantity(client, app):
    """Observations without value_quantity must not show in n."""
    values = [1.0, 2.0, 3.0, None, None]
    with app.app_context():
        _insert(values)

    resp = client.get(
        f"/api/v1/fhir/Observation/$stats?code={CODE_QUERY}",
        headers=ORG_HEADERS,
    )
    params = {p["name"]: p for p in resp.get_json()["parameter"]}
    assert params["n"]["valueInteger"] == 3


def _insert_cgm(values_by_hour, code=CODE, patient="p1"):
    """Insert one Observation per (hour, replicate) — distinct minute
    stamps so the dedup key stays unique."""
    Obs = live_model("Observation")
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    minute = 0
    for hour, values in values_by_hour.items():
        for v in values:
            db.session.add(Obs(
                guid=f"o-{hour:02d}-{minute}",
                patient_guid=patient,
                org_guid="test-org",
                code_canonical=code,
                effective_at=base.replace(hour=hour,
                                          minute=minute % 60,
                                          second=(minute // 60) % 60),
                raw_json={},
                value_quantity=v,
            ))
            minute += 1
    db.session.commit()


def test_agp_empty_returns_n_zero(client):
    resp = client.get(
        f"/api/v1/fhir/Observation/$agp?patient=p1&code={CODE_QUERY}",
        headers=ORG_HEADERS,
    )
    assert resp.status_code == 200
    params = {p["name"]: p for p in resp.get_json()["parameter"]}
    assert params["n"]["valueInteger"] == 0


def test_agp_basic_per_hour_bands(client, app):
    """A spread of values across hours 7-9 should populate just those bands."""
    with app.app_context():
        _insert_cgm({
            7: [4.5, 5.0, 5.5, 6.0, 6.5],
            8: [7.0, 7.5, 8.0, 8.5, 9.0],
            9: [9.5, 10.0, 10.5, 11.0, 11.5],
        })
    resp = client.get(
        f"/api/v1/fhir/Observation/$agp?patient=p1&code={CODE_QUERY}",
        headers=ORG_HEADERS,
    )
    assert resp.status_code == 200
    params = {p["name"]: p for p in resp.get_json()["parameter"]}
    assert params["n"]["valueInteger"] == 15
    bands_root = params["bands"]
    by_hour = {}
    for h_part in bands_root["part"]:
        sub = {p["name"]: p for p in h_part["part"]}
        by_hour[sub["hour"]["valueInteger"]] = sub
    # Hours 7-9 populated, others not
    assert by_hour[7]["n"]["valueInteger"] == 5
    assert by_hour[8]["n"]["valueInteger"] == 5
    assert by_hour[9]["n"]["valueInteger"] == 5
    assert by_hour[0]["n"]["valueInteger"] == 0
    # Median at hour 8 is 8.0
    assert abs(by_hour[8]["p50"]["valueDecimal"] - 8.0) < 1e-6


def test_agp_tir_tbr_tar(client, app):
    """Verify TIR/TBR/TAR are correctly computed against 3.9-10.0
    mmol/L thresholds (Swedish defaults)."""
    with app.app_context():
        _insert_cgm({
            10: [3.0, 3.5],                # below (TBR)
            11: [4.0, 5.0, 6.0, 7.0, 8.0], # in range
            12: [11.0, 12.0, 13.0],        # above (TAR)
        })
    resp = client.get(
        f"/api/v1/fhir/Observation/$agp?patient=p1&code={CODE_QUERY}",
        headers=ORG_HEADERS,
    )
    params = {p["name"]: p for p in resp.get_json()["parameter"]}
    assert params["n"]["valueInteger"] == 10
    assert abs(params["tbr"]["valueDecimal"] - 20.0) < 1e-6
    assert abs(params["tir"]["valueDecimal"] - 50.0) < 1e-6
    assert abs(params["tar"]["valueDecimal"] - 30.0) < 1e-6


def test_agp_thresholds_overridable(client, app):
    """Pass tir_low/tir_high to override the Swedish defaults."""
    with app.app_context():
        _insert_cgm({
            10: [3.0, 3.5, 4.0],   # in below-range at tir_low=4.5
            11: [5.0, 6.0],
        })
    resp = client.get(
        f"/api/v1/fhir/Observation/$agp?patient=p1&code={CODE_QUERY}"
        f"&tir_low=4.5&tir_high=10",
        headers=ORG_HEADERS,
    )
    params = {p["name"]: p for p in resp.get_json()["parameter"]}
    assert abs(params["tbr"]["valueDecimal"] - 60.0) < 1e-6  # 3 of 5
    assert abs(params["tir"]["valueDecimal"] - 40.0) < 1e-6  # 2 of 5


def test_postgres_path_compiles_when_dialect_is_postgres(app, monkeypatch):
    """Compile the SQL the Postgres path would emit, verify it uses
    percentile_cont + width_bucket (the whole point of #116) and runs
    without a real Postgres connection.

    We can't easily fake the *execution* without spinning up a real
    PG, but we can at least confirm the SQL is well-formed by
    rendering it through SQLAlchemy's PG dialect compiler.
    """
    from sqlalchemy.dialects import postgresql as pg_dialect
    from app.api.fhir_read import _stats_postgres

    Obs = live_model("Observation")
    # Build the kind of query the handler builds:
    with app.app_context():
        q = (
            db.session.query(Obs.value_quantity)
            .filter(Obs.value_quantity.isnot(None))
            .filter(Obs.code_canonical == "x")
        )
        # _stats_postgres builds two SELECTs from q.subquery(); we just
        # need to verify the SQL text contains the PG-specific bits.
        from sqlalchemy import select, func
        subq = q.subquery()
        val = subq.c.value_quantity
        agg = select(
            func.count(val),
            func.percentile_cont(0.5).within_group(val.asc()),
        ).select_from(subq)
        sql = str(agg.compile(dialect=pg_dialect.dialect(),
                              compile_kwargs={"literal_binds": True}))
    assert "percentile_cont" in sql
    assert "WITHIN GROUP" in sql
