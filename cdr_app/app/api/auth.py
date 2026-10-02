"""Service key authentication for inbound requests from gateways."""
import functools
import logging
from flask import request, jsonify, current_app, g

logger = logging.getLogger(__name__)

# INGEST (write) allowlist. Each entry names the config key holding that
# caller's own service key.
#
# `2gate.pdhc` was removed by #732. It was a *planned* second gateway that was
# never built — `progress.md` 6.b is still an unchecked box and readme.md says
# "Later 2gateway will be added" — yet it sat on the WRITE path of five
# production CDRs. It was never exploitable, but only because
# TWOGATE_PDHC_SERVICE_KEY was empty in every instance and the check below
# refuses on `not expected`. That is protection by accident: populating the
# variable from an .env template or an old backup would have made a write
# identity live with no owner and nothing to rotate. If a second gateway is
# ever built, add it then, with its own key, under its own name.
KNOWN_SERVICES = {
    "gateway.pdhc": "GATEWAY_PDHC_SERVICE_KEY",
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
        if not expected:
            # #732: an allowlisted caller whose key is UNSET is refused, which
            # is right — but it used to be indistinguishable from a wrong key,
            # so an entry that could never authenticate looked exactly like one
            # that simply had not been used. That is how `2gate.pdhc` sat on
            # this path unnoticed. Say it out loud instead.
            logger.warning(
                "service-key refused: %s is allowlisted but %s is unset in this "
                "instance's config — the entry can never authenticate, so it is "
                "either misconfigured or should be removed (#732)",
                source, config_key,
            )
            return jsonify({"error": "invalid service key"}), 403
        if key != expected:
            return jsonify({"error": "invalid service key"}), 403

        g.source_service = source
        return f(*args, **kwargs)
    return wrapper
