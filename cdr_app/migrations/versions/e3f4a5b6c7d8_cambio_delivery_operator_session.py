"""#423 (X2): add operator_session_id to cambio_delivery_log.

Nullable String(128). Captured from the inbound ingest request's
X-Operator-Session-Id header (forwarded by gateway) and replayed by the
cambio_worker on the cdr1 -> Cambio hop, so the operator chain-of-custody
survives the async queue gap. Additive + nullable = safe on prod.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'e3f4a5b6c7d8'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cambio_delivery_log') as batch_op:
        batch_op.add_column(sa.Column('operator_session_id', sa.String(length=128), nullable=True))


def downgrade():
    with op.batch_alter_table('cambio_delivery_log') as batch_op:
        batch_op.drop_column('operator_session_id')
