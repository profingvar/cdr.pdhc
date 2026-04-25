"""FHIR ↔ openEHR bidirectional transformation.

Uses the LOINC-to-archetype mapping table for known types.
Falls back to generic laboratory_test_result archetype.
"""
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# In-memory seed mappings (loaded from DB at init, but seed here for bootstrap)
LOINC_TO_ARCHETYPE = {
    "29463-7": {"archetype": "openEHR-EHR-OBSERVATION.body_weight.v2", "metric": "body_weight_kg", "unit": "kg"},
    "85354-9": {"archetype": "openEHR-EHR-OBSERVATION.blood_pressure.v2", "metric": "blood_pressure_systolic", "unit": "mmHg"},
    "8867-4":  {"archetype": "openEHR-EHR-OBSERVATION.pulse.v2", "metric": "heart_rate_bpm", "unit": "/min"},
    "8310-5":  {"archetype": "openEHR-EHR-OBSERVATION.body_temperature.v2", "metric": "body_temperature_c", "unit": "Cel"},
    "2708-6":  {"archetype": "openEHR-EHR-OBSERVATION.pulse_oximetry.v1", "metric": "spo2_percent", "unit": "%"},
    "8302-2":  {"archetype": "openEHR-EHR-OBSERVATION.height.v2", "metric": "body_height_cm", "unit": "cm"},
    "9279-1":  {"archetype": "openEHR-EHR-OBSERVATION.respiration.v2", "metric": "respiratory_rate", "unit": "/min"},
    "39156-5": {"archetype": "openEHR-EHR-OBSERVATION.body_mass_index.v2", "metric": "bmi", "unit": "kg/m2"},
    "2339-0":  {"archetype": "openEHR-EHR-OBSERVATION.laboratory_test_result.v1", "metric": "blood_glucose_mmol", "unit": "mmol/L"},
    "8280-0":  {"archetype": "openEHR-EHR-OBSERVATION.waist_circumference.v2", "metric": "waist_circumference_cm", "unit": "cm"},
    "93832-4": {"archetype": "openEHR-EHR-OBSERVATION.sleep.v0", "metric": "sleep_hours", "unit": "h"},
}

# Reverse map: archetype → LOINC
ARCHETYPE_TO_LOINC = {}
for loinc, info in LOINC_TO_ARCHETYPE.items():
    ARCHETYPE_TO_LOINC.setdefault(info["archetype"], []).append({
        "loinc_code": loinc, **info
    })


class FhirOpenEhrTransformer:

    @staticmethod
    def fhir_to_openehr(fhir_resource):
        """Transform a FHIR Observation to openEHR composition."""
        if fhir_resource.get("resourceType") != "Observation":
            return None

        loinc_code = None
        for coding in ((fhir_resource.get("code") or {}).get("coding") or []):
            if coding.get("system") == "http://loinc.org":
                loinc_code = coding.get("code")
                break

        mapping = LOINC_TO_ARCHETYPE.get(loinc_code) if loinc_code else None
        archetype_id = (mapping or {}).get("archetype", "openEHR-EHR-OBSERVATION.laboratory_test_result.v1")

        vq = fhir_resource.get("valueQuantity") or {}
        value = vq.get("value")
        unit = vq.get("unit") or (mapping or {}).get("unit", "")

        effective = fhir_resource.get("effectiveDateTime", datetime.now(timezone.utc).isoformat())
        concept_name = (fhir_resource.get("code") or {}).get("text", "")

        composition = {
            "archetype_id": archetype_id,
            "uid": str(uuid.uuid4()),
            "composition": {
                "_type": "COMPOSITION",
                "archetype_details": {
                    "archetype_id": {"value": "openEHR-EHR-COMPOSITION.report-result.v1"},
                    "template_id": {"value": "generic"},
                },
                "name": {"value": concept_name or archetype_id},
                "context": {
                    "start_time": {"value": effective},
                    "setting": {"value": "other care", "defining_code": {"code_string": "238"}},
                },
                "content": [{
                    "_type": "OBSERVATION",
                    "archetype_details": {"archetype_id": {"value": archetype_id}},
                    "name": {"value": concept_name or archetype_id},
                    "data": {
                        "events": [{
                            "time": {"value": effective},
                            "data": {
                                "items": [{
                                    "value": _build_dv_quantity(value, unit) if value is not None else {"_type": "DV_TEXT", "value": str(fhir_resource.get("valueString", ""))},
                                }],
                            },
                        }],
                    },
                }],
            },
        }

        return composition

    @staticmethod
    def openehr_to_fhir(openehr_comp, patient_guid):
        """Transform an openEHR composition to FHIR Observation."""
        archetype_id = openehr_comp.get("archetype_id", "")
        mappings = ARCHETYPE_TO_LOINC.get(archetype_id, [])
        loinc_code = mappings[0]["loinc_code"] if mappings else None
        loinc_display = mappings[0].get("metric", "") if mappings else ""

        # Extract value from composition structure
        value, unit = _extract_openehr_value(openehr_comp)
        effective = _extract_openehr_time(openehr_comp)

        obs = {
            "resourceType": "Observation",
            "id": str(uuid.uuid4()),
            "status": "final",
            "code": {"coding": [], "text": archetype_id},
            "subject": {"reference": f"Patient/{patient_guid}"},
            "effectiveDateTime": effective,
        }

        if loinc_code:
            obs["code"]["coding"].append({
                "system": "http://loinc.org",
                "code": loinc_code,
                "display": loinc_display,
            })

        if value is not None:
            try:
                obs["valueQuantity"] = {"value": float(value), "unit": unit or ""}
            except (TypeError, ValueError):
                obs["valueString"] = str(value)

        return obs


def _build_dv_quantity(value, unit):
    return {
        "_type": "DV_QUANTITY",
        "magnitude": float(value) if value is not None else 0,
        "units": unit or "",
    }


def _extract_openehr_value(comp):
    try:
        content = comp.get("composition", {}).get("content", [{}])[0]
        items = content.get("data", {}).get("events", [{}])[0].get("data", {}).get("items", [{}])
        dv = items[0].get("value", {})
        if dv.get("_type") == "DV_QUANTITY":
            return dv.get("magnitude"), dv.get("units")
        return dv.get("value"), None
    except (IndexError, KeyError, TypeError):
        return None, None


def _extract_openehr_time(comp):
    try:
        content = comp.get("composition", {}).get("content", [{}])[0]
        return content.get("data", {}).get("events", [{}])[0].get("time", {}).get("value")
    except (IndexError, KeyError, TypeError):
        return datetime.now(timezone.utc).isoformat()
