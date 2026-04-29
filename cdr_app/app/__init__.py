"""cdr.pdhc — Clinical Data Repository for the PDHC platform."""
import os
import logging
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()

logger = logging.getLogger(__name__)


def create_app(config_override=None):
    app = Flask(__name__)

    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "postgresql://cdr_user:cdr_pass@localhost:9047/cdr_db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret")

    # Cambio sandbox config
    app.config["CAMBIO_DELIVERY_ENABLED"] = os.environ.get("CAMBIO_DELIVERY_ENABLED", "false").lower() == "true"
    app.config["CAMBIO_CLIENT_ID"] = os.environ.get("CAMBIO_CLIENT_ID", "")
    app.config["CAMBIO_CLIENT_SECRET"] = os.environ.get("CAMBIO_CLIENT_SECRET", "")
    app.config["CAMBIO_TOKEN_URL"] = os.environ.get("CAMBIO_TOKEN_URL", "")
    app.config["CAMBIO_BASE_URL"] = os.environ.get("CAMBIO_BASE_URL", "")
    app.config["CAMBIO_TENANT"] = os.environ.get("CAMBIO_TENANT", "sandbox-dev")
    app.config["CAMBIO_COMMISSION_HSA_ID"] = os.environ.get("CAMBIO_COMMISSION_HSA_ID", "")
    app.config["CAMBIO_HEALTHCARE_UNIT_HSA_ID"] = os.environ.get("CAMBIO_HEALTHCARE_UNIT_HSA_ID", "")
    app.config["CAMBIO_HEALTHCARE_PROVIDER_HSA_ID"] = os.environ.get("CAMBIO_HEALTHCARE_PROVIDER_HSA_ID", "")
    app.config["CAMBIO_ORG_ID"] = os.environ.get("CAMBIO_ORG_ID", "")

    # SSO config
    app.config["AUTH_MODE"] = os.environ.get("AUTH_MODE", "off")
    app.config["SSO_BASE_URL"] = os.environ.get("SSO_BASE_URL", "")
    app.config["SSO_CLIENT_ID"] = os.environ.get("SSO_CLIENT_ID", "")
    app.config["SSO_CLIENT_SECRET"] = os.environ.get("SSO_CLIENT_SECRET", "")
    app.config["SSO_CALLBACK_URL"] = os.environ.get("SSO_CALLBACK_URL", "")

    # Service keys for inbound from gateways
    app.config["GATEWAY_PDHC_SERVICE_KEY"] = os.environ.get("GATEWAY_PDHC_SERVICE_KEY", "")
    app.config["TWOGATE_PDHC_SERVICE_KEY"] = os.environ.get("TWOGATE_PDHC_SERVICE_KEY", "")
    app.config["SIM_PDHC_SERVICE_KEY"] = os.environ.get("SIM_PDHC_SERVICE_KEY", "")
    app.config["DASHBOARD_PDHC_SERVICE_KEY"] = os.environ.get("DASHBOARD_PDHC_SERVICE_KEY", "")

    # Canonicalisation: when sim emits FHIR resources whose
    # coding[0].system = https://plan.pdhc.se/Concept, the canonicaliser
    # short-circuits the xlate hop and trusts plan.pdhc as the authority.
    # STRICT_CANONICALISATION=false additionally relaxes the plan-validate
    # step on transient unreachability so seeding doesn't deadlock if
    # plan.pdhc is briefly unreachable.
    app.config["PLAN_BASE_URL"] = os.environ.get("PLAN_BASE_URL", "")
    app.config["STRICT_CANONICALISATION"] = (
        os.environ.get("STRICT_CANONICALISATION", "true").lower() in ("true", "1", "yes")
    )

    if config_override:
        app.config.update(config_override)

    db.init_app(app)
    migrate.init_app(app, db)

    from .auth import install_request_loader, register_cli
    install_request_loader(app)
    register_cli(app)

    from .routes.auth import bp as auth_bp
    from .routes.views import bp as views_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)

    from .api import (
        health_bp, ingest_bp, fhir_bp, fhir_write_bp, fhir_read_bp,
        openehr_bp, canonical_bp, cambio_bp, stats_bp,
    )
    app.register_blueprint(health_bp)
    app.register_blueprint(ingest_bp, url_prefix="/api/v1")
    app.register_blueprint(fhir_bp, url_prefix="/api/v1/fhir")
    app.register_blueprint(fhir_write_bp, url_prefix="/api/v1/fhir")
    app.register_blueprint(fhir_read_bp, url_prefix="/api/v1/fhir")
    app.register_blueprint(openehr_bp, url_prefix="/api/v1/openehr")
    app.register_blueprint(canonical_bp, url_prefix="/api/v1/canonical")
    app.register_blueprint(cambio_bp, url_prefix="/api/v1/cambio")
    app.register_blueprint(stats_bp, url_prefix="/api/v1")

    # Start Cambio delivery scheduler if enabled
    if app.config["CAMBIO_DELIVERY_ENABLED"] and not app.config.get("TESTING"):
        _start_delivery_scheduler(app)

    return app


def _start_delivery_scheduler(app):
    """Start background APScheduler for Cambio delivery cycles."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from app.services.cambio_worker import run_delivery_cycle

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            run_delivery_cycle,
            "interval",
            seconds=60,
            args=[app],
            id="cambio_delivery",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("Cambio delivery scheduler started (60s interval)")
    except Exception as e:
        logger.warning("Could not start Cambio delivery scheduler: %s", e)
