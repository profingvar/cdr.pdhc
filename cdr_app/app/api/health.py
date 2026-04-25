"""Health check endpoint."""
from flask import Blueprint, jsonify
from app import db

bp = Blueprint("health", __name__)


@bp.get("/healthz")
@bp.get("/api/v1/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    status = "ok" if db_ok else "degraded"
    # Field name `database` (string 'connected'|'unavailable') matches the
    # canonical shape services.html parses: `data.database === 'connected'`.
    # Previously emitted `db: <bool>` which services.html could not read.
    resp = jsonify({
        "status": status,
        "service": "cdr.pdhc",
        "database": "connected" if db_ok else "unavailable",
    })
    # CORS: let www.pdhc.se/services.html read the JSON body cross-origin so it
    # can drive real status/DB dots (ticket #70 / CLAUDE.md §10). Specific
    # origin + Vary: Origin (not "*") keeps future Allow-Credentials
    # spec-compliant.
    resp.headers["Access-Control-Allow-Origin"] = "https://www.pdhc.se"
    resp.headers["Access-Control-Allow-Methods"] = "GET"
    resp.headers["Vary"] = "Origin"
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200 if db_ok else 503
