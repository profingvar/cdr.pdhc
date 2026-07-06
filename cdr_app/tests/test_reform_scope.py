"""M0 #416 — cdr1 adopts affiliations[] Zone-1 scope + session_phases.

The Rule-24 org filter (auth._blob_to_user AND fhir_read._org_filter) swaps
from the flat organization_ids to affiliations[].care_unit_guid, dual-read.
"""
from app.auth import scope_org_guids, _phases, _blob_to_user, has_analysis_access


def test_scope_from_affiliations():
    blob = {"affiliations": [{"care_unit_guid": "u1"}, {"care_unit_guid": "u2"}]}
    assert scope_org_guids(blob) == ["u1", "u2"]


def test_scope_precedence_over_legacy():
    blob = {"affiliations": [{"care_unit_guid": "u1"}],
            "organization_ids": ["other"]}
    assert scope_org_guids(blob) == ["u1"]


def test_scope_legacy_fallback():
    assert scope_org_guids({"organization_ids": ["o1"]}) == ["o1"]


def test_scope_empty():
    assert scope_org_guids({}) == []


def test_blob_to_user_uses_affiliation_scope():
    u = _blob_to_user({"user_guid": "x", "affiliations": [
        {"care_unit_guid": "u1"}], "organization_ids": ["legacy"]})
    assert u.org_ids == ["u1"]


def test_analysis_gate_prefers_session_phases():
    assert has_analysis_access(
        {"user_type": "professional", "session_phases": ["analysis"],
         "effective_phases": []}) is True


def test_analysis_gate_legacy_fallback():
    assert has_analysis_access(
        {"user_type": "professional", "effective_phases": ["analysis"]}) is True


def test_analysis_gate_denies():
    assert has_analysis_access(
        {"user_type": "professional", "session_phases": ["request"]}) is False
