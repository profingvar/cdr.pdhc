"""FHIR ↔ openEHR transformer tests."""
from app.services.transformer import FhirOpenEhrTransformer, LOINC_TO_ARCHETYPE
from tests.conftest import SAMPLE_FHIR_OBSERVATION


def test_fhir_to_openehr_known_loinc(app):
    with app.app_context():
        result = FhirOpenEhrTransformer.fhir_to_openehr(SAMPLE_FHIR_OBSERVATION)
        assert result is not None
        assert result["archetype_id"] == "openEHR-EHR-OBSERVATION.body_weight.v2"
        assert result["uid"]
        comp = result["composition"]
        assert comp["_type"] == "COMPOSITION"
        items = comp["content"][0]["data"]["events"][0]["data"]["items"]
        assert items[0]["value"]["_type"] == "DV_QUANTITY"
        assert items[0]["value"]["magnitude"] == 85.2
        assert items[0]["value"]["units"] == "kg"


def test_fhir_to_openehr_unknown_loinc(app):
    with app.app_context():
        obs = {
            "resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": "99999-9"}]},
            "valueQuantity": {"value": 42, "unit": "ml"},
        }
        result = FhirOpenEhrTransformer.fhir_to_openehr(obs)
        assert result is not None
        assert result["archetype_id"] == "openEHR-EHR-OBSERVATION.laboratory_test_result.v1"


def test_fhir_to_openehr_not_observation(app):
    with app.app_context():
        result = FhirOpenEhrTransformer.fhir_to_openehr({"resourceType": "Patient"})
        assert result is None


def test_roundtrip(app):
    with app.app_context():
        openehr = FhirOpenEhrTransformer.fhir_to_openehr(SAMPLE_FHIR_OBSERVATION)
        fhir_back = FhirOpenEhrTransformer.openehr_to_fhir(openehr, "pat-roundtrip")
        assert fhir_back["resourceType"] == "Observation"
        assert fhir_back["code"]["coding"][0]["code"] == "29463-7"
        assert fhir_back["valueQuantity"]["value"] == 85.2


def test_loinc_mappings_complete():
    assert len(LOINC_TO_ARCHETYPE) >= 11
    for code, info in LOINC_TO_ARCHETYPE.items():
        assert "archetype" in info
        assert "metric" in info
        assert "unit" in info
