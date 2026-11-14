"""M0 #416 — cdr1 adopts affiliations[] Zone-1 scope + session_phases.

The Rule-24 org filter (auth._blob_to_user AND fhir_read._org_filter) swaps
from the flat organization_ids to affiliations[].care_unit_guid. The dual-read
fallback to organization_ids was removed 2026-11-14.
"""
from app.auth import scope_org_guids, _phases, _blob_to_user, has_analysis_access


def test_scope_from_affiliations():
    blob = {"affiliations": [{"care_unit_guid": "u1"}, {"care_unit_guid": "u2"}]}
    assert scope_org_guids(blob) == ["u1", "u2"]


def test_scope_precedence_over_legacy():
    blob = {"affiliations": [{"care_unit_guid": "u1"}],
            "organization_ids": ["other"]}
    assert scope_org_guids(blob) == ["u1"]


def test_legacy_organization_ids_ALONE_grant_no_scope():
    """2026-11-14: this test used to PIN the fallback. Memberships are not
    care-unit assignments; no affiliation, no scope."""
    assert scope_org_guids({"organization_ids": ["o1"]}) == []
    assert scope_org_guids({"affiliations": [], "organization_ids": ["o1"]}) == []


def test_blob_to_user_with_only_legacy_ids_has_NO_org_scope():
    """Through the real mapping the request loader uses: the user object
    that Rule 24 filters on carries no unit for a membership-only blob."""
    u = _blob_to_user({"user_guid": "x", "organization_ids": ["legacy"]})
    assert u.org_ids == []


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
