"""#733 — the concept inventory, and its k-floor.

/stats says HOW MUCH data a CDR holds. A researcher writing a cohort predicate
needs to know WHAT they can ask about, and nothing exposed that — the analyse
landing page could show counts per source and not one concept name.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from app.models import db
from app.models.resources import live_model

from tests.test_ingest import SAMPLE_INGEST_BODY  # noqa: F401  (shared fixtures)

BAD = "urn:pdhc:concept/smoke-not-a-guid"


def canon(tag: str) -> str:
    """A VALID Path-B canonical unique to each test.

    Two things this has to get right. The `app` fixture is session-scoped, so
    rows seeded by one test are still there for the next — the first version
    shared one constant and the counts accumulated across tests, the same leak
    that 403'd five innocent provenance tests under #732. And the tail must be
    a real GUID: the version before this interpolated the tag into the first
    segment (`mix-0000-...`), which is not hex, so every test that believed it
    was seeding a resolvable concept was seeding an unbound one.
    """
    h = hashlib.sha1(tag.encode()).hexdigest()
    return (f"urn:pdhc:concept/{h[:8]}-{h[8:12]}-4{h[13:16]}-"
            f"8{h[17:20]}-{h[20:32]}")


def _seed(app, canon, n_patients, obs_each=1, unit=None):
    Obs = live_model("Observation")
    with app.app_context():
        for p in range(n_patients):
            for _ in range(obs_each):
                raw = {"resourceType": "Observation"}
                if unit:
                    raw["valueQuantity"] = {"unit": unit, "code": unit}
                db.session.add(Obs(
                    guid=str(uuid.uuid4()), patient_guid=f"pat-{canon[-4:]}-{p}",
                    org_guid="00000000-0000-0000-0000-000000000000",
                    code_canonical=canon, raw_json=json.dumps(raw)))
        db.session.commit()


def _get(client):
    r = client.get("/api/v1/concept-inventory")
    assert r.status_code == 200, r.data[:200]
    return r.get_json()


def test_a_concept_held_for_few_patients_is_SUPPRESSED(client, app):
    """The reason the k-floor is not optional: a per-concept patient count is a
    disclosure surface. Two patients, named and counted, is a smaller crowd to
    hide in than any analysis result this platform will publish."""
    c = canon("aaaaaaaa"); _seed(app, c, n_patients=2)
    body = _get(client)
    hit = [x for x in body["concepts"] if x["code_canonical"] == c]
    assert hit, body
    assert hit[0]["patients"] == f"<{body['k_min']}", hit[0]
    assert isinstance(hit[0]["patients"], str), "a small count must not be a number"


def test_a_concept_at_or_above_k_reports_the_real_count(client, app):
    c = canon("bbbbbbbb"); _seed(app, c, n_patients=6)
    hit = [x for x in _get(client)["concepts"] if x["code_canonical"] == c][0]
    assert hit["patients"] == 6


def test_observation_counts_are_NOT_floored(client, app):
    """They say how much data exists, not how many people it describes."""
    c = canon("cccccccc"); _seed(app, c, n_patients=2, obs_each=3)
    hit = [x for x in _get(client)["concepts"] if x["code_canonical"] == c][0]
    assert hit["observations"] == 6
    assert hit["patients"] == "<5"


def test_an_unresolvable_canonical_is_LISTED_not_hidden(client, app):
    """#731 found four of these in cdr1. Hiding them would make this endpoint
    quietly disagree with /stats."""
    _seed(app, BAD, n_patients=6)
    body = _get(client)
    assert any(u["code_canonical"] == BAD for u in body["unresolved"]), body
    assert not any(c["code_canonical"] == BAD for c in body["concepts"])
    bad = [u for u in body["unresolved"] if u["code_canonical"] == BAD][0]
    assert bad["concept_guid"] is None
    assert "CodeSystem" in bad["why_unresolved"]
    assert bad["binding"] == "unbound"


def test_units_are_collected_from_the_fhir_body(client, app):
    """The unit is not a column; it lives in raw_json. #752's unit-disagreement
    check needs it, so the inventory has to carry it."""
    c = canon("dddddddd"); _seed(app, c, n_patients=6, unit="mmol/L")
    hit = [x for x in _get(client)["concepts"] if x["code_canonical"] == c][0]
    assert hit["units"].get("mmol/L") == 6, hit["units"]


def test_it_returns_no_patient_identifiers_at_all(client, app):
    """Aggregate only. A single patient_guid in this payload would turn an
    inventory into a disclosure."""
    _seed(app, canon("eeeeeeee"), n_patients=6)
    text = json.dumps(_get(client))
    assert "pat-" not in text, "a patient guid leaked into the inventory"
    assert "patient_guid" not in text


# ---------------------------------------------------------------------------
# Portability across CDRs (#733)
#
# cdr2-cdr5 run the same codebase without app/api/clinical_read.py. The
# inventory must answer there, and it must classify canonicals the SAME way it
# does on cdr1 — otherwise one source calls a code resolvable and another calls
# it unresolved, and the merged inventory in analyse disagrees with itself.
# ---------------------------------------------------------------------------

CANON_CASES = [
    "urn:pdhc:concept/0b9c1f2e-3a4d-5b6c-7d8e-9f0a1b2c3d4e",
    "urn:pdhc:concept/not-a-guid",
    "urn:pdhc:concept/",
    "https://plan.pdhc.se/api/v1/concepts/0b9c1f2e-3a4d-5b6c-7d8e-9f0a1b2c3d4e",
    "http://termbank.pdhc.se/CodeSystem/foo|bar",
    "SMOKE-1",
    "",
    None,
]


def test_the_fallback_parser_agrees_with_the_real_one():
    from app.api.clinical_read import _concept_guid_from_canonical as real
    from app.api.stats import _canon_guid_fallback as fb
    for c in CANON_CASES:
        assert fb(c) == real(c), c


def test_inventory_answers_on_a_cdr_without_the_clinical_read_surface(
        client, app):
    """The import that used to be module-level. Simulate cdr2-cdr5, where
    ``app/api/clinical_read.py`` is not deployed at all, and go through the
    ROUTE: with the old module-level import this call was a 500, which is why
    asserting on the helper alone would not have caught it."""
    _seed(app, canon("noread"), n_patients=6, unit="mmol/L")

    import builtins
    real_import = builtins.__import__

    def no_clinical_read(name, *a, **kw):
        if name == "app.api.clinical_read":
            raise ImportError("No module named 'app.api.clinical_read'")
        return real_import(name, *a, **kw)

    builtins.__import__ = no_clinical_read
    try:
        r = client.get("/api/v1/concept-inventory")
    finally:
        builtins.__import__ = real_import

    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    # It still reports the inventory; only the cosmetic display is gone.
    assert body["distinct"] >= 1
    plan = [c for c in body["concepts"] if c["binding"] == "plan_concept"]
    assert plan, body["bindings"]
    # Only the plan.pdhc display is lost — the guid parse is pure and an
    # external code still names itself.
    assert all(c["concept_guid"] for c in plan)
    assert all(c.get("display") is None for c in plan)
    assert all(c["display"] for c in body["concepts"]
               if c["binding"] == "external_code")


# ---------------------------------------------------------------------------
# Binding kinds (#733)
#
# cdr1 stores Path-B concept GUIDs; cdr2 and cdr3 store termbank CodeSystem
# URIs (LOINC) for all 2.6M of their observations. The first version of this
# endpoint had one bucket for "a plan.pdhc GUID" and one for "unresolvable",
# so every concept cdr2 and cdr3 hold landed in the second — and analyse
# merges only the first, which made the platform's two largest sources read as
# empty on the researcher's landing page.
# ---------------------------------------------------------------------------

LOINC = "https://termbank.pdhc.se/CodeSystem/loinc/41653-7"


def test_a_termbank_code_is_a_BINDING_not_a_failure(client, app):
    _seed(app, LOINC, n_patients=6, unit="mmol/L")
    body = _get(client)
    hit = [c for c in body["concepts"] if c["code_canonical"] == LOINC]
    assert hit, "a LOINC code must be askable, not filed as unresolvable"
    c = hit[0]
    assert c["binding"] == "external_code"
    assert c["code"] == "41653-7"
    assert c["code_system_name"] == "loinc"
    assert c["concept_guid"] is None
    # It names itself — no plan.pdhc round trip for a code that is already
    # interoperable.
    assert c["display"] == "loinc 41653-7"
    assert not any(u["code_canonical"] == LOINC for u in body["unresolved"])


def test_the_binding_mix_is_reported(client, app):
    _seed(app, canon("mix"), n_patients=6)
    _seed(app, LOINC, n_patients=6)
    _seed(app, BAD, n_patients=6)
    b = _get(client)["bindings"]
    assert b["plan_concept"] >= 1 and b["external_code"] >= 1 and b["unbound"] >= 1


def test_the_unit_column_is_used_when_raw_json_has_none(client, app):
    """cdr2/cdr3 fill value_unit and their raw_json carries no valueQuantity.
    The first version read units from raw_json ONLY, so those two CDRs — the
    ones with 2.6M rows each — reported no unit for anything."""
    from app.models.resources import live_model
    Obs = live_model("Observation")
    key = canon("colonly")
    with app.app_context():
        for p in range(6):
            db.session.add(Obs(
                guid=str(uuid.uuid4()), patient_guid=f"colonly-{p}",
                org_guid="00000000-0000-0000-0000-000000000000",
                code_canonical=key, value_unit="cm",
                raw_json=json.dumps({"resourceType": "Observation"})))
        db.session.commit()
    c = [x for x in _get(client)["concepts"] if x["code_canonical"] == key][0]
    assert c["units"] == {"cm": 6}
