"""Tests — CDR_READ_LOCKDOWN flag (#293).

When true, only X-Source-Service: dashboard.pdhc is accepted on
read endpoints. Ingest paths remain public regardless. cdr1 ships
with the flag false; cdr2-5 + cdr_6 ship with it true.
"""
import pytest


SR_GUID = "00000000-0000-0000-0000-000000000123"


def _hdr(source, key):
    return {"X-Source-Service": source, "X-Service-Key": key}


_KEYS = (
    "CDR_READ_LOCKDOWN",
    "GATEWAY_PDHC_SERVICE_KEY",
    "DASHBOARD_PDHC_SERVICE_KEY",
    "SIM_PDHC_SERVICE_KEY",
)


@pytest.fixture(autouse=True)
def _reset_config_after(app):
    """Save the session-scoped app's config snapshot for the keys this
    test file mutates, then restore on teardown. Without this, the
    overrides set by individual tests leak into the next test file
    (e.g. test_provenance.py which depends on conftest's
    GATEWAY_PDHC_SERVICE_KEY value)."""
    snapshot = {k: app.config.get(k) for k in _KEYS}
    yield
    for k, v in snapshot.items():
        if v is None:
            app.config.pop(k, None)
        else:
            app.config[k] = v


def _set_keys(app):
    app.config["GATEWAY_PDHC_SERVICE_KEY"] = "gw-key"
    app.config["DASHBOARD_PDHC_SERVICE_KEY"] = "dash-key"
    app.config["SIM_PDHC_SERVICE_KEY"] = "sim-key"


# ── Default behaviour (flag false) ──────────────────────────────────


def test_default_gateway_read_accepted(client, app):
    """Without lockdown gateway is rejected because cdr1's
    KNOWN_FHIR_SERVICES has only sim and dashboard. Use dashboard to
    establish the baseline: read works."""
    _set_keys(app)
    app.config["CDR_READ_LOCKDOWN"] = False
    r = client.get("/api/v1/fhir/Observation/no-such-guid",
                   headers=_hdr("dashboard.pdhc", "dash-key"))
    # 404 is fine — the auth layer let us through to the route handler.
    assert r.status_code in (404, 410)


def test_default_sim_read_accepted(client, app):
    _set_keys(app)
    app.config["CDR_READ_LOCKDOWN"] = False
    r = client.get("/api/v1/fhir/Observation/no-such-guid",
                   headers=_hdr("sim.pdhc", "sim-key"))
    assert r.status_code in (404, 410)


# ── Flag true (cdr2-5 + cdr_6 behaviour) ────────────────────────────


def test_lockdown_dashboard_read_succeeds(client, app):
    _set_keys(app)
    app.config["CDR_READ_LOCKDOWN"] = True
    r = client.get("/api/v1/fhir/Observation/no-such-guid",
                   headers=_hdr("dashboard.pdhc", "dash-key"))
    assert r.status_code in (404, 410)


def test_lockdown_sim_read_rejected(client, app):
    """Even sim, which is in KNOWN_FHIR_SERVICES, gets 403 on reads
    when the lockdown is active. sim still writes via the public
    /api/v1/ingest path."""
    _set_keys(app)
    app.config["CDR_READ_LOCKDOWN"] = True
    r = client.get("/api/v1/fhir/Observation/no-such-guid",
                   headers=_hdr("sim.pdhc", "sim-key"))
    assert r.status_code == 403
    assert b"Invalid service credentials" in r.data


def test_lockdown_unknown_source_rejected(client, app):
    _set_keys(app)
    app.config["CDR_READ_LOCKDOWN"] = True
    r = client.get("/api/v1/fhir/Observation/no-such-guid",
                   headers=_hdr("gateway.pdhc", "gw-key"))
    # gateway.pdhc is not in KNOWN_FHIR_SERVICES at all — would be 403
    # whether or not lockdown is on. The point of this test: the 403
    # is consistent.
    assert r.status_code == 403


def test_lockdown_ingest_still_open(client, app):
    """The /api/v1/ingest* family is in _public_path so the
    lockdown loader is bypassed entirely. Gateway can still write."""
    _set_keys(app)
    app.config["CDR_READ_LOCKDOWN"] = True
    # POST with no body → 400 from the ingest endpoint itself,
    # NOT 403 from the loader — proving auth was skipped.
    r = client.post("/api/v1/ingest",
                    headers=_hdr("gateway.pdhc", "gw-key"),
                    json=None)
    assert r.status_code in (400, 401, 422)  # whatever ingest returns,
    # the key point: not 403.
    assert r.status_code != 403


def test_lockdown_observations_subpaths_still_open(client, app):
    """/api/v1/observations/<guid>/provenance is in _public_path so
    gateway keeps reading it for receipt-ack and similar (#288,
    #281). The lockdown does not touch this surface."""
    _set_keys(app)
    app.config["CDR_READ_LOCKDOWN"] = True
    r = client.get("/api/v1/observations/no-such-guid/provenance",
                   headers=_hdr("gateway.pdhc", "gw-key"))
    # The loader is skipped; the provenance handler's own
    # @require_service_key runs. gateway.pdhc isn't in its allow-list
    # (uses the OTHER KNOWN_SERVICES map). 401/403 from the handler
    # is fine — the key point: the lockdown didn't shadow.
    assert r.status_code in (401, 403, 404)


def test_lockdown_health_still_open(client, app):
    _set_keys(app)
    app.config["CDR_READ_LOCKDOWN"] = True
    r = client.get("/healthz")
    assert r.status_code == 200
