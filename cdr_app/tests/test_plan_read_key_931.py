"""#931 — every CDR reads plan.pdhc with its OWN read_only service key.

cdr1/cdr2/cdr3 run this code; each names itself through PLAN_SOURCE_SERVICE,
so plan can tell them apart and a key rotation touches one CDR.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services import plan_client as pc


def _ok(body=None):
    r = MagicMock(status_code=200)
    r.json.return_value = body or {}
    return r


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PLAN_SERVICE_KEY", "k931-cdr")
    monkeypatch.delenv("PLAN_SOURCE_SERVICE", raising=False)
    return monkeypatch


def test_unset_key_sends_nothing(monkeypatch):
    monkeypatch.delenv("PLAN_SERVICE_KEY", raising=False)
    assert pc.plan_read_headers() == {}


def test_cdr1_names_itself_by_default(env):
    assert pc.plan_read_headers() == {"X-Source-Service": "cdr.pdhc", "X-Service-Key": "k931-cdr"}


def test_cdr2_names_itself_from_its_env(env):
    env.setenv("PLAN_SOURCE_SERVICE", "cdr2.pdhc")
    assert pc.plan_read_headers()["X-Source-Service"] == "cdr2.pdhc"


def test_the_key_is_read_at_CALL_time_not_import_time(env):
    env.setenv("PLAN_SERVICE_KEY", "rotated")
    assert pc.plan_read_headers()["X-Service-Key"] == "rotated"


def test_the_one_http_chokepoint_carries_it_alongside_session_headers(env):
    client = pc.PlanClient(base_url="http://plan-test")
    with patch.object(pc.requests, "get", return_value=_ok()) as g, \
         patch("app.services.session_headers.outbound_session_headers",
               return_value={"X-Operator-Session-Id": "sid-1"}):
        client._get_with_retry("http://plan-test/api/v1/lookup/whole-system-libs", operation="t")
    h = g.call_args.kwargs["headers"]
    assert h["X-Service-Key"] == "k931-cdr" and h["X-Source-Service"] == "cdr.pdhc"
    assert h["X-Operator-Session-Id"] == "sid-1", "session correlation (#408) is kept"


def test_the_chokepoint_is_the_ONLY_requests_call_in_plan_client():
    # If a second raw requests.get appears, it would bypass the key.
    import ast, inspect
    tree = ast.parse(inspect.getsource(pc))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr in ("get", "post")
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "requests"]
    assert len(calls) == 1
