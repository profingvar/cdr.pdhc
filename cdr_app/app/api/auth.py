"""Service key authentication for inbound requests from gateways."""
import functools
import logging
from flask import request, jsonify, current_app, g

logger = logging.getLogger(__name__)

KNOWN_SERVICES = {
    "gateway.pdhc": "GATEWAY_PDHC_SERVICE_KEY",
    "2gate.pdhc": "TWOGATE_PDHC_SERVICE_KEY",
    # sim.pdhc writes synthetic cohorts to cdr_6 (REFINEMENT_PLAN v0.2,
    # ticket #178). Existing cdr1..5 instances reject sim.pdhc unless
    # their .env carries SIM_PDHC_SERVICE_KEY — additive, no behaviour
    # change for the production CDRs.
    "sim.pdhc": "SIM_PDHC_SERVICE_KEY",
}


def require_service_key(f):
    """Decorator: validate X-Service-Key + X-Source-Service headers."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        source = request.headers.get("X-Source-Service", "").strip()
        key = request.headers.get("X-Service-Key", "").strip()

        if not source or not key:
            return jsonify({"error": "missing X-Source-Service or X-Service-Key"}), 401

        config_key = KNOWN_SERVICES.get(source)
        if not config_key:
            return jsonify({"error": f"unknown source service: {source}"}), 403

        expected = current_app.config.get(config_key, "")
        if not expected or key != expected:
            return jsonify({"error": "invalid service key"}), 403

        g.source_service = source
        return f(*args, **kwargs)
    return wrapper
