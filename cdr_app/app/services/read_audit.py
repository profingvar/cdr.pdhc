"""X1 (#407/#443) — read-audit emission for cdr's FHIR read surface.

One ``record_read_audit`` call per read request, just before the
response returns. Best-effort: a failed audit write is logged at
WARNING and swallowed — the data response is never blocked (same
posture as every other PDHC audit writer).

Tuple derivation (plans/pdhc_data_shapes.md §5):
  - operator (session-SSO blob): person_guid + Zone-1 org scope +
    role_guid from the ACTIVE affiliation; purpose via the #422 role
    mapping (analysis_consent.analysis_purpose); access_basis su_admin /
    research_consent (research reads admitted by the ips join) /
    same_unit.
  - machine (service-key): caller_service only, tuple NULL — the
    sibling reader (dashboard #415, sim) holds the operator context and
    logs the real purpose on its side.
"""
from __future__ import annotations

import logging

from flask import g, request

from app import db
from app.auth import scope_org_guids
from app.models import ReadAudit
from app.services.analysis_consent import analysis_purpose

log = logging.getLogger(__name__)


def _forwarded_session_id() -> str | None:
    val = request.headers.get("X-Operator-Session-Id")
    return val[:128] if val else None


def _active_role_guid(blob: dict) -> str | None:
    affs = blob.get("affiliations") or []
    active_guid = blob.get("active_affiliation_guid")
    if active_guid:
        for a in affs:
            if a.get("affiliation_guid") == active_guid:
                return a.get("role_guid")
    if len(affs) == 1:
        return affs[0].get("role_guid")
    return None


def record_read_audit(*, resource_type: str | None = None,
                      patient_guid: str | None = None,
                      n_rows: int | None = None,
                      response_status: int = 200) -> None:
    try:
        blob = getattr(g, "access_blob", None)
        blob = blob if isinstance(blob, dict) else {}
        service = blob.get("service_source")

        row = ReadAudit(
            caller_service=service,
            caller_user_guid=None if service else blob.get("user_guid"),
            caller_org_guids=None if service else scope_org_guids(blob),
            route=(f"{request.method} "
                   f"{request.url_rule.rule if request.url_rule else request.path}"),
            resource_type=resource_type,
            patient_guid=patient_guid,
            n_rows_returned=n_rows,
            response_status=response_status,
            # Operator sid: the blob carries it on session reads; machine
            # hops (dashboard federation) forward it as
            # X-Operator-Session-Id (X2 #408) — same resolution the
            # ingest path uses, so the kontroller chain survives the hop.
            session_id=(blob.get("session_id") or _forwarded_session_id()),
        )
        if not service and blob:
            purpose, _projects = analysis_purpose(blob)
            row.role_guid = _active_role_guid(blob)
            row.purpose = purpose if not blob.get("is_su_admin") \
                else "administration"
            if blob.get("is_su_admin"):
                row.access_basis = "su_admin"
            elif purpose == "research":
                row.access_basis = "research_consent"
            else:
                row.access_basis = "same_unit"
        db.session.add(row)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 — audit must not break reads
        try:
            db.session.rollback()
        except Exception:
            pass
        log.warning("cdr read-audit write failed: %s", exc)
