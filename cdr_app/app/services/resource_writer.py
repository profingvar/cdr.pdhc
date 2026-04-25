"""Generic FHIR resource writer for the CDR.

One ``write_resource()`` per inbound FHIR resource. Handles the full
write path described in execution plan §1.2:

  - Dedup lookup (existing row → idempotent return).
  - Insert vs. update.
  - On update: copy live row to the matching ``*_history`` table and
    increment ``version_id`` on the new live row (§1.2.g).
  - Optimistic-concurrency check via ``If-Match`` ETag (§1.2.h, raises
    ``EtagMismatch`` on a stale tag).
  - Mint or attach a ``sync_group`` row (§1.2.i, §1.1.g).
  - Stamp ``mapping_version`` (§1.2.j).
  - Provenance stamping in the live ``raw_json`` (§1.2.f).
  - Append a ``change_feed`` row (§1.5).

Caller chooses the resource type at dispatch time (Bundle endpoint or
per-type endpoint) and passes the FHIR body. The writer figures out
which per-type table to write to via :func:`app.models.resources.live_model`.

Canonicalisation is **not** done here — call ``Canonicaliser`` first
and pass the rewritten resource in. This keeps the writer stateless
about external services and easier to unit-test.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import inspect

from app import db
from app.models.resources import (
    ChangeFeed,
    SyncGroup,
    history_model,
    live_model,
)


log = logging.getLogger(__name__)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class WriterError(Exception):
    """Generic writer failure; subclasses below."""


class UnknownResourceType(WriterError):
    """The ``resourceType`` is not handled by the per-type tables."""


class EtagMismatch(WriterError):
    """``If-Match`` was provided and did not match the current row's ETag.

    HTTP layer turns this into 412 Precondition Failed.
    """


class IntegerPatientReference(WriterError):
    """Rule 18 — patient references must be GUIDs, not integers."""


# ---------------------------------------------------------------------------
# Write context
# ---------------------------------------------------------------------------

@dataclass
class WriteContext:
    """Per-request context the writer needs in addition to the resource body."""

    org_guid: str
    source: str  # calling service (e.g. "gateway.pdhc")
    source_request_id: str | None = None
    sim_run_id: str | None = None
    if_match_etag: str | None = None  # request header value, if provided
    mapping_version: str | None = None
    primary_canonical_uri: str | None = None  # filled in by canonicaliser


# ---------------------------------------------------------------------------
# Dedup keys per resource type — execution plan §1.2.e
# ---------------------------------------------------------------------------

def _dedup_key(resource_type: str, fhir: dict, *, patient_guid: str | None,
               primary_canonical_uri: str | None) -> str | None:
    """Return a stable deterministic dedup key, or None if no key applies.

    The key is hashed into a string used to look up an existing row. The
    actual lookup is on (patient_guid, code_canonical, dedup_key_hash) —
    keeping the index narrow and Postgres-friendly.
    """
    if resource_type == "Observation":
        # (patient_guid, code_canonical, effective_at, value)
        eff = fhir.get("effectiveDateTime") or fhir.get("effectivePeriod") or fhir.get("issued")
        value = fhir.get("valueQuantity") or fhir.get("valueString") or fhir.get("valueCodeableConcept") \
                or fhir.get("valueBoolean") or fhir.get("valueInteger") or fhir.get("valueRatio")
        return _hash([patient_guid, primary_canonical_uri, eff, value])
    if resource_type == "QuestionnaireResponse":
        # (patient_guid, questionnaire_canonical, authored_at)
        return _hash([
            patient_guid, fhir.get("questionnaire"), fhir.get("authored")
        ])
    if resource_type == "Encounter":
        period = fhir.get("period") or {}
        return _hash([patient_guid, period.get("start")])
    if resource_type == "Condition":
        onset = fhir.get("onsetDateTime") or fhir.get("onsetPeriod") or fhir.get("onsetAge")
        return _hash([patient_guid, primary_canonical_uri, onset])
    if resource_type == "MedicationStatement":
        eff = fhir.get("effectivePeriod") or fhir.get("effectiveDateTime")
        return _hash([patient_guid, primary_canonical_uri, eff])
    if resource_type == "MedicationRequest":
        return _hash([patient_guid, primary_canonical_uri, fhir.get("authoredOn")])
    if resource_type == "AllergyIntolerance":
        return _hash([patient_guid, primary_canonical_uri])
    if resource_type == "Procedure":
        return _hash([
            patient_guid,
            primary_canonical_uri,
            fhir.get("performedDateTime") or fhir.get("performedPeriod"),
        ])
    if resource_type == "DiagnosticReport":
        return _hash([
            patient_guid, primary_canonical_uri, fhir.get("issued"), fhir.get("effectiveDateTime"),
        ])
    if resource_type == "Patient":
        # Identifier-based dedup: first identifier on the body.
        idents = fhir.get("identifier") or []
        if idents:
            i0 = idents[0]
            return _hash(["Patient", i0.get("system"), i0.get("value")])
        return None
    return None


def _hash(parts: list[Any]) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Per-type column extractors — pull common-shaped columns out of FHIR.
# ---------------------------------------------------------------------------

def _extract_patient_guid(fhir: dict, resource_type: str) -> str | None:
    """Patient resources have no ``patient_guid`` (they ARE the patient)."""
    if resource_type == "Patient":
        return None
    ref = (fhir.get("subject") or {}).get("reference") \
        or (fhir.get("patient") or {}).get("reference")
    if not ref:
        return None
    # FHIR reference shape: "Patient/<guid>". Strip the prefix.
    if "/" in ref:
        ref = ref.rsplit("/", 1)[-1]
    if ref.isdigit():
        # Rule 18 — integer patient ids are forbidden across services.
        raise IntegerPatientReference(
            f"Patient reference '{ref}' is an integer; PDHC requires GUIDs."
        )
    return ref


def _extract_effective_at(fhir: dict, resource_type: str) -> datetime | None:
    candidates = []
    if resource_type == "Observation":
        candidates = [fhir.get("effectiveDateTime"), fhir.get("issued"),
                      (fhir.get("effectivePeriod") or {}).get("start")]
    elif resource_type == "QuestionnaireResponse":
        candidates = [fhir.get("authored")]
    elif resource_type == "Condition":
        candidates = [fhir.get("onsetDateTime"),
                      (fhir.get("onsetPeriod") or {}).get("start")]
    elif resource_type == "Encounter":
        candidates = [(fhir.get("period") or {}).get("start")]
    elif resource_type in ("MedicationStatement",):
        candidates = [fhir.get("effectiveDateTime"),
                      (fhir.get("effectivePeriod") or {}).get("start")]
    elif resource_type == "MedicationRequest":
        candidates = [fhir.get("authoredOn")]
    elif resource_type == "Procedure":
        candidates = [fhir.get("performedDateTime"),
                      (fhir.get("performedPeriod") or {}).get("start")]
    elif resource_type == "DiagnosticReport":
        candidates = [fhir.get("issued"), fhir.get("effectiveDateTime")]
    elif resource_type == "AllergyIntolerance":
        candidates = [fhir.get("recordedDate"), fhir.get("onsetDateTime")]
    for c in candidates:
        if c:
            return _parse_datetime(c)
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromisoformat(s.split("T")[0])
        except ValueError:
            return None


def _extract_per_type_columns(fhir: dict, resource_type: str) -> dict:
    """Pull flat columns specific to a resource type out of the FHIR body."""
    out: dict = {}
    if resource_type == "Patient":
        out["identifiers"] = fhir.get("identifier")
        out["names"] = fhir.get("name")
        out["gender"] = fhir.get("gender")
        bd = fhir.get("birthDate")
        if bd:
            try:
                out["birth_date"] = datetime.fromisoformat(bd).date()
            except ValueError:
                out["birth_date"] = None
        out["active"] = bool(fhir.get("active", True))
    elif resource_type == "Observation":
        vq = fhir.get("valueQuantity") or {}
        out["value_quantity"] = vq.get("value")
        out["value_unit"] = vq.get("unit") or vq.get("code")
        out["value_string"] = fhir.get("valueString")
        vc = fhir.get("valueCodeableConcept") or {}
        if vc.get("coding"):
            out["value_code"] = vc["coding"][0].get("code")
        out["status"] = fhir.get("status")
    elif resource_type == "QuestionnaireResponse":
        out["questionnaire_canonical"] = fhir.get("questionnaire")
        out["authored_at"] = _parse_datetime(fhir.get("authored"))
        out["status"] = fhir.get("status")
    elif resource_type == "Condition":
        out["onset_at"] = _parse_datetime(fhir.get("onsetDateTime"))
        out["abatement_at"] = _parse_datetime(fhir.get("abatementDateTime"))
        cs = fhir.get("clinicalStatus") or {}
        if cs.get("coding"):
            out["clinical_status"] = cs["coding"][0].get("code")
        vs = fhir.get("verificationStatus") or {}
        if vs.get("coding"):
            out["verification_status"] = vs["coding"][0].get("code")
    elif resource_type == "MedicationStatement":
        med = fhir.get("medicationCodeableConcept") or {}
        if med.get("coding"):
            out["medication_canonical"] = _coding_canonical_uri(med["coding"][0])
        period = fhir.get("effectivePeriod") or {}
        out["effective_period_start"] = _parse_datetime(period.get("start"))
        out["effective_period_end"] = _parse_datetime(period.get("end"))
        out["status"] = fhir.get("status")
    elif resource_type == "MedicationRequest":
        med = fhir.get("medicationCodeableConcept") or {}
        if med.get("coding"):
            out["medication_canonical"] = _coding_canonical_uri(med["coding"][0])
        out["authored_on"] = _parse_datetime(fhir.get("authoredOn"))
        out["status"] = fhir.get("status")
        out["intent"] = fhir.get("intent")
    elif resource_type == "AllergyIntolerance":
        out["type"] = fhir.get("type")
        cats = fhir.get("category") or []
        out["category"] = cats[0] if cats else None
        out["criticality"] = fhir.get("criticality")
        cs = fhir.get("clinicalStatus") or {}
        if cs.get("coding"):
            out["clinical_status"] = cs["coding"][0].get("code")
    elif resource_type == "Procedure":
        out["performed_at"] = _parse_datetime(
            fhir.get("performedDateTime")
            or (fhir.get("performedPeriod") or {}).get("start")
        )
        out["status"] = fhir.get("status")
    elif resource_type == "Encounter":
        period = fhir.get("period") or {}
        out["period_start"] = _parse_datetime(period.get("start"))
        out["period_end"] = _parse_datetime(period.get("end"))
        out["status"] = fhir.get("status")
        cls = fhir.get("class") or {}
        if cls.get("coding"):
            out["class_code"] = cls["coding"][0].get("code")
        elif cls.get("code"):
            out["class_code"] = cls.get("code")
    elif resource_type == "DiagnosticReport":
        out["issued_at"] = _parse_datetime(fhir.get("issued"))
        out["status"] = fhir.get("status")
        out["conclusion"] = fhir.get("conclusion")
    return out


def _coding_canonical_uri(coding: dict) -> str | None:
    sys_uri = coding.get("system", "")
    code = coding.get("code", "")
    if not sys_uri or not code:
        return None
    if sys_uri.startswith("https://termbank.pdhc.se/"):
        return f"{sys_uri.rstrip('/')}/{code}"
    return None


# ---------------------------------------------------------------------------
# Provenance stamping (§1.2.f)
# ---------------------------------------------------------------------------

def _stamp_provenance(fhir: dict, ctx: WriteContext) -> dict:
    """Return a copy of ``fhir`` with ``meta.source / .tag / .security`` set
    per §1.2.f. Never mutates the input."""
    out = dict(fhir)
    meta = dict(out.get("meta") or {})
    meta["source"] = f"{ctx.source}|{ctx.source_request_id or ''}"

    tags = list(meta.get("tag") or [])
    tags.append({
        "system": "https://cdr.pdhc.se/CodeSystem/ingest",
        "code": "ingest_at",
        "display": _now().isoformat(),
    })
    if ctx.sim_run_id:
        tags.append({
            "system": "https://cdr.pdhc.se/CodeSystem/sim",
            "code": "sim_run_id",
            "display": ctx.sim_run_id,
        })
    meta["tag"] = tags

    sec = list(meta.get("security") or [])
    sec.append({
        "system": "https://cdr.pdhc.se/CodeSystem/org",
        "code": "org_guid",
        "display": ctx.org_guid,
    })
    meta["security"] = sec

    out["meta"] = meta
    return out


# ---------------------------------------------------------------------------
# The main entry point
# ---------------------------------------------------------------------------

@dataclass
class WriteOutcome:
    """What ``write_resource`` returns to the HTTP layer."""

    operation: str  # 'created' | 'updated' | 'unchanged'
    resource: dict  # full FHIR resource as persisted
    etag: str
    version_id: int
    sync_group_id: str
    location: str   # FHIR-shaped "<Type>/<guid>/_history/<version>"


def write_resource(
    fhir: dict,
    ctx: WriteContext,
    *,
    resource_id: str | None = None,
    update_by_guid: str | None = None,
) -> WriteOutcome:
    """Persist a single FHIR resource. See module docstring for the full
    write-path contract.

    Modes:
      - POST (default): ``update_by_guid=None``. Use dedup-key lookup;
        if a matching live row exists, update it; otherwise insert a
        new row (with ``resource_id`` if provided, else minted GUID).
      - PUT (update-by-id): ``update_by_guid=<guid>``. Look up by GUID;
        if found, update; if not found, insert a new row with that
        GUID (FHIR "update as create" semantics).
    """
    rt = fhir.get("resourceType")
    if not rt:
        raise WriterError("resource has no resourceType")
    Live = live_model(rt)
    Hist = history_model(rt)
    if Live is None or Hist is None:
        raise UnknownResourceType(f"resourceType '{rt}' has no per-type table")

    patient_guid = _extract_patient_guid(fhir, rt)
    effective_at = _extract_effective_at(fhir, rt)
    code_canonical = ctx.primary_canonical_uri or _primary_code_canonical(fhir, rt)

    # Stamp provenance into a copy of the body before we hash / store it.
    stamped = _stamp_provenance(fhir, ctx)

    if update_by_guid is not None:
        existing = db.session.query(Live).filter(
            Live.guid == update_by_guid
        ).one_or_none()
    else:
        dedup_key = _dedup_key(rt, fhir,
                               patient_guid=patient_guid,
                               primary_canonical_uri=code_canonical)
        existing = _find_existing(Live, dedup_key, patient_guid, code_canonical, fhir, rt)

    if existing is None:
        return _insert(Live, stamped, ctx,
                       resource_id=resource_id or update_by_guid,
                       resource_type=rt,
                       patient_guid=patient_guid,
                       effective_at=effective_at,
                       code_canonical=code_canonical)

    # Update path: copy live row to history, bump version_id, update.
    return _update_with_history(
        Live, Hist, existing, stamped, ctx,
        resource_type=rt,
        patient_guid=patient_guid,
        effective_at=effective_at,
        code_canonical=code_canonical,
    )


def _primary_code_canonical(fhir: dict, resource_type: str) -> str | None:
    """If the canonicaliser didn't explicitly hand us a primary canonical
    (e.g. resource type with no code paths), fall back to inspecting the
    body for a termbank-shaped coding."""
    candidates: list[dict] = []
    for path in ("code", "valueCodeableConcept", "medicationCodeableConcept"):
        cc = fhir.get(path)
        if cc and cc.get("coding"):
            candidates.append(cc["coding"][0])
            break
    for c in candidates:
        u = _coding_canonical_uri(c)
        if u:
            return u
    return None


def _find_existing(Live, dedup_key, patient_guid, code_canonical, fhir, rt):
    """Look up an existing live row by the dedup key.

    Strategy: filter by patient_guid + code_canonical first to keep the
    candidate set small (those are indexed columns), then verify by
    recomputing the dedup key on the candidate's raw_json.
    """
    if dedup_key is None:
        return None
    q = db.session.query(Live)
    if patient_guid is not None:
        q = q.filter(Live.patient_guid == patient_guid)
    if code_canonical is not None:
        q = q.filter(Live.code_canonical == code_canonical)
    candidates = q.limit(50).all()
    for cand in candidates:
        cand_key = _dedup_key(rt, cand.raw_json or {},
                              patient_guid=patient_guid,
                              primary_canonical_uri=code_canonical)
        if cand_key == dedup_key:
            return cand
    return None


def _make_etag(version_id: int) -> str:
    return f'W/"{version_id}"'


def _insert(Live, fhir: dict, ctx: WriteContext, *,
            resource_id: str | None,
            resource_type: str,
            patient_guid: str | None,
            effective_at: datetime | None,
            code_canonical: str | None) -> WriteOutcome:
    """Create a brand-new row. Mints sync_group, change_feed."""
    guid = resource_id or _uuid()
    version_id = 1
    etag = _make_etag(version_id)

    # Mint a sync_group for this fact (FHIR side; openEHR mirror deferred).
    sync_group_id = _mint_sync_group(
        origin_api="fhir",
        fhir_resource_guid=guid,
        mapping_version=ctx.mapping_version,
    )

    # Embed the persisted ID in the FHIR body so reads round-trip cleanly.
    persisted = dict(fhir)
    persisted["id"] = guid
    persisted_meta = dict(persisted.get("meta") or {})
    persisted_meta["versionId"] = str(version_id)
    persisted_meta["lastUpdated"] = _now().isoformat()
    persisted["meta"] = persisted_meta

    cols = dict(
        guid=guid,
        patient_guid=patient_guid,
        org_guid=ctx.org_guid,
        code_canonical=code_canonical,
        effective_at=effective_at,
        raw_json=persisted,
        source=ctx.source,
        source_request_id=ctx.source_request_id,
        meta_tag=persisted_meta.get("tag"),
        version_id=version_id,
        sync_group_id=sync_group_id,
        mapping_version=ctx.mapping_version,
        etag=etag,
        created_at=_now(),
        updated_at=_now(),
    )
    cols.update(_extract_per_type_columns(persisted, resource_type))

    row = Live(**{k: v for k, v in cols.items()
                  if k in {c.key for c in inspect(Live).columns}})
    db.session.add(row)
    db.session.flush()  # ensure the row is queryable for change_feed FK consistency

    _emit_change_feed(
        event_type="create",
        resource_type=resource_type,
        resource_guid=guid,
        patient_guid=patient_guid,
        org_guid=ctx.org_guid,
        version_id=version_id,
        sync_group_id=sync_group_id,
        code_canonical=code_canonical,
        source_request_id=ctx.source_request_id,
        payload_summary={"operation": "create"},
    )

    # NOTE: callers commit. Services do not call db.session.commit() so that
    # bundle / batch dispatchers can wrap multiple writes in one transaction.
    return WriteOutcome(
        operation="created",
        resource=persisted,
        etag=etag,
        version_id=version_id,
        sync_group_id=sync_group_id,
        location=f"{resource_type}/{guid}/_history/{version_id}",
    )


def _update_with_history(Live, Hist, existing, fhir: dict, ctx: WriteContext, *,
                          resource_type: str,
                          patient_guid: str | None,
                          effective_at: datetime | None,
                          code_canonical: str | None) -> WriteOutcome:
    """Move ``existing`` into the history table, write a new live row with
    incremented ``version_id``."""
    # If-Match check
    if ctx.if_match_etag is not None:
        # Accept either bare `"<n>"` or weak `W/"<n>"` per RFC 7232.
        if ctx.if_match_etag.strip() not in (
            existing.etag,
            existing.etag.replace('W/', '') if existing.etag else "",
            f'"{existing.version_id}"',
        ):
            raise EtagMismatch(
                f"If-Match {ctx.if_match_etag!r} does not match "
                f"current etag {existing.etag!r}"
            )

    # 1) Copy current live row to history.
    hist_cols = {col.key: getattr(existing, col.key)
                 for col in inspect(Live).columns
                 if col.key != "created_at" and col.key != "updated_at"}
    hist_cols["superseded_at"] = _now()
    hist_cols["superseded_by_request_id"] = ctx.source_request_id
    hist_row = Hist(**{k: v for k, v in hist_cols.items()
                       if k in {c.key for c in inspect(Hist).columns}})
    db.session.add(hist_row)

    # 2) Bump version on live row.
    new_version = (existing.version_id or 0) + 1
    new_etag = _make_etag(new_version)

    persisted = dict(fhir)
    persisted["id"] = existing.guid
    pmeta = dict(persisted.get("meta") or {})
    pmeta["versionId"] = str(new_version)
    pmeta["lastUpdated"] = _now().isoformat()
    persisted["meta"] = pmeta

    new_cols = dict(
        patient_guid=patient_guid,
        org_guid=ctx.org_guid,
        code_canonical=code_canonical,
        effective_at=effective_at,
        raw_json=persisted,
        source=ctx.source,
        source_request_id=ctx.source_request_id,
        meta_tag=pmeta.get("tag"),
        version_id=new_version,
        mapping_version=ctx.mapping_version,
        etag=new_etag,
        updated_at=_now(),
    )
    new_cols.update(_extract_per_type_columns(persisted, resource_type))

    for k, v in new_cols.items():
        if k in {c.key for c in inspect(Live).columns}:
            setattr(existing, k, v)

    db.session.flush()

    _emit_change_feed(
        event_type="update",
        resource_type=resource_type,
        resource_guid=existing.guid,
        patient_guid=patient_guid,
        org_guid=ctx.org_guid,
        version_id=new_version,
        sync_group_id=existing.sync_group_id,
        code_canonical=code_canonical,
        source_request_id=ctx.source_request_id,
        payload_summary={"operation": "update", "from_version": new_version - 1},
    )

    # NOTE: callers commit. Services do not call db.session.commit() so that
    # bundle / batch dispatchers can wrap multiple writes in one transaction.
    return WriteOutcome(
        operation="updated",
        resource=persisted,
        etag=new_etag,
        version_id=new_version,
        sync_group_id=existing.sync_group_id,
        location=f"{resource_type}/{existing.guid}/_history/{new_version}",
    )


# ---------------------------------------------------------------------------
# sync_group + change_feed helpers
# ---------------------------------------------------------------------------

def _mint_sync_group(*, origin_api: str, fhir_resource_guid: str,
                      mapping_version: str | None) -> str:
    sg = SyncGroup(
        origin_api=origin_api,
        composition_guid=None,
        fhir_resource_guids=[fhir_resource_guid],
        mapping_version=mapping_version,
    )
    db.session.add(sg)
    db.session.flush()
    return sg.sync_group_id


def _emit_change_feed(**kwargs):
    db.session.add(ChangeFeed(**kwargs))
