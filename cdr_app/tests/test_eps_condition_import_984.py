"""#984 — the EPS's diagnoses are brought into the CDR, which is the record."""
from __future__ import annotations

import pytest

from app.models.resources import live_model
from app.services import eps_condition_import as imp
from app.services.canonicalisation import CanonicalisationResult, CodeMiss
from tests.test_fhir_write import _hba1c_body, clean_tables, fake_canon  # noqa: F401

P = "462c1906-d09f-415b-80af-e1b094b54451"
ORG = "11111111-2222-4333-8444-555555555555"


def _eps(rid="24814fdd-b769-4fb5-b24f-0dc90e678f08", code="49436004", display="Atrial fibrillation"):
    return {"resourceType": "Condition", "id": rid,
            "code": {"text": display, "coding": [{"system": imp.SNOMED, "code": code, "display": display}]},
            "subject": {"reference": f"Patient/{P}"},
            "recordedDate": "2026-10-23T06:15:53+00:00",
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
            "text": {"status": "generated", "div": "<div>x</div>"}}


def _absent():
    r = _eps(rid="a0000000-0000-4000-8000-000000000001")
    r["code"]["coding"] = [{"system": imp.ABSENT_UNKNOWN, "code": "no-known-problems"}]
    return r


def _seed_patient(client, org=ORG, patient=P, value=6.4, eff="2026-04-01T10:00:00Z"):
    hdr = {"X-Source-Service": "sim.pdhc", "X-Patient-Org-Guid": org}
    from tests.test_fhir_write import _SERVICE_KEYS
    hdr["X-Service-Key"] = _SERVICE_KEYS["sim.pdhc"]
    r = client.post("/api/v1/fhir/Observation", json=_hba1c_body(value=value, eff=eff, patient=patient), headers=hdr)
    assert r.status_code == 201, r.get_json()


# ---------------------------------------------------------------- the mapping

def test_snomed_is_presented_canonically_and_the_original_coding_is_kept():
    c = imp.to_cdr_condition(_eps(), P)
    assert c["code"]["coding"][0] == {"system": "https://termbank.pdhc.se/CodeSystem/snomed",
                                      "code": "49436004", "display": "Atrial fibrillation"}
    assert c["code"]["coding"][1]["system"] == imp.SNOMED
    assert c["subject"] == {"reference": f"Patient/{P}"}
    assert c["identifier"] == [{"system": imp.IPS_IDENTIFIER_SYSTEM, "value": _eps()["id"]}]
    assert imp.MIGRATED_TAG in c["meta"]["tag"]
    assert c["clinicalStatus"]["coding"][0]["code"] == "active"
    assert "text" not in c, "the EPS narrative is ips's rendering, not the record"


def test_the_id_is_stable_so_a_rerun_upserts_the_same_row():
    assert imp.to_cdr_condition(_eps(), P)["id"] == imp.to_cdr_condition(_eps(), P)["id"]
    assert imp.cdr_guid_for("a") != imp.cdr_guid_for("b")


@pytest.mark.parametrize("res, reason", [
    (_absent(), "absent/unknown"),
    ({**_eps(), "code": {"coding": []}}, "no coding"),
    ({**_eps(), "code": {"coding": [{"system": "http://loinc.org", "code": "x"}]}}, "no SNOMED"),
    (_eps(code="12 34"), "grammar"),
])
def test_what_is_not_a_snomed_diagnosis_is_skipped_by_name(res, reason):
    with pytest.raises(imp.Skip, match=reason):
        imp.to_cdr_condition(res, P)


# ---------------------------------------------------------------- the run

def test_dry_run_writes_nothing(app, client, fake_canon):
    _seed_patient(client)
    with app.app_context():
        rep = imp.run([{"patient_guid": P, "resource": _eps()}], apply=False, canonicaliser=fake_canon)
        assert rep.written == {"would write": 1}
        assert live_model("Condition").query.count() == 0


def test_apply_writes_one_row_per_eps_condition_with_the_patients_org(app, client, fake_canon):
    _seed_patient(client)
    twice = [_eps(), _eps(rid="b0000000-0000-4000-8000-000000000002")]   # same diagnosis twice
    entries = [{"patient_guid": P, "resource": r} for r in twice + [_absent()]]
    with app.app_context():
        rep = imp.run(entries, apply=True)
        assert rep.written == {"created": 2}
        assert rep.skipped == {"absent/unknown assertion, not a diagnosis": 1}
        rows = live_model("Condition").query.all()
        assert {r.guid for r in rows} == {imp.cdr_guid_for(r["id"]) for r in twice}
        assert {r.patient_org_guid for r in rows} == {ORG}
        assert {r.source for r in rows} == {"ips.pdhc"}
        assert {r.code_canonical for r in rows} == {"https://termbank.pdhc.se/CodeSystem/snomed/49436004"}
        # rerun: same rows, updated, nothing new
        rep2 = imp.run(entries, apply=True)
        assert rep2.written == {"updated": 2}
        assert live_model("Condition").query.count() == 2


def test_a_patient_this_cdr_does_not_hold_is_skipped_not_created(app, fake_canon):
    with app.app_context():
        rep = imp.run([{"patient_guid": P, "resource": _eps()}], apply=True)
        assert rep.not_here == {P}
        assert live_model("Condition").query.count() == 0


def test_a_patient_whose_rows_name_two_orgs_is_refused(app, client, fake_canon):
    _seed_patient(client)
    _seed_patient(client, org="99999999-2222-4333-8444-555555555555", value=7.1, eff="2026-05-01T10:00:00Z")
    with app.app_context():
        rep = imp.run([{"patient_guid": P, "resource": _eps()}], apply=True)
        assert rep.skipped == {"patient's rows name 2 organisations": 1}
        assert live_model("Condition").query.count() == 0


def test_a_refusal_is_reported_and_nothing_half_written(app, client, fake_canon):
    _seed_patient(client)
    fake_canon.set_outcome(CanonicalisationResult(status="plan_miss", misses=[
        CodeMiss(kind="plan_miss", location="Condition.code", canonical_uri="x")]))
    with app.app_context():
        rep = imp.run([{"patient_guid": P, "resource": _eps()}], apply=True)
        assert [s for _, s, _ in rep.refused] == [422]
        assert live_model("Condition").query.count() == 0


# ---------------------------------------------------------------- contradictions

@pytest.mark.parametrize("code, eps_label, termbank, refused", [
    ("44054006", "Atrial fibrillation", "Type 2 diabetes mellitus", True),
    ("80146002", "Appendicitis", "Appendectomy", True),
    ("49436004", "Atrial fibrillation", "Atrial fibrillation", False),
    ("49436004", "atrial fibrillation ", "Atrial fibrillation", False),
    ("35489007", "Depression", "Depressive disorder", False),
    ("73211009", "Diabetes mellitus type 2", "Diabetes mellitus", False),
    ("49436004", "Atrial fibrillation", None, False),
])
def test_a_label_that_contradicts_its_code_is_not_imported(code, eps_label, termbank, refused):
    res = _eps(code=code, display=eps_label)
    if refused:
        with pytest.raises(imp.Skip, match="contradicts"):
            imp.to_cdr_condition(res, P, termbank)
    else:
        assert imp.to_cdr_condition(res, P, termbank)["code"]["coding"][0]["code"] == code


def test_run_passes_termbank_display_through(app, client, fake_canon):
    _seed_patient(client)
    with app.app_context():
        rep = imp.run([{"patient_guid": P, "resource": _eps(code="44054006"),
                        "termbank_display": "Type 2 diabetes mellitus"}], apply=True)
        assert rep.skipped == {"EPS label contradicts the code": 1}
        assert live_model("Condition").query.count() == 0
