"""SSO auth routes — mirrors rosetta.pdhc pattern."""
from __future__ import annotations

import uuid
import requests as http_requests
from flask import Blueprint, current_app, request, redirect, url_for, session, flash, render_template_string
from app.auth import initiate_sso_login, validate_sso_token, has_analysis_access, _upsert_local_user

bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.get("/login")
def login():
    if current_app.config.get("AUTH_MODE", "off") == "off":
        return redirect("/")
    next_url = request.args.get("next") or session.pop("sso_next", None) or "/"
    state = str(uuid.uuid4())
    session["sso_state"] = state
    session["sso_next"] = next_url
    return redirect(initiate_sso_login(next_url, state))


@bp.get("/callback")
def callback():
    if current_app.config.get("AUTH_MODE", "off") == "off":
        return redirect("/")
    token = request.args.get("token")
    state = request.args.get("state")
    if not token:
        flash("No token received from SSO.", "danger")
        return redirect(url_for("auth.login"))
    expected = session.pop("sso_state", None)
    if state != expected:
        flash("Invalid state.", "danger")
        return redirect(url_for("auth.login"))
    blob = validate_sso_token(token)
    if not blob:
        flash("SSO token validation failed.", "danger")
        return redirect(url_for("auth.login"))
    if not has_analysis_access(blob):
        flash("Access denied — analysis phase required.", "danger")
        return redirect(url_for("auth.login"))
    _upsert_local_user(blob)
    session["sso_token"] = token
    session["access_blob"] = blob
    session.permanent = True
    next_url = session.pop("sso_next", "/")
    return redirect(next_url)


@bp.get("/logout")
def logout():
    token = session.get("sso_token")
    base = (current_app.config.get("SSO_BASE_URL") or "").rstrip("/")
    if token and current_app.config.get("AUTH_MODE", "off") == "sso" and base:
        try:
            http_requests.post(f"{base}/api/auth/logout",
                               headers={"Authorization": f"Bearer {token}"}, timeout=5)
        except Exception:
            pass
    session.clear()
    return redirect(url_for("auth.logged_out"))


@bp.get("/logged-out")
def logged_out():
    base = (current_app.config.get("SSO_BASE_URL") or "").rstrip("/")
    sso_logout_url = f"{base}/logout" if base else ""
    return render_template_string(LOGGED_OUT_PAGE, sso_logout_url=sso_logout_url)


LOGGED_OUT_PAGE = """\
<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Logged out — cdr.pdhc</title>
<style>body{font-family:system-ui,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#f5f5f5}
.box{text-align:center;background:white;padding:2rem 3rem;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1)}a{color:#2563eb;font-weight:600}</style>
</head><body><div class="box"><h2>Logged out</h2><p><a href="/">Log in again</a></p></div>
{% if sso_logout_url %}<iframe name="f" style="display:none"></iframe>
<form id="s" method="post" action="{{ sso_logout_url }}" target="f"></form>
<script>document.getElementById('s').submit()</script>{% endif %}
</body></html>"""
