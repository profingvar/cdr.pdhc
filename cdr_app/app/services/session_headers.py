"""Operator-session propagation (X2, tickets #408 / #423).

Per-repo copy of the gateway.pdhc reference helper (there is deliberately no
shared library across the PDHC services). Forwards the operator's SSO session
id (the JWT ``sid`` claim, ticket #191) on onward calls so one session_id
threads the whole request -> gateway -> cdr1 -> Cambio chain end-to-end.

Two callers:
  - synchronous ingest-context clients (plan/xlate canonicalisation) call
    ``outbound_session_headers()`` and it resolves the sid from the live ingest
    request (the X-Operator-Session-Id header gateway forwarded).
  - the async cambio_worker has NO request context, so it captures the sid on
    the CambioDeliveryLog row at ingest and replays it by passing it explicitly:
    ``outbound_session_headers(log.operator_session_id)``.

Returns ``{}`` (never an empty header) when no session id is available.
"""
from flask import request, session


def current_session_id():
    try:
        header_val = request.headers.get("X-Operator-Session-Id")
    except RuntimeError:
        header_val = None
    if header_val:
        return header_val[:128]
    blob = session.get("access_blob") if session else None
    if isinstance(blob, dict):
        sid = blob.get("session_id")
        if sid:
            return str(sid)[:128]
    return None


def outbound_session_headers(session_id=None):
    """Headers to attach to an onward operator-context call. Pass ``session_id``
    explicitly for calls made outside a request context (the cambio_worker)."""
    sid = session_id if session_id is not None else current_session_id()
    if sid:
        return {"X-Operator-Session-Id": str(sid)[:128]}
    return {}
