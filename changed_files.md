# cdr.pdhc — Changed Files

| Date | File | Change |
|------|------|--------|
| 2026-03-30 | readme.md | Created deployment plan (Phases 1–10) |
| 2026-03-30 | CLAUDE.md | Created with design system reference |
| 2026-03-30 | progress.md | Created with phase checklist |
| 2026-03-30 | changed_files.md | Created |
| 2026-04-10 | readme.md | Added Phase 3f — Cambio CDR sandbox delivery (patient mapping, delivery tracking, async pipeline, token management, API client, status endpoints). Updated data flow diagram. Added Cambio .env variables. Added Cambio delivery endpoints to endpoint summary. Clarified delivery scope (plan.pdhc.se concept-mapped only). |
| 2026-04-10 | progress.md | Added Phase 3f checklist items |
| 2026-04-15 | cdr_app/app/auth.py | Ticket #52 — before_request loader redirects to `{SSO_BASE_URL}/change-password` when freshly validated blob has `must_change_password=True`, gated between `session["access_blob"]=blob` and `has_analysis_access`. Public paths (`_public_path`) still short-circuit first so health/static stay reachable. Deployed to `/usr/local/www/cdr.pdhc/cdr_app/app/auth.py`; backup `.bak-2026-04-15T18-30-57Z` on server. Gunicorn master (pid 6383) HUPed; module `syntax ok` via server venv; `/` returns 302 (SSO redirect) as expected. `/healthz` continues to OOM-kill workers per the pre-existing CLAUDE.md §13 incident — not introduced by this change. |
| 2026-04-16 | cdr_app/app/api/health.py | Ticket #70 — /healthz adds Access-Control-Allow-Origin https://www.pdhc.se + Methods GET + Vary: Origin + Cache-Control: no-store so services.html can use mode:'cors' instead of no-cors false-greens |
| 2026-04-16 | cdr_app/app/api/health.py | Ticket #70 follow-up — renamed `db:bool` field to canonical `database:"connected"\|"unavailable"` (CLAUDE.md §10) so services.html DB dot reads real state. Deployed (sha 75c7537e); SIGHUP'd gunicorn master pid 7884. Verified curl returns `{"database":"connected","service":"cdr.pdhc","status":"ok"}`. |
| 2026-04-16 | cdr_app/tests/test_health.py | Ticket #70 follow-up — updated assertions from `"db" in data` to `database in ("connected","unavailable")` to match new canonical shape. |
| 2026-04-16 | safe_restart.sh | Ticket #78 reconcile (cdr): pulled server→local. Server had 3 improvements local lacked: (1) `export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` near top (pdhc-wide gunicorn fork-safety fix), (2) `PORTS=(9046)` narrowed from `(9046 9047 9048 9049)` — killing 9047 breaks colima ssh-mux (per feedback memory), (3) multi-PID TERM-then-9 kill loop instead of single `$PID`. Local backup at `/tmp/cdr_safe_restart.local.bak.20260416T202225Z.sh`. Sha now matches server: 5b1d6d46. |
| 2026-04-16 | start.sh | Ticket #78 reconcile (cdr): pulled server→local. Same 3 deltas as safe_restart.sh (OBJC export + PORTS narrowing). Local backup `/tmp/cdr_start.local.bak.20260416T202225Z.sh`. Sha matches server: 5cd5cf3b. |
| 2026-04-16 | cdr_app/migrations/versions/ | Ticket #78 reconcile (cdr): resolved divergent alembic history. Verified live prod revision = `8aa2748e0139` (via `docker exec cdr_pdhc_db psql -U cdr_user -d cdr_db -c 'SELECT version_num FROM alembic_version'`). Pulled server's 2 migrations (`45928917a5a1_initial_migration.py`, `8aa2748e0139_add_user_model_and_sso.py`) into local; removed local's orphan `1be600110381_initial_migration.py` which was a divergent local-only reset that never ran against production. Orphan backed up at `/tmp/cdr_orphan_migration.local.bak.20260416T202345Z.py`. Local dev DB will need `flask db stamp 8aa2748e0139` or a fresh init when next started. |
| 2026-04-24 | .gitignore, baseline | Rule 14 — git init the cdr.pdhc repo and commit the existing implementation as the baseline (Phases 1–4 of the local deployment plan + Cambio sandbox delivery). 51 files in initial commit. Excludes venv/, .env, logs/, __pycache__/, results/, *.pdf, *.docx fixture artefacts. |
| 2026-04-24 | cdr_app/app/models/resources.py | NEW — platform-plan §1.1 — FHIR per-type ORM models for Patient, Observation, QuestionnaireResponse, Condition, MedicationStatement, MedicationRequest, AllergyIntolerance, Procedure, Encounter, DiagnosticReport. Each gets a live table and a matching `*_history` table. Common columns per §1.1.b: guid, patient_guid, org_guid, code_canonical, effective_at, raw_json (JSONB on Postgres / JSON on SQLite via dialect-variant), source, source_request_id, meta_tag, version_id, sync_group_id, mapping_version, etag, created_at, updated_at. Cross-cutting: SyncGroup, CdrAuditPlanMiss, ChangeFeed. |
| 2026-04-24 | cdr_app/app/models/__init__.py | platform-plan §1.1 — added side-effect import of `app.models.resources` so SQLAlchemy registers the new tables on `db.metadata`. Re-exports SyncGroup, CdrAuditPlanMiss, ChangeFeed, live_model, history_model. |
| 2026-04-24 | cdr_app/migrations/versions/2b6d8e6624ce_phase1_schema_resource_tables_history.py | NEW — Alembic migration creating all per-type live + history tables, sync_group, cdr_audit_plan_miss, change_feed. Includes downgrade. Verified up/down/up clean against fresh Postgres `cdr_test_phase1` DB. JSONB columns on Postgres. |
| 2026-04-24 | cdr_app/app/services/xlate_client.py | NEW — platform-plan §1.2.c — HTTP client for xlate.pdhc /translate. Thread-safe TTL cache with sentinel pattern. Raises XlateUnreachable on network errors so writer can return 503 instead of 422. |
| 2026-04-24 | cdr_app/app/services/plan_client.py | NEW — platform-plan §1.2.d — HTTP client for plan.pdhc /api/v1/ValueSet/$validate-code. Same cache shape as xlate_client. parse_canonical_uri helper. Raises PlanUnreachable. |
| 2026-04-24 | cdr_app/app/services/canonicalisation.py | NEW — write-side canonicalisation chain. For every CodeableConcept on a resource, walks coding[] through xlate→plan and rewrites to put the termbank canonical at coding[0] with original codings preserved at [1..n]. CanonicalisationResult statuses: ok / xlate_miss / plan_miss / transient. OperationOutcome builders for each error shape (with issue.location per §1.2.d.i). record_plan_miss() upserts cdr_audit_plan_miss rows. |
| 2026-04-24 | cdr_app/app/services/resource_writer.py | NEW — generic FHIR resource writer. Dedup-key lookup (per-resource-type, §1.2.e) → insert OR update-with-history-copy. version_id increment, ETag (W/"<n>"), If-Match check (412 EtagMismatch), sync_group minting (§1.2.i), mapping_version stamping (§1.2.j), provenance via meta.source / .tag / .security (§1.2.f), change_feed event emission. Rule 18: integer patient ids rejected at extract time (IntegerPatientReference). Services don't commit — caller commits so Bundle dispatch can group writes. |
| 2026-04-24 | cdr_app/app/api/fhir_write.py | NEW — FHIR write API blueprint mounted at /api/v1/fhir. Endpoints: POST /<Type> (create), PUT /<Type>/<guid> (update with optimistic concurrency), POST /Bundle (transaction or batch dispatch with per-entry routing and atomic vs. per-entry commit semantics). Returns 422+xlate_miss / 422+plan_miss / 412 / 503 OperationOutcomes per §1.2.d.i / .d.ii / .h. |
| 2026-04-24 | cdr_app/app/api/__init__.py + cdr_app/app/__init__.py | Wired fhir_write_bp into the app factory at /api/v1/fhir. |
| 2026-04-24 | cdr_app/app/models/resources.py | change_feed.seq uses BigInteger.with_variant(Integer, "sqlite") so SQLite test fixture autoincrements via INTEGER ROWID alias while Postgres keeps bigint. |
| 2026-04-24 | cdr_app/tests/test_fhir_write.py | NEW — 16 tests covering execution-plan §1.5 unit-test list: bundle_dispatch, dedup_observation, canonicalisation, xlate_miss, plan_miss, plan_miss_dedup, provenance_stamped, integer_patient_rejected, history_on_update, etag_if_match, sync_group_minted, mapping_version_stamped, change_feed_create / change_feed_update, bundle_transaction_rollback. Uses a fake canonicaliser injected via current_app._canonicaliser to keep tests hermetic. |
| 2026-04-24 | cdr_app/app/api/fhir_read.py | NEW — platform-plan §1.3 — read / search / vread / $everything / $stats / terminology shims / events. Endpoints under /api/v1/fhir: GET /<Type>, GET /<Type>/<guid>, GET /<Type>/<guid>/_history, GET /<Type>/<guid>/_history/<vid>, GET /Patient/<guid>/$everything, GET /Observation/$stats, POST /CodeSystem/$lookup → termbank, POST /ConceptMap/$translate → xlate, POST /ValueSet/$validate-code → plan, GET /events. Search supports patient/code/date/_id/_tag/_count, chained search via patient.identifier, _has reverse-chain, _include/_revinclude. $stats computes n/min/max/mean/sd/p25/p50/p75/histogram in pure-python (statistics module). Org-scoping (Rule 24) via X-Org-Guids / X-Is-Admin headers — production wiring lands when SSO is wired in app.auth. |
| 2026-04-24 | cdr_app/app/api/fhir_api.py | Replaced legacy /Observation read/search routes (which read from the generic fhir_resources legacy table) with a richer CapabilityStatement that advertises every per-type resource, the three terminology operations, and $everything / $stats. The actual read endpoints are now in fhir_read.py. |
| 2026-04-24 | cdr_app/app/api/__init__.py + cdr_app/app/__init__.py | Wired fhir_read_bp into the app factory at /api/v1/fhir alongside fhir_write_bp. |
| 2026-04-24 | cdr_app/tests/test_fhir_read.py | NEW — 24 tests covering execution-plan §1.5 unit-test items for the read surface: read_returns_resource_with_etag, read_404, read_org_scoped_non_admin, admin_sees_cross_org, search_by_patient/code/date_range, chained_search_patient_identifier, include / revinclude / _has, history_list_after_update, vread_specific_version, vread_404_for_unknown_version, everything_returns_all_for_patient, everything_respects_org_scope, everything_type_filter, stats_math_against_fixture (1000-value comparison vs. statistics module reference), stats_empty, codesystem_lookup_proxy, conceptmap_translate_proxy, validate_code_proxy, events_returns_change_feed_rows, events_since_advances. Terminology shim tests use unittest.mock.patch on requests.get/post. |
| 2026-04-26 | cdr_app/docker-compose.yml | Platform-plan §3.1.a — parametrised: COMPOSE_PROJECT_NAME / CDR_INSTANCE / APP_PORT / DB_PORT / DB_VOLUME read from env. Defaults preserve local-dev mapping (DB on 9047) so existing workflows keep working; stamped instances 1..5 use the new 9046/9045 + 100*(N-1) port pairs. |
| 2026-04-26 | deploy/stamp.sh | NEW — platform-plan §3.1.b — `deploy/stamp.sh N` (N ∈ 1..5) generates `.env` for instance N with the right port pair, project name, volume name. Empties out secrets the operator must fill (POSTGRES_PASSWORD, FLASK_SECRET_KEY, SSO_*, CAMBIO_* on CDR1, etc.). `--print` mode for inspection. |
| 2026-04-26 | deploy/README.md | NEW — multi-instance deploy reference: port plan table, stamping recipe, operator-only steps (SSO registration, nginx server block, tarball deploy), local-dev backwards compatibility note, per-instance shared/ layout, backup pointer. |
| 2026-04-26 | deploy/server_backup_all.diff | NEW — platform-plan §3.5 draft diff for `~/T7_sidewinder/server_backup_all.sh` on miserver: pg_dump for cdr_pdhc_{1..5}_db containers, skipping any not running, custom-format dumps with no-owner / no-acl. Operator applies. |

# 2026-04-28 — Multi-CDR canary deploy + plan.pdhc indirection + service-key auth + 5-CDR seed

| File | Change |
|------|--------|
| `cdr_app/docker-compose.yml` | Two compose-template fixes caught during cdr2 canary deploy. (a) Port mapping was double-parametrised `${APP_PORT}:${APP_PORT}` but Dockerfile hardcodes container CMD to `0.0.0.0:9046`, so for cdr2-5 the host port (9146/9246/9346/9446) mapped to a container port nothing was listening on. Fixed: host stays per-instance, container side stays `9046`. (b) `volumes: - .:/app` bind mount removed. The Colima VM doesn't share `/usr/local/www/`, so the bind put an empty dir over the image's code. Dockerfile already `COPY . .`'s the app. |
| `cdr_app/app/__init__.py` | New env-driven config: `SIM_PDHC_SERVICE_KEY`, `PLAN_BASE_URL`, `STRICT_CANONICALISATION`. |
| `cdr_app/app/auth.py` | Added service-key auth path. `KNOWN_FHIR_SERVICES = {"sim.pdhc": "SIM_PDHC_SERVICE_KEY"}`. `_service_key_outcome()` inspects `X-Source-Service` + `X-Service-Key`. `_service_blob()` synthesises a SU-equivalent access blob with `is_su_admin=True`, `effective_phases=["analysis","ingestion"]`, empty `organization_ids`, and a `service_source` marker so downstream code can distinguish service writes from human users. In `install_request_loader._loader`, service-key check runs first (before SSO redirect) for non-public paths. |
| `cdr_app/app/services/plan_client.py` | Added `_LIB_SLUG` mapping (LOINC / Snomed CT / ICD10 / ATC / KVÅ / local → uri-slug). New `resolve_concept(guid)` method: `GET /api/v1/concepts/<guid>` from plan.pdhc, looks up canonical_lib name (cached separately via `_lib_name`), composes a termbank canonical URI `https://termbank.pdhc.se/CodeSystem/<slug>/<refnumber>`, returns dict with system / canonical_uri / canonical_lib_name / canonical_refnumber / display / guid. Cached via the same TTL cache as `validate_canonical`. |
| `cdr_app/app/services/canonicalisation.py` | Added `PLAN_CONCEPT_SYSTEM = "https://plan.pdhc.se/Concept"` constant. `_canonicalise_codeable_concept` now FIRST-passes plan.pdhc Concept system codings: if `coding[i].system == PLAN_CONCEPT_SYSTEM`, calls `plan.resolve_concept(<guid>)`, builds a canonical termbank coding from the result, prepends to `coding[]` (originals preserved at [1..n]), returns. No xlate hop, no `$validate-code` round-trip — plan.pdhc is the authority for indirect-referenced concepts. Plan-miss only happens if the GUID itself isn't found in plan.pdhc. Encounter dispatch in `_CODE_PATHS` switched from `["class"]` to `["code"]` — class is FHIR-structural HL7 v3-ActCode, not a clinical concept. |
| `cdr2.pdhc / cdr3 / cdr4 / cdr5 .env` (per-instance generated) | Added `SIM_PDHC_SERVICE_KEY=<43-char>` and `PLAN_BASE_URL=http://host.docker.internal:9030` (Colima VM resolves host.docker.internal but not `127.0.0.1` from inside the container). Backups at `deploy/instances/cdr{N}/.env.bak.20260428-pre-indirection`. Out-of-repo snapshot at `~/T7_sidewinder/backups/2026-04-28T11-00-00Z-indirect/cdr_envs/`. |
| `miserver:/usr/local/www/cdr{2,3,4,5}.pdhc/releases/<TS>/` | Two release tarballs deployed during the session: `2026-04-28T09-37-23Z` (initial 5-CDR up + healthz green) and `2026-04-28T11-03-39Z` (canonicaliser indirection + service-key auth). Release-symlink swap; db volumes preserved across both. |
| `cdr_app/cdr_pdhc_{2..5}_db` (volumes on miserver) | Seeded to 100 patients each via `sim run --days 730 --no-cgm-raw`. Profile mapping: cdr2=cohort_nord (low-SES), cdr3=cohort_vast, cdr4=cohort_ost (low-SES), cdr5=cohort_mitt. After parallel-seed → manual purge (rate-limit 4xx during burst) → sequential re-seed via SSH tunnels (workaround for nginx `client_body_temp` macOS provenance issue, see `plans/post_seed_followups.md` Block B). Final state: 400 patients, 43,200 observations, ~50K total resources. cdr1 (legacy cdr.pdhc) unchanged. |
| `~/backups/cdr_pdhc_20260428T134010Z/` | First CDR backup set: 5 pgdumps (cdr1 = 29KB, cdr2 = 10MB, cdr3 = 3MB, cdr4 = 6.5MB, cdr5 = 6.5MB). Restore-smoke into a throwaway DB on cdr3 returned identical row counts (100 patients / 10800 obs / 512 conditions). |

Pending follow-ups documented in `plans/post_seed_followups.md`:

- Block A: PlanClient should detect HTTP 429 (rate-limit) and retry with backoff instead of treating it as plan_miss. 6/400 bundles 4xx-ed during the original parallel seed because of this.
- Block B: nginx `client_body_temp` permission issue (macOS provenance attributes block worker writes). Today bypassed via SSH port-forwards; needs `brew services restart nginx` or `client_body_buffer_size 1M` directive.
- Block G3: `fhir_read._org_filter` should respect `g.access_blob.is_su_admin` so service-key callers and proper SSO admins bypass org-scoping without needing the legacy `X-Is-Admin: 1` header.
- Block G4: Sim should emit FHIR Bundle entries with `request.method=PUT Patient/<guid>` so re-pushes upsert (today's POST creates duplicate patient rows).

| 2026-04-28 | cdr_app/app/api/fhir_write.py | FHIR R5 "create with client-supplied id" — `_write_one` now defaults `resource_id` from the canonicalised body's `id` whenever the caller didn't provide one (and it isn't an update-by-guid). Applies uniformly to direct POSTs (`POST /<Type>`) AND to Bundle entries (`POST /Bundle`) — sim seeds via Bundle, so the dispatch path needed coverage too. Resolves the orphan-Patient seed integrity bug: sim's Patient.id and the patient_guid carried on every Observation/Condition reference now match. Initial fix shipped 2026-04-28T20:12Z covered only `post_resource`; full fix in `_write_one` deployed 2026-04-28T20:21Z. Round-trip verified on both single POST and Bundle paths. |
| 2026-04-28 | cdr_app/app/api/stats.py | `/api/v1/stats` now sums per-type live tables (Phase-1.2 ingest path) for `fhir_resources` and reads Patient count from the per-type `patient` table (falling back to legacy `ingest_raw` distinct-by-guid). Fixes the CDR landing page showing 0 patients/0 fhir_resources after the platform-plan migration. Deployed alongside the writer fix. Verified: cdr2 stats now 101 patients, 40,527 fhir_resources. |
| 2026-04-28 | cdr_app/app/templates/base.html | Added `Workspace` nav link pointing to `https://dashboard.pdhc.se/workspace` so users entering the CDR can hop across to the analysis dashboards in one click. Deployed to all 4 cdr containers + restart for Jinja2 cache reload. |
| 2026-04-28 | cdr_app/tests/test_fhir_write.py | Added `test_post_preserves_client_supplied_id` covering the FHIR R5 client-supplied-id semantics restored by today's writer fix. Pre-existing 403 issue (test helper sends `X-Source-Service: test-suite` which isn't in `KNOWN_FHIR_SERVICES`) blocks the whole test_fhir_write suite from running locally — separate fix needed. |
| 2026-07-02 | cdr_app/app/services/ingest_pipeline.py + cdr_app/tests/test_live_observation_ingest.py | Fixed `_extract_loinc` (renamed → `_extract_primary_code_hint`, legacy name kept as alias for the openEHR→FHIR ingest branch + existing tests): now also reads `urn:pdhc:concept` and `https://plan.pdhc.se/api/v1/concepts` codings that gateway.pdhc forwards. Pre-fix every forwarded row had NULL `FhirResource.loinc_code` and the `has_concept_ref` gate never fired for production traffic (Cambio delivery not enqueued for PDHC-scoped observations — cdr1_ssot_cutover_plan.md §11 called this out as a known post-cutover issue). Column is String(16); the new `_loinc_column_writable(code)` guard filters PDHC concept GUIDs (36 chars) so they don't blow up the flush, and the `has_concept_ref` gate at ingest_pipeline.py:183 now also checks `_primary_canonical_uri(fhir_row.resource_json)` — mirrors how the LiveObservation.code_canonical write path already recognises PDHC concepts (#295). 7 new tests. Historical FhirResource.loinc_code stays NULL; downstream reads go via LiveObservation.code_canonical, populated correctly since #295. Historical Live rows filled by the existing `flask backfill-live-observations` CLI. |
| 2026-07-02 | cdr_app/app/api/stats.py + cdr_app/app/api/__init__.py + cdr_app/app/__init__.py | Restored the `/api/v1/stats` blueprint on cdr1 (dropped in commit 1fdcee5 as part of #292 analyse-split phase 6). Dashboard.pdhc's analyse layer still fanouts to `/api/v1/stats` on every CDR in the registry — cdr2-5 kept the endpoint, only cdr1 lost it, so every fanout to cdr1 was 404'ing and "analyse can't see any CDRs" ensued. Restored the pre-#292 stats.py verbatim (25-line per-type row-count aggregate) + registered `stats_bp` under `/api/v1` prefix in the app factory. Comment in api/__init__.py documents this as a compatibility shim: when the analyse layer eventually does its own aggregation locally, this can be removed. Deployed to cdr1 prod 2026-07-02 (rebuild + up -d --build via cdr.pdhc/cdr_app/docker-compose.yml). Verified: cdr1 /api/v1/stats returns 200 with real counts (7063 observations, 9 patients). |

## 2026-07-06 — M0 #416: adopt affiliations[] Zone-1 scope + session_phases
- cdr_app/app/auth.py — public scope_org_guids() (affiliations[].care_unit_guid,
  dual-read fallback) + _phases() (session_phases w/ fallback); wired into
  _blob_to_user.org_ids and has_analysis_access.
- cdr_app/app/api/fhir_read.py — both Rule-24 filter sites (_org_filter +
  change-feed) use scope_org_guids(blob) instead of blob['organization_ids'].
- cdr_app/tests/test_reform_scope.py — NEW, 8 tests.
- Note: test_fhir_read.py has 17 PRE-EXISTING failures (structural fixture
  KeyErrors, fail on baseline) — unrelated to this change.

| 2026-07-13 | cdr_app/docs/00-user-manual.md (new) | Shared layman user manual for CDR 1-5 (identical software; role differences in §5). Published on pdhc.se/documentation.html for all five CDR rows. |
| 2026-07-13 | cdr_app/app/api/clinical_read.py (new) | #468/#462 D6 — care-delivery read surface for the rebuilt clinical dashboard: GET /api/v1/clinical/patients (org's patients WITH data + counts) and GET /api/v1/clinical/patient/<guid>/summary (per-concept counts desc). Explicit X-Org-Guids/X-Is-Admin scoping (service blob is is_su_admin so fhir_read._org_filter can't be reused); requires X-Access-Purpose:care-delivery + dashboard.pdhc service identity; #422 already passes through for service callers. |
| 2026-07-13 | cdr_app/app/api/__init__.py | Register clinical_read_bp (#468). |
| 2026-07-13 | cdr_app/app/__init__.py | Mount clinical_read_bp at /api/v1/clinical (#468). |
| 2026-07-13 | cdr_app/app/auth.py | Add /api/v1/clinical to _is_read_path so CDR_READ_LOCKDOWN (cdr2-5) admits dashboard.pdhc on it (#468). |
| 2026-07-13 | cdr_app/tests/test_clinical_read.py (new) | 7 tests: purpose/identity guards, org-scoped patient index (names+counts, cross-org excluded, admin sees all), per-patient summary desc + org-scope block. |
| 2026-07-13 | cdr_app/app/api/clinical_read.py | #464 — add GET /api/v1/clinical/patient/<guid>/series: org-scoped care-delivery time-series points (code+effective-date filters), each carries org_guid for dashboard-side spärr. |
| 2026-07-13 | cdr_app/tests/test_clinical_read.py | +4 series tests (ordering+value+org, code/date filter, cross-org block, care-delivery guard). |
| 2026-07-15 | cdr_app/app/services/plan_client.py | #471 item 5 — add PlanClient.lookup_display(guid) via CodeSystem/$lookup (cached, FAIL-OPEN cosmetic). |
| 2026-07-15 | cdr_app/app/api/clinical_read.py | #471 item 5 — resolve_display(code_canonical): parse embedded concept guid (urn:pdhc:concept/<guid>, live prod format) → plan.pdhc display; enrich /clinical/patient/<guid>/summary parameters with `display`. Skips when PLAN_BASE_URL unset. |
| 2026-07-15 | cdr_app/tests/test_clinical_read.py | +4 tests (guid parse, lookup_display parse+fail-open, summary display enrichment, display None when plan unconfigured). |
2026-08-08T09:19:48Z cdr_app/app/auth.py — #541 add analyse.pdhc to KNOWN_FHIR_SERVICES + _ANALYSE_READER_SOURCES lockdown allow-list (additive; dashboard.pdhc retained)
2026-08-08T09:19:48Z cdr_app/app/__init__.py — #541 wire ANALYSE_PDHC_SERVICE_KEY config
2026-08-08T09:19:48Z cdr_app/tests/test_lockdown.py — #541 analyse.pdhc read accepted under lockdown + wrong-key 403
- cdr.pdhc/cdr_app/docs/01-technical-manual.md (Port Allocation section)

## 2026-09-03 — admit gateway.pdhc as a FHIR-write service
- cdr_app/app/auth.py — KNOWN_FHIR_SERVICES += gateway.pdhc:GATEWAY_PDHC_SERVICE_KEY (for Patient demographics upsert)
Deployed to prod CDR1 SURGICALLY (in-place edit — prod cdr1 is behind local git re: #541 analyse additions; do NOT file-overwrite). Key already 3-way-synced.

## 2026-09-23 — #664 declared service purpose, #665 author_org_guid
- cdr_app/app/services/analysis_consent.py — declared_service_purpose(), _resolve_purpose(); both entry points routed through it
- cdr_app/app/models/__init__.py — ClinicalContext.author_org_guid (13th field)
- cdr_app/app/services/ingest_pipeline.py — accept author_org_guid from clinical_context
- cdr_app/app/api/provenance.py — expose author org; no performer fallback
- cdr_app/migrations/versions/a1b2c3d4e5f6_add_author_org.py (new) — additive, nullable, no backfill
- cdr_app/tests/test_declared_service_purpose.py (new, 12)
- cdr_app/tests/test_author_org.py (new, 5)

## 2026-09-23 — #664/#665 deployed to cdr1 (surgical: 4 files + 1 migration, auth.py/__init__.py untouched)

## #689 deploy 2026-09-23 — copied to cdr2–cdr5 on miserver (no local source change)
- cdr_app/app/services/analysis_consent.py
- cdr_app/app/services/ingest_pipeline.py
- cdr_app/migrations/versions/a1b2c3d4e5f6_add_author_org.py

## #698 — FHIR value-quantity search, 2026-09-24
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/fhir_read.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/fhir_api.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_fhir_read.py

## #701 (cdr half) — patient allow-list, 2026-09-24
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/fhir_read.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_fhir_read.py

## 2026-10-02 — #708: cross-service smoke for cdr1
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/deploy/smoke_siblings.py — NEW. Read-only sibling smoke: plan.pdhc concept resolution against a canonical this CDR actually stores, ips.pdhc consent verdict for a service-shaped read, sso reachability + client-credential presence, Cambio config (deliberately NOT called — external third party), xlate absent-as-expected, and a Rule 18 check on stored code_canonical tails. Runs via stdin so no rebuild is needed.

## 2026-10-02 — #732: remove the 2gate.pdhc ingest identity
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/auth.py — 2gate.pdhc dropped from the INGEST KNOWN_SERVICES; require_service_key now logs a named warning when an allowlisted caller's config key is unset, instead of refusing it indistinguishably from a wrong key.
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/__init__.py — TWOGATE_PDHC_SERVICE_KEY no longer read into config.
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_ingest.py — 3 new tests (two verified to fail with 2gate re-added); the cross-service isolation test repointed from 2gate.pdhc to sim.pdhc, keeping the coverage.
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/conftest.py — TWOGATE_PDHC_SERVICE_KEY dropped.

## 2026-10-06 — #733 concept inventory
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/stats.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_concept_inventory.py

## 2026-10-06 — #760 read-only service keys
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/auth.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_read_only_service_key.py

## 2026-10-06 — #769 datapoint context on the row
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/models/resources.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/migrations/versions/a7b8c9d0e1f2_datapoint_context_on_the_row.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_datapoint_context.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/ingest_pipeline.py (#773)
- cdr_app/app/api/stats.py (#756 — add the `observations` key)
- cdr_app/app/services/analysis_consent.py
- cdr_app/.env.example
- cdr_app/tests/test_analysis_consent.py
- cdr_app/deploy/smoke_siblings.py
- 2026-10-12 cdr_app/app/services/ingest_pipeline.py — _clinical_context_from: accept the FLAT canonical shape, not only a nested block (#820); store plan_activity_guid
- 2026-10-12 cdr_app/app/models/__init__.py — ClinicalContext.plan_activity_guid (#820)
- 2026-10-12 cdr_app/migrations/versions/a5b6c7d8e9f0_clinical_context_plan_activity.py (NEW, APPLIED to cdr1+cdr2+cdr3)
- 2026-10-12 cdr_app/tests/test_flat_context_ingest.py (NEW)
- 2026-10-12 cdr_app/app/models/__init__.py — ClinicalContext.sim_run_id (#821)
- 2026-10-12 cdr_app/app/services/ingest_pipeline.py — sim_run_id in _CONTEXT_KEYS + stored on the insert (#821)
- 2026-10-12 cdr_app/migrations/versions/b6c7d8e9f0a1_clinical_context_sim_run_id.py (NEW, APPLIED to cdr1+cdr2+cdr3)
- 2026-10-12 cdr_app/tests/test_flat_context_ingest.py — 5 sim_run_id tests
- 2026-10-13 cdr_app/app/services/ingest_pipeline.py — #809: build_live_observation_row stores patient_org_guid from the SR context; stale #774 comment replaced
- 2026-10-13 cdr_app/tests/test_datapoint_context.py — #809: superseded test_ingest_does_NOT_write_patient_org_guid_from_the_producer replaced by 3 cases (stores-from-context, absent-stays-NULL, backfill-does-not-invent)
- 2026-10-13 cdr_app/app/__init__.py — #816: SESSION_COOKIE_SECURE/HTTPONLY/SAMESITE
- 2026-10-13 cdr_app/app/services/ingest_pipeline.py — #787.2: _primary_canonical_uri prefers urn:pdhc:concept explicitly (two passes); legacy plan.pdhc URL still accepted
- 2026-10-13 cdr_app/tests/test_concept_canonical_spelling.py (NEW)
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/fhir_read.py (#831 cap)
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_fhir_read.py (#831 cap)
/Users/martiningvar/T7_sidewinder/cdr.pdhc/deploy/smoke_siblings.py  (#708/#830: sibling smoke for cdr1/cdr2/cdr3)

## #833 — xlate: a permanent condition reported as transient (2026-10-16)

- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/xlate_client.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/canonicalisation.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/fhir_write.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_xlate_client.py (new)
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_canonicaliser_xlate_not_configured.py (new)
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/.env.example
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/deploy/smoke_siblings.py

## #836 — consolidate the two sibling smokes (2026-10-18)

- /Users/martiningvar/T7_sidewinder/cdr.pdhc/deploy/smoke_siblings.py (absorbed four checks; 14 -> 18)
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/deploy/smoke_siblings.py (DELETED)
- cdr_app/app/api/fhir_read.py + app/services/resource_writer.py + app/api/fhir_write.py + tests/test_fhir_read.py + tests/test_analysis_consent.py — #818: Rule 24 org scoping repointed from `org_guid` (the PERFORMER) to `patient_org_guid` (where the patient belongs), at all three filter sites in `_org_filter` including the fail-closed sentinel. **The ticket's premise was wrong in the operator's favour**: it expected the repoint to move access in both directions, with performer-affiliated readers losing ~95% of cdr1. Measured 2026-10-20, it grants and revokes nothing, because the old filter compared two DISJOINT IDENTIFIER SPACES and matched nothing at all — `scope_org_guids` returns `affiliations[].care_unit_guid` and all 10 rows of sso's `affiliations` table hold care-unit/organisation guids, while `observation.org_guid` holds the ips CLINIC guid (cdr2: performers 02b83b6b/880d2fe5 against patient orgs 7d55624c/560fcbe4, and no clinic's care_unit_guid equals any performer value). So Rule 24 was failing CLOSED for every non-admin reader and the repoint restores access it never actually gave. Safe on live data: 73,582 rows across cdr1/cdr2/cdr3, **0 NULL** patient_org_guid, every row from sim via the ingest pipeline which sets it. Second half, found because the tests failed: `POST /api/v1/fhir/<Type>` never set the column — `WriteContext` had no such field and `resource_writer` no reference — so every row written through the FHIR API would have become admin-only once the filter repointed. Latent rather than live (gateway has written none of the current rows) but it would have bitten on the next gateway write. `WriteContext.patient_org_guid` added, stamped on both live-row inserts, sourced by `_patient_org_guid_from_request()` from an `X-Patient-Org-Guid` header or the body's clinical context in either payload shape (the #820 both-shapes rule). Fixtures reworked: they used ONE value for performer and patient org, so a filter on either column passed identically — `PATIENT_ORG` is now deliberately distinct from `ORG`, which is what makes the new tests discriminate. 3 new tests: the row is visible to a patient-org reader and invisible to a performer-org reader (negative-tested — reverting the filter to `org_guid` fails it with 0 != 1), a row with no patient_org_guid is admin-only, and the header form of the write. KNOWN ASYMMETRY recorded in the tests: `/api/v1/fhir/events` still scopes on `change_feed.org_guid` because that table has no patient_org_guid column, so a reader can see feed events for rows they cannot read. 293 green. NOT YET DEPLOYED.

## 2026-10-21 — icd11.pdhc added as a FHIR write identity (step 7.a of the icd11 plan)

/Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/auth.py
/Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/__init__.py
/Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/.env.example

## 2026-10-31 — #857 (b): a service key may not assert an ICD-11 diagnosis

Operator chose (b) after (a)'s real cost was measured. Repo only — nothing
deployed.

Edited:

- `cdr_app/app/auth.py` — `ICD11_ASSERTING_SERVICES` (a frozenset, **empty**),
  `ICD11_WHO_SYSTEM`, `ICD11_CANONICAL_LIB`, `icd11_assertion_refused_for()`
  and `_codings_in()`. Placed beside `KNOWN_FHIR_SERVICES` and
  `READ_ONLY_SERVICE_SOURCES` on purpose: CLAUDE.md's own lesson is that an
  auditor must already check two dicts here, and a third rule in a third file
  makes that worse.
- `cdr_app/app/api/fhir_write.py` — the refusal is evaluated in `_write_one`,
  the one path POST, PUT and every Bundle entry share, and BEFORE
  canonicalisation so a refused write costs no upstream round trip. 403, not
  the 422 the plan_miss/xlate_miss paths use: the code may be perfectly valid
  and still not this caller's to assert.
- `cdr_app/tests/test_icd11_assertion_gate.py` — NEW, 11 tests.

### Why the set is empty, and why that is not a placeholder

Measured 2026-10-31 across cdr1, cdr2 and cdr3: `fhir_resources` holds **zero
Condition rows**. Only Observations — 1,942 + 7 on cdr1 (sim, gateway), 52,131
on cdr2, 19,509 on cdr3, all sim. So no legitimate producer is affected.

And `icd11.pdhc` is not in `KNOWN_FHIR_SERVICES` at all, so "only icd11.pdhc
may write these" and "nobody may" are the same rule today. The empty set is the
honest spelling. When icd11.pdhc's projection goes live it gets a key, an entry
in the first list, and its name here — and adding it to the first without the
second refuses its writes with a message naming this constant, which is the
intended failure.

### Two decisions inside the guard

- **The coding walk is recursive, not a path list.** A Condition carries
  codings under `code`, `bodySite`, `severity`, `stage.summary` and
  `evidence.code`, and a Bundle nests them deeper. Reading known paths is how a
  guard ends up checking the one place the producer did not use. Depth-bounded
  at 12.
- **Both spellings are matched.** WHO's `http://id.who.int/icd/release/11/mms`
  arrives on a raw FHIR resource; the slug `icd11-mms` arrives inside a
  termbank canonical URI, which is what `parse_canonical_uri` hands to plan.
  Only one of them looks like ICD-11 at a glance.

### Verified

304 passed (was 293). Control: the rule was disabled and **7 of the 11 went
red while 4 stayed green** — the 4 being exactly the ones that should pass
without it (a non-ICD-11 coding, ICD-10, a human caller, and the assertion that
the allowlist is empty). One test posts a real Condition through the HTTP route
and asserts 403, because asserting the predicate is not asserting the guard
runs.

### What this does NOT do

Option (a) — cdr validating the code against termbank's used-concept record —
would close the hole whatever key the caller holds. It is not done, and the
cost was measured rather than estimated: this service has **no termbank HTTP
client**, only URI strings and comments, so it means a new client (patterns
exist in `plan_client`, `xlate_client`, `cambio_client`), new configuration,
and putting termbank — a loopback-only service with no public subdomain — on
the clinical write path. Tracked against the #868 gate: before real patient
data.

### Found on the way

`cdr_app/app/api/fhir_read.py:11` advertises
`POST /CodeSystem/$lookup — proxy to termbank.pdhc`. **That route does not
exist anywhere in this service** — the line is the only `CodeSystem` mention in
the file. Same class as #804: a docstring advertising a capability the module
does not have. Not fixed here; it is unrelated to #857 and deserves its own
look at whether the proxy should exist or the line should go.

### Follow-up, same day: the $lookup line is fixed

Recorded above as "not fixed here". Done now, and it was three lines rather
than one.

`cdr_app/app/api/fhir_read.py`'s module header advertised:

```
POST /CodeSystem/$lookup       — proxy to termbank.pdhc
POST /ConceptMap/$translate    — proxy to xlate.pdhc
POST /ValueSet/$validate-code  — proxy to plan.pdhc
```

**None has existed since 2026-06-28.** `git log -S` found the deletion:
`6b86e7c`, "#290 SSOT phase 4", −105 lines. They were pure HTTP passthroughs,
cdr held no terminology data, and plan.pdhc has owned terminology since
2026-06-22. The code was right for four months; only the header lied. The
module title said "terminology shims" too, and no longer does.

The three lines are **quoted in the header rather than silently deleted**,
with the commit, the ticket and the date, so the next person reading an old
client or a stale integration note finds the reason here instead of concluding
the routes were lost by accident.

Checked before and after:

- Zero route definitions matching `CodeSystem`, `ConceptMap` or
  `validate-code` anywhere in `cdr_app/app` — none has been re-added.
- Nothing on the platform calls cdr's removed endpoints. The eight files
  mentioning those paths are termbank's **own** `$lookup` route
  (`termbank.pdhc/app/__init__.py:106`) and documentation about it. termbank
  is the owner, so that is correct.
- 304 passed, unchanged.

My first route check here was sloppy and is worth recording: it counted files
containing both a route decorator and the word, not actual route definitions,
and reported 3 / 1 / 1. Redone against `@bp.(post|get|route)(` patterns: zero.
A count is not a measurement unless it counts the right thing.

## 2026-11-01 — #872: record WHY a code was accepted, not only that it was
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/models/resources.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/models/__init__.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/canonicalisation.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/fhir_write.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/migrations/versions/c8d9e0f1a2b3_whole_system_accept_audit.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_whole_system_accept_audit.py

plan.pdhc answers `$validate-code` per code, EXCEPT for a canonical library
flagged `whole_system_answer_space` — then it answers `result: true` for any
code in the system, because ~85 000 ICD-11 codes picked at the point of care
cannot be pre-enumerated. plan has said so on the wire since #857: a
`whole_system` boolean plus a message naming icd11.pdhc as the per-code
existence checker.

**Measured 2026-11-01: nothing read it.** Zero readers across the workspace.
`PlanClient._parse_parameters` copies every boolean in the response by name, so
the key arrived in cdr's own dict and no code inspected it. Nothing was let
through wrongly — but an ICD-11 diagnosis and a planned LOINC measurement were
indistinguishable in this repository's record of *why* each was accepted.

Now: `cdr_audit_whole_system_accept`, deliberately the mirror of
`cdr_audit_plan_miss` — same columns, same one-row-per-canonical dedup with
`seen_count`, same recorder shape. One table says what plan refused; the other
says what plan accepted without being asked about the specific code. Read
together they answer "on what basis is this code in the repository?".

Threaded through the canonicaliser by passing a list INTO
`_canonicalise_codeable_concept` rather than widening its return type: the
method has 17 `return` statements and one call site, so a 4-tuple would have
touched every one of them for nothing.

Both accept sites are wired, which is the easy thing to half-do: the
termbank-prefixed branch AND the xlate-translated branch. The second has its
own test for that reason.

**The controls are the content.** Three of the 11 tests assert that a per-code
acceptance records NOTHING, and one asserts a refusal records nothing. Proven
by disabling the rule — `if accepts is None` instead of also checking
`whole_system` — which turned exactly the two per-code controls red (2 failed,
9 passed) and nothing else. Restored: 11/11, and 315/315 for the full suite.

The first run of the write-path control failed on my own test setup, not on the
code: the fake canonicaliser returned the same `primary_canonical_uri` in both
tests, so the second POST deduplicated against the first row and returned 200
instead of 201. Fixed by parameterising it; the fake now documents why.

Migration applied, reverted and re-applied against real Postgres
(`cdr_pdhc_db`, database `cdr_db`): table created with
`uq_whole_system_canonical`, dropped on downgrade, recreated on upgrade, head
`c8d9e0f1a2b3`. NOT deployed to production.

Additive only, and nothing is backfilled: rows written before this migration
have no recorded basis and cannot get one. That is worth knowing the first
time the table is read — an empty table does not mean no system-level
acceptances happened.

---

## 2026-11-02 — #874: the assertion gate asks plan which libraries are
## whole-system, instead of naming one

- `cdr_app/app/services/plan_client.py`
- `cdr_app/app/auth.py`
- `cdr_app/app/api/fhir_write.py`
- `cdr_app/tests/conftest.py`
- `cdr_app/tests/test_whole_system_assertion_gate.py` (renamed from
  `test_icd11_assertion_gate.py` via `git mv`, so history follows)
- `cdr_app/tests/test_whole_system_accept_audit.py`

**What was wrong.** #857 shipped a gate that named ONE canonical library,
`icd11-mms`. #872 flagged `icd10` twelve days later and the gate did not
notice. A rule written against a library's NAME goes stale the moment a second
library is flagged, and cdr had no way to learn that it had.

**The fix: ask, don't copy.** plan.pdhc publishes the canonical-library table —
including `whole_system_answer_space` — at `GET /api/v1/lookup/canonical-libs`,
and the GET is unauthenticated (verified against production 2026-11-02; the
POST/PUT siblings are not). `PlanClient.whole_system_systems()` fetches it
through the existing TTL cache, so it is one GET per minute for the process,
not one per request.

**`WHOLE_SYSTEM_LIBS_FLOOR` is the part worth defending.** plan may WIDEN the
flagged set; it may not narrow it. Without a floor an unreachable *or merely
misconfigured* plan would silently disarm the gate while writes kept
succeeding — open at exactly the wrong moment. The floor is `icd11-mms`,
i.e. the level #857 shipped at, so this cannot regress below it. The client
returns `None` for "plan did not answer" rather than `{}`, because an empty
mapping would read as "nothing is flagged", which is permission.

**Resolution is EXACT, never substring.** The predecessor matched
`f"/{lib}/" in system`. A substring test cannot tell `icd10` from `icd10-se` or
from `.../ValueSet/icd10` — the same defect #804 recorded in gateway's path
guard. Four parametrised lookalikes pin it.

**A url is used only when exactly one library holds it.** Measured in
production: `icd10` carries a copy of `snomed`'s `canonical_lib_url` (#875).
Matching on url alone would have resolved a SNOMED coding to a FLAGGED library
and refused legitimate SNOMED writes. Dropping non-unique urls makes that
collision unrepresentable here instead of a latent surprise.

**Still broader than the canonicaliser, on purpose.** The authoritative signal
would be #872's `whole_system_accepts`, but that only covers the paths
`_CODE_PATHS` walks (`Condition.code`, `Observation.code`, …). A fabricated
classification code in `bodySite`, `severity`, `stage.summary` or
`evidence.code` never reaches it. So the gate keeps walking every coding and
runs BEFORE canonicalisation.

**Policy: every flagged library admits nobody.** `WHOLE_SYSTEM_ASSERTING_SERVICES`
is an empty mapping with an empty default. `icd11-mms` will list `icd11.pdhc`
when its Condition projection goes live; `icd10` has no entry-point validator
at all, so there is no service that could honestly be trusted to assert one.
Permission is per library — a test pins that permitting `icd11.pdhc` for
`icd11-mms` earns it nothing for `icd10`.

**Hermeticity.** The gate now calls plan on every service-key write, and
fourteen test files send one. A conftest autouse fixture stubs the client to
return `None`, so the suite exercises the FLOOR by default and makes no network
call — the same problem `_consent_allow_all` exists for. A regression that
stops the floor refusing `icd11-mms` therefore turns tests red rather than
passing quietly.

**The #872 write-path test had to grant itself permission**, and that is the
two rules meeting: the gate decides who may put such a code in, this table
records that it got in on SYSTEM grounds. Rows come from human (SSO) writers
and from explicitly permitted services. Written into the test rather than left
as a coincidence.

33 tests (was 11). Controls proven by deliberate breakage:

| breakage | red |
|---|---|
| ignore plan's answer (the #874 regression itself) | 3 |
| remove the floor | 2 |
| substring instead of exact match | 4 |

Control 2 left the WHO-URI parametrisation green, correctly: that spelling is
carried by `WHOLE_SYSTEM_SYSTEM_ALIASES`, a separate mechanism from the floor.

**337 passed.** NOT deployed — and the deploy is larger than this change,
because #857's gate has never been in production (0 occurrences in the running
cdr1) and `auth.py` also carries the `icd11.pdhc` write identity, which needs
an operator-set `ICD11_PDHC_SERVICE_KEY`.

**Blast radius, measured 2026-11-02 across all three CDRs** — and measured on
resources, not on a Condition count, because the gate applies to every type:

| | cdr1 | cdr2 | cdr3 |
|---|---|---|---|
| fhir_resources (all Observations) | 1 949 | 52 131 | 19 509 |
| body contains `icd` | 0 | 0 | 0 |
| body contains `id.who.int` | 0 | 0 | 0 |
| `observation.code_canonical` contains `icd` | 0 | 0 | 0 |

Positive control: 1 949 / 52 131 / 19 509 bodies contain `coding`, so the
searches ran. No producer has ever sent a classification code to any CDR, so
turning the gate on refuses nothing that currently happens.

## 2026-11-04 — #874: the ticket was wrong on both counts, and the floor was the real gap

- cdr_app/app/auth.py — EDIT. `WHOLE_SYSTEM_ASSERTING_SERVICES` gains its FIRST
  entry that admits anybody: `{"icd10": frozenset({"sim.pdhc"})}` (operator
  decision). `WHOLE_SYSTEM_LIBS_FLOOR` gains `icd10`.
- cdr_app/tests/test_whole_system_assertion_gate.py — EDIT. 33 -> 43 tests. The
  emptiness guard became a grant guard keeping its original insight; two tests
  switched their caller from `sim.pdhc` to `gateway.pdhc` so they keep measuring
  what they meant to measure rather than becoming tests of the grant.

**#874's two stated problems are both FALSE, measured 2026-11-04.**

| it claims | measured |
|---|---|
| "the gate is not deployed AT ALL" | **deployed.** Image built 2026-11-02T06:42:27Z, container started 06:43:02Z — the same day the ticket was filed. Importable in the container, and WIRED: `app/api/fhir_write.py:44` imports it, line 156 calls it |
| its evidence: `grep -c icd11_assertion_refused_for` -> 0 | the symbol is `whole_system_assertion_refused_for`. `icd11_assertion_refused_for` exists **nowhere** — not locally, not deployed, not in any commit. The probe measured a name that never existed |
| "when deployed it still will not cover icd10" | **false.** `flagged_systems()` asks plan and merges a floor; plan publishes **icd10 AND icd11-mms** as whole-system answer spaces on the live unauthenticated `/api/v1/lookup/canonical-libs`. icd10 is covered dynamically |
| the ICD-11-only matcher it quotes | already replaced by exact matching that cites #804 and drops non-unique urls for #875 |

This is #804's lesson a second time: searching for a symbol is not checking for
a capability. The note in this file dated 2026-11-02 saying the gate was "NOT
deployed ... 0 occurrences in the running cdr1" is stale by hours — the image
that carries it was built at 06:42 that morning.

**The one REAL gap, and it was not in the ticket.** The live floor is
`{icd11-mms}` only. #872 flagged `icd10` in plan production on 2026-11-02 and
did not add it to the floor, so a plan outage left EVERY service identity free
to assert any ICD-10 code — exactly the window the floor exists to close, left
open for the one library that had just been flagged. Proven test-first: the
guard failed before the fix, with "a plan outage disarmed the gate for icd10".

**The grant was NARROWED from the decision as drafted.** It covered icd10 and
icd11-mms; sim emits **zero** ICD-11 codings (0 references against 22 for
icd10, measured with a working control), so icd11-mms would have been a right
with no consumer. sim's `concepts/diabetes_v1.yaml` uses only ATC, LOINC and
Snomed CT as `canonical_lib`; ICD-10 enters solely via `icd10_alt`.

**Why sim at all.** Its diagnosis codes are hand-curated real codes dual-coded
with SNOMED PRIMARY: 46635009/E10, 44054006/E11, 38341003/I10. SNOMED is not a
whole-system answer space, so what tripped the gate was a correct ICD-10
cross-reference on a properly-adopted resource — the gate walks every coding by
design, which is right, and is why the entry is needed rather than a reason to
loosen the walk.

**Blast radius: still zero.** The 2026-11-02 measurement in this file holds —
no producer has ever sent a classification code to any CDR (0 of 1 949 / 52 131
/ 19 509, with a positive control on `coding`). So neither the grant nor the
floor change alters anything that happens today; both are forward-looking.

347 tests pass (the gate suite 33 -> 43; the rest unchanged). Four mutations —
grant revoked, over-granted by library, over-granted by identity, icd10 dropped
from the floor — fail 5, 10, 5 and 2 tests.

**NOT DEPLOYED.** The deploy needs a `--build`: cdr1 has no bind mount, so
`/app` is baked. Measured before stopping: all 11 requirements are pinned, so
the rebuild is not also a dependency upgrade (#785); and 30 of 31 `.py` files
under `app/` are byte-identical host-to-container. The one that differs is
`app/__init__.py` line 18 — a `DATABASE_URL` DEFAULT that is dead code because
the container sets the env (len=78). The host copy carries
`change-me-in-production` and not the dead original; the container carries the
dead 8-char dev default (#848) and no placeholder. So the running image predates #848's source fix, and a
rebuild would REMOVE that dead literal from the image rather than
risk anything.

## 2026-11-04 — #935 (cdr half): the #874 gate reads a route that exists for it

- cdr_app/app/services/plan_client.py — EDIT. `WHOLE_SYSTEM_LIBS_PATH` points
  at `/api/v1/lookup/whole-system-libs` instead of the whole `canonical-libs`
  table.
- cdr_app/tests/test_whole_system_assertion_gate.py — EDIT. 43 → 49 tests.

**Why it matters, and it is not performance.** Before this, the #874
whole-system assertion gate read plan's ENTIRE canonical-libs table, and that
worked because the table happened to be open. So a **deployed security control
depended on an accident**: gating `canonical-libs` would have degraded this gate
to its hard-coded floor — still refusing `icd10` and `icd11-mms` from the floor,
but silently no longer following plan for anything flagged later.

plan now publishes `/api/v1/lookup/whole-system-libs` FOR this purpose (#935),
with the three fields the gate reads and nothing else, documented as public by
decision. The dependency is explicit rather than incidental.

This client still sends only `X-Operator-Session-Id` (#408) — session
propagation, not authentication. #931 is the ticket for giving plan a usable
machine-auth door; until then an unauthenticated read is the only read
available, and plan's route now says that is deliberate.

### The uniqueness guard is KEPT, and it is now weaker — stated, not dropped

`whole_system_systems` drops any url held by more than one library in the
response. #875 made `canonical_lib_url` UNIQUE in plan (verified in production
2026-11-04: `uq_canonical_libs_canonical_lib_url`, 0 duplicates), so the
collision is unrepresentable at the source — the tempting conclusion is that the
guard is now dead code.

It is not, and it is also no longer as strong. On the full table it could spot a
FLAGGED library sharing an UNFLAGGED one's url, which is exactly the case #875
was filed about (`icd10` holding `snomed`'s). On the narrow read it sees only
the flagged rows and cannot.

Kept anyway, for one reason: **a security decision should not depend on a remote
invariant holding.** If two flagged libraries ever collided, the guard drops both
urls and falls back to name-only matching, which still resolves plan's own
`icd10`/`icd11-mms` spellings — it degrades rather than misfires. Removing it
would buy nothing and would make cdr trust a constraint it cannot see.

The flagged filter is likewise belt-and-braces now: plan's route returns only
flagged rows, but the client should not break if that projection ever widens.

### Checked before touching production

cdr's container reaches the new route over `https://plan.pdhc.se` — its real
path, through the reverse proxy — with 200. The repoint cannot work otherwise,
and that is a cheaper thing to measure than to discover. 31 of 31 `app/*.py`
byte-identical host-to-container, 11 of 11 requirements pinned (#785).

353 tests pass (was 347). Three mutations — the path reverted, the uniqueness
guard removed, the flagged filter removed — fail 2, 2 and 4. The keeper asserts
the URL **on the wire**, not the constant: a constant nobody uses would pass a
test of the constant.

**DEPLOYED AND VERIFIED 2026-11-04.** In the container, against the LIVE narrow
route: `flagged_systems()` returns the same five mappings as before the repoint
— `icd10`, `icd11-mms`, both termbank urls, and WHO's
`http://id.who.int/icd/release/11/mms` — the floor is intact, and #874 behaves
unchanged (gateway + icd10 REFUSED, sim + icd10 permitted via its #874 grant,
gateway + SNOMED permitted). Control: with plan silent the floor still refuses.
`/healthz` 200, sibling smoke 18/18.

#869 — ONE canonical-URI rule, and ICD-11 reaches it (2026-11-05)
-----------------------------------------------------------------
/Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/plan_client.py
/Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/canonicalisation.py
/Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/resource_writer.py
/Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/auth.py
/Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_canonical_uri_one_rule_869.py  (new)

375 tests pass (was 353). NOT DEPLOYED (#939) — cdr1/cdr2/cdr3 are three separately
deployed codebases (`infra_cdr_prod_behind_local_git`), and the Mac had no
route to miserver when this was written.

THE TICKET ASKED FOR A THIRD CANONICAL FORM. There must not be one.
`https://termbank.pdhc.se/CodeSystem/icd11-mms/<code>` is Path A unchanged, it
was already in this suite's own fixtures, termbank owns the vocabulary
(icd11.pdhc's Rule 18), and plan already knows `icd11-mms` at SYSTEM
granularity (#874) so an 85,000-code classification needs no row per code.

The four decisions the ticket asked for, ratified by the operator:
  1. stored shape  — termbank form, i.e. no new form at all
  2. release       — NOT in the canonical. icd11 puts it in `coding.version`
                     (its Rule 28), so the identity key is release-free and
                     two statements of one entity collide for dedup/search
  3. postcoordination — the FULL cluster expression, unescaped. Measured:
                     `parse_canonical_uri`'s refnumber group is `(.+)` not
                     `([^/]+)`, so `BD13&XT8W&XS3A/CA60.3` round-trips
  4. one rule      — in plan_client, beside the two parsers

WHAT WAS ACTUALLY BROKEN was a level deeper than the ticket looked. It named
the two READER predicates (stats.py:26, clinical_read.py:160) — those are a
DIFFERENT rule (plan-concept Path B) and are untouched; a test pins that an
ICD-11 canonical is correctly "not a plan concept" to both. The real split was
the BUILDER, in two copies:

    canonicalisation.py::_coding_to_canonical_uri   (ingest)
    resource_writer.py::_coding_canonical_uri       (storage, with the
                                                     termbank prefix as a
                                                     LITERAL, not even the
                                                     shared constant)

so a system either had learned about was unknown to the other. Both now
delegate to `plan_client.canonical_uri_for`. The two parsers there already
carried the note "a second copy somewhere else is how the two drift apart" —
it just did not cover the builder.

AND THE WHO SYSTEM WAS ALREADY IN THE CODEBASE, in the wrong layer.
`auth.py` has held `ICD11_WHO_SYSTEM` -> `icd11-mms` since #874 for the
whole-system assertion gate, with the comment "WHO's own URI is what appears
on a raw FHIR Condition from a real producer". The canonicaliser did not know
it, so a WHO-coded Condition fell to the xlate branch — and xlate is deployed
NOWHERE (#833) — and was stored with `code_canonical` NULL: unsearchable by
code, weakly deduplicated. The alias now lives in plan_client and `auth`
imports it, so the gate and the canonicaliser cannot disagree. A test asserts
they are the SAME object.

`SYSTEM_ALIASES` is deliberately tiny and its docstring says why: a system
belongs there only when the mapping is a pure RENAME with no code
transformation. Anything needing the code itself changed is xlate's job.

ORDER IS NOW A DOCUMENTED RULE. The first canonicalisable coding wins, so a
producer sending several must put the ASSERTED statement first. icd11 sends a
cluster and sometimes a pre-coordinated `simplified_to` equivalent, which its
own comment calls "additional information, not a replacement" — so the
cluster must come first or cdr would store the simplification as the row's
identity. Pinned, with a control that reversing the order picks the other, so
the test measures order rather than a cluster preference no code implements.

5 mutations, all caught — fail 9, 6, 3, 2, 1. The "one builder" test asserts
on the PARSE TREE, because the docstrings written for #869 quote the termbank
prefix to explain it and a substring search would match the prose explaining
the fix (the #885/#904/#912/#924 instrument error).
