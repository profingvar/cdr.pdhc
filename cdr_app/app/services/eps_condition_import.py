"""#984 — the CDR becomes the record for diagnoses the EPS held alone.

Operator decision 2026-11-16: diagnoses are RECORDED in the CDR; ips.pdhc's
EPS problems section is a summary derived from it. On that date the EPS held
446 SNOMED Conditions and the three CDRs held 9 (icd11's, in cdr2), so the
EPS's diagnoses are brought INTO the CDR here, for the patients whose home is
this CDR. Nothing is invented on the way:

- the write goes through `_write_one`, the same canonicalisation + writer the
  FHIR API uses, so plan validation, dedup and versioning are the API's own;
- SNOMED is presented in termbank's canonical form (a pure rename, what xlate
  would return) with the original coding kept after it — no CDR has xlate
  configured, and SNOMED is deliberately not a cdr alias (#833);
- the id is a uuid5 of the ips resource id, so a re-run upserts the same row;
- `patient_org_guid` is taken from the patient's own rows in this CDR and the
  patient is REFUSED when those disagree, since that column scopes reads;
- absent/unknown assertions ("no known problems") are not diagnoses and are
  skipped by name; a patient this CDR does not hold is skipped by name.
"""
from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field

from app.services.plan_client import build_canonical

SOURCE = "ips.pdhc"
REQUEST_ID = "eps-backfill-984"
SNOMED = "http://snomed.info/sct"
ABSENT_UNKNOWN = "http://hl7.org/fhir/uv/ips/CodeSystem/absent-unknown-uv-ips"
MIGRATED_TAG = {"system": "https://cdr.pdhc.se/CodeSystem/provenance",
                "code": "migrated-from-eps-984",
                "display": "Migrated from the ips.pdhc EPS (#984)"}
IPS_IDENTIFIER_SYSTEM = "https://ips.pdhc.se/fhir/Condition"
_NS = uuid.UUID("5b0f2a8e-9840-4c6e-a1d4-984984984984")
_KEEP = ("clinicalStatus", "verificationStatus", "category", "severity",
         "onsetDateTime", "onsetPeriod", "abatementDateTime", "recordedDate",
         "note", "bodySite")


class Skip(Exception):
    """This row is not imported, for the stated reason."""


#: (SNOMED code, EPS display) pairs whose display differs from termbank's
#: preferred term but does NOT contradict the code. Everything else that
#: differs is a contradiction (measured 2026-11-16: 44054006 "Type 2 diabetes"
#: labelled "Atrial fibrillation"; 40055000 "Chronic sinusitis" labelled
#: "Chronic kidney disease"; 80146002 "Appendectomy" — a procedure — labelled
#: "Appendicitis"; 1734006 a vertebral fracture labelled "Fracture of radius").
#: Which half of a contradiction is true cannot be known from the row, so it is
#: not imported: the CDR is the record and must not start with a guess.
ACCEPTED_DISPLAY_VARIANTS = {
    ("35489007", "depression"),                 # synonym of Depressive disorder
    ("73211009", "diabetes mellitus type 2"),   # true, the code is the broader concept
}


def contradicts(code: str, eps_display: str | None, termbank_display: str | None) -> bool:
    """True when the EPS label says something the code does not."""
    if not termbank_display or not eps_display:
        return False
    a, b = eps_display.strip().lower(), termbank_display.strip().lower()
    return a != b and (code, a) not in ACCEPTED_DISPLAY_VARIANTS


def cdr_guid_for(ips_resource_id: str) -> str:
    return str(uuid.uuid5(_NS, f"{SOURCE}/Condition/{ips_resource_id}"))


def to_cdr_condition(ips_resource: dict, patient_guid: str,
                     termbank_display: str | None = None) -> dict:
    """The CDR's Condition for one EPS Condition. Raises Skip.

    `termbank_display` is termbank's preferred term for the SNOMED code; when
    given, a label that contradicts it is refused (`contradicts`).
    """
    codings = ((ips_resource.get("code") or {}).get("coding")) or []
    if not codings:
        raise Skip("no coding")
    if any(c.get("system") == ABSENT_UNKNOWN for c in codings):
        raise Skip("absent/unknown assertion, not a diagnosis")
    snomed = [c for c in codings if c.get("system") == SNOMED and c.get("code")]
    if not snomed:
        raise Skip("no SNOMED coding")
    if contradicts(snomed[0]["code"], snomed[0].get("display"), termbank_display):
        raise Skip("EPS label contradicts the code")
    canonical = build_canonical("snomed", snomed[0]["code"])
    if canonical is None:
        raise Skip(f"SNOMED code outside termbank's grammar: {snomed[0]['code']!r}")
    lib_system, _, code = canonical.rpartition("/")
    # `rpartition` is safe HERE only: a SNOMED code never contains "/".
    if "/" in snomed[0]["code"]:
        raise Skip("SNOMED code containing '/'")
    out = {
        "resourceType": "Condition",
        "id": cdr_guid_for(ips_resource["id"]),
        "subject": {"reference": f"Patient/{patient_guid}"},
        "code": {
            "text": (ips_resource.get("code") or {}).get("text") or snomed[0].get("display"),
            "coding": [{"system": lib_system, "code": code,
                        "display": snomed[0].get("display")}] + list(codings),
        },
        "identifier": [{"system": IPS_IDENTIFIER_SYSTEM, "value": ips_resource["id"]}],
        "meta": {"tag": [dict(MIGRATED_TAG)]},
    }
    for k in _KEEP:
        if k in ips_resource:
            out[k] = ips_resource[k]
    return out


#: Where a patient's presence in this CDR is read from. Condition as well as
#: Observation: icd11's six patients in cdr2 had Conditions and no
#: observations, and were wrongly read as "not here" (measured 2026-11-16).
_HOME_TABLES = ("observation", "condition")


def patient_org_for(patient_guid: str) -> str | None:
    """The patient's organisation as this CDR already records it. Raises Skip."""
    from app import db
    union = " union ".join(
        f"select patient_org_guid from {t} where patient_guid = :p and patient_org_guid is not null"
        for t in _HOME_TABLES)
    rows = db.session.execute(db.text(f"select distinct patient_org_guid from ({union}) x"),
                              {"p": patient_guid}).scalars().all()
    if len(rows) > 1:
        raise Skip(f"patient's rows name {len(rows)} organisations")
    return rows[0] if rows else None


def patient_is_here(patient_guid: str) -> bool:
    from app import db
    return any(db.session.execute(db.text(
        f"select 1 from {t} where patient_guid = :p limit 1"), {"p": patient_guid}).first() is not None
        for t in _HOME_TABLES)


@dataclass
class Report:
    seen: int = 0
    written: Counter = field(default_factory=Counter)   # created / updated / would write
    skipped: Counter = field(default_factory=Counter)
    refused: list = field(default_factory=list)        # (ips id, status, text)
    not_here: set = field(default_factory=set)

    def lines(self) -> list[str]:
        out = [f"seen {self.seen}", f"written {dict(self.written)}",
               f"skipped {dict(self.skipped)}", f"patients not in this CDR {len(self.not_here)}",
               f"refused {len(self.refused)}"]
        out += [f"  REFUSED {i} {s} {t}" for i, s, t in self.refused[:20]]
        return out


def run(entries: list[dict], *, apply: bool, write_one=None, canonicaliser=None) -> Report:
    """entries: [{"patient_guid", "resource", "termbank_display"?}] — the EPS
    Conditions as exported, each with termbank's term for its SNOMED code.

    Dry run canonicalises only (plan validation included) and writes nothing.
    """
    from app import db
    from app.models.resources import live_model
    from app.services.resource_writer import WriteContext
    if write_one is None:
        from app.api.fhir_write import _write_one as write_one
    if canonicaliser is None:
        from app.api.fhir_write import _get_canonicaliser
        canonicaliser = _get_canonicaliser()
    rep = Report()
    for e in entries:
        rep.seen += 1
        pg, res = e["patient_guid"], e["resource"]
        if not patient_is_here(pg):
            rep.not_here.add(pg)
            rep.skipped["patient not in this CDR"] += 1
            continue
        try:
            fhir = to_cdr_condition(res, pg, e.get("termbank_display"))
            org = patient_org_for(pg)
        except Skip as s:
            rep.skipped[str(s)] += 1
            continue
        if live_model("Condition").query.filter_by(guid=fhir["id"]).first() is not None:
            # Already imported. Skipped rather than rewritten: the writer
            # version-bumps even on identical content (measured: a rerun made
            # 146 no-op versions on cdr2).
            rep.skipped["already in this CDR"] += 1
            continue
        if not apply:
            r = canonicaliser.canonicalise(fhir)
            if r.status != "ok":
                rep.refused.append((res["id"], r.status, [m.__dict__ for m in r.misses][:1]))
            else:
                rep.written["would write"] += 1
            continue
        ctx = WriteContext(org_guid=None, patient_org_guid=org, source=SOURCE,
                           source_request_id=REQUEST_ID)
        body, status, _ = write_one(fhir, ctx)
        if status in (200, 201):
            # The body carries an id, so the writer upserts BY ID and never
            # dedups: one EPS Condition is one CDR row, duplicates included.
            rep.written["created" if status == 201 else "updated"] += 1
            db.session.commit()
        else:
            db.session.rollback()
            text = ((body.get("issue") or [{}])[0].get("details") or {}).get("text")
            rep.refused.append((res["id"], status, text))
    return rep
