"""Health endpoint tests."""


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["service"] == "cdr.pdhc"


def test_health_detailed(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["service"] == "cdr.pdhc"
    assert "database" in data
    assert data["database"] in ("connected", "unavailable")
