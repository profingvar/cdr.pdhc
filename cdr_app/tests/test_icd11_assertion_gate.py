"""#857 — a service key may not assert an ICD-11 diagnosis.

plan.pdhc's `$validate-code` answers TRUE for ANY `icd11-mms` code, including
one that does not exist, because `icd11-mms` is a whole-system answer space:
plan holds no copy of an 85 000-code classification chosen at the point of
care, so the per-code question has no meaningful answer there. Measured
2026-10-21:

    system=icd11-mms&code=1A00         -> true
    system=icd11-mms&code=ZZ-nonsense  -> TRUE
    system=icd10&code=A00.9            -> false     (the control)

That is correct, and it MOVES the existence check rather than removing it:
per-code existence now rests on icd11.pdhc validating against WHO at entry.
The exposure is beside that path. `KNOWN_FHIR_SERVICES` gates FHIR WRITES as
well as reads (#760) and mints an SU-equivalent blob, so any identity on it
could POST a Condition with a fabricated ICD-11 code and this CDR would store
it.

The set of services permitted to assert one is EMPTY, which is not a
placeholder: measured 2026-10-31 across cdr1/2/3, `fhir_resources` holds zero
Condition rows, and icd11.pdhc is not in KNOWN_FHIR_SERVICES at all. So
"only icd11.pdhc may" and "nobody may" are currently the same rule.
"""
from __future__ import annotations

import pytest
from flask import g

from app.auth import (
    ICD11_ASSERTING_SERVICES,
    ICD11_CANONICAL_LIB,
    ICD11_WHO_SYSTEM,
    icd11_assertion_refused_for,
)

TERMBANK_ICD11 = f"https://termbank.pdhc.se/CodeSystem/{ICD11_CANONICAL_LIB}/1A00"


def _condition(system: str, code: str = "1A00") -> dict:
    return {
        "resourceType": "Condition",
        "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"system": system, "code": code}]},
    }


def _as(app, source: str | None, fhir: dict) -> str | None:
    """Evaluate the rule as `source` would see it. None = a human caller."""
    with app.test_request_context():
        g.access_blob = {"service_source": source} if source else {}
        return icd11_assertion_refused_for(fhir)


# =======================================================================
# The rule
# =======================================================================

def test_the_allowlist_is_EMPTY_and_that_is_the_point(app):
    assert ICD11_ASSERTING_SERVICES == frozenset()


@pytest.mark.parametrize("system", [ICD11_WHO_SYSTEM, TERMBANK_ICD11])
def test_a_service_may_not_assert_an_ICD11_code(app, system):
    """Both spellings. WHO's own system URI arrives on a raw FHIR resource;
    the canonical-library slug arrives inside a termbank canonical URI, and
    only one of them looks like ICD-11 at a glance."""
    msg = _as(app, "sim.pdhc", _condition(system))
    assert msg is not None
    assert "sim.pdhc" in msg
    assert "#857" in msg


def test_a_FABRICATED_code_is_refused_the_same_way(app):
    """The actual hazard: plan would answer `true` for this."""
    assert _as(app, "gateway.pdhc",
               _condition(ICD11_WHO_SYSTEM, "ZZ-nonsense")) is not None


def test_a_coding_NESTED_anywhere_is_still_found(app):
    """A Condition carries codings under bodySite, severity, stage.summary
    and evidence.code too — reading a fixed list of paths is how a guard ends
    up checking the one place the producer did not use."""
    fhir = {
        "resourceType": "Condition",
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "x"}]},
        "bodySite": [{"coding": [{"system": ICD11_WHO_SYSTEM, "code": "1A00"}]}],
    }
    assert _as(app, "sim.pdhc", fhir) is not None


def test_a_BUNDLE_entry_is_reached(app):
    bundle = {
        "resourceType": "Bundle", "type": "transaction",
        "entry": [{"resource": _condition(ICD11_WHO_SYSTEM)}],
    }
    assert _as(app, "sim.pdhc", bundle) is not None


# =======================================================================
# Controls — without these the tests above only prove nothing is allowed
# =======================================================================

def test_a_NON_ICD11_coding_from_the_same_service_passes(app):
    assert _as(app, "sim.pdhc",
               _condition("http://loinc.org", "41653-7")) is None


def test_ICD10_is_NOT_caught(app):
    """icd10 is deliberately not a whole-system library in plan, so its
    per-code answer is real and this rule has no business touching it."""
    assert _as(app, "sim.pdhc",
               _condition("https://termbank.pdhc.se/CodeSystem/icd10/A00.9")) is None


def test_a_HUMAN_caller_is_not_touched(app):
    """The rule is about machine identities holding an SU-equivalent blob."""
    assert _as(app, None, _condition(ICD11_WHO_SYSTEM)) is None


def test_an_ALLOWED_service_may_assert(app, monkeypatch):
    """Proves the allowlist is wired, so adding icd11.pdhc later actually
    does something. Without this the empty set and a hardcoded refusal are
    indistinguishable."""
    monkeypatch.setattr("app.auth.ICD11_ASSERTING_SERVICES",
                        frozenset({"icd11.pdhc"}))
    assert _as(app, "icd11.pdhc", _condition(ICD11_WHO_SYSTEM)) is None
    assert _as(app, "sim.pdhc", _condition(ICD11_WHO_SYSTEM)) is not None


# =======================================================================
# Wired into the write path, not merely defined
# =======================================================================

def test_the_WRITE_PATH_refuses_with_403(client):
    """Asserting the predicate is not asserting the guard runs."""
    resp = client.post(
        "/api/v1/fhir/Condition",
        json=_condition(ICD11_WHO_SYSTEM),
        headers={"X-Org-Guid": "org-xyz",
                 "X-Source-Service": "sim.pdhc",
                 "X-Service-Key": "test-sim-key"},
    )
    assert resp.status_code == 403
    text = resp.get_json()["issue"][0]["details"]["text"]
    assert "may not assert an ICD-11 code" in text
