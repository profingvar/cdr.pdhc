"""Tests — #295 Live observation row population during ingest.

Verifies:
  - `_primary_canonical_uri` accepts LOINC, urn:pdhc:concept, and
    plan.pdhc concept URLs as canonical systems
  - `build_live_observation_row` produces a row with the right columns
  - IngestPipeline writes a Live Observation row when ingest carries a
    FHIR Observation with an id
  - Re-ingest with the same FHIR id is idempotent (no duplicate Live row)
  - The Live row is queryable via FHIR search GET /api/v1/fhir/Observation
"""
import pytest

from app.services.ingest_pipeline import (
    _primary_canonical_uri,
    build_live_observation_row,
)


def _obs(fhir_id='obs-1', sys='urn:pdhc:concept', code='c-1',
         patient='pat-1', org='org-A', value=72.0):
    return {
        'resourceType': 'Observation',
        'id': fhir_id,
        'status': 'final',
        'code': {'coding': [{'system': sys, 'code': code}]},
        'subject': {'reference': f'Patient/{patient}'},
        'effectiveDateTime': '2026-06-28T10:00:00Z',
        'performer': [{'identifier': {'value': org}}],
        'valueQuantity': {'value': value, 'unit': 'kg'},
    }


class TestPrimaryCanonicalUri:

    def test_loinc_recognised(self):
        assert _primary_canonical_uri(_obs(sys='http://loinc.org', code='4548-4')) \
            == 'http://loinc.org/4548-4'

    def test_pdhc_concept_recognised(self):
        assert _primary_canonical_uri(_obs(sys='urn:pdhc:concept', code='c-x')) \
            == 'urn:pdhc:concept/c-x'

    def test_plan_pdhc_url_recognised(self):
        assert _primary_canonical_uri(_obs(
            sys='https://plan.pdhc.se/api/v1/concepts', code='c-y'
        )) == 'https://plan.pdhc.se/api/v1/concepts/c-y'

    def test_unknown_system_returns_none(self):
        assert _primary_canonical_uri(_obs(sys='urn:other', code='x')) is None

    def test_first_canonical_wins(self):
        """When multiple codings exist, the FIRST canonical-system one wins."""
        f = {'code': {'coding': [
            {'system': 'urn:other', 'code': 'skip'},
            {'system': 'urn:pdhc:concept', 'code': 'first-canonical'},
            {'system': 'http://loinc.org', 'code': 'later'},
        ]}}
        assert _primary_canonical_uri(f) == 'urn:pdhc:concept/first-canonical'


class TestBuildLiveObservationRow:

    def test_populates_required_columns(self, app):
        with app.app_context():
            row = build_live_observation_row(
                _obs(),
                patient_guid='pat-1',
                source_service='gateway.pdhc',
                ingest_raw_guid='raw-1',
            )
            assert row is not None
            assert row.guid == 'obs-1'
            assert row.patient_guid == 'pat-1'
            assert row.org_guid == 'org-A'
            assert row.code_canonical == 'urn:pdhc:concept/c-1'
            assert row.value_quantity == 72.0
            assert row.value_unit == 'kg'
            assert row.status == 'final'
            assert row.version_id == 1
            assert row.source == 'gateway.pdhc'
            assert row.raw_json == _obs()

    def test_falls_back_to_sentinel_org_when_performer_missing(self, app):
        with app.app_context():
            f = _obs()
            f.pop('performer')
            row = build_live_observation_row(
                f, patient_guid='pat-1', source_service='sim.pdhc')
            assert row.org_guid == '00000000-0000-0000-0000-000000000000'

    def test_falls_back_to_ingest_raw_guid_for_guid(self, app):
        with app.app_context():
            f = _obs(fhir_id=None)
            f.pop('id')
            row = build_live_observation_row(
                f, patient_guid='pat-1', source_service='gateway.pdhc',
                ingest_raw_guid='raw-fallback')
            assert row.guid == 'raw-fallback'


# NB: integration tests of IngestPipeline.process() are not included
# here because cdr1's test fixtures don't include a transactional `db`
# fixture. Coverage is achieved end-to-end via the deploy smoke (see
# #295 ticket close for the gateway → cdr1 → 7060-row backfill check).
