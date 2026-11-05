"""#869 — one canonical-URI rule, and ICD-11 reaches it.

THE TICKET ASKED FOR A THIRD FORM. The measurement says there must not be
one. `https://termbank.pdhc.se/CodeSystem/icd11-mms/<code>` is Path A
unchanged, it is already in this suite's fixtures, termbank owns the
vocabulary (icd11.pdhc's own Rule 18), and plan already knows `icd11-mms`
at SYSTEM granularity (#874), so an 85,000-code classification needs no row
per code.

WHAT WAS ACTUALLY BROKEN was one level deeper than the ticket looked. The
ticket named the two READER predicates (stats.py, clinical_read.py) — those
implement a different rule, plan-concept Path B, and are untouched; a test
below pins that. The real split was the BUILDER, in two copies:

    canonicalisation.py::_coding_to_canonical_uri   (ingest)
    resource_writer.py::_coding_canonical_uri       (storage, with the
                                                     prefix as a LITERAL)

so a system either had learned about would be unknown to the other. And
`http://id.who.int/icd/release/11/mms` was known to NEITHER — only to
`auth.py`, which has held that exact alias since #874 for the whole-system
assertion gate. A raw WHO-coded Condition therefore fell to the xlate
branch, and xlate is deployed NOWHERE (#833), so it was stored with
`code_canonical` NULL: unsearchable by code, weakly deduplicated.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import MagicMock

import pytest

from app.services import canonicalisation as canon_mod
from app.services import resource_writer as rw_mod
from app.services.canonicalisation import Canonicaliser
from app.services.plan_client import (
    ICD11_CANONICAL_LIB,
    ICD11_WHO_SYSTEM,
    SYSTEM_ALIASES,
    TERMBANK_PREFIX,
    canonical_uri_for,
    is_canonicalisable_system,
    parse_canonical_uri,
)

CLUSTER = "BD13&XT8W&XS3A/CA60.3"
SIMPLE = "BA00.Z"
EXPECTED_CLUSTER_URI = f"{TERMBANK_PREFIX}CodeSystem/{ICD11_CANONICAL_LIB}/{CLUSTER}"


class _XlateMustNotBeCalled:
    def translate(self, *args, **kwargs):
        raise AssertionError(
            "xlate.translate called for a coding cdr can canonicalise by "
            "rename alone — #869 made that a short-circuit")


class _XlateRecorder:
    def __init__(self):
        self.called = False

    def translate(self, *args, **kwargs):
        self.called = True
        return None


def _canon(xlate, *, validate=None):
    plan = MagicMock()
    plan.resolve_concept.return_value = None
    plan.validate_canonical.return_value = validate
    return Canonicaliser(xlate=xlate, plan=plan)


def _condition(codings):
    return {"resourceType": "Condition", "code": {"coding": codings}}


# ---------------------------------------------------------------------------


class TestThereIsOneBuilder:
    """Asserted on the PARSE TREE, not on text: the docstrings written for
    #869 quote the termbank prefix to explain it, so a substring search
    would match the prose explaining the fix. That instrument error happened
    four times on 2026-11-02 (#885/#904/#912) and once inside
    tools/audit_inline_credentials.py itself (#924)."""

    @staticmethod
    def _code_only(fn) -> str:
        """Source with comments gone (ast.unparse drops them) and docstrings
        blanked, so only executable code is left."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Module))
                    and ast.get_docstring(node)):
                node.body[0] = ast.Pass()
        return ast.unparse(tree)

    @pytest.mark.parametrize("fn", [
        canon_mod.Canonicaliser._coding_to_canonical_uri,
        rw_mod._coding_canonical_uri,
    ])
    def test_neither_copy_builds_the_uri_itself(self, fn):
        src = self._code_only(fn)
        assert "termbank.pdhc.se" not in src, (
            f"{fn.__qualname__} still has its own copy of the rule")
        assert "canonical_uri_for" in src, (
            f"{fn.__qualname__} does not delegate to the one builder")

    def test_CONTROL_the_builder_itself_DOES_contain_the_rule(self):
        """Without this, the two assertions above would also pass on a
        codebase where the rule had been deleted entirely."""
        src = self._code_only(canonical_uri_for)
        assert "TERMBANK_PREFIX" in src


class TestTheWhoSystemNowCanonicalises:
    def test_the_builder_renames_it_to_the_termbank_lib(self):
        assert canonical_uri_for(ICD11_WHO_SYSTEM, CLUSTER) == EXPECTED_CLUSTER_URI

    def test_a_termbank_coding_is_byte_identical_to_before(self):
        """The pre-existing Path A must not have moved."""
        sysuri = f"{TERMBANK_PREFIX}CodeSystem/loinc"
        assert canonical_uri_for(sysuri, "41653-7") == f"{sysuri}/41653-7"

    def test_the_canonicaliser_short_circuits_instead_of_calling_xlate(self):
        c = _canon(_XlateMustNotBeCalled(),
                   validate={"result": True, "whole_system": True})
        res = c.canonicalise(_condition([
            {"system": ICD11_WHO_SYSTEM, "version": "2024-01", "code": CLUSTER},
        ]))
        assert res.primary_canonical_uri == EXPECTED_CLUSTER_URI

    def test_CONTROL_a_foreign_system_still_reaches_xlate(self):
        """The fix must not swallow the xlate path. SNOMED needs a real
        translation, not a rename, so it must NOT be short-circuited."""
        rec = _XlateRecorder()
        c = _canon(rec, validate=None)
        c.canonicalise(_condition([
            {"system": "http://snomed.info/sct", "code": "73211009"},
        ]))
        assert rec.called, "a genuinely foreign coding bypassed xlate"

    def test_CONTROL_the_release_is_NOT_in_the_canonical(self):
        """icd11 puts the release in `coding.version` (its Rule 28), not in
        the system, so the identity key is release-free and two statements
        of the same entity collide for dedup and search."""
        a = canonical_uri_for(ICD11_WHO_SYSTEM, SIMPLE)
        assert "2024" not in a and "release" not in a.split("CodeSystem/")[1]


class TestPostcoordinatedClustersRoundTrip:
    """The ticket asked what is canonical for `BD13&XS3A/BA41`. The whole
    expression is, and it needs no escaping: `parse_canonical_uri`'s
    refnumber group is `(.+)` and not `([^/]+)`."""

    @pytest.mark.parametrize("code", [
        SIMPLE, "BA00.Z&XS31", "8A84.1&XS5B", CLUSTER,
    ])
    def test_build_then_parse_returns_the_code_unchanged(self, code):
        uri = canonical_uri_for(ICD11_WHO_SYSTEM, code)
        parsed = parse_canonical_uri(uri)
        assert parsed == (ICD11_CANONICAL_LIB, code)

    @pytest.mark.parametrize("uri", [
        "http://id.who.int/icd/release/11/mms/BD13",
        "urn:pdhc:concept/11111111-1111-1111-1111-111111111111",
        "https://termbank.pdhc.se/CodeSystem/loinc",
    ])
    def test_CONTROL_these_are_not_canonical_uris(self, uri):
        """Without these the test above would pass on a regex matching
        anything."""
        assert parse_canonical_uri(uri) is None

    def test_the_code_is_not_escaped(self):
        """Stated as a test because escaping would change every existing
        Path-A value in every CDR, for no gain."""
        assert "%26" not in EXPECTED_CLUSTER_URI
        assert "&" in EXPECTED_CLUSTER_URI


class TestOrderIsSignificant:
    """icd11 sends the asserted cluster and, when WHO defines one, a
    pre-coordinated `simplified_to` equivalent — "additional information,
    not a replacement". The FIRST canonicalisable coding wins, so the
    cluster must come first or cdr stores the simplification as the row's
    identity."""

    @staticmethod
    def _two(first_code, second_code):
        c = _canon(_XlateMustNotBeCalled(),
                   validate={"result": True, "whole_system": True})
        return c.canonicalise(_condition([
            {"system": ICD11_WHO_SYSTEM, "code": first_code},
            {"system": ICD11_WHO_SYSTEM, "code": second_code},
        ])).primary_canonical_uri

    def test_the_first_coding_wins(self):
        assert self._two(CLUSTER, SIMPLE) == EXPECTED_CLUSTER_URI

    def test_CONTROL_reversing_them_picks_the_other(self):
        """Proves the test above measures ORDER and not a preference for
        clusters, which no code implements."""
        assert self._two(SIMPLE, CLUSTER).endswith(f"/{SIMPLE}")


class TestTheGateAndTheCanonicaliserCannotDisagree:
    def test_auth_reuses_the_same_alias_object(self):
        from app import auth
        assert auth.WHOLE_SYSTEM_SYSTEM_ALIASES is SYSTEM_ALIASES
        assert auth.ICD11_WHO_SYSTEM == ICD11_WHO_SYSTEM

    def test_every_alias_target_is_buildable(self):
        for system, lib in SYSTEM_ALIASES.items():
            uri = canonical_uri_for(system, "X")
            assert uri == f"{TERMBANK_PREFIX}CodeSystem/{lib}/X"
            assert is_canonicalisable_system(system)


class TestTheReadersAreUntouched:
    """The ticket's stated worry — that a third form would make stats and
    clinical_read misbehave — does not apply: they implement plan-concept
    Path B, a different rule. An ICD-11 canonical is correctly 'not a plan
    concept' to both. Pinned so a future change to either is deliberate."""

    def test_neither_reader_claims_an_icd11_canonical(self):
        from app.api.stats import _canon_guid_fallback
        assert _canon_guid_fallback(EXPECTED_CLUSTER_URI) is None
        try:
            from app.api.clinical_read import _concept_guid_from_canonical
        except ImportError:
            pytest.skip("this CDR carries no clinical read surface (cdr2/cdr3)")
        assert _concept_guid_from_canonical(EXPECTED_CLUSTER_URI) is None

    def test_CONTROL_a_path_B_canonical_still_yields_its_guid(self):
        from app.api.stats import _canon_guid_fallback
        guid = "11111111-1111-1111-1111-111111111111"
        assert _canon_guid_fallback(f"urn:pdhc:concept/{guid}") == guid
