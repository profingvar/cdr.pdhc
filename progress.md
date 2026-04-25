# cdr.pdhc — Progress

## Status

Implementation in progress. Core service built 2026-04-10.
15 tests passing (health, ingest, transformer, cambio).

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
