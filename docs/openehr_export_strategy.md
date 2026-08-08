---
title: "Exporting PDHC data to a general openEHR CDR"
subtitle: "Assessment, strategy, and execution guide"
author: "PDHC platform — cdr.pdhc"
date: "2026-07-21"
---

# How to use this document

This is both a **findings report** and a **learning guide**. It is written for
someone who knows PDHC well but is new to openEHR interoperability, and who has
to actually execute the work.

- **Part 1** teaches the openEHR concepts you need. Read this first; the rest
  will not make sense without it.
- **Part 2** states what PDHC has today, with evidence from the running system.
- **Part 3** explains precisely why today's output cannot be sent to a general
  openEHR CDR.
- **Part 4** is the recommended strategy.
- **Part 5** is how to prove a target server can accept your data.
- **Part 6** is the execution plan and the tickets that track it.

A one-line summary, if you read nothing else:

> PDHC does not currently produce interoperable openEHR. The stored
> compositions are an openEHR-*shaped* internal artifact that a conformant
> server would reject. The correct move is to **regenerate** compositions from
> the canonical observation data against a real operational template — not to
> migrate the ones we have.

> **Update (2026-07-22) — consolidation decision.** All openEHR production is
> being consolidated onto **`rosetta.pdhc`**, and `cdr.pdhc`'s parallel
> implementation retired. Two independent openEHR emitters existed
> (`cdr/transformer.py` and `rosetta/openehr_converter.py`) and made the same
> mistakes; the concern was homeless. Rosetta is the right home — its schema is
> already the canonical-core-plus-projections shape, it is closer to
> conformant, it carries the right security posture, and it is empty (nothing to
> migrate). Rosetta **renders and delivers** openEHR; `cdr` keeps FHIR/raw and
> the canonical store. The first live target is an **external sandbox openEHR
> CDR**, exercised from rosetta. Tracked by rollup **#511** (this supersedes the
> earlier #487 plan). See Part 6 for the full ticket map.

\newpage

# Part 1 — openEHR in ten minutes

openEHR and FHIR solve different problems, and the difference is the single
biggest source of confusion when moving between them.

**FHIR** is a wire format for exchange. A server publishes a
`CapabilityStatement`, and if you send a structurally valid `Observation`, it
will generally accept it. Meaning is carried by terminology codes (LOINC,
SNOMED).

**openEHR** is a modelling architecture for the record itself. Meaning is
carried by **structure** — where a value sits in a defined tree — rather than
primarily by codes. This has a direct consequence that catches everyone:

> An openEHR CDR will reject a composition whose **operational template** it
> does not have. Capability is per-template, not per-server.

## The five concepts

**Reference Model (RM).** The fixed set of building blocks: `COMPOSITION`,
`OBSERVATION`, `ITEM_TREE`, `ELEMENT`, and data types like `DV_QUANTITY`,
`DV_CODED_TEXT`, `DV_TEXT`, `DV_BOOLEAN`, `DV_DATE_TIME`. The RM never changes
per project. Every node in a real composition carries `_type` and
`archetype_node_id`.

**Archetype.** A maximal, reusable definition of one clinical concept — for
example `openEHR-EHR-OBSERVATION.body_weight.v2`. Written in ADL, published in
the international **CKM** (Clinical Knowledge Manager). You normally *reuse*
these; authoring new ones is a governance exercise.

**Template / OPT.** A template selects and constrains archetypes for one
specific use case — "the vitals form we actually use". The **operational
template (`.opt`)** is the flattened, fully-resolved form the server consumes.
**This is the artifact that must exist on both sides.** PDHC currently has none.

**Composition.** The unit committed to an EHR — one clinical document. It has
mandatory attributes: `language`, `territory`, `category`, `composer`,
`archetype_details` (with `template_id`), and `context` for event compositions.

**EHR and AQL.** Each patient has an **EHR** identified by a UUID, whose
`EHR_STATUS` carries `subject.external_ref` — the namespace + id that lets you
find a patient by *your* identifier. **AQL** (Archetype Query Language) is how
you query across compositions; it is also the only honest way to prove data
landed correctly.

## Composition serialisation formats

This choice drives most of the implementation effort.

| Format | What it looks like | Effort |
|---|---|---|
| **Canonical** JSON/XML | The full RM tree, every `_type` and `archetype_node_id` explicit | Highest — you build the whole tree |
| **FLAT / simSDT** | Dot-separated paths against a web template, `{"vitals/body_weight/.../weight\|magnitude": 72.5}` | **Lowest** — server expands it |
| **STRUCTURED** | Nested JSON mirroring template structure | Middle |

**Recommendation: FLAT.** It removes most of the code that is currently wrong,
because the server does the RM scaffolding for you. Both EHRbase and Better
support it.

\newpage

# Part 2 — What PDHC has today

All statements below were verified against the code and the production
database on 2026-07-21.

## The pipeline is FHIR end-to-end

`gateway.pdhc` builds FHIR R5 `Observation` resources and forwards them to
CDR1. `source_type: 'fhir'` is hardcoded and there is no openEHR branch in the
forwarder. CDR1 then *derives* an openEHR composition from that FHIR in
`cdr_app/app/services/transformer.py` — roughly 90 lines of hand-written
dictionary literals.

## Finding 1 — the composition is openEHR-flavoured, not RM-conformant

It borrows canonical key names (`_type`, `archetype_details`, `DV_QUANTITY`)
but omits nearly everything the RM requires.

Actual stored composition from production (values shortened):

```json
{
  "uid": "3b7f0013-c361-404d-8599-cdb43babcc86",
  "composition": {
    "_type": "COMPOSITION",
    "name": {"value": "smoke-408x2"},
    "archetype_details": {
      "archetype_id": {"value": "openEHR-EHR-COMPOSITION.report-result.v1"},
      "template_id": {"value": "generic"}
    },
    "context": {
      "start_time": {"value": "2026-07-08T21:09:21.901459+00:00"},
      "setting": {"value": "other care", "defining_code": {"code_string": "238"}}
    },
    "content": [{
      "_type": "OBSERVATION",
      "archetype_details": {"archetype_id": {"value": "openEHR-EHR-OBSERVATION.laboratory_test_result.v1"}},
      "data": {"events": [{
        "time": {"value": "2026-07-08T21:09:21.901459+00:00"},
        "data": {"items": [{"value": {"_type": "DV_QUANTITY", "magnitude": 4.08, "units": "mg/L"}}]}
      }]}
    }]
  }
}
```

Missing, each individually fatal to validation:

- **No `archetype_node_id` anywhere.** Every RM node requires one.
- **No `language`, `territory`, `category`, `composer`** — all mandatory on
  `COMPOSITION`.
- **No `ITEM_TREE` / `ELEMENT` layer.** `data.items[]` jumps straight to
  `{"value": {...}}`. The string `_type: "ELEMENT"` does not exist anywhere in
  the codebase.
- **No `rm_version`**, and `archetype_details` lacks `_type: "ARCHETYPED"`.
- `name` is a bare `{"value": ...}` with no `_type: "DV_TEXT"`.
- `setting` has a `defining_code` with **no `terminology_id`**, so it is not a
  valid `CODE_PHRASE`.
- **No subject.** The composition carries no patient reference at all.

## Finding 2 — all 7,065 compositions are semantically wrong

Production query result:

```
 template_id | archetype_id                                       | count
 generic     | openEHR-EHR-OBSERVATION.laboratory_test_result.v1  |  7065
```

Every composition, without exception, is stored as a *laboratory test result*.

**Root cause.** `transformer.py` chooses the archetype from a hardcoded
**LOINC** lookup table. But gateway emits **PDHC concept GUIDs**
(`urn:pdhc:concept`) and never LOINC. The lookup therefore misses 100% of the
time and every observation falls through to the lab-result fallback. Body
weight, blood pressure, pulse — all labelled as lab results.

The `loinc_archetype_map` table that was designed to drive this is **dead
code**: the model is defined, but nothing queries it, inserts into it, or seeds
it. The transformer comment claims it is "loaded from DB at init"; that load
does not exist.

## Finding 3 — this has never been validated against a real openEHR server

`CAMBIO_DELIVERY_ENABLED=false` in production. The delivery log contains
exactly **2** openEHR rows, both `pending`, **zero delivered**. So the belief
"we already produce openEHR" has never been tested against anything that
validates.

## Finding 4 — supporting infrastructure is absent

- **No template artifacts.** No `.opt`, `.adl`, or `.oet` files exist in the
  repo. `template_id` is the literal string `"generic"` — and is in fact never
  written by any code path, so it is always the column default.
- **No AQL.** No AQL string, no `/query/aql` call, no query builder. The
  "AQL-lite" search endpoint was removed; only read-by-GUID survives.
- **No UCUM validation or unit conversion.** Whatever display string arrives
  lands directly in `DV_QUANTITY.units`. A source sending `"lbs"` for body
  weight produces a valid-looking composition with `units: "lbs"`.
- **Coded values essentially unhandled.** `DV_CODED_TEXT` appears nowhere.
  LOINC coding is discarded in the FHIR→openEHR direction.

## Finding 5 — upstream value-typing defects

These originate in gateway and corrupt data before openEHR is even reached.

- Every value is passed through `float()`, so a categorical answer of `"120"`
  silently becomes a numeric `valueQuantity` — with the concept's unit attached.
- Booleans become the **strings** `"True"` / `"False"`.
- `unit_display` (human-readable) is used as the machine `code`.
- `plan_definition_guid`, `requesting_org_guid` and `requester_user_guid` never
  reach CDR1, because the forwarder calls the builder with `sr_contexts=None`.

\newpage

# Part 3 — Why today's output cannot be transferred

If you POST a current composition to a conformant openEHR CDR, it fails
validation before it is ever stored. Concretely:

1. **No template.** `template_id: "generic"` does not identify a real OPT. The
   server has nothing to validate against and rejects immediately.
2. **Missing mandatory RM attributes** — `language`, `territory`, `category`,
   `composer` — each a hard validation error.
3. **Missing `archetype_node_id`** on every node — the server cannot bind any
   value to a template path.
4. **Missing `ELEMENT` layer** — values sit at a path that does not exist in any
   archetype.
5. **No subject** — the server cannot associate the composition with an EHR.
6. **Wrong archetype** — even if all the above were fixed, the data would be
   filed as lab results.

This is why the recommendation is **regenerate, not migrate**. Transforming the
existing `composition_json` would carry defects 4, 5 and 6 into the target
system, where they become much more expensive to correct.

\newpage

# Part 4 — Recommended strategy: template-first

> **Where this work lives.** Per the consolidation decision (#511), all of the
> build steps below are implemented in **`rosetta.pdhc`**, not `cdr.pdhc`.
> Rosetta already holds the right schema (a canonical cache plus FHIR / openEHR /
> OMOP projections) and the right security posture, and is empty of data.
> `gateway.pdhc` supplies the canonical `observation` (Steps 0 below); rosetta
> renders and delivers; `cdr`'s duplicate emitter is retired afterwards (#509).

## Step 0 — Fix the source before rendering (gateway)

Two gateway defects corrupt values *before* any rendering and must be fixed
first, or rosetta will faithfully reproduce the corruption: numeric coercion of
every value (categorical `"120"` → quantity) and booleans serialised as the
strings `"True"`/`"False"` (#489), plus provenance dropped because the forwarder
passes `sr_contexts=None` (#490). Then gateway emits a canonical `observation`
block in its envelope (#500) as the single source both projections read.

## Step 1 — Model before you code

The real work is clinical modelling, not Python. Using **Archetype Designer**
(free, web-based), build templates over existing CKM archetypes and export
operational templates (`.opt`).

Start with the smallest set that covers real traffic — likely three:

- a **vitals** template (weight, height, BP, pulse, temperature, SpO2),
- a **laboratory result** template,
- a **questionnaire / patient-reported** template.

Commit the `.opt` files to the repo. They are source artifacts, not build
output.

*This step is a prerequisite for every following step. Nothing can be validated
until an OPT exists.*

## Step 2 — Re-key the archetype mapping on `concept_guid`

The dead `loinc_archetype_map` table has the right idea and the wrong key.
Replace it with a mapping keyed on what PDHC actually carries:

```
concept_guid → (template_id, archetype_id, archetype_node_id, ucum_unit)
```

The unit is canonical on the plan.pdhc concept, so the UCUM code should be
sourced from there — not from `unit_display`. Seed it, and make unmapped
concepts a **loud failure** rather than a silent fallback. The current silent
fallback is exactly what produced 7,065 mislabelled rows.

## Step 3 — Emit FLAT (simSDT), not canonical

Replace the hand-rolled dict builder with a FLAT emitter:

```json
{
  "vitals/body_weight/any_event:0/weight|magnitude": 72.5,
  "vitals/body_weight/any_event:0/weight|unit": "kg",
  "vitals/context/start_time": "2026-07-08T21:09:21Z",
  "vitals/language": "sv",
  "vitals/territory": "SE",
  "vitals/composer|name": "gateway.pdhc"
}
```

The server expands this against the OPT and builds the RM tree. This eliminates
the entire class of defects in Finding 1.

Response types then map cleanly:

| PDHC `response_type` | openEHR data type |
|---|---|
| `numeric` | `DV_QUANTITY` (with UCUM unit) |
| `categorical` | `DV_CODED_TEXT` |
| `boolean` | `DV_BOOLEAN` |
| `dateTime` | `DV_DATE_TIME` |
| `text` | `DV_TEXT` |

Note this requires fixing the upstream coercion bugs (Finding 5) first, or
booleans will arrive as text and categoricals as quantities.

## Step 4 — Establish openEHR identity

Create one **EHR per patient**, with:

```json
"subject": {"external_ref": {
  "namespace": "urn:pdhc:patient-guid",
  "id": {"value": "<pdhc patient_guid>"},
  "type": "PERSON"
}}
```

Then persist the returned `ehr_id` against the patient. Today the composition
carries no subject at all, and the existing Cambio path uses *Cambio's* patient
id under namespace `"cambio"` — so the PDHC guid never appears on the openEHR
side. Agreeing the namespace with the receiving party is a **contractual**
decision, not a technical one.

## Step 5 — Generate from the canonical layer

Build compositions from the `observation` data, not from the stored
`composition_json`. Treat the existing 7,065 compositions as disposable derived
data. Keep the old table until the new path is verified, then drop it.

\newpage

# Part 5 — Proving a target server can receive your data

## The question to ask first

> **Can we upload our own operational template, or must we use templates you
> already host?**

If you control templates (typical for EHRbase), Steps 1–3 apply as written. If
the vendor controls them, Steps 1–2 become *"remodel PDHC data onto their
template"* — a different and larger project. Ask before designing anything.

## Capability probe, in order

**1. Does it speak the openEHR REST API, and which version?**

```
GET /rest/openehr/v1/definition/template/adl1.4    # lists uploaded OPTs
GET /rest/status                                   # EHRbase-specific
```

The spec'd base path is `/rest/openehr/v1`. Vendors differ — EHRbase, Better,
DIPS, Cambio, Code24. Some also expose ADL2 at `/definition/template/adl2`.

**2. Does it hold *your* template?** If your `template_id` is not in that list,
every composition POST fails. Upload it:

```
POST /rest/openehr/v1/definition/template/adl1.4
Content-Type: application/xml
```

Whether this succeeds *is* the answer to the question above.

**3. Which composition formats does it accept?** Canonical, FLAT, or
STRUCTURED — this varies by vendor and is the most common integration surprise.
Test with the format you intend to send.

**4. Round-trip — the only proof that counts.**

```
POST /rest/openehr/v1/ehr                          # create EHR
POST /rest/openehr/v1/ehr/{ehr_id}/composition     # commit one canary
POST /rest/openehr/v1/query/aql                    # read it back
```

Read back via **AQL**, not via composition GET — AQL exercises the server's path
indexing, which is where template mismatches actually surface:

```sql
SELECT c/uid/value,
       o/data[at0001]/events[at0002]/data[at0003]/items[at0004]/value/magnitude
FROM EHR e CONTAINS COMPOSITION c
     CONTAINS OBSERVATION o[openEHR-EHR-OBSERVATION.body_weight.v2]
WHERE c/archetype_details/template_id/value = 'pdhc_vitals.v1'
```

If magnitude **and** unit come back matching the source, you have a real
capability proof. Anything less is not evidence.

## Probe result — the external sandbox (2026-07-23, #508)

The operator's sandbox is **`https://openehr.phanera.se`** — Phanera's
**"ASHA-PDHC"**, a custom ASP.NET Core teaching platform that orchestrates
docker-based openEHR CDR instances (backend behaviour consistent with EHRbase).
Read-only probe findings:

- **Can we upload our own OPT? — YES.** `/Tools/Templates` accepts a multipart
  `.opt`/`.xml` upload (with versioning). Instance **`cdr1` ("PDHC CDR") is
  empty** — a greenfield target for our first template. This answers the gating
  question in the affirmative: we control templates.
- **Composition ingest is FLAT JSON** (single) or **CSV** (bulk, against a
  template) — which **confirms Step 3's FLAT recommendation** directly from the
  target.
- **AQL is available** (`/Tools/Aql`) — the round-trip read surface for Step 4.
- **Auth is ASP.NET form login** (session cookie + antiforgery token), **not**
  HTTP Basic / OAuth2 / a REST bearer.
- **The standard openEHR REST API (`/rest/openehr/v1`) is *not* exposed** — it
  404s even when authenticated. All access is via the app's page handlers.

**Consequence for delivery (#506).** Because there is no spec REST endpoint,
delivering *into this sandbox* means driving the app's handlers (login →
antiforgery → multipart POST of FLAT JSON, or CSV bulk-import), i.e.
UI-automation rather than a REST client. Three options: (a) rosetta scripts the
app handlers; (b) ask Phanera to expose the backend EHRbase REST with a token;
(c) treat this sandbox as *learning-only* — walk its 4-step guide by hand — and
point rosetta's production delivery at a spec-REST CDR later. The emitter format
(#504, FLAT) is unaffected and confirmed either way.

## A second test target you already have

Cambio's `service.xcdr` sandbox is a real openEHR CDR, and the client code
already exists in `cambio_client.py` (EHR create, composition POST, OAuth2).
Delivery is currently disabled by flag.

Enabling it against the **sandbox** with the current payload would produce real
validation errors — which is genuinely the fastest way to learn the target's
strictness. Treat that as a deliberate diagnostic experiment, not as a deploy,
and read the resulting 4xx bodies carefully.

\newpage

# Part 6 — Execution plan

Recommended order. Steps 1–2 are prerequisites for everything else; the two
gateway defects should be fixed early because they corrupt data at source.

Tracked under rollup **#511** (`cdr.pdhc`). This replaces the earlier #487
set, which was re-issued after the consolidation onto `rosetta.pdhc` (the old
tickets are closed with forward pointers to their successors).

| Ticket | Service | Work | Depends on | Kind |
|---|---|---|---|---|
| **#489** | gateway | Fix value-typing defects (bool→string, categorical→quantity) | — | bug |
| **#490** | gateway | Fix forwarder context loss + `row.value` crash | — | bug |
| **#500** | gateway | Emit canonical `observation` block in the envelope | #489, #490 | build |
| **#508** | rosetta | Capability probe of the external sandbox CDR | — | investigation |
| **#501** | rosetta | Re-point input: canonical observation, not gateway FHIR | #500 | build |
| **#502** | rosetta | Author operational templates (`.opt`) | #508 | **modelling** |
| **#503** | rosetta | concept_guid → archetype/template map | #502 | build |
| **#504** | rosetta | FLAT (simSDT) emitter from canonical | #501, #502, #503 | build |
| **#505** | rosetta | EHR identity + subject external_ref | #502 | build |
| **#506** | rosetta | openEHR REST delivery client → external sandbox | #504, #505, #508 | build |
| **#507** | rosetta | AQL round-trip conformance harness | #506 | verification |
| **#509** | cdr | Clip redundant openEHR software once rosetta is green | #504, #506 | cleanup |
| **#510** | rosetta | Fix OMOP concept-id type violation | — | bug |

**Critical path runs through #502 (templates)** — the only step that is not
primarily software. The **external sandbox** is the first delivery and
conformance target (#506/#507/#508); Cambio's `service.xcdr` remains a later,
Cambio-specific target.

**Redundant software is clipped, not left in place (#509).** Once rosetta's
emitter and delivery round-trip green against the sandbox, `cdr`'s parallel
openEHR path — `transformer.py`, `openehr_api.py`, the Cambio openEHR delivery
branch, the dead `loinc_archetype_map`, and the `openehr_compositions` table
(all 7,065 rows mislabelled, disposable) — is removed. Not before: while broken,
it is still `cdr`'s only openEHR path.

**The critical path runs through step 4.** It is the only step that is not
primarily a software task, and it is the one most likely to be underestimated.
Budget real clinical-modelling time for it, and expect to iterate with whoever
owns the receiving system.

## Glossary

| Term | Meaning |
|---|---|
| **RM** | Reference Model — openEHR's fixed structural building blocks |
| **Archetype** | Reusable maximal definition of one clinical concept (ADL) |
| **CKM** | Clinical Knowledge Manager — the international archetype registry |
| **Template / OPT** | Use-case-specific constraint over archetypes; `.opt` is the runtime form |
| **Composition** | One clinical document committed to an EHR |
| **EHR** | Per-patient container, identified by UUID |
| **AQL** | Archetype Query Language |
| **FLAT / simSDT** | Path-based flat JSON serialisation of a composition |
| **UCUM** | Unified Code for Units of Measure — the expected unit coding |
| **DV_\*** | RM data types (`DV_QUANTITY`, `DV_CODED_TEXT`, …) |

---

*Prepared 2026-07-21. Findings verified against `cdr.pdhc` / `gateway.pdhc`
source and the CDR1 production database. Tracked by tickets listed in the
covering note.*
