"""#942 — cdr implements termbank's canonical URI grammar; proven on termbank's corpus.

tests/conformance/canonical_uri_cases.json is a BYTE-IDENTICAL copy of
termbank.pdhc/conformance/canonical_uri_cases.json; the workspace check
tools/check_canonical_uri_conformance.py fails when they differ.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.api.stats import _classify
from app.services import plan_client as pc

CORPUS = json.loads((pathlib.Path(__file__).parent / "conformance" / "canonical_uri_cases.json").read_text())
TERMBANK = "https://termbank.pdhc.se"


@pytest.mark.parametrize("c", CORPUS["valid"], ids=lambda c: c["uri"])
def test_parse(c):
    assert pc.parse_canonical_uri(c["uri"]) == (c["lib"], c["code"])


@pytest.mark.parametrize("uri", CORPUS["invalid"])
def test_parse_refuses(uri):
    assert pc.parse_canonical_uri(uri) is None


@pytest.mark.parametrize("c", [c for c in CORPUS["valid"] if c["base"] == TERMBANK], ids=lambda c: c["uri"])
def test_both_builders(c):
    assert pc.build_canonical(c["lib"], c["code"]) == c["uri"]
    assert pc.canonical_uri_for(pc.canonical_system_for(c["lib"]), c["code"]) == c["uri"]


@pytest.mark.parametrize("c", CORPUS["systems_valid"], ids=lambda c: c["system"])
def test_systems(c):
    assert pc.parse_canonical_system(c["system"]) == c["lib"]


@pytest.mark.parametrize("s", CORPUS["systems_invalid"])
def test_systems_refused(s):
    assert pc.parse_canonical_system(s) is None


@pytest.mark.parametrize("c", CORPUS["valid"], ids=lambda c: c["uri"])
def test_stats_reads_every_valid_uri_as_an_external_code(c):
    r = _classify(c["uri"], lambda _c: None)
    assert (r["binding"], r["code_system_name"], r["code"]) == ("external_code", c["lib"], c["code"])


def test_the_builders_refuse_what_the_grammar_refuses():
    assert pc.build_canonical("loinc", "a b") is None
    assert pc.canonical_uri_for(f"{TERMBANK}/CodeSystem/loinc", "a b") is None
