"""#816 — the flat payload shape, and the visit.

Two faults, one symptom. A cohort transferred out of cdr_6 into cdr1/2/3
arrived with its contract, plandef, visit and transaction **all missing**,
while the transfer reported `verified: true` (it verifies the row COUNT).
Measured 2026-10-11: 30 of 30 rows, every context field empty.

1. `IngestPipeline.process` read the context from `body["clinical_context"]`
   and nowhere else. cdr_6's own ingest reads those keys at the TOP LEVEL, so
   for every flat producer `context` was None and the `ClinicalContext` insert
   — guarded by `if context:` — was skipped entirely. Silently.

2. `plan_activity_guid` did not exist on `clinical_context` in any CDR, cdr1
   included, so the visit had nowhere to go even once the context arrived.
"""
from __future__ import annotations

import pytest

from app.services.ingest_pipeline import (
    _clinical_context_from, _CONTEXT_KEYS,
)

FLAT = {
    "patient_guid": "pat-1",
    "concept_guid": "con-1",
    "value": "98.9",
    "contract_guid": "contract-1",
    "plan_definition_guid": "plandef-1",
    "plan_activity_guid": "visit-1",
    "transaction_guid": "txn-1",
    "provider_org_guid": "org-provider",
    "patient_org_guid": "org-patient",
}


def test_a_FLAT_payload_yields_a_context():
    """The fix. This returned None, so nothing was stored."""
    ctx = _clinical_context_from(FLAT)
    assert ctx is not None, "a flat payload still yields no context"
    assert ctx["contract_guid"] == "contract-1"
    assert ctx["plan_definition_guid"] == "plandef-1"
    assert ctx["plan_activity_guid"] == "visit-1"
    assert ctx["transaction_guid"] == "txn-1"
    assert ctx["patient_org_guid"] == "org-patient"


def test_a_NESTED_payload_still_works_unchanged():
    body = {"patient_guid": "p", "clinical_context": {
        "contract_guid": "c-nested", "plan_activity_guid": "v-nested"}}
    ctx = _clinical_context_from(body)
    assert ctx["contract_guid"] == "c-nested"
    assert ctx["plan_activity_guid"] == "v-nested"


def test_NESTED_WINS_over_top_level():
    """An explicit block is a deliberate statement; top level is the
    fallback. Letting the fallback override it would make a caller's explicit
    context unpredictable."""
    body = dict(FLAT)
    body["clinical_context"] = {"contract_guid": "c-nested"}
    ctx = _clinical_context_from(body)
    assert ctx["contract_guid"] == "c-nested"


def test_a_payload_with_NO_context_still_yields_None():
    """The `if context:` guard must keep meaning "nothing was said about this
    row" — otherwise every bare observation gains an empty context row."""
    assert _clinical_context_from(
        {"patient_guid": "p", "value": "1"}) is None


def test_an_EMPTY_nested_block_is_preserved_not_second_guessed():
    body = {"patient_guid": "p", "clinical_context": {}}
    assert _clinical_context_from(body) == {}


def test_deprecated_wire_aliases_are_still_accepted():
    """#294's aliases are still dual-emitted by some producers; dropping them
    here would lose context from exactly the callers least likely to notice."""
    ctx = _clinical_context_from({
        "patient_guid": "p",
        "plandef_guid": "pd-alias",
        "careplan_guid": "cp-alias",
        "activity_guid": "txn-alias",
    })
    assert ctx["plan_definition_guid"] == "pd-alias"
    assert ctx["care_plan_guid"] == "cp-alias"
    # `activity_guid` is the TRANSACTION alias, never the visit.
    assert ctx["transaction_guid"] == "txn-alias"
    assert "plan_activity_guid" not in ctx


def test_the_canonical_key_list_covers_the_12_field_context():
    """A field absent from `_CONTEXT_KEYS` is silently dropped from every flat
    payload, which is the whole defect this module exists for."""
    for field in ("service_request_guid", "transaction_guid", "concept_guid",
                  "plan_definition_guid", "plan_activity_guid",
                  "care_plan_guid", "contract_guid", "requesting_org_guid",
                  "provider_org_guid", "requester_user_guid",
                  "patient_org_guid"):
        assert field in _CONTEXT_KEYS, f"{field} would be dropped"


def test_the_model_has_the_visit_column():
    from app.models import ClinicalContext
    assert hasattr(ClinicalContext, "plan_activity_guid")
    # And it is NOT the deprecated transaction alias.
    assert hasattr(ClinicalContext, "transaction_guid")


def test_THE_PIPELINE_ACTUALLY_CALLS_THE_EXTRACTOR():
    """Every test above imports `_clinical_context_from` directly, so they all
    still pass if `process` goes back to `body.get("clinical_context")` — which
    is the entire bug. Verified: reverting the call site left them green.

    So this asserts the call SITE. It is a source check rather than a run of
    `process`, because `process` needs a database session and an IngestRaw row,
    and the thing at risk here is one line of wiring.
    """
    import inspect
    from app.services import ingest_pipeline as ip

    src = inspect.getsource(ip.IngestPipeline.process)
    assert "_clinical_context_from(body)" in src, (
        "process no longer calls the extractor, so every flat payload loses "
        "its context again")
    assert 'body.get("clinical_context")' not in src, (
        "process reads the nested block directly, bypassing the flat fallback")


def test_the_insert_stores_the_visit():
    """The column is useless if the insert never sets it."""
    import inspect
    from app.services import ingest_pipeline as ip
    src = inspect.getsource(ip.IngestPipeline.process)
    assert 'plan_activity_guid=context.get("plan_activity_guid")' in src, (
        "the ClinicalContext insert does not store the visit")


# ---------------------------------------------------------------------------
# #817 — which RUN, not just which writer
# ---------------------------------------------------------------------------

def test_sim_run_id_is_extracted_from_a_flat_payload():
    """`source_service` says sim.pdhc wrote the row; `sim_run_id` says which
    generation produced it. A promoted cohort kept the writer and lost the
    run, so "these 300 rows came from that run" was unanswerable."""
    ctx = _clinical_context_from(dict(FLAT, sim_run_id="sim-20261012T0558Z-5"))
    assert ctx["sim_run_id"] == "sim-20261012T0558Z-5"


def test_sim_run_id_is_in_the_canonical_key_list():
    """Absent from `_CONTEXT_KEYS` it is silently dropped from every flat
    payload — the exact defect #816 existed to fix."""
    assert "sim_run_id" in _CONTEXT_KEYS


def test_the_insert_stores_the_run_id():
    import inspect
    from app.services import ingest_pipeline as ip
    src = inspect.getsource(ip.IngestPipeline.process)
    assert 'sim_run_id=context.get("sim_run_id")' in src, (
        "the ClinicalContext insert does not store the run id")


def test_the_model_holds_a_run_id_that_is_NOT_a_guid():
    """A sim run id is `sim-<ISO>-<8 hex>` (29 chars today) and synthea's is
    longer. String(36) would fit but leave no headroom; this must not be
    sized as though it were a GUID."""
    from app.models import ClinicalContext
    col = ClinicalContext.__table__.c["sim_run_id"]
    assert col.type.length >= 64, (
        f"sim_run_id is String({col.type.length}); run ids are not GUIDs")
    assert col.nullable, "rows that predate this cannot be attributed to a run"


def test_a_payload_with_only_a_run_id_still_yields_a_context():
    """sim stamps the run id on every row even when nothing else is set, so a
    bare run id must not be discarded."""
    ctx = _clinical_context_from({"patient_guid": "p", "value": "1",
                                  "sim_run_id": "sim-x"})
    assert ctx is not None and ctx["sim_run_id"] == "sim-x"
