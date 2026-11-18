"""_pdhc_counts=patient,code on Observation search (2026-11-18).

analyse's cohort preview needs, per patient, how many measurements of each
concept a CDR holds. Counting rides on the existing search so the org scope,
the filters, the consent filter and the read audit apply unchanged.
"""
import app.api.fhir_read as fr
from tests.test_fhir_read import (  # noqa: F401  (fixtures)
    _hba1c, _post, ORG_HEADERS, clean_tables, fake_canon,
)

P1 = "11111111-aaaa-4aaa-8aaa-000000000001"
P2 = "11111111-aaaa-4aaa-8aaa-000000000002"


def _seed(client):
    for i in range(3):
        _post(client, "/api/v1/fhir/Observation",
              _hba1c(value=6 + i, eff=f"2026-04-0{i + 1}T10:00:00Z", patient=P1))
    _post(client, "/api/v1/fhir/Observation", _hba1c(patient=P1, code="2345-7"))
    _post(client, "/api/v1/fhir/Observation", _hba1c(patient=P2))


def _counts(client, extra=""):
    return client.get(f"/api/v1/fhir/Observation?_pdhc_counts=patient,code{extra}",
                      headers=ORG_HEADERS)


def test_counts_per_patient_and_code(client, fake_canon):
    _seed(client)
    r = _counts(client)
    assert r.status_code == 200
    body = r.get_json()
    assert body["resourceType"] == "PdhcCounts"
    got = {(c["patient"], c["code"].rsplit("/", 1)[-1]): c["n"] for c in body["counts"]}
    assert got == {(P1, "4548-4"): 3, (P1, "2345-7"): 1, (P2, "4548-4"): 1}


def test_the_other_search_filters_still_apply(client, fake_canon):
    _seed(client)
    body = _counts(client, f"&patient={P2}").get_json()
    assert [(c["patient"], c["n"]) for c in body["counts"]] == [(P2, 1)]


def test_consent_filter_applies_to_counts(client, fake_canon, monkeypatch):
    _seed(client)
    monkeypatch.setattr(fr, "consent_allowed_guids", lambda guids: {P2})
    body = _counts(client).get_json()
    assert {c["patient"] for c in body["counts"]} == {P2}


def test_org_scope_applies_to_counts(client, fake_canon):
    _seed(client)
    r = client.get("/api/v1/fhir/Observation?_pdhc_counts=patient,code",
                   headers={"X-Org-Guids": "99999999-9999-4999-8999-999999999999"})
    assert r.status_code == 200
    assert r.get_json()["counts"] == []


def test_only_patient_code_grouping_is_accepted(client):
    r = client.get("/api/v1/fhir/Observation?_pdhc_counts=code",
                   headers=ORG_HEADERS)
    assert r.status_code == 400
