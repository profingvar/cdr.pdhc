"""#984 (operator 2026-11-17): cdr_read_audit is THE ONE access log for diagnoses.

Covers: the on-behalf user and break-glass headers (allow-listed services only),
the fail-closed break-glass confirmation, POST /api/v1/read-audit/events, and
the append-only guard (ORM + trigger).

Mutation controls (run 2026-11-17, each reverted afterwards; see progress.md):
  M1 accept the headers from any service      -> test_headers_from_an_UNLISTED_service_are_ignored fails
  M2 accept events from any service           -> test_an_event_from_an_UNLISTED_service_is_403 fails
  M3 drop `outcome=` from the event row       -> test_a_denied_event_lands_as_one_row fails
"""
from __future__ import annotations

import logging
from unittest.mock import patch
from urllib.parse import quote

import pytest
import sqlalchemy

from app.models import ReadAudit, db
from app.models.history_guard import history_purge
from app.models.read_audit_guard import ReadAuditIsAppendOnly
from app.services import read_audit as ra
from tests.test_fhir_write import _SERVICE_KEYS, clean_tables, fake_canon  # noqa: F401

P = "462c1906-d09f-415b-80af-e1b094b54451"
ORG = "11111111-2222-4333-8444-555555555555"
USER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CID = "c0000000-0000-4000-8000-000000000984"
SIM = {"X-Source-Service": "sim.pdhc", "X-Service-Key": _SERVICE_KEYS["sim.pdhc"]}
ICD11 = {"X-Source-Service": "icd11.pdhc", "X-Service-Key": "test-icd11-key"}
DASH = {"X-Source-Service": "dashboard.pdhc", "X-Service-Key": "test-dashboard-key"}
AT = f"/api/v1/fhir/Condition/$at?patient={P}&date=2030-01-01"
EVENTS = "/api/v1/read-audit/events"


@pytest.fixture(autouse=True)
def _keys_and_clean_log(app):
    old = {k: app.config.get(k) for k in ("ICD11_PDHC_SERVICE_KEY", "DASHBOARD_PDHC_SERVICE_KEY")}
    app.config["ICD11_PDHC_SERVICE_KEY"] = "test-icd11-key"
    app.config["DASHBOARD_PDHC_SERVICE_KEY"] = "test-dashboard-key"
    with app.app_context(), history_purge(db.session):
        db.session.execute(db.text("DELETE FROM cdr_read_audit"))
    yield
    app.config.update(old)


def _seed(client):
    body = {"resourceType": "Condition", "id": CID, "subject": {"reference": f"Patient/{P}"},
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
            "code": {"coding": [{"system": "https://termbank.pdhc.se/CodeSystem/snomed", "code": "38341003"}]}}
    r = client.put(f"/api/v1/fhir/Condition/{CID}", json=body,
                   headers={**SIM, "X-Patient-Org-Guid": ORG})
    assert r.status_code in (200, 201), r.get_json()


def _rows(app):
    with app.app_context():
        return db.session.query(ReadAudit).order_by(ReadAudit.id).all()


# =========================================================================
# the on-behalf user
# =========================================================================
def test_icd11_states_its_user_and_it_is_recorded_as_a_CLAIM(app, client, fake_canon):
    _seed(client)
    r = client.get(AT, headers={**ICD11, "X-Operator-User-Guid": USER,
                                "X-Operator-Session-Id": "sid-1"})
    assert r.status_code == 200
    row = _rows(app)[-1]
    assert row.caller_service == "icd11.pdhc"
    assert row.on_behalf_of_user_guid == USER
    assert row.caller_user_guid is None, "a claim never fills the verified column"
    assert row.session_id == "sid-1"
    assert row.outcome == "served"
    assert row.patient_guid == P
    assert row.detail["as_of"].startswith("2030-01-01")
    assert r.headers["X-CDR-Read-Audit-Id"] == str(row.id)


def test_headers_from_an_UNLISTED_service_are_ignored(app, client, fake_canon, caplog):
    _seed(client)
    with caplog.at_level(logging.WARNING, logger="app.services.read_audit"):
        r = client.get(AT, headers={**SIM, "X-Operator-User-Guid": USER,
                                    "X-Access-Basis": "break_glass",
                                    "X-Break-Glass-Reason": "nope"})
    assert r.status_code == 200
    row = _rows(app)[-1]
    assert row.caller_service == "sim.pdhc"
    assert row.on_behalf_of_user_guid is None
    assert row.outcome == "served"
    assert not (row.detail or {}).get("reason")
    assert "IGNORED" in caplog.text and "sim.pdhc" in caplog.text


def test_headers_from_an_OPERATOR_session_are_ignored(app, client, fake_canon, caplog):
    _seed(client)
    with caplog.at_level(logging.WARNING, logger="app.services.read_audit"):
        r = client.get(AT, headers={"X-Is-Admin": "1", "X-Operator-User-Guid": USER})
    assert r.status_code == 200
    assert _rows(app)[-1].on_behalf_of_user_guid is None
    assert "non-service caller" in caplog.text


def test_an_overlong_user_guid_is_not_recorded(app, client, fake_canon):
    _seed(client)
    client.get(AT, headers={**ICD11, "X-Operator-User-Guid": "x" * 37})
    assert _rows(app)[-1].on_behalf_of_user_guid is None


# =========================================================================
# break-glass
# =========================================================================
def test_break_glass_is_marked_and_the_reason_kept(app, client, fake_canon):
    _seed(client)
    reason = "remitterad hit — akut bedömning"
    r = client.get(AT, headers={**ICD11, "X-Operator-User-Guid": USER,
                                "X-Access-Basis": "break_glass",
                                "X-Break-Glass-Reason": quote(reason, safe="")})
    assert r.status_code == 200
    row = _rows(app)[-1]
    assert row.outcome == "break_glass"
    assert row.detail["reason"] == reason
    assert row.on_behalf_of_user_guid == USER
    assert r.headers["X-CDR-Read-Audit-Id"] == str(row.id)


def test_a_reason_over_500_chars_is_cut_and_says_so(app, client, fake_canon):
    _seed(client)
    client.get(AT, headers={**ICD11, "X-Access-Basis": "break_glass",
                            "X-Break-Glass-Reason": "a" * 700})
    d = _rows(app)[-1].detail
    assert len(d["reason"]) == 500 and d["reason_truncated"] is True


def test_a_break_glass_read_whose_log_FAILED_is_withheld(app, client, fake_canon):
    _seed(client)
    with patch.object(ra, "ReadAudit", side_effect=RuntimeError("boom")):
        r = client.get(AT, headers={**ICD11, "X-Access-Basis": "break_glass",
                                    "X-Break-Glass-Reason": "giltigt skäl"})
    assert r.status_code == 503
    assert r.get_json()["issue"][0]["diagnostics"] == "BREAK_GLASS_AUDIT_UNAVAILABLE"
    assert "entry" not in r.get_json(), "no patient data in the refusal"
    assert "X-CDR-Read-Audit-Id" not in r.headers


def test_an_ORDINARY_read_whose_log_failed_is_still_served(app, client, fake_canon):
    """Control for the test above: only break-glass fails closed here."""
    _seed(client)
    with patch.object(ra, "ReadAudit", side_effect=RuntimeError("boom")):
        r = client.get(AT, headers={**ICD11, "X-Operator-User-Guid": USER})
    assert r.status_code == 200
    assert "X-CDR-Read-Audit-Id" not in r.headers


# =========================================================================
# POST /api/v1/read-audit/events
# =========================================================================
def _event(**kw):
    ev = {"patient_guid": P, "user_guid": USER, "outcome": "denied",
          "route": "GET /api/v1/episodes/<guid>/history",
          "detail": {"refusal_code": "PATIENT_ACCESS_DENIED", "scope": "organisation",
                     "action": "history", "resource": f"Condition/{CID}"}}
    ev.update(kw)
    return ev


def test_a_denied_event_lands_as_one_row(app, client):
    r = client.post(EVENTS, json=_event(), headers={**ICD11, "X-Operator-Session-Id": "sid-9"})
    assert r.status_code == 201, r.get_json()
    rows = _rows(app)
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == "denied"
    assert row.response_status == 403
    assert row.caller_service == "icd11.pdhc"
    assert row.on_behalf_of_user_guid == USER
    assert row.patient_guid == P and row.session_id == "sid-9"
    assert row.route == "GET /api/v1/episodes/<guid>/history"
    assert row.detail["refusal_code"] == "PATIENT_ACCESS_DENIED"
    assert row.detail["recorded_via"] == "event"
    assert r.get_json() == {"id": row.id, "outcome": "denied"}


def test_a_break_glass_event_carries_its_reason(app, client):
    r = client.post(EVENTS, json=_event(outcome="break_glass", detail={"reason": "akut"}),
                    headers=ICD11)
    assert r.status_code == 201
    row = _rows(app)[0]
    assert (row.outcome, row.response_status, row.detail["reason"]) == ("break_glass", 200, "akut")


def test_an_event_from_an_UNLISTED_service_is_403(app, client):
    r = client.post(EVENTS, json=_event(), headers=SIM)
    assert r.status_code == 403
    assert r.get_json()["code"] == "SERVICE_NOT_PERMITTED"
    assert _rows(app) == []


def test_an_event_from_an_OPERATOR_is_403(app, client):
    r = client.post(EVENTS, json=_event(), headers={"X-Is-Admin": "1"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "SERVICE_KEY_REQUIRED"
    assert _rows(app) == []


def test_read_only_dashboard_may_NOT_post_an_event(app, client):
    """dashboard.pdhc is read-only (#760) and not in EVENT_SERVICES: no write
    path at all, this one included. Its reads still carry the user header."""
    r = client.post(EVENTS, json=_event(), headers=DASH)
    assert r.status_code == 403
    assert _rows(app) == []
    assert client.post(EVENTS, json=_event(), headers=ICD11).status_code == 201   # control

@pytest.mark.parametrize("body,code", [
    ("not-an-object", "BODY_NOT_OBJECT"),
    (_event(patient_guid=None), "PATIENT_GUID_REQUIRED"),
    (_event(patient_guid="a b"), "PATIENT_GUID_INVALID"),
    (_event(user_guid=""), "USER_GUID_REQUIRED"),
    (_event(user_guid="x" * 37), "USER_GUID_INVALID"),
    (_event(outcome="served"), "OUTCOME_INVALID"),
    (_event(route="GET /x\nforged"), "ROUTE_INVALID"),
    (_event(route=""), "ROUTE_REQUIRED"),
    ({**_event(), "code": "1A00"}, "UNKNOWN_FIELD"),
    (_event(detail={"diagnosis": "1A00"}), "DETAIL_UNKNOWN_KEY"),
    (_event(detail={"reason": {"nested": 1}}), "DETAIL_VALUE_INVALID"),
    (_event(detail={"reason": "x" * 501}), "DETAIL_VALUE_TOO_LONG"),
    (_event(detail={"refusal_code": "lower"}), "DETAIL_VALUE_INVALID"),
    (_event(detail=[]), "DETAIL_NOT_OBJECT"),
    (_event(outcome="break_glass", detail={}), "REASON_REQUIRED"),
])
def test_events_are_validated_strictly_by_name(app, client, body, code):
    r = client.post(EVENTS, json=body, headers=ICD11)
    assert r.status_code == 400
    assert r.get_json()["code"] == code
    assert _rows(app) == []


def test_the_events_path_takes_no_read_or_update(app, client):
    for m in ("get", "put", "delete"):
        assert getattr(client, m)(EVENTS, headers=ICD11).status_code == 405


def test_an_event_reads_no_patient_data(app, client):
    """It inserts one log row and touches no other table."""
    stmts = []
    from sqlalchemy import event as sa_event
    with app.app_context():
        eng = db.engine

        def _rec(conn, cursor, statement, *a):
            stmts.append(statement)
        sa_event.listen(eng, "before_cursor_execute", _rec)
        try:
            r = client.post(EVENTS, json=_event(), headers=ICD11)
        finally:
            sa_event.remove(eng, "before_cursor_execute", _rec)
    assert r.status_code == 201
    assert stmts, "control: the listener saw the INSERT"
    assert all("cdr_read_audit" in s for s in stmts), stmts
    assert any(s.lstrip().upper().startswith("INSERT") for s in stmts), stmts


# =========================================================================
# append-only
# =========================================================================
def _one_row(app, client):
    assert client.post(EVENTS, json=_event(), headers=ICD11).status_code == 201


def test_an_ORM_update_or_delete_of_a_log_row_is_refused(app, client):
    _one_row(app, client)
    with app.app_context():
        row = db.session.query(ReadAudit).one()
        row.outcome = "served"
        with pytest.raises(ReadAuditIsAppendOnly):
            db.session.flush()
        db.session.rollback()
        row = db.session.query(ReadAudit).one()
        db.session.delete(row)
        with pytest.raises(ReadAuditIsAppendOnly):
            db.session.flush()
        db.session.rollback()
        assert db.session.query(ReadAudit).one().outcome == "denied"


def test_RAW_SQL_cannot_update_or_delete_the_log_either(app, client):
    _one_row(app, client)
    with app.app_context():
        for stmt in ("UPDATE cdr_read_audit SET outcome = 'served'",
                     "DELETE FROM cdr_read_audit"):
            with pytest.raises(sqlalchemy.exc.DatabaseError, match="append-only"):
                db.session.execute(db.text(stmt))
            db.session.rollback()
        assert db.session.query(ReadAudit).one().outcome == "denied"


def test_even_a_purge_cannot_REWRITE_the_log(app, client):
    _one_row(app, client)
    with app.app_context():
        with pytest.raises(sqlalchemy.exc.DatabaseError):
            with history_purge(db.session):
                db.session.execute(db.text("UPDATE cdr_read_audit SET outcome = 'served'"))
        assert db.session.query(ReadAudit).one().outcome == "denied"


def test_the_triggers_exist(app):
    with app.app_context():
        names = {r[0] for r in db.session.execute(db.text(
            "select name from sqlite_master where type='trigger'"))}
    assert {"cdr_read_audit_no_update", "cdr_read_audit_no_delete"} <= names
