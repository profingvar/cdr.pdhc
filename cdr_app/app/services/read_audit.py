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

#984 (operator, 2026-11-17): this table is THE ONE access log for
diagnoses. A service caller named in ``ON_BEHALF_SERVICES`` may add, per
request:

  - ``X-Operator-User-Guid``  -> ``on_behalf_of_user_guid``. A RECORDED CLAIM
    of the calling service (exactly like ``X-Operator-Session-Id``): the CDR
    did not authenticate that user, the service did. It never fills
    ``caller_user_guid``, which holds only identities the CDR verified.
  - ``X-Access-Basis: break_glass`` + ``X-Break-Glass-Reason`` (URL-encoded,
    at most 500 characters decoded) -> ``outcome='break_glass'`` and
    ``detail.reason``.

From anyone else (another service, or an operator session) the headers are
IGNORED and a WARNING is logged: an unlisted caller cannot put a user or a
break-glass claim into the log.

BREAK-GLASS IS NOT BEST-EFFORT. For an accepted break-glass request a failed
audit write turns the response into ``503 BREAK_GLASS_AUDIT_UNAVAILABLE``
(``install`` -> after_request), and every recorded row is confirmed to the
caller in ``X-CDR-Read-Audit-Id``. icd11 refuses to serve a break-glass read
whose answer lacks that header. Ordinary reads keep the best-effort posture.
"""
from __future__ import annotations

import logging

from urllib.parse import unquote

from flask import g, jsonify, request

from app import db
from app.auth import scope_org_guids
from app.models import ReadAudit
from app.services.analysis_consent import analysis_purpose

log = logging.getLogger(__name__)

#: Service callers whose on-behalf headers (X-Operator-User-Guid,
#: X-Access-Basis, X-Break-Glass-Reason) are recorded, and who may post
#: refusals to POST /api/v1/read-audit/events. Anyone else: ignored + WARNING.
ON_BEHALF_SERVICES = frozenset({"icd11.pdhc", "dashboard.pdhc"})
#: Who may POST /api/v1/read-audit/events (a refusal that never reached the
#: CDR). Narrower than ON_BEHALF_SERVICES: dashboard.pdhc is read-only (#760)
#: and has no consumer for it (operator review, 2026-11-17).
EVENT_SERVICES = frozenset({"icd11.pdhc"})

OUTCOMES = ("served", "denied", "break_glass")
MAX_REASON = 500
USER_GUID_MAX = 36
_ON_BEHALF_HEADERS = ("X-Operator-User-Guid", "X-Access-Basis",
                      "X-Break-Glass-Reason")
AUDIT_ID_HEADER = "X-CDR-Read-Audit-Id"


def _service_of_request() -> str | None:
    blob = getattr(g, "access_blob", None)
    return blob.get("service_source") if isinstance(blob, dict) else None


def break_glass_requested() -> bool:
    """An ALLOWED service asked for this request to be logged as break-glass."""
    return (_service_of_request() in ON_BEHALF_SERVICES
            and (request.headers.get("X-Access-Basis") or "").strip() == "break_glass")


def on_behalf_claims(service: str | None) -> tuple[str | None, str | None, dict]:
    """(on_behalf_of_user_guid, outcome, detail) from the on-behalf headers.

    Only for a service in ON_BEHALF_SERVICES. From anyone else the headers are
    ignored, loudly, and (None, None, {}) is returned.
    """
    present = [h for h in _ON_BEHALF_HEADERS if request.headers.get(h)]
    if not present:
        return None, None, {}
    if service not in ON_BEHALF_SERVICES:
        log.warning(
            "cdr read-audit: IGNORED %s from %s -- only %s may state an end "
            "user or a break-glass basis (#984)", ",".join(present),
            f"service {service!r}" if service else "a non-service caller",
            ",".join(sorted(ON_BEHALF_SERVICES)))
        return None, None, {}

    user = (request.headers.get("X-Operator-User-Guid") or "").strip() or None
    if user and len(user) > USER_GUID_MAX:
        log.warning("cdr read-audit: X-Operator-User-Guid from %s is %d chars "
                    "(max %d) -- not recorded", service, len(user), USER_GUID_MAX)
        user = None

    outcome, detail = None, {}
    basis = (request.headers.get("X-Access-Basis") or "").strip()
    if basis == "break_glass":
        outcome = "break_glass"
        reason = unquote(request.headers.get("X-Break-Glass-Reason") or "").strip()
        if not reason:
            log.warning("cdr read-audit: break_glass from %s WITHOUT a reason", service)
            detail["reason"] = None
            detail["reason_missing"] = True
        else:
            if len(reason) > MAX_REASON:
                reason = reason[:MAX_REASON]
                detail["reason_truncated"] = True
            detail["reason"] = reason
    elif basis:
        log.warning("cdr read-audit: unknown X-Access-Basis %r from %s -- ignored",
                    basis[:40], service)
    return user, outcome, detail


def _single_patient_param() -> str | None:
    """`?patient=<one guid>` on a search, so a per-patient search is logged
    against that patient. A list (comma) stays NULL rather than picking one."""
    vals = request.args.getlist("patient")
    if len(vals) == 1 and vals[0] and "," not in vals[0]:
        return vals[0][:64]
    return None


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
                      response_status: int = 200,
                      detail: dict | None = None,
                      outcome: str | None = None) -> None:
    """One row for this read. `detail` is metadata only (e.g. as_of) --
    NEVER clinical content."""
    g._read_audit_attempted = True
    try:
        blob = getattr(g, "access_blob", None)
        blob = blob if isinstance(blob, dict) else {}
        service = blob.get("service_source")
        on_behalf, claimed, claim_detail = on_behalf_claims(service)
        # #1045: a route that REFUSED a person states it (`denied`); a
        # service's break-glass claim still wins over that.
        outcome = claimed or outcome
        merged = {**(detail or {}), **claim_detail}

        row = ReadAudit(
            caller_service=service,
            caller_user_guid=None if service else blob.get("user_guid"),
            caller_org_guids=None if service else scope_org_guids(blob),
            route=(f"{request.method} "
                   f"{request.url_rule.rule if request.url_rule else request.path}"),
            resource_type=resource_type,
            patient_guid=patient_guid or _single_patient_param(),
            n_rows_returned=n_rows,
            response_status=response_status,
            # Operator sid: the blob carries it on session reads; machine
            # hops (dashboard federation) forward it as
            # X-Operator-Session-Id (X2 #408) — same resolution the
            # ingest path uses, so the kontroller chain survives the hop.
            session_id=(blob.get("session_id") or _forwarded_session_id()),
            on_behalf_of_user_guid=on_behalf,
            outcome=outcome or "served",
            detail=merged or None,
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
        g.setdefault("_read_audit_ids", []).append(row.id)
    except Exception as exc:  # noqa: BLE001 — audit must not break reads
        g._read_audit_failed = True
        try:
            db.session.rollback()
        except Exception:
            pass
        log.warning("cdr read-audit write failed: %s", exc)


def _after_request(response):
    """Confirm recorded rows; withhold a break-glass answer that was not logged."""
    if not getattr(g, "_read_audit_attempted", False):
        return response
    ids = getattr(g, "_read_audit_ids", None) or []
    failed = getattr(g, "_read_audit_failed", False)
    if break_glass_requested() and (failed or not ids):
        log.error("cdr read-audit: break-glass read %s %s WITHHELD -- the log "
                  "write failed (#984, fail closed)", request.method, request.path)
        resp = jsonify({
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "transient",
                       "diagnostics": "BREAK_GLASS_AUDIT_UNAVAILABLE",
                       "details": {"text": "the break-glass read could not be "
                                           "written to the read log, so it is "
                                           "not served"}}]})
        resp.status_code = 503
        return resp
    if ids and not failed:
        response.headers[AUDIT_ID_HEADER] = ",".join(str(i) for i in ids)
    return response


def install(app) -> None:
    app.after_request(_after_request)
