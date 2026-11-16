"""Where a resource keeps its medication and its occurrence, in R4 AND R5 (#936).

The platform declares FHIR R5, and cdr read only the R4 names. An R5-correct
resource was accepted and stored WITHOUT a canonical code (medication), or
with no time at all (procedure), which also made every repeat of one
(patient, code) hash to the same dedup key and collapse to one row.

So every reader asks this module rather than naming a field. Three
medication shapes are in circulation, and all three are read:

  medicationCodeableConcept: {coding}    R4; sim and most producers today
  medication: {concept: {coding}}        R5 CodeableReference, the correct one
  medication: {coding}                   a bare CodeableConcept under the R5
                                         name: not conformant, but it is what
                                         ips writes today, so refusing it would
                                         drop real data

R4 is checked first, so a resource that already canonicalised keeps exactly
the same answer (and the same dedup key) as before.
"""
from __future__ import annotations

R4_MEDICATION = ("medicationCodeableConcept",)
R5_MEDICATION = ("medication", "concept")
BARE_MEDICATION = ("medication",)


def medication_path(fhir: dict) -> tuple[str, ...] | None:
    """Key path to the medication's CodeableConcept, or None."""
    if isinstance(fhir.get("medicationCodeableConcept"), dict):
        return R4_MEDICATION
    med = fhir.get("medication")
    if isinstance(med, dict):
        if isinstance(med.get("concept"), dict):
            return R5_MEDICATION
        if "coding" in med or "text" in med:
            return BARE_MEDICATION
    return None


def get_path(fhir: dict, path: tuple[str, ...] | None):
    node = fhir
    for key in path or ():
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if path else None


def set_path(fhir: dict, path: tuple[str, ...], value) -> None:
    """Set a nested value, COPYING each dict on the way so the caller's input
    is never mutated (the canonicaliser works on a shallow copy)."""
    node = fhir
    for key in path[:-1]:
        child = dict(node.get(key) or {})
        node[key] = child
        node = child
    node[path[-1]] = value


def medication_concept(fhir: dict) -> dict:
    return get_path(fhir, medication_path(fhir)) or {}


def procedure_occurrence(fhir: dict):
    """The Procedure's time as given: R4 performed[x] first, then R5
    occurrence[x]. Used for the dedup key, so the SAME value is returned
    whichever name carried it."""
    return (fhir.get("performedDateTime") or fhir.get("performedPeriod")
            or fhir.get("occurrenceDateTime") or fhir.get("occurrencePeriod"))


def procedure_occurrence_start(fhir: dict):
    """A single instant for the Procedure (a period's start)."""
    for dt, period in (("performedDateTime", "performedPeriod"),
                       ("occurrenceDateTime", "occurrencePeriod")):
        if fhir.get(dt):
            return fhir[dt]
        start = (fhir.get(period) or {}).get("start")
        if start:
            return start
    return None
