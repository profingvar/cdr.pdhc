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

**Two failure states, deliberately distinct** (#833). "We could not
reach xlate" and "there is no xlate to reach" need different answers,
because only one of them is worth retrying:

  - ``XLATE_BASE_URL`` unset  -> ``XlateNotConfigured``, permanent.
    The caller must reject terminally (422), because no amount of
    retrying will conjure the service into existence.
  - ``XLATE_BASE_URL`` set, call failed -> ``XlateUnavailable``,
    transient. The caller returns 503 and the producer may retry.

There is deliberately **no default base URL**. A fallback of
``http://127.0.0.1:9017`` (which this module carried until #833) is
indistinguishable from a real configuration to every caller, resolves
inside the app container to the container itself, and so turned "nobody
wired xlate up" into an endless 503 retry loop. An unset variable must
read as unset.
"""
from __future__ import annotations

import logging
import os
import time
from threading import Lock
from typing import Any

import requests


log = logging.getLogger(__name__)


# No DEFAULT_BASE_URL on purpose — see the module docstring (#833).
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_CACHE_TTL_SECONDS = 60.0


class XlateUnreachable(Exception):
    """Base: xlate.pdhc did not answer, so no mapping decision was made.

    Raised explicitly so the writer never mis-attributes a reachability
    failure to a missing mapping. **Never raised directly** — catch one
    of the two subclasses below, which differ in whether a retry can
    ever succeed. Kept as the base class so an older
    ``except XlateUnreachable`` still catches both.
    """


class XlateNotConfigured(XlateUnreachable):
    """PERMANENT: no ``XLATE_BASE_URL``, so this CDR has no xlate at all.

    A foreign coding cannot be translated here and never will be until
    someone configures the service. The caller must reject terminally
    (422 + ``xlate_not_configured``) rather than return a retryable 503
    — a producer that retries a 503 forever is the live defect #833 was
    filed for.
    """

    def __init__(self, message: str, *, system: str | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.system = system
        self.code = code


class XlateUnavailable(XlateUnreachable):
    """TRANSIENT: xlate is configured but did not answer this attempt.

    Someone wired a base URL up, so a blip is plausible and a retry may
    well succeed. The caller returns 503 + ``transient``.
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
        # Empty string and unset both mean "not configured" — normalise
        # to None so `configured` is a single honest predicate.
        self.base_url = (
            base_url or os.environ.get("XLATE_BASE_URL") or ""
        ).rstrip("/") or None
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._lock = Lock()

    @property
    def configured(self) -> bool:
        """True when an xlate base URL was actually supplied.

        Lets a smoke test or a health endpoint state which of the two
        failure modes this deployment is in without issuing a write.
        """
        return self.base_url is not None

    def translate(self, system: str, code: str) -> dict | None:
        """Return a dict ``{canonical_uri, mapping_quality, via}`` on a hit,
        or ``None`` on a clean miss (xlate said ``result: false``).

        Raises ``XlateNotConfigured`` (permanent, caller 422s) when no
        ``XLATE_BASE_URL`` is set, or ``XlateUnavailable`` (transient,
        caller 503s) when it is set but the call failed.
        """
        if not self.configured:
            # Refuse before the network call: there is nothing to call,
            # and a 5s connect timeout per coding would be a slow lie.
            raise XlateNotConfigured(
                "XLATE_BASE_URL is not set, so this CDR cannot translate "
                f"foreign coding system={system!r} code={code!r}",
                system=system, code=code,
            )

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
            log.warning("xlate.pdhc unreachable at %s: %s", self.base_url, e)
            raise XlateUnavailable(str(e)) from e

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
