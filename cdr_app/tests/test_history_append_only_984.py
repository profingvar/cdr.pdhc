"""#984: the CDR is the ONE diagnosis register, so its history is append-only
(it replaces icd11.pdhc's append-only assertion log, #873)."""
from __future__ import annotations

import pytest
import sqlalchemy

from app.models import db
from app.models.history_guard import HistoryIsAppendOnly, history_purge
from app.models.resources import history_model, live_model
from tests.test_fhir_write import _SERVICE_KEYS, clean_tables, fake_canon  # noqa: F401

P = "462c1906-d09f-415b-80af-e1b094b54451"
ORG = "11111111-2222-4333-8444-555555555555"
HDR = {"X-Source-Service": "sim.pdhc", "X-Service-Key": _SERVICE_KEYS["sim.pdhc"], "X-Patient-Org-Guid": ORG}


def _cond(status="active", cid="c0000000-0000-4000-8000-000000000984"):
    return {"resourceType": "Condition", "id": cid, "subject": {"reference": f"Patient/{P}"},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": status}]},
            "code": {"coding": [{"system": "https://termbank.pdhc.se/CodeSystem/snomed", "code": "38341003"}]}}


def _two_versions(client):
    for st in ("active", "resolved"):
        r = client.put(f"/api/v1/fhir/Condition/{_cond()['id']}", json=_cond(st), headers=HDR)
        assert r.status_code in (200, 201), r.get_json()
    H = history_model("Condition")
    rows = H.query.all()
    assert len(rows) == 1, "control: an update moved the old version into history"
    return H, rows[0]


def test_an_ORM_update_of_a_history_row_is_refused(app, client, fake_canon):
    with app.app_context():
        H, row = _two_versions(client)
        row.source = "rewritten"
        with pytest.raises(HistoryIsAppendOnly):
            db.session.flush()
        db.session.rollback()


def test_RAW_SQL_cannot_update_or_delete_history_either(app, client, fake_canon):
    with app.app_context():
        _two_versions(client)
        for stmt in ("UPDATE condition_history SET source = 'x'", "DELETE FROM condition_history"):
            with pytest.raises(sqlalchemy.exc.DatabaseError, match="append-only"):
                db.session.execute(db.text(stmt))
            db.session.rollback()
        assert history_model("Condition").query.count() == 1


def test_the_permission_does_not_outlive_the_purge(app, client, fake_canon):
    with app.app_context():
        _two_versions(client)
        with history_purge(db.session):
            db.session.execute(db.text("DELETE FROM condition_history"))
        assert history_model("Condition").query.count() == 0
        client.put(f"/api/v1/fhir/Condition/{_cond()['id']}", json=_cond("active"), headers=HDR)
        assert history_model("Condition").query.count() == 1
        with pytest.raises(sqlalchemy.exc.DatabaseError):
            db.session.execute(db.text("DELETE FROM condition_history"))
        db.session.rollback()


def test_a_purge_still_cannot_REWRITE_history_and_an_error_rolls_it_back(app, client, fake_canon):
    with app.app_context():
        _two_versions(client)
        with pytest.raises(sqlalchemy.exc.DatabaseError):
            with history_purge(db.session):
                db.session.execute(db.text("UPDATE condition_history SET source = 'x'"))
        assert history_model("Condition").query.one().source != "x"
        with pytest.raises(sqlalchemy.exc.DatabaseError):     # guard closed again
            db.session.execute(db.text("DELETE FROM condition_history"))
        db.session.rollback()


def test_the_purge_flag_is_EMPTY_after_test_cleanup(app):
    """The leak this module was first written with: clean_tables' purge left
    the flag row behind, and every later test ran with the guard open."""
    with app.app_context():
        assert db.session.execute(db.text("select count(*) from cdr_history_purge_flag")).scalar() == 0


def test_encounter_and_condition_history_are_guarded(app):
    """#1054 widened the guard to every type (tests/test_record_stage2_1054.py
    covers the rest); #984's two stay guarded."""
    with app.app_context():
        names = {r[0] for r in db.session.execute(db.text(
            "select name from sqlite_master where type='trigger'"))}
        assert {"encounter_history_no_update", "encounter_history_no_delete",
                "condition_history_no_update", "condition_history_no_delete"} <= names
