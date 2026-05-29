"""Regression test for the canonicaliser plan_miss fall-through bug
(found 2026-05-29 while wiring sim → cdr_6).

Before the fix, a coding whose system was the plan.pdhc/Concept
indirection AND whose GUID didn't resolve in plan.pdhc was appended
to the misses list with `continue`. Execution then fell through to
the termbank section and finally to the xlate hop — so a "plan GUID
not found" outcome would 503 the moment xlate happened to be down,
even though the canonicaliser had everything it needed to return a
clean 422 plan_miss.

The fix makes the plan_miss terminal: append + return.
"""
from unittest.mock import MagicMock

import pytest

from app.services.canonicalisation import (
    Canonicaliser, PLAN_CONCEPT_SYSTEM,
)


class _XlateMustNotBeCalled:
    """Sentinel xlate client whose translate() blows up loudly. If the
    canonicaliser falls through to xlate on a plan_miss, this test
    fails with a clear message instead of a 503 we'd have to chase."""
    def translate(self, *args, **kwargs):
        raise AssertionError(
            "xlate.translate called for a plan.pdhc/Concept coding whose "
            "GUID was a plan_miss — the canonicaliser should have returned "
            "plan_miss terminally without falling through to xlate."
        )


def _make_canonicaliser(plan_returns):
    plan = MagicMock()
    plan.resolve_concept.return_value = plan_returns
    plan.validate_canonical.return_value = None
    return Canonicaliser(xlate=_XlateMustNotBeCalled(), plan=plan)


def test_plan_concept_miss_is_terminal_no_xlate_fallthrough():
    """A plan.pdhc/Concept coding with an unknown GUID returns plan_miss
    without calling xlate."""
    canon = _make_canonicaliser(plan_returns=None)
    observation = {
        "resourceType": "Observation",
        "status": "final",
        "code": {
            "coding": [
                {
                    "system": PLAN_CONCEPT_SYSTEM,
                    "code": "deadbeef-1111-2222-3333-444455556666",
                    "display": "Unknown",
                },
            ],
        },
    }
    result = canon.canonicalise(observation)
    assert result.status == "plan_miss"
    assert len(result.misses) == 1
    miss = result.misses[0]
    assert miss.kind == "plan_miss"
    assert miss.foreign_system == PLAN_CONCEPT_SYSTEM
    assert miss.foreign_code == "deadbeef-1111-2222-3333-444455556666"


def test_plan_concept_resolved_returns_ok_with_termbank_canonical():
    """Belt-and-braces: the happy path still resolves to a termbank
    canonical (no regression from the fall-through fix)."""
    canon = _make_canonicaliser(plan_returns={
        "canonical_uri": "https://termbank.pdhc.se/CodeSystem/loinc/467447",
        "system": "https://termbank.pdhc.se/CodeSystem/loinc",
        "canonical_refnumber": "467447",
        "canonical_lib_name": "loinc",
        "display": "FEV1",
        "guid": "6521528c-db59-45c5-a492-003c28f27623",
    })
    observation = {
        "resourceType": "Observation",
        "status": "final",
        "code": {
            "coding": [{
                "system": PLAN_CONCEPT_SYSTEM,
                "code": "6521528c-db59-45c5-a492-003c28f27623",
                "display": "FEV1",
            }],
        },
    }
    result = canon.canonicalise(observation)
    assert result.status == "ok"
    assert result.primary_canonical_uri == "https://termbank.pdhc.se/CodeSystem/loinc/467447"
    # coding[0] is the termbank canonical, original preserved at [1].
    rewritten = result.rewritten["code"]["coding"]
    assert rewritten[0]["system"] == "https://termbank.pdhc.se/CodeSystem/loinc"
    assert rewritten[0]["code"] == "467447"
    assert rewritten[1]["system"] == PLAN_CONCEPT_SYSTEM
