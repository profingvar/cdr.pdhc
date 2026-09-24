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

---

## #468 / #462 D6 — care-delivery read surface for the clinical dashboard (2026-07-13)

The rebuilt dashboard.pdhc (#462) reads CDR1 under a CARE-DELIVERY basis,
not analysis-consent. Two facts shaped the design:
  1. #422 (check_patient_allowed) ALREADY passes through for service-key
     callers (_operator_blob returns None when service_source set) — so a
     dashboard service-key read is already consent-bypassed = the
     care-delivery basis for consent. No change needed there.
  2. BUT the service blob is is_su_admin=True, so fhir_read._org_filter
     short-circuits and returns ALL orgs — it ignores X-Org-Guids. Reusing
     it for a care-delivery read would leak every org's patients.

New blueprint `api/clinical_read.py` (mounted /api/v1/clinical) does its OWN
explicit org scoping from X-Org-Guids / X-Is-Admin, and requires the caller
to be the dashboard.pdhc service declaring X-Access-Purpose: care-delivery:
  - GET /api/v1/clinical/patients — org's patients that HAVE data, with
    name/birth_date (from patient table) + observation_count +
    last_observed_at, most-recent-activity first. Feeds #465 picker.
  - GET /api/v1/clinical/patient/<guid>/summary — per-concept
    (code_canonical) counts + first/last + unit, count desc. Feeds #466
    sorted parameter dropdown.
Spärr enforced dashboard-side (operator #469 Q1); CDR-side spärr
(defense-in-depth) deferred. /api/v1/clinical added to _is_read_path so
CDR_READ_LOCKDOWN admits dashboard.pdhc.

Tests: test_clinical_read.py 7/7. Full suite 131 passed. NOT deployed —
cdr1 already has DASHBOARD_PDHC_SERVICE_KEY in its config (auth.py
KNOWN_FHIR_SERVICES); a deploy needs the key value present in cdr1's .env
and matching dashboard's DASHBOARD_PDHC_SERVICE_KEY.

## #464 — CDR1 care-delivery series endpoint (2026-07-13)
Added GET /api/v1/clinical/patient/<guid>/series to clinical_read.py: the
actual time-series points (code_canonical, effective_at, value_quantity→float,
unit, value_string, org_guid), filtered by ?code (repeatable) + ?from/?to
(effective_at ge/le), ordered oldest→newest, capped (10k default/50k max).
org_guid on every point so the dashboard applies spärr on its side (#469 Q1).
Same care-delivery guard + org scoping as the #468 endpoints. Tests 11/11 in
test_clinical_read.py; full suite 135 passed.

## #471 item 5 — concept display names via plan.pdhc (2026-07-15)
LIVE-DATA finding (read-only query of cdr_pdhc_db, operator-approved): prod
code_canonical is dominantly `urn:pdhc:concept/<concept-guid>` (7064/7065 rows;
14 distinct codes; ZERO termbank URIs). The concept GUID is embedded in
code_canonical — so display resolution is: parse the guid, then plan.pdhc
CodeSystem/$lookup → display (cached in PlanClient, FAIL-OPEN). Added
PlanClient.lookup_display + clinical_read.resolve_display; the summary endpoint
now returns a `display` per parameter (dashboard dropdown/legend uses it, falls
back to the raw code). Skips entirely when PLAN_BASE_URL is unset (tests/
plan-less envs). Tests 15/15 in test_clinical_read; full suite 139 passed.
Remaining #471: item 1 (retire legacy view, blocked #469 Q6), item 2 (#212
re-home, needs legal #437), item 4 (spärr lift refinement — now EASY since the
guid is embedded in code_canonical: parse → compare to lift_concept_guids; still
legal-sensitive, deferred).

---

## 2026-09-23 — #664 + #665: enablers for the analyse.pdhc reconstruction

Both raised by analyse.pdhc AN-0 discovery (#642). 159 tests pass, up from a
clean 142 baseline. NOT DEPLOYED.

**#664 — a registered analysis service may declare a read purpose.**
`analysis_consent._operator_blob()` returned None for any service-key caller,
so every machine read passed the #422 consent gate untouched. That reasoning
("a machine identity has no role to derive a purpose from") holds for
dashboard.pdhc and sim, which read under a sibling's operator context. It does
not hold for an analyse node, whose purpose is an explicit parameter of the
analysis spec.

`declared_service_purpose()` reads `X-Access-Purpose` from a machine caller
and, when present, filters on it. Both entry points (`consent_allowed_guids`,
`check_patient_allowed`) now go through one `_resolve_purpose()` rather than
each testing the blob themselves.

The safety property: **declaring can only ever reduce access**, because the
alternative is the pass-through. A caller that sends no header behaves exactly
as before, so dashboard.pdhc and sim are untouched.

Three guards, each with a test:
- Only the SECONDARY purposes are declarable (research, statistics,
  quality_registry). `administration` is never blocked by ips, so allowing a
  service to declare it would turn the gate into a bypass.
- `research` requires `X-Research-Project-Guids`; consent is per project.
- A human operator cannot declare — the header is a machine affordance, and an
  operator's purpose comes from their role. Otherwise a header would let them
  pick a softer purpose than their role implies.
- ips down still fails closed (503) for a machine, exactly as for an operator.

**#665 — `clinical_context.author_org_guid`.** The platform had fields for who
ORDERED (`requesting_org_guid`) and who SUBMITTED (`provider_org_guid`) but
none for who AUTHORED. Migration `a1b2c3d4e5f6`, additive and nullable.

Two deliberate non-decisions, both from analyse.pdhc ADR-0006:
- **No backfill.** `author = provider` is a guess that becomes
  indistinguishable from a declared fact once written. NULL means unknown.
- **No `performer` fallback in provenance.** On this platform gateway stamps
  the authenticated SUBMITTER into FHIR `performer`, so filling author from
  performer would recreate exactly the conflation the field exists to end.

The asymmetry worth repeating: `provider_org_guid` is AUTHENTICATED (from the
PAT, unfalsifiable); `author_org_guid` can only be DECLARED, because only the
submitter knows. Anything filtering on it is trusting the submitter.

**Test-isolation note.** `tests/test_ingest.py` asserts on
`IngestRaw.query.first()` and `OpenEhrComposition.query.first()`, which only
mean anything when those tables hold one row. The new `test_author_org.py`
creates ingest rows, so it wipes the ingest tables before AND after itself. It
neither inherits another test's rows nor leaks its own; `test_ingest.py` was
left alone.

**Operator note for deployment:** the migration runs against every CDR
instance (cdr.pdhc, cdr1-5, cdr_6). Additive and nullable, so it is safe
instance by instance, and nothing reads the column until gateway.pdhc #666
starts populating it.

---

## 2026-09-23 — #664 + #665 DEPLOYED to cdr1 (cdr.pdhc)

Live and functionally verified. `healthz` 200, alembic head `a1b2c3d4e5f6`,
`clinical_context.author_org_guid` and its index present.

Verified in the running container, not just deployed: a service declaring
`statistics` is filtered; a service declaring nothing still passes through
(dashboard.pdhc and sim unaffected); `administration` is refused with 400;
`research` without project guids is refused with 400.

**The deploy method matters, and nearly went wrong.** Checksumming the deployed
tree against local found **six** differing files, not the four I changed —
`app/__init__.py` and `app/auth.py` differ too. Those carry the **#541
federation wiring**: `ANALYSE_PDHC_SERVICE_KEY`, `analyse.pdhc` as a trusted
service identity, and `_ANALYSE_READER_SOURCES`. **Local git has it; cdr1
production does not.**

A wholesale file copy would have silently admitted analyse.pdhc as a trusted
reader on cdr1 — a real authorisation change nobody asked for, shipped as a
side effect of an unrelated deploy.

So: the four files I changed were each verified **byte-identical to HEAD~1**
before copying, which proves copying them applies my change and nothing else.
The two divergent files were left untouched, and there is an explicit
post-copy check that `auth.py` still lacks the #541 wiring.

That is a better instrument than the surgical string edits
`infra_cdr_prod_behind_local_git` prescribes — it is deterministic and
verifiable — but only because the baseline was *proven* first. Where a file
does not match its baseline, the surgical rule still stands.

**NOT deployed to cdr2–cdr5 or cdr_6.** Those are the instances that memory
records as behind local git, and there is no urgency: nothing reads
`author_org_guid` until gateway #666 populates it, and nothing sends
`X-Access-Purpose` until an analyse node exists, which needs transport that is
not built. Doing five divergent instances for zero current benefit is risk
without return. They should be done as their own scheduled piece of work,
each baseline-checked the same way.

**Open, and it predates today:** cdr1 is missing the #541 wiring that cdr2–5
have. Whether that is deliberate (analyse federates cdr2–6, not cdr1) or drift
is worth settling — it is exactly the kind of gap that is invisible until
something fails.

---

## 2026-09-23 — cdr1 read wiring brought in line with cdr2–5 (#541 gap)

Operator: the wiring to get data out of cdr1 should be identical to every
other CDR. It was not, and that is now fixed and verified.

**What the gap actually was.** Diffing cdr1's `auth.py` against cdr2's showed
divergence in BOTH directions, which is why it had gone unnoticed:

- cdr1 has things cdr2 lacks and *should* lack — the `gateway.pdhc` write
  identity (patient-demographics upsert) and the `/api/v1/clinical`
  care-delivery read surface. Both are cdr1-specific and correct.
- cdr2–5 have the **#541 analyse wiring** that cdr1 lacked: `analyse.pdhc` as
  a trusted service identity, and its inclusion in the read-lockdown
  allow-list.

So a federated read that worked against every other CDR failed **only** on
cdr1 — the CDR that matters most, since it is the primary analysis record.

**Three surgical edits**, chosen so cdr1 keeps what is legitimately its own:
`analyse.pdhc` added to the trusted-services map *alongside* `gateway.pdhc`;
the read-lockdown check widened from `!= "dashboard.pdhc"` to
`not in ("dashboard.pdhc", "analyse.pdhc")`; and `ANALYSE_PDHC_SERVICE_KEY`
read from config as cdr2–5 do. The key was copied from cdr2 and verified to
hash identically across cdr1, cdr2, analyse and the running container.

**Verified functionally, not just deployed:** `analyse.pdhc` against cdr1
`/api/v1/stats` returns 200 where it previously could not authenticate, a
wrong key still returns 403, and cdr2 returns 200 for the same call.

**Deliberately NOT applied:** local git's `_ANALYSE_READER_SOURCES` refactor.
It exists in HEAD but on **no deployed CDR**, so shipping it to cdr1 alone
would have created a *new* asymmetry while fixing an old one. The deployed
form on cdr2–5 is what cdr1 now matches.

**Still open:** all five instances run an older form of this code than local
git. A proper reconcile-prod-to-local for cdr2–5 remains the follow-up it has
been since #541.

---

## 2026-09-23 — #689: #664 + #665 rolled out to cdr2–cdr5

Deployed and verified on all four. Each: alembic head `a1b2c3d4e5f6`, a
single head, `clinical_context.author_org_guid` present, `#664`'s
`declared_service_purpose` live **inside the container**, `/healthz` 200,
zero errors in the log. 41 containers on the mini, none unhealthy.

The consent-gate asymmetry is closed: a federated analysis query fanning out
across cdr1–cdr5 is now consent-enforced on every node rather than on one.

### cdr_6 is NOT in scope, and the ticket was wrong to include it

Established before touching anything:

- Its tables are `cdr_6_observations` / `cdr_6_read_audit`. **There is no
  `clinical_context` table**, so #665 has nothing to add a column to.
- Its alembic lineage is entirely separate — head `0006_add_observation_unit`,
  numbered migrations `0001`–`0006`, not the hash lineage cdr1–5 share.
  Applying `a1b2c3d4e5f6` there would fail on a missing `down_revision`.
- Its `analysis_consent.py` is a different, shorter file (110 lines) whose
  `_operator_blob()` does **not** contain the `service_source` bug #664
  fixed — it has no service-caller branch at all.

cdr_6 is a different service that shares a name prefix. Whether a service-key
caller can reach its consent gate is a real question, but a different one;
ticketed separately rather than forced through here.

### Method

The standing rule for these boxes is *never file-overwrite deployed cdr2–5
source* — a wholesale copy previously dropped `clinical_read_bp` and produced
a crash-loop. So:

- Verified cdr2–5's `analysis_consent.py` and `ingest_pipeline.py` were
  **byte-identical to local's pre-#664 versions** (`1cd93ab`), i.e. no
  server-only edits to lose.
- Copied **three files only**. Confirmed afterwards that `app/__init__.py`
  still has no `clinical_read` on cdr2–5 — that divergence from cdr1 is real
  and was left alone, which is exactly what a wholesale copy would have
  destroyed.
- Checked every model `ingest_pipeline.py` imports exists on cdr2 before
  rebuilding.
- `py_compile` per file with automatic restore of the saved copy on failure.
- `docker-compose up -d --build` (source is baked; no mounts) then
  `flask db upgrade` per container — nothing auto-migrates on boot here.

### Backups

`~/backups/predeploy/cdr2-5/20260923T185955Z/` — pg_dump per CDR, 513–515 MB
gzipped each. Disk checked first (48 GB free) given this box's disk-full
history. `clinical_context` held **0 rows** in all four beforehand, so the
migration added a column to an empty table.

---

## 2026-09-24 — #698: FHIR `value-quantity` search

171 tests pass (+12). **Not deployed.**

`GET /api/v1/fhir/Observation?code=<concept>&value-quantity=ge5` now works.
It filters on the indexed `value_quantity` column, so an analysis node can
narrow a cohort in SQL instead of reading every candidate and discarding most
of them.

FHIR prefix syntax (`eq`/`ne`/`gt`/`ge`/`lt`/`le`, default `eq`) with an
optional `|system|code` unit.

### The safety property

The filter is applied **before** `_consent_filter`, exactly like `code` and
`date` already are. That is safe because the rows that come back are still
consent-filtered: this narrows what is READ without widening what is
RETURNED. A test pins it — with the consent verdict stubbed to allow nobody,
a matching search returns zero rows.

The warning in the ticket was about a different design — one where a *count*
is returned before the consent join. Nothing here does that.

### Two decisions

- **A unit, if given, is matched, never ignored.** Comparing 5 mg against a
  row holding 5 g would be a wrong answer that looks like a right one.
- **A malformed value is a 400, not a silently dropped predicate.** The
  existing `date` filter returns the query unchanged when it cannot parse —
  a pre-existing weakness left alone, but not one to copy. Dropping a value
  predicate returns MORE rows than were asked for and the caller cannot tell
  it from a genuinely wider cohort. Same class of bug as the analyse cohort
  one (#696).

`value-quantity` is declared in the CapabilityStatement **for Observation
only**, since that is the one live table with a numeric value column.
Advertising it everywhere would send a client looking for a bug in its own
request when Patient answers 400.

### Not wired on the analyse side

cdr can now answer the question; analyse does not yet ask it. The push-down
needs care — it must preserve "spärr before read" and agree with
`cohort_criteria`'s ANY-observation semantics, or the two paths will disagree
about who is in a cohort. Ticketed separately rather than bolted on here.
