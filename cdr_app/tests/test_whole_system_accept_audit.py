"""#872 — record WHY a code was accepted, not only that it was.

plan.pdhc's ``$validate-code`` normally answers from a Concept or
ValueCatalog row for that exact code. For a CLASSIFICATION — a canonical lib
with ``whole_system_answer_space`` — it answers ``result: true`` for ANY code
in the system, because ~85 000 ICD-11 codes picked at the point of care
cannot be pre-enumerated. plan says so on the wire with a ``whole_system``
boolean.

Measured 2026-11-01: **nothing read that boolean.** cdr's own
``_parse_parameters`` carried the key into a dict no code inspected, so an
ICD-11 diagnosis and a planned LOINC measurement were indistinguishable in
this repository's record of why each was accepted.

THE CONTROLS ARE THE POINT. Three tests here assert that a PER-CODE
acceptance records NOTHING. Without them, a bug that recorded every accepted
code would pass every other test in this file, and the audit table would
answer "on what basis?" with "all of them, always" — which is no answer.
"""
from unittest.mock import MagicMock

import pytest

from app import db
from app.models.resources import CdrAuditWholeSystemAccept
from app.services.canonicalisation import (
    Canonicaliser,
    CanonicalisationResult,
    WholeSystemAccept,
    record_whole_system_accept,
)

ICD11_URI = "https://termbank.pdhc.se/CodeSystem/icd11-mms/BA00.Z"
LOINC_URI = "https://termbank.pdhc.se/CodeSystem/loinc/4548-4"


def _plan(outcome):
    plan = MagicMock()
    plan.validate_canonical.return_value = outcome
    plan.resolve_concept.return_value = None
    return plan


def _canon(outcome, xlate=None):
    return Canonicaliser(xlate=xlate or MagicMock(), plan=_plan(outcome))


def _condition(uri=ICD11_URI):
    system, code = uri.rsplit("/", 1)
    return {
        "resourceType": "Condition",
        "code": {"coding": [{"system": system, "code": code,
                             "display": "Cholera, unspecified"}]},
    }


# ---------------------------------------------------------------------------
# The canonicaliser notices the basis
# ---------------------------------------------------------------------------

def test_a_WHOLE_SYSTEM_acceptance_is_recorded_on_the_result():
    canon = _canon({"result": True, "whole_system": True})
    result = canon.canonicalise(_condition())
    assert result.status == "ok"
    assert len(result.whole_system_accepts) == 1
    note = result.whole_system_accepts[0]
    assert note.canonical_uri == ICD11_URI
    assert note.canonical_lib_name == "icd11-mms"
    assert note.canonical_refnumber == "BA00.Z"
    assert note.location == "Condition.code.coding[0]"


def test_a_PER_CODE_acceptance_records_NOTHING():
    """THE CONTROL. plan found a row for this exact code, so there is no
    system-level assertion to record. If this list is ever non-empty the
    audit table stops distinguishing anything."""
    canon = _canon({"result": True})          # no whole_system key
    result = canon.canonicalise(_condition(LOINC_URI))
    assert result.status == "ok"
    assert result.whole_system_accepts == []


def test_whole_system_FALSE_also_records_nothing():
    """The second control: present-but-false must behave like absent."""
    canon = _canon({"result": True, "whole_system": False})
    result = canon.canonicalise(_condition())
    assert result.whole_system_accepts == []


def test_a_REFUSED_code_records_nothing_and_still_misses():
    """The third control: a plan_miss is not an acceptance. The write does
    not happen, so there is no basis to record."""
    canon = _canon({"result": False})
    result = canon.canonicalise(_condition())
    assert result.status == "plan_miss"
    assert result.whole_system_accepts == []
    assert len(result.misses) == 1


def test_TWO_code_paths_each_get_their_own_note():
    """Observation canonicalises both `code` and `valueCodeableConcept`, so
    a resource can carry two system-level acceptances with distinct
    locations."""
    canon = _canon({"result": True, "whole_system": True})
    system, code = ICD11_URI.rsplit("/", 1)
    obs = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": system, "code": code}]},
        "valueCodeableConcept": {"coding": [{"system": system, "code": code}]},
    }
    result = canon.canonicalise(obs)
    assert result.status == "ok"
    locations = sorted(n.location for n in result.whole_system_accepts)
    assert locations == ["Observation.code.coding[0]",
                         "Observation.valueCodeableConcept.coding[0]"]


def test_the_XLATE_translated_branch_also_notices():
    """The second accept site: a foreign coding translated by xlate and then
    accepted at system level. Easy to miss when wiring only the termbank
    branch — so it gets its own test."""
    xlate = MagicMock()
    xlate.translate.return_value = {
        "canonical_uri": ICD11_URI,
        "canonical_system": "icd11-mms",
        "canonical_code": "BA00.Z",
        "display": "Cholera, unspecified",
    }
    canon = _canon({"result": True, "whole_system": True}, xlate=xlate)
    foreign = {
        "resourceType": "Condition",
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                             "code": "A00.9"}]},
    }
    result = canon.canonicalise(foreign)
    assert result.status == "ok"
    assert [n.canonical_uri for n in result.whole_system_accepts] == [ICD11_URI]


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------

def _note(uri=ICD11_URI):
    return WholeSystemAccept(canonical_uri=uri, location="Condition.code.coding[0]",
                             canonical_lib_name="icd11-mms",
                             canonical_refnumber="BA00.Z")


def test_the_recorder_INSERTS_on_first_sighting(app):
    with app.app_context():
        CdrAuditWholeSystemAccept.query.delete()
        db.session.commit()
        record_whole_system_accept(db.session, _note(), request_id="req-1")
        db.session.commit()
        row = CdrAuditWholeSystemAccept.query.one()
        assert row.canonical_uri == ICD11_URI
        assert row.canonical_lib_name == "icd11-mms"
        assert row.canonical_refnumber == "BA00.Z"
        assert row.seen_count == 1
        assert row.last_request_id == "req-1"


def test_the_recorder_INCREMENTS_rather_than_duplicating(app):
    with app.app_context():
        CdrAuditWholeSystemAccept.query.delete()
        db.session.commit()
        for n in range(3):
            record_whole_system_accept(db.session, _note(), request_id=f"req-{n}")
            db.session.commit()
        row = CdrAuditWholeSystemAccept.query.one()
        assert row.seen_count == 3
        assert row.last_request_id == "req-2"
        assert row.last_seen_at >= row.first_seen_at


def test_the_recorder_ignores_an_EMPTY_uri(app):
    with app.app_context():
        CdrAuditWholeSystemAccept.query.delete()
        db.session.commit()
        record_whole_system_accept(db.session, _note(uri=""), request_id="req-x")
        db.session.commit()
        assert CdrAuditWholeSystemAccept.query.count() == 0


# ---------------------------------------------------------------------------
# Through the real write path — asserting the predicate is not asserting
# that the write path calls it
# ---------------------------------------------------------------------------

WRITE_HEADERS = {
    "X-Org-Guid": "org-xyz",
    "X-Source-Service": "sim.pdhc",
    "X-Service-Key": "test-sim-key",
}


class _CanonWithNote:
    """Fake canonicaliser that reports a system-level acceptance.

    ``canonical`` must differ between tests in this module: the writer
    deduplicates on (patient, code_canonical), so reusing one URI turns the
    second POST into a version bump and a 200 — which is how the control
    below first failed, on the test's own setup rather than on the code.
    """

    def __init__(self, notes, canonical=ICD11_URI):
        self.notes = notes
        self.canonical = canonical

    def canonicalise(self, fhir):
        return CanonicalisationResult(
            status="ok", rewritten=fhir,
            primary_canonical_uri=self.canonical,
            whole_system_accepts=self.notes,
        )


@pytest.fixture
def clean_audit(app):
    with app.app_context():
        CdrAuditWholeSystemAccept.query.delete()
        db.session.commit()
    yield


def test_the_WRITE_PATH_records_the_basis(app, client, clean_audit, monkeypatch):
    """#874 interacts with this, deliberately, and the permission below is the
    interaction made explicit rather than left as a coincidence.

    That gate refuses a SERVICE assertion of any code from a whole-system
    library, and `icd11-mms` is one — so without the grant this POST is a 403
    and the audit row can never be written by a service key. The two rules are
    consistent: the gate decides who may put such a code in, and this table
    records that it got in on SYSTEM grounds. Rows therefore come from human
    (SSO) writers and from services explicitly permitted here.
    """
    from app import auth
    monkeypatch.setattr(auth, "WHOLE_SYSTEM_ASSERTING_SERVICES",
                        {"icd11-mms": frozenset({"sim.pdhc"})})
    app._canonicaliser = _CanonWithNote([_note()])
    try:
        resp = client.post("/api/v1/fhir/Condition", json=_condition(),
                           headers={**WRITE_HEADERS, "X-Request-Id": "w-1"})
    finally:
        delattr(app, "_canonicaliser")
    assert resp.status_code == 201, resp.get_data(as_text=True)
    with app.app_context():
        row = CdrAuditWholeSystemAccept.query.one()
        assert row.canonical_uri == ICD11_URI
        assert row.last_request_id == "w-1"


def test_the_WRITE_PATH_records_NOTHING_for_a_per_code_accept(app, client, clean_audit):
    """The control on the wiring: same POST, same 201, no row. Without this
    a recorder called unconditionally would look correct above."""
    app._canonicaliser = _CanonWithNote([], canonical=LOINC_URI)
    try:
        resp = client.post("/api/v1/fhir/Condition", json=_condition(LOINC_URI),
                           headers={**WRITE_HEADERS, "X-Request-Id": "w-2"})
    finally:
        delattr(app, "_canonicaliser")
    assert resp.status_code == 201, resp.get_data(as_text=True)
    with app.app_context():
        assert CdrAuditWholeSystemAccept.query.count() == 0
