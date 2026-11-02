"""#857 / #874 — a service key may not assert a code from a WHOLE-SYSTEM
answer space, and the gate asks plan.pdhc which libraries those are.

plan.pdhc's `$validate-code` answers TRUE for ANY code in a library flagged
`whole_system_answer_space`, including one that does not exist: plan holds no
copy of an 85 000-code classification chosen at the point of care, so the
per-code question has no meaningful answer there. That is correct, and it MOVES
the existence check rather than removing it. The exposure is beside that path —
`KNOWN_FHIR_SERVICES` gates FHIR WRITES as well as reads (#760) and mints an
SU-equivalent blob, so any identity on it could POST a Condition with a
fabricated code and this CDR would store it.

**Why this file was rewritten.** #857 shipped a gate that named ONE library,
`icd11-mms`. #872 flagged `icd10` twelve days later and the gate did not
notice — the defect #874 was filed for. Two of the tests here previously
asserted the opposite of what the platform now does, most pointedly
`test_ICD10_is_NOT_caught`. They are rewritten rather than deleted: the old
assertions recorded a real decision, and the new ones record its reversal.

The gate now resolves a coding's library by asking plan, with
`WHOLE_SYSTEM_LIBS_FLOOR` as the level it cannot regress below.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask import g

from app import auth
from app.auth import (
    ICD11_CANONICAL_LIB,
    ICD11_WHO_SYSTEM,
    WHOLE_SYSTEM_ASSERTING_SERVICES,
    WHOLE_SYSTEM_LIBS_FLOOR,
    whole_system_assertion_refused_for,
)

TERMBANK = "https://termbank.pdhc.se/CodeSystem"
ICD11_SYSTEM = f"{TERMBANK}/{ICD11_CANONICAL_LIB}"
ICD10_SYSTEM = f"{TERMBANK}/icd10"
SNOMED_SYSTEM = "http://snomed.info/sct"

#: The real duplicate measured in production 2026-11-02 (#875): icd10 carries
#: a copy of snomed's URL.
SHARED_URL = ("https://browser.ihtsdotools.org/?perspective=full"
              "&conceptId1=404684003&edition=MAIN/SNOMEDCT")


def _condition(system: str, code: str = "1A00") -> dict:
    return {
        "resourceType": "Condition",
        "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"system": system, "code": code}]},
    }


def _plan_says(monkeypatch, systems):
    """Make the gate see `systems` as plan's answer. None = plan is silent."""
    monkeypatch.setattr(
        auth, "_plan_client",
        lambda: SimpleNamespace(whole_system_systems=lambda: systems))


def _as(app, source: str | None, fhir: dict) -> str | None:
    """Evaluate the rule as `source` would see it. None = a human caller."""
    with app.test_request_context():
        g.access_blob = {"service_source": source} if source else {}
        return whole_system_assertion_refused_for(fhir)


# =======================================================================
# The floor — what the gate does when plan does not answer
# =======================================================================

def test_the_asserting_map_is_EMPTY_and_that_is_the_point(app):
    """Not a placeholder. icd11.pdhc is not in KNOWN_FHIR_SERVICES at all, so
    "only icd11.pdhc may" and "nobody may" are currently the same rule."""
    assert WHOLE_SYSTEM_ASSERTING_SERVICES == {}


def test_the_floor_holds_icd11(app):
    assert ICD11_CANONICAL_LIB in WHOLE_SYSTEM_LIBS_FLOOR


@pytest.mark.parametrize("system", [ICD11_WHO_SYSTEM, ICD11_SYSTEM])
def test_plan_SILENT_still_refuses_icd11(app, monkeypatch, system):
    """The fail-closed case, and the reason the floor exists: an unreachable or
    misconfigured plan must not disarm the gate while writes still succeed."""
    _plan_says(monkeypatch, None)
    msg = _as(app, "sim.pdhc", _condition(system))
    assert msg is not None
    assert "sim.pdhc" in msg and ICD11_CANONICAL_LIB in msg


def test_plan_UNFLAGGING_icd11_cannot_narrow_the_floor(app, monkeypatch):
    """plan may widen this set; it may not shrink it below what #857 shipped."""
    _plan_says(monkeypatch, {"loinc": "loinc"})
    assert _as(app, "sim.pdhc", _condition(ICD11_WHO_SYSTEM)) is not None


# =======================================================================
# Driven by plan — the actual #874 fix
# =======================================================================

def test_ICD10_IS_caught_once_plan_FLAGS_it(app, monkeypatch):
    """REVERSES `test_ICD10_is_NOT_caught`. #872 flipped icd10 on 2026-11-02;
    the whole point of #874 is that the gate follows without an edit here."""
    _plan_says(monkeypatch, {"icd10": "icd10", ICD11_CANONICAL_LIB: ICD11_CANONICAL_LIB})
    msg = _as(app, "sim.pdhc", _condition(ICD10_SYSTEM, "A00.9"))
    assert msg is not None
    assert "icd10" in msg


def test_ICD10_is_NOT_caught_while_plan_leaves_it_UNFLAGGED(app, monkeypatch):
    """The control for the test above, and what #857's world looked like. It is
    the SAME code and the SAME caller; only plan's answer differs."""
    _plan_says(monkeypatch, {ICD11_CANONICAL_LIB: ICD11_CANONICAL_LIB})
    assert _as(app, "sim.pdhc", _condition(ICD10_SYSTEM, "A00.9")) is None


def test_an_UNFLAGGED_library_passes_even_when_others_are_flagged(app, monkeypatch):
    """Without this the tests above would also pass for a gate that refuses
    every service write outright."""
    _plan_says(monkeypatch, {"icd10": "icd10", ICD11_CANONICAL_LIB: ICD11_CANONICAL_LIB})
    assert _as(app, "sim.pdhc", _condition(SNOMED_SYSTEM, "73211009")) is None
    assert _as(app, "sim.pdhc", _condition("http://loinc.org", "41653-7")) is None


def test_a_FABRICATED_code_is_refused_the_same_way(app, monkeypatch):
    """The actual hazard: plan would answer `true` for this."""
    _plan_says(monkeypatch, None)
    assert _as(app, "gateway.pdhc",
               _condition(ICD11_WHO_SYSTEM, "ZZ-nonsense")) is not None


# =======================================================================
# Resolution — exact, never substring
# =======================================================================

@pytest.mark.parametrize("system", [
    "http://example.org/icd10-lookalike",
    "https://termbank.pdhc.se/CodeSystem/icd10-extra/A00",
    "icd10-se",
    "https://termbank.pdhc.se/ValueSet/icd10",
])
def test_a_LOOKALIKE_system_is_not_caught(app, monkeypatch, system):
    """The predecessor matched `f"/{lib}/" in system`. A substring test cannot
    tell `icd10` from a system that merely contains it — the defect #804
    recorded in gateway's path guard."""
    _plan_says(monkeypatch, {"icd10": "icd10"})
    assert _as(app, "sim.pdhc", _condition(system, "A00.9")) is None


def test_the_library_NAME_alone_is_accepted_as_a_system(app, monkeypatch):
    """plan's own global/cdr spelling: `system=icd10`, not a URL."""
    _plan_says(monkeypatch, {"icd10": "icd10"})
    assert _as(app, "sim.pdhc", _condition("icd10", "A00.9")) is not None


def test_a_UNIQUE_canonical_lib_url_is_accepted_as_a_system(app, monkeypatch):
    _plan_says(monkeypatch, {"icd11-mms": "icd11-mms",
                             ICD11_SYSTEM: "icd11-mms"})
    assert _as(app, "sim.pdhc", _condition(ICD11_SYSTEM)) is not None


def test_a_coding_NESTED_anywhere_is_still_found(app, monkeypatch):
    """`canonicalise()` only walks `_CODE_PATHS` — Condition.code and friends.
    A classification code under bodySite, severity, stage.summary or
    evidence.code never reaches it, which is why this gate is broader than the
    canonicaliser rather than reading its result."""
    _plan_says(monkeypatch, None)
    fhir = {
        "resourceType": "Condition",
        "code": {"coding": [{"system": SNOMED_SYSTEM, "code": "x"}]},
        "bodySite": [{"coding": [{"system": ICD11_WHO_SYSTEM, "code": "1A00"}]}],
    }
    assert _as(app, "sim.pdhc", fhir) is not None


def test_a_BUNDLE_entry_is_reached(app, monkeypatch):
    _plan_says(monkeypatch, None)
    bundle = {
        "resourceType": "Bundle", "type": "transaction",
        "entry": [{"resource": _condition(ICD11_WHO_SYSTEM)}],
    }
    assert _as(app, "sim.pdhc", bundle) is not None


# =======================================================================
# Policy — who may assert, per library
# =======================================================================

def test_a_HUMAN_caller_is_not_touched(app, monkeypatch):
    """The rule is about machine identities holding an SU-equivalent blob."""
    _plan_says(monkeypatch, None)
    assert _as(app, None, _condition(ICD11_WHO_SYSTEM)) is None


def test_an_ALLOWED_service_may_assert(app, monkeypatch):
    """Proves the map is wired, so adding icd11.pdhc later actually does
    something. Without this, the empty map and a hardcoded refusal are
    indistinguishable."""
    _plan_says(monkeypatch, None)
    monkeypatch.setattr(auth, "WHOLE_SYSTEM_ASSERTING_SERVICES",
                        {ICD11_CANONICAL_LIB: frozenset({"icd11.pdhc"})})
    assert _as(app, "icd11.pdhc", _condition(ICD11_WHO_SYSTEM)) is None
    assert _as(app, "sim.pdhc", _condition(ICD11_WHO_SYSTEM)) is not None


def test_permission_is_PER_LIBRARY_not_global(app, monkeypatch):
    """icd11.pdhc validates ICD-11 against WHO at entry. That earns it nothing
    for ICD-10-SE, which has no equivalent entry-point validator."""
    _plan_says(monkeypatch, {"icd10": "icd10",
                             ICD11_CANONICAL_LIB: ICD11_CANONICAL_LIB})
    monkeypatch.setattr(auth, "WHOLE_SYSTEM_ASSERTING_SERVICES",
                        {ICD11_CANONICAL_LIB: frozenset({"icd11.pdhc"})})
    assert _as(app, "icd11.pdhc", _condition(ICD11_WHO_SYSTEM)) is None
    msg = _as(app, "icd11.pdhc", _condition(ICD10_SYSTEM, "A00.9"))
    assert msg is not None and "icd10" in msg


def test_the_message_names_the_library_the_caller_and_where_to_look(app, monkeypatch):
    """A refusal nobody can act on costs more than it saves."""
    _plan_says(monkeypatch, None)
    msg = _as(app, "sim.pdhc", _condition(ICD11_WHO_SYSTEM))
    assert "sim.pdhc" in msg
    assert ICD11_CANONICAL_LIB in msg
    assert "WHOLE_SYSTEM_ASSERTING_SERVICES" in msg
    assert "no service" in msg
    assert "#874" in msg


# =======================================================================
# PlanClient.whole_system_systems — what it builds from plan's payload
# =======================================================================

class _Resp:
    def __init__(self, status=200, payload=None, text="{}"):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is _BAD_JSON:
            raise ValueError("not json")
        return self._payload


_BAD_JSON = object()


def _client(monkeypatch, resp):
    from app.services.plan_client import PlanClient
    c = PlanClient(base_url="http://plan.invalid")
    monkeypatch.setattr(c, "_get_with_retry", lambda *a, **k: resp)
    return c


def _row(name, flag, url=None):
    return {"canonical_lib_name": name, "whole_system_answer_space": flag,
            "canonical_lib_url": url}


def test_only_FLAGGED_libraries_are_returned(app, monkeypatch):
    c = _client(monkeypatch, _Resp(payload=[
        _row("icd10", True), _row("icd11-mms", True), _row("loinc", False),
        _row("snomed", False),
    ]))
    assert c.whole_system_systems() == {"icd10": "icd10",
                                        "icd11-mms": "icd11-mms"}


def test_a_UNIQUE_url_is_included_under_its_library(app, monkeypatch):
    c = _client(monkeypatch, _Resp(payload=[
        _row("icd11-mms", True, ICD11_SYSTEM), _row("loinc", False, "https://LOINC.org"),
    ]))
    got = c.whole_system_systems()
    assert got == {"icd11-mms": "icd11-mms", ICD11_SYSTEM: "icd11-mms"}


def test_a_SHARED_url_is_DROPPED_so_snomed_is_never_read_as_icd10(app, monkeypatch):
    """#875, measured in production: icd10 holds a copy of snomed's URL. Without
    this, a SNOMED coding sent with that system would resolve to a FLAGGED
    library and legitimate SNOMED writes would be refused."""
    c = _client(monkeypatch, _Resp(payload=[
        _row("snomed", False, SHARED_URL),
        _row("icd10", True, SHARED_URL),
    ]))
    got = c.whole_system_systems()
    assert got == {"icd10": "icd10"}
    assert SHARED_URL not in got


def test_the_gate_does_not_refuse_a_snomed_coding_sent_with_the_shared_url(app, monkeypatch):
    """The same collision, end to end through the gate."""
    _plan_says(monkeypatch, {"icd10": "icd10"})   # url deliberately absent
    assert _as(app, "sim.pdhc", _condition(SHARED_URL, "73211009")) is None


@pytest.mark.parametrize("resp", [
    _Resp(status=404, payload=None),
    _Resp(status=500, payload=None),
    _Resp(payload=_BAD_JSON),
    _Resp(payload={"not": "a list"}),
])
def test_an_UNUSABLE_answer_is_None_not_an_empty_set(app, monkeypatch, resp):
    """None means "plan did not answer". An empty dict would mean "no library is
    flagged", which the gate would read as permission."""
    assert _client(monkeypatch, resp).whole_system_systems() is None


def test_plan_UNREACHABLE_is_None_and_is_NOT_cached(app, monkeypatch):
    from app.services.plan_client import PlanClient, PlanUnreachable
    c = PlanClient(base_url="http://plan.invalid")
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise PlanUnreachable("refused")

    monkeypatch.setattr(c, "_get_with_retry", boom)
    assert c.whole_system_systems() is None
    assert c.whole_system_systems() is None
    assert len(calls) == 2, "a blip must not pin 'unknown' for the whole TTL"


def test_a_GOOD_answer_IS_cached(app, monkeypatch):
    from app.services.plan_client import PlanClient
    c = PlanClient(base_url="http://plan.invalid")
    calls = []

    def once(*a, **k):
        calls.append(1)
        return _Resp(payload=[_row("icd10", True)])

    monkeypatch.setattr(c, "_get_with_retry", once)
    assert c.whole_system_systems() == {"icd10": "icd10"}
    assert c.whole_system_systems() == {"icd10": "icd10"}
    assert len(calls) == 1, "one GET per TTL, not one per request"


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
    assert "may not assert a 'icd11-mms' code" in text


def test_the_WRITE_PATH_does_not_403_a_PERMITTED_service(client, monkeypatch):
    """The control: proves the 403 above comes from THIS gate and not from
    something else on the write path. The request may well fail further down
    (canonicalisation is not stubbed here); it must not fail at 403."""
    monkeypatch.setattr(auth, "WHOLE_SYSTEM_ASSERTING_SERVICES",
                        {ICD11_CANONICAL_LIB: frozenset({"sim.pdhc"})})
    resp = client.post(
        "/api/v1/fhir/Condition",
        json=_condition(ICD11_WHO_SYSTEM),
        headers={"X-Org-Guid": "org-xyz",
                 "X-Source-Service": "sim.pdhc",
                 "X-Service-Key": "test-sim-key"},
    )
    assert resp.status_code != 403
