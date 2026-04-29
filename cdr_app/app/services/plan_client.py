"""plan.pdhc HTTP client for CDR's write-side validation.

After xlate.pdhc has translated a foreign coding to a termbank canonical
URI, the CDR has to confirm the canonical is **referenced from an active
Concept or ValueCatalog row** in plan.pdhc — i.e. the workgroup has
actually adopted it. plan.pdhc exposes
``GET /api/v1/ValueSet/$validate-code?system=&code=`` which answers that
question (platform-plan §1.2.d).

If the canonical is not in plan.pdhc's working set, the CDR rejects the
write with 422 + ``plan_miss`` and bookkeeps the canonical to its own
``cdr_audit_plan_miss`` table for the workgroup to triage out-of-band
(§1.2.d.ii).

This client also resolves plan.pdhc Concept GUIDs to termbank canonical
URIs (`resolve_concept`) — used by the canonicaliser when sim emits
indirect references with `system = https://plan.pdhc.se/Concept`.

All HTTP gets retry transparently on 429/503 (post_seed_followups Block A);
exhausting the retry budget escalates to PlanUnreachable so the writer
returns 503 transient, not 422 plan_miss.
"""
from __future__ import annotations

import logging
import os
import re
import time
from threading import Lock
from typing import Any

import requests


log = logging.getLogger(__name__)


DEFAULT_BASE_URL = "http://127.0.0.1:9030"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_CACHE_TTL_SECONDS = 60.0

# When plan.pdhc returns 429 (rate-limited) or 503 (briefly overloaded)
# the canonicaliser previously interpreted it as "concept not adopted"
# and rejected the FHIR write with 422 plan_miss. That mis-attribution
# bit us during the first parallel seed (6/400 bundles 4xx). Retry
# with exponential backoff and only escalate to PlanUnreachable when
# we've exhausted the budget — at which point the writer correctly
# returns 503 transient (sim retries on its own loop).
_TRANSIENT_HTTP_STATUSES = (429, 503)
_MAX_RETRIES = 3
_INITIAL_BACKOFF_SECONDS = 0.25


_CANONICAL_RE = re.compile(r"^https?://[^/]+/CodeSystem/([^/]+)/(.+)$")


# canonical_lib_name → URI slug used in the termbank canonical URI.
# This mapping has to match what termbank.pdhc would publish if it
# were live; keeping it deterministic here lets the CDR store stable
# URIs even before termbank is deployed.
_LIB_SLUG = {
    "LOINC":     "loinc",
    "Snomed CT": "snomed",
    "ICD10":     "icd10",
    "ATC":       "atc",
    "KVÅ":       "kva",
    "local":     "local",
}


def parse_canonical_uri(canonical_uri: str) -> tuple[str, str] | None:
    """Pull the (canonical_lib_name, canonical_refnumber) pair out of a
    termbank canonical URI."""
    if not canonical_uri:
        return None
    m = _CANONICAL_RE.match(canonical_uri)
    if m is None:
        return None
    return m.group(1), m.group(2)


class PlanUnreachable(Exception):
    """plan.pdhc could not be reached on this attempt.

    Distinguished from a clean ``result: false`` so the writer can return
    a transient error rather than mis-attributing a reachability blip to
    a workgroup-adoption miss.
    """


class PlanClient:

    _MISS = object()

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("PLAN_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._lock = Lock()

    def validate_canonical(self, canonical_uri: str) -> dict | None:
        """Return ``{result: bool, ref_via, ref_guid, display, ...}`` from
        plan.pdhc's ``$validate-code`` response, or ``None`` if the
        canonical URI is malformed.

        Raises ``PlanUnreachable`` if plan.pdhc is unreachable.
        """
        parsed = parse_canonical_uri(canonical_uri)
        if parsed is None:
            return None
        system, code = parsed
        return self.validate_code(system, code)

    def _get_with_retry(self, url: str, *, params: dict | None = None,
                        operation: str) -> "requests.Response":
        """GET with retry on 429/503 (transient) — exponential backoff,
        bounded retries. On exhaustion, raises ``PlanUnreachable`` so the
        canonicaliser returns 503 to the writer (transient, retry-friendly)
        rather than 422 plan_miss (terminal, write-rejected)."""
        backoff = _INITIAL_BACKOFF_SECONDS
        last_status: int | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = requests.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("plan.pdhc unreachable on %s (attempt %d): %s",
                            operation, attempt + 1, e)
                raise PlanUnreachable(str(e)) from e
            if resp.status_code not in _TRANSIENT_HTTP_STATUSES:
                return resp
            last_status = resp.status_code
            if attempt >= _MAX_RETRIES:
                break
            log.info("plan.pdhc %s returned %s — backoff %.2fs (attempt %d/%d)",
                     operation, resp.status_code, backoff, attempt + 1, _MAX_RETRIES)
            time.sleep(backoff)
            backoff *= 2
        raise PlanUnreachable(
            f"plan.pdhc {operation} returned {last_status} after {_MAX_RETRIES} retries"
        )

    def validate_code(self, system: str, code: str) -> dict:
        """Return the parsed ``Parameters`` body from plan.pdhc."""
        key = ("validate_code", system, code)
        cached = self._get_cached(key)
        if cached is not self._MISS:
            return cached

        url = f"{self.base_url}/api/v1/ValueSet/$validate-code"
        resp = self._get_with_retry(url,
                                     params={"system": system, "code": code},
                                     operation="validate-code")

        if resp.status_code != 200:
            log.warning(
                "plan.pdhc /ValueSet/$validate-code returned %s for %s/%s",
                resp.status_code, system, code,
            )
            # On terminal 4xx/5xx (post-retry), treat as a clean "no" so the
            # CDR rejects with plan_miss rather than a transient 503.
            return {"result": False, "_status": resp.status_code}

        body = resp.json()
        parsed = self._parse_parameters(body, system=system, code=code)
        self._set_cached(key, parsed)
        return parsed

    @staticmethod
    def _parse_parameters(body: dict, *, system: str, code: str) -> dict:
        """Pull a flat dict out of a FHIR Parameters body."""
        out: dict = {"system": system, "code": code, "result": False}
        for p in body.get("parameter") or []:
            name = p.get("name")
            if "valueBoolean" in p:
                out[name] = p["valueBoolean"]
            elif "valueString" in p:
                out[name] = p["valueString"]
            elif "valueCode" in p:
                out[name] = p["valueCode"]
        return out

    # ----- cache helpers -------------------------------------------------
    def _get_cached(self, key: tuple):
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return self._MISS
            ttl_expires_at, value = entry
            if time.monotonic() >= ttl_expires_at:
                self._cache.pop(key, None)
                return self._MISS
            return value

    def _set_cached(self, key: tuple, value, *, ttl: float | None = None) -> None:
        ttl = ttl if ttl is not None else self.cache_ttl
        with self._lock:
            self._cache[key] = (time.monotonic() + ttl, value)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ---------------------------------------------------------------
    # Concept-GUID indirection (sim emits coding[].system =
    # https://plan.pdhc.se/Concept and code = <guid>).
    # ---------------------------------------------------------------
    def resolve_concept(self, guid: str) -> dict | None:
        """Look up plan.pdhc /api/v1/concepts/<guid> and compose a
        termbank-style canonical URI from the Concept's
        canonical_lib + canonical_refnumber. Returns None if the
        Concept doesn't exist or its canonical_lib slug is unknown.

        Result keys: canonical_uri, system, canonical_refnumber,
        canonical_lib_name, display, guid."""
        key = ("resolve_concept", guid)
        cached = self._get_cached(key)
        if cached is not self._MISS:
            return cached

        url = f"{self.base_url}/api/v1/concepts/{guid}"
        resp = self._get_with_retry(url, operation=f"resolve_concept[{guid[:8]}]")

        if resp.status_code != 200:
            log.warning("plan.pdhc /concepts/%s returned %s", guid, resp.status_code)
            return None

        body = resp.json()
        canon_lib_guid = body.get("canonical_lib")
        canon_ref = body.get("canonical_refnumber")
        if not canon_lib_guid or not canon_ref:
            return None

        lib_name = self._lib_name(canon_lib_guid)
        if not lib_name:
            return None
        slug = _LIB_SLUG.get(lib_name)
        if not slug:
            log.warning("unknown canonical_lib_name '%s' for concept %s", lib_name, guid)
            return None

        system = f"https://termbank.pdhc.se/CodeSystem/{slug}"
        canonical_uri = f"{system}/{canon_ref}"
        result = {
            "canonical_uri": canonical_uri,
            "system": system,
            "canonical_refnumber": canon_ref,
            "canonical_lib_name": lib_name,
            "display": body.get("concept_display_text") or body.get("concept_name"),
            "guid": guid,
        }
        self._set_cached(key, result)
        return result

    def _lib_name(self, lib_guid: str) -> str | None:
        """canonical_lib_name lookup by GUID, cached. Libs change very
        rarely so cache TTL is the standard one — entries refresh on
        their own; clear_cache() flushes."""
        key = ("lib_name", lib_guid)
        cached = self._get_cached(key)
        if cached is not self._MISS:
            return cached
        url = f"{self.base_url}/api/v1/canonical-libs/{lib_guid}"
        resp = self._get_with_retry(url, operation=f"lib_name[{lib_guid[:8]}]")
        if resp.status_code != 200:
            return None
        name = resp.json().get("canonical_lib_name")
        if name:
            self._set_cached(key, name, ttl=self.cache_ttl * 60)
        return name
