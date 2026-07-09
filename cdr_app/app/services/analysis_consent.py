"""#422 — analysis-phase consent enforcement (EHDS opt-out, per-project
research consent, quality-registry opt-out) for cdr1..cdr5.

ips.pdhc owns the consent flags (D1 #404) and the verdict
(``POST /api/v1/patients/analysis-filter``, contract locked in
plans/pdhc_data_shapes.md §5) — nothing is computed locally.

Per-reader boundary (the #422 rollout model): enforcement fires when THIS
CDR is the reader serving a human operator (session-SSO blob). Service-key
reads (dashboard.pdhc federation, sim) are the *sibling's* operator
context — dashboard applies the same join on its side (#415) — and a
machine identity has no role to derive a purpose from, so they pass
through here.

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
from flask import abort, current_app, g, session


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


def _fail_closed(e: IpsUnreachable):
    current_app.logger.error("analysis-filter unavailable: %s", e)
    abort(503, description=(
        "consent filter (ips.pdhc) unavailable — analysis reads fail closed"))


def consent_allowed_guids(patient_guids: set) -> set:
    """Batch verdict: the subset of patient_guids the current operator may
    read under their role-derived purpose. Pass-through for machine/SU
    contexts. One ips call per request; aborts 503 when ips is down."""
    blob = _operator_blob()
    if blob is None or not patient_guids:
        return set(patient_guids)
    purpose, projects = analysis_purpose(blob)
    try:
        verdict = _analysis_filter(sorted(patient_guids), purpose, projects)
    except IpsUnreachable as e:
        _fail_closed(e)
    return set(patient_guids) & set(verdict["allowed"])


def check_patient_allowed(patient_guid: str) -> None:
    """Single-patient gate: abort 403 with the ips reason if excluded."""
    blob = _operator_blob()
    if blob is None or not patient_guid:
        return
    purpose, projects = analysis_purpose(blob)
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
