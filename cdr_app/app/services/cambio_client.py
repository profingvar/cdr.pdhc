"""Cambio CDR sandbox API client — token management + FHIR delivery.

openEHR (EHR creation + composition delivery) removed 2026-11-20 (#682):
rosetta.pdhc renders and delivers openEHR.
"""
import logging
import time
import requests
from datetime import datetime, timezone
from flask import current_app

logger = logging.getLogger(__name__)


class CambioClient:
    """Handles OAuth2 tokens and API calls to Cambio Platform Innovation sandbox."""

    _token = None
    _token_expires_at = 0

    @classmethod
    def _get_token(cls):
        """Obtain or reuse a cached OAuth2 client-credentials token."""
        now = time.time()
        if cls._token and now < cls._token_expires_at - 30:
            return cls._token

        token_url = current_app.config["CAMBIO_TOKEN_URL"]
        client_id = current_app.config["CAMBIO_CLIENT_ID"]
        client_secret = current_app.config["CAMBIO_CLIENT_SECRET"]

        if not all([token_url, client_id, client_secret]):
            raise RuntimeError("Cambio OAuth2 credentials not configured")

        # Request all needed audiences in one token
        audiences = [
            "service.fhir-gateway",
            "service.xcdr",
            "service.patient",
            "service.consent",
            "service.organization",
        ]

        resp = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "audience": " ".join(audiences),
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()

        cls._token = body["access_token"]
        cls._token_expires_at = now + body.get("expires_in", 300)
        logger.info("Cambio OAuth2 token acquired, expires in %ds", body.get("expires_in", 300))
        return cls._token

    @classmethod
    def _headers(cls, extra=None, operator_session_id=None):
        from app.services.session_headers import outbound_session_headers
        token = cls._get_token()
        h = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra:
            h.update(extra)
        # X2 (#423): replay the operator session on the cdr1 -> Cambio hop.
        # Pass operator_session_id explicitly from the worker (no request
        # context); it resolves from the live request otherwise.
        h.update(outbound_session_headers(operator_session_id))
        return h

    @classmethod
    def _base_url(cls):
        return current_app.config["CAMBIO_BASE_URL"].rstrip("/")

    @classmethod
    def _org_headers(cls):
        """Cambio requires HSA IDs as custom headers."""
        return {
            "CommissionHsaId": current_app.config["CAMBIO_COMMISSION_HSA_ID"],
            "HealthCareUnitHsaId": current_app.config["CAMBIO_HEALTHCARE_UNIT_HSA_ID"],
            "HealthCareProviderHsaId": current_app.config["CAMBIO_HEALTHCARE_PROVIDER_HSA_ID"],
            "OrganizationIdentifier": current_app.config["CAMBIO_ORG_ID"],
        }

    # ------------------------------------------------------------------
    # Patient operations
    # ------------------------------------------------------------------

    @classmethod
    def ensure_patient(cls, pdhc_patient_guid):
        """Create patient in Cambio if not already mapped. Returns cambio_patient_id."""
        from app import db
        from app.models import CambioPatientMap

        mapping = CambioPatientMap.query.filter_by(pdhc_patient_guid=pdhc_patient_guid).first()
        if mapping and mapping.cambio_patient_id:
            return mapping.cambio_patient_id

        # Create patient via FHIR Gateway
        patient_resource = {
            "resourceType": "Patient",
            "identifier": [{
                "system": "urn:pdhc:patient-guid",
                "value": pdhc_patient_guid,
            }],
            "active": True,
        }

        base = cls._base_url()
        resp = requests.post(
            f"{base}/service.fhir-gateway/fhir/Patient",
            json=patient_resource,
            headers=cls._headers(cls._org_headers()),
            timeout=15,
        )
        resp.raise_for_status()
        created = resp.json()
        cambio_patient_id = created.get("id")

        # Store mapping
        if mapping:
            mapping.cambio_patient_id = cambio_patient_id
        else:
            db.session.add(CambioPatientMap(
                pdhc_patient_guid=pdhc_patient_guid,
                cambio_patient_id=cambio_patient_id,
            ))
        db.session.commit()

        logger.info(
            "Cambio patient created: pdhc=%s cambio=%s",
            pdhc_patient_guid, cambio_patient_id,
        )
        return cambio_patient_id

    # ------------------------------------------------------------------
    # FHIR delivery
    # ------------------------------------------------------------------

    @classmethod
    def deliver_fhir_observation(cls, fhir_json, cambio_patient_id, operator_session_id=None):
        """POST a FHIR Observation to the Cambio FHIR Gateway. Returns cambio resource id."""
        base = cls._base_url()
        obs = dict(fhir_json)
        obs["subject"] = {"reference": f"Patient/{cambio_patient_id}"}

        resp = requests.post(
            f"{base}/service.fhir-gateway/fhir/Observation",
            json=obs,
            headers=cls._headers(cls._org_headers(), operator_session_id=operator_session_id),
            timeout=15,
        )
        resp.raise_for_status()
        created = resp.json()
        return created.get("id")
