"""External read door (2026-11-19): a time-limited sso read grant reads
de-identified patient data, bound to the grant's organisations and CDRs."""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app import db
from app.api import external_read as er
from app.models.resources import live_model

ORG_IN, ORG_OUT = "org-in-" + uuid.uuid4().hex[:8], "org-out-" + uuid.uuid4().hex[:8]
KEY = "rg.11111111-1111-1111-1111-111111111111.secret"
H = {"Authorization": f"Bearer {KEY}"}


def _resp(code, body=None):
    r = MagicMock(status_code=code, headers={"content-type": "application/json"})
    r.json.return_value = body or {}
    return r


def _grant(orgs=(ORG_IN,)):
    return {"grant_guid": "g-1", "partner_guid": "p-1", "partner_name": "X",
            "org_guids": list(orgs), "cdrs": ["cdr1"], "expires_at": "2026-11-21T00:00:00+00:00"}


IPS = {"patient_guid": None, "sex": "female", "birth_year": 1961,
       "conditions": [{"code": [{"code": "5A11"}]}], "medications": []}


@pytest.fixture
def wired(app):
    """sso answers with the grant, ips with the summary; consent allows all."""
    calls = {"sso": [], "ips": []}

    def post(url, json=None, headers=None, timeout=None):
        if "read-grants/validate" in url:
            calls["sso"].append(json)
            return calls.get("sso_answer") or _resp(200, _grant())
        calls["ips"].append(json)
        return _resp(200, {"patients": [{**IPS, "patient_guid": json["patient_guids"][0]}]})

    cfg = {"SSO_BASE_URL": "http://sso", "SSO_CLIENT_ID": "c", "SSO_CLIENT_SECRET": "s",
           "IPS_BASE_URL": "http://ips", "IPS_API_KEY": "k"}
    from app.services import analysis_consent as ac
    with patch.dict(app.config, cfg), patch.object(er.requests, "post", side_effect=post), \
            patch.object(ac, "_analysis_filter",
                         side_effect=lambda g, p, pr: {"allowed": list(g)}):
        yield calls


def _obs(patient, org, when, value, code="glucose"):
    M = live_model("Observation")
    row = M(guid=str(uuid.uuid4()), patient_guid=patient, patient_org_guid=org,
            code_canonical=code, effective_at=when, value_quantity=value, value_unit="mmol/L",
            raw_json={"resourceType": "Observation", "status": "final",
                      "subject": {"display": "Greta Hemligsson"},
                      "note": [{"text": "ringde Greta"}]})
    db.session.add(row)
    db.session.commit()
    return row.guid


def test_no_key_or_wrong_kind_of_key_is_refused(client):
    assert client.get("/external/v1/patients").status_code == 401
    assert client.get("/external/v1/patients",
                      headers={"Authorization": "Bearer eyJ.sso.token"}).status_code == 401


def test_lists_only_patients_in_the_grants_organisations(client, wired):
    p_in, p_out = str(uuid.uuid4()), str(uuid.uuid4())
    _obs(p_in, ORG_IN, datetime(2026, 1, 1, tzinfo=timezone.utc), 5.0)
    _obs(p_out, ORG_OUT, datetime(2026, 1, 1, tzinfo=timezone.utc), 6.0)
    r = client.get("/external/v1/patients", headers=H)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert p_in in r.get_json()["patients"] and p_out not in r.get_json()["patients"]
    assert wired["sso"][0] == {"key": KEY, "cdr": "cdr1"}


def test_patient_view_has_values_both_times_and_ips_but_no_identity(client, wired):
    p = str(uuid.uuid4())
    _obs(p, ORG_IN, datetime(2026, 1, 1, 8, tzinfo=timezone.utc), 5.0)
    _obs(p, ORG_IN, datetime(2026, 1, 4, 22, tzinfo=timezone.utc), 7.5)
    _obs(p, ORG_OUT, datetime(2026, 1, 2, tzinfo=timezone.utc), 99.0)   # other org: hidden
    r = client.get(f"/external/v1/patients/{p}", headers=H)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    pts = body["datapoints"]
    assert [(x["value"], x["day"]) for x in pts] == [(5.0, 0), (7.5, 3)]
    assert pts[0]["effective_at"].startswith("2026-01-01T08:00") and pts[0]["unit"] == "mmol/L"
    assert body["ips"]["birth_year"] == 1961 and body["ips_status"] == "ok"
    text = r.get_data(as_text=True)
    assert "Greta" not in text and "Hemligsson" not in text and "raw_json" not in text


def test_patient_outside_the_grant_is_a_plain_404(client, wired):
    p = str(uuid.uuid4())
    _obs(p, ORG_OUT, datetime(2026, 1, 1, tzinfo=timezone.utc), 5.0)
    assert client.get(f"/external/v1/patients/{p}", headers=H).status_code == 404
    assert client.get(f"/external/v1/patients/{uuid.uuid4()}", headers=H).status_code == 404
    assert wired["ips"] == []                       # ips never asked about them


def test_opted_out_patient_is_not_served(client, wired):
    p = str(uuid.uuid4())
    _obs(p, ORG_IN, datetime(2026, 1, 1, tzinfo=timezone.utc), 5.0)
    from app.services import analysis_consent as ac
    with patch.object(ac, "_analysis_filter", return_value={"allowed": []}):
        assert p not in client.get("/external/v1/patients", headers=H).get_json()["patients"]
        assert client.get(f"/external/v1/patients/{p}", headers=H).status_code == 404


def test_sso_refusal_is_passed_on_and_an_outage_fails_closed(client, wired):
    wired["sso_answer"] = _resp(403, {"error": "forbidden", "reason": "expired"})
    r = client.get("/external/v1/patients", headers=H)
    assert r.status_code == 403 and r.get_json()["reason"] == "expired"
    wired["sso_answer"] = _resp(401)
    assert client.get("/external/v1/patients", headers=H).status_code == 401
    wired["sso_answer"] = _resp(500)
    assert client.get("/external/v1/patients", headers=H).status_code == 503


@pytest.mark.parametrize("inst,name", [(None, "cdr1"), ("cdr_pdhc", "cdr1"),
                                       ("cdr_pdhc_2", "cdr2"), ("cdr_pdhc_3", "cdr3")])
def test_cdr_id(monkeypatch, inst, name):
    if inst is None:
        monkeypatch.delenv("CDR_INSTANCE", raising=False)
    else:
        monkeypatch.setenv("CDR_INSTANCE", inst)
    assert er.cdr_id() == name
