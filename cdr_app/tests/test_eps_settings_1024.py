"""#1024 — EPS_* is read first, IPS_* is the deprecated fallback, no default."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app import create_app
from app.services import analysis_consent as ac
from app.services.eps_settings import eps_env, eps_setting

#: Captured at import (collection), before conftest's autouse fixture swaps
#: _analysis_filter for an allow-all stub.
_REAL_ANALYSIS_FILTER = ac._analysis_filter

_NAMES = ("EPS_BASE_URL", "IPS_BASE_URL", "EPS_API_KEY", "IPS_API_KEY")


@pytest.fixture
def env(monkeypatch):
    for n in _NAMES:
        monkeypatch.delenv(n, raising=False)
    return monkeypatch


@pytest.mark.parametrize("suffix", ["BASE_URL", "API_KEY"])
def test_new_name_wins(env, suffix):
    env.setenv(f"EPS_{suffix}", "new")
    env.setenv(f"IPS_{suffix}", "old")
    assert eps_env(suffix) == "new"


@pytest.mark.parametrize("suffix", ["BASE_URL", "API_KEY"])
def test_old_name_still_works(env, suffix):
    env.setenv(f"IPS_{suffix}", "old")
    assert eps_env(suffix) == "old"


@pytest.mark.parametrize("suffix", ["BASE_URL", "API_KEY"])
def test_an_empty_new_name_falls_back_to_the_old(env, suffix):
    env.setenv(f"EPS_{suffix}", "")
    env.setenv(f"IPS_{suffix}", "old")
    assert eps_env(suffix) == "old"


@pytest.mark.parametrize("suffix", ["BASE_URL", "API_KEY"])
def test_both_unset_is_unset_with_no_invented_default(env, suffix):
    assert eps_env(suffix) == ""
    env.setenv(f"EPS_{suffix}", "")
    env.setenv(f"IPS_{suffix}", "")
    assert eps_env(suffix) == ""


def test_app_config_takes_the_new_name_at_startup(env):
    env.setenv("EPS_BASE_URL", "https://eps.example")
    env.setenv("IPS_BASE_URL", "https://ips.example")
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                      "AUTH_MODE": "off"})
    assert app.config["IPS_BASE_URL"] == "https://eps.example"


def test_app_config_still_takes_the_old_name_alone(env):
    env.setenv("IPS_BASE_URL", "https://ips.example")
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                      "AUTH_MODE": "off"})
    assert app.config["IPS_BASE_URL"] == "https://ips.example"


def test_app_config_both_unset_is_empty(env):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                      "AUTH_MODE": "off"})
    assert app.config["IPS_BASE_URL"] == ""


def _consent_call(app, env_key=None):
    """Run the REAL consent call with the app's own config emptied, so the
    address and key can only come from the environment."""
    seen = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"allowed": ["p1"], "excluded": []}

    def _post(url, **kw):
        seen["url"], seen["headers"] = url, kw.get("headers") or {}
        return _Resp()

    prev = {k: app.config.get(k) for k in ("IPS_BASE_URL", "IPS_API_KEY")}
    app.config["IPS_BASE_URL"] = ""
    app.config["IPS_API_KEY"] = ""
    try:
        with app.test_request_context("/api/v1/fhir/Observation"):
            with patch("app.services.analysis_consent.requests.post", side_effect=_post):
                _REAL_ANALYSIS_FILTER(["p1"], "care", [])
    finally:
        app.config.update(prev)
    return seen


def test_consent_call_uses_the_eps_names_over_the_ips_names(app, env):
    env.setenv("EPS_BASE_URL", "https://eps.example")
    env.setenv("IPS_BASE_URL", "https://ips.example")
    env.setenv("EPS_API_KEY", "k-new")
    env.setenv("IPS_API_KEY", "k-old")
    seen = _consent_call(app)
    assert seen["url"] == "https://eps.example/api/v1/patients/analysis-filter"
    assert seen["headers"]["Authorization"] == "ApiKey k-new"


def test_consent_call_still_works_on_the_ips_names_alone(app, env):
    env.setenv("IPS_BASE_URL", "https://ips.example")
    env.setenv("IPS_API_KEY", "k-old")
    seen = _consent_call(app)
    assert seen["url"] == "https://ips.example/api/v1/patients/analysis-filter"
    assert seen["headers"]["Authorization"] == "ApiKey k-old"


def test_consent_call_with_nothing_set_refuses_by_name(app, env):
    with pytest.raises(ac.IpsUnreachable, match="EPS_BASE_URL"):
        _consent_call(app)


def test_external_read_summary_uses_the_eps_names(app, env):
    from app.api import external_read as er
    env.setenv("EPS_BASE_URL", "https://eps.example")
    env.setenv("IPS_BASE_URL", "https://ips.example")
    env.setenv("EPS_API_KEY", "k-new")
    seen = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"patients": [{"birth_year": 1961}]}

    def _post(url, **kw):
        seen["url"], seen["auth"] = url, (kw.get("headers") or {}).get("Authorization")
        return _Resp()

    prev = {k: app.config.get(k) for k in ("IPS_BASE_URL", "IPS_API_KEY")}
    app.config["IPS_BASE_URL"] = ""
    app.config["IPS_API_KEY"] = ""
    try:
        with app.test_request_context("/"):
            from flask import g
            g.read_grant = {"grant_guid": "g1"}
            with patch.object(er.requests, "post", side_effect=_post):
                out, status = er._ips_summary("p1")
    finally:
        app.config.update(prev)
    assert status == "ok"
    assert seen["url"].startswith("https://eps.example/")
    assert seen["auth"] == "ApiKey k-new"


def test_external_read_summary_unset_is_not_configured(app, env):
    from app.api import external_read as er
    prev = {k: app.config.get(k) for k in ("IPS_BASE_URL", "IPS_API_KEY")}
    app.config["IPS_BASE_URL"] = ""
    app.config["IPS_API_KEY"] = ""
    try:
        with app.test_request_context("/"):
            assert er._ips_summary("p1") == (None, "not_configured")
    finally:
        app.config.update(prev)


def test_eps_setting_prefers_the_app_config_then_the_environment(env):
    env.setenv("EPS_API_KEY", "k-env")
    assert eps_setting("API_KEY", {"IPS_API_KEY": "k-cfg"}) == "k-cfg"
    assert eps_setting("API_KEY", {}) == "k-env"
