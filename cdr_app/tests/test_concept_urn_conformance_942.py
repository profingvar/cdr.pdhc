"""#942 — this reader implements plan.pdhc's concept reference format.

conformance/concept_urn_cases.json is a BYTE-IDENTICAL copy of
plan.pdhc/conformance/concept_urn_cases.json (workspace check:
tools/check_canonical_uri_conformance.py)."""
import json
import pathlib

import pytest

from app.api.clinical_read import _concept_guid_from_canonical
from app.api.stats import _canon_guid_fallback

CORPUS = json.loads((pathlib.Path(__file__).parent / "conformance" / "concept_urn_cases.json").read_text())


@pytest.mark.parametrize("c", CORPUS["guid_of"], ids=lambda c: repr(c["value"]))
def test_guid_of(c):
    assert _both(c["value"]) == c["guid"]


def _both(v):
    a, b = _concept_guid_from_canonical(v), _canon_guid_fallback(v)
    assert a == b, "the two cdr copies disagree"
    return a
