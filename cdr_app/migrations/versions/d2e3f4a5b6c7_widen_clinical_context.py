"""#302 phase 3: widen ClinicalContext to the canonical 12-field set.

Renames:
  - careplan_guid    -> care_plan_guid
  - plandef_guid     -> plan_definition_guid

Adds:
  - service_request_guid
  - concept_guid
  - contract_guid
  - requesting_org_guid
  - provider_org_guid
  - requester_user_guid
  - received_at

The `clinical_context` table is small (one row per ingest, ~7060 rows
in prod), so the migration backfills the new columns from each row's
linked FhirResource.resource_json (gateway's builder writes back-refs
to ServiceRequest, PlanDefinition, performer org, contract extension).

Backfill is best-effort: rows whose FhirResource is missing or where
the JSON lacks a field stay NULL. The `flask backfill-clinical-context`
CLI (registered in app/cli.py) can be re-run idempotently to fill in
late arrivals.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-06-28
"""
from alembic import op
import sqlalchemy as sa


revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "clinical_context" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("clinical_context")}

    if "careplan_guid" in cols and "care_plan_guid" not in cols:
        op.alter_column("clinical_context", "careplan_guid",
                         new_column_name="care_plan_guid")
    if "plandef_guid" in cols and "plan_definition_guid" not in cols:
        op.alter_column("clinical_context", "plandef_guid",
                         new_column_name="plan_definition_guid")

    new_cols = [
        ("service_request_guid", sa.String(36)),
        ("concept_guid",         sa.String(36)),
        ("contract_guid",        sa.String(36)),
        ("requesting_org_guid",  sa.String(36)),
        ("provider_org_guid",    sa.String(36)),
        ("requester_user_guid",  sa.String(36)),
        ("received_at",          sa.DateTime(timezone=True)),
    ]
    cols_after = {c["name"] for c in inspector.get_columns("clinical_context")}
    for name, type_ in new_cols:
        if name in cols_after:
            continue
        op.add_column("clinical_context",
                       sa.Column(name, type_, nullable=True))

    # Indexes
    existing_idx = {i["name"] for i in inspector.get_indexes("clinical_context")}
    if "ix_cc_service_request_guid" not in existing_idx:
        op.create_index("ix_cc_service_request_guid", "clinical_context",
                         ["service_request_guid"])
    if "ix_cc_concept_guid" not in existing_idx:
        op.create_index("ix_cc_concept_guid", "clinical_context",
                         ["concept_guid"])
    if "ix_cc_provider_org_guid" not in existing_idx:
        op.create_index("ix_cc_provider_org_guid", "clinical_context",
                         ["provider_org_guid"])

    # Backfill from FhirResource.resource_json — best-effort.
    #
    # The shape gateway's builder writes (see
    # gateway.pdhc/gateway_app/app/services/fhir_observation_builder.py):
    #   basedOn[].reference        -> ServiceRequest/<guid>, PlanDefinition/<guid>
    #   performer[].identifier.value -> provider org guid
    #   extension[url=…contract] / .url=…requesting-org / .url=…concept
    op.execute(r"""
        UPDATE clinical_context cc SET
            service_request_guid = COALESCE(cc.service_request_guid, sub.sr_guid),
            plan_definition_guid = COALESCE(cc.plan_definition_guid, sub.pd_guid),
            provider_org_guid    = COALESCE(cc.provider_org_guid,    sub.perf_guid),
            contract_guid        = COALESCE(cc.contract_guid,        sub.contract_guid),
            requesting_org_guid  = COALESCE(cc.requesting_org_guid,  sub.req_org_guid),
            concept_guid         = COALESCE(cc.concept_guid,         sub.concept_guid),
            received_at          = COALESCE(cc.received_at,          ir.received_at)
        FROM (
            SELECT
                fr.ingest_raw_guid AS irg,
                substring(b->>'reference' from '^ServiceRequest/(.+)$')   AS sr_guid,
                substring(b2->>'reference' from '^PlanDefinition/(.+)$')  AS pd_guid,
                p->'identifier'->>'value' AS perf_guid,
                ext_contract.value->>'valueReference' AS contract_guid,
                ext_req.value->>'valueReference'      AS req_org_guid,
                ext_concept.value->>'valueReference'  AS concept_guid
            FROM fhir_resources fr
            LEFT JOIN LATERAL jsonb_array_elements(
                COALESCE((fr.resource_json::jsonb)->'basedOn', '[]'::jsonb)
            ) b ON TRUE
            LEFT JOIN LATERAL jsonb_array_elements(
                COALESCE((fr.resource_json::jsonb)->'basedOn', '[]'::jsonb)
            ) b2 ON TRUE
            LEFT JOIN LATERAL jsonb_array_elements(
                COALESCE((fr.resource_json::jsonb)->'performer', '[]'::jsonb)
            ) p ON TRUE
            LEFT JOIN LATERAL jsonb_array_elements(
                COALESCE((fr.resource_json::jsonb)->'extension', '[]'::jsonb)
            ) ext_contract ON ext_contract->>'url' LIKE '%%/contract'
            LEFT JOIN LATERAL jsonb_array_elements(
                COALESCE((fr.resource_json::jsonb)->'extension', '[]'::jsonb)
            ) ext_req ON ext_req->>'url' LIKE '%%/requesting-org'
            LEFT JOIN LATERAL jsonb_array_elements(
                COALESCE((fr.resource_json::jsonb)->'extension', '[]'::jsonb)
            ) ext_concept ON ext_concept->>'url' LIKE '%%/concept'
            WHERE fr.resource_type = 'Observation'
        ) sub
        LEFT JOIN ingest_raw ir ON ir.guid = sub.irg
        WHERE cc.ingest_raw_guid = sub.irg
    """)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "clinical_context" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("clinical_context")}
    idx = {i["name"] for i in inspector.get_indexes("clinical_context")}

    for name in ("ix_cc_service_request_guid",
                 "ix_cc_concept_guid",
                 "ix_cc_provider_org_guid"):
        if name in idx:
            op.drop_index(name, table_name="clinical_context")

    for name in ("service_request_guid", "concept_guid", "contract_guid",
                 "requesting_org_guid", "provider_org_guid",
                 "requester_user_guid", "received_at"):
        if name in cols:
            op.drop_column("clinical_context", name)

    if "care_plan_guid" in cols and "careplan_guid" not in cols:
        op.alter_column("clinical_context", "care_plan_guid",
                         new_column_name="careplan_guid")
    if "plan_definition_guid" in cols and "plandef_guid" not in cols:
        op.alter_column("clinical_context", "plan_definition_guid",
                         new_column_name="plandef_guid")
