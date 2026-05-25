# Observation import template

Flat CSV format for ingesting FHIR Observation resources into a PDHC CDR.

## File: `observations_import.csv`

One row = one Observation. The first row is the header; every following row is a data row. Empty cells are allowed for optional columns.

### Columns

| Column | Required | Format / Notes |
|---|---|---|
| `patient_guid` | yes | The patient resource's `id` in the target CDR (UUID-shaped). Must already exist on that CDR — create the Patient first if not. |
| `effective_datetime` | yes | ISO-8601 with timezone, e.g. `2026-05-09T08:30:00Z`. This populates `Observation.effectiveDateTime`. |
| `code_system` | yes | Canonical URI of the terminology, e.g. `https://termbank.pdhc.se/CodeSystem/loinc`, `https://termbank.pdhc.se/CodeSystem/snomed`. Maps to `Observation.code.coding[0].system`. |
| `code` | yes | The code within that system, e.g. `4548-4` for HbA1c. Maps to `Observation.code.coding[0].code`. |
| `code_display` | recommended | Human-readable name of the code. If left blank, the CDR may resolve via termbank.pdhc; supplying it makes the record self-describing. |
| `value_quantity` | yes (for numeric) | Decimal value. Use `.` as decimal separator. For non-numeric Observations, leave blank and (TODO) extend the template with `value_string` / `value_code`. |
| `value_unit` | yes (when numeric) | UCUM-compatible unit, e.g. `mmol/L`, `kg`, `%`, `mm[Hg]`, `mL/min/{1.73_m2}`, `mmol/mol`. |
| `status` | yes | One of `final`, `preliminary`, `amended`, `corrected`, `cancelled`, `entered-in-error`, `unknown`. Default to `final` for completed measurements. |
| `category` | recommended | One of `laboratory`, `vital-signs`, `social-history`, `survey`, `imaging`, `procedure`, `exam`, `therapy`, `activity`. Maps to `Observation.category[0].coding[0].code` with system `http://terminology.hl7.org/CodeSystem/observation-category`. |
| `source_service` | recommended | Free-text identifier for the importing pipeline (e.g. `manual.pdhc`, `cambio.pdhc`, `sim.pdhc`). Stored on `Observation.meta.tag` and used by the CDR's audit trail. |
| `note` | optional | Free-text note. Maps to `Observation.note[0].text`. |

### Ingesting the CSV

The CDR exposes `POST /api/v1/ingest/batch` (max 100 rows per batch) and `POST /api/v1/ingest` (single). Each row is converted to a FHIR `Observation` resource with the shape:

```json
{
  "resourceType": "Observation",
  "status": "<status>",
  "category": [{
    "coding": [{
      "system": "http://terminology.hl7.org/CodeSystem/observation-category",
      "code": "<category>"
    }]
  }],
  "code": {
    "coding": [{
      "system": "<code_system>",
      "code": "<code>",
      "display": "<code_display>"
    }]
  },
  "subject": { "reference": "Patient/<patient_guid>" },
  "effectiveDateTime": "<effective_datetime>",
  "valueQuantity": {
    "value": <value_quantity>,
    "unit": "<value_unit>",
    "system": "http://unitsofmeasure.org",
    "code": "<value_unit>"
  },
  "note": [{ "text": "<note>" }]
}
```

A small converter script is in `convert_observations.py` (next to this README).

### Example rows

The file ships with **10 example rows** for a single patient (`015391ad-1e59-5e6d-a974-b19527662c70` from cdr2). They illustrate the most common PDHC measurement types: HbA1c, body weight, systolic/diastolic BP, BMI, eGFR, LDL, fasting glucose, CGM TIR (3.9–10), and CGM mean glucose. Replace these with your own data; keep the headers.

### Validation tips

Before ingesting:

1. Check every `patient_guid` exists on the target CDR (`GET /api/v1/fhir/Patient/<guid>` should return 200).
2. Validate every `code_system + code` pair resolves on termbank.pdhc (`GET https://termbank.pdhc.se/CodeSystem/$lookup?system=<code_system>&code=<code>`).
3. Verify `value_unit` against UCUM; the CDR will not auto-convert units.
