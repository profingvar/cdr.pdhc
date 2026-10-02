#!/usr/bin/env python3
"""cdr.pdhc (cdr1) — does every call to a SIBLING service actually work? (#708)

Run it INSIDE the container, because that is the only place the real service
keys and the real network names exist. Pipe it in over stdin — that needs no
copy inside the container and no rebuild, which matters for cdr specifically
because its deployed tree is known to sit behind local git (memory: CDR prod
behind local git), and rebuilding a patient-data service to add a diagnostic is
not a trade worth making:

    ssh miserver 'docker exec -i cdr_pdhc_app python -' < cdr_app/deploy/smoke_siblings.py

Once a real deploy carries the file into the image, the direct form works too:

    docker exec cdr_pdhc_app python deploy/smoke_siblings.py
    docker exec cdr_pdhc_app python deploy/smoke_siblings.py --json

READ-ONLY. It creates nothing and writes nothing, so it is safe against
production at any time, including immediately after a deploy.

## Why it drives the app's own clients

#708's whole point is that static analysis inside one repo cannot find a
contract mismatch across a service boundary, and that hand-written ``curl``
calls cannot either — they test the author's idea of the contract instead of
the code's. So this imports ``PlanClient`` and ``analysis_consent`` and calls
them. If a header, a key name or a response shape is wrong, it is wrong here
too.

## What it deliberately does NOT do

It does not call **Cambio**. That is an external third party's sandbox, not a
sibling, and a smoke that pokes someone else's system on every deploy is a
smoke that gets switched off. Cambio is checked for configuration only.

``XLATE_BASE_URL`` being unset is reported as expected, not failed —
xlate.pdhc is declared in compose and not running in production (CLAUDE.md §3).
A smoke that cries wolf about a service that is deliberately absent teaches
the operator to ignore it.

Exit code is 0 only if every check passed.
"""
from __future__ import annotations

import json as _json
import pathlib
import sys
import time

# Running a script puts ITS directory on sys.path, not the app root, so
# `import app` fails from deploy/. Add the app root both ways: derived from this
# file when there is one, and the working directory (which is /app in the
# container). The second path is what lets the script be piped in over stdin —
# `docker exec -i ... python - < deploy/smoke_siblings.py` — so it can be run
# against a container that does not yet carry a copy of it, without writing
# anything into that container.
if "__file__" in globals():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path.cwd()))

RESET, RED, GRN, YEL, BOLD = "\033[0m", "\033[31m", "\033[32m", "\033[33m", "\033[1m"

results: list[dict] = []
SMOKE_PATIENT = "smoke-not-a-real-patient"
CONCEPT_PREFIX = "urn:pdhc:concept/"


def concept_guid(code_canonical: str) -> str | None:
    """GUID out of a stored ``code_canonical``, or None if it is not one.

    cdr stores Path-B: ``urn:pdhc:concept/<guid>`` — NOT a termbank URI. This
    matters because ``plan_client.parse_canonical_uri`` parses the *termbank*
    form, so pointing it at a stored canonical returns None for EVERY row. The
    first version of this smoke did exactly that and reported 23/23 "bad",
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


def main(as_json: bool = False) -> int:
    import requests
    from app import create_app
    app = create_app()

    with app.app_context():
        from flask import current_app
        cfg = current_app.config

        # ── config the calls depend on ─────────────────────────────────────
        @check("config: PLAN_BASE_URL is set")
        def _plan_cfg():
            v = cfg.get("PLAN_BASE_URL")
            return bool(v), v or "MISSING — concept canonicalisation cannot work"

        @check("config: IPS_BASE_URL is set")
        def _ips_cfg():
            v = cfg.get("IPS_BASE_URL")
            return bool(v), v or "MISSING — the consent gate cannot get a verdict"

        @check("config: SSO client credential pair is present")
        def _sso_cfg():
            cid, sec = cfg.get("SSO_CLIENT_ID"), cfg.get("SSO_CLIENT_SECRET")
            # An ABSENT pair is the failure that has actually happened before
            # (memory: PDHC SSO client creds audit) — /me/service 401s and the
            # callback fails silently. Presence is what a read-only smoke can
            # check without minting a token.
            return bool(cid and sec), ("client id + secret present" if cid and sec
                                       else "MISSING — /me/service will 401")

        # ── sso.pdhc ───────────────────────────────────────────────────────
        @check("sso: SSO_BASE_URL reachable")
        def _sso_live():
            base = (cfg.get("SSO_BASE_URL") or "").rstrip("/")
            if not base:
                return False, "SSO_BASE_URL not set"
            r = requests.get(f"{base}/api/health", timeout=10)
            return r.status_code == 200, f"/api/health {r.status_code}"

        # ── plan.pdhc: drive the real client against a REAL canonical uri ──
        @check("plan: a real code_canonical from this CDR resolves")
        def _plan_resolve():
            from app.models.resources import live_model
            from app.services.plan_client import PlanClient, PlanUnreachable
            base = (cfg.get("PLAN_BASE_URL") or "").rstrip("/")
            if not base:
                return False, "PLAN_BASE_URL not set"

            Observation = live_model("Observation")
            if Observation is None:
                return False, "no Observation model registered"
            # A canonical this CDR actually stored, so the check exercises the
            # real shape rather than one invented here.
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
            client = PlanClient(base_url=base)
            try:
                resolved = client.resolve_concept(guid)
            except PlanUnreachable as e:
                return False, f"plan.pdhc unreachable: {e}"
            if not resolved:
                return False, (f"plan has no canonical binding for a concept this "
                               f"CDR stores on live rows: {uri}")
            return True, (f"{guid[:8]}… → "
                          f"{resolved.get('canonical_uri') or resolved.get('display')}")

        # ── ips.pdhc: can a consent verdict be obtained AT ALL? ────────────
        @check("ips: a consent verdict is obtainable for a service-shaped read")
        def _ips_verdict():
            from app.services.analysis_consent import _analysis_filter, IpsUnreachable
            base = (cfg.get("IPS_BASE_URL") or "").rstrip("/")
            if not base:
                return False, "IPS_BASE_URL not set"
            # No flask session here, which is exactly the shape of a
            # service-key caller (gateway, analyse, sim). _analysis_filter only
            # attaches `Authorization: Bearer` when a session token exists, and
            # ips reads ONLY that header — X-API-Key is silently ignored
            # (memory: ips.pdhc auth header scheme). So this answers the
            # question the unit tests mock away: with no operator bearer, does
            # cdr get a verdict, or does the gate simply refuse everything?
            try:
                out = _analysis_filter([SMOKE_PATIENT], "research", [])
            except IpsUnreachable as e:
                return False, (f"{e} — the gate fails CLOSED, which is safe but "
                               f"means a service-key caller reads NOTHING. cdr1 "
                               f"holds no IPS_API_KEY, so it can only authenticate "
                               f"by forwarding an operator's bearer.")
            if not isinstance(out, dict) or "allowed" not in out:
                return False, f"unexpected analysis-filter shape: {out!r}"
            return True, (f"allowed={len(out['allowed'])} "
                          f"excluded={len(out['excluded'])} for a fake guid")

        # ── cdr's own rows must satisfy the contract it sends to plan ──────
        @check("data: every stored code_canonical carries a GUID (Rule 18)")
        def _canonical_shape():
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
            # Not a sibling call, but it is about the contract cdr sends to
            # plan: a canonical with a non-GUID tail can never be resolved, so
            # the row is permanently un-canonicalisable. Rule 18 is GUID-only.
            if bad:
                shown = ", ".join(sorted(bad)[:4])
                more = f" (+{len(bad) - 4} more)" if len(bad) > 4 else ""
                return False, (f"{len(bad)}/{total} distinct canonicals have a "
                               f"non-GUID tail and can never resolve: "
                               f"{shown}{more}")
            return True, f"all {total} distinct canonicals carry a GUID"

        # ── Cambio: configuration only, deliberately not called ────────────
        @check("cambio: delivery config is coherent (not called)")
        def _cambio_cfg():
            enabled = bool(cfg.get("CAMBIO_DELIVERY_ENABLED"))
            needed = ("CAMBIO_BASE_URL", "CAMBIO_TOKEN_URL",
                      "CAMBIO_CLIENT_ID", "CAMBIO_CLIENT_SECRET")
            missing = [k for k in needed if not cfg.get(k)]
            if not enabled:
                return True, ("delivery disabled; "
                              + ("config present" if not missing
                                 else f"{len(missing)} value(s) unset — fine while disabled"))
            return not missing, ("enabled, config complete" if not missing
                                 else f"ENABLED but missing {', '.join(missing)}")

        # ── xlate: absent on purpose ───────────────────────────────────────
        @check("xlate: absent as expected (not running in production)")
        def _xlate():
            import os
            v = cfg.get("XLATE_BASE_URL") or os.environ.get("XLATE_BASE_URL", "")
            if not v:
                return True, "unset — matches CLAUDE.md §3 (declared, not running)"
            r = requests.get(f"{v.rstrip('/')}/healthz", timeout=5)
            return r.status_code == 200, f"configured and answered {r.status_code}"

    failed = [r for r in results if not r["ok"]]
    if as_json:
        print(_json.dumps({"service": "cdr.pdhc", "checks": results,
                           "failed": len(failed)}, indent=2))
    else:
        print(f"\n{BOLD}cdr.pdhc — sibling smoke{RESET}\n")
        for r in results:
            mark = f"{GRN}✓{RESET}" if r["ok"] else f"{RED}✗{RESET}"
            print(f"  {mark} {r['check']:<58} {r['note']}  ({r['ms']}ms)")
        if failed:
            print(f"\n{RED}{BOLD}{len(failed)} check(s) failed.{RESET} "
                  f"cdr cannot do its job until these pass.\n")
        else:
            print(f"\n{GRN}{BOLD}All {len(results)} checks passed.{RESET}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(as_json="--json" in sys.argv))
