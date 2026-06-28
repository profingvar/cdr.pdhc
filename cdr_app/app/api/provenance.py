"""Per-point provenance bundle — recover the full GUID kostym for one Observation.

CDR1's storage contract (cdr1_analyse_split_plan §5 phase 2, ticket #288):
every stored data point is recoverable with its semantic context — the
linked ServiceRequest, PlanDefinition, Contract, and Organizations.
This is the storage-layer counterpart of the aggregation moves that
ship $stats and $agp out to the analyse layer.

The provenance is reconstructed from two sources, in order:

1. The FHIR Observation `resource_json` itself, which already carries
   the GUIDs in `basedOn[]`, `performer[]`, and pdhc-namespaced
   extensions (gateway's `fhir_observation_builder` writes them
   there).
2. The `ClinicalContext` row keyed by `ingest_raw_guid`, which
   carries `care_plan_guid`, `plan_definition_guid`, `transaction_guid` as
   structured columns.

Missing data degrades gracefully — the bundle simply omits the entry
for that role. Never fails on partial context.
"""
from datetime import datetime, timezone
from flask import Blueprint, jsonify
from app.models import FhirResource, ClinicalContext
from .auth import require_service_key


bp = Blueprint("provenance", __name__)


# pdhc extension URLs that gateway's fhir_observation_builder writes:
_EXT_CONTRACT = "urn:pdhc:fhir:extension:contract"
_EXT_REQUESTING_ORG = "urn:pdhc:fhir:extension:requesting-organization"


def _identifier_value(reference_or_dict):
    """Pull `identifier.value` out of a FHIR Reference, tolerant to shape."""
    if not isinstance(reference_or_dict, dict):
        return None
    ident = reference_or_dict.get("identifier") or {}
    if isinstance(ident, dict):
        return ident.get("value")
    return None


def _basedon_guid(observation, ref_type):
    """Find the GUID in `basedOn[]` whose entry has `type == ref_type`."""
    for entry in (observation.get("basedOn") or []):
        if isinstance(entry, dict) and entry.get("type") == ref_type:
            return _identifier_value(entry)
    return None


def _performer_guid(observation):
    """First performer's identifier.value, treated as the provider Organization."""
    for entry in (observation.get("performer") or []):
        v = _identifier_value(entry)
        if v:
            return v
    return None


def _extension_ref_guid(observation, url):
    """Find an `extension[*].valueReference.identifier.value` by `url`."""
    for ext in (observation.get("extension") or []):
        if isinstance(ext, dict) and ext.get("url") == url:
            return _identifier_value(ext.get("valueReference") or {})
    return None


def _stub_resource(rtype, guid, full_url):
    """Build a minimal Bundle entry — just enough for a consumer to
    deref via fullUrl. The body is the resource shell, not a faked copy.
    """
    return {
        "fullUrl": full_url,
        "resource": {
            "resourceType": rtype,
            "id": guid,
            "identifier": [{"value": guid}],
        },
    }


@bp.get("/observations/<guid>/provenance")
@require_service_key
def observation_provenance(guid):
    """Return a FHIR R5 Bundle (collection) with the Observation + its
    GUID provenance: ServiceRequest, PlanDefinition, Contract,
    requesting + provider Organizations. Missing context degrades
    gracefully.
    """
    fhir_row = (
        FhirResource.query
        .filter_by(guid=guid, resource_type="Observation")
        .first()
    )
    if fhir_row is None:
        return jsonify({"error": f"Observation {guid} not found"}), 404

    observation = fhir_row.resource_json or {}

    # #307 phase 7: read context guids from the canonical
    # ClinicalContext columns first (#302 widened the schema and
    # backfilled them). Fall back to FhirResource.resource_json for
    # rows that pre-date the backfill or whose ClinicalContext row is
    # missing fields.
    context_row = (
        ClinicalContext.query
        .filter_by(ingest_raw_guid=fhir_row.ingest_raw_guid)
        .first()
    )
    if context_row is not None:
        sr_guid             = context_row.service_request_guid
        plan_definition_guid        = context_row.plan_definition_guid
        provider_org_guid   = context_row.provider_org_guid
        contract_guid       = context_row.contract_guid
        requesting_org_guid = context_row.requesting_org_guid
        care_plan_guid       = context_row.care_plan_guid
    else:
        sr_guid = plan_definition_guid = provider_org_guid = None
        contract_guid = requesting_org_guid = care_plan_guid = None

    # Fill remaining gaps from the FHIR Observation's own back-refs.
    sr_guid             = sr_guid             or _basedon_guid(observation, "ServiceRequest")
    plan_definition_guid        = plan_definition_guid        or _basedon_guid(observation, "PlanDefinition")
    provider_org_guid   = provider_org_guid   or _performer_guid(observation)
    contract_guid       = contract_guid       or _extension_ref_guid(observation, _EXT_CONTRACT)
    requesting_org_guid = requesting_org_guid or _extension_ref_guid(observation, _EXT_REQUESTING_ORG)

    entries = []
    # 1. The Observation itself — `fullUrl` is the cdr1 by-GUID path.
    entries.append({
        "fullUrl": f"https://cdr.pdhc.se/api/v1/fhir/Observation/{fhir_row.guid}",
        "resource": observation,
    })

    if sr_guid:
        entries.append(_stub_resource(
            "ServiceRequest", sr_guid,
            f"https://request.pdhc.se/api/v1/service-requests/{sr_guid}",
        ))
    if plan_definition_guid:
        entries.append(_stub_resource(
            "PlanDefinition", plan_definition_guid,
            f"https://plan.pdhc.se/api/v1/plandefinitions/{plan_definition_guid}",
        ))
    if care_plan_guid and care_plan_guid != plan_definition_guid:
        entries.append(_stub_resource(
            "CarePlan", care_plan_guid,
            f"https://plan.pdhc.se/api/v1/careplans/{care_plan_guid}",
        ))
    if contract_guid:
        entries.append(_stub_resource(
            "Contract", contract_guid,
            f"https://contract.pdhc.se/fhir/Contract/{contract_guid}",
        ))
    if requesting_org_guid:
        entries.append(_stub_resource(
            "Organization", requesting_org_guid,
            f"https://sso.pdhc.se/api/organisations/{requesting_org_guid}",
        ))
    if provider_org_guid and provider_org_guid != requesting_org_guid:
        entries.append(_stub_resource(
            "Organization", provider_org_guid,
            f"https://sso.pdhc.se/api/organisations/{provider_org_guid}",
        ))

    return jsonify({
        "resourceType": "Bundle",
        "type": "collection",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(entries),
        "entry": entries,
    }), 200
