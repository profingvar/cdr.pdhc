"""#718 — index observation.effective_at for the unfiltered ordering.

Every FHIR search ends in ``ORDER BY effective_at DESC NULLS LAST LIMIT n``
(api/fhir_read.py). The existing composite
``(patient_guid, org_guid, code_canonical, effective_at)`` cannot serve that
when no patient is given, because the leading columns are unconstrained — so
Postgres seq-scanned the table and sorted it.

Measured on cdr2, 2.6M rows, with the real call analyse makes when no patient
is selected (``_count=10000``, unfiltered):

    before:  23,498 ms, external merge sort spilling ~1.5 GB PER WORKER
             (~4.4 GB of temporary writes for one query)
    after:        118 ms, Index Scan, no sort at all

Five CDRs are queried in parallel, so a single unfiltered analyse page could
write ~22 GB of temp files on a host with 47 GB free. 2026-05-22 is the
precedent for what filling that disk does: it corrupted Colima's containerd.

cdr_6 already had an effective_at index and answered the same query in 6 ms,
which is what pointed at the cause.

Plain CREATE INDEX rather than CONCURRENTLY on purpose: alembic runs a
migration inside a transaction and CONCURRENTLY cannot. On the five live CDRs
the index was already built concurrently by hand, so IF NOT EXISTS makes this
a no-op there; anywhere else the table is new and empty, where a plain build
is instant.
"""
from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

INDEX = "ix_observation_effective_at_desc"


def upgrade():
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX} "
        f"ON observation (effective_at DESC NULLS LAST)")


def downgrade():
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
