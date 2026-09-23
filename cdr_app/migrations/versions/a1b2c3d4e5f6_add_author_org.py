"""#665: add clinical_context.author_org_guid — who CREATED an observation.

The platform had no field for the organisation that AUTHORED an observation,
only for who ORDERED it (requesting_org_guid) and who SUBMITTED it
(provider_org_guid). Those are three different parties, and conflating author
with submitter made the analyse.pdhc node policy "may rows authored by other
organisations stored in this CDR be used" unimplementable.

NO BACKFILL, deliberately. Existing rows keep NULL.

The tempting backfill is author_org_guid = provider_org_guid, on the reasoning
that today's submitter usually is the author. It is rejected: once written,
a guess is indistinguishable from a value the submitter actually declared, and
a consumer reading it cannot tell which it has. NULL means unknown and stays
honest. A consumer that wants a fallback can COALESCE at read time, where the
choice is visible.

Note for the operator: this runs against every CDR instance (cdr.pdhc,
cdr1-5, cdr_6). It is additive and nullable, so it is safe to apply
instance by instance; nothing reads the column until #666 lands on
gateway.pdhc and starts populating it.

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "clinical_context",
        sa.Column("author_org_guid", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_clinical_context_author_org_guid",
        "clinical_context",
        ["author_org_guid"],
    )


def downgrade():
    op.drop_index("ix_clinical_context_author_org_guid",
                  table_name="clinical_context")
    op.drop_column("clinical_context", "author_org_guid")
