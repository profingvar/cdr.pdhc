"""#422 — analysis-phase consent enforcement (EHDS opt-out, per-project
research consent, quality-registry opt-out) for cdr1..cdr5.

ips.pdhc owns the consent flags (D1 #404) and the verdict
(``POST /api/v1/patients/analysis-filter``, contract locked in
plans/pdhc_data_shapes.md §5) — nothing is computed locally.

Per-reader boundary (the #422 rollout model): enforcement fires when THIS
CDR is the reader serving a human operator (session-SSO blob). Service-key
reads (dashboard.pdhc federation, sim) are the *sibling's* operator
context — dashboard applies the same join on its side (#415) — and such a
machine identity has no role to derive a purpose from, so they pass
through here.

#664 amends that for one case: a machine MAY DECLARE a purpose via
``X-Access-Purpose``, and is then filtered on it. An analysis node is a
machine whose purpose is an explicit parameter of the analysis spec, not
something to be inferred from a role it does not have. Declaring can only
ever REDUCE what a caller sees — the alternative is the pass-through above —
so this is additive and cannot regress dashboard.pdhc or sim, neither of
which sends the header.

Purpose derivation from the ACTIVE affiliation role (v3 locked spec):
researcher → research (+ that affiliation's research_project_guids);
quality/registry roles → quality_registry; other clinical roles →
statistics; SU-admin without affiliations → administration (never
blocked; ips call skipped as an equivalent-outcome shortcut).

Failure mode is CLOSED: no ips verdict → 503, no patient data.
"""
from __future__ import annotations

import os

import requests
from flask import abort, current_app, g, request, session


DEFAULT_TIMEOUT = 4.0


class IpsUnreachable(Exception):
    """ips.pdhc could not answer the consent question — reads fail closed."""


def _operator_blob() -> dict | None:
    """The current operator's blob, or None for machine/dev-SU contexts."""
    blob = getattr(g, "access_blob", None)
    if not isinstance(blob, dict):
        return None
    if blob.get("service_source"):          # service-key machine identity
        return None
    if blob.get("is_su_admin") and not (blob.get("affiliations") or []):
        return None                          # administration purpose — never blocked
    return blob


def _active_affiliation(blob: dict) -> dict | None:
    affs = blob.get("affiliations") or []
    if not affs:
        return None
    active_guid = blob.get("active_affiliation_guid")
    if active_guid:
        for a in affs:
            if a.get("affiliation_guid") == active_guid:
                return a
    if len(affs) == 1:
        return affs[0]
    return None


def analysis_purpose(blob: dict) -> tuple[str, list]:
    """Map the caller's active role to (purpose, research_project_guids)."""
    aff = _active_affiliation(blob)
    role = str((aff or {}).get("role")
               or blob.get("professional_role") or "").lower()
    if "research" in role:
        return "research", list((aff or {}).get("research_project_guids") or [])
    if "quality" in role or "registr" in role:
        return "quality_registry", []
    return "statistics", []


# #664: the purposes a MACHINE caller may declare. Deliberately only the
# SECONDARY-use purposes. An analysis node reads for secondary use by
# definition, and the primary-use values (care, care_coordination,
# patient_access, administration) are either never blocked or belong to a
# care-delivery basis the analysis path has no claim to — letting a service
# declare one of those would turn this from a gate into a bypass.
DECLARABLE_SERVICE_PURPOSES = frozenset({
    "research", "statistics", "quality_registry",
})

PURPOSE_HEADER = "X-Access-Purpose"
RESEARCH_PROJECTS_HEADER = "X-Research-Project-Guids"


def declared_service_purpose() -> tuple[str, list] | None:
    """(purpose, research_project_guids) when a MACHINE caller declares a
    read purpose, else None.

    #664. Before this, every service-key caller passed the consent gate
    untouched, on the reasoning that 'a machine identity has no role to derive
    a purpose from'. That holds for dashboard.pdhc and sim, which read under a
    sibling's operator context. It does NOT hold for an analysis node, whose
    purpose is an explicit, validated, logged parameter of the analysis spec.

    The safety property worth stating: declaring a purpose can only ever
    REDUCE what a caller sees, because the alternative is today's
    pass-through. A caller that declares nothing behaves exactly as before,
    so this is additive and cannot regress dashboard.pdhc or sim.
    """
    blob = getattr(g, "access_blob", None)
    if not isinstance(blob, dict) or not blob.get("service_source"):
        return None                       # not a machine identity
    try:
        raw = (request.headers.get(PURPOSE_HEADER) or "").strip().lower()
    except RuntimeError:                  # outside a request context
        return None
    if not raw:
        return None
    if raw not in DECLARABLE_SERVICE_PURPOSES:
        abort(400, description=(
            f"{PURPOSE_HEADER}: a service may declare only "
            f"{', '.join(sorted(DECLARABLE_SERVICE_PURPOSES))}"))
    projects: list = []
    if raw == "research":
        hdr = (request.headers.get(RESEARCH_PROJECTS_HEADER) or "").strip()
        projects = [p.strip() for p in hdr.split(",") if p.strip()]
        if not projects:
            # research consent is per project; without one ips cannot give a
            # meaningful verdict and every patient would be excluded anyway.
            abort(400, description=(
                f"{PURPOSE_HEADER}: research requires "
                f"{RESEARCH_PROJECTS_HEADER}"))
    return raw, projects


def _analysis_filter(patient_guids: list, purpose: str, projects: list) -> dict:
    base = (current_app.config.get("IPS_BASE_URL")
            or os.environ.get("IPS_BASE_URL", "")).rstrip("/")
    if not base:
        raise IpsUnreachable("IPS_BASE_URL not configured")
    from app.services.session_headers import outbound_session_headers
    headers = {"Accept": "application/json"}
    token = None
    try:
        token = session.get("sso_token")
    except RuntimeError:
        token = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(outbound_session_headers())
    try:
        r = requests.post(
            f"{base}/api/v1/patients/analysis-filter",
            json={"patient_guids": list(patient_guids), "purpose": purpose,
                  "research_project_guids": list(projects)},
            headers=headers, timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as e:
        raise IpsUnreachable(f"analysis-filter network error: {e}") from e
    if r.status_code != 200:
        raise IpsUnreachable(f"analysis-filter returned {r.status_code}")
    body = r.json() or {}
    return {"allowed": list(body.get("allowed") or []),
            "excluded": list(body.get("excluded") or [])}


def _resolve_purpose() -> tuple[str, list] | None:
    """(purpose, research_project_guids) for this read, or None to pass through.

    Two ways to have a purpose. A human operator gets one derived from their
    active affiliation role (the #422 model). A machine may DECLARE one
    (#664) — checked first, because a service blob makes _operator_blob()
    return None and would otherwise short-circuit to pass-through.

    None means no purpose could be established, which preserves the existing
    behaviour for dashboard.pdhc, sim, and SU-admin-without-affiliations.
    """
    declared = declared_service_purpose()
    if declared is not None:
        return declared
    blob = _operator_blob()
    if blob is None:
        return None
    return analysis_purpose(blob)


def _fail_closed(e: IpsUnreachable):
    current_app.logger.error("analysis-filter unavailable: %s", e)
    abort(503, description=(
        "consent filter (ips.pdhc) unavailable — analysis reads fail closed"))


def consent_allowed_guids(patient_guids: set) -> set:
    """Batch verdict: the subset of patient_guids the current operator may
    read under their role-derived purpose. Pass-through for machine/SU
    contexts. One ips call per request; aborts 503 when ips is down."""
    resolved = _resolve_purpose()
    if resolved is None or not patient_guids:
        return set(patient_guids)
    purpose, projects = resolved
    try:
        verdict = _analysis_filter(sorted(patient_guids), purpose, projects)
    except IpsUnreachable as e:
        _fail_closed(e)
    return set(patient_guids) & set(verdict["allowed"])


def check_patient_allowed(patient_guid: str) -> None:
    """Single-patient gate: abort 403 with the ips reason if excluded."""
    resolved = _resolve_purpose()
    if resolved is None or not patient_guid:
        return
    purpose, projects = resolved
    try:
        verdict = _analysis_filter([patient_guid], purpose, projects)
    except IpsUnreachable as e:
        _fail_closed(e)
    if patient_guid in set(verdict["allowed"]):
        return
    reason = "consent_excluded"
    for ex in verdict["excluded"]:
        if (ex or {}).get("patient_guid") == patient_guid:
            reason = ex.get("reason") or reason
            break
    abort(403, description=f"excluded by patient consent ({reason})")
