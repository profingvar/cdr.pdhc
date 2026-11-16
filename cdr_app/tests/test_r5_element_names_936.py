"""#936 — cdr reads the R5 element names as well as the R4 ones.

cdr read only `medicationCodeableConcept` and `performed[x]`. An R5-correct
MedicationStatement was stored with no canonical code, and an R5 Procedure
series (occurrence[x]) hashed every repeat of one (patient, code) to the same
dedup key and collapsed to a single row. Driven through the REAL canonicaliser
and the real write route, because the nested R5 write-back is the part a fake
would skip.
"""
import copy
from unittest.mock import MagicMock

import pytest

from app.models.resources import live_model
from app.services.canonicalisation import Canonicaliser
from app.services.resource_writer import _dedup_key, _extract_effective_at
from tests.test_fhir_write import _SERVICE_KEYS, clean_tables  # noqa: F401

ATC = {"system": "https://termbank.pdhc.se/CodeSystem/atc", "code": "A10BA02", "display": "metformin"}
CANON = "https://termbank.pdhc.se/CodeSystem/atc/A10BA02"
SNOMED_PROC = {"system": "https://termbank.pdhc.se/CodeSystem/snomed", "code": "80146002"}
PATIENT = "pat-aaaa-bbbb-cccc-ddddeeeeffff"

SHAPES = {
    "r4": {"medicationCodeableConcept": {"coding": [ATC]}},
    "r5": {"medication": {"concept": {"coding": [ATC]}}},
    "ips_bare": {"medication": {"coding": [ATC]}},
}


def _med(shape, eff="2026-04-01T10:00:00Z"):
    return {"resourceType": "MedicationStatement", "status": "recorded",
            "subject": {"reference": f"Patient/{PATIENT}"},
            "effectiveDateTime": eff, **copy.deepcopy(SHAPES[shape])}


def _proc(when, r5=True):
    key = "occurrenceDateTime" if r5 else "performedDateTime"
    return {"resourceType": "Procedure", "status": "completed",
            "subject": {"reference": f"Patient/{PATIENT}"},
            "code": {"coding": [SNOMED_PROC]}, key: when}


@pytest.fixture
def real_canon(app):
    plan = MagicMock()
    plan.validate_canonical.return_value = {"result": True}
    app._canonicaliser = Canonicaliser(xlate=MagicMock(), plan=plan)
    yield app._canonicaliser
    delattr(app, "_canonicaliser")


def _post(client, rt, body, rid):
    return client.post(f"/api/v1/fhir/{rt}", json=body, headers={
        "X-Org-Guid": "org-xyz", "X-Source-Service": "sim.pdhc",
        "X-Service-Key": _SERVICE_KEYS["sim.pdhc"], "X-Request-Id": rid})


# ---------------------------------------------------------------- medication

@pytest.mark.parametrize("shape", list(SHAPES))
def test_every_medication_shape_canonicalises_and_keeps_its_shape(real_canon, shape):
    body = _med(shape)
    before = copy.deepcopy(body)
    r = real_canon.canonicalise(body)
    assert r.status == "ok" and r.primary_canonical_uri == CANON
    assert body == before, "the caller's resource was mutated"
    if shape == "r5":
        assert r.rewritten["medication"]["concept"]["coding"][0]["code"] == "A10BA02"
        assert "medicationCodeableConcept" not in r.rewritten


@pytest.mark.parametrize("shape", list(SHAPES))
def test_every_medication_shape_is_STORED_with_its_canonical(client, app, real_canon, shape):
    r = _post(client, "MedicationStatement", _med(shape), f"req-{shape}")
    assert r.status_code == 201, r.get_json()
    row = live_model("MedicationStatement").query.one()
    assert row.medication_canonical == CANON
    assert row.code_canonical == CANON


def test_the_nested_write_back_does_not_touch_the_callers_resource(real_canon):
    """The termbank coding is SECOND, so canonicalising reorders the list --
    a change a mutated input would show. (With it first, the write-back is
    content-identical and a mutation is invisible.)"""
    body = _med("r5")
    body["medication"]["concept"]["coding"] = [{"system": "urn:local:x", "code": "met"}, ATC]
    before = copy.deepcopy(body)
    r = real_canon.canonicalise(body)
    assert r.rewritten["medication"]["concept"]["coding"][0]["code"] == "A10BA02"
    assert body == before


# ---------------------------------------------------------------- procedure

def test_an_R5_procedure_series_is_NOT_collapsed(client, app, real_canon):
    for i, when in enumerate(("2026-01-10T09:00:00Z", "2026-02-10T09:00:00Z", "2026-03-10T09:00:00Z")):
        assert _post(client, "Procedure", _proc(when), f"req-p{i}").status_code == 201
    rows = live_model("Procedure").query.all()
    assert len(rows) == 3
    assert sorted(r.performed_at.isoformat()[:10] for r in rows) == ["2026-01-10", "2026-02-10", "2026-03-10"]


def test_the_same_event_in_R4_and_R5_is_ONE_row(client, app, real_canon):
    _post(client, "Procedure", _proc("2026-01-10T09:00:00Z", r5=False), "req-r4")
    _post(client, "Procedure", _proc("2026-01-10T09:00:00Z", r5=True), "req-r5")
    assert live_model("Procedure").query.count() == 1


def test_r4_dedup_keys_are_UNCHANGED_by_the_fix():
    """Rows already stored keep their keys: R4 is read first and the hashed
    value is the same, so the pre-#936 formula still produces the stored key."""
    from app.services.resource_writer import _hash
    when = "2026-01-10T09:00:00Z"
    pre_936 = _hash([PATIENT, "c", when])
    key = lambda b: _dedup_key("Procedure", b, patient_guid=PATIENT, primary_canonical_uri="c")
    assert key(_proc(when, r5=False)) == pre_936
    assert key(_proc(when, r5=True)) == pre_936, "R4 and R5 spellings of one event share a key"


@pytest.mark.parametrize("key,value", [
    ("occurrenceDateTime", "2026-01-10T09:00:00Z"),
    ("occurrencePeriod", {"start": "2026-01-10T09:00:00Z", "end": "2026-01-10T10:00:00Z"}),
])
def test_effective_at_reads_the_R5_occurrence(key, value):
    at = _extract_effective_at({"resourceType": "Procedure", key: value}, "Procedure")
    assert at is not None and at.isoformat()[:10] == "2026-01-10"
