"""#787.2 — one concept identifier, one written spelling.

cdr accepts two serialisations of the same concept GUID:

    urn:pdhc:concept/<guid>                      <- canonical
    https://plan.pdhc.se/api/v1/concepts/<guid>  <- legacy alias

gateway dual-emits both (the #294 alias window), so both must stay ACCEPTED on
read. The question #787 asked was which one gets WRITTEN, and the answer was
previously "whichever the producer listed first" — a membership test over
`_CANONICAL_CODE_SYSTEMS`, whose tuple order meant nothing.

Why that matters rather than being untidy: `fhir_read._filter_by_code`
EQUALITY-matches `code_canonical`. If gateway ever reordered its two codings,
cdr would begin writing the other spelling for the same concept, and every row
written before the reorder would silently stop matching queries built after
it. One identifier, two strings, no error anywhere.

Measured on production 2026-10-13, after seeding: cdr1 1,942 / cdr2 19,490 /
cdr3 19,509 rows with `code_canonical`, **100% in the urn form, zero in the
other**. So the canonical choice matches the data; this pins it so it stays
matching by decision rather than by the producer's array order.
"""
from __future__ import annotations

from app.services.ingest_pipeline import (
    _primary_canonical_uri,
    _CONCEPT_SYSTEM_CANONICAL,
    _CONCEPT_SYSTEM_LEGACY,
    _CANONICAL_CODE_SYSTEMS,
)

GUID = "0139525f-fbc7-427e-a508-cf33c4c681eb"


def _obs(*systems):
    return {"resourceType": "Observation",
            "code": {"coding": [{"system": s, "code": GUID} for s in systems]}}


def test_the_canonical_spelling_wins_when_listed_first():
    """gateway's current order. This passed before the fix too."""
    uri = _primary_canonical_uri(_obs(_CONCEPT_SYSTEM_CANONICAL,
                                      _CONCEPT_SYSTEM_LEGACY))
    assert uri == f"urn:pdhc:concept/{GUID}"


def test_the_canonical_spelling_ALSO_wins_when_listed_SECOND():
    """The fix. Before #787 this returned the legacy URL, purely because the
    producer happened to list it first — and nothing in cdr expressed a
    preference. This is the test that would have caught a gateway reorder."""
    uri = _primary_canonical_uri(_obs(_CONCEPT_SYSTEM_LEGACY,
                                      _CONCEPT_SYSTEM_CANONICAL))
    assert uri == f"urn:pdhc:concept/{GUID}", (
        "the written spelling still depends on the producer's coding order")


def test_the_legacy_spelling_is_still_accepted_when_it_is_all_there_is():
    """It must keep working: gateway is mid-#294 and a producer that sends only
    the URL form must not silently lose its concept reference."""
    uri = _primary_canonical_uri(_obs(_CONCEPT_SYSTEM_LEGACY))
    assert uri == f"{_CONCEPT_SYSTEM_LEGACY}/{GUID}"


def test_LOINC_ordering_is_NOT_re_ranked_by_this_change():
    """Deliberately unchanged. LOINC is a real external code system, not a
    second spelling of ours, so which of LOINC-vs-PDHC wins still follows the
    producer's array order. Re-ranking it would have been an unrequested
    behaviour change riding along with #787."""
    assert _primary_canonical_uri(
        _obs("http://loinc.org", _CONCEPT_SYSTEM_CANONICAL)
    ) == f"http://loinc.org/{GUID}"
    assert _primary_canonical_uri(
        _obs(_CONCEPT_SYSTEM_CANONICAL, "http://loinc.org")
    ) == f"urn:pdhc:concept/{GUID}"


def test_an_unknown_system_is_ignored():
    assert _primary_canonical_uri(_obs("http://example.org/whatever")) is None


def test_a_coding_with_no_code_is_not_emitted_as_a_bare_system():
    """`urn:pdhc:concept/` with nothing after it would equality-match nothing
    and look like a real value in the column."""
    obs = {"code": {"coding": [{"system": _CONCEPT_SYSTEM_CANONICAL}]}}
    assert _primary_canonical_uri(obs) is None


def test_no_code_block_at_all():
    assert _primary_canonical_uri({"resourceType": "Observation"}) is None
    assert _primary_canonical_uri({"code": {}}) is None


def test_both_spellings_remain_in_the_ACCEPTED_set():
    """Picking a canonical spelling must not drop the other from reads while
    gateway still dual-emits it (#294)."""
    assert _CONCEPT_SYSTEM_CANONICAL in _CANONICAL_CODE_SYSTEMS
    assert _CONCEPT_SYSTEM_LEGACY in _CANONICAL_CODE_SYSTEMS


def test_the_read_side_parses_BOTH_spellings_back_to_the_same_guid():
    """The round trip that makes the legacy alias harmless on read: whichever
    spelling is stored, the concept guid recovered from it is identical."""
    from app.api.clinical_read import _concept_guid_from_canonical
    assert _concept_guid_from_canonical(
        f"urn:pdhc:concept/{GUID}") == GUID
    assert _concept_guid_from_canonical(
        f"{_CONCEPT_SYSTEM_LEGACY}/{GUID}") == GUID
