# cdr.pdhc — Deployment Plan

Clinical Data Repository for the PDHC platform.
Ports: 9046 (Flask), 9047 (PostgreSQL), 9048–9049 reserved.
App folder: `cdr_app/`
FHIR R5 + openEHR dual-standard. Dockerized. Receives data from `gateway.pdhc` and `2gate.pdhc`.

---

## Role in the PDHC ecosystem

cdr.pdhc is the **canonical clinical data repository** — the single authoritative store for patient health data regardless of ingestion pathway. It receives normalized observations from

- **gateway.pdhc** (port 9050) — workflow-aware ingestion with GUID chain resolution, PAT authentication, and vector embeddings. Data arrives with rich clinical context (careplan, plan definition, transaction semantics).

It stores every catch in a rich data format (FHIR R5 + openEHR dual-standard) and **automatically delivers** what is possible into the **Cambio CDR sandbox** (both FHIR resources via the FHIR Gateway and openEHR compositions via the xCDR service). It keeps track of what has been delivered to the sandbox and flags data to ensure no double reporting takes place. Later it will deliver data upon request, so both a FHIR API and an openEHR API exist as query endpoints.

**Delivery scope:** Only observations with canonical references to the local concept store (`plan.pdhc.se`) are eligible for Cambio delivery. Data without concept mappings is stored locally but not pushed upstream.

Later 2gateway will be added.

**Why a separate CDR?**

The gateway solve *ingestion* — validation, auth, deduplication, transformation. The CDR solves *persistence and query* — a single schema, unified provenance, FHIR+openEHR dual-format storage, and a standards-compliant query API. Keeping these concerns separate means:

1. Either gateway can evolve independently without breaking downstream consumers
2. New ingestion sources can be added without modifying the CDR
3. Query consumers (clinical apps, analytics, AI) have one stable API
4. GDPR erasure and consent enforcement happen in one place

**Data flow:**

```
gateway.pdhc                          2gate.pdhc
(workflow-aware,                      (standalone,
 PAT auth, vectors)                    API-key auth, FHIR+openEHR)
       │                                     │
       │  POST /api/v1/ingest                │  POST /api/v1/ingest
       │  X-Source-Service: gateway.pdhc     │  X-Source-Service: 2gate.pdhc
       │  X-Service-Key: <shared secret>     │  X-Service-Key: <shared secret>
       │                                     │
       └──────────────┬──────────────────────┘
                      │
                      ▼
                 cdr.pdhc (port 9046)
                      │
    ┌─────────────────┼─────────────────┐
    ▼                 ▼                 ▼
 Raw Store      Standard Store     Canonical Store
 (ingest_raw)   (fhir_resources    (health_observations,
                 openehr_comps)     activities, meals, …)
                      │
          ┌───────────┼───────────┐
          ▼                       ▼
   Query API (port 9046)    Cambio CDR sandbox
   GET /fhir/Observation    (async delivery)
   GET /openehr/composition ├─ FHIR → service.fhir-gateway
   GET /canonical/<table>   └─ openEHR → service.xcdr
```

**Access control:** The CDR does not face external systems directly. Only the two gateways and authorized internal services (via service keys) can write. Read access is via JWT (vårdgivare, admin) or service key (internal consumers). Patient-facing access is not in scope.

---

## Phase 1 — Foundation

### 1.a Project scaffold
- Create `cdr_app/` with Flask app structure
- Create venv inside `cdr_app/venv/`
- Create `requirements.txt` (Flask, Flask-SQLAlchemy, Flask-Migrate, psycopg2-binary, pgvector, pytest)
- Create `CLAUDE.md` referencing `../css_instrux/repo_css.md`
- Copy `pdhc.css` into `cdr_app/static/css/`

### 1.b Docker and database setup
- `Dockerfile` for Flask app
- `docker-compose.yml` with PostgreSQL (port 9047) and Flask (port 9046)
- `.env` file with DB credentials, Flask secret, service keys, bootstrap SU API key
- PostgreSQL with **pgvector** extension enabled
- Flask-Migrate / Alembic for schema migrations

### 1.c start.sh
- Kill processes on ports 9046–9049
- Activate venv
- Start Docker (PostgreSQL) if not running
- Start Flask app
- Ctrl+C graceful shutdown and deactivate

### 1.d safe_restart.sh
- For web instance restarts per Rule 19

---

## Phase 2 — Database schema (three-layer storage)

The CDR stores every piece of data in three layers. Each layer serves a different consumer need.

### 2.a Layer 1 — Raw Store (`ingest_raw`)

Immutable record of exactly what arrived. Never modified after write.

```
ingest_raw
├── guid                  VARCHAR(36) PK, UUID4
├── source_service        VARCHAR(64)    -- 'gateway.pdhc' | '2gate.pdhc'
├── source_system_id      VARCHAR(64)    -- upstream system identifier
├── patient_guid          VARCHAR(36)    -- patient GUID (pseudonymized)
├── payload_json          JSONB          -- exact payload as received
├── payload_hash          VARCHAR(64)    -- SHA-256 of payload_json
├── headers_json          JSONB          -- relevant headers snapshot
├── source_type           VARCHAR(32)    -- 'fhir' | 'openehr'
├── received_at           TIMESTAMPTZ    -- when CDR received it
├── created_at            TIMESTAMPTZ    -- default now()
```

### 2.b Layer 2 — Standard Store (dual-format)

Every observation is stored in **both** FHIR R5 and openEHR format. If only one format arrives, the CDR generates the other via transformation.

**FHIR resources:**

```
fhir_resources
├── guid                  VARCHAR(36) PK, UUID4
├── ingest_raw_guid       VARCHAR(36)    -- FK → ingest_raw.guid
├── patient_guid          VARCHAR(36)
├── resource_type         VARCHAR(64)    -- 'Observation', 'NutritionIntake', 'Procedure', 'CarePlan', 'QuestionnaireResponse'
├── resource_json         JSONB          -- complete FHIR R5 resource
├── loinc_code            VARCHAR(16)    -- extracted primary LOINC code (nullable)
├── status                VARCHAR(32)    -- 'active' | 'final' | 'amended' | 'cancelled'
├── effective_at          TIMESTAMPTZ    -- observation effective time
├── source_service        VARCHAR(64)
├── created_at            TIMESTAMPTZ
```

Index: `(patient_guid, resource_type, effective_at DESC)`

**openEHR compositions:**

```
openehr_compositions
├── guid                  VARCHAR(36) PK, UUID4
├── ingest_raw_guid       VARCHAR(36)    -- FK → ingest_raw.guid
├── fhir_resource_guid    VARCHAR(36)    -- FK → fhir_resources.guid (nullable, links the pair)
├── patient_guid          VARCHAR(36)
├── archetype_id          VARCHAR(128)   -- e.g. 'openEHR-EHR-OBSERVATION.body_weight.v2'
├── template_id           VARCHAR(128)   -- default 'generic'
├── composition_json      JSONB          -- complete openEHR composition
├── effective_at          TIMESTAMPTZ
├── source_service        VARCHAR(64)
├── created_at            TIMESTAMPTZ
```

Index: `(patient_guid, archetype_id, effective_at DESC)`

### 2.c Layer 3 — Canonical Store (query-optimized)

Flat, strongly-typed tables for fast querying. Derived from Layer 2. All reference `ingest_raw_guid` for full traceability.

```
health_observations
├── guid                  VARCHAR(36) PK
├── ingest_raw_guid       VARCHAR(36)    -- FK → ingest_raw.guid
├── patient_guid          VARCHAR(36)
├── metric                VARCHAR(64)    -- 'body_weight_kg', 'heart_rate_bpm', 'blood_pressure_systolic', etc.
├── value                 NUMERIC(12,4)
├── unit                  VARCHAR(32)    -- 'kg', 'bpm', 'mmHg', 'Cel', '%', 'cm', 'kg/m2', 'mmol/L'
├── source_type           VARCHAR(16)    -- 'fhir' | 'openehr'
├── source_code           VARCHAR(32)    -- LOINC code or archetype node ID
├── source_service        VARCHAR(64)    -- 'gateway.pdhc' | '2gate.pdhc'
├── effective_at          TIMESTAMPTZ
├── created_at            TIMESTAMPTZ

activities
├── guid                  VARCHAR(36) PK
├── ingest_raw_guid       VARCHAR(36)
├── patient_guid          VARCHAR(36)
├── activity_type         VARCHAR(64)    -- 'steps', 'distance', 'active_calories', 'exercise'
├── value                 NUMERIC(12,4)
├── unit                  VARCHAR(32)
├── source_type           VARCHAR(16)
├── source_code           VARCHAR(32)
├── source_service        VARCHAR(64)
├── effective_at          TIMESTAMPTZ
├── created_at            TIMESTAMPTZ

meals
├── guid                  VARCHAR(36) PK
├── ingest_raw_guid       VARCHAR(36)
├── patient_guid          VARCHAR(36)
├── meal_type             VARCHAR(32)    -- 'breakfast', 'lunch', 'dinner', 'snack'
├── energy_kcal           NUMERIC(10,2)
├── protein_g             NUMERIC(10,2)
├── carbs_g               NUMERIC(10,2)
├── fat_g                 NUMERIC(10,2)
├── items_json            JSONB          -- ingredient list
├── source_service        VARCHAR(64)
├── effective_at          TIMESTAMPTZ
├── created_at            TIMESTAMPTZ

daily_readiness
├── guid                  VARCHAR(36) PK
├── ingest_raw_guid       VARCHAR(36)
├── patient_guid          VARCHAR(36)
├── sleep_score           NUMERIC(5,2)
├── energy_score          NUMERIC(5,2)
├── mental_score          NUMERIC(5,2)
├── calculated_readiness  NUMERIC(5,2)
├── source_service        VARCHAR(64)
├── effective_at          TIMESTAMPTZ
├── created_at            TIMESTAMPTZ

session_completions
├── guid                  VARCHAR(36) PK
├── ingest_raw_guid       VARCHAR(36)
├── patient_guid          VARCHAR(36)
├── session_type          VARCHAR(64)
├── status                VARCHAR(32)    -- 'completed', 'partial', 'cancelled'
├── started_at            TIMESTAMPTZ
├── ended_at              TIMESTAMPTZ
├── duration_minutes      INTEGER
├── source_service        VARCHAR(64)
├── created_at            TIMESTAMPTZ

session_exercise_logs
├── guid                  VARCHAR(36) PK
├── ingest_raw_guid       VARCHAR(36)
├── patient_guid          VARCHAR(36)
├── session_guid          VARCHAR(36)    -- FK → session_completions.guid
├── exercise_name         VARCHAR(128)
├── sets                  INTEGER
├── reps                  INTEGER
├── weight_kg             NUMERIC(8,2)
├── duration_seconds      INTEGER
├── created_at            TIMESTAMPTZ

user_program_run
├── guid                  VARCHAR(36) PK
├── ingest_raw_guid       VARCHAR(36)
├── patient_guid          VARCHAR(36)
├── program_name          VARCHAR(128)
├── status                VARCHAR(32)    -- 'active', 'completed', 'paused', 'cancelled'
├── started_at            TIMESTAMPTZ
├── ended_at              TIMESTAMPTZ
├── source_service        VARCHAR(64)
├── created_at            TIMESTAMPTZ
```

### 2.d Provenance and context store

Links observations to clinical workflow context when available (from gateway.pdhc).

```
clinical_context
├── guid                  VARCHAR(36) PK
├── ingest_raw_guid       VARCHAR(36)    -- FK → ingest_raw.guid
├── patient_guid          VARCHAR(36)
├── transaction_guid      VARCHAR(36)    -- nullable
├── careplan_guid         VARCHAR(36)    -- nullable
├── plandef_guid          VARCHAR(36)    -- nullable
├── resolved_context_json JSONB          -- full resolved GUID chain (from gateway.pdhc)
├── source_service        VARCHAR(64)    -- always 'gateway.pdhc' for context-rich records
├── created_at            TIMESTAMPTZ
```

Records from 2gate.pdhc will have NULL context GUIDs — the observation stands alone.
Records from gateway.pdhc will have the full chain populated.

### 2.e Vector store (experimental, pgvector)

```
observation_vectors
├── guid                  VARCHAR(36) PK
├── ingest_raw_guid       VARCHAR(36)    -- FK → ingest_raw.guid
├── patient_guid          VARCHAR(36)
├── clinical_context_guid VARCHAR(36)    -- FK → clinical_context.guid (nullable)
├── embedding             VECTOR(384)    -- pgvector; dimension configurable
├── embedding_model       VARCHAR(64)    -- model used to generate
├── embedding_input_json  JSONB          -- text/structure that was embedded
├── created_at            TIMESTAMPTZ
```

Note: vectors are only generated when clinical context is present (gateway.pdhc pathway) or when standalone observations have sufficient metadata for meaningful embedding. Design is experimental per gateway.pdhc convention.

### 2.f Deduplication registry

```
dedupe_registry
├── guid                  VARCHAR(36) PK
├── payload_hash          VARCHAR(64)    -- SHA-256, unique
├── source_service        VARCHAR(64)
├── patient_guid          VARCHAR(36)
├── first_seen_at         TIMESTAMPTZ
├── last_seen_at          TIMESTAMPTZ
├── hit_count             INTEGER        -- default 1
```

Unique constraint: `(payload_hash, source_service)`

### 2.g Audit and governance

```
audit_log
├── guid                  VARCHAR(36) PK
├── event_type            VARCHAR(64)    -- 'ingest.accepted', 'ingest.duplicate', 'ingest.rejected',
│                                        --  'query.fhir', 'query.openehr', 'query.canonical',
│                                        --  'gdpr.erasure', 'gdpr.export', 'consent.granted', 'consent.revoked'
├── actor_guid            VARCHAR(36)    -- service or user performing the action
├── data_subject_guid     VARCHAR(36)    -- patient GUID (GDPR requirement)
├── source_service        VARCHAR(64)
├── correlation_id        VARCHAR(64)    -- trace across service boundaries
├── payload_snapshot      JSONB
├── ip_address            VARCHAR(45)
├── created_at            TIMESTAMPTZ

consent_records
├── guid                  VARCHAR(36) PK
├── patient_guid          VARCHAR(36)
├── system_id             VARCHAR(64)    -- which system/service has consent
├── purpose               VARCHAR(128)
├── legal_basis           VARCHAR(64)    -- 'consent', 'legitimate_interest', 'vital_interest', 'legal_obligation'
├── granted_at            TIMESTAMPTZ
├── revoked_at            TIMESTAMPTZ    -- nullable; set when revoked
├── created_at            TIMESTAMPTZ

gdpr_settings
├── key                   VARCHAR(64) PK
├── value                 JSONB
├── updated_at            TIMESTAMPTZ
```

### 2.h LOINC-to-archetype mapping table

Authoritative bidirectional mapping used for FHIR↔openEHR transformation within the CDR.

```
loinc_archetype_map
├── guid                  VARCHAR(36) PK
├── loinc_code            VARCHAR(16)    -- e.g. '29463-7'
├── loinc_display         VARCHAR(128)   -- e.g. 'Body weight'
├── archetype_id          VARCHAR(128)   -- e.g. 'openEHR-EHR-OBSERVATION.body_weight.v2'
├── archetype_node_id     VARCHAR(32)    -- e.g. 'at0004' (for BP systolic/diastolic)
├── canonical_metric      VARCHAR(64)    -- e.g. 'body_weight_kg'
├── canonical_unit        VARCHAR(32)    -- e.g. 'kg'
├── canonical_table       VARCHAR(64)    -- e.g. 'health_observations'
├── active                BOOLEAN        -- default true
├── created_at            TIMESTAMPTZ
├── updated_at            TIMESTAMPTZ
```

Seed data (14 mappings):

| LOINC | Archetype | Metric | Unit | Table |
|-------|-----------|--------|------|-------|
| 29463-7 | OBSERVATION.body_weight.v2 | body_weight_kg | kg | health_observations |
| 85354-9 | OBSERVATION.blood_pressure.v2 | blood_pressure_systolic | mmHg | health_observations |
| 85354-9 | OBSERVATION.blood_pressure.v2 | blood_pressure_diastolic | mmHg | health_observations |
| 8867-4 | OBSERVATION.pulse.v2 | heart_rate_bpm | /min | health_observations |
| 8310-5 | OBSERVATION.body_temperature.v2 | body_temperature_c | Cel | health_observations |
| 2708-6 | OBSERVATION.pulse_oximetry.v1 | spo2_percent | % | health_observations |
| 8302-2 | OBSERVATION.height.v2 | body_height_cm | cm | health_observations |
| 9279-1 | OBSERVATION.respiration.v2 | respiratory_rate | /min | health_observations |
| 39156-5 | OBSERVATION.body_mass_index.v2 | bmi | kg/m2 | health_observations |
| 2339-0 | OBSERVATION.laboratory_test_result.v1 | blood_glucose_mmol | mmol/L | health_observations |
| 8280-0 | OBSERVATION.waist_circumference.v2 | waist_circumference_cm | cm | health_observations |
| 93832-4 | OBSERVATION.sleep.v0 | sleep_hours | h | health_observations |
| 55423-8 | — | steps_count | {count} | activities |
| 55411-3 | — | distance_km | km | activities |

---

## Phase 3 — Ingest API (receiving from gateways)

### 3.a Service key authentication

Both gateways authenticate to the CDR via shared service keys (not PATs, not user JWTs).

```
Header: X-Service-Key: <shared-secret>
Header: X-Source-Service: gateway.pdhc | 2gate.pdhc
```

- Service keys stored as bcrypt hashes in `service_keys` table
- One key per source service, rotatable
- Rate limit: 500 req/60s per service key

```
service_keys
├── guid                  VARCHAR(36) PK
├── service_name          VARCHAR(64)    -- 'gateway.pdhc', '2gate.pdhc'
├── key_hash              VARCHAR(128)   -- bcrypt
├── active                BOOLEAN
├── created_at            TIMESTAMPTZ
├── expires_at            TIMESTAMPTZ    -- nullable
```

### 3.b Unified ingest endpoint

`POST /api/v1/ingest`

Accepts a normalized payload envelope from either gateway:

```json
{
  "patient_guid": "abc-123-def",
  "source_type": "fhir",
  "source_system_id": "garmin-connect-01",

  "fhir_resource": { "resourceType": "Observation", ... },
  "openehr_composition": null,

  "canonical": {
    "table": "health_observations",
    "metric": "body_weight_kg",
    "value": 78.5,
    "unit": "kg",
    "effective_at": "2026-03-30T08:15:00Z"
  },

  "clinical_context": {
    "transaction_guid": "tx-001",
    "careplan_guid": "cp-001",
    "plandef_guid": "pd-001",
    "resolved_context_json": { ... }
  }
}
```

Fields:
- `patient_guid` — required
- `source_type` — `fhir` | `openehr`
- `fhir_resource` — present if source is FHIR or if gateway already transformed
- `openehr_composition` — present if source is openEHR or if gateway already transformed
- `canonical` — pre-normalized values (the gateway has already done extraction)
- `clinical_context` — only from gateway.pdhc; null from 2gate.pdhc

### 3.c Ingest processing pipeline

On each accepted payload:

1. **Deduplicate** — SHA-256 hash of `payload_json` checked against `dedupe_registry`
2. **Store raw** — write to `ingest_raw` (immutable)
3. **Transform missing format** — if only FHIR arrived, generate openEHR composition via `loinc_archetype_map`; if only openEHR arrived, generate FHIR resource
4. **Store standard** — write to `fhir_resources` and `openehr_compositions`, cross-linked
5. **Store canonical** — write to the appropriate canonical table
6. **Store context** — if `clinical_context` present, write to `clinical_context`
7. **Vectorize** (async, optional) — if context-rich, generate embedding → `observation_vectors`
8. **Audit** — write to `audit_log`
9. **Return** — `202 Accepted` with `{ "ingest_raw_guid": "...", "status": "accepted" }`

### 3.d Batch ingest

`POST /api/v1/ingest/batch`

Accepts array of payloads (max 100). Returns per-entry status:

```json
{
  "accepted": 98,
  "duplicate": 1,
  "rejected": 1,
  "entries": [
    { "index": 0, "status": "accepted", "ingest_raw_guid": "..." },
    { "index": 47, "status": "duplicate", "dedupe_hash": "..." },
    { "index": 99, "status": "rejected", "errors": ["missing patient_guid"] }
  ]
}
```

### 3.e FHIR↔openEHR transformation service

Internal service that generates the missing standard format:

**FHIR → openEHR:**
1. Extract LOINC code from `Observation.code.coding`
2. Look up `loinc_archetype_map` for target archetype
3. Build openEHR composition structure: `{ archetype_id, composition: { context, content: [{ data: { events: [{ data: { items: [{ value: { magnitude, units } }] } }] } }] } }`
4. Handle special cases: blood pressure (two items per composition), panels

**openEHR → FHIR:**
1. Extract archetype_id
2. Look up `loinc_archetype_map` for target LOINC code
3. Build FHIR Observation: `{ resourceType: "Observation", code: { coding: [{ system: "http://loinc.org", code, display }] }, valueQuantity: { value, unit, system, code } }`
4. Set `status: "final"`, `effectiveDateTime`

Unmapped types: store in raw and standard layers only (whichever format arrived), skip canonical. Log warning.

---

## Phase 3f — Cambio CDR sandbox delivery

Automatic delivery of stored data to the **Cambio Platform Innovation** sandbox instance. Both FHIR resources and openEHR compositions are pushed. Only observations with canonical references to `plan.pdhc.se` concepts are eligible.

**Cambio sandbox endpoints (from Cambiosandbox.pdf):**

| Service | Purpose |
|---------|---------|
| `service.xcdr` | openEHR composition CRUD |
| `service.fhir-gateway` | FHIR resource CRUD |
| `service.patient` | Patient registration |

**Auth:** OAuth2 client credentials flow via Cambio IdP (Keycloak).

```
Token URL:  https://idp.innovation.platform.cambio.se/auth/realms/cambio-platform/protocol/openid-connect/token
Base URL:   https://sandbox-dev.innovation.platform.cambio.se
Tenant:     sandbox-dev
```

Credentials stored in `.env` (never committed):
```bash
CAMBIO_CLIENT_ID=persona.ki.01.sandbox-dev
CAMBIO_CLIENT_SECRET=<from Cambiosandbox.pdf>
CAMBIO_TOKEN_URL=https://idp.innovation.platform.cambio.se/auth/realms/cambio-platform/protocol/openid-connect/token
CAMBIO_BASE_URL=https://sandbox-dev.innovation.platform.cambio.se
CAMBIO_TENANT=sandbox-dev
CAMBIO_COMMISSION_HSA_ID=SE0000000006-CO0001
CAMBIO_HEALTHCARE_UNIT_HSA_ID=SE0000000006-CU0001
CAMBIO_HEALTHCARE_PROVIDER_HSA_ID=SE0000000006-CP0001
CAMBIO_ORG_ID=0000000006
CAMBIO_DELIVERY_ENABLED=true
```

### 3f.a Patient identity mapping

PDHC uses pseudonymized patient GUIDs. Cambio has its own patient registry. The CDR auto-creates patients in Cambio on first encounter and retains the mapping.

```
cambio_patient_map
├── guid                  VARCHAR(36) PK, UUID4
├── pdhc_patient_guid     VARCHAR(36)    -- PDHC patient GUID (unique)
├── cambio_patient_id     VARCHAR(128)   -- Cambio-side patient ID
├── cambio_ehr_id         VARCHAR(128)   -- openEHR EHR ID in Cambio (nullable)
├── created_at            TIMESTAMPTZ
├── updated_at            TIMESTAMPTZ
```

Unique constraint: `(pdhc_patient_guid)`

**Flow on first encounter:**
1. Create FHIR Patient via `POST {base}/patient/fhir/Patient` with pseudonymized identifier
2. Store Cambio patient ID in `cambio_patient_map`
3. Query openEHR EHR by subject: `GET {base}/xcdr/rest/openehr/v1/ehr?subject_id={cambio_patient_id}`
4. If EHR exists → store `cambio_ehr_id`. If not → attempt `POST {base}/xcdr/rest/openehr/v1/ehr` with subject. If create fails (scope limitation) → log warning, skip openEHR delivery for this patient, FHIR delivery still works.

### 3f.b Delivery tracking

```
cambio_delivery_log
├── guid                  VARCHAR(36) PK, UUID4
├── ingest_raw_guid       VARCHAR(36)    -- FK → ingest_raw.guid
├── fhir_resource_guid    VARCHAR(36)    -- FK → fhir_resources.guid (nullable)
├── openehr_comp_guid     VARCHAR(36)    -- FK → openehr_compositions.guid (nullable)
├── patient_guid          VARCHAR(36)
├── delivery_type         VARCHAR(16)    -- 'fhir' | 'openehr'
├── cambio_resource_id    VARCHAR(128)   -- ID returned by Cambio on success (nullable)
├── status                VARCHAR(32)    -- 'pending' | 'delivered' | 'failed' | 'skipped'
├── attempt_count         INTEGER        -- default 0
├── last_attempt_at       TIMESTAMPTZ    -- nullable
├── last_error            TEXT           -- nullable, last failure message
├── delivered_at          TIMESTAMPTZ    -- nullable, set on success
├── created_at            TIMESTAMPTZ
```

Unique constraint: `(ingest_raw_guid, delivery_type)` — prevents double-delivery per format.

### 3f.c Delivery pipeline

Delivery runs **asynchronously** after ingest completes. Non-blocking — ingest returns `202 Accepted` immediately.

**Pipeline:**
1. After successful ingest (Phase 3.c step 8), enqueue delivery task
2. Background worker picks up pending deliveries from `cambio_delivery_log`
3. For each pending delivery:
   a. Resolve or create Cambio patient (via `cambio_patient_map`)
   b. **FHIR delivery:** `POST {base}/fhir-gateway/fhir/Observation` with Bearer token
   c. **openEHR delivery:** If `cambio_ehr_id` exists, `POST {base}/xcdr/rest/openehr/v1/ehr/{ehr_id}/composition` with the openEHR composition
   d. On success → set `status='delivered'`, store `cambio_resource_id`
   e. On failure → increment `attempt_count`, set `last_error`, schedule retry
4. Retry policy: exponential backoff (5s, 15s, 45s, 135s, max 10 min). Max 10 attempts → `status='failed'`, alert.

**Eligibility filter:**
- Only observations where `fhir_resources.loinc_code IS NOT NULL` OR concept_guid resolves to a plan.pdhc.se concept are delivered
- Observations without concept mappings → `status='skipped'`

### 3f.d Token management

OAuth2 access tokens are cached and refreshed before expiry.

```python
class CambioTokenManager:
    """Cache Cambio OAuth2 token, refresh 60s before expiry."""
    def get_token(self) -> str: ...
    def _refresh(self) -> None: ...
```

### 3f.e Cambio API client

```python
class CambioClient:
    """Client for Cambio Platform Innovation sandbox."""
    
    def create_patient(self, pdhc_patient_guid: str) -> str:
        """Create FHIR Patient in Cambio. Returns Cambio patient ID."""
    
    def get_or_create_ehr(self, cambio_patient_id: str) -> str | None:
        """Query openEHR EHR by subject. Create if missing (may fail). Returns EHR ID or None."""
    
    def deliver_fhir_observation(self, resource_json: dict, token: str) -> str:
        """POST FHIR Observation to Cambio. Returns Cambio resource ID."""
    
    def deliver_openehr_composition(self, ehr_id: str, composition_json: dict, token: str) -> str:
        """POST openEHR composition to Cambio. Returns composition UID."""
```

### 3f.f Delivery status endpoint

```
GET /api/v1/cambio/status
```

Returns delivery statistics: pending, delivered, failed, skipped counts. Admin JWT required.

```
GET /api/v1/cambio/patient/<pdhc_patient_guid>
```

Returns Cambio patient mapping and delivery history for that patient.

---

## Phase 4 — Query API (FHIR-compliant read)

### 4.a FHIR R5 read endpoints

Standard FHIR search interface, read-only.

```
GET /api/v1/fhir/Observation?patient=<guid>
GET /api/v1/fhir/Observation?patient=<guid>&code=<loinc>
GET /api/v1/fhir/Observation?patient=<guid>&date=ge2026-01-01&date=le2026-03-30
GET /api/v1/fhir/Observation/<guid>
GET /api/v1/fhir/metadata                        → CapabilityStatement
```

Returns FHIR R5 Bundle (searchset) with proper `Content-Type: application/fhir+json`.

Auth: JWT Bearer (vårdgivare, admin) or X-Service-Key (internal).

### 4.b openEHR AQL-lite query endpoint

Simplified openEHR query (not full AQL, but archetype-filtered):

```
GET /api/v1/openehr/composition?patient_guid=<guid>
GET /api/v1/openehr/composition?patient_guid=<guid>&archetype_id=<id>
GET /api/v1/openehr/composition/<guid>
GET /api/v1/openehr/ehr/<patient_guid>/compositions      → all compositions for patient
```

Returns openEHR composition JSON.

### 4.c Canonical query endpoints

Direct access to the flat canonical tables:

```
GET /api/v1/canonical/health_observations?patient_guid=<guid>&metric=<metric>
GET /api/v1/canonical/activities?patient_guid=<guid>&since=<iso-date>
GET /api/v1/canonical/meals?patient_guid=<guid>
GET /api/v1/canonical/daily_readiness?patient_guid=<guid>
GET /api/v1/canonical/sessions?patient_guid=<guid>
GET /api/v1/canonical/programs?patient_guid=<guid>
```

All support: `limit` (default 100, max 500), `offset`, `since`, `until`, `sort` (asc/desc).

### 4.d Provenance query

```
GET /api/v1/provenance/<ingest_raw_guid>
```

Returns the full trace: raw payload → FHIR resource → openEHR composition → canonical record → clinical context (if any).

### 4.e Vector similarity search (experimental)

```
GET /api/v1/vectors/similar?patient_guid=<guid>&text=<query>&limit=10
GET /api/v1/vectors/by-patient/<patient_guid>
GET /api/v1/vectors/by-careplan/<careplan_guid>
```

---

## Phase 5 — GDPR compliance

### 5.a Patient erasure (Right to erasure, Art. 17)

`POST /api/v1/gdpr/erase`

```json
{ "patient_guid": "abc-123-def", "reason": "patient request" }
```

Deletes across ALL tables in correct FK order:
1. `observation_vectors`
2. `clinical_context`
3. `session_exercise_logs`
4. `session_completions`
5. `user_program_run`
6. `daily_readiness`
7. `meals`
8. `activities`
9. `health_observations`
10. `openehr_compositions`
11. `fhir_resources`
12. `dedupe_registry`
13. `ingest_raw`
14. `consent_records`

Audit log entry preserved (with `data_subject_guid` but no payload) per Art. 30.

Auth: admin only (JWT).

### 5.b Patient data export (Right to data portability, Art. 20)

`GET /api/v1/gdpr/export/<patient_guid>`

Returns complete patient record as JSON:
- All FHIR resources
- All openEHR compositions
- All canonical records
- Consent history
- Audit log (filtered to this patient)

Auth: admin only.

### 5.c Retention policy

Configurable via `gdpr_settings`:
- `retention_days` — observations older than this are eligible for cleanup
- Cleanup runs via cron (`POST /api/v1/gdpr/cleanup` with `X-Cron-Secret`)
- Cascades through all three storage layers

### 5.d Consent tracking

Consent records synced from upstream gateways. CDR enforces:
- No query results returned for revoked consent
- Erasure available regardless of consent status (Art. 17 overrides)

---

## Phase 6 — Gateway integration adapters

### 6.a gateway.pdhc adapter

A service in gateway.pdhc that, after storing an observation locally, POSTs to `cdr.pdhc /api/v1/ingest` with:
- The FHIR observation
- The resolved clinical context (transaction → careplan → plan definition chain)
- Pre-extracted canonical values
- `X-Source-Service: gateway.pdhc`

Implementation: add a `cdr_forwarder` service to gateway.pdhc that fires after successful ingestion. Fire-and-forget with retry queue on failure.

### 6.b 2gate.pdhc adapter

A Supabase Database Webhook or Edge Function trigger in 2gate.pdhc that, on new `observations` insert, POSTs to `cdr.pdhc /api/v1/ingest` with:
- The FHIR resource (from `observations.fhir_resource`)
- The openEHR composition (from `openehr_compositions` if exists)
- Pre-extracted canonical values (from whichever canonical table was written)
- `X-Source-Service: 2gate.pdhc`

Implementation: new edge function `cdr-forwarder` in 2gate.pdhc triggered by database webhook.

### 6.c Resilience

Both adapters must handle CDR downtime:
- Queue failed deliveries locally
- Retry with exponential backoff (1s, 2s, 4s, 8s, max 60s)
- Dead-letter after 10 retries → alert
- CDR deduplication ensures replayed deliveries are safe

---

## Phase 7 — Frontend (CDR admin dashboard)

### 7.a Dashboard

`GET /` — overview page showing:
- Total observations by source service (gateway.pdhc vs 2gate.pdhc)
- Observations ingested today / this week / this month
- Storage layer counts (raw, FHIR, openEHR, canonical)
- Deduplication hit rate
- Latest 10 ingestion events

### 7.b Patient data viewer

`GET /patients/<patient_guid>` — per-patient view:
- Health observations timeline (chart)
- All FHIR resources (expandable JSON)
- All openEHR compositions (expandable JSON)
- Canonical data tabs (observations, activities, meals, readiness, sessions, programs)
- Clinical context chain (if available from gateway.pdhc)
- Provenance trail

### 7.c Mapping manager

`GET /mappings` — view and edit `loinc_archetype_map`:
- Table view of all mappings
- Add / edit / deactivate mappings
- Test transformation: paste FHIR → see generated openEHR (and vice versa)

### 7.d GDPR tools

`GET /gdpr` — admin page:
- Patient search + erasure trigger
- Data export trigger
- Retention settings
- Consent overview
- Audit log viewer

### 7.e System status

`GET /status` — operational view:
- Service key status (active, expiry)
- Ingestion rate by source
- Error rate
- Queue depth (pending retries from gateways)

---

## Phase 8 — FHIR CapabilityStatement

### 8.a CapabilityStatement

`GET /api/v1/fhir/metadata`

```json
{
  "resourceType": "CapabilityStatement",
  "id": "cdr-pdhc",
  "status": "active",
  "kind": "instance",
  "fhirVersion": "5.0.0",
  "format": ["json"],
  "rest": [{
    "mode": "server",
    "resource": [
      {
        "type": "Observation",
        "interaction": [
          { "code": "read" },
          { "code": "search-type" }
        ],
        "searchParam": [
          { "name": "patient", "type": "reference" },
          { "name": "code", "type": "token" },
          { "name": "date", "type": "date" },
          { "name": "_count", "type": "number" }
        ]
      },
      {
        "type": "NutritionIntake",
        "interaction": [{ "code": "read" }, { "code": "search-type" }],
        "searchParam": [{ "name": "patient", "type": "reference" }]
      },
      {
        "type": "Procedure",
        "interaction": [{ "code": "read" }, { "code": "search-type" }],
        "searchParam": [{ "name": "patient", "type": "reference" }]
      },
      {
        "type": "CarePlan",
        "interaction": [{ "code": "read" }, { "code": "search-type" }],
        "searchParam": [{ "name": "patient", "type": "reference" }]
      }
    ]
  }]
}
```

---

## Phase 9 — Testing and integration

### 9.a Unit tests (pytest)
- Ingest pipeline (raw → standard → canonical)
- FHIR→openEHR transformation (all 14 mappings)
- openEHR→FHIR transformation (all mapped archetypes)
- Deduplication (identical payload, different payload)
- GDPR erasure (verify all tables cleaned)
- Auth (valid key, invalid key, expired key, missing key)
- Query endpoints (FHIR search, openEHR filter, canonical filter)
- Results in `./results/<timestamp>_results/`

### 9.b Integration tests
- gateway.pdhc → cdr.pdhc: submit observation with clinical context → verify all three layers + context stored
- 2gate.pdhc → cdr.pdhc: submit FHIR observation → verify openEHR generated, canonical stored
- 2gate.pdhc → cdr.pdhc: submit openEHR composition → verify FHIR generated, canonical stored
- Both gateways submit same patient → verify unified patient view
- GDPR erasure → verify clean across all layers

### 9.c Full endpoint test script (Rules 9, 20)
- Script testing all API endpoints per capability statement

---

## Phase 10 — Deployment preparation

### 10.a Documentation
- API contract documentation
- Auth scope matrix
- FHIR CapabilityStatement
- Gateway integration guide (for gateway.pdhc and 2gate.pdhc)

### 10.b Server preparation
- `.env` fully prepared with bootstrap SU user (Rule 23)
- `safe_restart.sh` for web instance
- Reverse proxy caution per Rule 22

### 10.c Web deployment (Rule 12)
- Download current server state before changes
- Compare with local
- Present comparison, then operator applies changes

---

## .env variables

```bash
# Database
DATABASE_URL=postgresql://cdr_user:password@localhost:9047/cdr_db
FLASK_SECRET_KEY=<random-64-char>

# Service keys (for inbound from gateways)
GATEWAY_PDHC_SERVICE_KEY=<random-64-char>
TWOGATE_PDHC_SERVICE_KEY=<random-64-char>

# Bootstrap
BOOTSTRAP_SU_API_KEY=<initial-superuser-key>

# GDPR
CRON_SECRET=<random-32-char>
DEFAULT_RETENTION_DAYS=365

# Vector storage (experimental)
PGVECTOR_DIMENSIONS=384
EMBEDDING_MODEL=local

# Upstream (for GUID resolution passthrough, optional)
REQUEST_SERVICE_URL=https://request.pdhc.se/api/v1
GUID_CACHE_TTL_SECONDS=3600

# Cambio CDR sandbox delivery
CAMBIO_DELIVERY_ENABLED=true
CAMBIO_CLIENT_ID=persona.ki.01.sandbox-dev
CAMBIO_CLIENT_SECRET=<from Cambiosandbox.pdf>
CAMBIO_TOKEN_URL=https://idp.innovation.platform.cambio.se/auth/realms/cambio-platform/protocol/openid-connect/token
CAMBIO_BASE_URL=https://sandbox-dev.innovation.platform.cambio.se
CAMBIO_TENANT=sandbox-dev
CAMBIO_COMMISSION_HSA_ID=SE0000000006-CO0001
CAMBIO_HEALTHCARE_UNIT_HSA_ID=SE0000000006-CU0001
CAMBIO_HEALTHCARE_PROVIDER_HSA_ID=SE0000000006-CP0001
CAMBIO_ORG_ID=0000000006

# Flask
FLASK_ENV=development
FLASK_DEBUG=1
```

---

## Endpoint summary

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/v1/ingest` | Service key | Receive single observation from gateway |
| POST | `/api/v1/ingest/batch` | Service key | Receive batch (max 100) from gateway |
| GET | `/api/v1/fhir/metadata` | None | FHIR R5 CapabilityStatement |
| GET | `/api/v1/fhir/Observation` | JWT / Service key | FHIR search |
| GET | `/api/v1/fhir/Observation/<guid>` | JWT / Service key | FHIR read |
| GET | `/api/v1/openehr/composition` | JWT / Service key | openEHR query |
| GET | `/api/v1/openehr/composition/<guid>` | JWT / Service key | openEHR read |
| GET | `/api/v1/openehr/ehr/<patient_guid>/compositions` | JWT / Service key | All compositions for patient |
| GET | `/api/v1/canonical/<table>` | JWT / Service key | Canonical table query |
| GET | `/api/v1/provenance/<ingest_raw_guid>` | JWT / Service key | Full provenance trace |
| GET | `/api/v1/vectors/similar` | JWT / Service key | Similarity search (experimental) |
| GET | `/api/v1/vectors/by-patient/<guid>` | JWT / Service key | Vectors for patient |
| POST | `/api/v1/gdpr/erase` | Admin JWT | Patient erasure (Art. 17) |
| GET | `/api/v1/gdpr/export/<guid>` | Admin JWT | Patient data export (Art. 20) |
| POST | `/api/v1/gdpr/cleanup` | Cron secret | Retention enforcement |
| GET | `/api/v1/cambio/status` | Admin JWT | Cambio delivery statistics |
| GET | `/api/v1/cambio/patient/<guid>` | Admin JWT | Cambio patient mapping + delivery history |
| POST | `/api/v1/cambio/retry` | Admin JWT | Retry all failed Cambio deliveries |
| GET | `/api/v1/health` | None | Health check |

---

## Port allocation

| Port | Service |
|------|---------|
| 9046 | Flask API |
| 9047 | PostgreSQL (with pgvector) |
| 9048 | Reserved |
| 9049 | Reserved |
