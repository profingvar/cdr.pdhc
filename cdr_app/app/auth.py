"""Auth + phase gating for cdr.pdhc.

AUTH_MODE=off  → dev SU user.
AUTH_MODE=sso  → OAuth against sso.pdhc (admin-only, analysis phase).
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Optional

import click
import requests
from flask import current_app, g, request, session, redirect, url_for, abort

from app import db
from app.models import User


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


_DEV_BLOB = {
    "user_guid": "00000000-0000-0000-0000-000000000000",
    "email": "dev@local",
    "display_name": "Dev SU",
    "user_type": "professional",
    "is_su_admin": True,
    "effective_phases": ["analysis"],
    "organization_ids": [],
}


def _blob_to_user(blob: dict) -> SimpleNamespace:
    return SimpleNamespace(
        guid=blob.get("user_guid"),
        username=blob.get("email") or blob.get("user_guid"),
        is_admin=bool(blob.get("is_su_admin")),
        is_su=bool(blob.get("is_su_admin")),
        org_ids=list(blob.get("organization_ids") or []),
        blob=blob,
    )


def has_analysis_access(blob: Optional[dict]) -> bool:
    if not blob:
        return False
    if blob.get("is_su_admin"):
        return True
    return (
        blob.get("user_type") == "professional"
        and "analysis" in (blob.get("effective_phases") or [])
    )


def validate_sso_token(token: str) -> Optional[dict]:
    base = current_app.config.get("SSO_BASE_URL", "").rstrip("/")
    cid = current_app.config.get("SSO_CLIENT_ID", "")
    sec = current_app.config.get("SSO_CLIENT_SECRET", "")
    if not (base and cid and sec):
        return None
    try:
        r = requests.get(
            f"{base}/api/auth/me/service",
            headers={
                "Authorization": f"Bearer {token}",
                "X-SSO-Client-Id": cid,
                "X-SSO-Client-Secret": sec,
            },
            timeout=10,
        )
        return r.json() if r.status_code == 200 else None
    except requests.RequestException:
        return None


def initiate_sso_login(next_url: str, state: str) -> str:
    base = current_app.config.get("SSO_BASE_URL", "").rstrip("/")
    cb = current_app.config.get("SSO_CALLBACK_URL", "")
    return f"{base}/login?next={cb}&state={state}"


def _upsert_local_user(blob: dict) -> None:
    guid = blob.get("user_guid")
    if not guid:
        return
    u = User.query.filter_by(guid=guid).first()
    if not u:
        u = User(guid=guid, username=blob.get("email") or guid,
                 is_admin=bool(blob.get("is_su_admin")), is_su=bool(blob.get("is_su_admin")))
        db.session.add(u)
        db.session.commit()


def _public_path(path: str) -> bool:
    return (
        path.startswith("/auth/")
        or path == "/healthz"
        or path.startswith("/api/v1/health")
        or path.startswith("/api/v1/ingest")
        # /api/v1/observations/<guid>/provenance (per-point lookup,
        # ticket #288). The route handler enforces @require_service_key
        # itself. The bare /api/v1/observations search endpoint moved
        # to dashboard.pdhc in #291.
        or path.startswith("/api/v1/observations")
        or path.startswith("/static/")
    )


# Service-key auth: trusted sibling services may write FHIR via
# /api/v1/fhir/* and read via /api/v1/canonical/* without an SSO
# session. Header pair: X-Source-Service + X-Service-Key.
KNOWN_FHIR_SERVICES = {
    "sim.pdhc":       "SIM_PDHC_SERVICE_KEY",
    "dashboard.pdhc": "DASHBOARD_PDHC_SERVICE_KEY",
}


def _service_key_outcome(app):
    """None = no headers (fall through), True = valid, False = bad."""
    source = request.headers.get("X-Source-Service", "").strip()
    key = request.headers.get("X-Service-Key", "").strip()
    if not source and not key:
        return None
    if not source or not key:
        return False
    cfg_var = KNOWN_FHIR_SERVICES.get(source)
    if not cfg_var:
        return False
    expected = app.config.get(cfg_var, "")
    if not expected or key != expected:
        return False
    g.source_service = source
    return True


def _service_blob(source_service: str) -> dict:
    """Synthetic access blob for service-key auth path. Marks the
    request as SU-equivalent (organisation-blind) so Rule-24 org
    scoping does not block sim's writes — tagged so downstream code can
    distinguish service writes from human users."""
    return {
        "user_guid": f"00000000-0000-0000-0000-service-{source_service[:8]}",
        "email": f"service:{source_service}",
        "display_name": f"service:{source_service}",
        "user_type": "service",
        "is_su_admin": True,
        "effective_phases": ["analysis", "ingestion"],
        "organization_ids": [],
        "service_source": source_service,
    }


def install_request_loader(app):
    @app.before_request
    def _loader():
        if _public_path(request.path):
            return None
        sk = _service_key_outcome(app)
        if sk is True:
            blob = _service_blob(g.source_service)
            g.access_blob = blob
            g.current_user = _blob_to_user(blob)
            return None
        if sk is False:
            from flask import jsonify
            return jsonify({"error": "Invalid service credentials"}), 403
        mode = app.config.get("AUTH_MODE", "off")
        if mode == "off":
            g.access_blob = _DEV_BLOB
            g.current_user = _blob_to_user(_DEV_BLOB)
            return None
        token = session.get("sso_token")
        if not token:
            session["sso_next"] = request.url
            return redirect(url_for("auth.login"))
        blob = validate_sso_token(token)
        if not blob:
            session.clear()
            session["sso_next"] = request.url
            return redirect(url_for("auth.login"))
        session["access_blob"] = blob
        # Ticket #52 / SSO #43: forced password reset — bounce to SSO's
        # change-password page until SSO clears the flag on the next blob.
        if blob.get("must_change_password"):
            base = app.config.get("SSO_BASE_URL", "").rstrip("/")
            return redirect(f"{base}/change-password")
        if not has_analysis_access(blob):
            abort(403)
        g.access_blob = blob
        g.current_user = _blob_to_user(blob)
        return None


def register_cli(app):
    @app.cli.command("create-su")
    @click.option("--username", required=True)
    @click.option("--password", required=True)
    def create_su(username, password):
        existing = User.query.filter_by(username=username).first()
        if existing:
            existing.is_su = True
            existing.is_admin = True
            existing.password_hash = _hash(password)
        else:
            db.session.add(User(username=username, password_hash=_hash(password), is_su=True, is_admin=True))
        db.session.commit()
        click.echo(f"SU {username} ready")
