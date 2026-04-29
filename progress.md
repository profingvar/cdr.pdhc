# cdr.pdhc — Progress

## Status

Implementation in progress. Core service built 2026-04-10.
15 tests passing (health, ingest, transformer, cambio).

Platform-plan Phase 1 in flight (CDR completion). §1.1 schema migration
done 2026-04-24 — adds 9 FHIR per-type live tables + 9 history tables +
sync_group + cdr_audit_plan_miss + change_feed. Migration round-trips
clean on fresh Postgres.

---

## Phase 1 — Foundation
- [x] 1.a Project scaffold
- [x] 1.b Docker and database setup
- [x] 1.c start.sh
- [x] 1.d safe_restart.sh

## Phase 2 — Database schema
- [x] 2.a Layer 1 — Raw Store
- [x] 2.b Layer 2 — Standard Store (FHIR + openEHR)
- [x] 2.c Layer 3 — Canonical Store
- [x] 2.d Provenance and context store
- [ ] 2.e Vector store
- [x] 2.f Deduplication registry
- [x] 2.g Audit and governance
- [x] 2.h LOINC-to-archetype mapping table

## Phase 3 — Ingest API
- [x] 3.a Service key authentication
- [x] 3.b Unified ingest endpoint
- [x] 3.c Ingest processing pipeline
- [x] 3.d Batch ingest
- [x] 3.e FHIR↔openEHR transformation service

## Phase 3f — Cambio CDR sandbox delivery
- [x] 3f.a Patient identity mapping (cambio_patient_map table)
- [x] 3f.b Delivery tracking (cambio_delivery_log table)
- [x] 3f.c Delivery pipeline (async worker, retry with backoff)
- [x] 3f.d Token management (OAuth2 client credentials, caching)
- [x] 3f.e Cambio API client (FHIR + openEHR delivery)
- [x] 3f.f Delivery status endpoint

## Phase 4 — Query API
- [x] 4.a FHIR R5 read endpoints
- [x] 4.b openEHR query endpoint
- [x] 4.c Canonical query endpoints
- [ ] 4.d Provenance query
- [ ] 4.e Vector similarity search

## Phase 5 — GDPR compliance
- [ ] 5.a Patient erasure
- [ ] 5.b Patient data export
- [ ] 5.c Retention policy
- [ ] 5.d Consent tracking

## Phase 6 — Gateway integration adapters
- [ ] 6.a gateway.pdhc adapter
- [ ] 6.b 2gate.pdhc adapter
- [ ] 6.c Resilience

## Phase 7 — Frontend
- [ ] 7.a Dashboard
- [ ] 7.b Patient data viewer
- [ ] 7.c Mapping manager
- [ ] 7.d GDPR tools
- [ ] 7.e System status

## Phase 8 — FHIR CapabilityStatement
- [x] 8.a CapabilityStatement

## Phase 9 — Testing
- [x] 9.a Unit tests (15 passing)
- [ ] 9.b Integration tests
- [ ] 9.c Full endpoint test script

## Phase 10 — Deployment
- [ ] 10.a Documentation
- [ ] 10.b Server preparation
- [ ] 10.c Web deployment

---

## Platform-plan Phase 1 (CDR completion) — overlay on the local plan above

Per `../plans/CDR_sim_dashboard_execution_plan.md` §1.

### §1.1 Schema — DONE (2026-04-24)
- [x] §1.1.a–c — per-type FHIR resource tables (patient, observation,
  questionnaire_response, condition, medication_statement,
  medication_request, allergy_intolerance, procedure, encounter,
  diagnostic_report). Common columns + composite index
  `(patient_guid, org_guid, code_canonical, effective_at)` + sync_group
  index + org index + code index.
- [x] §1.1.e — matching `*_history` tables, PK `(guid, version_id)`.
- [x] §1.1.f — `version_id` column on every live row.
- [x] §1.1.g — `sync_group` table.
- [x] §1.1.h — `mapping_version` column on every resource.
- [x] §1.1.i — `change_feed` table (also covers Phase 1.5 plumbing).
- [x] §1.2.d.ii — `cdr_audit_plan_miss` table.
- [ ] §1.1.d — `cdr_audit` event-detail append-only table is partially
  covered by the existing `audit_log` table; verify column shape on
  next pass.

### §1.2 Ingest — DONE (2026-04-24)
- [x] §1.2.a — `POST /api/v1/fhir/Bundle` (transaction + batch dispatch).
- [x] §1.2.b — `POST /api/v1/fhir/<Type>` per-resource endpoints.
- [x] §1.2.c — write-side canonicalisation step 1 (xlate.pdhc /translate
  via `app/services/xlate_client.py`).
- [x] §1.2.d — step 2 (plan.pdhc $validate-code via
  `app/services/plan_client.py`); rewrites `coding[]` so the canonical
  is at index 0 with foreign codings preserved.
- [x] §1.2.d.i — xlate miss → 422 + xlate_miss OperationOutcome with
  `issue.location`.
- [x] §1.2.d.ii — plan miss → 422 + plan_miss OperationOutcome and
  `cdr_audit_plan_miss` upsert (seen_count, first_seen_at, last_seen_at,
  last_request_id).
- [x] §1.2.e — dedup keys per resource type
  (Observation/QR/Condition/MedStmt/MedReq/Encounter/Procedure/AllergyIntol/DxReport/Patient).
- [x] §1.2.f — provenance stamping in `meta.source / .tag / .security`.
- [x] §1.2.g — history copy on update + version_id increment.
- [x] §1.2.h — ETag (`W/"<n>"`) on every read, If-Match required for
  PUT, returns 412 on mismatch.
- [x] §1.2.i — sync_group_id minted on every write.
- [x] §1.2.j — mapping_version stamped on every resource.
- 16 new pytest tests pass; 31/31 total.

### §1.3 Query surface — DONE (2026-04-24)
- [x] §1.3.a — `GET /api/v1/fhir/<Type>` Search with patient / code /
  date / _id / _tag / _count.
- [x] §1.3.b–c — `GET /Observation/$stats` returns
  `{n, min, max, mean, sd, p25, p50, p75, histogram[]}` (live
  aggregation; materialised-view caching deferred to perf-tuning pass).
- [x] §1.3.d — `_has:Observation:patient:code=<code>` reverse-chain.
- [x] §1.3.e — `GET /Patient/<guid>/$everything` with `_since`,
  `_type`, `_count`. Org-scoped via Rule 24.
- [x] §1.3.f — chained search: `Observation?patient.identifier=...`
  (and `subject.identifier`).
- [x] §1.3.g — `_include` (e.g. `Observation:patient`) and
  `_revinclude` (e.g. `Observation:patient` against Patient search).
- [x] §1.3.h — `GET /<Type>/<guid>/_history` (version list) and
  `GET /<Type>/<guid>/_history/<vid>` (vread).
- [x] §1.3.i — `POST /api/v1/fhir/Bundle` covered by §1.2.
- [x] §1.3.j — terminology shims:
    - `POST /CodeSystem/$lookup` → termbank.pdhc
    - `POST /ConceptMap/$translate` → xlate.pdhc
    - `POST /ValueSet/$validate-code` → plan.pdhc
- 24 new tests in `test_fhir_read.py`; 55/55 total.

### §1.5 Event backbone — DONE (2026-04-24)
- [x] `change_feed` table created in §1.1.
- [x] Write-path inserts a `change_feed` row on every create / update
  (§1.2); no DB triggers needed since we own the writer.
- [x] `GET /api/v1/fhir/events?since=&_count=&resource_type=` — pull-based
  long-poll surface for sibling services (dashboard, simulator, other
  CDRs). Org-scoped per Rule 24.

### §3.1 Multi-instance compose — DONE (2026-04-26)
- [x] §3.1.a — `cdr_app/docker-compose.yml` parametrised on
  COMPOSE_PROJECT_NAME / CDR_INSTANCE / APP_PORT / DB_PORT / DB_VOLUME.
  Defaults match local-dev so nothing breaks for existing workflows.
- [x] §3.1.b — `deploy/stamp.sh N` emits `.env` for instance N (1..5);
  port-block computed from N (no operator math required).
- [x] §3.1.c — port blocks documented in `deploy/README.md` table:
  9046/9045, 9146/9145, 9246/9245, 9346/9345, 9446/9445.
- [x] §3.1.d — per-instance `shared/` layout described.

### §3.2 SSO client registration — operator action (pending)
Documented in `deploy/README.md`. Each instance N needs
`SSO_CLIENT_ID_CDR{N}` / `SSO_CLIENT_SECRET_CDR{N}` in
`sso.pdhc/.env` plus `https://cdr{N}.pdhc.se/auth/callback` in
`ALLOWED_CALLBACK_URLS`.

### §3.3 Reverse-proxy server blocks — operator action (pending)
Each instance needs an nginx server block proxying
`cdr{N}.pdhc.se → 127.0.0.1:{APP_PORT}`. Same TLS chain as the rest
of pdhc.se.

### §3.4 Seeding runs — DONE (locally; needs live CDRs to actually run)
- Profiles authored in sim.pdhc: `cohort_{nord,syd,vast,ost,mitt}.yaml`.
- `sim.pdhc/seed_all.sh` drives the five runs; SEEDING.md in
  sim.pdhc captures the audit trail.

### §3.5 Backups — diff drafted (operator applies on miserver)
- `deploy/server_backup_all.diff` shows the change to add
  `cdr_pdhc_{1..5}_db` pg_dumps to `server_backup_all.sh`.

### §3.6 Phase 3 tests
- Smoke / isolation / auth / seeding-validation / backup-restore
  tests are server-side and operator-collaborative; they are not
  drafted as pytest because they need real running instances.

## Known issues

- Local dev DB at revision `1be600110381` (an orphan from before
  ticket #78). Needs `flask db stamp 8aa2748e0139 && flask db upgrade`
  before §1.2 work can run against it. The Phase 1.1 migration was
  validated against a fresh `cdr_test_phase1` Postgres DB.

## 2026-04-28 — Multi-CDR canary + plan.pdhc indirection + service-key + seeded

5-CDR demonstrator deploy completed end-to-end:

- Steps 4–7 of the deploy-time smoke protocol (`plans/test_inventory.md`
  Section B) all green: 4 stamped `.env` files, 4 docker-compose ups,
  `flask db upgrade` × 4 (revision `2b6d8e6624ce`), `/healthz` 200 with
  `database: connected` on every public hostname.
- Two compose-template fixes caught during the cdr2 canary
  (`docker-compose.yml`): port mapping was double-parametrised
  (`${APP_PORT}:${APP_PORT}` while Dockerfile listens on hardcoded
  9046) and the `volumes: - .:/app` bind mount hid the image's code
  under an empty Colima dir.
- Service-key auth path added to the SSO request loader: sim.pdhc
  posts FHIR Bundles with `X-Source-Service: sim.pdhc` +
  `X-Service-Key: $SIM_PDHC_SERVICE_KEY` and gets a synthetic
  SU-equivalent access blob. Existing SSO flow unchanged.
- Canonicaliser short-circuits on `https://plan.pdhc.se/Concept`
  system codings: resolves the GUID via plan.pdhc, composes the
  termbank canonical URI, promotes that coding, no xlate hop.
  Encounter dispatch in `_CODE_PATHS` corrected to walk `code` (not
  `class`).
- 4 CDRs seeded with 100 patients each, 730-day window, plan.pdhc
  indirection through the canonicaliser. Final state stored as
  proper LOINC / SNOMED / ICD-10 / ATC URIs in `code_canonical`.

### §3.5 Backups — exercised
- Ad-hoc cdr1..5 pg_dump produced 5 dumps (29 KB legacy + 3-10 MB
  per seeded instance). Restore-smoke on cdr3 returned exact-match
  row counts (100 / 10800 / 512). The diff in
  `deploy/server_backup_all.diff` is still pending operator
  integration into `~/backup_pdhc_family.sh` (covered by Block D ack
  in `plans/post_seed_followups.md`).

## Known issues

- Local dev DB at revision `1be600110381` (an orphan from before
  ticket #78). Needs `flask db stamp 8aa2748e0139 && flask db upgrade`
  before §1.2 work can run against it. The Phase 1.1 migration was
  validated against a fresh `cdr_test_phase1` Postgres DB.
- `fhir_read._org_filter` ignores `g.access_blob.is_su_admin`; service-
  key callers and proper SSO admins need the legacy `X-Is-Admin: 1`
  header today (`plans/post_seed_followups.md` Block G3).
- nginx `client_body_temp` blocked by macOS provenance — Block B.
- PlanClient should treat HTTP 429 as transient, not plan_miss —
  Block A.
