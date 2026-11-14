"""#1000 — synthetic data never leaves for Cambio.

Through the real ingest route and the real worker. Each assertion was shown to
fail with its rule removed.
"""
import copy
import uuid
from unittest.mock import patch

import pytest

from app import db
from app.models import CambioDeliveryLog, IngestRaw
from app.services import cambio_worker
from app.services.synthetic import synthetic_reason
from tests.conftest import SAMPLE_INGEST_BODY

@pytest.fixture(autouse=True)
def _remove_my_rows(app):
    """Ingest COMMITS, so conftest's rollback does not undo it, and the next
    module reads `IngestRaw.query.first()`. Delete exactly this file's rows
    (patients pat-1000-*), children before parents."""
    yield
    from app.models import (ClinicalContext, DedupeRegistry, FhirResource,
                            HealthObservation, OpenEhrComposition)
    with app.app_context():
        db.session.rollback()
        raws = [r.guid for r in IngestRaw.query.filter(IngestRaw.patient_guid.like("pat-1000-%"))]
        if raws:
            for model in (CambioDeliveryLog, OpenEhrComposition, FhirResource,
                          HealthObservation, ClinicalContext):
                model.query.filter(model.ingest_raw_guid.in_(raws)).delete(synchronize_session=False)
            DedupeRegistry.query.filter(DedupeRegistry.patient_guid.like("pat-1000-%")).delete(
                synchronize_session=False)
            from app.models.resources import live_model
            LiveObservation = live_model("Observation")
            LiveObservation.query.filter(LiveObservation.ingest_raw_guid.in_(raws)).delete(
                synchronize_session=False)
            IngestRaw.query.filter(IngestRaw.guid.in_(raws)).delete(synchronize_session=False)
            db.session.commit()


GATEWAY = {"X-Service-Key": "test-gateway-key", "X-Source-Service": "gateway.pdhc"}
SIM = {"X-Service-Key": "test-sim-key", "X-Source-Service": "sim.pdhc"}


def _body(**context):
    b = copy.deepcopy(SAMPLE_INGEST_BODY)
    b["patient_guid"] = f"pat-1000-{uuid.uuid4().hex[:8]}"   # no dedupe across tests
    if context:
        b["clinical_context"] = dict(context)
    return b


def _ingest(client, body, headers):
    r = client.post("/api/v1/ingest", json=body, headers=headers)
    assert r.status_code == 202, r.get_json()
    return r.get_json()["ingest_raw_guid"]


def _logs(app, raw_guid):
    with app.app_context():
        return sorted((l.delivery_type, l.status, l.last_error)
                      for l in CambioDeliveryLog.query.filter_by(ingest_raw_guid=raw_guid))


# ---------------------------------------------------------------- the rule

@pytest.mark.parametrize("kw,synthetic", [
    ({"source_service": "sim.pdhc"}, True),
    ({"context_source": "sim.pdhc"}, True),
    ({"sim_run_id": "sim-20261114-1"}, True),
    ({"source_service": "gateway.pdhc"}, False),
    ({}, False),
    ({"source_service": "gateway.pdhc", "sim_run_id": ""}, False),
])
def test_synthetic_reason(kw, synthetic):
    r = synthetic_reason(**kw)
    assert bool(r) is synthetic
    if r:
        assert r.startswith("synthetic:") and "#1000" in r


# ---------------------------------------------------------------- ingest

def test_REAL_data_is_still_queued_pending(client, app):
    """The control: the rule must not stop real deliveries."""
    raw = _ingest(client, _body(), GATEWAY)
    logs = _logs(app, raw)
    assert logs and all(s == "pending" and e is None for _, s, e in logs), logs


def test_data_written_by_SIM_is_never_queued(client, app):
    raw = _ingest(client, _body(), SIM)
    logs = _logs(app, raw)
    assert {t for t, _, _ in logs} == {"fhir", "openehr"}
    assert all(s == "skipped" and "sim.pdhc" in e for _, s, e in logs), logs


def test_a_PROMOTED_cohort_is_caught_by_its_run_id(client, app):
    """cdr-transfer may write through another hop; the run id survives."""
    raw = _ingest(client, _body(sim_run_id="sim-20261114-promoted"), GATEWAY)
    logs = _logs(app, raw)
    assert logs and all(s == "skipped" and "sim_run_id" in e for _, s, e in logs), logs


def test_a_context_naming_sim_as_its_source_is_caught(client, app):
    raw = _ingest(client, _body(source_service="sim.pdhc"), GATEWAY)
    assert all(s == "skipped" for _, s, _ in _logs(app, raw))


# ---------------------------------------------------------------- worker

def _requeue_as_pending(app, raw_guid):
    """A row queued BEFORE #1000 existed: synthetic, but 'pending'."""
    with app.app_context():
        for l in CambioDeliveryLog.query.filter_by(ingest_raw_guid=raw_guid):
            l.status, l.last_error = "pending", None
        db.session.commit()


def test_the_worker_skips_a_PRE_QUEUED_sim_row_before_touching_cambio(client, app, monkeypatch):
    sim_raw = _ingest(client, _body(), SIM)
    _requeue_as_pending(app, sim_raw)
    monkeypatch.setitem(app.config, "CAMBIO_DELIVERY_ENABLED", True)
    with patch.object(cambio_worker.CambioClient, "ensure_patient", return_value=("cp", "ehr")) as ensure, \
         patch.object(cambio_worker.CambioClient, "deliver_fhir_observation", return_value="f") as fhir, \
         patch.object(cambio_worker.CambioClient, "deliver_openehr_composition", return_value="o") as oe:
        cambio_worker.run_delivery_cycle(app)
    with app.app_context():
        mine = CambioDeliveryLog.query.filter_by(ingest_raw_guid=sim_raw).all()
        assert mine and all(l.status == "skipped" and l.cambio_resource_id is None for l in mine)
    sim_patient = None
    with app.app_context():
        sim_patient = IngestRaw.query.get(sim_raw).patient_guid
    # ensure_patient would CREATE the synthetic patient in Cambio
    assert sim_patient not in [c.args[0] for c in ensure.call_args_list]


def test_the_worker_still_delivers_REAL_rows(client, app, monkeypatch):
    real_raw = _ingest(client, _body(), GATEWAY)
    monkeypatch.setitem(app.config, "CAMBIO_DELIVERY_ENABLED", True)
    with app.app_context():   # only this test's row is pending
        for l in CambioDeliveryLog.query.filter(CambioDeliveryLog.ingest_raw_guid != real_raw,
                                                CambioDeliveryLog.status == "pending"):
            l.status = "skipped"
        db.session.commit()
    with patch.object(cambio_worker.CambioClient, "ensure_patient", return_value=("cp", "ehr")), \
         patch.object(cambio_worker.CambioClient, "deliver_fhir_observation", return_value="f"), \
         patch.object(cambio_worker.CambioClient, "deliver_openehr_composition", return_value="o"):
        cambio_worker.run_delivery_cycle(app)
    assert {s for _, s, _ in _logs(app, real_raw)} == {"delivered"}
