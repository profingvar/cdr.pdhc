"""#1057 (record.pdhc stage 5): the MEDICAL DECISION as cdr1 stores it.

record.pdhc writes a decision as a ClinicalImpression through its own door
(app/api/record_write.py, #1054) with the elements the IG's JournalBeslut
profile names. This file proves the door keeps every one of them, on every
version, exactly as sent — the traceability of stage 5 is read back FROM these
stored elements, so an element dropped here would silently cut the chain:

  supportingInfo  version-specific Observation refs (the vidimerade results)
                  and journal posts in cdr1
  finding         item -> a version-specific Observation, with basis text
  problem         a LOGICAL reference to a Condition in cdr2 (icd11 writes
                  diagnoses there, D2/#1005): identifier system
                  https://cdr.pdhc.se/fhir/Condition + assigner cdr2. Not a
                  literal `Condition/<id>`: that would resolve in cdr1, where
                  the Condition does not exist.
  extension       journal-patientinformation (information to the patient,
                  the patient's inställning)
  meta.profile    journal-beslut, which is what tells a decision from a note

Also: the patient extract names a decision "Medicinskt beslut" and a plain
note "Journalanteckning" (before #1057 every ClinicalImpression was called a
decision, though stage 3's notes are ClinicalImpressions too).
"""
from __future__ import annotations

import uuid

import pytest

from app import db
from app.models.resources import live_model
from tests.test_fhir_write import clean_tables, fake_canon  # noqa: F401
from tests.test_record_stage2_1054 import (  # noqa: F401
    RECORD_KEY, UNIT, _ci, _enc, _keys, _rec, _wipe_ci,
)
from tests.test_review_1044 import _isolate  # noqa: F401

PROFILE = "https://record.pdhc.se/fhir/StructureDefinition/journal-beslut"
PATINFO = "https://record.pdhc.se/fhir/StructureDefinition/journal-patientinformation"
P = lambda: str(uuid.uuid4())   # noqa: E731


def _decision(pid, enc, obs, cond, **kw):
    return _ci(pid, enc, **{
        "meta": {"profile": [PROFILE]},
        "description": "Bedömning av förhöjt glukos",
        "summary": "Diabetes typ 2. Nytt HbA1c om tre månader.",
        "supportingInfo": [{"reference": f"Observation/{obs}/_history/2",
                            "display": "P-Glukos 7,1 mmol/L"},
                           {"reference": f"Encounter/{enc}/_history/1"}],
        "finding": [{"item": {"reference": {"reference": f"Observation/{obs}/_history/2"}},
                     "basis": "över gränsen"}],
        "problem": [{"type": "Condition",
                     "identifier": {"system": "https://cdr.pdhc.se/fhir/Condition",
                                    "value": cond, "assigner": {"display": "cdr2"}},
                     "display": "5A11 Diabetes mellitus typ 2"}],
        "extension": [{"url": PATINFO, "extension": [
            {"url": "lamnadInformation", "valueMarkdown": "Informerad om diagnosen."},
            {"url": "patientensInstallning", "valueMarkdown": "Vill börja med levnadsvanor."}]}],
        **kw})


def _kept(sent: dict, got: dict):
    for k in ("supportingInfo", "finding", "problem", "extension", "summary", "description"):
        assert got.get(k) == sent[k], k
    assert PROFILE in ((got.get("meta") or {}).get("profile") or [])


def test_every_decision_element_survives_create_version_and_history(client):
    pid, obs, cond = P(), P(), P()
    enc = client.post("/api/v1/record/fhir/Encounter", json=_enc(pid),
                      headers=_rec()).get_json()["id"]
    cid = P()   # record mints the id first (the Condition names it)
    body = _decision(pid, enc, obs, cond, id=cid)
    r = client.post("/api/v1/record/fhir/ClinicalImpression", json=body, headers=_rec())
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["id"] == cid
    _kept(body, r.get_json())
    v2 = _decision(pid, enc, obs, cond, summary="Rättad: diabetes typ 2.")
    r = client.put(f"/api/v1/record/fhir/ClinicalImpression/{cid}", json=v2,
                   headers=_rec(**{"If-Match": 'W/"1"'}))
    assert r.status_code == 200, r.get_json()
    h = client.get(f"/api/v1/fhir/ClinicalImpression/{cid}/_history").get_json()
    by_v = {int(e["resource"]["meta"]["versionId"]): e["resource"] for e in h["entry"]}
    _kept(body, by_v[1])
    _kept(v2, by_v[2])
    # the encounter column is filled from the literal reference (D2)
    assert db.session.get(live_model("ClinicalImpression"), cid).encounter_guid == enc


def test_extract_names_a_decision_and_a_note_apart(client):
    from app.api.patient_extract import summary_sv as _summary_sv
    pid, enc = P(), P()
    note = _ci(pid, enc, summary="Mår bra.")
    dec = _decision(pid, enc, P(), P())
    assert _summary_sv("ClinicalImpression", note, None).startswith("Journalanteckning")
    assert _summary_sv("ClinicalImpression", dec, None).startswith("Medicinskt beslut")
