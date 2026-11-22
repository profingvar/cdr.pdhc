"""#1044: care reviews a patient-reported answer before it can become EPS.

The patient app sends EPS answers (allergies, medication, history,
vaccinations, contacts). They are stored as every other ingest is, flagged
`data_origin = 'patient_generated'` (#1014) and, when gateway says
`review_required: true`, `review_status = 'pending'`.

A professional then decides, ONCE:

  approve   a NEW row is written (the endorsement): same patient, concept,
            time and context; value = the original's or an amended one;
            `data_origin` NULL (care's); `endorses_guid` = the original;
            author = the professional and the care unit they acted for.
            The original becomes `endorsed`.
  reject    the original becomes `rejected`, with the reason.

THE ORIGINAL IS NEVER REWRITTEN. `_flip` names the only columns that change
and pins `updated_at` to itself, so the row's value, timestamps, origin and
raw FHIR are byte-for-byte what the patient sent. Both writes of an approval
(and the audit row) are one transaction, and the flip is a conditional UPDATE
(`WHERE review_status = 'pending'`), so two simultaneous decisions cannot
both win: the second matches no row and is answered 409.

WHICH ORGANISATION THE GATE SCOPES ON: `observation.patient_org_guid`. It is
the organisation the PATIENT was listed at in ips when the request was made
(request.pdhc snapshots it on the ServiceRequest, gateway forwards it in the
clinical context, ingest_pipeline stores it on the row). It is the column
fhir_read's Rule 24 filter and the external read door already scope on.
`org_guid` is NOT used: it holds the PERFORMER, which on this platform is the
submitting provider. For a patient-app answer that is the patient app's own
organisation, not a care unit, so scoping on it would admit nobody (or the
wrong people). A row whose patient_org_guid is NULL can be listed by an admin
and decided by no one: an unknown organisation is not "any organisation".

WHO MAY DECIDE: a professional whose SSO blob has an affiliation to that care
unit with role `doctor` or `nurse` (`affiliations[].care_unit_guid` + `.role`,
sso.pdhc's role registry). An SU admin without such an affiliation may list
and may not decide.
"""
from __future__ import annotations

import copy
import math
import uuid
from datetime import datetime, timezone

from flask import request

from app import db
from app.models import AuditLog, ClinicalContext, IngestRaw
from app.models.resources import live_model
from app.services import data_origin as _origin

PENDING = "pending"
ENDORSED = "endorsed"
REJECTED = "rejected"
#: #1049: a pending card version replaced by a newer pending version of the
#: same section (contract s7.4). Set by an INGEST, never by a person.
SUPERSEDED = "superseded"
STATUSES = (PENDING, ENDORSED, REJECTED)
CARD_STATUSES = (PENDING, ENDORSED, REJECTED, SUPERSEDED)

#: The linkId prefixes of the EPS patient form (contract s3.2).
SECTIONS = ("allergy", "medication", "history", "vaccination", "contact")

#: sso.pdhc role codes that may approve or reject (contract s4.2:
#: "physician or nurse"). `other_care` and the analysis roles may not.
DECIDER_ROLES = frozenset({"doctor", "nurse"})

#: `source` on an endorsement row and its provenance: written by this
#: repository's review surface, not by a producer.
REVIEW_SOURCE = "cdr.pdhc/review"

#: The #1014 marks gateway puts on a patient-generated Observation. An
#: endorsement is care's statement, so its FHIR must not carry them.
_PGD_EXT_URL = "urn:pdhc:fhir:extension:data-origin"
_PGD_TAG_SYSTEM = "urn:pdhc:fhir:CodeSystem:data-origin"

MAX_REASON = 2000
_VALUE_KEYS = ("valueQuantity", "valueString", "valueBoolean", "valueInteger",
               "valueDateTime", "valueCodeableConcept")

#: The ONLY columns of an original a decision may write.
FLIP_COLUMNS = ("review_status", "review_reason", "reviewed_by_user_guid",
                "reviewed_by_name", "reviewed_at")


class AlreadyDecided(Exception):
    """The original is no longer pending (HTTP 409)."""


class Superseded(AlreadyDecided):
    """#1049: the card version was replaced by a newer one (HTTP 409)."""


class InvalidValue(Exception):
    """An amended value that an ingest would not have produced (HTTP 422)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(at):
    """ISO 8601 WITH its offset (#1049). eps orders card versions by
    reviewed_at and refuses a value without one. Every reviewed_at is written
    as UTC (`_now`); a backend that drops tzinfo on read (SQLite) returns it
    naive, so naive here means UTC."""
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.isoformat()


# ---------------------------------------------------------------------------
# Ingest side
# ---------------------------------------------------------------------------

def check_required(flag, origin) -> tuple[bool, str | None]:
    """(review_required, error) for an ingest.

    Absent / false -> not subject to review. `true` REQUIRES
    data_origin = patient_generated: a review is of what a patient said, and
    care's own data marked "pending" would be hidden from the EPS for no
    reason anyone could find. Anything but a JSON boolean is refused rather
    than guessed at ("true" the string is a producer bug, not a yes).
    """
    if flag is None or flag is False:
        return False, None
    if flag is not True:
        return False, (f"invalid_review_required: {flag!r}; send the JSON "
                       f"boolean true, or omit it")
    if origin != _origin.PATIENT_GENERATED:
        return False, ("review_required_without_patient_generated: "
                       "review_required needs data_origin = "
                       f"{_origin.PATIENT_GENERATED!r}; only a patient-reported "
                       "answer is reviewed")
    return True, None


MAX_GROUP = 64


def check_group(group) -> str | None:
    """Error for an unusable `group`, else None. Optional; a short string."""
    if group is None or group == "":
        return None
    if not isinstance(group, (str, int)) or isinstance(group, bool) \
            or len(str(group)) > MAX_GROUP:
        return (f"invalid_group: {group!r}; send a string of at most "
                f"{MAX_GROUP} characters, or omit it")
    return None


def with_group(resolved, group):
    """`resolved_context_json` carrying the group instance, when one was
    sent. The producer's own bag is copied, never mutated or replaced."""
    if group is None or group == "":
        return resolved
    out = dict(resolved) if isinstance(resolved, dict) else {}
    out["group"] = str(group)
    return out


# ---------------------------------------------------------------------------
# #1049: the patient card (contract s7.4)
# ---------------------------------------------------------------------------

def _uuid_or_none(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def check_card(ctx: dict, *, review_required: bool, patient_guid: str):
    """(card_section, card_version, error) for an ingest.

    Neither sent -> (None, None, None): not a card answer, v1 rules apply.
    A card answer is ALWAYS reviewed, has a known section that its own
    linkId agrees with, names the care unit (the gate's column; a card row
    nobody can decide is refused rather than stored), and joins a version
    that belongs to this patient, section and unit and is still pending.
    A card answer needs NO service_request_guid: v2 has no care order.
    """
    section = ctx.get("card_section")
    version = ctx.get("card_version")
    if section in (None, "") and version in (None, ""):
        return None, None, None
    if section in (None, "") or version in (None, ""):
        return None, None, ("incomplete_card: card_section and card_version "
                            "come together, or neither")
    if not isinstance(section, str) or section not in SECTIONS:
        return None, None, (f"invalid_card_section: {section!r}; one of "
                            f"{', '.join(SECTIONS)}")
    norm = _uuid_or_none(version)
    if norm is None:
        return None, None, (f"invalid_card_version: {version!r}; send the "
                            f"uuid the patient app minted for this send")
    if not review_required:
        return None, None, ("card_requires_review: a card answer is reviewed "
                            "by care; send review_required: true")
    link = ctx.get("transaction_guid") or ctx.get("activity_guid")
    if section_of(link) != section:
        return None, None, (f"card_section_mismatch: linkId {link!r} is not "
                            f"in section {section!r}")
    unit = ctx.get("patient_org_guid")
    if not unit:
        return None, None, ("card_needs_patient_org: a card answer names the "
                            "care unit (patient_org_guid); without it nobody "
                            "could decide it")
    Obs = live_model("Observation")
    peers = (db.session.query(Obs.patient_guid, Obs.card_section,
                              Obs.patient_org_guid, Obs.review_status)
             .filter(Obs.card_version == norm).distinct().all())
    for p_guid, p_section, p_unit, p_status in peers:
        if (p_guid, p_section, p_unit) != (patient_guid, section, unit):
            return None, None, ("card_version_conflict: this card_version "
                                "already belongs to another patient, section "
                                "or care unit")
        if p_status != PENDING:
            return None, None, (f"card_version_closed: this card version is "
                                f"already {p_status}; a new answer needs a new "
                                f"card_version")
    return section, norm, None


def supersede_older(*, patient_guid: str, section: str, version: str,
                    at: datetime | None = None) -> int:
    """Every PENDING row of this patient and section with ANOTHER version
    becomes superseded (`reviewed_at` set, no reviewer). Endorsed, rejected
    and v1 rows (no card_section) are untouched. No commit: it is part of
    the ingest's transaction. -> rows superseded."""
    Obs = live_model("Observation")
    res = db.session.execute(
        Obs.__table__.update()
        .where(Obs.patient_guid == patient_guid,
               Obs.card_section == section,
               Obs.card_version.isnot(None),
               Obs.card_version != version,
               Obs.review_status == PENDING)
        .values(updated_at=Obs.updated_at, review_status=SUPERSEDED,
                reviewed_at=at or _now(), reviewed_by_user_guid=None,
                reviewed_by_name=None, review_reason=None))
    return res.rowcount or 0


def groups_of(ingest_raw_guids: list) -> dict[str, str]:
    """ingest_raw_guid -> group instance, for rows whose context has one."""
    guids = [g for g in ingest_raw_guids if g]
    if not guids:
        return {}
    out = {}
    for ctx in (ClinicalContext.query
                .filter(ClinicalContext.ingest_raw_guid.in_(guids)).all()):
        bag = ctx.resolved_context_json
        if isinstance(bag, dict) and bag.get("group") not in (None, ""):
            out[ctx.ingest_raw_guid] = str(bag["group"])
    return out


# ---------------------------------------------------------------------------
# Reading a row
# ---------------------------------------------------------------------------

def link_id_of(row) -> str | None:
    """The Questionnaire linkId the answer belongs to.

    gateway's QuestionnaireResponse extraction sends it as the context's
    `transaction_guid` (one extracted item = one transaction), which #769
    put on the row.
    """
    return getattr(row, "transaction_guid", None) or None


def section_of(link_id: str | None) -> str | None:
    """`allergy.substance` -> `allergy`; None when it is not an EPS linkId."""
    if not link_id or "." not in link_id:
        return None
    head = link_id.split(".", 1)[0]
    return head if head in SECTIONS else None


def label_of(raw: dict) -> str | None:
    code = raw.get("code") if isinstance(raw, dict) else None
    if not isinstance(code, dict):
        return None
    if code.get("text"):
        return str(code["text"])
    for coding in code.get("coding") or []:
        if isinstance(coding, dict) and coding.get("display"):
            return str(coding["display"])
    return None


def value_of(raw: dict):
    """(fhir value key, plain value, unit) of an Observation, or (None,)*3."""
    if not isinstance(raw, dict):
        return None, None, None
    for key in _VALUE_KEYS:
        if key not in raw:
            continue
        v = raw[key]
        if key == "valueQuantity":
            v = v if isinstance(v, dict) else {}
            return key, v.get("value"), v.get("unit") or v.get("code")
        if key == "valueCodeableConcept":
            v = v if isinstance(v, dict) else {}
            coding = (v.get("coding") or [{}])[0] or {}
            return key, coding.get("code") or v.get("text"), None
        return key, v, None
    return None, None, None


def display_of(raw: dict) -> str | None:
    """#1049: the answer as the patient chose it, in words (same as the
    status list's summary)."""
    return summary_of(raw)


def summary_of(raw: dict) -> str | None:
    """The answer as one short text, for the patient's own status list."""
    key, value, unit = value_of(raw)
    if value is None:
        return None
    if key == "valueCodeableConcept":
        # #1044: the patient reads words, not the stored code (`has`, `none`).
        v = raw.get(key) if isinstance(raw.get(key), dict) else {}
        coding = (v.get("coding") or [{}])[0] or {}
        return str(coding.get("display") or v.get("text") or value)
    if isinstance(value, bool):
        return "Ja" if value else "Nej"
    return f"{value} {unit}" if unit else str(value)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def review_units(blob: dict | None) -> dict[str, dict]:
    """care_unit_guid -> affiliation, for the units this person may DECIDE in.

    Affiliations only (the Rule 24 scope, auth.scope_org_guids), narrowed to
    the deciding roles. Empty for a patient, for a professional with no
    affiliation, and for an admin who is not also affiliated: admin widens
    nothing here.
    """
    out: dict[str, dict] = {}
    if not isinstance(blob, dict) or blob.get("user_type") != "professional":
        return out
    for aff in blob.get("affiliations") or []:
        if not isinstance(aff, dict):
            continue
        unit = aff.get("care_unit_guid")
        if unit and aff.get("role") in DECIDER_ROLES:
            out.setdefault(unit, aff)
    return out


def may_decide(blob: dict | None, patient_org_guid: str | None) -> dict | None:
    """The affiliation that lets this person decide on a row of that patient
    organisation, or None. None for an unknown organisation, always."""
    if not patient_org_guid:
        return None
    return review_units(blob).get(patient_org_guid)


# ---------------------------------------------------------------------------
# Amending
# ---------------------------------------------------------------------------

def _amended_raw(raw: dict, value) -> dict:
    """`raw` with its value replaced by `value`, validated against what the
    ORIGINAL holds: an amendment corrects an answer, it does not change what
    kind of answer it is. The limits are those of the columns an ingest
    writes (observation.value_quantity NUMERIC(18,6), value_string 512,
    value_code 128)."""
    key, _old, _unit = value_of(raw)
    if key is None:
        raise InvalidValue("no_value", "the original has no value to amend")
    out = copy.deepcopy(raw)
    if key in ("valueQuantity", "valueInteger"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidValue("wrong_type", "the original is a number; the "
                                             "amended value must be a number")
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidValue("wrong_type", "the amended value must be finite")
        if abs(value) >= 10 ** 12:
            raise InvalidValue("out_of_range", "the amended value is too large")
        if key == "valueInteger":
            if isinstance(value, float) and not value.is_integer():
                raise InvalidValue("wrong_type", "the original is a whole "
                                                 "number; so must the amended be")
            out[key] = int(value)
        else:
            out[key] = dict(out[key] or {}, value=float(value))
    elif key == "valueBoolean":
        if not isinstance(value, bool):
            raise InvalidValue("wrong_type", "the original is yes/no; the "
                                             "amended value must be true or false")
        out[key] = value
    elif key == "valueDateTime":
        if not isinstance(value, str) or not value.strip():
            raise InvalidValue("wrong_type", "the original is a date; the "
                                             "amended value must be an ISO date")
        try:
            datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise InvalidValue("wrong_type", "the amended value is not an "
                                             "ISO-8601 date") from None
        out[key] = value.strip()
    else:   # valueString, valueCodeableConcept
        limit = 512 if key == "valueString" else 128
        if not isinstance(value, str) or not value.strip():
            raise InvalidValue("required", "the amended value must be "
                                           "non-empty text")
        if len(value.strip()) > limit:
            raise InvalidValue("out_of_range", f"the amended value is longer "
                                               f"than {limit} characters")
        text = value.strip()
        if key == "valueString":
            out[key] = text
        else:
            old = out[key] if isinstance(out[key], dict) else {}
            system = ((old.get("coding") or [{}])[0] or {}).get("system")
            coding = {"code": text, "display": text}
            if system:
                coding["system"] = system
            out[key] = {"coding": [coding], "text": text}
    return out


def _endorsement_fhir(original, raw: dict, *, guid: str, actor: dict,
                      unit: dict, note: str | None, at: datetime) -> dict:
    """The endorsement as a FHIR Observation: the original's resource with a
    new id, care as performer, `derivedFrom` -> the original, and without the
    patient-generated marks."""
    out = copy.deepcopy(raw)
    out["id"] = guid
    meta = dict(out.get("meta") or {})
    meta.pop("versionId", None)
    meta.pop("lastUpdated", None)
    tags = [t for t in (meta.get("tag") or [])
            if not (isinstance(t, dict) and t.get("system") == _PGD_TAG_SYSTEM)]
    if tags:
        meta["tag"] = tags
    else:
        meta.pop("tag", None)
    if meta:
        out["meta"] = meta
    else:
        out.pop("meta", None)
    exts = [e for e in (out.get("extension") or [])
            if not (isinstance(e, dict) and e.get("url") == _PGD_EXT_URL)]
    if exts:
        out["extension"] = exts
    else:
        out.pop("extension", None)
    # performer[0] is the organisation: build_live_observation_row reads
    # org_guid from the first performer identifier.
    out["performer"] = [
        {"type": "Organization",
         "identifier": {"value": unit["care_unit_guid"]},
         "display": unit.get("care_unit_name") or "Care unit"},
        {"type": "Practitioner",
         "identifier": {"value": actor["user_guid"]},
         "display": actor["name"]},
    ]
    out["derivedFrom"] = [{"reference": f"Observation/{original.guid}",
                           "type": "Observation",
                           "identifier": {"value": original.guid}}]
    out["issued"] = at.isoformat()
    if note:
        out["note"] = list(out.get("note") or []) + [{
            "authorString": actor["name"], "time": at.isoformat(),
            "text": note}]
    return out


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------

def _flip(original, *, status: str, actor: dict, at: datetime,
          reason: str | None = None) -> None:
    """pending -> status, on the original, or AlreadyDecided.

    A conditional UPDATE naming FLIP_COLUMNS and nothing else. `updated_at`
    is set to itself so its onupdate default does not fire: the timestamps
    of the patient's row are part of what must not change.
    """
    Obs = type(original)
    values = {"review_status": status, "review_reason": reason,
              "reviewed_by_user_guid": actor["user_guid"],
              "reviewed_by_name": actor["name"], "reviewed_at": at}
    assert set(values) == set(FLIP_COLUMNS)
    res = db.session.execute(
        Obs.__table__.update()
        .where(Obs.guid == original.guid, Obs.review_status == PENDING)
        .values(updated_at=Obs.updated_at, **values))
    if res.rowcount != 1:
        raise AlreadyDecided(original.guid)


def _audit(event: str, *, actor: dict, patient_guid: str | None,
           snapshot: dict) -> None:
    db.session.add(AuditLog(
        event_type=event,
        actor_guid=actor.get("user_guid"),
        data_subject_guid=patient_guid,
        source_service=REVIEW_SOURCE,
        correlation_id=request.headers.get("X-Correlation-Id"),
        ip_address=request.remote_addr,
        payload_snapshot=snapshot,
    ))


def _prepare(original, value) -> tuple[dict, bool]:
    """(the FHIR to endorse, amended). Validates an amendment and writes
    nothing, so a bad value is refused before the first write."""
    raw = original.raw_json if isinstance(original.raw_json, dict) else {}
    _key, old_value, _unit = value_of(raw)
    amended = value is not None and value != old_value
    return (_amended_raw(raw, value) if amended else raw), amended


def approve(original, *, actor: dict, unit: dict, value=None,
            note: str | None = None) -> tuple[str, bool]:
    """Endorse `original`. -> (endorsement guid, amended).

    One transaction: flip, endorsement row (+ its raw and context rows),
    audit. Raises AlreadyDecided / InvalidValue with nothing written.
    The caller has already established that `actor` may decide for `unit`.
    """
    base, amended = _prepare(original, value)              # may raise, pre-write
    at = _now()
    try:
        guid = _endorse(original, base, amended, actor=actor, unit=unit,
                        note=note, at=at)
        _audit("review.approved", actor=actor,
               patient_guid=original.patient_guid,
               snapshot={"original_guid": original.guid,
                         "endorsement_guid": guid, "amended": amended,
                         "care_unit_guid": unit["care_unit_guid"],
                         "role": unit.get("role"), "has_note": bool(note)})
        db.session.commit()
    except BaseException:
        db.session.rollback()
        raise
    return guid, amended


def _endorse(original, base: dict, amended: bool, *, actor: dict, unit: dict,
             note: str | None, at: datetime) -> str:
    """Flip `original` to endorsed and write its endorsement row, raw and
    context rows. NO commit and no audit: the caller owns the transaction
    (one row for s4.2, every row of a card version for s7.4)."""
    from app.services.ingest_pipeline import build_live_observation_row

    guid = str(uuid.uuid4())
    fhir = _endorsement_fhir(original, base, guid=guid, actor=actor,
                             unit=unit, note=note, at=at)
    _flip(original, status=ENDORSED, actor=actor, at=at)

    body = {"patient_guid": original.patient_guid, "source_type": "review",
            "review": {"decision": "approve", "original_guid": original.guid,
                       "amended": amended,
                       "decided_by_user_guid": actor["user_guid"],
                       "care_unit_guid": unit["care_unit_guid"]},
            "fhir_resource": fhir}
    ingest = IngestRaw(
        source_service=REVIEW_SOURCE, source_system_id=guid,
        patient_guid=original.patient_guid, payload_json=body,
        payload_hash=IngestRaw.compute_hash(body), source_type="review",
        received_at=at)
    db.session.add(ingest)
    db.session.flush()

    row = build_live_observation_row(
        fhir, patient_guid=original.patient_guid,
        source_service=REVIEW_SOURCE, ingest_raw_guid=ingest.guid,
        received_at=at,
        context={"transaction_guid": original.transaction_guid,
                 "care_plan_guid": original.care_plan_guid,
                 "patient_org_guid": original.patient_org_guid})
    # Same concept and clinical time as the original, by construction
    # rather than by re-deriving them from the copied FHIR.
    row.code_canonical = original.code_canonical
    row.effective_at = original.effective_at
    row.data_origin = None            # care's, by policy (#1014)
    row.review_status = None          # an endorsement is not itself reviewed
    row.endorses_guid = original.guid
    row.reviewed_by_user_guid = actor["user_guid"]
    row.reviewed_by_name = actor["name"]
    row.reviewed_at = at
    # #1049: the endorsement belongs to the same card section and version.
    row.card_section = original.card_section
    row.card_version = original.card_version
    db.session.add(row)

    ctx = (ClinicalContext.query
           .filter_by(ingest_raw_guid=original.ingest_raw_guid).first()
           if original.ingest_raw_guid else None)

    def _from(field):
        return getattr(ctx, field, None) if ctx is not None else None

    db.session.add(ClinicalContext(
        ingest_raw_guid=ingest.guid,
        patient_guid=original.patient_guid,
        service_request_guid=_from("service_request_guid"),
        transaction_guid=original.transaction_guid or _from("transaction_guid"),
        concept_guid=_from("concept_guid"),
        plan_definition_guid=_from("plan_definition_guid"),
        plan_activity_guid=_from("plan_activity_guid"),
        care_plan_guid=original.care_plan_guid or _from("care_plan_guid"),
        contract_guid=_from("contract_guid"),
        requesting_org_guid=_from("requesting_org_guid"),
        # No PAT submitted this row, so there is no authenticated
        # provider; the author is the care unit that endorsed it.
        provider_org_guid=None,
        author_org_guid=unit["care_unit_guid"],
        requester_user_guid=_from("requester_user_guid"),
        received_at=at,
        source_service=REVIEW_SOURCE,
        sim_run_id=_from("sim_run_id"),
        data_origin=None,
        resolved_context_json=with_group(
            {"endorses_guid": original.guid,
             "endorsed_by_user_guid": actor["user_guid"]},
            (_from("resolved_context_json") or {}).get("group")
            if isinstance(_from("resolved_context_json"), dict) else None),
    ))
    return guid


def reject(original, *, actor: dict, unit: dict, reason: str) -> None:
    """Reject `original` with `reason`. One transaction with its audit row."""
    at = _now()
    try:
        _flip(original, status=REJECTED, actor=actor, at=at, reason=reason)
        _audit("review.rejected", actor=actor,
               patient_guid=original.patient_guid,
               snapshot={"original_guid": original.guid,
                         "care_unit_guid": unit["care_unit_guid"],
                         "role": unit.get("role"), "reason": reason})
        db.session.commit()
    except BaseException:
        db.session.rollback()
        raise


# ---------------------------------------------------------------------------
# #1049: deciding a card version as a whole (contract s7.4)
# ---------------------------------------------------------------------------

def card_rows(card_version: str) -> list:
    """The ORIGINALS of one card version (endorsement rows excluded), in a
    stable order. Empty for an unknown or malformed version."""
    norm = _uuid_or_none(card_version)
    if norm is None:
        return []
    Obs = live_model("Observation")
    return (Obs.query
            .filter(Obs.card_version == norm, Obs.review_status.isnot(None))
            .order_by(Obs.transaction_guid, Obs.received_at, Obs.guid).all())


def card_state(rows: list) -> str | None:
    """The version's status: one value for all its rows by construction
    (ingest refuses a row into a closed version; supersede and a decision
    move every pending row at once). None for a mixed version, which is
    treated as not decidable."""
    states = {r.review_status for r in rows}
    return states.pop() if len(states) == 1 else None


def _raise_closed(card_version: str):
    db.session.expire_all()
    if card_state(card_rows(card_version)) == SUPERSEDED:
        raise Superseded(card_version)
    raise AlreadyDecided(card_version)


def approve_card(rows: list, *, actor: dict, unit: dict, amend: dict | None = None,
                 note: str | None = None) -> tuple[int, int, list[str]]:
    """Endorse every row of one card version. -> (endorsed, amended, guids).

    `amend` maps an original's guid to its corrected value; every key must
    be a row of this version and every value is validated BEFORE the first
    write. One transaction for all flips, all endorsement rows and the audit
    row: if any row is no longer pending (a concurrent decision or a newer
    version superseding it) nothing is written and Superseded /
    AlreadyDecided is raised.
    """
    amend = amend or {}
    by_guid = {r.guid: r for r in rows}
    unknown = sorted(set(amend) - set(by_guid))
    if unknown:
        raise InvalidValue("unknown_row", "amend names rows that are not in "
                                          f"this card version: {unknown}")
    prepared = []
    for row in rows:
        base, amended = _prepare(row, amend.get(row.guid))   # may raise
        prepared.append((row, base, amended))
    at = _now()
    version = rows[0].card_version
    try:
        guids = [_endorse(row, base, amended, actor=actor, unit=unit,
                          note=note, at=at)
                 for row, base, amended in prepared]
        n_amended = sum(1 for _r, _b, a in prepared if a)
        _audit("review.card_approved", actor=actor,
               patient_guid=rows[0].patient_guid,
               snapshot={"card_version": version,
                         "card_section": rows[0].card_section,
                         "original_guids": [r.guid for r in rows],
                         "endorsement_guids": guids, "amended": n_amended,
                         "care_unit_guid": unit["care_unit_guid"],
                         "role": unit.get("role"), "has_note": bool(note)})
        db.session.commit()
    except AlreadyDecided:
        db.session.rollback()
        _raise_closed(version)
    except BaseException:
        db.session.rollback()
        raise
    return len(guids), n_amended, guids


def reject_card(rows: list, *, actor: dict, unit: dict, reason: str) -> int:
    """Reject every row of one card version with `reason`. One transaction
    with its audit row; nothing written if any row is no longer pending."""
    at = _now()
    version = rows[0].card_version
    try:
        for row in rows:
            _flip(row, status=REJECTED, actor=actor, at=at, reason=reason)
        _audit("review.card_rejected", actor=actor,
               patient_guid=rows[0].patient_guid,
               snapshot={"card_version": version,
                         "card_section": rows[0].card_section,
                         "original_guids": [r.guid for r in rows],
                         "care_unit_guid": unit["care_unit_guid"],
                         "role": unit.get("role"), "reason": reason})
        db.session.commit()
    except AlreadyDecided:
        db.session.rollback()
        _raise_closed(version)
    except BaseException:
        db.session.rollback()
        raise
    return len(rows)


def card_versions(rows: list) -> list[dict]:
    """Group originals into versions: [{card_version, patient_guid,
    section, patient_org_guid, status, submitted_at, decided_at, reason,
    rows: [...]}], oldest submission first. `submitted_at` is the earliest
    receipt of the version's rows."""
    groups = groups_of([r.ingest_raw_guid for r in rows])
    out: dict[str, dict] = {}
    for r in rows:
        v = out.setdefault(r.card_version, {
            "card_version": r.card_version, "patient_guid": r.patient_guid,
            "section": r.card_section, "patient_org_guid": r.patient_org_guid,
            "_rows": []})
        v["_rows"].append(r)
    result = []
    for v in out.values():
        rs = v.pop("_rows")
        v["status"] = card_state(rs)
        v["submitted_at"] = min((r.received_at for r in rs if r.received_at),
                                default=None)
        v["decided_at"] = max((r.reviewed_at for r in rs if r.reviewed_at),
                              default=None)
        v["reason"] = next((r.review_reason for r in rs if r.review_reason), None)
        v["originals"] = rs
        v["groups"] = groups
        result.append(v)
    result.sort(key=lambda v: (v["submitted_at"] is None,
                               v["submitted_at"] or datetime.min,
                               v["card_version"]))
    return result


def answer_of(row, groups: dict) -> dict:
    """One answer as the patient gave it: link_id, group, value, display."""
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    _key, value, _unit = value_of(raw)
    return {"link_id": link_id_of(row), "group": groups.get(row.ingest_raw_guid),
            "value": value, "display": display_of(raw)}


def endorsed_card(patient_guid: str, section: str):
    """The currently endorsed version of one section: the one decided last
    (`reviewed_at`), as {"card_version", "reviewed_at", "originals",
    "endorsements"}; or None."""
    Obs = live_model("Observation")
    latest = (Obs.query
              .filter(Obs.patient_guid == patient_guid,
                      Obs.card_section == section,
                      Obs.review_status == ENDORSED)
              .order_by(Obs.reviewed_at.desc(), Obs.card_version.desc())
              .first())
    if latest is None:
        return None
    originals = card_rows(latest.card_version)
    endorsements = (Obs.query
                    .filter(Obs.card_version == latest.card_version,
                            Obs.endorses_guid.isnot(None),
                            Obs.data_origin.is_(None))
                    .order_by(Obs.transaction_guid, Obs.guid).all())
    return {"card_version": latest.card_version,
            "reviewed_at": latest.reviewed_at,
            "originals": originals, "endorsements": endorsements}


def audit_denied(*, actor: dict, guid: str, action: str, why: str) -> None:
    """A refused decision is recorded too. Best-effort and in its own
    transaction: it carries no patient and no row content, because the
    caller was not entitled to learn either."""
    try:
        _audit("review.denied", actor=actor, patient_guid=None,
               snapshot={"target_guid": guid, "action": action, "why": why})
        db.session.commit()
    except Exception:   # noqa: BLE001 - a failed audit must not turn 403 into 500
        db.session.rollback()


def endorsement_guids(original_guids: list[str]) -> dict[str, str]:
    """original guid -> its endorsement row's guid."""
    if not original_guids:
        return {}
    Obs = live_model("Observation")
    rows = (db.session.query(Obs.endorses_guid, Obs.guid)
            .filter(Obs.endorses_guid.in_(original_guids)).all())
    return {orig: guid for orig, guid in rows}


# ---------------------------------------------------------------------------
# What the EPS derivation may read (contract s4.4)
# ---------------------------------------------------------------------------

def export_endorsements(patient_guid: str | None = None) -> list[dict]:
    """The rows eps.pdhc may derive patient-reported EPS content from.

    ENDORSEMENT rows only: `endorses_guid` set AND `data_origin` NULL. A row
    with data_origin = 'patient_generated' is never in this export, whatever
    its review_status: an original is the patient's statement, and only
    care's endorsement of it is a source for the summary. Both conditions
    are in the query, so a row that somehow carried both marks is left out
    rather than trusted.

    One object per row, in the shape eps.pdhc's derivation reads (contract
    Deviations, [ips]): `data_origin` is PRESENT on every row and null;
    `reviewed_by_*` / `reviewed_at` name the endorsing professional (the
    attester in the EU IPS header); `author_org_guid` is the care unit they
    acted for; `group` ties the fields of one repeating-group instance
    together when the producer sent one, else null.
    """
    from app.api.external_read import cdr_id

    Obs = live_model("Observation")
    q = Obs.query.filter(Obs.endorses_guid.isnot(None),
                         Obs.data_origin.is_(None))
    if patient_guid:
        q = q.filter(Obs.patient_guid == patient_guid)
    rows = q.order_by(Obs.patient_guid, Obs.reviewed_at, Obs.guid).all()
    groups = groups_of([r.ingest_raw_guid for r in rows])
    out = []
    for row in rows:
        raw = row.raw_json if isinstance(row.raw_json, dict) else {}
        _key, value, unit = value_of(raw)
        link_id = link_id_of(row)
        out.append({
            "cdr": cdr_id(),
            "guid": row.guid,
            "patient_guid": row.patient_guid,
            "patient_org_guid": row.patient_org_guid,
            "section": section_of(link_id),
            "link_id": link_id,
            "group": groups.get(row.ingest_raw_guid),
            "label": label_of(raw),
            "code": row.code_canonical,
            "value": value,
            "unit": unit,
            "effective_at": row.effective_at.isoformat() if row.effective_at else None,
            "data_origin": row.data_origin,
            "review_status": row.review_status,
            "endorses_guid": row.endorses_guid,
            "reviewed_by_user_guid": row.reviewed_by_user_guid,
            "reviewed_by_name": row.reviewed_by_name,
            "reviewed_at": _utc_iso(row.reviewed_at),
            # #1049 (s7.4): eps keeps, per patient and section, only the
            # rows of the latest endorsed card version (by reviewed_at).
            "card_section": row.card_section,
            "card_version": row.card_version,
            "author_org_guid": row.org_guid,
            "raw_json": raw,
        })
    return out
