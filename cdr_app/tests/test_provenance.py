"""Tests for the per-point provenance Bundle endpoint (ticket #288).

Verifies:
  - Bundle structure: resourceType=Bundle, type=collection, entries
  - Hit returns the Observation plus the linked context resources
  - Miss returns 404 with the documented error shape
  - Partial context degrades gracefully — no failure, fewer entries
  - Auth required
"""
import uuid

from app import db
from app.models import FhirResource, IngestRaw, ClinicalContext


HEADERS = {
    "X-Service-Key": "test-gateway-key",
    "X-Source-Service": "gateway.pdhc",
}


def _full_observation(sr_guid, plandef_guid, contract_guid,
                      requesting_org_guid, provider_org_guid):
    """An Observation shaped the way gateway's fhir_observation_builder
    emits it — with basedOn / performer / extension carrying all the
    provenance GUIDs.
    """
    return {
        "resourceType": "Observation",
        "id": str(uuid.uuid4()),
        "status": "final",
        "code": {
            "coding": [{
                "system": "https://plan.pdhc.se/api/v1/concepts",
                "code": "concept-001",
                "display": "B-glucose",
            }],
            "text": "B-glucose",
        },
        "subject": {"reference": "Patient/pat-prov-1"},
        "effectiveDateTime": "2026-04-10T10:00:00Z",
        "valueQuantity": {"value": 5.6, "unit": "mmol/L"},
        "basedOn": [
            {
                "reference": f"https://request.pdhc.se/api/v1/service-requests/{sr_guid}",
                "type": "ServiceRequest",
                "identifier": {"value": sr_guid},
            },
            {
                "reference": f"https://plan.pdhc.se/api/v1/plandefinitions/{plandef_guid}",
                "type": "PlanDefinition",
                "identifier": {"value": plandef_guid},
            },
        ],
        "performer": [{
            "reference": f"https://sso.pdhc.se/api/organisations/{provider_org_guid}",
            "type": "Organization",
            "identifier": {"value": provider_org_guid},
        }],
        "extension": [
            {
                "url": "urn:pdhc:fhir:extension:contract",
                "valueReference": {
                    "reference": f"https://contract.pdhc.se/fhir/Contract/{contract_guid}",
                    "type": "Contract",
                    "identifier": {"value": contract_guid},
                },
            },
            {
                "url": "urn:pdhc:fhir:extension:requesting-organization",
                "valueReference": {
                    "reference": f"https://sso.pdhc.se/api/organisations/{requesting_org_guid}",
                    "type": "Organization",
                    "identifier": {"value": requesting_org_guid},
                },
            },
        ],
    }


def _seed(app, *, observation, with_context=True, plandef_guid=None,
          careplan_guid=None):
    """Insert IngestRaw + FhirResource (+ optional ClinicalContext).
    Returns the FhirResource.guid for the GET request.
    """
    with app.app_context():
        raw = IngestRaw(
            source_service="gateway.pdhc",
            patient_guid="pat-prov-1",
            payload_json={"observation": observation},
            payload_hash="hash-prov-" + str(uuid.uuid4()),
        )
        db.session.add(raw)
        db.session.flush()

        fhir = FhirResource(
            ingest_raw_guid=raw.guid,
            patient_guid="pat-prov-1",
            resource_type="Observation",
            resource_json=observation,
            source_service="gateway.pdhc",
        )
        db.session.add(fhir)

        if with_context:
            db.session.add(ClinicalContext(
                ingest_raw_guid=raw.guid,
                patient_guid="pat-prov-1",
                transaction_guid="tx-prov-1",
                care_plan_guid=careplan_guid,
                plan_definition_guid=plandef_guid,
                source_service="gateway.pdhc",
            ))

        db.session.commit()
        return fhir.guid


def test_provenance_requires_auth(client):
    resp = client.get("/api/v1/observations/whatever/provenance")
    assert resp.status_code == 401


def test_provenance_miss_returns_404(client):
    resp = client.get(
        "/api/v1/observations/00000000-0000-0000-0000-000000000000/provenance",
        headers=HEADERS,
    )
    assert resp.status_code == 404
    body = resp.get_json()
    assert "not found" in body["error"]


def test_provenance_full_bundle(client, app):
    sr_guid = "sr-" + str(uuid.uuid4())[:8]
    plandef_guid = "pd-" + str(uuid.uuid4())[:8]
    contract_guid = "ct-" + str(uuid.uuid4())[:8]
    req_org = "org-req-" + str(uuid.uuid4())[:8]
    prov_org = "org-prov-" + str(uuid.uuid4())[:8]
    obs = _full_observation(sr_guid, plandef_guid, contract_guid,
                            req_org, prov_org)
    fhir_guid = _seed(app, observation=obs)

    resp = client.get(
        f"/api/v1/observations/{fhir_guid}/provenance",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    bundle = resp.get_json()
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"
    assert "timestamp" in bundle
    assert bundle["total"] == len(bundle["entry"])

    entries_by_type = {}
    for e in bundle["entry"]:
        rtype = e["resource"]["resourceType"]
        entries_by_type.setdefault(rtype, []).append(e)

    # Observation itself
    assert entries_by_type["Observation"][0]["resource"]["id"] == obs["id"]
    assert entries_by_type["Observation"][0]["fullUrl"].endswith(fhir_guid)

    # ServiceRequest stub
    sr = entries_by_type["ServiceRequest"][0]
    assert sr["resource"]["id"] == sr_guid
    assert sr["fullUrl"].endswith(sr_guid)

    # PlanDefinition stub
    pd = entries_by_type["PlanDefinition"][0]
    assert pd["resource"]["id"] == plandef_guid

    # Contract stub
    ct = entries_by_type["Contract"][0]
    assert ct["resource"]["id"] == contract_guid

    # Both Organization stubs (requesting + provider) — distinct ids
    orgs = [e["resource"]["id"] for e in entries_by_type["Organization"]]
    assert req_org in orgs and prov_org in orgs


def test_provenance_partial_context_degrades(client, app):
    # No basedOn, no extension — just a bare Observation.
    bare = {
        "resourceType": "Observation",
        "id": str(uuid.uuid4()),
        "status": "final",
        "code": {"text": "bare"},
        "subject": {"reference": "Patient/pat-prov-1"},
    }
    fhir_guid = _seed(app, observation=bare, with_context=False)

    resp = client.get(
        f"/api/v1/observations/{fhir_guid}/provenance",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    bundle = resp.get_json()
    # Only the Observation entry, nothing else.
    assert bundle["total"] == 1
    assert bundle["entry"][0]["resource"]["resourceType"] == "Observation"


def test_provenance_falls_back_to_clinical_context_for_plandef(client, app):
    """If the Observation lacks basedOn but ClinicalContext has plandef_guid,
    surface it from there.
    """
    plandef_guid = "pd-fallback-" + str(uuid.uuid4())[:8]
    obs_no_basedon = {
        "resourceType": "Observation",
        "id": str(uuid.uuid4()),
        "status": "final",
        "code": {"text": "ctx-only"},
        "subject": {"reference": "Patient/pat-prov-1"},
        # No basedOn, no extension
    }
    fhir_guid = _seed(
        app, observation=obs_no_basedon, with_context=True,
        plandef_guid=plandef_guid,
    )

    resp = client.get(
        f"/api/v1/observations/{fhir_guid}/provenance",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    bundle = resp.get_json()
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert "PlanDefinition" in types
    pd_entry = next(e for e in bundle["entry"]
                    if e["resource"]["resourceType"] == "PlanDefinition")
    assert pd_entry["resource"]["id"] == plandef_guid


def test_provenance_only_matches_observation_resource_type(client, app):
    """A FhirResource of resource_type != 'Observation' must 404 even if
    the guid matches.
    """
    with app.app_context():
        raw = IngestRaw(
            source_service="gateway.pdhc",
            patient_guid="pat-not-obs",
            payload_json={"x": 1},
            payload_hash="hash-not-obs-" + str(uuid.uuid4()),
        )
        db.session.add(raw)
        db.session.flush()
        fhir = FhirResource(
            ingest_raw_guid=raw.guid,
            patient_guid="pat-not-obs",
            resource_type="Condition",
            resource_json={"resourceType": "Condition", "id": "c1"},
            source_service="gateway.pdhc",
        )
        db.session.add(fhir)
        db.session.commit()
        guid = fhir.guid

    resp = client.get(
        f"/api/v1/observations/{guid}/provenance",
        headers=HEADERS,
    )
    assert resp.status_code == 404
