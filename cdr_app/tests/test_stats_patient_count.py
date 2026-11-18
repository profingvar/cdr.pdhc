"""/stats `patients` counts every patient the CDR holds data for (2026-11-18).

It counted the Patient table and fell back to ingest_raw only when that table
was EMPTY. cdr1 held 2 Patient rows (gateway writes them) and observations for
8 patients (sim writes no Patient), so analyse showed 2.
"""
import json
import uuid

from app import db
from app.models.resources import live_model


def _add(app, *, patients=(), obs_for=()):
    P, O = live_model("Patient"), live_model("Observation")
    with app.app_context():
        for g in patients:
            db.session.add(P(guid=g, org_guid="00000000-0000-0000-0000-000000000000",
                             raw_json=json.dumps({"resourceType": "Patient", "id": g})))
        for g in obs_for:
            db.session.add(O(guid=str(uuid.uuid4()), patient_guid=g,
                             org_guid="00000000-0000-0000-0000-000000000000",
                             code_canonical="urn:pdhc:concept/x",
                             raw_json=json.dumps({"resourceType": "Observation"})))
        db.session.commit()


def _n(client):
    return client.get("/api/v1/stats").get_json()["patients"]


def _baseline(client, app):
    """A non-empty union first, so the ingest_raw fallback (used only when
    there is neither a Patient nor an observation) cannot shape the delta."""
    _add(app, obs_for=[str(uuid.uuid4())])
    return _n(client)


def test_patients_with_only_observations_are_counted_beside_patient_rows(client, app):
    before = _baseline(client, app)
    with_row = str(uuid.uuid4())
    obs_only = [str(uuid.uuid4()) for _ in range(3)]
    _add(app, patients=[with_row], obs_for=[with_row, *obs_only, obs_only[0]])
    # 1 with a Patient row (and its own observation, not double-counted)
    # + 3 known only through observations (one of them twice) = 4 new.
    assert _n(client) - before == 4


def test_a_patient_row_without_observations_still_counts(client, app):
    before = _baseline(client, app)
    _add(app, patients=[str(uuid.uuid4())])
    assert _n(client) - before == 1
