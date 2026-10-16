"""Unit tests for the xlate.pdhc client — the two failure states (#833).

``test_fhir_write.py`` has advertised this module in its docstring since
Phase 1 and it did not exist; these tests make that claim true for the
xlate half.

The distinction under test is the one #833 was filed for. "xlate did not
answer" had a single exception, so a CDR with no xlate service at all
returned the same retryable 503 as a CDR whose xlate blipped. xlate.pdhc
is not deployed anywhere on the platform, so every foreign coding would
have been retried forever against a service that does not exist.

Two states now:

  - no ``XLATE_BASE_URL``        -> ``XlateNotConfigured``, permanent, 422
  - set but the call failed      -> ``XlateUnavailable``, transient, 503

Both still subclass ``XlateUnreachable``, so an older catch-all keeps
working — but the catch-all can no longer silently relabel a permanent
condition as a transient one.
"""
from __future__ import annotations

import pytest
import requests

from app.services import xlate_client as xc
from app.services.xlate_client import (
    XlateClient,
    XlateNotConfigured,
    XlateUnavailable,
    XlateUnreachable,
)


# ---------------------------------------------------------------------------
# configured / not configured
# ---------------------------------------------------------------------------

def test_unset_env_is_not_configured(monkeypatch):
    """An unset variable must read as unset.

    Before #833 the client fell back to ``http://127.0.0.1:9017``, which
    is indistinguishable from a real configuration to every caller and
    resolves, inside the app container, to the container itself.
    """
    monkeypatch.delenv("XLATE_BASE_URL", raising=False)
    client = XlateClient()
    assert client.base_url is None
    assert client.configured is False


def test_empty_env_is_not_configured(monkeypatch):
    """``XLATE_BASE_URL=`` is a blank, not a configuration."""
    monkeypatch.setenv("XLATE_BASE_URL", "")
    assert XlateClient().configured is False


def test_no_module_level_default_base_url():
    """Guard the deleted fallback so it cannot be reintroduced.

    A module-level default is what turned "nobody wired xlate up" into
    an endless 503 retry loop; reviving it would silently undo the fix.
    """
    assert not hasattr(xc, "DEFAULT_BASE_URL")


def test_explicit_base_url_is_configured(monkeypatch):
    monkeypatch.delenv("XLATE_BASE_URL", raising=False)
    client = XlateClient(base_url="https://xlate.pdhc.se/")
    assert client.configured is True
    assert client.base_url == "https://xlate.pdhc.se"  # trailing / stripped


def test_env_base_url_is_configured(monkeypatch):
    monkeypatch.setenv("XLATE_BASE_URL", "https://xlate.pdhc.se")
    assert XlateClient().configured is True


# ---------------------------------------------------------------------------
# the permanent state
# ---------------------------------------------------------------------------

def test_translate_unconfigured_raises_not_configured(monkeypatch):
    monkeypatch.delenv("XLATE_BASE_URL", raising=False)
    client = XlateClient()
    with pytest.raises(XlateNotConfigured) as ei:
        client.translate("http://loinc.org", "4548-4")
    # The exception carries the coding so the 422 can name what to fix.
    assert ei.value.system == "http://loinc.org"
    assert ei.value.code == "4548-4"
    assert "XLATE_BASE_URL" in str(ei.value)


def test_unconfigured_makes_no_network_call(monkeypatch):
    """Refuse before the socket, not after a 5s connect timeout per coding.

    This is also the proof that the permanent case is decided from
    configuration alone and never from a reachability probe.
    """
    monkeypatch.delenv("XLATE_BASE_URL", raising=False)

    def _boom(*a, **kw):
        raise AssertionError(
            "requests.post called although XLATE_BASE_URL is unset — the "
            "client must refuse from configuration, without a network call."
        )

    monkeypatch.setattr(xc.requests, "post", _boom)
    with pytest.raises(XlateNotConfigured):
        XlateClient().translate("http://loinc.org", "4548-4")


def test_not_configured_is_still_an_xlate_unreachable(monkeypatch):
    """Back-compatible: an older ``except XlateUnreachable`` still catches it."""
    monkeypatch.delenv("XLATE_BASE_URL", raising=False)
    with pytest.raises(XlateUnreachable):
        XlateClient().translate("http://loinc.org", "4548-4")


# ---------------------------------------------------------------------------
# the transient state
# ---------------------------------------------------------------------------

def test_configured_but_unreachable_raises_unavailable(monkeypatch):
    """Someone wired a URL up, so a blip is plausible — keep the 503."""
    monkeypatch.setenv("XLATE_BASE_URL", "https://xlate.pdhc.se")

    def _refused(*a, **kw):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(xc.requests, "post", _refused)
    with pytest.raises(XlateUnavailable):
        XlateClient().translate("http://loinc.org", "4548-4")


def test_unavailable_is_not_mistaken_for_not_configured(monkeypatch):
    """The two states must not be interchangeable in either direction."""
    monkeypatch.setenv("XLATE_BASE_URL", "https://xlate.pdhc.se")

    def _refused(*a, **kw):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(xc.requests, "post", _refused)
    with pytest.raises(XlateUnavailable):
        XlateClient().translate("http://loinc.org", "4548-4")
    # ...and the permanent class must NOT catch it.
    with pytest.raises(XlateUnavailable):
        try:
            XlateClient().translate("http://loinc.org", "4548-4")
        except XlateNotConfigured:  # pragma: no cover - would be the bug
            raise AssertionError(
                "a reachability failure was classed as XlateNotConfigured"
            )


def test_hit_is_unaffected(monkeypatch):
    """The happy path still parses the Parameters body (no regression)."""
    monkeypatch.setenv("XLATE_BASE_URL", "https://xlate.pdhc.se")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "resourceType": "Parameters",
                "parameter": [
                    {"name": "result", "valueBoolean": True},
                    {"name": "match", "valueCoding": {
                        "system": "loinc", "code": "4548-4",
                        "display": "HbA1c"}},
                    {"name": "mapping_quality", "valueCode": "equivalent"},
                    {"name": "via", "valueString": "system_alias"},
                ],
            }

    monkeypatch.setattr(xc.requests, "post", lambda *a, **kw: _Resp())
    match = XlateClient().translate("http://loinc.org", "4548-4")
    assert match["canonical_uri"] == (
        "https://termbank.pdhc.se/CodeSystem/loinc/4548-4"
    )
    assert match["mapping_quality"] == "equivalent"


def test_clean_miss_still_returns_none(monkeypatch):
    """``result: false`` is a mapping miss (422 xlate_miss), not an outage."""
    monkeypatch.setenv("XLATE_BASE_URL", "https://xlate.pdhc.se")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "resourceType": "Parameters",
                "parameter": [{"name": "result", "valueBoolean": False}],
            }

    monkeypatch.setattr(xc.requests, "post", lambda *a, **kw: _Resp())
    assert XlateClient().translate("urn:vendor-x", "ZX9") is None
