"""FHIR CapabilityStatement for cdr.pdhc.

The actual resource read / search / vread / $everything / terminology
endpoints live in ``fhir_read.py`` and the create / update / Bundle endpoints
live in ``fhir_write.py``. This module is now just the metadata surface.

$stats / $agp aggregations were moved to dashboard.pdhc's analyse
layer in phase 3 of the CDR1/Analyse split (ticket #289). cdr1 is
pure storage; analyse fetches raw Observations via the search endpoint
and computes aggregates locally.
"""
from datetime import datetime, timezone
from flask import Blueprint, jsonify

bp = Blueprint("fhir", __name__)


_RESOURCE_TYPES = [
    "Patient", "Observation", "QuestionnaireResponse", "Condition",
    "MedicationStatement", "MedicationRequest", "AllergyIntolerance",
    "Procedure", "Encounter", "DiagnosticReport",
]


@bp.get("/metadata")
def capability_statement():
    return jsonify({
        "resourceType": "CapabilityStatement",
        "id": "cdr-pdhc",
        "url": "https://cdr.pdhc.se/fhir/metadata",
        "status": "active",
        "kind": "instance",
        "date": datetime.now(timezone.utc).isoformat(),
        "publisher": "PDHC platform",
        "software": {"name": "cdr.pdhc", "version": "0.2.0"},
        "fhirVersion": "5.0.0",
        "format": ["json"],
        "rest": [{
            "mode": "server",
            "documentation": (
                "Common Clinical Data Repository. Per-type FHIR R5 tables, "
                "transaction Bundle endpoint, $everything per Patient, "
                "per-point Observation provenance, terminology operations "
                "proxied to termbank.pdhc / xlate.pdhc / plan.pdhc. "
                "Group aggregations ($stats, $agp) are served by the "
                "dashboard.pdhc analyse layer over raw search results."
            ),
            "resource": [
                {
                    "type": rt,
                    "interaction": [
                        {"code": "read"},
                        {"code": "vread"},
                        {"code": "search-type"},
                        {"code": "create"},
                        {"code": "update"},
                        {"code": "history-instance"},
                    ],
                    "searchParam": [
                        {"name": "patient", "type": "reference"},
                        {"name": "code", "type": "token"},
                        {"name": "date", "type": "date"},
                        {"name": "_tag", "type": "token"},
                        {"name": "_id", "type": "token"},
                        {"name": "_count", "type": "number"},
                    ],
                }
                for rt in _RESOURCE_TYPES
            ],
            "operation": [
                {
                    "name": "everything",
                    "definition": "http://hl7.org/fhir/OperationDefinition/Patient-everything",
                },
                {
                    "name": "provenance",
                    "definition": "https://cdr.pdhc.se/OperationDefinition/Observation-provenance",
                    "documentation": (
                        "GET /api/v1/observations/<guid>/provenance — "
                        "returns a Bundle (type=collection) with the "
                        "Observation plus its linked ServiceRequest, "
                        "PlanDefinition, Contract, requesting + "
                        "provider Organization. Missing context "
                        "degrades gracefully. Service-key auth."
                    ),
                },
            ],
        }],
    }), 200
