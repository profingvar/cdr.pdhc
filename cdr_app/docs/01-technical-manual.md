# cdr.pdhc — Technical Manual

## 1. Overview

cdr.pdhc is the canonical clinical data repository for the PDHC platform.
It receives normalised observations from gateway.pdhc (and later 2gate.pdhc),
stores them in a three-layer architecture (raw → standard → canonical), and
automatically delivers concept-mapped data to the Cambio CDR sandbox.

**Ports:** 9046 (Flask), 9047 (PostgreSQL), 9048–9049 reserved.

## 2. Three-Layer Storage

### Layer 1 — Raw Store
- Table: `ingest_raw`
- Immutable payload archive — every ingest request is stored verbatim
- SHA-256 hash computed for deduplication
- Columns: guid, source_service, patient_guid, payload_json, payload_hash, headers_json, received_at

### Layer 2 — Standard Store (dual-format)
- Table: `fhir_resources` — FHIR R5 Observation resources
- Table: `openehr_compositions` — openEHR Compositions
- Cross-linked via fhir_resource_guid ↔ openehr_comp_guid
- Bidirectional transformation: if only FHIR is provided, openEHR is generated; if only openEHR is provided, FHIR is generated

### Layer 3 — Canonical Store
- Table: `health_observations` — numeric health metrics (weight, BP, SpO2, etc.)
- Table: `activities` — activity-type observations (steps, exercise, etc.)
- Optimised for dashboard queries: patient_guid + metric + effective_at

## 3. FHIR ↔ openEHR Transformation

The transformer uses a LOINC-to-archetype mapping table (11 seed entries):

| LOINC    | Metric                   | openEHR Archetype                          | Unit    |
|----------|--------------------------|---------------------------------------------|---------|
| 29463-7  | body_weight_kg           | openEHR-EHR-OBSERVATION.body_weight.v2      | kg      |
| 85354-9  | blood_pressure_systolic  | openEHR-EHR-OBSERVATION.blood_pressure.v2   | mmHg    |
| 8867-4   | heart_rate_bpm           | openEHR-EHR-OBSERVATION.pulse.v2            | /min    |
| 8310-5   | body_temperature_c       | openEHR-EHR-OBSERVATION.body_temperature.v2 | Cel     |
| 2708-6   | spo2_percent             | openEHR-EHR-OBSERVATION.pulse_oximetry.v1   | %       |
| 8302-2   | body_height_cm           | openEHR-EHR-OBSERVATION.height.v2           | cm      |
| 9279-1   | respiratory_rate         | openEHR-EHR-OBSERVATION.respiration.v2      | /min    |
| 39156-5  | bmi                      | openEHR-EHR-OBSERVATION.body_mass_index.v2  | kg/m2   |
| 2339-0   | blood_glucose_mmol       | openEHR-EHR-OBSERVATION.laboratory_test_result.v1 | mmol/L |
| 8280-0   | waist_circumference_cm   | openEHR-EHR-OBSERVATION.waist_circumference.v2 | cm   |
| 93832-4  | sleep_hours              | openEHR-EHR-OBSERVATION.sleep.v0            | h       |

Unknown LOINC codes fall back to the generic `laboratory_test_result.v1` archetype.

## 4. Ingest Pipeline

The 8-step ingest pipeline processes each incoming observation:

1. **Deduplicate** — check payload SHA-256 hash against dedupe_registry
2. **Store raw** — immutable insert into ingest_raw
3. **Store/transform standard** — store provided FHIR/openEHR, generate missing format
4. **Store canonical** — insert into health_observations or activities
5. **Store context** — clinical provenance (transaction, careplan, plandef)
6. **Register dedupe** — add hash to dedupe_registry
7. **Enqueue Cambio** — create delivery_log entries if concept-mapped
8. **Audit** — write to audit_log with correlation ID and IP

## 5. Authentication

### Ingest (service-to-service)
Two headers required:
- `X-Service-Key`: shared secret per source service
- `X-Source-Service`: service identifier (`gateway.pdhc` or `2gate.pdhc`)

### Web UI (SSO)
- OAuth2 via sso.pdhc.se
- Admin-only, analysis phase required
- `AUTH_MODE=sso` enables SSO; `AUTH_MODE=off` uses dev SU user

## 6. Cambio CDR Sandbox Delivery

### Scope
Only observations with a `concept_guid` referencing the plan.pdhc.se concept store
are eligible for Cambio delivery. Data without concept mappings is stored locally only.

### Delivery process
1. Background worker (APScheduler, 60-second interval) picks up pending deliveries
2. Ensures patient exists in Cambio (FHIR Patient + openEHR EHR creation)
3. Delivers FHIR Observation via FHIR Gateway service
4. Delivers openEHR Composition via xCDR service
5. Retries with exponential backoff: 10s, 20s, 40s, 80s, 160s (max 5 attempts)

### OAuth2 token management
- Client credentials grant against Cambio IdP
- Tokens cached with 30-second safety margin before expiry
- Audiences: service.fhir-gateway, service.xcdr, service.patient, service.consent, service.organization

## 7. API Reference

### POST /api/v1/ingest
Single observation ingest. Returns 202 (accepted) or 200 (duplicate).

### POST /api/v1/ingest/batch
Batch ingest (max 100). Accepts `{"items": [...]}` or bare array.

### GET /api/v1/fhir/metadata
FHIR CapabilityStatement.

### GET /api/v1/fhir/Observation?patient_guid=X&loinc_code=Y
Search FHIR resources. Optional: limit, offset.

### GET /api/v1/fhir/Observation/<guid>
Read single FHIR resource.

### GET /api/v1/openehr/composition?patient_guid=X&archetype_id=Y
Search openEHR compositions.

### GET /api/v1/canonical/<table_name>?patient_guid=X&metric=Y
Query canonical tables (health_observations, activities).

### GET /api/v1/cambio/status
Delivery counts by status (pending, delivered, failed, skipped).

### GET /api/v1/cambio/patient/<guid>
Patient mapping + delivery history.

### POST /api/v1/cambio/retry
Reset all failed deliveries to pending.

## 8. Operations

### Cold start
```bash
cd /usr/local/www/cdr.pdhc
bash start.sh
```

### Graceful restart
```bash
cd /usr/local/www/cdr.pdhc
bash safe_restart.sh
```

### Environment variables
| Variable | Purpose |
|----------|---------|
| DATABASE_URL | PostgreSQL connection string |
| AUTH_MODE | `off` (dev) or `sso` (production) |
| SSO_BASE_URL | SSO service URL |
| SSO_CLIENT_ID / SSO_CLIENT_SECRET | SSO client credentials |
| GATEWAY_PDHC_SERVICE_KEY | Shared secret for gateway.pdhc |
| CAMBIO_DELIVERY_ENABLED | `true` to activate delivery worker |
| CAMBIO_CLIENT_ID / CAMBIO_CLIENT_SECRET | Cambio OAuth2 credentials |
| CAMBIO_BASE_URL | Cambio sandbox base URL |
| CAMBIO_TOKEN_URL | Cambio IdP token endpoint |

## 9. Database Schema

13 tables across PostgreSQL:

| Table | Layer | Description |
|-------|-------|-------------|
| ingest_raw | Raw | Immutable payload store |
| fhir_resources | Standard | FHIR R5 Observations |
| openehr_compositions | Standard | openEHR Compositions |
| health_observations | Canonical | Numeric health metrics |
| activities | Canonical | Activity observations |
| clinical_context | Provenance | Careplan/transaction links |
| dedupe_registry | Infra | SHA-256 dedup hashes |
| loinc_archetype_map | Infra | LOINC ↔ archetype mappings |
| service_keys | Auth | Gateway service keys |
| users | Auth | SSO-synced users |
| audit_log | Governance | Event audit trail |
| cambio_patient_map | Cambio | PDHC ↔ Cambio patient IDs |
| cambio_delivery_log | Cambio | Delivery tracking with retry |
