
## 2026-10-21 — #711 was never actually mitigated here
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/docker-compose.yml
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/__init__.py
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/.env.example
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/requirements.txt

## 2026-11-20 — #682 retire cdr's openEHR (rosetta renders openEHR)
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/ingest_pipeline.py — no openEHR store/generate; an openehr_composition is refused (422 openehr_input_retired)
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/transformer.py — DELETED
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/openehr_api.py — DELETED
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/__init__.py, /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/__init__.py — openehr blueprint unregistered
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/auth.py — /api/v1/openehr/ read path removed
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/api/stats.py — openehr_compositions count removed
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/models/__init__.py — OpenEhrComposition, LoincArchetypeMap, CambioDeliveryLog.openehr_comp_guid removed
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/cambio_client.py — EHR get/create and deliver_openehr_composition removed; ensure_patient returns the patient id
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/services/cambio_worker.py — openehr branch removed
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/templates/dashboard.html, /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/app/templates/docs.html
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/migrations/versions/b0c1d2e3f4a5_682_retire_openehr.py — NEW
- /Users/martiningvar/T7_sidewinder/cdr.pdhc/cdr_app/tests/test_transformer.py — DELETED; test_ingest.py (+2), test_cambio_synthetic_1000.py, test_session_propagation.py, test_author_org.py
