"""#1059 (record.pdhc stage 6b): EVERYTHING this CDR holds about one patient.

    GET /api/v1/patient-extract/<patient guid>

Contract: plans/record_pdhc_extract_contract.md (schema
`pdhc.patient-extract/1`). record.pdhc assembles the parts of every service
into the patient's journal copy (PDL 8 kap.) / register extract (GDPR art. 15).

THE DOOR AND THE GATE are the access log's (#1045, app/api/access_log.py),
imported rather than copied so the two cannot drift:
  * the professional's own SSO bearer, validated with sso.pdhc per request;
    a service key is never looked at (this path is self-authenticating in
    `auth._public_path`);
  * THE GATE IS NARROWER than the access log's (D9): SU, or a professional
    holding an affiliation in role `doctor` or `nurse` to a care unit that
    is the `patient_org_guid` of a row of this patient here
    (`d9_allows`). sso puts only ACTIVE affiliations in the blob
    (`build_affiliations_for_blob`), so an ended one is never seen; the
    role is `affiliations[].role`, as in eps 211a7c7 and sso 013f544. Any
    other role at the unit (e.g. `other_care`, who may read the access log)
    gets the same flat 403 as a patient not held here, so probing learns
    nothing -- never an empty extract.
  * Every call is logged in `cdr_read_audit`, refusals included
    (resource_type `PatientExtract`, detail.action `patient_extract`,
    outcome served / denied).

WHAT IS IN IT: every per-type row of the patient, LIVE and HISTORY, so ALL
versions, oldest first. The Patient row (demographics, the personnummer as
an identifier) is deliberately left out: personnummer appears only in eps's
identity section (contract). No truncation: `complete` is always true here,
because nothing is cut.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify

from app import db
from app.api.access_log import _FORBIDDEN, _RESOURCE_MODELS, _err, _person
from app.auth import patient_scope_clause
from app.models import IngestRaw
from app.models.resources import history_model, live_model
from app.services.read_audit import record_read_audit

bp = Blueprint("patient_extract", __name__)

#: D9: the affiliation roles that may take out an extract (läkare,
#: sjuksköterska). Same set as eps and sso.
EXTRACT_ROLES = frozenset({"doctor", "nurse"})


def d9_units(blob: dict) -> list[str]:
    """The care units where the caller is affiliated in an
    extract role. An affiliation that says it is not active is skipped
    (sso emits active ones only; this is belt and braces)."""
    out = set()
    for a in blob.get("affiliations") or []:
        if not isinstance(a, dict) or not a.get("care_unit_guid"):
            continue
        if str(a.get("role") or "").strip().lower() not in EXTRACT_ROLES:
            continue
        if a.get("status") not in (None, "active") or a.get("ended_at"):
            continue
        out.add(a["care_unit_guid"])   # as given, like auth.scope_org_guids
    return sorted(out)


def d9_allows(blob: dict, patient_guid: str) -> bool:
    """D9: SU; or a professional whose doctor/nurse unit holds a
    `patient_org_guid` of this patient in this CDR."""
    if blob.get("is_su_admin"):
        return True
    if blob.get("user_type") != "professional":
        return False
    units = d9_units(blob)
    if not units:
        return False
    for Model in _RESOURCE_MODELS.values():
        if (db.session.query(Model.guid)
                .filter(Model.patient_guid == patient_guid)
                .filter(patient_scope_clause(Model, units))
                .first()) is not None:
            return True
    return False

SCHEMA = "pdhc.patient-extract/1"

#: (section key, Swedish title, resource types). Order = the order in the PDF.
#: `endorsements` are care's endorsement rows (#1044, `endorses_guid` set);
#: they are NOT also counted under their type's own section.
SECTIONS = (
    ("encounters", "Vårdkontakter", ("Encounter",)),
    ("clinical_impressions", "Journalanteckningar och medicinska beslut", ("ClinicalImpression",)),
    ("observations", "Observationer och svar", ("Observation",)),
    ("questionnaire_responses", "Formulärsvar", ("QuestionnaireResponse",)),
    ("conditions", "Diagnoser", ("Condition",)),
    ("medications", "Läkemedel", ("MedicationStatement", "MedicationRequest")),
    ("allergies", "Allergier och överkänslighet", ("AllergyIntolerance",)),
    ("procedures", "Åtgärder", ("Procedure",)),
    ("diagnostic_reports", "Svarsrapporter", ("DiagnosticReport",)),
    ("endorsements", "Vidimerade patientsvar", None),   # None = every type
)
EXTRACT_TYPES = ("Encounter", "ClinicalImpression", "Observation",
                 "QuestionnaireResponse", "Condition", "MedicationStatement",
                 "MedicationRequest", "AllergyIntolerance", "Procedure",
                 "DiagnosticReport")


def service_name() -> str:
    from app.api.external_read import cdr_id
    return f"{cdr_id()}.pdhc"


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _status_of(row) -> str | None:
    """The row's status: the column where the type has one, else the body
    (Condition's clinicalStatus, AllergyIntolerance's clinical_status)."""
    for attr in ("status", "clinical_status"):
        v = getattr(row, attr, None)
        if v:
            return v
    body = row.raw_json if isinstance(row.raw_json, dict) else {}
    if isinstance(body.get("status"), str):
        return body["status"]
    cs = (body.get("clinicalStatus") or {}).get("coding") or [{}]
    return cs[0].get("code") if isinstance(cs[0], dict) else None


def _text(cc) -> str | None:
    if not isinstance(cc, dict):
        return None
    if cc.get("text"):
        return str(cc["text"])
    for c in cc.get("coding") or []:
        if isinstance(c, dict) and (c.get("display") or c.get("code")):
            return str(c.get("display") or c.get("code"))
    return None


def _num(v) -> str:
    """6.1 -> '6,1' (Swedish decimal comma)."""
    s = f"{v:g}" if isinstance(v, (int, float)) else str(v)
    return s.replace(".", ",")


#: record.pdhc's JournalBeslut profile (#1057): marks a ClinicalImpression
#: that is a medical decision rather than a journal note.
DECISION_PROFILE = "https://record.pdhc.se/fhir/StructureDefinition/journal-beslut"


def summary_sv(rtype: str, body: dict, term: str | None = None) -> str:
    """One short human line for the PDF.

    `term` is plan.pdhc's name for the row's concept (the same label the
    dashboard's charts show). It wins over the FHIR body because most ingested
    observations carry only the concept GUID there, so the extract printed
    `urn:pdhc:concept/<guid>` where a reader needs "Stress"."""
    body = body if isinstance(body, dict) else {}
    name = term or _text(body.get("code"))
    if rtype == "Observation":
        if "valueQuantity" in body and isinstance(body["valueQuantity"], dict):
            vq = body["valueQuantity"]
            val = f"{_num(vq.get('value'))} {vq.get('unit') or vq.get('code') or ''}".strip()
        else:
            val = (_text(body.get("valueCodeableConcept")) or body.get("valueString")
                   or body.get("valueDateTime") or body.get("valueBoolean"))
        return " ".join(x for x in (name or "Observation", str(val) if val is not None else None) if x)
    if rtype == "Encounter":
        cls = body.get("class")
        cls = _text(cls[0] if isinstance(cls, list) and cls else cls)
        start = (body.get("period") or {}).get("start") or \
            (body.get("actualPeriod") or {}).get("start")
        return " ".join(x for x in ("Vårdkontakt", cls, start) if x)
    if rtype == "ClinicalImpression":
        # #1057: a decision carries record's JournalBeslut profile; a stage-3
        # journal note is a ClinicalImpression too and must not be called one.
        label = ("Medicinskt beslut" if DECISION_PROFILE in
                 ((body.get("meta") or {}).get("profile") or []) else "Journalanteckning")
        text = body.get("summary") or body.get("description") or ""
        return (f"{label}: " + str(text))[:160].rstrip() if text else label
    if rtype in ("MedicationStatement", "MedicationRequest"):
        med = body.get("medication")
        med = _text(med.get("concept") if isinstance(med, dict) and "concept" in med else med) \
            or _text(body.get("medicationCodeableConcept"))
        return med or "Läkemedel"
    if rtype == "QuestionnaireResponse":
        return f"Formulärsvar {body.get('questionnaire') or ''}".strip()
    if rtype == "DiagnosticReport":
        return " ".join(x for x in (name or "Svarsrapport", body.get("conclusion")) if x)[:160]
    return name or rtype


def _term(row) -> str | None:
    """plan.pdhc's display for the row's code_canonical; None when the row has
    none, plan is not configured or does not answer (cosmetic, never raises,
    cached per concept by the plan client)."""
    code = getattr(row, "code_canonical", None)
    if not code:
        return None
    from app.api.clinical_read import resolve_display  # lazy: avoid an import cycle
    return resolve_display(code)


def _author(row, raws: dict) -> dict:
    """Who wrote this version: a reviewer for an endorsement row (#1044);
    the professional record.pdhc wrote on behalf of (its ingest_raw);
    otherwise only the writing service is known (never a guess)."""
    raw = raws.get(getattr(row, "ingest_raw_guid", None))
    payload = raw.payload_json if raw is not None and isinstance(raw.payload_json, dict) else {}
    guid = name = role = None
    if getattr(row, "endorses_guid", None):
        guid, name = row.reviewed_by_user_guid, row.reviewed_by_name
    elif payload.get("on_behalf_of_user_guid"):
        guid = payload["on_behalf_of_user_guid"]
        role = payload.get("on_behalf_role")
    return {"guid": guid, "name": name, "role": role, "service": row.source}


def _version(rtype: str, row, raws: dict, *, live: bool) -> dict:
    if live:
        recorded = row.updated_at
    else:
        recorded = row.version_written_at
        raw = raws.get(row.ingest_raw_guid)
        if recorded is None and raw is not None:
            recorded = raw.received_at
    return {
        "version": row.version_id,
        "event_time": _iso(row.effective_at),
        "recorded_at": _iso(recorded),
        "author": _author(row, raws),
        "status": _status_of(row),
        "summary_sv": summary_sv(rtype, row.raw_json, _term(row)),
        "fhir": row.raw_json,
    }


def _items(rtype: str, patient: str) -> list[dict]:
    """One item per resource of this type, every version, oldest first."""
    Live, Hist = live_model(rtype), history_model(rtype)
    lives = {r.guid: r for r in Live.query.filter(Live.patient_guid == patient).all()}
    hists: dict[str, list] = {}
    for h in Hist.query.filter(Hist.patient_guid == patient).all():
        hists.setdefault(h.guid, []).append(h)
    raw_ids = {r.ingest_raw_guid for r in lives.values() if r.ingest_raw_guid}
    raw_ids |= {h.ingest_raw_guid for hs in hists.values() for h in hs if h.ingest_raw_guid}
    raws = ({r.guid: r for r in IngestRaw.query.filter(IngestRaw.guid.in_(raw_ids)).all()}
            if raw_ids else {})
    out = []
    for guid in sorted(set(lives) | set(hists)):
        live = lives.get(guid)
        versions = [_version(rtype, h, raws, live=False)
                    for h in sorted(hists.get(guid, []), key=lambda h: h.version_id)]
        if live is not None:
            versions.append(_version(rtype, live, raws, live=True))
        anchor = live if live is not None else hists[guid][-1]
        out.append({
            "resource_type": rtype,
            "id": guid,
            # NULL when only history remains: no current version exists.
            "current_status": _status_of(live) if live is not None else None,
            "endorsement": bool(getattr(anchor, "endorses_guid", None)),
            "versions": versions,
        })
    return out


def build_extract(patient: str) -> dict:
    by_type = {t: _items(t, patient) for t in EXTRACT_TYPES}
    chosen = []
    for key, title, types in SECTIONS:
        if types is None:
            items = [i for t in EXTRACT_TYPES for i in by_type[t] if i["endorsement"]]
        else:
            items = [i for t in types for i in by_type[t] if not i["endorsement"]]
        chosen.append((key, title, items))
    for t in EXTRACT_TYPES:            # the flag only routes; it never leaves
        for i in by_type[t]:
            del i["endorsement"]
    sections = [{"key": k, "title_sv": t, "count": len(items), "items": items}
                for k, t, items in chosen if items]
    notes = []
    if not sections:
        notes.append(f"{service_name()} holds no clinical data for this patient")
    notes.append("Patient demographics are not part of this section; identity "
                 "is eps.pdhc's section")
    if any(v["recorded_at"] is None for s in sections for i in s["items"]
           for v in i["versions"]):
        notes.append("recorded_at is null for versions written before the CDR "
                     "kept a per-version registration time (#1054)")
    return {
        "schema": SCHEMA,
        "service": service_name(),
        "patient_guid": patient,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "complete": True,
        "sections": sections,
        "notes": notes,
    }


@bp.get("/patient-extract/<patient_guid>")
def patient_extract(patient_guid: str):
    blob, refused = _person()
    if refused:
        return refused
    try:
        guid = str(uuid.UUID(patient_guid))
    except (ValueError, TypeError):
        return _err(400, "invalid_patient_guid", "the patient guid is not a GUID")
    if not d9_allows(blob, guid):
        record_read_audit(resource_type="PatientExtract", patient_guid=guid,
                          response_status=403, outcome="denied",
                          detail={"refusal_code": "OUT_OF_SCOPE",
                                  "action": "patient_extract"})
        return jsonify(_FORBIDDEN), 403
    body = build_extract(guid)
    n = sum(len(i["versions"]) for s in body["sections"] for i in s["items"])
    record_read_audit(resource_type="PatientExtract", patient_guid=guid,
                      n_rows=n, detail={"action": "patient_extract"})
    return jsonify(body)
