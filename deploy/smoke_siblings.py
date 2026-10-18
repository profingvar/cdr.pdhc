#!/usr/bin/env python3
"""cdr.pdhc — does every call to a SIBLING service actually work? (#708)

Run it INSIDE the container, which is the only place the real credentials and
the real network names exist:

    docker exec cdr_pdhc_app   python deploy/smoke_siblings.py
    docker exec cdr_pdhc_2_app python deploy/smoke_siblings.py --json
    docker exec cdr_pdhc_3_app python deploy/smoke_siblings.py

READ-ONLY. It creates nothing, writes nothing and mints no token, so it is safe
against production at any time.

## One smoke, three different deployments

cdr1, cdr2 and cdr3 run the same repo name and NOT the same code. cdr2 and cdr3
have no ``app/api/clinical_read.py`` and a 28-line ``app/api/stats.py`` against
cdr1's 295. So this file touches only what all three provably have —
``app/auth.py``, ``app/services/analysis_consent.py``,
``app/services/plan_client.py`` — and probes EDGES and CONFIG rather than
importing a module that exists in one deployment. A smoke that imports a
cdr1-only module reports a cdr2 defect that is really its own.

## Why these checks and not others

``analysis_consent`` reaches ips as ``POST /api/v1/patients/analysis-filter``.
A POST is a write shape even when the operation is a read, so this smoke never
sends one: the PATH gets a liveness probe (a GET must be refused, not 404), and
the CREDENTIAL is tested separately on an ips GET route. Those are two different
questions and a single check could not answer both without writing.

## This file absorbed the original #708 smoke (#836)

There were two of these for ten days: this one, and
``cdr_app/deploy/smoke_siblings.py`` from #708/#730, which was deployed in no
container. I wrote this one without finding that one — a root-level ``grep``
skips service repo contents, because grep here is ugrep and the root
``.gitignore`` is a whitelist. An empty result is not absence.

The older file was NOT redundant, which is why it was read before being
deleted rather than after. Four of its checks had no equivalent here and were
carried over: the real-``code_canonical`` plan resolution, the Rule 18 canonical
shape check, the cambio config check, and the positive consent verdict (#730).
Its own ``xlate: absent as expected`` check was superseded by the one below.

## Why there is an xlate check but no xlate service

xlate.pdhc is declared in CLAUDE.md §3 and is deployed nowhere — no release
directory, nothing listening on 9016 or 9017. Its own ``.env.example`` sets
``APP_PORT=9017`` and ``DB_PORT=9016``, so §3's "9016 declared" records the
database port as the service port; the cdr client's ``:9017`` was right.

The check therefore asserts a REFUSAL, not a connection: a foreign coding must
come back 422 ``xlate_not_configured`` (terminal) rather than 503 ``transient``
(retry me). cdr1 left ``XLATE_BASE_URL`` unset and cdr2/cdr3 pointed it at
``host.docker.internal:9017``; both were unreachable and both 503'd, so this
was never a URL-form problem (#833).

## What the sso check CANNOT tell you — measured 2026-10-16

``/api/auth/me/service`` is ``@require_auth`` decorated, and the decorator runs
**before** the function body. The client-pair comparison that returns 403
``invalid_service_credentials`` is inside the body. So a junk bearer is refused
401 by the decorator and the pair is never examined:

    real pair  + junk bearer -> 401
    real id    + WRONG secret -> 401
    WRONG id   + WRONG secret -> 401
    no pair at all            -> 401

A 403 is reachable only while holding a VALID user token, which this smoke will
not mint. So "401 proves our pair was accepted" is false, and three smokes plus
one operator script asserted exactly that. This check therefore claims only
that the route is live and the bearer path is reachable, and the 403 branch is
kept because a 403 would be real information if it ever appeared.

**To prove a pair is actually registered**, compare digests instead: sha256 of
this service's ``SSO_CLIENT_SECRET`` against sso's ``SSO_CLIENT_SECRET_<NAME>``.
That needs no bearer and is the check that genuinely verified cdr_6 in #828.
It spans two services' environments, so it is an operator task, not this file's.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time

#: Path templates this service composes, with the module that owns each. The
#: path-drift check asserts the literal still appears there, so a rename in the
#: client is caught before the next deploy rather than at runtime (#804).
CLIENT_PATHS = {
    "/api/v1/patients/analysis-filter": "app/services/analysis_consent.py",
    "/api/v1/ValueSet/$validate-code": "app/services/plan_client.py",
    "/api/auth/me/service": "app/auth.py",
}

#: Probed but composed inline here, with no client module to drift from:
#:   /api/health, /healthz — bare liveness pings.
INLINE_PATHS = ("/api/health", "/healthz")

#: A guid that is valid in shape and belongs to nobody, so every probe that
#: carries one is a miss by construction.
NOBODY = "00000000-0000-4000-8000-000000000000"

#: plan_client falls back to this when PLAN_BASE_URL is unset. Inside a
#: container 127.0.0.1 is the container itself, so the fallback reaches nothing
#: and a missing variable would look like plan being down. Assert against it.
PLAN_FALLBACK_TRAP = "127.0.0.1:9030"

# A deliberately MALFORMED guid, kept from the #708/#730 smoke this file
# absorbed (#836). ips's PatientIndex.guid is a UUID column, so this value once
# made /analysis-filter raise at the driver and answer 500 — which cdr turned
# into IpsUnreachable and reported as an ips outage (#730). ips now treats a
# malformed guid as unknown and excludes it by name, so keeping this value
# keeps that path covered. NOBODY above is well-formed and covers the other
# half: a valid guid that is simply nobody.
SMOKE_PATIENT = "smoke-not-a-real-patient"

CONCEPT_PREFIX = "urn:pdhc:concept/"

RESET, RED, GRN, YEL, BOLD = "\033[0m", "\033[31m", "\033[32m", "\033[33m", "\033[1m"

results: list[dict] = []


def check(name: str):
    def wrap(fn):
        started = time.time()
        try:
            ok, note = fn()
        except Exception as e:                      # noqa: BLE001
            ok, note = False, f"{type(e).__name__}: {e}"
        results.append({"check": name, "ok": bool(ok), "note": note,
                        "ms": int((time.time() - started) * 1000)})
        return ok
    return wrap


CAMBIO_REQUIRED = ("CAMBIO_BASE_URL", "CAMBIO_TOKEN_URL",
                   "CAMBIO_CLIENT_ID", "CAMBIO_CLIENT_SECRET")


def cambio_verdict(cfg) -> tuple[bool, str]:
    """Is cambio delivery configuration coherent? Pure, so it is testable.

    Deliberately a module-level function taking a mapping rather than a
    closure over ``current_app.config``. The only way to exercise the
    enabled-but-incomplete branch otherwise is to set
    ``CAMBIO_DELIVERY_ENABLED=true`` and call ``create_app()``, which starts
    the real APScheduler delivery worker (``app/__init__.py``) against live
    Cambio sandbox credentials. That is not a test, it is an outbound
    delivery with a 60-second fuse. Pass a dict instead.
    """
    enabled = bool(cfg.get("CAMBIO_DELIVERY_ENABLED"))
    missing = [k for k in CAMBIO_REQUIRED if not cfg.get(k)]
    if not enabled:
        return True, ("delivery disabled; "
                      + ("config present" if not missing
                         else f"{len(missing)} value(s) unset — fine "
                              "while disabled"))
    return not missing, ("enabled, config complete" if not missing
                         else f"ENABLED but missing {', '.join(missing)}")


def concept_guid(code_canonical: str) -> str | None:
    """GUID out of a stored ``code_canonical``, or None if it is not one.

    cdr stores Path-B: ``urn:pdhc:concept/<guid>`` — NOT a termbank URI. This
    matters because ``plan_client.parse_canonical_uri`` parses the *termbank*
    form, so pointing it at a stored canonical returns None for EVERY row. The
    first version of the #708 smoke did exactly that and reported 23/23 "bad",
    which is how an obviously wrong check announces itself: the data was fine
    and the test was using the wrong parser. The guid-based resolvers
    (``resolve_concept``, ``lookup_display``) are the right door for this shape.
    """
    import uuid
    if not code_canonical or not code_canonical.startswith(CONCEPT_PREFIX):
        return None
    tail = code_canonical[len(CONCEPT_PREFIX):]
    try:
        uuid.UUID(tail)
    except (ValueError, AttributeError, TypeError):
        return None
    return tail


def _repo_root() -> pathlib.Path:
    """Where ``app/`` lives.

    The image flattens the package to /app/app/..., while a checkout is
    <repo>/cdr_app/app/... — one hardcoded relative path resolves in only one
    of them, and the guard is worth most BEFORE a deploy (#804).
    """
    here = pathlib.Path(__file__).resolve().parent.parent
    for cand in (here, here / "cdr_app", here.parent / "cdr_app"):
        if (cand / "app" / "services").is_dir():
            return cand
    return here


def _path_appears_in_client(template: str, src: str) -> bool:
    """The literal must sit against a URL boundary, not merely occur.

    A bare substring search passes on a prose mention in a docstring, which is
    how #804's truncated-prefix bug survived a guard that was looking for it.
    """
    frags = [f for f in template.split("%s") if f]
    if not frags:
        return False
    if not re.search(r"""[}"'/]""" + re.escape(frags[0].lstrip("/")), src):
        return False
    return all(f in src for f in frags[1:])


def _probe(url, headers=None, params=None, timeout=12):
    """(status, short body). A transport failure is status 0, so a service that
    is DOWN and a path that MOVED stay distinguishable."""
    import requests
    try:
        r = requests.get(url, headers=headers or {}, params=params or {},
                         timeout=timeout)
        return r.status_code, (r.text or "")[:140]
    except requests.RequestException as e:
        return 0, f"{type(e).__name__}: {e}"


def _path_is_live(code: int) -> tuple[bool, str]:
    """Does the ROUTE exist? Deliberately NOT an auth verdict.

    401 and 403 both mean the route is there. Where the credential itself is
    the question, the check asks for the exact code instead — see the sso
    check, which fails on 403.
    """
    if code == 0:
        return False, "unreachable"
    if code == 404:
        return False, "404 — the path MOVED"
    if code in (400, 401, 403, 405, 415, 422):
        return True, f"{code} (refused, so the path is live)"
    if 200 <= code < 300:
        return True, f"{code} (answered)"
    return False, f"{code}"


def _scheme_note(url: str) -> str:
    if "host.docker.internal" in url or "127.0.0.1" in url or "localhost" in url:
        return f"{YEL}loopback — #830 wants the public subdomain{RESET}"
    if url.startswith("https://"):
        return "public subdomain"
    return f"{YEL}neither loopback nor https{RESET}"


def main(as_json: bool = False) -> int:
    # The image flattens the package to /app/app, and python puts the SCRIPT's
    # directory on sys.path, not the app root — so an absolute invocation
    # (`python /app/deploy/smoke_siblings.py`) cannot import `app` unless we
    # put the root there ourselves. Both invocation styles then work.
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from app import create_app
    app = create_app()

    with app.test_request_context():
        from flask import current_app
        cfg = current_app.config

        sso = (cfg.get("SSO_BASE_URL") or "").rstrip("/")
        ips = (cfg.get("IPS_BASE_URL") or os.environ.get("IPS_BASE_URL", "")).rstrip("/")
        plan = (cfg.get("PLAN_BASE_URL") or os.environ.get("PLAN_BASE_URL", "")).rstrip("/")
        api_key = cfg.get("IPS_API_KEY") or os.environ.get("IPS_API_KEY", "")
        cid = cfg.get("SSO_CLIENT_ID") or os.environ.get("SSO_CLIENT_ID", "")
        csec = cfg.get("SSO_CLIENT_SECRET") or os.environ.get("SSO_CLIENT_SECRET", "")
        # cdr1 does not set CDR_INSTANCE; cdr2/cdr3 do. Not a defect, but the
        # banner should not imply the value was lost.
        instance = os.environ.get("CDR_INSTANCE") or "unset (cdr1)"

        # ---------------------------------------------------------------- paths
        @check("paths: every probed path still appears in its client module")
        def _():
            missing, checked = [], 0
            for template, rel in CLIENT_PATHS.items():
                f = root / rel
                if not f.is_file():
                    missing.append(f"{rel} ABSENT in this deployment")
                    continue
                checked += 1
                if not _path_appears_in_client(template, f.read_text("utf-8")):
                    missing.append(f"{template} not in {rel}")
            if missing:
                return False, "; ".join(missing)
            return True, (f"all {checked} anchored to a URL boundary; "
                          f"{len(INLINE_PATHS)} inline liveness paths unguarded "
                          f"by design")

        # --------------------------------------------------------------- config
        @check("config: SSO_BASE_URL is set")
        def _():
            return bool(sso), f"{sso or '(unset)'}  {_scheme_note(sso) if sso else ''}"

        @check("config: the SSO client pair is present")
        def _():
            if not cid or not csec:
                return False, (f"client id {'present' if cid else 'MISSING'}, "
                               f"secret {'present' if csec else 'MISSING'} — "
                               f"without both, every bearer read 503s")
            return True, f"id {len(cid)} chars, secret {len(csec)} chars"

        @check("config: IPS_BASE_URL is set")
        def _():
            return bool(ips), f"{ips or '(unset)'}  {_scheme_note(ips) if ips else ''}"

        @check("config: IPS_API_KEY is set — the ONLY credential ips reads")
        def _():
            # ips's require_auth reads Authorization only; X-API-Key is ignored
            # outright, so a key in the wrong header is indistinguishable from
            # no key at all.
            return bool(api_key), ("present" if api_key else
                                   "MISSING — analysis reads fail closed")

        @check("config: PLAN_BASE_URL is set and is not the silent fallback")
        def _():
            if not plan:
                return False, (f"unset, so plan_client falls back to "
                               f"{PLAN_FALLBACK_TRAP}, which inside a container "
                               f"is the container itself — plan would look down")
            if PLAN_FALLBACK_TRAP in plan:
                return False, (f"{plan} is the fallback trap: inside a container "
                               f"127.0.0.1 is this container, not plan")
            return True, f"{plan}  {_scheme_note(plan)}"

        # ------------------------------------------------------------------ sso
        @check("sso: /api/health answers 200")
        def _():
            if not sso:
                return False, "SSO_BASE_URL not set"
            code, body = _probe(f"{sso}/api/health")
            return code == 200, f"{code} {body[:60]}"

        @check("sso: the bearer path is live (401; NOT proof the pair is good)")
        def _():
            # @require_auth runs before the handler, so a junk bearer is
            # refused 401 and the client pair is never compared. Verified
            # 2026-10-16: a wrong secret and no pair at all both also 401.
            # Do not read a 401 here as "our pair was accepted".
            if not (sso and cid and csec):
                return False, "SSO_BASE_URL or the client pair is not set"
            code, body = _probe(
                f"{sso}/api/auth/me/service",
                headers={"Authorization": "Bearer not-a-real-token",
                         "X-SSO-Client-Id": cid,
                         "X-SSO-Client-Secret": csec})
            if code == 401:
                return True, ("401 — route live, junk bearer refused. Pair "
                              "validity is NOT tested here; compare digests "
                              "against sso to prove registration")
            if code == 403:
                return False, (f"403 — sso reached the pair check and REJECTED "
                               f"our pair. Unexpected with a junk bearer, and "
                               f"real information. {body[:60]}")
            return False, f"{code} {body[:70]} — expected 401"

        # ------------------------------------------------------------------ ips
        @check("ips: X-API-Key ALONE is refused (the #783 mistake)")
        def _():
            if not (ips and api_key):
                return False, "IPS_BASE_URL or IPS_API_KEY not set"
            code, body = _probe(f"{ips}/api/v1/patients/{NOBODY}/clinics",
                                headers={"X-API-Key": api_key})
            # ips reads Authorization only. A 200 here would mean it had started
            # honouring X-API-Key, and our clients would then be sending a
            # credential in a header that is silently ignored half the time.
            return code == 401, f"{code} {body[:70]}"

        @check("ips: Authorization: ApiKey IS accepted (credential, not path)")
        def _():
            if not (ips and api_key):
                return False, "IPS_BASE_URL or IPS_API_KEY not set"
            code, body = _probe(f"{ips}/api/v1/patients/{NOBODY}/clinics",
                                headers={"Authorization": f"ApiKey {api_key}"})
            if code == 404:
                return True, "404 — key accepted, and this guid is nobody"
            if code == 401:
                return False, (f"401 — ips REJECTED our key. Analysis reads "
                               f"fail closed until it is fixed. {body[:60]}")
            return False, f"{code} {body[:70]} — expected 404"

        @check("ips: the analysis-filter path is LIVE (probed, never POSTed)")
        def _():
            if not ips:
                return False, "IPS_BASE_URL not set"
            # A GET on a POST-only route must be refused (405), not 404. This
            # smoke does not send the POST: a write shape stays unsent even
            # when the operation behind it is a read.
            code, body = _path_is_live(
                _probe(f"{ips}/api/v1/patients/analysis-filter")[0])
            return code, body

        @check("ips: the consent client fails CLOSED, by name")
        def _():
            # Point it at a host that cannot answer and require the named
            # exception. A filter that fails OPEN returns everything, which is
            # the one outcome that must be impossible (cf. ips e7e81a1).
            from app.services import analysis_consent as ac
            # _analysis_filter is the network boundary itself. The public
            # wrappers (consent_allowed_guids, check_patient_allowed) go
            # through _resolve_purpose first, which returns None without an
            # operator blob and would skip the call entirely — so this check
            # would pass while proving nothing. Probe the private function on
            # purpose, and assert the exception BY NAME.
            fn = getattr(ac, "_analysis_filter", None)
            if fn is None:
                return False, ("_analysis_filter is gone — analysis_consent was "
                               "restructured and this check is now blind")
            if not hasattr(ac, "IpsUnreachable"):
                return False, "IpsUnreachable is gone; fail-closed is unprovable"
            saved = current_app.config.get("IPS_BASE_URL")
            current_app.config["IPS_BASE_URL"] = "http://127.0.0.1:1"
            try:
                try:
                    fn([NOBODY], "research", [])
                except ac.IpsUnreachable as e:
                    return True, f"IpsUnreachable: {str(e)[:70]}"
                except Exception as e:                  # noqa: BLE001
                    return False, (f"raised {type(e).__name__}, not "
                                   f"IpsUnreachable: {str(e)[:60]}")
                return False, ("RETURNED A RESULT with ips unreachable — the "
                               "filter fails OPEN, which returns every patient")
            finally:
                current_app.config["IPS_BASE_URL"] = saved

        @check("ips: a consent verdict IS obtainable for a service-shaped read")
        def _():
            # The complement of the fail-closed check above. That one proves the
            # gate cannot fail OPEN; this one proves it does not fail CLOSED in
            # normal operation, which is the failure that looks like safety and
            # silently returns nothing. #730 was exactly that.
            #
            # No flask session here, which is the shape of a service-key caller
            # (gateway, analyse, sim). _analysis_filter only attaches
            # Authorization: Bearer when a session token exists, and ips reads
            # ONLY that header. So this answers the question the unit tests mock
            # away: with no operator bearer, does cdr get a verdict at all?
            from app.services.analysis_consent import (
                _analysis_filter, IpsUnreachable,
            )
            if not ips:
                return False, "IPS_BASE_URL not set"
            try:
                out = _analysis_filter([SMOKE_PATIENT], "research", [])
            except IpsUnreachable as e:
                return False, (f"{str(e)[:70]} — the gate fails CLOSED, which is "
                               "safe but means a service-key caller reads "
                               "NOTHING. Since #730 this service holds its own "
                               "IPS_API_KEY; check that before suspecting ips")
            if not isinstance(out, dict) or "allowed" not in out:
                return False, f"unexpected analysis-filter shape: {out!r}"
            return True, (f"allowed={len(out['allowed'])} "
                          f"excluded={len(out['excluded'])} for a malformed guid")

        # ---------------------------------------------------------------- xlate
        @check("xlate: unconfigured is TERMINAL, not transient (#833)")
        def _():
            # xlate.pdhc is not deployed anywhere on the platform: no
            # release directory, nothing on 9016 or 9017. Until #833 the
            # client fell back to http://127.0.0.1:9017 and a foreign
            # coding came back 503 "transient" — so a producer would
            # retry forever against a service that does not exist.
            #
            # Assert the two exceptions BY NAME and assert the status the
            # canonicaliser derives. No POST: the client refuses before
            # the socket when unconfigured, so this costs no network.
            from app.services import xlate_client as xc
            from app.services.canonicalisation import Canonicaliser

            for name in ("XlateNotConfigured", "XlateUnavailable"):
                if not hasattr(xc, name):
                    return False, (f"{name} is gone — the permanent and "
                                   "transient states have been re-merged, "
                                   "and an undeployed xlate 503s again")
            if hasattr(xc, "DEFAULT_BASE_URL"):
                return False, ("DEFAULT_BASE_URL is back: an unset "
                               "XLATE_BASE_URL again looks configured")

            unconfigured = xc.XlateClient(base_url=None)
            if unconfigured.configured:
                # Something in the environment IS pointing xlate somewhere.
                return False, (f"XLATE_BASE_URL={unconfigured.base_url} — set, "
                               "but no xlate service is deployed on this "
                               "platform, so writes will 503 and be retried")

            class _PlanMustNotBeAsked:
                def __getattr__(self, _n):
                    def _boom(*a, **kw):
                        raise AssertionError("plan consulted before xlate")
                    return _boom

            result = Canonicaliser(
                xlate=unconfigured, plan=_PlanMustNotBeAsked()
            ).canonicalise({
                "resourceType": "Observation", "status": "final",
                "subject": {"reference": f"Patient/{NOBODY}"},
                "code": {"coding": [{"system": "http://loinc.org",
                                     "code": "4548-4"}]},
                "effectiveDateTime": "2026-04-01T10:00:00Z",
            })
            if result.status == "transient":
                return False, ("a foreign coding still reports 'transient' "
                               "-> 503, which invites an endless retry")
            if result.status != "xlate_not_configured":
                return False, f"unexpected status {result.status!r}"
            return True, ("unset -> xlate_not_configured -> 422 terminal "
                          "(foreign codings refused, not retried)")

        # ----------------------------------------------------------------- plan
        @check("plan: the terminology edge is live")
        def _():
            if not plan:
                return False, "PLAN_BASE_URL not set"
            code, body = _probe(f"{plan}/api/v1/ValueSet/$validate-code",
                                params={"system": "urn:pdhc:nonexistent",
                                        "code": "nope"})
            ok, note = _path_is_live(code)
            return ok, f"{note} {body[:50]}"

        @check("plan: a REAL code_canonical from this CDR resolves")
        def _():
            # The check above proves the path answers. This one proves the
            # contract holds for data this CDR actually stored — a canonical
            # taken off a live row, not one invented here. Absorbed from the
            # #708 smoke (#836); the probe-only version could pass while every
            # stored row was unresolvable.
            from app.models.resources import live_model
            from app.services.plan_client import PlanClient, PlanUnreachable
            if not plan:
                return False, "PLAN_BASE_URL not set"
            Observation = live_model("Observation")
            if Observation is None:
                return False, "no Observation model registered"
            guid = uri = None
            for (cand,) in (Observation.query
                            .with_entities(Observation.code_canonical)
                            .filter(Observation.code_canonical.isnot(None))
                            .distinct().limit(200)):
                g = concept_guid(cand)
                if g:
                    guid, uri = g, cand
                    break
            if guid is None:
                return False, ("no stored code_canonical carries a valid concept "
                               "guid — inconclusive, reported as a failure so it "
                               "is not mistaken for proof")
            try:
                resolved = PlanClient(base_url=plan).resolve_concept(guid)
            except PlanUnreachable as e:
                return False, f"plan.pdhc unreachable: {str(e)[:60]}"
            if not resolved:
                return False, (f"plan has no canonical binding for a concept this "
                               f"CDR stores on live rows: {uri}")
            return True, (f"{guid[:8]}… → "
                          f"{resolved.get('canonical_uri') or resolved.get('display')}")

        # ----------------------------------------------------------------- data
        @check("data: every stored code_canonical carries a GUID (Rule 18)")
        def _():
            # Not a sibling call, but it is about the contract cdr sends to
            # plan: a canonical with a non-GUID tail can never be resolved, so
            # the row is permanently un-canonicalisable. Rule 18 is GUID-only.
            from app.models.resources import live_model
            Observation = live_model("Observation")
            if Observation is None:
                return False, "no Observation model registered"
            bad, total = [], 0
            for (cand,) in (Observation.query
                            .with_entities(Observation.code_canonical)
                            .filter(Observation.code_canonical.isnot(None))
                            .distinct()):
                total += 1
                if concept_guid(cand) is None:
                    bad.append(cand)
            if bad:
                shown = ", ".join(sorted(bad)[:4])
                more = f" (+{len(bad) - 4} more)" if len(bad) > 4 else ""
                return False, (f"{len(bad)}/{total} distinct canonicals have a "
                               f"non-GUID tail and can never resolve: "
                               f"{shown}{more}")
            return True, f"all {total} distinct canonicals carry a GUID"

        # --------------------------------------------------------------- cambio
        @check("cambio: delivery config is coherent (not called)")
        def _():
            # Configuration only, deliberately never called: cambio is an
            # external sandbox and a smoke must not generate traffic there.
            # Logic lives in cambio_verdict() so the enabled-but-incomplete
            # branch can be tested without starting the delivery worker.
            return cambio_verdict(cfg)

    # ------------------------------------------------------------------- report
    if as_json:
        print(json.dumps({"service": f"cdr.pdhc ({instance})",
                          "results": results}, indent=2))
    else:
        print(f"\n{BOLD}cdr.pdhc — sibling smoke{RESET}  "
              f"[CDR_INSTANCE={instance}]\n")
        for r in results:
            tag = f"{GRN}OK{RESET}" if r["ok"] else f"{RED}FAIL{RESET}"
            print(f"  {tag:<12} {r['check']:<58}")
            print(f"                  {r['note']}  ({r['ms']}ms)")
        print("\n  Created nothing. No state changed on any service.")

    failed = [r for r in results if not r["ok"]]
    if failed:
        print(f"\n{RED}{BOLD}{len(failed)} of {len(results)} checks FAILED.{RESET}\n")
        return 1
    print(f"\n{GRN}{BOLD}All {len(results)} checks passed.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(as_json="--json" in sys.argv))
