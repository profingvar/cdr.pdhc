"""#443 (X1): cdr_read_audit — read-side audit table for cdr1..5.

Additive table only; nothing existing is touched.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
"""
import sqlalchemy as sa
from alembic import op

revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cdr_read_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("caller_service", sa.String(64), nullable=True),
        sa.Column("caller_user_guid", sa.String(36), nullable=True),
        sa.Column("caller_org_guids", sa.JSON(), nullable=True),
        sa.Column("route", sa.String(200), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("patient_guid", sa.String(64), nullable=True),
        sa.Column("n_rows_returned", sa.Integer(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("role_guid", sa.String(64), nullable=True),
        sa.Column("purpose", sa.String(32), nullable=True),
        sa.Column("access_basis", sa.String(32), nullable=True),
    )
    op.create_index("ix_cdr_read_audit_timestamp", "cdr_read_audit", ["timestamp"])
    op.create_index("ix_cdr_read_audit_patient", "cdr_read_audit", ["patient_guid"])
    op.create_index("ix_cdr_read_audit_session", "cdr_read_audit", ["session_id"])
    op.create_index("ix_cdr_read_audit_caller", "cdr_read_audit", ["caller_service"])


def downgrade():
    op.drop_table("cdr_read_audit")
