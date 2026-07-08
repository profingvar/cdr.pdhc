"""xlate.pdhc HTTP client for the CDR write path.

Calls ``POST /translate`` with a foreign ``(system, code)`` pair and
gets back a FHIR ``Parameters`` body. CDR's ingest pipeline calls this
for every ``CodeableConcept`` on a write so foreign codings get rewritten
to a termbank canonical (platform-plan §1.2.c).

Thread-safe TTL cache with a sentinel pattern that distinguishes
"absent" from "cached miss" — same shape as the termbank client in
xlate.pdhc itself.

Network errors are NOT cached as misses; a transient blip should not
lock us into a minute of false ``xlate_miss`` 422s. Known 404s and
explicit ``result: false`` responses are cached.
"""
from __future__ import annotations

import logging
import os
import time
from threading import Lock
from typing import Any

import requests


log = logging.getLogger(__name__)


DEFAULT_BASE_URL = "http://127.0.0.1:9017"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_CACHE_TTL_SECONDS = 60.0


class XlateUnreachable(Exception):
    """xlate.pdhc could not be reached on this attempt.

    Raised explicitly so the writer can return a ``transient_error`` /
    503 to the caller rather than mis-attributing the failure to a
    missing mapping.
    """


class XlateClient:

    _MISS = object()

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("XLATE_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._lock = Lock()

    def translate(self, system: str, code: str) -> dict | None:
        """Return a dict ``{canonical_uri, mapping_quality, via}`` on a hit,
        or ``None`` on a clean miss (xlate said ``result: false``).

        Raises ``XlateUnreachable`` if xlate.pdhc cannot be reached at all.
        """
        key = ("translate", system, code)
        cached = self._get_cached(key)
        if cached is not self._MISS:
            return cached

        url = f"{self.base_url}/translate"
        try:
            from app.services.session_headers import outbound_session_headers
            resp = requests.post(
                url, json={"system": system, "code": code},
                headers=outbound_session_headers(), timeout=self.timeout
            )
        except requests.RequestException as e:
            log.warning("xlate.pdhc unreachable: %s", e)
            raise XlateUnreachable(str(e)) from e

        if resp.status_code != 200:
            log.warning("xlate.pdhc /translate returned %s for %s/%s",
                        resp.status_code, system, code)
            return None

        body = resp.json()
        match = self._extract_match(body)
        # Cache both hits and clean misses for the full TTL.
        self._set_cached(key, match)
        return match

    @staticmethod
    def _extract_match(body: dict) -> dict | None:
        """Pull canonical_uri / mapping_quality / via from the FHIR Parameters
        body returned by xlate.pdhc /translate.

        Body shape::

            {
              "resourceType": "Parameters",
              "parameter": [
                {"name": "result", "valueBoolean": true},
                {"name": "match",  "valueCoding": {"system": "...", "code": "...", "display": "..."}},
                {"name": "mapping_quality", "valueCode": "equivalent"},
                {"name": "via", "valueString": "system_alias"}
              ]
            }
        """
        params = {p["name"]: p for p in body.get("parameter") or []}

        result = params.get("result", {}).get("valueBoolean", False)
        if not result:
            return None

        match = params.get("match", {}).get("valueCoding") or {}
        canonical_system = match.get("system", "")
        canonical_code = match.get("code", "")
        if not canonical_system or not canonical_code:
            return None

        # Reconstruct the canonical URI. termbank canonical is
        # "https://termbank.pdhc.se/CodeSystem/<system>/<code>".
        canonical_uri = (
            f"https://termbank.pdhc.se/CodeSystem/{canonical_system}/{canonical_code}"
        )
        return {
            "canonical_uri": canonical_uri,
            "canonical_system": canonical_system,
            "canonical_code": canonical_code,
            "display": match.get("display"),
            "mapping_quality": params.get("mapping_quality", {}).get("valueCode"),
            "via": params.get("via", {}).get("valueString"),
        }

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
