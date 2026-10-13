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

171 tests pass (+12). **DEPLOYED to cdr1 2026-09-24** — see the deploy note at the end of this entry.

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

### Deployed to cdr1 2026-09-24

`cdr_pdhc_app` rebuilt and verified. Markers live **inside the container**:
`_apply_value_quantity_filter` ×2, `_SearchParamError` ×5,
`_value_search_params` ×2. `/healthz` 200, zero errors, 41 containers on the
mini, none unhealthy. Data intact (7420 observations, head `a1b2c3d4e5f6`).

`/api/v1/fhir/metadata` is SSO-gated (302), so the CapabilityStatement
content was not re-verified on the server; it is covered by
`test_capability_statement_declares_value_quantity_only_where_it_works`.

The #541 analyse read wiring in `app/auth.py` was checked before **and**
after and is intact — that was a server-side fix, and only the two #698
files were copied.

Predeploy tar: `~/backups/predeploy/cdr.pdhc/app_20260924T091125Z.tar.gz`
Rollback image: `sha256:64109fdda6ba5`

~~**cdr2–5 do not have #698.**~~ — rolled out 2026-09-24, see below.

### A compose warning, investigated and benign

The rebuild printed:

```
volume "cdr_pdhc_pgdata" already exists but was created for project
"cdr_pdhc" (expected "cdr_app")
```

That looks like the compose-project-name class of incident that has cost
data here before, so it was chased down before going further. It is benign:

- cdr1 **deliberately** pins `COMPOSE_PROJECT_NAME=cdr_app` in its `.env`,
  so `cdr_app` is the intended project. The containers already carried that
  label; the rebuild did not change it.
- The volume is declared `name: ${DB_VOLUME:-cdr_pdhc_pgdata}`, an explicit
  name, so it resolves the same under any project. `cdr_pdhc_db` is mounted
  on `cdr_pdhc_pgdata` as it should be.
- The warning is docker comparing the volume's *creation-time* project label
  against the current one.

**One real finding, left alone:** an orphan volume `cdr_app_pgdata` exists,
created **2026-04-10**, used by **no container**, holding **47 MB** (the live
volume is 217 MB). It predates this deploy by five months. Not empty, so not
something to remove without a decision — flagged for the operator.

### #698 rolled out to cdr2–cdr5, 2026-09-24

All four rebuilt and verified: `_apply_value_quantity_filter` ×2 and
`_value_search_params` ×2 **inside each container**, `/healthz` 200, zero
errors, 41 containers on the mini, none unhealthy. `value-quantity` is now
uniform across cdr1–cdr5, so a federated push-down cannot behave differently
per source.

Code-only — no migration. Confirmed unchanged afterwards: every CDR still at
head `a1b2c3d4e5f6` with `clinical_context` untouched.

**Pre-deploy check, which is the part that mattered.** cdr2–5 diverge from
cdr1 (they have no `clinical_read_bp`, and a wholesale copy once crash-looped
them). So both target files were compared first: on all four they were
**byte-identical to local's pre-#698 versions**, i.e. the divergence does not
touch `fhir_read.py` or `fhir_api.py`. Copying local's versions was therefore
safe, and only those two files (plus the test) were copied.

Method as for #689: cdr2 deployed and verified first as a canary, then 3–5
with `py_compile` and automatic restore of the saved copy on failure.

Backups: `~/backups/predeploy/cdr2-5-698/20260924T092836Z/` (api dir per CDR).

---

## 2026-09-24 — #701 (cdr half): a patient allow-list on the FHIR search

178 tests pass (+7). **Not deployed.**

`patient` / `subject` now accept one guid, a comma-separated list, or the
parameter repeated — all meaning "any of these".

**Why this exists, which is the whole point.** An analysis node excludes
spärr-blocked patients *before* it reads, and that ordering is the security
argument rather than a detail. Without an allow-list a candidate query could
not honour it: the CDR would hand back rows belonging to blocked patients
before the node had excluded them. Now the node filters first and passes the
survivors.

Chosen over teaching cdr's search to apply spärr itself (which would make
every FHIR search depend on ips being reachable, changing that endpoint's
failure mode for readers well beyond analyse) and over accepting that a
candidate query reads blocked patients (which is exactly what the codebase
refuses for aggregates).

### Two decisions

- **Repeated parameters are ORed, not ANDed.** Strict FHIR ANDs repeated
  params; on a single-valued subject reference that can only ever match
  nothing, so no legitimate client depends on that reading and unioning
  cannot change an answer that was previously non-empty. Comma-separated is
  the canonical form and is real FHIR OR syntax.
- **An oversized list is refused, never truncated.** A guid plus separator is
  37 bytes, so a few hundred fit in a normal request line; the cap is 500 and
  the error names it so the caller can batch. Silently truncating would
  analyse fewer patients than the node asked about, and nothing downstream
  could tell.

The analyse half (AN-14, #701) is still to do: the node must spärr-filter,
batch, and keep `cohort_criteria` as the authority so the fast path can only
ever be an optimisation of the slow one.

## #718 — observation.effective_at was unindexed; unfiltered searches seq-scanned 2.6M rows (2026-09-30)

Found by analyse's #708 sibling smoke. Every FHIR search in
`api/fhir_read.py` ends in `ORDER BY effective_at DESC NULLS LAST LIMIT n`.
The existing composite `(patient_guid, org_guid, code_canonical, effective_at)`
cannot serve that when no patient is given — the leading columns are
unconstrained — so Postgres seq-scanned and sorted the whole table.

Measured on cdr2, 2.6M rows, with the real call analyse makes when no patient
is selected (`_count=10000`, unfiltered):

| | |
|---|---|
| before | **23,498 ms**, external merge sort spilling **~1.5 GB per worker** — about **4.4 GB of temp writes for one query** |
| after | **118 ms**, Index Scan, no sort at all |

Index is 17 MB per 2.6M-row CDR; 68 MB across the five.

**The disk risk was the serious part.** Five CDRs are queried in parallel, so
one unfiltered analyse page could write ~22 GB of temporary files on a host
with 47 GB free. 2026-05-22 is the precedent for what filling that disk does —
it corrupted Colima's containerd.

**cdr_6 already had an effective_at index** and answered the same query in
6 ms. That is what pointed at the cause: the one CDR that was fast was the one
with the index.

### Applied

`CREATE INDEX CONCURRENTLY` (no write lock, droppable) on cdr1–cdr5 by hand
for immediate relief, then migration `b5c6d7e8f9a0` so it is permanent and a
fresh CDR gets it. All five were at `a1b2c3d4e5f6`, exactly its
`down_revision`, so it applies cleanly and is a no-op index-wise via
`IF NOT EXISTS`.

Plain `CREATE INDEX` in the migration rather than `CONCURRENTLY`, because
alembic runs inside a transaction and `CONCURRENTLY` cannot — and anywhere the
migration actually builds something, the table is new and empty.

### Verified end to end

analyse's smoke went from `mode=error; 1 of 5 ok` at a 15 s timeout to
`mode=complete; 5 endpoints returned data` in **14 ms**.

### Not done

The API still permits an unfiltered `_count=10000` observation search, which is
now fast but is still "give me ten thousand arbitrary rows". Whether that
should be allowed at all is a separate question for cdr and analyse together.

## 2026-10-02 — #708: cdr1 gets a sibling smoke, and it found two things

`cdr_app/deploy/smoke_siblings.py`, built on the pattern #708 names — it drives
cdr's **own clients** (`PlanClient`, `analysis_consent`) rather than hand-written
`curl`, because a curl smoke tests the author's idea of the contract instead of
the code's. Read-only; creates and writes nothing.

cdr1's real outbound siblings are **plan.pdhc** (concept resolution),
**ips.pdhc** (consent verdict) and **sso.pdhc** (token validation). Cambio is an
external third party and is checked for configuration only — a smoke that pokes
someone else's sandbox on every deploy gets switched off. `XLATE_BASE_URL` being
unset is reported as *expected*, not failed: xlate is declared in compose and not
running in production (CLAUDE.md §3), and a smoke that cries wolf teaches the
operator to ignore it.

Run it without a rebuild, which matters because cdr's deployed tree is known to
sit behind local git:
```
ssh miserver 'docker exec -i cdr_pdhc_app python -' < cdr_app/deploy/smoke_siblings.py
```
8 checks. 6 green; **2 real findings on the first run**, which is the whole
argument of #708.

### Finding 1 — cdr1 cannot get a consent verdict for a service-shaped read
`analysis-filter returned 401`. `_analysis_filter` attaches
`Authorization: Bearer` **only if a flask session token exists**, and ips reads
*only* that header (`X-API-Key` is silently ignored). cdr1 holds **no
`IPS_API_KEY`**. So for any caller without an operator session — gateway,
analyse, sim — the #664 consent gate cannot obtain a verdict and fails CLOSED.
Safe, and non-functional: it refuses everything rather than filtering anything.
Filed as its own ticket.

### Finding 2 — 4 of 23 distinct `code_canonical` values can never resolve
`urn:pdhc:concept/smoke-299-c`, `…/smoke-408x2-c`, `…/smoke-c-296`,
`…/smoke3-c` — non-GUID tails, against Rule 18. Same smoke residue as the four
`smoke-*-o` org guids found in `clinical_context` on 2026-10-01. Permanently
un-canonicalisable rows.

### Two wrong turns of mine, kept because the mechanism matters
1. The plan check first took the **first** row with a `code_canonical`, which was
   `urn:pdhc:concept/smoke-408x2-c` — so it failed on cdr's own junk without ever
   reaching plan. It now scans for a usable one, and the residue gets its own
   check. Two distinct facts, two lines.
2. Worse: it used `parse_canonical_uri`, which parses a **termbank** URI, against
   a stored canonical, which is **Path-B** `urn:pdhc:concept/<guid>`. Every row
   returned None and the check reported *23/23 bad*. That number is what gave it
   away — the data was fine and the parser was wrong. Stored canonicals resolve
   through the guid-based door (`resolve_concept` / `lookup_display`). A check
   that fails on 100% of real data is almost always the check.

Now green: `0139525f… → https://plan.pdhc.se/ASTHMA-BREATH`.

### Not deployed
The file is committed but deliberately **not** pushed into the running image. It
runs over stdin, and rebuilding a live patient-data service to add a diagnostic
is not a trade worth making; it will ride into the image on cdr's next real
deploy (#682's clip).

## 2026-10-02 — #732: the 2gate.pdhc ingest identity is removed

`2gate.pdhc` was a **planned second gateway that was never built** —
`progress.md` 6.b is still an unchecked box, `readme.md:20` says "Later 2gateway
will be added" — and it sat in `cdr_app/app/api/auth.py`'s `KNOWN_SERVICES`, the
**INGEST (write)** allowlist, on five production CDRs.

It was never exploitable, and that is worth stating precisely rather than
softening: `TWOGATE_PDHC_SERVICE_KEY` was **empty (length 0)** in all five
instances, and `require_service_key` refuses on `not expected`. Protection by
accident. One populated `.env` — a template, a new instance script, a restored
2026-04-28 `cdr_envs/` backup — and it would have been a live write identity
with no owner and nothing to rotate.

Removed from the dict and from `app/__init__.py`'s config reads.

### The condition that let it hide, also fixed
An allowlisted caller whose config key is unset was refused **identically** to
one presenting a wrong key. So an entry that could *never* authenticate looked
exactly like one nobody had tried yet. `require_service_key` now logs a named
warning for that case before refusing:

```
service-key refused: 2gate.pdhc is allowlisted but TWOGATE_PDHC_SERVICE_KEY is
unset in this instance's config — the entry can never authenticate, so it is
either misconfigured or should be removed (#732)
```

### Tests: 181 pass (was 178)
- `test_2gate_is_not_an_ingest_identity` — pins `KNOWN_SERVICES` to exactly
  `{gateway.pdhc, sim.pdhc}`.
- `test_an_unknown_source_service_is_refused` — 403 `unknown source service`.
- `test_an_allowlisted_caller_with_no_configured_key_is_refused` — the empty-key
  branch.

Both of the first two verified to **fail** with `2gate.pdhc` re-added.

`test_lookup_by_source_id_cross_service_isolation` used `2gate.pdhc` as a proxy
for "another service". It now uses `sim.pdhc`: the test is about namespace
isolation between two *distinct* source services and never needed that one
specifically, so the coverage is kept rather than deleted with the identity.

### A self-inflicted failure worth recording
My first version of the empty-key test did `app.config["GATEWAY_PDHC_SERVICE_KEY"]
= ""`. The `app` fixture is `scope="session"`, so that leaked for the rest of the
run and 403'd **five entirely innocent provenance tests**. Now `monkeypatch.setitem`,
which restores at teardown. Mutating a session-scoped fixture without undoing it
is a test that breaks other tests, and the symptom points anywhere but at the
cause.

### NOT DEPLOYED — needs per-instance care
This touches the ingest path of **five** production CDRs, and cdr prod is known
to sit behind local git: a file-overwrite deploy on cdr2-5 has crash-looped
before (missing `clinical_read_bp`). The deploy wants a divergence check per
instance and surgical edits, not a copy. `TWOGATE_PDHC_SERVICE_KEY` should be
dropped from each `.env` in the same pass — it is inert once this ships.

### 2026-10-02 — #732 DEPLOYED to cdr1 only (canary)

Operator chose one instance first rather than all five.

**The divergence check earned its keep.** cdr1's deployed
`app/__init__.py` did **not** match local git — prod `138412a6…` vs local
`c841ad85…`. A file-overwrite deploy would have been exactly the mistake the
"CDR prod behind local git" note warns about. The diff turned out to be
*cosmetic*: the `#541` `ANALYSE_PDHC_SERVICE_KEY` line sits in a different
position with a different comment (applied on-server, later committed locally in
a slightly different form). No functionality was at risk — but that was
established by looking, not by assuming.

So: `app/api/auth.py` was **copied** (byte-identical to local pre-change, so
nothing on-server could be lost), and `app/__init__.py` was edited **in place**,
removing only the one `TWOGATE_PDHC_SERVICE_KEY` config line and asserting that
the `ANALYSE_` and `SIM_` lines survived. `TWOGATE_PDHC_SERVICE_KEY` also dropped
from `.env` (47 lines → 46).

Note for the next deploy: prod and local `app/__init__.py` **still differ
cosmetically** by that line's position. That is deliberate — the surgical edit
preserved prod's ordering. Do not "fix" it with a copy.

Layout worth recording: cdr1 is **not** the release-symlink layout. It is a flat
`/usr/local/www/cdr.pdhc/cdr_app/`, no `current` symlink, compose project rooted
there. (`cdr_app.baremetal.bak.20260525/` beside it is the pre-container tree.)

Backup: `~/backups/predeploy/cdr.pdhc/2026-10-02T05-43-27Z/` — `app` tarball,
`.env` copy, rollback image `sha256:1b0de54dcede3821`.

### Verified in the running container
```
INGEST KNOWN_SERVICES: {'gateway.pdhc': …, 'sim.pdhc': …}     2gate present: False
TWOGATE in config: False
kept: GATEWAY_ / ANALYSE_ / SIM_ / DASHBOARD_PDHC_SERVICE_KEY  all True
/healthz 200, database connected
```
The discriminating check:
- `X-Source-Service: 2gate.pdhc` → 403 **`unknown source service: 2gate.pdhc`**
- `X-Source-Service: gateway.pdhc` + wrong key → 403 **`invalid service key`**

Different messages, so the identity is *gone* rather than merely keyless. Before
this, 2gate produced the second message — indistinguishable from an untried
entry, which is precisely how it hid.

**No positive ingest was attempted**: a successful write would have put a real
row in a production CDR. The log check for gateway's 60s forwarder came back
**inconclusive** — zero requests in the window, because the platform is quiet —
so it is evidence of no errors, not evidence of working traffic.

### cdr2-5 and cdr_6 NOT deployed
Each needs its own divergence check; cdr1's was already a surprise. cdr_6 does
not read `TWOGATE_PDHC_SERVICE_KEY` at all.

### 2026-10-05 — #732 deployed to cdr2 and cdr3 (cdr4/cdr5 destroyed instead)

cdr4 and cdr5 were destroyed the same day, so the remaining instances were two,
not four.

**The divergence check earned its keep again, and this time it would have broken
federation.** cdr2 and cdr3's deployed `app/api/auth.py` carries an entry that
**local git does not have**:

```
live:  ['analyse.pdhc', 'gateway.pdhc', 'sim.pdhc', '2gate.pdhc']
local: ['gateway.pdhc', 'sim.pdhc']
```

`analyse.pdhc` is the #541 federation wiring — applied on the server and never
committed. A file-copy deploy would have silently deleted analyse's read
identity from both CDRs, and the federated analysis path would have stopped
working with a 403 that looked like a key problem.

So: **surgical in-place edit**, removing only the `2gate.pdhc` line, with
assertions that `analyse.pdhc`, `gateway.pdhc` and `sim.pdhc` all survived, plus
the `TWOGATE_PDHC_SERVICE_KEY` config read and the `.env` line (63 → 62 on both).

Backups at `~/backups/predeploy/cdr{2,3}.pdhc/2026-10-05T…/` with rollback
images `8e250b0b502ef70` (cdr2) and `3f6b84b7ee57c91` (cdr3).

### Verified
```
cdr2  allowlist ['analyse.pdhc','gateway.pdhc','sim.pdhc']  2gate False  TWOGATE False  ANALYSE key kept True
cdr3  allowlist ['analyse.pdhc','gateway.pdhc','sim.pdhc']  2gate False  TWOGATE False  ANALYSE key kept True
healthz: cdr1 200, cdr2 200, cdr3 200, cdr6 200
analyse sibling smoke: all 8 checks pass, mode=complete across three sources
```

### Known divergence, deliberately left
cdr2 and cdr3 still differ from local git in two ways, and **should not be
reconciled by copying**:
1. `analyse.pdhc` in `KNOWN_SERVICES` (#541, deployed-only).
2. They did **not** get the loud-warning-on-empty-key change that cdr1 received
   with #732 — a surgical edit removed the identity without adding the new
   branch. Lower risk than a copy; worth folding in when cdr2-5 prod and git are
   properly reconciled (the #168/#170 problem).

### 2026-10-06 — #731: the smoke-test residue is deleted

Four synthetic patients removed from cdr1: `smoke-299-p`, `smoke3-p`,
`smoke-408x2-p`, `smoke-pat-296`. **37 rows across 7 tables**, in one guarded
transaction.

The patient guids were themselves non-GUID, so these were whole test records —
not real patients carrying a bad code. Verified beforehand: **zero** real
patients held a non-GUID canonical, and no child row was shared with real data.

```
observation 7420 → 7415      clinical_context 7421 → 7416
bad_canonicals  5 → 0        bad_org_guids    5 → 0
```

The `#708` sibling smoke that first reported this now reads
**"all 19 distinct canonicals carry a GUID"**, where it previously said 4 of 23
could never resolve.

### Why it took three attempts
The first two aborted on foreign keys that had not been surveyed —
`openehr_compositions → fhir_resources`, then
`cambio_delivery_log → openehr_compositions`. Both rolled back with nothing
committed, which is the only reason a wrong order cost a retry rather than a
restore. The third ran the FK **closure**, computed from
`information_schema` rather than guessed from a list of tables with a
`patient_guid` column — which is what the first survey did, and why it missed
everything linked by `ingest_raw_guid`.

A fourth attempt was refused by the permission classifier; the operator cleared
it 2026-10-06 and the same transaction ran unchanged.

### Guard
The transaction refuses to commit unless exactly 5 observations and 5 ingest
rows go and zero smoke rows remain. Fresh `pg_dump` first at
`~/backups/predeploy/cdr.pdhc/2026-10-06T06-49-04Z-731-delete/` (5.7 MB) —
the 2026-10-02 dump predated the #732 deploy.

### Not done
`clinical_context.provider_org_guid` had the matching org-side residue
(`smoke-408x2-o` and friends); those rows went with their patients, and
`bad_org_guids` is now 0. The **ingest-side rejection** of a non-GUID tail,
which is what stops this recurring, was proposed in the ticket and is **not
built** — it changes a live write path and was never authorised.

## 2026-10-06 — #733 `GET /api/v1/concept-inventory` (cdr1, cdr2, cdr3)

What a researcher can ask about, not just how much data there is. Aggregate
only; the per-concept patient count goes through a k-floor (`CONCEPT_INVENTORY_K`,
default 5) and reports `<5` below it, because a concept named and counted for two
patients is a smaller crowd to hide in than any analysis the platform publishes.

Three things cost more than the endpoint itself:

1. **`clinical_read` is not deployed on cdr2-cdr5.** They run this codebase as a
   pure analysis repository with no clinical read surface, so the first version's
   module-level `from app.api.clinical_read import ...` made the endpoint a 500 on
   exactly the CDRs holding the most data (2.6M observations each). `_concept_helpers()`
   now degrades: the guid parse is pure and always available, the plan.pdhc display
   lookup is cosmetic and absent where the module is.

2. **Codes are not all Path-B.** cdr1 stores `urn:pdhc:concept/<guid>`; cdr2 and
   cdr3 store termbank CodeSystem URIs (LOINC) for all 2.6M of their rows. The
   first classifier had two buckets — "a plan.pdhc GUID" and "unresolvable" — which
   filed every concept those two CDRs hold under the second. analyse merges only
   the first, so the two largest sources read as EMPTY. A LOINC code is a stronger
   binding than a local GUID, not a weaker one. Now three kinds: `plan_concept`,
   `external_code`, `unbound`, and only the third is a defect.

3. **Units are columnar, mostly.** Reading them from `raw_json::jsonb` costs 8.2s
   on a 2.6M-row CDR; the `value_unit` column costs 2.8s for the same answer. But
   cdr1 has ~7000 rows whose `value_unit` is NULL while the body carries the unit,
   so the column alone hides the unit of its busiest concept. Two independent
   passes: the column, then `raw_json` only `WHERE value_unit IS NULL` — zero rows
   on cdr2/cdr3, so the expensive path never runs there. They are independent on
   purpose; when they shared one try/except a failure in the second re-ran the
   first inside the fallback and every unit was counted twice.

Tests: 192 pass (was 181). `tests/test_concept_inventory.py` covers the k-floor,
the degraded import through the ROUTE (it was a 500 before the fix — asserting on
the helper alone would not have caught it), and that the fallback parser agrees
with `clinical_read`'s on a table of canonicals. The `canon()` helper was also
interpolating the test tag into the first GUID segment (`mix-0000-...`, not hex),
so every test that believed it seeded a resolvable concept seeded an unbound one.

LIVE on cdr1 (19 concepts, all plan_concept, 0.08s), cdr2 (14, all external_code,
3.9s) and cdr3 (same, 10.2s cold). Predeploy backups under
`~/backups/predeploy/{cdr.pdhc,cdr2.pdhc,cdr3.pdhc}/` on miserver with rollback
image ids. cdr2/cdr3 were patched surgically — their deployed `stats.py` is 28
lines against local's 295 and overwriting it would have removed routes.

### Found while doing it, filed not fixed
- `/stats` reports `health_observations: 0` on EVERY CDR. That key counts the
  legacy `HealthObservation` table, which is empty platform-wide; the data is in
  the live per-type `observation` table (cdr1 7,415 / cdr2 2,629,300 /
  cdr3 2,629,300). The researcher landing page therefore shows "0 observations"
  next to a concept list totalling 5.18M.
- cdr1's "Reliever (SABA) puffs in last 24h" carries two units: `doser` on 16 rows
  and `/d` on 1. Now visible with counts, which is what #752 needs.

## 2026-10-06 — #760 a service key added for READS no longer carries a write grant

Found while verifying #732 was actually deployed. Membership in
`KNOWN_FHIR_SERVICES` was the entire check: it gates `POST /api/v1/fhir/<type>`,
`PUT /api/v1/fhir/<type>/<guid>` and `POST /api/v1/fhir/Bundle` alongside the GET
searches, and `_service_blob` mints an **SU-equivalent, organisation-blind**
identity. So every service ever added so it could read was also able to overwrite
any resource in the repository.

Measured before the fix, on the deployed loaders:

    cdr1  analyse POST /api/v1/fhir/Observation -> True      <-- could write
    cdr2  analyse in BOTH allowlists, key live              <-- could write
    cdr3  same as cdr2                                      <-- could write

cdr1 holds the real multi-organisation data, and analyse does not even read from
cdr1 (this coordinator does not federate it, #712) — the key was live there
anyway.

**The narrowing costs analyse nothing**, which is why it was safe to do without
a soak: all nine of its CDR call sites issue `method="GET"`
(`grep -o 'method="[A-Z]*"'` → `9 method="GET"`), and the paths it uses are
`/api/v1/stats`, `/api/v1/concept-inventory` and `GET /api/v1/fhir/Observation`.

Two changes:
- `READ_ONLY_SERVICE_SOURCES = {"analyse.pdhc"}` + a safe-method check in
  `_service_key_outcome`. `sim.pdhc` and `gateway.pdhc` write by design and are
  deliberately absent.
- A `WRITE_REFUSED` sentinel distinct from `False`, so the 403 says "this service
  key is read-only" instead of "Invalid service credentials". The credentials ARE
  valid; sharing the generic message would send whoever hits it to rotate a key
  that was never the problem.

On cdr2/cdr3 only, `analyse.pdhc` was also removed from
`app/api/auth.py::KNOWN_SERVICES` — that dict gates the four ingest/provenance
routes and analyse calls none of them. Local has no such entry.

Tests: 201 pass (was 192). `tests/test_read_only_service_key.py` goes through the
loader via real requests, because the defect was never in a predicate — it was in
what the loader let past, and a test asserting on `READ_ONLY_SERVICE_SOURCES`
would pass against the broken code. Verified: removing the gate fails exactly the
three write tests.

### Deploy notes
LIVE on cdr1, cdr2, cdr3; 14 assertions green on each, gateway still writes to
cdr1 (`POST/PUT -> True` before and after), platform 39 containers 0 unhealthy,
analyse sibling smoke 8/8, analyse's merged inventory unchanged at 35 concepts.

**cdr1's deployed `app/auth.py` had diverged from git** (709e7e71 vs local's
feae56ad) — structurally only, same allowlist and same lockdown behaviour, but it
carried a 2026-09-23 comment git never had. Recovered into the local file rather
than overwritten. All three patches were built FROM the deployed file and
dry-run inside the target container.

### A mistake worth recording
The first dry-run verified "sim is unaffected" by issuing a real
`POST /api/v1/fhir/Observation` against **production cdr2**. It reached the INSERT
and failed only on a NOT NULL constraint; the transaction rolled back and
`observation` stayed at 2,629,300 with 0 rows created, confirmed. Verifying a
write permission by exercising it is the wrong tool when the subject is 2.6M rows
of production data — ask the loader for its decision in a
`test_request_context` instead. The rewritten dry-run does that.

### Still open on #760
`dashboard.pdhc` holds a live `DASHBOARD_PDHC_SERVICE_KEY` in
`KNOWN_FHIR_SERVICES` on all three CDRs. It was NOT narrowed: #540 repointed
dashboard's analyse-pull but dashboard still carries CDR config, so whether it
writes is not established. Needs its call path traced before the key is narrowed
or removed.

## 2026-10-06 — #769 each datapoint carries its own context

`clinical_context` is keyed on `ingest_raw_guid`; the per-type live tables key on
the FHIR resource id. The two never joined — `observation.guid =
clinical_context.ingest_raw_guid` matched **0 rows** while cdr1 held 7,415
transaction GUIDs belonging to no reachable datapoint. A side table that cannot
be joined is worse than a missing column, because it looks populated.

Four columns on **every** per-type live and history table (10 + 10 = 20 tables,
80 columns), not Observation-only — per-type special-casing is what produces the
congruency problems #777 catalogues:

| column | why |
|---|---|
| `ingest_raw_guid` | **the join.** The pipeline already had it in scope and discarded it |
| `transaction_guid` | which step of the care plan the measurement answers |
| `care_plan_guid` | the CarePlan instance — 0 % until #772 is decided |
| `patient_org_guid` | the patient's ips organisation (#768), **not** the performer and **not** the requester |

All nullable. The flush removes the legacy-row argument, but `patient_org_guid`
still cannot be NOT NULL: its resolution is #774, blocked on #778, so requiring
it would reject every ingest today. A test records that as a deliberate state to
revisit, not an oversight.

### The migration graph was already forked, and I nearly made it worse
`grep down_revision | tail -3` listed files alphabetically and showed
`f4a5b6c7d8e9` as the last one, so the first draft chained onto it. Building the
actual graph showed the chain continues
`f4a5b6c7d8e9 → a1b2c3d4e5f6 → b5c6d7e8f9a0`, and `b5c6d7e8f9a0` is both the
single head and the revision live on all three CDRs. Corrected to chain from
there. `test_the_migration_graph_has_exactly_one_head` now fails on a fork — a
fork is invisible until a deploy applies one branch.

### Flush
Operator-authorised; the CDR contents were mock. **Kept**: `users` (the SU
mirror — flushing breaks login), `alembic_version`, `service_keys`, and
`audit_log` + `cdr_read_audit`. The audit tables record *who did what*, not the
clinical data; deleting an audit trail is a different act from clearing mock
data and nobody asked for it.

Backups at `~/backups/predeploy/cdr-769-flush/20261006T152533Z/`: a **full**
`pg_dump` of cdr1 (5.2 MB compressed — it held the only record that the
Medituner, CambioCaregiver, Provider1 and 1177 onboardings ever delivered data),
and **schema + row-count manifests only** for cdr2/cdr3. Those are 13 GB each of
single-organisation synthetic rows whose `org_guid` is the nil UUID on 100 % of
rows; a full dump would have added 26 GB to a host with 49 GB free and 10 GB of
existing backups, for data reproducible by reseeding. Deliberate — and the
`n_live_tup` manifest written first was a stale estimate reporting "1 table with
rows", so it was redone with real counts.

### Result
All three CDRs identical: head `a7b8c9d0e1f2`, 38 tables, 88 context columns
each. cdr2/cdr3 went 13 GB → 10 MB; the Colima volume went 52 G used → 26 G,
**26 GB reclaimed**. `/healthz`, `/api/v1/stats` and `/api/v1/concept-inventory`
all 200 and report zero, which is correct after a flush and is the thing that
had to not break. analyse's fanout still answers (21 concepts, all cdr6 now).
Gateway→CDR line untouched at delivered 354 / skipped 6. 39 containers, 0
unhealthy. 225 tests pass, was 201.

A test-isolation leak of my own: the new tests seeded an `IngestRaw` that
`test_ingest.py::test_ingest_accepted` picked up via an unordered
`IngestRaw.query.first()`. Passed alone, failed in the suite. Third time this
session-scoped-fixture leak has appeared (#732, #733), so it is now a cleanup
fixture rather than another one-off fix.

## 2026-10-06 — #773 ingest writes the context onto the row

`build_live_observation_row` **already took `ingest_raw_guid` as a parameter** —
and used it only as a fallback for the row's own guid, never storing it. That is
precisely why `clinical_context`, which keys on it, could not be joined to the
observation table. One assignment closed it.

`transaction_guid` and `care_plan_guid` are read from the producer's context
block, accepting the same `activity_guid` / `careplan_guid` aliases
`ClinicalContext` accepts (#302) — otherwise a producer mid-way through the #294
deprecation window would populate the context table and leave the row empty, and
the two would disagree.

**`patient_org_guid` is deliberately NOT read from the producer.** It is the
patient's ips affiliation and only ips can answer it (#768/#774). A producer's
idea of "the organisation" is the performer or the requester, and both already
have their own columns — writing either here is the exact conflation #768
documents. A test asserts the field stays NULL even when a producer sends
`patient_org_guid` or `org_guid` in the context block.

Live on cdr1, through the real ingest endpoint:

    observation.guid           630d0c55-…
    ingest_raw_guid            302272c2-…     <- the join
    transaction_guid           tx-verify
    care_plan_guid             cp-verify
    patient_org_guid           None           <- correct until #774
    org_guid (performer)       performer-org-773
    ctx.plan_definition_guid   pd-verify      <- arriving since #776
    ctx.requesting_org_guid    requester-verify

228 tests pass (was 225); the three new ones fail against the pre-#773 code.

### Two things found while testing
`SAMPLE_FHIR_OBSERVATION` has no `id`, and the live-row path is gated on
`fhir_resource.get("id")` — so an ingest without one creates **no datapoint row
at all**, silently. Gateway always sets an id, so production is fine, but the
shared fixture does not exercise this path.

The FK chain for deleting observation data is
`fhir_resources <- openehr_compositions <- cambio_delivery_log`; the same chain
aborted two of #731's delete attempts. Use the #769 flush script rather than
hand-ordered DELETEs.

## 2026-10-06 — #762 dashboard.pdhc narrowed to read-only

#760 narrowed `analyse.pdhc` and left `dashboard.pdhc` alone, because it was not
provably unused: #540 repointed its analyse-pull but it still carried
`CDR_ENDPOINTS` and `CDR1_BASE_URL`.

Traced, the same way analyse was settled:

- all **six** `fanout()` call sites in `app/routes/nurse.py` pass `method="GET"`
- `app/services/cdr1_client.py` has a **single** transport, `_get_json`, built on
  `requests.get`; every public method goes through it
- its only two POSTs are to **sso** (token exchange) and **ips** — neither is a CDR
- no call site targets `/api/v1/ingest` or a FHIR write verb

So dashboard is an **active reader**, not a writer. Narrowed rather than retired:
#727 removed `monitor.pdhc` because it named nothing that existed, which is a
different situation.

Live on all three CDRs:

    dashboard GET   -> ADMITTED
    dashboard POST  -> WRITE REFUSED
    dashboard PUT   -> WRITE REFUSED

dashboard `/healthz` 200, 39 containers 0 unhealthy, 238 tests pass (was 235).

With this, every service-key identity on the CDRs is either read-only
(`analyse.pdhc`, `dashboard.pdhc`) or writes by design (`gateway.pdhc`,
`sim.pdhc`). No identity holds a write grant it does not use.

## 2026-10-07 — #730 FIXED and DEPLOYED: the consent gate could not get a verdict

`_analysis_filter` attached `Authorization: Bearer <token>` only when a flask
session held one. A service-shaped read — gateway, analyse, sim — has no
session, so the request went out with **no Authorization header at all**. ips's
`require_auth` reads only that header (`X-API-Key` is silently ignored), so it
answered 401, `_analysis_filter` raised `IpsUnreachable`, and `_fail_closed`
excluded every patient.

The #664 gate is meant to **filter** a read down to the patients who consented.
It was refusing the whole read instead — not applying consent, denying access,
and surfacing as though ips were down. `declared_service_purpose()` in the same
module shows service callers were always expected here, so this had never worked
for them.

**Fix:** fall back to `Authorization: ApiKey <key>` from the service's own
`IPS_API_KEY`, the same remedy #719 took for analyse. A missing key still fails
closed — correct — but the message now names the missing credential instead of
leaving a bare 401 to be misread as an outage.

### A second defect, in ips, found by fixing the first

With the key in place the 401 became a **500**:
`badly formed hexadecimal UUID string`. `PatientIndex.guid` is a UUID column, so
`/analysis-filter`'s `IN` clause raised at the driver whenever a guid was not a
well-formed UUID — and the smoke's probe guid deliberately is not one.

A 500 is the worst answer a consent gate can give: the caller sees an exception
rather than a verdict, and it is indistinguishable from ips being down, so cdr
fail-closed the **whole cohort** and blamed the wrong service. One malformed
guid anywhere denied everything.

ips now treats a malformed guid as **unknown**: it fails closed for that patient
alone and is named in `excluded` with `reason: malformed_guid`, so a caller can
tell it sent something unusable. Per-patient fail-closed beats per-request, and
neither should be a 500.

### Tests — and what they cannot do

Every pre-existing test in `test_analysis_consent.py` patches
`_analysis_filter` itself, and conftest's **autouse** `_consent_allow_all`
replaces it suite-wide. So the header-building code was unreachable from any
test in the file. That is why this survived. New tests capture the real function
at import time and patch `requests.post` one level lower, asserting the header
on the wire; one of them guards the guard, failing if conftest ever stops
stubbing.

    cdr_app: 244 passed
    ips:     401 passed, 1 failed (pre-existing, unrelated — a Swedish
             legal-review banner in test_patient_portal_html; verified against
             the untouched tree)

Proven, not assumed: with the fix stashed, 4 of the new cdr tests fail and 2 of
the 3 new ips tests fail. **The ips 500 itself is not reproducible in the unit
suite** — the test DB is SQLite, which does not coerce UUIDs. The tests pin the
new contract; only Postgres reproduces the bug, so the production smoke is the
real verification.

### Deployed to all three CDRs + ips

Each `analysis_consent.py` was diffed against the local pre-change baseline
first — **all three identical**, so a file copy applied this diff and nothing
else (the rule from `infra_cdr_prod_behind_local_git`). Same for ips's
`patient_routes.py`.

Each CDR was issued its **own** ips ApiKey (prefixes `PmipxjGj`, `D3LjQGa3`,
`0lRKHoVZ`), minted through ips's own `/api/v1/auth/keys`. `docker-compose up -d
--build` throughout, because `docker restart` keeps create-time env and would
not have seen the new `.env`.

Verified in-container on all three:

    cdr_pdhc_app     VERDICT OK  allowed=0 excluded=1  reasons=['malformed_guid']
    cdr_pdhc_2_app   VERDICT OK  allowed=0 excluded=1  reasons=['malformed_guid']
    cdr_pdhc_3_app   VERDICT OK  allowed=0 excluded=1  reasons=['malformed_guid']

and cdr1's sibling smoke now reports
`✓ ips: a consent verdict is obtainable for a service-shaped read`.

Backups: `~/backups/predeploy/{cdr,cdr2,cdr3}.pdhc/...` and
`~/backups/predeploy/ips.pdhc/20261007T092918Z/`.

### Noticed in passing, NOT fixed — operator call

cdr1's compose project is **`cdr_app`** (the directory name), not the
`COMPOSE_PROJECT_NAME=cdr_pdhc` pinned in its `.env`. The live data volume is
still `cdr_pdhc_pgdata` and the db container dates from 2026-06-27, so **my
app-only rebuild did not touch it** — this predates today. There is also an
orphan volume `cdr_app_pgdata` from 2026-04-10, mounted by nothing. Same shape
as the dashboard #113 incident. Volume removal is operator-only.

### Still failing in the smoke, correctly

`plan: a real code_canonical from this CDR resolves` — there are 0 observations
after the #769 flush, so there is nothing to resolve. The check reports
inconclusive **as a failure** so it cannot be mistaken for proof. It goes green
when data flows.

---

## 2026-10-12 — #820: the contract and the visit now reach every CDR

Operator: "Since I have chosen the contract it is strange that the contract
guid is not registered in the CDRs, also plan activity guid should be part of
all CDRs."

Both right. A contract the operator picks, that then isn't recorded, makes the
gate decorative.

### I was wrong about the cause yesterday

I reported that "cdr2 has no contract_guid column". That was true of
`observation` and **wrong about the database**: `contract_guid` and
`plan_definition_guid` have been in `clinical_context` on **all three** CDRs all
along. Schemas verified column-by-column — cdr1, cdr2 and cdr3 are identical.

The real cause was one line:

```python
context = body.get("clinical_context")      # nested block, or nothing
```

cdr_6 and sim emit the canonical 12-field context at the **TOP LEVEL** — that
is what cdr_6's own ingest reads. So for every flat producer `context` was
`None`, and the `ClinicalContext` insert, guarded by `if context:`, was skipped
**silently**. One canonical context, two payload contracts.

`plan_activity_guid` was the genuinely missing piece, and missing from all
three, exactly as the operator said.

### The fix

`_clinical_context_from(body)` accepts either shape — nested wins when present
(an explicit block is a deliberate statement), top-level is the fallback,
`None` only when neither says anything so the `if context:` guard keeps its
meaning. #294's wire aliases still map (`plandef_guid`, `careplan_guid`,
`activity_guid` → *transaction*, never the visit).

`ClinicalContext.plan_activity_guid` + migration `a5b6c7d8e9f0`, applied to
cdr1, cdr2 and cdr3 (all were at `a7b8c9d0e1f2`, its parent).

### Two defects that were hiding each other

Deploying to cdr3 made every flat ingest **500**:

```
TypeError: 'author_org_guid' is an invalid keyword argument for ClinicalContext
```

cdr2/cdr3 never received #665, so their model does not declare
`author_org_guid` — **though the column exists in their databases**. It had
never mattered, because `context` was always `None` and the insert never ran.
Fixing the extraction turned a latent incompatibility into an outage. Patched
in place (model-only; the column was already there).

### Deployed, per-CDR

`ingest_pipeline.py` was **byte-identical** across all three and matched local
HEAD, so a file copy was safe everywhere. `models/__init__.py` diverges
(cdr2/cdr3 lack #665), so there it was **patched in place** by anchor, never
overwritten — `infra_cdr_prod_behind_local_git`. Both files `py_compile`d
before any restart.

### Verified against the real services

```
  transfer 20 rows cdr_6 -> cdr3   accepted=20 rejected=0 verified=True
  transfer 20 rows cdr_6 -> cdr2   accepted=20 rejected=0 verified=True

  clinical_context after:
    cdr2   20 rows: contract=20 visit=20 plandef=20 txn=20
    cdr3   20 rows: contract=20 visit=20 plandef=20 txn=20
```

cdr1 cannot be transferred to — it has no `SIM_PDHC_SERVICE_KEY` — so its path
was exercised in-container instead: `_clinical_context_from` on a flat payload
plus the `ClinicalContext(...)` construction, both fine, contract and visit
preserved.

Gateway (which writes to cdr1 live) stayed healthy; no unhealthy container on
the box.

### A test that failed to fail

My first tests imported `_clinical_context_from` directly, so reverting the
**call site** left all eight green — the exact bug they were written for.
`test_THE_PIPELINE_ACTUALLY_CALLS_THE_EXTRACTOR` asserts the call site, and
goes red when it is reverted. Also: my migration first chained onto
`f4a5b6c7d8e9` and forked the graph; the real head was `a7b8c9d0e1f2`, caught
by the repo's own one-head test.

254 tests pass.

### Cleaned up

Test data purged from cdr_6, cdr2 and cdr3. Including **140 pending
`cambio_delivery_log` rows** my transfers queued for outbound delivery — never
dispatched (nothing has ever left `pending`, and the 60s APScheduler logs no
start line), but they were synthetic rows aimed at an external system. Audit
rows (`audit_log`, `cdr_read_audit`) deliberately kept: they record that the
ingests happened, which is true.

---

## 2026-10-12 — #821: which RUN, not just which writer

Operator: "add sim_run_id to cdr1 2 and 3".

`source_service = sim.pdhc` says who **wrote** a row. `sim_run_id` says which
**generation** produced it — a different and stronger claim, and the one that
makes a cohort traceable and purgeable as a unit, which is what cdr_6 has
always been able to do (`DELETE /api/v1/runs/<id>`). A cohort promoted out of
cdr_6 kept its writer and lost its run identity, so "these 300 rows came from
that run" had no answer short of matching effective dates and hoping.

### Two deliberate choices

**On `clinical_context`, not `observation`.** That is where the rest of a row's
provenance lives, and `observation.ingest_raw_guid` joins to it — #769 records
that column as THE join to provenance. Putting it on both would be two places
to keep in step. The query that matters stays one join:

```sql
select cc.sim_run_id, count(o.guid)
from clinical_context cc join observation o on o.ingest_raw_guid = cc.ingest_raw_guid
group by cc.sim_run_id;
```

**`String(64)`, not `String(36)`.** A run id is not a GUID:
`sim-20261012T060311Z-00000331` is 29 characters and
`synthea-20260603T141456Z-fd45c6` is another live shape. 36 would fit today
with no headroom, and sizing it like a GUID invites someone to "tidy" it.
A test asserts the width.

Nullable with no backfill: a row written before this cannot be attributed to a
run, and inferring one from timestamps would invent provenance. NULL means
"not recorded" — and for anything gateway wrote, "not from sim", which is the
honest answer.

### Verified against all four

```
  cdr_6 -> cdr1   accepted=20 rejected=0 verified=True
  cdr_6 -> cdr2   accepted=20 rejected=0 verified=True
  cdr_6 -> cdr3   accepted=20 rejected=0 verified=True

  clinical_context in each:
    20 rows, sim_run_id=20, distinct=1, visit=20, contract=20

  the join, on cdr1:
    sim-20261012T060311Z-00000331  ->  20 observations, 2 patients
```

A promoted cohort now carries three levels of identity: the writer
(`source_service`), the generation (`sim_run_id`), and the authority
(`contract_guid` + `plan_activity_guid`).

### Deploy

Same shape as #820. `ingest_pipeline.py` was byte-identical across all three
and matched local HEAD, so it was copied; `models/__init__.py` diverges on
cdr2/cdr3 so it was patched **in place by anchor**. The anchor is the
`received_at` + `source_service` PAIR — the `source_service` line alone
appears six times in that module, and the patcher refuses rather than guessing
when an anchor is not unique. Both files `py_compile`d before any restart; all
three migrated to `b6c7d8e9f0a1`.

259 tests pass. Test data purged from all four; no unhealthy container.
