"""#664 — a registered analysis service may DECLARE a read purpose.

Before this, every service-key caller passed the #422 consent gate untouched:
_operator_blob() returns None for a machine identity, on the reasoning that
"a machine identity has no role to derive a purpose from". True for
dashboard.pdhc and sim, which read under a sibling's operator context. Not
true for an analyse.pdhc node, whose purpose is an explicit, validated,
logged parameter of the analysis spec.

The safety property these tests pin down: declaring a purpose can only ever
REDUCE what a caller sees, because the alternative is the pass-through. A
caller that declares nothing behaves exactly as before.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from werkzeug.exceptions import BadRequest, ServiceUnavailable

from app.services import analysis_consent as ac


SERVICE_BLOB = {"service_source": "analyse.pdhc", "is_su_admin": True}
OPERATOR_BLOB = {
    "affiliations": [{"affiliation_guid": "a1", "role": "researcher",
                      "research_project_guids": ["proj-1"]}],
    "active_affiliation_guid": "a1",
}
PATIENTS = {"pat-A", "pat-B"}


def _ctx(app, blob, headers=None):
    """A request context with `g.access_blob` set, as auth would leave it."""
    from flask import g
    ctx = app.test_request_context("/api/v1/fhir/Observation",
                                   headers=headers or {})
    ctx.push()
    g.access_blob = blob
    return ctx


# ── the machine path ──────────────────────────────────────────────────

class TestDeclaredPurpose:

    def test_service_declaring_a_purpose_is_filtered(self, app):
        ctx = _ctx(app, SERVICE_BLOB, {"X-Access-Purpose": "statistics"})
        try:
            with patch.object(ac, "_analysis_filter",
                              return_value={"allowed": ["pat-A"], "excluded": []}) as f:
                out = ac.consent_allowed_guids(set(PATIENTS))
            assert out == {"pat-A"}                 # pat-B excluded by ips
            assert f.call_args[0][1] == "statistics"
        finally:
            ctx.pop()

    def test_service_declaring_nothing_still_passes_through(self, app):
        """dashboard.pdhc and sim must be completely unaffected."""
        ctx = _ctx(app, SERVICE_BLOB)
        try:
            with patch.object(ac, "_analysis_filter") as f:
                out = ac.consent_allowed_guids(set(PATIENTS))
            assert out == PATIENTS
            f.assert_not_called()                   # ips never consulted
        finally:
            ctx.pop()

    def test_research_carries_its_project_guids(self, app):
        ctx = _ctx(app, SERVICE_BLOB, {
            "X-Access-Purpose": "research",
            "X-Research-Project-Guids": "proj-1, proj-2",
        })
        try:
            with patch.object(ac, "_analysis_filter",
                              return_value={"allowed": ["pat-A"], "excluded": []}) as f:
                ac.consent_allowed_guids(set(PATIENTS))
            assert f.call_args[0][1] == "research"
            assert f.call_args[0][2] == ["proj-1", "proj-2"]
        finally:
            ctx.pop()

    def test_single_patient_gate_honours_a_declared_purpose(self, app):
        ctx = _ctx(app, SERVICE_BLOB, {"X-Access-Purpose": "quality_registry"})
        try:
            with patch.object(ac, "_analysis_filter",
                              return_value={"allowed": [], "excluded": [
                                  {"patient_guid": "pat-A", "reason": "qreg_opt_out"}]}):
                with pytest.raises(Exception) as e:
                    ac.check_patient_allowed("pat-A")
            assert "403" in str(e.value) or "Forbidden" in str(type(e.value).__name__)
        finally:
            ctx.pop()


# ── the guards ────────────────────────────────────────────────────────

class TestDeclarationIsNotAnEscapeHatch:

    @pytest.mark.parametrize("bad", ["administration", "care",
                                     "care_coordination", "patient_access"])
    def test_primary_use_purposes_cannot_be_declared(self, app, bad):
        """The whole risk of this feature. `administration` is never blocked
        by ips, so allowing a service to declare it would turn a gate into a
        bypass."""
        ctx = _ctx(app, SERVICE_BLOB, {"X-Access-Purpose": bad})
        try:
            with pytest.raises(BadRequest):
                ac.declared_service_purpose()
        finally:
            ctx.pop()

    def test_unknown_purpose_is_rejected(self, app):
        ctx = _ctx(app, SERVICE_BLOB, {"X-Access-Purpose": "quality_followup"})
        try:
            with pytest.raises(BadRequest):
                ac.declared_service_purpose()
        finally:
            ctx.pop()

    def test_research_without_projects_is_rejected(self, app):
        ctx = _ctx(app, SERVICE_BLOB, {"X-Access-Purpose": "research"})
        try:
            with pytest.raises(BadRequest):
                ac.declared_service_purpose()
        finally:
            ctx.pop()

    def test_a_human_operator_cannot_declare_a_purpose(self, app):
        """The header is a machine affordance. An operator's purpose comes
        from their role, so a header must not let them pick a softer one."""
        ctx = _ctx(app, OPERATOR_BLOB, {"X-Access-Purpose": "statistics"})
        try:
            assert ac.declared_service_purpose() is None
            resolved = ac._resolve_purpose()
            assert resolved[0] == "research"        # from the role, not the header
        finally:
            ctx.pop()

    def test_declared_purpose_still_fails_closed(self, app):
        """ips down must 503 for a machine exactly as for an operator —
        otherwise declaring a purpose would be less safe than not."""
        ctx = _ctx(app, SERVICE_BLOB, {"X-Access-Purpose": "statistics"})
        try:
            with patch.object(ac, "_analysis_filter",
                              side_effect=ac.IpsUnreachable("down")):
                with pytest.raises(ServiceUnavailable):
                    ac.consent_allowed_guids(set(PATIENTS))
        finally:
            ctx.pop()
