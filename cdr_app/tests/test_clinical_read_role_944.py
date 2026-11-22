"""#944: one codebase for every CDR; the clinical read surface is a ROLE.

cdr1 (clinical) registers /api/v1/clinical/*; cdr2/cdr3 (analysis) set
CDR_CLINICAL_READ=false and must not have it at all.
"""
from sqlalchemy.pool import StaticPool

from app import create_app

BASE = {"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"check_same_thread": False},
                                      "poolclass": StaticPool}}


def _rules(app):
    return {r.rule for r in app.url_map.iter_rules()}


def test_the_default_registers_the_clinical_surface():
    """cdr1 sets nothing and keeps its surface."""
    rules = _rules(create_app(dict(BASE)))
    assert any(r.startswith("/api/v1/clinical/") for r in rules)


def test_an_ANALYSIS_cdr_has_no_clinical_surface():
    app = create_app(dict(BASE, CDR_CLINICAL_READ=False))
    rules = _rules(app)
    assert not any(r.startswith("/api/v1/clinical/") for r in rules), \
        sorted(r for r in rules if "clinical" in r)
    assert app.test_client().get("/api/v1/clinical/patients").status_code == 404


def test_the_rest_of_the_surface_is_the_same_either_way():
    """Only the clinical routes differ between the two roles."""
    on = _rules(create_app(dict(BASE)))
    off = _rules(create_app(dict(BASE, CDR_CLINICAL_READ=False)))
    # #1044: the review queue is care delivery too and follows the same flag.
    assert on - off == {r for r in on if r.startswith("/api/v1/clinical/")
                        or r.startswith("/api/v1/reviews")}
    assert {r for r in on if r.startswith("/api/v1/reviews")}
    assert off <= on


def test_the_env_reading(monkeypatch):
    for raw, want in (("false", False), ("0", False), ("off", False), ("no", False),
                      ("true", True), ("", True), ("  FALSE ", False)):
        monkeypatch.setenv("CDR_CLINICAL_READ", raw)
        assert create_app(dict(BASE)).config["CDR_CLINICAL_READ"] is want, raw
    monkeypatch.delenv("CDR_CLINICAL_READ")
    assert create_app(dict(BASE)).config["CDR_CLINICAL_READ"] is True
