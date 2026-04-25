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


_CANONICAL_RE = re.compile(r"^https?://[^/]+/CodeSystem/([^/]+)/(.+)$")


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

    def validate_code(self, system: str, code: str) -> dict:
        """Return the parsed ``Parameters`` body from plan.pdhc."""
        key = ("validate_code", system, code)
        cached = self._get_cached(key)
        if cached is not self._MISS:
            return cached

        url = f"{self.base_url}/api/v1/ValueSet/$validate-code"
        try:
            resp = requests.get(
                url,
                params={"system": system, "code": code},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            log.warning("plan.pdhc unreachable: %s", e)
            raise PlanUnreachable(str(e)) from e

        if resp.status_code != 200:
            log.warning(
                "plan.pdhc /ValueSet/$validate-code returned %s for %s/%s",
                resp.status_code, system, code,
            )
            # On 4xx/5xx that aren't unreachable, treat as a clean "no" so we
            # don't mis-attribute server bugs to a missing canonical.
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
