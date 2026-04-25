"""Write-side canonicalisation chain for the CDR.

For every ``CodeableConcept`` on an inbound resource, walks the foreign
codings through ``xlate.pdhc /translate`` to get a termbank canonical,
then through ``plan.pdhc /ValueSet/$validate-code`` to confirm the
canonical is in the working set. On success, rewrites the
CodeableConcept so that ``coding[0]`` is the canonical and the
original codings are preserved in ``coding[1..n]``. Platform-plan
§1.2.c–d.

Outcome possibilities for a single CodeableConcept:

  - ``ok`` — at least one foreign coding mapped clean through both
    services. ``coding[]`` rewritten in place.
  - ``xlate_miss`` — every foreign coding had no xlate mapping. Caller
    rejects with 422 + xlate_miss OperationOutcome (§1.2.d.i).
  - ``plan_miss`` — xlate produced a canonical but plan.pdhc did not
    confirm adoption. Caller rejects with 422 + plan_miss
    OperationOutcome AND writes a ``cdr_audit_plan_miss`` row
    (§1.2.d.ii).
  - ``transient`` — xlate or plan unreachable; caller returns 503.

A coding whose system already starts with ``https://termbank.pdhc.se/``
is considered already-canonical and short-circuits to ``ok`` without an
xlate hop (still validated against plan).

Resource paths walked: every ``coding`` array we recognise on the FHIR
resource. The exact set is per-resource-type (e.g. Observation.code,
Observation.valueCodeableConcept; Condition.code; MedicationStatement
.medicationCodeableConcept; etc.).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.xlate_client import XlateClient, XlateUnreachable
from app.services.plan_client import PlanClient, PlanUnreachable, parse_canonical_uri


log = logging.getLogger(__name__)

TERMBANK_PREFIX = "https://termbank.pdhc.se/"


@dataclass
class CodeMiss:
    """Where a coding got rejected (xlate or plan) and which canonical /
    foreign code it concerns. Used to build the OperationOutcome and the
    plan-miss audit row."""

    kind: str  # 'xlate_miss' | 'plan_miss'
    location: str  # FHIR-path-ish: e.g. "Observation.code.coding[0]"
    foreign_system: str | None = None
    foreign_code: str | None = None
    canonical_uri: str | None = None
    canonical_lib_name: str | None = None
    canonical_refnumber: str | None = None


@dataclass
class CanonicalisationResult:
    """Return value of the canonicaliser.

    ``status`` is one of:
        ok | xlate_miss | plan_miss | transient
    ``rewritten`` is the FHIR resource dict (possibly with rewritten
    codings); only meaningful when ``status == "ok"``.
    """
    status: str
    rewritten: dict | None = None
    misses: list[CodeMiss] = field(default_factory=list)
    transient_reason: str | None = None
    # The first canonical_uri we mapped through cleanly — used as the
    # row's `code_canonical` indexed column.
    primary_canonical_uri: str | None = None


# ---------------------------------------------------------------------------
# Per-resource-type code paths
# ---------------------------------------------------------------------------

# Map from FHIR resource type to a list of (json_path, kind) tuples that
# describe where canonical concepts live. The kind is informational —
# we walk every coding[] regardless.
_CODE_PATHS: dict[str, list[str]] = {
    # ``code`` paths produce the row's code_canonical.
    "Observation": ["code", "valueCodeableConcept"],
    "Condition": ["code"],
    "MedicationStatement": ["medicationCodeableConcept"],
    "MedicationRequest": ["medicationCodeableConcept"],
    "AllergyIntolerance": ["code"],
    "Procedure": ["code"],
    "Encounter": ["class"],
    "DiagnosticReport": ["code"],
    "QuestionnaireResponse": [],  # references a Questionnaire by canonical, no CodeableConcept on the body
    "Patient": [],  # no canonical concept on Patient
}


# ---------------------------------------------------------------------------
# Public canonicaliser
# ---------------------------------------------------------------------------

class Canonicaliser:
    """Wraps the two clients. One instance per Flask app."""

    def __init__(self, xlate: XlateClient, plan: PlanClient):
        self.xlate = xlate
        self.plan = plan

    def canonicalise(self, fhir_resource: dict) -> CanonicalisationResult:
        """Walk every CodeableConcept on the resource. Return a result that
        the writer can act on."""
        rt = fhir_resource.get("resourceType")
        if rt not in _CODE_PATHS:
            # Resource type with no code paths — pass through unchanged.
            return CanonicalisationResult(status="ok", rewritten=fhir_resource)

        rewritten = dict(fhir_resource)  # shallow copy; we'll set keys back
        primary_canonical: str | None = None
        misses: list[CodeMiss] = []

        for path in _CODE_PATHS[rt]:
            cc = rewritten.get(path)
            if cc is None:
                continue
            try:
                new_cc, canonical_uri, path_misses = self._canonicalise_codeable_concept(
                    cc, location=f"{rt}.{path}"
                )
            except XlateUnreachable as e:
                return CanonicalisationResult(
                    status="transient",
                    transient_reason=f"xlate.pdhc unreachable: {e}",
                )
            except PlanUnreachable as e:
                return CanonicalisationResult(
                    status="transient",
                    transient_reason=f"plan.pdhc unreachable: {e}",
                )
            misses.extend(path_misses)
            if new_cc is not None:
                rewritten[path] = new_cc
                if primary_canonical is None and canonical_uri:
                    primary_canonical = canonical_uri

        if misses:
            # Determine kind: plan_miss takes precedence (xlate succeeded
            # but plan rejected), else xlate_miss.
            kind = "plan_miss" if any(m.kind == "plan_miss" for m in misses) \
                   else "xlate_miss"
            return CanonicalisationResult(
                status=kind,
                rewritten=None,
                misses=misses,
            )

        return CanonicalisationResult(
            status="ok",
            rewritten=rewritten,
            primary_canonical_uri=primary_canonical,
        )

    # ----- internals -----------------------------------------------------
    def _canonicalise_codeable_concept(
        self, cc: dict, *, location: str
    ) -> tuple[dict | None, str | None, list[CodeMiss]]:
        """Process one CodeableConcept. Returns (rewritten_cc, canonical_uri, misses).

        On success, ``rewritten_cc`` has the canonical coding promoted to
        position [0] and the original codings preserved in [1..n]. On
        failure, ``rewritten_cc`` is None and ``misses`` is populated.
        """
        codings = list(cc.get("coding") or [])
        if not codings:
            return cc, None, []

        misses: list[CodeMiss] = []

        # If any coding is already a termbank canonical, that's our chosen
        # canonical — verify against plan and we're done.
        for i, coding in enumerate(codings):
            sys_uri = coding.get("system", "")
            if sys_uri.startswith(TERMBANK_PREFIX):
                canonical_uri = self._coding_to_canonical_uri(coding)
                if canonical_uri is None:
                    continue
                plan_outcome = self.plan.validate_canonical(canonical_uri)
                if plan_outcome and plan_outcome.get("result"):
                    # Promote this coding to position 0 (it likely already is).
                    promoted = self._promote(codings, i)
                    return self._with_codings(cc, promoted), canonical_uri, []
                # plan miss
                parsed = parse_canonical_uri(canonical_uri)
                lib, ref = (parsed[0], parsed[1]) if parsed else (None, None)
                misses.append(CodeMiss(
                    kind="plan_miss",
                    location=f"{location}.coding[{i}]",
                    canonical_uri=canonical_uri,
                    canonical_lib_name=lib,
                    canonical_refnumber=ref,
                ))
                return None, None, misses

        # No termbank canonical present. Try xlate-translating each foreign
        # coding in order. First successful translate wins.
        for i, coding in enumerate(codings):
            sys_uri = coding.get("system")
            code_val = coding.get("code")
            if not sys_uri or not code_val:
                continue
            match = self.xlate.translate(sys_uri, code_val)
            if match is None:
                misses.append(CodeMiss(
                    kind="xlate_miss",
                    location=f"{location}.coding[{i}]",
                    foreign_system=sys_uri,
                    foreign_code=code_val,
                ))
                continue

            canonical_uri = match["canonical_uri"]
            plan_outcome = self.plan.validate_canonical(canonical_uri)
            if not plan_outcome or not plan_outcome.get("result"):
                parsed = parse_canonical_uri(canonical_uri)
                lib, ref = (parsed[0], parsed[1]) if parsed else (None, None)
                misses.append(CodeMiss(
                    kind="plan_miss",
                    location=f"{location}.coding[{i}]",
                    foreign_system=sys_uri,
                    foreign_code=code_val,
                    canonical_uri=canonical_uri,
                    canonical_lib_name=lib,
                    canonical_refnumber=ref,
                ))
                # Try other codings before giving up — a CodeableConcept may
                # have multiple foreign codings, and we accept the first one
                # that's both translatable AND adopted.
                continue

            # Success — build the canonical coding and promote it.
            canonical_coding = {
                "system": f"https://termbank.pdhc.se/CodeSystem/{match['canonical_system']}",
                "code": match["canonical_code"],
                "display": match.get("display"),
            }
            new_codings = [canonical_coding] + codings  # original preserved
            return self._with_codings(cc, new_codings), canonical_uri, []

        # All foreign codings missed (or were skipped). misses list captures why.
        if not misses:
            # No usable system+code on any coding — treat as xlate miss with
            # an empty pointer so callers can produce a sensible message.
            misses.append(CodeMiss(
                kind="xlate_miss",
                location=f"{location}.coding[0]",
            ))
        return None, None, misses

    @staticmethod
    def _coding_to_canonical_uri(coding: dict) -> str | None:
        """termbank canonical URI built from {system, code} where system
        starts with https://termbank.pdhc.se/CodeSystem/<lib>."""
        sys_uri = coding.get("system", "")
        code = coding.get("code", "")
        if not sys_uri.startswith(TERMBANK_PREFIX) or not code:
            return None
        # Two valid shapes: "https://termbank.pdhc.se/CodeSystem/loinc" (system
        # only) or already a full canonical URI (rare). We treat the first.
        return f"{sys_uri.rstrip('/')}/{code}"

    @staticmethod
    def _promote(codings: list[dict], idx: int) -> list[dict]:
        if idx == 0:
            return list(codings)
        return [codings[idx]] + [c for j, c in enumerate(codings) if j != idx]

    @staticmethod
    def _with_codings(cc: dict, new_codings: list[dict]) -> dict:
        out = dict(cc)
        out["coding"] = new_codings
        return out


# ---------------------------------------------------------------------------
# OperationOutcome builders
# ---------------------------------------------------------------------------

def operation_outcome_xlate_miss(misses: list[CodeMiss]) -> dict:
    """Build the FHIR OperationOutcome body for a 422 xlate-miss response.

    Includes ``issue.location`` per execution-plan §1.2.d.i so the producer
    knows which coding to fix.
    """
    issues = []
    for m in misses:
        if m.kind != "xlate_miss":
            continue
        issues.append({
            "severity": "error",
            "code": "code-invalid",
            "details": {
                "text": (
                    f"No xlate mapping for system={m.foreign_system!r} "
                    f"code={m.foreign_code!r}"
                ),
            },
            "diagnostics": "xlate_miss",
            "location": [m.location],
        })
    if not issues:
        issues.append({
            "severity": "error",
            "code": "code-invalid",
            "details": {"text": "xlate_miss"},
            "diagnostics": "xlate_miss",
        })
    return {"resourceType": "OperationOutcome", "issue": issues}


def operation_outcome_plan_miss(misses: list[CodeMiss]) -> dict:
    issues = []
    for m in misses:
        if m.kind != "plan_miss":
            continue
        issues.append({
            "severity": "error",
            "code": "code-invalid",
            "details": {
                "text": (
                    f"Canonical {m.canonical_uri!r} is not referenced "
                    "from any active Concept or ValueCatalog in plan.pdhc"
                ),
            },
            "diagnostics": "plan_miss",
            "location": [m.location],
        })
    if not issues:
        issues.append({
            "severity": "error",
            "code": "code-invalid",
            "details": {"text": "plan_miss"},
            "diagnostics": "plan_miss",
        })
    return {"resourceType": "OperationOutcome", "issue": issues}


def operation_outcome_transient(reason: str) -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{
            "severity": "error",
            "code": "transient",
            "details": {"text": reason},
            "diagnostics": "upstream-unreachable",
        }],
    }


# ---------------------------------------------------------------------------
# plan_miss audit row dedup helper — used by the writer to record one row
# per (canonical_uri) and increment seen_count on repeats.
# ---------------------------------------------------------------------------
def record_plan_miss(db_session, miss: CodeMiss, *, request_id: str | None) -> None:
    """Idempotently record a plan-miss canonical to ``cdr_audit_plan_miss``.

    First sighting → INSERT; subsequent → UPDATE (seen_count += 1,
    last_seen_at, last_request_id).
    """
    from app.models.resources import CdrAuditPlanMiss

    if not miss.canonical_uri:
        return

    row = db_session.query(CdrAuditPlanMiss).filter_by(
        canonical_uri=miss.canonical_uri
    ).one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        row = CdrAuditPlanMiss(
            canonical_uri=miss.canonical_uri,
            canonical_lib_name=miss.canonical_lib_name,
            canonical_refnumber=miss.canonical_refnumber,
            seen_count=1,
            first_seen_at=now,
            last_seen_at=now,
            last_request_id=request_id,
        )
        db_session.add(row)
    else:
        row.seen_count = (row.seen_count or 0) + 1
        row.last_seen_at = now
        row.last_request_id = request_id
