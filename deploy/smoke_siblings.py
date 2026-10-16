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
