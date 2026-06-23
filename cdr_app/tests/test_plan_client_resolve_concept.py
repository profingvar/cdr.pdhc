"""Unit tests for PlanClient.resolve_concept after the 2026-06-23
migration to FHIR ConceptMap/$translate (ticket #258).

These tests mock the HTTP layer (requests.get) and verify:
1. The right URL is hit (plan.pdhc's $translate operation).
2. The right query params are sent (system = LOCAL_CS_URL, code = guid).
3. The Parameters response is parsed correctly.
4. The return dict has the same shape callers used to read.
5. No-match / HTTP-error / transient-retry paths return the expected
   values without breaking the canonicaliser's terminal-vs-transient
   semantics.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.plan_client import (
    PLAN_LOCAL_CS_URL,
    PlanClient,
    PlanUnreachable,
    _parse_translate_response,
)


HBA1C_GUID = "8c7f3a16-b482-4e2a-87f8-29c8d8c9c4d5"


def _success_body(system: str, code: str, display: str) -> dict:
    """Shape that plan.pdhc.fhir_conceptmap.scoped_validate_code emits
    for a single matched binding."""
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "result", "valueBoolean": True},
            {"name": "message", "valueString": "matched 1 target"},
            {
                "name": "match",
                "part": [
                    {"name": "relationship", "valueCode": "equivalent"},
                    {
                        "name": "concept",
                        "valueCoding": {
                            "system": system,
                            "code": code,
                            "display": display,
                        },
                    },
                ],
            },
        ],
    }


def _no_match_body() -> dict:
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "result", "valueBoolean": False},
            {"name": "message", "valueString": "no plan.pdhc Concept ..."},
        ],
    }


# ---------------------------------------------------------------------------
# _parse_translate_response helper
# ---------------------------------------------------------------------------
class TestParseTranslateResponse:
    def test_happy_path_pulls_coding(self):
        body = _success_body(
            "https://termbank.pdhc.se/CodeSystem/loinc", "4548-4", "HbA1c",
        )
        result, coding = _parse_translate_response(body)
        assert result is True
        assert coding == {
            "system": "https://termbank.pdhc.se/CodeSystem/loinc",
            "code": "4548-4",
            "display": "HbA1c",
        }

    def test_no_match_returns_false_none(self):
        result, coding = _parse_translate_response(_no_match_body())
        assert result is False
        assert coding is None

    def test_empty_body_returns_false_none(self):
        result, coding = _parse_translate_response({})
        assert result is False
        assert coding is None

    def test_match_without_concept_is_skipped(self):
        body = {
            "resourceType": "Parameters",
            "parameter": [
                {"name": "result", "valueBoolean": True},
                {"name": "match", "part": [{"name": "relationship",
                                              "valueCode": "equivalent"}]},
            ],
        }
        result, coding = _parse_translate_response(body)
        assert result is True
        assert coding is None


# ---------------------------------------------------------------------------
# PlanClient.resolve_concept — end-to-end with mocked HTTP
# ---------------------------------------------------------------------------
class TestResolveConceptViaTranslate:
    def _client(self):
        return PlanClient(base_url="https://plan.pdhc.example", cache_ttl=60)

    def test_happy_path_returns_expected_dict_shape(self):
        c = self._client()
        body = _success_body(
            "https://termbank.pdhc.se/CodeSystem/loinc", "4548-4", "HbA1c",
        )
        mock_resp = MagicMock(status_code=200, json=lambda: body)
        with patch("requests.get", return_value=mock_resp) as mg:
            r = c.resolve_concept(HBA1C_GUID)

        assert r is not None
        # Same return shape as the old /api/v1/concepts/<guid> path:
        assert r["guid"] == HBA1C_GUID
        assert r["system"] == "https://termbank.pdhc.se/CodeSystem/loinc"
        assert r["canonical_refnumber"] == "4548-4"
        assert r["canonical_uri"] == \
            "https://termbank.pdhc.se/CodeSystem/loinc/4548-4"
        assert r["display"] == "HbA1c"
        # Reverse-mapped from slug:
        assert r["canonical_lib_name"] == "LOINC"

        # And: HTTP hit was the new endpoint with the correct query params
        called_url = mg.call_args.args[0]
        called_params = mg.call_args.kwargs.get("params") or {}
        assert called_url.endswith("/api/v1/ConceptMap/$translate")
        assert called_params == {
            "system": PLAN_LOCAL_CS_URL,
            "code": HBA1C_GUID,
        }

    def test_no_match_returns_none(self):
        c = self._client()
        mock_resp = MagicMock(status_code=200, json=_no_match_body)
        with patch("requests.get", return_value=mock_resp):
            assert c.resolve_concept(HBA1C_GUID) is None

    def test_404_returns_none(self):
        c = self._client()
        mock_resp = MagicMock(status_code=404, json=lambda: {})
        with patch("requests.get", return_value=mock_resp):
            assert c.resolve_concept(HBA1C_GUID) is None

    def test_caches_within_ttl(self):
        c = self._client()
        body = _success_body(
            "https://termbank.pdhc.se/CodeSystem/snomed", "44054006", "T2DM",
        )
        mock_resp = MagicMock(status_code=200, json=lambda: body)
        with patch("requests.get", return_value=mock_resp) as mg:
            r1 = c.resolve_concept(HBA1C_GUID)
            r2 = c.resolve_concept(HBA1C_GUID)
        assert r1 == r2
        assert mg.call_count == 1  # second call hit the cache

    def test_unknown_slug_yields_name_none_but_other_fields_present(self):
        """A canonical_lib_url whose slug isn't in _LIB_SLUG: callers
        still get the rest of the data; canonical_lib_name is None."""
        c = self._client()
        body = _success_body(
            "https://termbank.pdhc.se/CodeSystem/some-new-lib",
            "X-1", "Whatever",
        )
        mock_resp = MagicMock(status_code=200, json=lambda: body)
        with patch("requests.get", return_value=mock_resp):
            r = c.resolve_concept(HBA1C_GUID)
        assert r is not None
        assert r["canonical_refnumber"] == "X-1"
        assert r["canonical_uri"] == \
            "https://termbank.pdhc.se/CodeSystem/some-new-lib/X-1"
        assert r["canonical_lib_name"] is None  # slug not in _LIB_SLUG

    def test_unreachable_propagates_PlanUnreachable(self):
        """Transient-retry exhaustion still raises PlanUnreachable so
        the writer returns 503 not plan_miss."""
        import requests as _requests
        c = self._client()
        with patch("requests.get",
                   side_effect=_requests.RequestException("boom")):
            with pytest.raises(PlanUnreachable):
                c.resolve_concept(HBA1C_GUID)
