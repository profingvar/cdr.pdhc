"""A permanent condition must not be reported as a transient one (#833).

xlate.pdhc is declared in the platform port map and is **not deployed
anywhere** — no release directory, nothing listening on 9016 or 9017.
Until this fix, a CDR asked to ingest a foreign coding raised a single
``XlateUnreachable``, which the canonicaliser turned into
``status="transient"`` and the writer into a 503. 503 + "transient" tells
a producer to retry, so any emitter sending a foreign coding would have
retried forever against a service that does not exist.

The failing direction is also worth recording: cdr1 left
``XLATE_BASE_URL`` unset and cdr2/cdr3 set it to
``host.docker.internal:9017``. Both were unreachable and both produced
the same 503, so this was never a URL-form problem — it was one failure
state standing for two different prognoses.

These tests go through the real ``XlateClient`` and the real HTTP entry
point rather than the stubbed canonicaliser, because the bug lived in
the seam between them: the client raised, the canonicaliser relabelled.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app import db
from app.models.resources import live_model
from app.services.canonicalisation import (
    Canonicaliser,
    operation_outcome_xlate_not_configured,
    CodeMiss,
)
from app.services.xlate_client import XlateClient


FOREIGN_CODING = {
    "system": "http://loinc.org",
    "code": "4548-4",
    "display": "Hemoglobin A1c/Hemoglobin.total in Blood",
}

WRITE_HEADERS = {
    "X-Org-Guid": "org-xyz",
    "X-Source-Service": "sim.pdhc",
    "X-Service-Key": "test-sim-key",
    "X-Request-Id": "req-833",
}


def _foreign_observation() -> dict:
    return {
        "resourceType": "Observation",
        "status": "final",
        "subject": {"reference": "Patient/pat-aaaa-bbbb-cccc-ddddeeeeffff"},
        "code": {"coding": [dict(FOREIGN_CODING)]},
        "effectiveDateTime": "2026-04-01T10:00:00Z",
        "valueQuantity": {"value": 6.4, "unit": "%", "code": "%"},
    }


def _plan_that_must_not_be_asked():
    """A foreign coding reaches xlate before plan is consulted.

    If plan gets called, the tier ordering changed and this test is no
    longer exercising what it claims to.
    """
    plan = MagicMock()
    plan.resolve_concept.side_effect = AssertionError(
        "plan.resolve_concept called for a foreign coding — tier ordering "
        "changed; this test no longer covers the xlate tier."
    )
    plan.validate_canonical.side_effect = AssertionError(
        "plan.validate_canonical called before the xlate hop."
    )
    return plan


# ---------------------------------------------------------------------------
# canonicaliser level
# ---------------------------------------------------------------------------

def test_unconfigured_xlate_is_terminal_not_transient(monkeypatch):
    """The core of #833: status must be xlate_not_configured, NOT transient."""
    monkeypatch.delenv("XLATE_BASE_URL", raising=False)
    canon = Canonicaliser(xlate=XlateClient(), plan=_plan_that_must_not_be_asked())

    result = canon.canonicalise(_foreign_observation())

    assert result.status == "xlate_not_configured", (
        "a CDR with no xlate service reported %r — a producer reading this "
        "as transient would retry forever" % result.status
    )
    assert result.status != "transient"
    assert result.transient_reason is None
    assert len(result.misses) == 1
    miss = result.misses[0]
    assert miss.kind == "xlate_not_configured"
    assert miss.location == "Observation.code"
    # The coding survives into the miss so the 422 can name what to fix.
    assert miss.foreign_system == "http://loinc.org"
    assert miss.foreign_code == "4548-4"


def test_configured_but_unreachable_is_still_transient(monkeypatch):
    """The other half of the split must keep its retryable 503.

    Without this, the fix would have swapped one over-broad answer for
    another: a genuine blip at a deployed xlate deserves a retry.
    """
    import app.services.xlate_client as xc
    import requests

    monkeypatch.setenv("XLATE_BASE_URL", "https://xlate.pdhc.se")
    monkeypatch.setattr(
        xc.requests, "post",
        lambda *a, **kw: (_ for _ in ()).throw(
            requests.ConnectionError("connection refused")),
    )
    canon = Canonicaliser(xlate=XlateClient(), plan=_plan_that_must_not_be_asked())

    result = canon.canonicalise(_foreign_observation())

    assert result.status == "transient"
    assert "unreachable" in (result.transient_reason or "")


def test_plan_concept_coding_never_reaches_xlate(monkeypatch):
    """Regression guard on the live data path.

    Every row in cdr1/cdr2/cdr3 arrived from sim.pdhc as a
    plan.pdhc/Concept coding, which resolves at tier 1 and returns
    before the xlate hop. That is why the dead dependency never surfaced
    in production, and it must keep working after the split.
    """
    from app.services.canonicalisation import PLAN_CONCEPT_SYSTEM

    monkeypatch.delenv("XLATE_BASE_URL", raising=False)
    plan = MagicMock()
    plan.resolve_concept.return_value = {
        "canonical_uri": "https://termbank.pdhc.se/CodeSystem/loinc/4548-4",
        "system": "https://termbank.pdhc.se/CodeSystem/loinc",
        "canonical_refnumber": "4548-4",
        "canonical_lib_name": "loinc",
        "display": "HbA1c",
        "guid": "6521528c-db59-45c5-a492-003c28f27623",
    }
    canon = Canonicaliser(xlate=XlateClient(), plan=plan)

    obs = _foreign_observation()
    obs["code"]["coding"] = [{
        "system": PLAN_CONCEPT_SYSTEM,
        "code": "6521528c-db59-45c5-a492-003c28f27623",
        "display": "HbA1c",
    }]

    result = canon.canonicalise(obs)
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# OperationOutcome body
# ---------------------------------------------------------------------------

def test_outcome_body_says_do_not_retry_and_what_to_send():
    body = operation_outcome_xlate_not_configured([CodeMiss(
        kind="xlate_not_configured",
        location="Observation.code",
        foreign_system="http://loinc.org",
        foreign_code="4548-4",
    )])
    assert body["resourceType"] == "OperationOutcome"
    issue = body["issue"][0]
    assert issue["diagnostics"] == "xlate_not_configured"
    assert issue["code"] == "not-supported"
    assert issue["location"] == ["Observation.code"]
    text = issue["details"]["text"]
    assert "Do not retry" in text
    assert "http://loinc.org" in text
    # Names the two systems that need no translation.
    assert "plan.pdhc.se/Concept" in text
    assert "termbank.pdhc.se" in text


def test_outcome_ignores_misses_of_other_kinds():
    """Mirrors the xlate_miss / plan_miss builders: filter by kind, and
    still produce a usable body when nothing matches."""
    body = operation_outcome_xlate_not_configured([
        CodeMiss(kind="plan_miss", location="Observation.code"),
    ])
    assert body["issue"][0]["details"]["text"] == "xlate_not_configured"


# ---------------------------------------------------------------------------
# end to end through the real HTTP entry point
# ---------------------------------------------------------------------------

def test_post_foreign_coding_returns_422_and_writes_nothing(
    client, app, monkeypatch
):
    """The whole chain, with no canonicaliser stub.

    Asserts the status code a producer's retry logic actually keys on.
    A 5xx here is the live defect; a 422 stops the loop.
    """
    monkeypatch.delenv("XLATE_BASE_URL", raising=False)
    # Force a fresh real canonicaliser (other modules' fixtures may have
    # installed a fake one on the app object).
    if hasattr(app, "_canonicaliser"):
        delattr(app, "_canonicaliser")

    resp = client.post(
        "/api/v1/fhir/Observation",
        json=_foreign_observation(),
        headers=WRITE_HEADERS,
    )

    assert resp.status_code == 422, (
        f"expected a terminal 422, got {resp.status_code} — "
        "a retryable status here is the #833 defect"
    )
    assert not str(resp.status_code).startswith("5")
    body = resp.get_json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["diagnostics"] == "xlate_not_configured"
    assert body["issue"][0]["code"] == "not-supported"

    # A refused write leaves no row behind.
    with app.app_context():
        assert live_model("Observation").query.count() == 0

    if hasattr(app, "_canonicaliser"):
        delattr(app, "_canonicaliser")
