# cdr.pdhc — Technical Manual

> **One codebase, five instances (CDR 1–5).** All five run the *identical*
> software and schema. They differ only in configuration — chiefly the
> `CDR_READ_LOCKDOWN` flag (see §3) — and in the data they hold.

## 1. Overview

cdr.pdhc is the platform's clinical data repository. It ingests normalised
observations from the gateways, stores every FHIR resource type in its own
per-type table (with full version history), exposes a FHIR R5 read/search
surface plus a care-delivery clinical surface, and — on CDR 1 only —
forwards concept-mapped data to the Cambio CDR sandbox.

**Ports:** 9046 (Flask/gunicorn, bound to `127.0.0.1`), 9047 (PostgreSQL),
9048–9049 reserved. Health at `GET /healthz` (also `GET /api/v1/health`).

## 2. Storage model — per-FHIR-type tables

The authoritative store is a **set of per-resource-type tables**, one live
table per FHIR type plus a matching `*_history` twin. This replaced the old
three-layer (`raw → standard → canonical`) design; the old tables still
exist but are legacy (see §2.3).

### 2.1 Per-type live + history tables

`app/models/resources.py` builds, from a single `RESOURCES` list, one live
ORM model and one history model for each of these ten FHIR R5 types:

| FHIR type | Live table | History table |
|-----------|-----------|----------------|
| Patient | `patient` | `patient_history` |
| Observation | `observation` | `observation_history` |
| QuestionnaireResponse | `questionnaire_response` | `questionnaire_response_history` |
| Condition | `condition` | `condition_history` |
| MedicationStatement | `medication_statement` | `medication_statement_history` |
| MedicationRequest | `medication_request` | `medication_request_history` |
| AllergyIntolerance | `allergy_intolerance` | `allergy_intolerance_history` |
| Procedure | `procedure` | `procedure_history` |
| Encounter | `encounter` | `encounter_history` |
| DiagnosticReport | `diagnostic_report` | `diagnostic_report_history` |

Every live table shares these common columns:

```
guid, patient_guid, org_guid, code_canonical, effective_at, raw_json,
source, source_request_id, meta_tag, version_id, sync_group_id,
mapping_version, etag, received_at, created_at, updated_at
```

plus a few type-specific columns (e.g. `observation` adds `value_quantity`,
`value_unit`, `value_string`, `value_code`, `status`; `patient` adds
`identifiers`, `names`, `gender`, `birth_date`, `active`). The full FHIR
resource is always kept verbatim in `raw_json`; the extracted columns exist
only to index and search.

**Time fields (#294 RFC E1):** `effective_at` is the clinical measurement
time; `received_at` is when the platform first saw the payload; `created_at`
is when this row was written; `updated_at` is the last version bump.

**History tables** carry the same columns (minus the live-only ones) plus
`superseded_at` and `superseded_by_request_id`. The primary key is the
composite `(guid, version_id)` — one row per historical version.

**`code_canonical`** is the concept key a row is searched by. On live CDR 1
it is dominantly the **Path-B embedded-GUID form** `urn:pdhc:concept/<guid>`
— the plan.pdhc Concept GUID is the last path segment. Display resolution
parses that GUID out and looks it up via plan.pdhc (see §6.2).

### 2.2 Cross-cutting tables

- **`sync_group`** — one row per logical clinical fact, linking the FHIR and
  openEHR representations. The openEHR side is a reserved placeholder today
  (bidirectional mapping deferred); the `sync_group_id` is minted now so the
  wiring is ready.
- **`change_feed`** — append-only event log; one row (`create`/`update`/
  `delete`) is emitted on every per-type write. Consumers poll it via
  `GET /api/v1/fhir/events` (§4.2).
- **`cdr_audit_plan_miss`** — a canonical URI returned by xlate.pdhc that no
  active plan.pdhc Concept references. Bookkept for out-of-band review.
- **`cdr_audit_whole_system_accept`** (#872) — the mirror of the row above:
  a canonical URI plan.pdhc accepted at **system** granularity rather than per
  code, because its canonical library is flagged `whole_system_answer_space`
  (`icd11-mms`, and `icd10` since 2026-11-02). plan answered `result: true`
  *without having been asked about that particular code* — an adoption
  statement about the code system, not a membership test. Deduped on
  `canonical_uri` with `seen_count`, like its sibling. One table says what plan
  REFUSED, the other what it assented to on system grounds; read together they
  answer "on what basis is this code in the repository?". Nothing is
  backfilled — the audit starts at first sighting after deploy.
- **`cdr_read_audit`** (X1 #407/#443) — one row per patient-touching FHIR
  read: caller service/user, org guids, route, resource type, patient guid,
  rows returned, HTTP status, and the session/role/purpose/access-basis
  tuple. Machine reads carry `caller_service` with a NULL role tuple.

  **Since #984 (operator 2026-11-17) this is THE ONE access log for
  diagnoses** (icd11.pdhc's own `read_audit` is no longer written). Migration
  `a9b8c7d6e5f4` adds three nullable columns:
  `on_behalf_of_user_guid` (String 36, indexed), `outcome` (String 16:
  `served` / `denied` / `break_glass`; rows from before the migration are
  NULL = served) and `detail` (JSON metadata: `reason`, `as_of`, `scope`,
  `refusal_code`, `action`, `resource` — never clinical content).

  - **On-behalf headers**, accepted ONLY from a service in
    `app/services/read_audit.py::ON_BEHALF_SERVICES` (`icd11.pdhc`,
    `dashboard.pdhc`); from anyone else they are ignored with a WARNING:
    `X-Operator-User-Guid` -> `on_behalf_of_user_guid` (a recorded CLAIM of
    the calling service, like `X-Operator-Session-Id`; `caller_user_guid`
    still only holds identities the CDR verified itself);
    `X-Access-Basis: break_glass` + `X-Break-Glass-Reason` (URL-encoded,
    <= 500 chars decoded; longer is cut and flagged `reason_truncated`) ->
    `outcome=break_glass`, `detail.reason`.
  - **Break-glass fails closed.** Every logged read is confirmed in the
    response header `X-CDR-Read-Audit-Id`. For an accepted break-glass
    request whose log write failed, an `after_request` hook replaces the
    answer with `503 BREAK_GLASS_AUDIT_UNAVAILABLE` (no data). Ordinary reads
    keep the best-effort posture (a failed write is a WARNING).
  - `$at` reads record `detail.as_of`; a search with a single `?patient=` is
    logged against that patient.
  - **`POST /api/v1/read-audit/events`** (`app/api/read_audit_events.py`) —
    a refusal made upstream that never reached the CDR. Service key from an
    `ON_BEHALF_SERVICES` source only (other services 403
    `SERVICE_NOT_PERMITTED`, operator sessions 403 `SERVICE_KEY_REQUIRED`).
    Body `{"patient_guid","user_guid","outcome":"denied"|"break_glass",
    "route","detail":{...}}`, validated strictly (unknown field or detail key,
    non-string detail value, overlong value, missing reason on break_glass ->
    400 with a `code`). One INSERT, no repository table read; `response_status`
    403 for denied; `detail.recorded_via = "event"`. 201 `{"id","outcome"}`;
    a failed write is 503 `AUDIT_WRITE_FAILED`, never swallowed. It is the
    one write path `dashboard.pdhc` (read-only, #760) is admitted to
    (`auth.READ_AUDIT_EVENTS_PATH`).
  - **Append-only** (`app/models/read_audit_guard.py`): ORM events refuse
    UPDATE/DELETE of a `ReadAudit` row and a trigger (Postgres + SQLite, same
    DDL as `history_guard.ddl_for`) refuses them in raw SQL. The only escape
    is the platform's one escape, `history_purge()`, which opens DELETE for
    its own transaction and never UPDATE. No production code uses it here.

### 2.3 Legacy tables (read-mostly)

The pre-platform-plan tables remain in place: `ingest_raw`, `fhir_resources`,
`openehr_compositions`, `health_observations`, `activities`, plus
`clinical_context`, `dedupe_registry`, `loinc_archetype_map`, `service_keys`,
`users`, `audit_log`, `cambio_patient_map`, `cambio_delivery_log`.

The **flat ingest path** (§4.1) still writes several of these — `ingest_raw`
(immutable payload + SHA-256), `fhir_resources`, an `openehr_compositions`
row generated by the transformer, `clinical_context`, `dedupe_registry`,
`cambio_delivery_log`, `audit_log` — because they back dedup, provenance and
Cambio delivery. But the **read/search surface no longer reads them**: step
3.5 of the flat ingest (#295) mirrors each Observation into the per-type
`observation` table, and all FHIR reads hit the per-type tables. So the
legacy standard/canonical tables (`fhir_resources`, `openehr_compositions`,
`health_observations`, `activities`) are effectively write-through legacy for
provenance, not the query store.

**Table count is 38 on CDR 1** (37 on cdr2/cdr3), not 13: 10 live + 10 history
+ 5 cross-cutting (`sync_group`, `change_feed`, `cdr_audit_plan_miss`,
`cdr_audit_whole_system_accept`, `cdr_read_audit`) + 13 legacy/infra tables.
`cdr_audit_whole_system_accept` exists on CDR 1 only so far (migration
`c8d9e0f1a2b3`, applied 2026-11-02); cdr2/cdr3 are one migration behind.
Counts exclude `alembic_version`.

## 3. The five CDRs and `CDR_READ_LOCKDOWN` (#293)

The single most important config difference between instances:

- **CDR 1 (`cdr.pdhc.se`)** — production clinical repository.
  `CDR_READ_LOCKDOWN=false`. Gateway writes real provider observations
  directly to CDR 1 (per SSOT), and CDR 1 forwards to Cambio.
- **CDR 2–5 (`cdr2…5.pdhc.se`)** — analysis instances.
  `CDR_READ_LOCKDOWN=true`. Populated with synthetic cohorts; only the
  analyse layer reads them.

**Under lockdown** (`_is_read_path` in `app/auth.py`), the read endpoints
(`/api/v1/fhir/*`, `/api/v1/openehr/*`, `/api/v1/stats`, `/api/v1/cambio/*`,
`/api/v1/clinical/*`) accept **only** the analyse-layer reader identities
`dashboard.pdhc` and `analyse.pdhc`. All other trusted services may still
**write**; they just cannot read a locked-down CDR. CDR 1, with the flag
false, serves reads to any valid trusted reader.

## 4. Data flow

### 4.1 Flat ingest (legacy wire, gateway → CDR 1)

`POST /api/v1/ingest` and `POST /api/v1/ingest/batch` (max 100). Service-key
auth via `KNOWN_SERVICES` (§5.1). Body must carry `patient_guid` and may
carry `fhir_resource`, `openehr_composition`, `canonical`, and
`clinical_context` blocks. `IngestPipeline.process` runs:

1. **Deduplicate** — SHA-256 of the payload vs `dedupe_registry` (scoped by
   source service). A repeat returns `200 {"status":"duplicate"}`.
2. **Store raw** — immutable `ingest_raw` insert.
3. **Store standard** — write the FHIR resource to `fhir_resources`; store or
   (from FHIR) **generate** an `openehr_compositions` row via the transformer
   (§6.1). If only openEHR arrived, generate the FHIR side.
   **3.5 Mirror to per-type** (#295) — build and insert the live
   `observation` row (this is what search reads).
4. **Store canonical** — `health_observations` or `activities` (legacy).
5. **Store context** — the canonical 12-field `clinical_context` row.
6. **Register dedupe** — add the hash to `dedupe_registry`.
7. **Enqueue Cambio** — create `cambio_delivery_log` rows (`pending` if a
   concept reference is present, else `skipped`). The X2 operator session id
   (`X-Operator-Session-Id`) is captured for later replay.
8. **Audit** — write `audit_log` with correlation id and client IP.

Returns `202 accepted`, `200 duplicate`, or `422 rejected`.

`GET /api/v1/ingest/by-source-id/<source_system_id>` (service-key,
source-scoped) confirms a prior delivery landed.

### 4.2 FHIR write (per-type path)

`POST /api/v1/fhir/<Type>`, `PUT /api/v1/fhir/<Type>/<guid>` (optimistic
concurrency via `If-Match`), and `POST /api/v1/fhir/Bundle` (transaction =
atomic; batch = per-entry). Each entry runs:

```
canonicalise → dedup-lookup → insert-or-update-with-history →
sync_group → mapping_version → change_feed
```

Canonicalisation resolves codings to a `code_canonical` (via xlate.pdhc /
plan.pdhc). A body carrying an `id` upserts by that id (FHIR "update as
create"), version-bumping and pushing the prior row to `*_history`. Error
outcomes: `422 xlate_miss`, `422 plan_miss` (also bookkept to
`cdr_audit_plan_miss`), `412` on stale `If-Match`, `503 transient` when xlate
or plan is unreachable.

On the **accept** path, a coding that plan assented to on whole-system grounds
(its answer carried `whole_system: true`) is bookkept to
`cdr_audit_whole_system_accept` (#872) on **both** accept branches — the
termbank-prefixed one and the xlate-translated one. The write proceeds either
way; the row records the *basis*, so a later reader can tell an individually
verified code from one admitted because its whole classification is adopted.

### 4.3 FHIR read / search

All under `/api/v1/fhir/` (`app/api/fhir_read.py`), reading the per-type
tables:

| Endpoint | Purpose |
|----------|---------|
| `GET /<Type>` | Search (see params below) |
| `GET /<Type>/<guid>` | Instance read (ETag) |
| `GET /<Type>/<guid>/_history` | Version list Bundle |
| `GET /<Type>/<guid>/_history/<vid>` | vread a specific version |
| `GET /Patient/<guid>/$everything` | Patient compartment Bundle |
| `GET /events` | `change_feed` long-poll (`?since=<seq>`) |
| `GET /metadata` | FHIR CapabilityStatement |

**Search parameters** are FHIR-standard (not the old `patient_guid` /
`loinc_code`):

- `patient` / `subject` — accepts `Patient/<guid>` or a bare guid.
- `code` — `system|code` (equality-matched against the indexed
  `code_canonical`) or a bare code (suffix match).
- `date` — FHIR prefix syntax `ge` / `le` / `gt` / `lt` / `eq`.
- `_id`, `_tag` (`system|code` against `meta_tag`), `_count` (cap 30000,
  default 100, most-recent-first by `effective_at`).
- `_has` reverse-chain, e.g. `_has:Observation:patient:code=<code>`.
- `_include` / `_revinclude` (e.g. `Observation:patient`).
- Chained `patient.identifier=<system>|<value>` (also `subject.identifier`).

Every read is org-scoped (Rule 24, §5.3), consent-filtered (§5.4), and
written to `cdr_read_audit`.

> Group aggregations (`$stats`, `$agp`) were moved out to the analyse layer
> (dashboard.pdhc) in the CDR1/analyse split (#289). CDR 1 is pure storage;
> analyse fetches raw Observations via search and aggregates locally.

### 4.4 Care-delivery clinical read surface (#468)

`app/api/clinical_read.py`, mounted at `/api/v1/clinical`. This serves the
rebuilt single-patient **clinical** dashboard (#462), which reads CDR 1 under
a **care-delivery** legal basis (vårdrelation + spärr), *not* the
analysis-consent basis:

| Endpoint | Returns |
|----------|---------|
| `GET /clinical/patients` | The org's patients that have Observation data, with counts |
| `GET /clinical/patient/<guid>/summary` | Per-concept counts + unit + first/last seen |
| `GET /clinical/patient/<guid>/series` | Time-series points (optional `code`, `from`, `to` filters) |

Guard (`_care_delivery_guard`): the request **must** carry
`X-Access-Purpose: care-delivery` **and** the service identity must be
`dashboard.pdhc`. Because the service blob is `is_su_admin`, the shared FHIR
org-filter would leak all orgs — so these endpoints do their **own** explicit
org scoping from the forwarded `X-Org-Guids` / `X-Is-Admin` headers (the
operator's affiliation care-unit guids). Consent (#422) is **bypassed** here:
a patient may be treatable while having declined research. Series points
carry `org_guid` so the dashboard can apply spärr (per-clinic blocks) on its
side. Concept display names are resolved via plan.pdhc (#471), fail-open.

**Scope predicate since #1044.** For a non-admin, all four routes
(`/patients`, `/patient/<g>/summary`, `/patient/<g>/series`,
`/observation/<g>`) apply one filter, `_apply_org_scope`:

    patient_org_guid IN (X-Org-Guids)  OR  org_guid IN (X-Org-Guids)

The first clause is the rule: the patient is listed at one of the operator's
care units. It is `auth.patient_scope_clause`, the same function the FHIR
read's Rule 24 filter calls, so the service has one definition of that
predicate. The second clause is the predicate these routes had on their own
until #1044 (the performer); #818 repointed the FHIR read and did not reach
this module. It is kept so that no row visible before disappears, and it is
the only way a row with no `patient_org_guid` is reached by a non-admin. A
row whose `patient_org_guid` is NULL is never in a care unit's scope through
the first clause. No `X-Org-Guids` and no admin: nothing. Admin
(`X-Is-Admin: 1` + reason) is unchanged. Before #1044 a patient-app answer
(performer = the patient app's organisation) was invisible to the treating
clinician here.

### 4.4b Review of patient-reported answers, and the endorsement row (#1044)

Clinical repository only: the routes are registered under the same role flag
as §4.4 (`CDR_CLINICAL_READ`). An analysis CDR (cdr2, cdr3) has none of them
and its ingest refuses `review_required` with `review_not_available_here`.

**Storage.** Six nullable columns on every per-type live table and its history
(migration `d5e6f7a8b9c0`, additive, no backfill, no rewrite):

| Column | Meaning |
|--------|---------|
| `review_status` | NULL = not subject to review. `pending`, `endorsed`, `rejected`: set on the ORIGINAL |
| `review_reason` | Text, required on a rejection |
| `reviewed_by_user_guid`, `reviewed_by_name`, `reviewed_at` | Who decided (on the original); the author (on the endorsement row) |
| `endorses_guid` | On the ENDORSEMENT row: the original's guid |

`clinical_context` has no copy: the status changes once and has one home. The
untouched submission is `ingest_raw.payload_json`, as for every ingest.

**Ingest.** `review_required: true` in the clinical context (or at the top
level) sets `review_status = 'pending'` on the live observation row. It
requires `data_origin = 'patient_generated'`, a JSON boolean, and a FHIR
Observation with an id that is not already stored; each failure is a named 422
(`review_required_without_patient_generated`, `invalid_review_required`,
`review_required_needs_observation`, `review_required_guid_exists`) and
nothing is stored. Optional `clinical_context.group` (a string, at most 64
characters) names the repeating-group instance the answer belongs to; it is
kept in `clinical_context.resolved_context_json["group"]`.

**Which organisation the gate scopes on: `observation.patient_org_guid`.** The
organisation the patient was listed at in eps when the request was made
(request.pdhc snapshots it, gateway forwards it as
`clinical_context.patient_org_guid`). It is the column the FHIR read filter
(#818) and the external read door scope on. `org_guid` is the PERFORMER (for a
patient-app answer, the patient app's own organisation) and is not used. A row
with no `patient_org_guid` can be listed by an admin and decided by nobody.

**Routes** (mounted at `/api/v1/reviews`; the request loader does not
authenticate them, the blueprint does):

| Route | Auth | Gate |
|-------|------|------|
| `GET /api/v1/reviews?status=pending\|endorsed\|rejected\|all[&patient_guid=][&limit=]` | `Authorization: Bearer <operator's SSO token>`, validated with sso on every request | `user_type = professional`, no forced password change. Sees originals whose `patient_org_guid` is a care unit where the caller's affiliation has role `doctor` or `nurse`. SU admin sees all. Anyone else: 403 `no_review_scope` |
| `POST /api/v1/reviews/<guid>/approve` body `{"value": <optional amended>, "note": "<optional>"}` | same | Affiliated to the row's `patient_org_guid` as `doctor` or `nurse`. Admin alone is not enough. 201 `{endorsement_guid, amended}` |
| `POST /api/v1/reviews/<guid>/reject` body `{"reason": "<required>"}` | same | Same gate. 422 `reason_required` without a reason. 200 |
| `GET /api/v1/reviews/by-patient/<patient guid>` | `X-Source-Service: patientapp.pdhc` + `X-Service-Key` (§5.2b) | That key only. 401 otherwise |

A decision on a guid that does not exist, on a row that is not a review item,
or on a row outside the caller's care units gets the same 403 body. A second
decision is 409 `already_decided`, told only to a caller inside the scope. No
service key opens the three professional routes.

**Approval** is one transaction: a conditional UPDATE flips the original
(`WHERE review_status = 'pending'`, so two simultaneous decisions cannot both
win), then the endorsement is written: a new `observation` row with a new
guid, the original's patient, concept, clinical time, transaction and patient
organisation, the original's value or the amended one, `data_origin` NULL,
`endorses_guid` = the original, `org_guid` = the endorsing care unit,
`reviewed_by_*` = the professional, `source = cdr.pdhc/review`; plus its own
`ingest_raw` and `clinical_context` rows (`author_org_guid` = the care unit).
Its FHIR carries `derivedFrom` -> the original, the unit and the professional
as `performer`, and no patient-generated tag. The flip writes only
`review_status`, `review_reason`, `reviewed_by_user_guid`, `reviewed_by_name`
and `reviewed_at`; the original's value, timestamps (including `updated_at`),
origin and raw FHIR are unchanged. An amended value must be of the original's
kind (number, text, yes/no, date) within the column limits, else 422.

The generic FHIR write path refuses (409) to rewrite an original or an
endorsement.

**Audit.** `audit_log`: `review.approved` and `review.rejected` in the same
transaction as the decision (actor, patient, original, endorsement, care unit,
role; no clinical value), `review.denied` for a refused decision (no patient).
The list and the patient-status read write `cdr_read_audit`.

**Readers.** `/clinical/patient/<g>/series` points and
`/clinical/observation/<g>` (`review` block) carry `review_status`,
`endorses_guid`, `reviewed_by_user_guid`, `reviewed_by_name`, `reviewed_at`,
always present, null when not applicable.

**EPS derivation.** `flask export-eps-endorsements [--patient GUID] [--out FILE]`
writes the JSON eps.pdhc derives from: endorsement rows only (`endorses_guid`
set AND `data_origin` NULL). An original is never in it, whatever its status.

### 4.4c The patient card: section versions (#1049)

Contract `plans/patient_app_contract.md` s7.4. Clinical repository only, like
§4.4b; an analysis CDR refuses a card answer with `review_not_available_here`
(a card answer is always a reviewed one) and has none of the routes.

**Storage.** Two more nullable columns beside the review columns, on every
per-type live table and its history (migration `e6f7a8b9c0d1` on
`d5e6f7a8b9c0`, additive, no backfill): `card_section` (`allergy`,
`medication`, `history`, `vaccination`, `contact`) and `card_version` (the
uuid the patient app mints for one send of one section). One index per live
table, `ix_<t>_card_version`. `review_status` gains `superseded`.

**Ingest.** gateway's s7.3 path sends, per answer, the s3.2 review context
plus `clinical_context.card_section` + `card_version` (also accepted flat).
No `service_request_guid`, care plan or contract is needed: nothing in the
ingest requires one, and v2 has no care order. Named 422s, nothing stored:
`incomplete_card` (one without the other), `invalid_card_section`,
`invalid_card_version` (not a uuid; stored lower-case canonical),
`card_requires_review` (no `review_required: true`), `card_section_mismatch`
(the linkId's prefix is another section), `card_needs_patient_org` (no care
unit: nobody could decide it), `card_version_conflict` (the version already
belongs to another patient, section or care unit) and `card_version_closed`
(the version is already decided or superseded). A performer of type
`Patient` (or a `Patient/` reference) is never read as `org_guid`: the
patient is not an organisation, so a card answer has `org_guid` NULL.

**Latest wins.** Storing a card answer marks every PENDING row of the same
patient and `card_section` with ANOTHER `card_version` as `superseded`
(`reviewed_at` = this ingest's receipt time, no reviewer, no reason), in the
ingest's own transaction. Rows of the same version, endorsed or rejected
versions, other sections, other patients and v1 answers (no
`card_section`) are untouched. "Older" means stored earlier; the CDR has no
other ordering.

**Routes** (same blueprint, same door and gate as §4.4b):

| Route | Gate / result |
|-------|---------------|
| `GET /api/v1/reviews/cards?status=pending\|endorsed\|rejected\|superseded\|all[&patient_guid=][&limit=]` | Doctor/nurse sees versions at their units, admin all, else 403 `no_review_scope`. `{items: [{card_version, patient_guid, patient_org_guid, section, status, submitted_at, decided_at, reason, rows: [{guid, link_id, group, label, value, display}], endorsed: {card_version, reviewed_at, rows: [... + original_guid]} \| null, can_decide}], total, limit, truncated}`. `endorsed` is the version currently in force for that patient and section (endorsed last, by `reviewed_at`), with the values care ENDORSED, for the diff; null when none or when it is the item itself |
| `POST /api/v1/reviews/cards/<card_version>/approve` body `{"amend": {"<row guid>": <value>}?, "note"?}` | Affiliated to the version's care unit as doctor/nurse. One endorsement row per row (§4.4b rules, `card_section`/`card_version` copied), all originals `endorsed`, one `review.card_approved` audit row, ONE transaction. 201 `{endorsed, amended, card_version, endorsement_guids, review_status}`. `amend` keys must be rows of the version (422 `unknown_row`); values validated as in §4.4b before any write; the same value is not an amendment |
| `POST /api/v1/reviews/cards/<card_version>/reject` body `{"reason"}` | Same gate; all rows `rejected` with the reason, one `review.card_rejected` row. 422 `reason_required` |
| `GET /api/v1/reviews/card/by-patient/<patient guid>` | `PATIENTAPP_PDHC_SERVICE_KEY` (§5.2b), GET only. `{sections: {<each of the five>: {latest: {card_version, submitted_at, status, reason, decided_at, answers: [{link_id, group, value, display}]} \| null, endorsed: {card_version, decided_at, answers} \| null}}}`. `latest` = the newest version that is not superseded. Answers are the patient's OWN (from the originals, never care's amended values); who decided is never returned; `reason` only when rejected; `decided_at` null while pending |

A second decision is 409 `already_decided`; a superseded version is 409
`superseded`. An unknown, malformed or out-of-scope version gets the same flat
403 as §4.4b. Every flip is the conditional UPDATE of §4.4b, so a version
superseded or decided between the check and the write leaves nothing written
and answers 409 `superseded` / `already_decided`.

**v1 alongside.** The per-row routes of §4.4b refuse a card answer with 409
`card_row` (+ `card_version`), after the scope check, so a version is never
decided piecemeal. `GET /api/v1/reviews` and `/by-patient/<g>` list only rows
WITHOUT `card_version`; card answers are in the two card routes.

**Export.** `flask export-eps-endorsements` rows also carry `card_section` and
`card_version` (null for v1); `reviewed_at` was already there. eps keeps, per
patient and section, only the rows of the latest endorsed version.

### 4.5 openEHR read

`GET /api/v1/openehr/composition/<guid>` — a single by-GUID read of a legacy
`openehr_compositions` row. The former search form
(`?patient_guid=&archetype_id=`) was moved to the analyse layer (#292); CDR 1
keeps only the storage-style per-GUID lookup.

### 4.6 Other read endpoints

- `GET /api/v1/stats` (#292 compatibility shim) — per-type row-count
  aggregate so the analyse-layer federation shape is uniform across CDRs.
- `GET /api/v1/observations/<guid>/provenance` (service-key) — a FHIR
  `collection` Bundle: the Observation plus its linked ServiceRequest,
  PlanDefinition, CarePlan, Contract, and requesting + provider Organizations,
  reconstructed from `clinical_context` and the resource's own back-refs.
  Missing context degrades gracefully.
- `GET /api/v1/cambio/status`, `/api/v1/cambio/patient/<guid>`,
  `POST /api/v1/cambio/retry` (§7).

## 5. Authentication and access control

### 5.1 Ingest (write) service keys — `KNOWN_SERVICES`

Header pair `X-Source-Service` + `X-Service-Key` (`app/api/auth.py`).
Accepted sources and the env var each key is matched against:

| Source service | Config / env var |
|----------------|------------------|
| `gateway.pdhc` | `GATEWAY_PDHC_SERVICE_KEY` |
| `2gate.pdhc` | `TWOGATE_PDHC_SERVICE_KEY` |
| `sim.pdhc` | `SIM_PDHC_SERVICE_KEY` |

### 5.2 FHIR/read trusted services — `KNOWN_FHIR_SERVICES`

Sibling services may read/write the FHIR surface with a service key
(`app/auth.py`), each keyed to its own env var:

| Source service | Config / env var | Role |
|----------------|------------------|------|
| `sim.pdhc` | `SIM_PDHC_SERVICE_KEY` | writes synthetic cohorts |
| `dashboard.pdhc` | `DASHBOARD_PDHC_SERVICE_KEY` | analyse-layer + clinical reader |
| `analyse.pdhc` | `ANALYSE_PDHC_SERVICE_KEY` | extracted analyse-layer reader (#541) |

Under `CDR_READ_LOCKDOWN`, only `dashboard.pdhc` and `analyse.pdhc` may take
a **read** path; every listed service may still **write**. A valid
service-key request gets a synthetic `is_su_admin` blob tagged with
`service_source`, which downstream code uses to distinguish machine writes
from human operators.

### 5.2b The patient app's one route — `PATIENT_STATUS_SERVICES` (#1044)

A third, separate allowlist, in `app/api/reviews.py`:

| Source service | Config / env var | Admits |
|----------------|------------------|--------|
| `patientapp.pdhc` | `PATIENTAPP_PDHC_SERVICE_KEY` | `GET /api/v1/reviews/by-patient/<patient guid>` and (#1049) `GET /api/v1/reviews/card/by-patient/<patient guid>`, nothing else |

The name is in neither `KNOWN_SERVICES` nor `KNOWN_FHIR_SERVICES`, so the key
is refused (401/403) on every other route, including the other review routes.
An unset key admits nobody.

### 5.3 SSO (human operators)

`AUTH_MODE=sso` validates the session bearer token against
`sso.pdhc /api/auth/me/service` on **every** request (no blob caching, so an
SSO logout takes effect immediately). Access requires the analysis phase gate
— `is_su_admin`, or `user_type == "professional"` with `analysis` in the
session phases. `flask create-su` bootstraps an SU. `AUTH_MODE=off` (local
dev only) loads a dev SU blob.

**Org scoping (Rule 24):** non-admin reads are filtered to the operator's
Zone-1 care-unit guids — `affiliations[].care_unit_guid`, with a dual-read
fallback to the legacy `organization_ids` (M0 #416). Admins (`is_su_admin`)
see all.

### 5.4 Analysis consent / spärr filtering (#422)

`app/services/analysis_consent.py` enforces analysis-phase consent (EHDS
opt-out, per-project research consent, quality-registry opt-out). The verdict
comes from **ips.pdhc** (`POST /api/v1/patients/analysis-filter`) — nothing
is computed locally. The operator's purpose is derived from their **active
affiliation role** (researcher → research; quality/registry → quality_registry;
other clinical → statistics; SU-admin without affiliations → administration,
never blocked). `check_patient_allowed` gates a single patient (403 on
exclusion); `consent_allowed_guids` batch-filters a search result set. **Fail
closed:** if ips is unreachable, reads abort `503`. Service-key/machine
contexts and the care-delivery surface (§4.4) pass through — the sibling
holds the real operator context.

## 6. FHIR ↔ openEHR transformation

### 6.1 Flat-ingest transformer

`app/services/transformer.py` generates an openEHR composition from a FHIR
Observation on the flat-ingest path, using a LOINC-to-archetype seed map (11
entries: body weight, blood pressure, pulse, temperature, SpO2, height,
respiration, BMI, blood glucose, waist circumference, sleep). Unknown LOINC
codes fall back to `openEHR-EHR-OBSERVATION.laboratory_test_result.v1`. This
is the only path that materialises openEHR; the per-type write path defers
openEHR and only mints the `sync_group_id`.

### 6.2 Concept display (#471)

`clinical_read.resolve_display` parses the Concept GUID out of a Path-B
`code_canonical` (`urn:pdhc:concept/<guid>`) and resolves a human label via
plan.pdhc `CodeSystem/$lookup` (cached in `PlanClient`). Cosmetic and
fail-open — a miss shows the raw code.

## 7. Cambio CDR sandbox delivery (CDR 1)

Only observations with a concept reference are eligible; unmapped data stays
local (`cambio_delivery_log.status = skipped`). When
`CAMBIO_DELIVERY_ENABLED=true`, an APScheduler background worker runs a
delivery cycle every 60 s: ensure the patient exists in Cambio (FHIR Patient
+ openEHR EHR), deliver the FHIR Observation and openEHR Composition, retry
with backoff. The X2 operator session id captured at ingest is replayed as
`X-Operator-Session-Id` on the CDR 1 → Cambio hop so chain-of-custody
survives the async gap. Status/retry via `/api/v1/cambio/*`.

## 8. Health check

`GET /healthz` (and `GET /api/v1/health`) returns:

```json
{ "status": "ok", "service": "cdr.pdhc", "database": "connected" }
```

`status` is `ok` (HTTP 200) when the DB `SELECT 1` succeeds, `degraded`
(HTTP 503) otherwise; `database` is `connected` / `unavailable`. **There is
no `version` field.** CORS headers allow `https://www.pdhc.se/services.html`
to read the body cross-origin and drive the real status/DB dots.

## 9. Configuration (environment)

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL connection string (default port 9047) |
| `AUTH_MODE` | `off` (dev) or `sso` (production) |
| `SSO_BASE_URL` / `SSO_CLIENT_ID` / `SSO_CLIENT_SECRET` / `SSO_CALLBACK_URL` | SSO validation |
| `CDR_READ_LOCKDOWN` | `false` on CDR 1, `true` on CDR 2–5 (#293) |
| `GATEWAY_PDHC_SERVICE_KEY` / `TWOGATE_PDHC_SERVICE_KEY` / `SIM_PDHC_SERVICE_KEY` | Ingest service keys |
| `DASHBOARD_PDHC_SERVICE_KEY` / `ANALYSE_PDHC_SERVICE_KEY` | Read/analyse service keys |
| `PATIENTAPP_PDHC_SERVICE_KEY` | patientapp.pdhc's key for the one patient-status route (#1044, §5.2b). Clinical repository only; empty admits nobody |
| `PLAN_BASE_URL` | plan.pdhc — canonicalisation + concept display |
| `EPS_BASE_URL` / `EPS_API_KEY` | eps.pdhc (formerly ips.pdhc) — consent analysis-filter (#422) and the external-read summary; e.g. `https://eps.pdhc.se`. Read first (#1024) |
| `IPS_BASE_URL` / `IPS_API_KEY` | **Deprecated** fallback, read only when the `EPS_*` name is empty. Both empty = not configured, no default (#833) |
| `STRICT_CANONICALISATION` | Relax plan-validate on transient unreachability when `false` |
| `CAMBIO_DELIVERY_ENABLED` | Activate the delivery worker (CDR 1) |
| `CAMBIO_CLIENT_ID` / `CAMBIO_CLIENT_SECRET` / `CAMBIO_TOKEN_URL` / `CAMBIO_BASE_URL` / `CAMBIO_TENANT` / `CAMBIO_*_HSA_ID` | Cambio sandbox credentials |

## 10. Operations

Cold start `bash start.sh`. Graceful restart of the CDR's own service only
(Rule 19 / CLAUDE.md §15) — never touch sibling services, volumes, or the
shared Colima VM. See the platform CLAUDE.md for the deploy layout, the
Postgres credential-drift trap, and the Docker/Colima gotchas.

## Port Allocation

All ports bind to `127.0.0.1` (loopback only); external traffic arrives
via the reverse proxy. Every instance runs identical software; only the
host port pair (`APP_PORT` / `DB_PORT`) differs. Inside every container
the app listens on `9046` and Postgres on `5432`.

| Instance | App (Gunicorn) | PostgreSQL |
|----------|----------------|------------|
| CDR 1 | 9046 | 9045 (local-dev default 9047) |
| CDR 2 | 9146 | 9145 |
| CDR 3 | 9246 | 9245 |
