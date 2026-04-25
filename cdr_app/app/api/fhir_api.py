"""FHIR CapabilityStatement for cdr.pdhc.

The actual resource read / search / vread / $everything / $stats / terminology
endpoints live in ``fhir_read.py`` and the create / update / Bundle endpoints
live in ``fhir_write.py``. This module is now just the metadata surface.
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
                "$stats per Observation code, terminology operations proxied "
                "to termbank.pdhc / xlate.pdhc / plan.pdhc."
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
            ] + [
                {
                    "type": "CodeSystem",
                    "operation": [{
                        "name": "lookup",
                        "definition": "http://hl7.org/fhir/OperationDefinition/CodeSystem-lookup",
                    }],
                },
                {
                    "type": "ConceptMap",
                    "operation": [{
                        "name": "translate",
                        "definition": "http://hl7.org/fhir/OperationDefinition/ConceptMap-translate",
                    }],
                },
                {
                    "type": "ValueSet",
                    "operation": [{
                        "name": "validate-code",
                        "definition": "http://hl7.org/fhir/OperationDefinition/ValueSet-validate-code",
                    }],
                },
            ],
            "operation": [
                {
                    "name": "everything",
                    "definition": "http://hl7.org/fhir/OperationDefinition/Patient-everything",
                },
                {
                    "name": "stats",
                    "definition": "http://hl7.org/fhir/OperationDefinition/Observation-stats",
                },
            ],
        }],
    }), 200
