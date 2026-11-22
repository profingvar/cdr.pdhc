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
STATUSES = (PENDING, ENDORSED, REJECTED)

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


class InvalidValue(Exception):
    """An amended value that an ingest would not have produced (HTTP 422)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def approve(original, *, actor: dict, unit: dict, value=None,
            note: str | None = None) -> tuple[str, bool]:
    """Endorse `original`. -> (endorsement guid, amended).

    One transaction: flip, endorsement row (+ its raw and context rows),
    audit. Raises AlreadyDecided / InvalidValue with nothing written.
    The caller has already established that `actor` may decide for `unit`.
    """
    from app.services.ingest_pipeline import build_live_observation_row

    raw = original.raw_json if isinstance(original.raw_json, dict) else {}
    _key, old_value, _unit = value_of(raw)
    amended = value is not None and value != old_value
    base = _amended_raw(raw, value) if amended else raw   # may raise, pre-write

    at = _now()
    guid = str(uuid.uuid4())
    fhir = _endorsement_fhir(original, base, guid=guid, actor=actor,
                             unit=unit, note=note, at=at)
    try:
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
            "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "author_org_guid": row.org_guid,
            "raw_json": raw,
        })
    return out
