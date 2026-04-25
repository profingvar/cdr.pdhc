"""Phase 1 — write-path unit tests for the FHIR write API.

Covers execution plan §1.5 unit-test items:

  - bundle_dispatch        — Bundle entry routing to per-type writers
  - dedup_observation      — same value posted twice yields one row
  - canonicalisation       — foreign LOINC rewritten to termbank canonical
  - xlate_miss             — no xlate mapping → 422 + xlate_miss
  - plan_miss              — xlate ok, plan rejects → 422 + plan_miss
  - plan_miss_dedup        — same canonical rejected twice → seen_count = 2
  - provenance_stamped     — meta.source / .tag / .security present
  - integer_patient        — Rule 18: numeric patient id rejected
  - history_on_update      — update moves prior row to *_history
  - etag_if_match          — stale If-Match → 412
  - sync_group_minted      — every write creates a sync_group row
  - mapping_version_stamped — every write stamps the mapping_version

The xlate.pdhc / plan.pdhc HTTP clients are NOT exercised here; we stub
the Canonicaliser so tests stay hermetic. The clients have their own
unit tests in test_xlate_client.py and test_plan_client.py.
"""
from __future__ import annotations

import pytest

from app import db
from app.models.resources import (
    CdrAuditPlanMiss,
    ChangeFeed,
    SyncGroup,
    live_model,
    history_model,
)
from app.services.canonicalisation import (
    CanonicalisationResult,
    CodeMiss,
)


# ---------------------------------------------------------------------------
# Fake canonicaliser — injected via current_app._canonicaliser
# ---------------------------------------------------------------------------

class _FakeCanonicaliser:
    """Drop-in replacement that lets tests dictate the canonicalisation
    outcome per call."""

    def __init__(self):
        # default behaviour: pass-through with a fixed canonical for
        # Observation. Tests can swap this with .set_outcome().
        self._next_outcome: CanonicalisationResult | None = None

    def set_outcome(self, result: CanonicalisationResult):
        self._next_outcome = result

    def canonicalise(self, fhir: dict) -> CanonicalisationResult:
        if self._next_outcome is not None:
            r = self._next_outcome
            self._next_outcome = None  # one-shot
            return r
        # Pass-through: assume body is already canonical, claim ok.
        rt = fhir.get("resourceType")
        primary = None
        for path in ("code", "valueCodeableConcept", "medicationCodeableConcept"):
            cc = fhir.get(path)
            if cc and cc.get("coding"):
                c0 = cc["coding"][0]
                if c0.get("system", "").startswith("https://termbank.pdhc.se/"):
                    primary = f"{c0['system'].rstrip('/')}/{c0['code']}"
                    break
        return CanonicalisationResult(
            status="ok", rewritten=fhir, primary_canonical_uri=primary
        )


@pytest.fixture
def fake_canon(app):
    """Install a fake canonicaliser on the app; yield it for per-test setup."""
    fake = _FakeCanonicaliser()
    app._canonicaliser = fake
    yield fake
    if hasattr(app, "_canonicaliser"):
        delattr(app, "_canonicaliser")


@pytest.fixture(autouse=True)
def clean_tables(app):
    """Each test starts with empty per-type tables. Doesn't touch the legacy
    fhir_resources / ingest_raw / etc tables."""
    with app.app_context():
        for table in (
            "observation_history", "observation",
            "patient_history", "patient",
            "questionnaire_response_history", "questionnaire_response",
            "condition_history", "condition",
            "medication_statement_history", "medication_statement",
            "medication_request_history", "medication_request",
            "allergy_intolerance_history", "allergy_intolerance",
            "procedure_history", "procedure",
            "encounter_history", "encounter",
            "diagnostic_report_history", "diagnostic_report",
            "change_feed", "sync_group", "cdr_audit_plan_miss",
        ):
            db.session.execute(db.text(f"DELETE FROM {table}"))
        db.session.commit()
    yield


# ---------------------------------------------------------------------------
# Sample bodies
# ---------------------------------------------------------------------------

def _hba1c_body(value: float = 6.4, eff="2026-04-01T10:00:00Z",
                patient="pat-aaaa-bbbb-cccc-ddddeeeeffff") -> dict:
    return {
        "resourceType": "Observation",
        "status": "final",
        "subject": {"reference": f"Patient/{patient}"},
        "code": {
            "coding": [{
                "system": "https://termbank.pdhc.se/CodeSystem/loinc",
                "code": "4548-4",
                "display": "Hemoglobin A1c/Hemoglobin.total in Blood",
            }],
        },
        "effectiveDateTime": eff,
        "valueQuantity": {"value": value, "unit": "%", "code": "%"},
    }


def _foreign_hba1c_body() -> dict:
    """Inbound LOINC URI version — what would arrive before canonicalisation."""
    body = _hba1c_body()
    body["code"]["coding"] = [{
        "system": "http://loinc.org",
        "code": "4548-4",
        "display": "Hemoglobin A1c/Hemoglobin.total in Blood",
    }]
    return body


def _post_observation(client, body, *, request_id="req-001",
                      org="org-xyz", source="test-suite"):
    return client.post(
        "/api/v1/fhir/Observation",
        json=body,
        headers={
            "X-Org-Guid": org,
            "X-Source-Service": source,
            "X-Request-Id": request_id,
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_post_observation_creates_row(client, fake_canon):
    resp = _post_observation(client, _hba1c_body())
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["resourceType"] == "Observation"
    assert "id" in body
    assert resp.headers["ETag"].startswith('W/"1"')
    assert "_history/1" in resp.headers["Location"]

    Obs = live_model("Observation")
    rows = Obs.query.all()
    assert len(rows) == 1
    assert rows[0].version_id == 1
    assert rows[0].code_canonical == "https://termbank.pdhc.se/CodeSystem/loinc/4548-4"


def test_dedup_observation(client, fake_canon):
    """Same value posted twice → one live row, idempotent."""
    body = _hba1c_body()
    r1 = _post_observation(client, body, request_id="req-1")
    r2 = _post_observation(client, body, request_id="req-2")
    assert r1.status_code == 201
    assert r2.status_code in (200, 201)  # writer treats unchanged as update path

    Obs = live_model("Observation")
    rows = Obs.query.all()
    assert len(rows) == 1


def test_canonicalisation_rewrites_foreign_loinc(client, fake_canon):
    """Foreign LOINC URI → canonical, original preserved in coding[1..]."""
    rewritten = _hba1c_body()  # already canonical
    rewritten["code"]["coding"].append({
        "system": "http://loinc.org",
        "code": "4548-4",
        "display": "Hemoglobin A1c/Hemoglobin.total in Blood",
    })
    fake_canon.set_outcome(CanonicalisationResult(
        status="ok",
        rewritten=rewritten,
        primary_canonical_uri="https://termbank.pdhc.se/CodeSystem/loinc/4548-4",
    ))

    resp = _post_observation(client, _foreign_hba1c_body())
    assert resp.status_code == 201
    body = resp.get_json()
    codings = body["code"]["coding"]
    assert codings[0]["system"] == "https://termbank.pdhc.se/CodeSystem/loinc"
    assert any(c.get("system") == "http://loinc.org" for c in codings)


def test_xlate_miss_returns_422(client, fake_canon):
    fake_canon.set_outcome(CanonicalisationResult(
        status="xlate_miss",
        misses=[CodeMiss(
            kind="xlate_miss",
            location="Observation.code.coding[0]",
            foreign_system="urn:vendor-x",
            foreign_code="ZX9",
        )],
    ))
    resp = _post_observation(client, _foreign_hba1c_body())
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["diagnostics"] == "xlate_miss"
    assert "Observation.code.coding[0]" in body["issue"][0]["location"]


def test_plan_miss_returns_422_and_writes_audit_row(client, fake_canon, app):
    fake_canon.set_outcome(CanonicalisationResult(
        status="plan_miss",
        misses=[CodeMiss(
            kind="plan_miss",
            location="Observation.code.coding[0]",
            canonical_uri="https://termbank.pdhc.se/CodeSystem/loinc/9999-9",
            canonical_lib_name="loinc",
            canonical_refnumber="9999-9",
        )],
    ))
    resp = _post_observation(client, _hba1c_body())
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["issue"][0]["diagnostics"] == "plan_miss"

    with app.app_context():
        rows = CdrAuditPlanMiss.query.all()
        assert len(rows) == 1
        assert rows[0].canonical_uri == "https://termbank.pdhc.se/CodeSystem/loinc/9999-9"
        assert rows[0].seen_count == 1


def test_plan_miss_dedup_increments_seen_count(client, fake_canon, app):
    miss = CodeMiss(
        kind="plan_miss",
        location="Observation.code.coding[0]",
        canonical_uri="https://termbank.pdhc.se/CodeSystem/loinc/9999-9",
        canonical_lib_name="loinc",
        canonical_refnumber="9999-9",
    )
    for i in range(3):
        fake_canon.set_outcome(CanonicalisationResult(
            status="plan_miss", misses=[miss],
        ))
        resp = _post_observation(client, _hba1c_body(), request_id=f"req-{i}")
        assert resp.status_code == 422

    with app.app_context():
        rows = CdrAuditPlanMiss.query.all()
        assert len(rows) == 1
        assert rows[0].seen_count == 3
        assert rows[0].last_request_id == "req-2"


def test_provenance_stamped(client, fake_canon):
    resp = _post_observation(client, _hba1c_body(),
                              request_id="trace-42",
                              org="org-rumi",
                              source="gateway.pdhc")
    assert resp.status_code == 201
    body = resp.get_json()
    meta = body["meta"]
    assert "source" in meta and "gateway.pdhc" in meta["source"] and "trace-42" in meta["source"]
    tags = meta.get("tag") or []
    assert any(t.get("code") == "ingest_at" for t in tags)
    sec = meta.get("security") or []
    assert any(s.get("display") == "org-rumi" for s in sec)


def test_integer_patient_rejected(client, fake_canon):
    body = _hba1c_body(patient="42")  # numeric — Rule 18 violation
    resp = _post_observation(client, body)
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["resourceType"] == "OperationOutcome"
    assert "integer" in body["issue"][0]["details"]["text"].lower() \
        or "guid" in body["issue"][0]["details"]["text"].lower()


def test_history_on_update(client, fake_canon, app):
    body = _hba1c_body(value=6.4)
    r1 = _post_observation(client, body)
    assert r1.status_code == 201
    guid = r1.get_json()["id"]

    # PUT a new value (different valueQuantity → different dedup key would
    # mean a new row; we want an update of the same resource, so PUT by id).
    new_body = _hba1c_body(value=7.1)
    new_body["id"] = guid
    r2 = client.put(
        f"/api/v1/fhir/Observation/{guid}",
        json=new_body,
        headers={"X-Org-Guid": "org-xyz", "X-Source-Service": "test",
                 "If-Match": r1.headers["ETag"]},
    )
    assert r2.status_code in (200, 201)
    assert r2.headers["ETag"].startswith('W/"2"')

    with app.app_context():
        ObsHist = history_model("Observation")
        hist = ObsHist.query.filter_by(guid=guid).all()
        assert len(hist) == 1
        assert hist[0].version_id == 1


def test_etag_if_match_stale_returns_412(client, fake_canon):
    body = _hba1c_body()
    r1 = _post_observation(client, body)
    guid = r1.get_json()["id"]
    new_body = _hba1c_body(value=7.0)
    new_body["id"] = guid
    r2 = client.put(
        f"/api/v1/fhir/Observation/{guid}",
        json=new_body,
        headers={
            "X-Org-Guid": "org-xyz",
            "X-Source-Service": "test",
            "If-Match": 'W/"99"',  # stale
        },
    )
    assert r2.status_code == 412


def test_sync_group_minted(client, fake_canon, app):
    r1 = _post_observation(client, _hba1c_body())
    assert r1.status_code == 201
    with app.app_context():
        Obs = live_model("Observation")
        row = Obs.query.first()
        assert row.sync_group_id
        sg = SyncGroup.query.filter_by(sync_group_id=row.sync_group_id).first()
        assert sg is not None
        assert sg.origin_api == "fhir"


def test_mapping_version_stamped(client, fake_canon, app):
    app.config["MAPPING_VERSION"] = "v2026-04-24"
    try:
        r = _post_observation(client, _hba1c_body())
        assert r.status_code == 201
        with app.app_context():
            Obs = live_model("Observation")
            row = Obs.query.first()
            assert row.mapping_version == "v2026-04-24"
    finally:
        app.config.pop("MAPPING_VERSION", None)


def test_change_feed_event_emitted_on_create(client, fake_canon, app):
    r = _post_observation(client, _hba1c_body())
    assert r.status_code == 201
    with app.app_context():
        events = ChangeFeed.query.all()
        assert len(events) == 1
        assert events[0].event_type == "create"
        assert events[0].resource_type == "Observation"


def test_change_feed_event_emitted_on_update(client, fake_canon, app):
    r1 = _post_observation(client, _hba1c_body())
    guid = r1.get_json()["id"]
    new_body = _hba1c_body(value=7.0)
    new_body["id"] = guid
    client.put(
        f"/api/v1/fhir/Observation/{guid}",
        json=new_body,
        headers={"X-Org-Guid": "org-xyz", "X-Source-Service": "test",
                 "If-Match": r1.headers["ETag"]},
    )
    with app.app_context():
        events = ChangeFeed.query.order_by(ChangeFeed.seq).all()
        assert [e.event_type for e in events] == ["create", "update"]


def test_bundle_transaction_dispatches_to_per_type_writers(client, fake_canon, app):
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"resource": _hba1c_body(value=6.4)},
            {"resource": _hba1c_body(value=8.1, eff="2026-04-15T10:00:00Z")},
        ],
    }
    resp = client.post(
        "/api/v1/fhir/Bundle",
        json=bundle,
        headers={"X-Org-Guid": "org-xyz", "X-Source-Service": "test",
                 "X-Request-Id": "bundle-1"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["type"] == "transaction-response"
    assert len(body["entry"]) == 2
    with app.app_context():
        Obs = live_model("Observation")
        assert Obs.query.count() == 2


def test_bundle_transaction_rolls_back_on_entry_failure(client, fake_canon, app):
    """One bad entry → whole bundle rejected, nothing persists."""
    # First entry ok, second xlate_miss.
    miss_outcome = CanonicalisationResult(
        status="xlate_miss",
        misses=[CodeMiss(kind="xlate_miss",
                         location="Observation.code.coding[0]",
                         foreign_system="urn:bad", foreign_code="X")],
    )

    class _SeqCanon:
        def __init__(self): self._n = 0
        def canonicalise(self, fhir):
            self._n += 1
            if self._n == 1:
                return CanonicalisationResult(
                    status="ok", rewritten=fhir,
                    primary_canonical_uri="https://termbank.pdhc.se/CodeSystem/loinc/4548-4",
                )
            return miss_outcome
    app._canonicaliser = _SeqCanon()

    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"resource": _hba1c_body(value=6.4)},
            {"resource": _hba1c_body(value=7.1, eff="2026-04-02T10:00:00Z")},
        ],
    }
    resp = client.post(
        "/api/v1/fhir/Bundle", json=bundle,
        headers={"X-Org-Guid": "org-xyz", "X-Source-Service": "test"},
    )
    assert resp.status_code == 422
    with app.app_context():
        Obs = live_model("Observation")
        assert Obs.query.count() == 0
