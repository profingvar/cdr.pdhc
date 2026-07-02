"""API blueprints for cdr.pdhc."""
from .health import bp as health_bp
from .ingest import bp as ingest_bp
from .fhir_api import bp as fhir_bp
from .fhir_write import bp as fhir_write_bp
from .fhir_read import bp as fhir_read_bp
from .openehr_api import bp as openehr_bp
from .cambio_api import bp as cambio_bp
from .provenance import bp as provenance_bp
# Ticket #292 removed `/api/v1/stats` from cdr1 as part of the
# analyse-split; the plan intent was for the analyse layer to own
# aggregation. But dashboard.pdhc's analyse module still fanouts to
# `/api/v1/stats` on every CDR in the registry (cdr2-5 kept it) —
# every fanout to cdr1 was 404'ing. Restored here as a thin
# per-type row-count aggregate matching what cdr2-5 return, so the
# federation shape is uniform. The analyse layer can migrate to
# doing its own aggregation later; this restoration is a soft
# compatibility shim for the analyse-split rollout.
from .stats import bp as stats_bp

__all__ = [
    "health_bp", "ingest_bp", "fhir_bp", "fhir_write_bp", "fhir_read_bp",
    "openehr_bp", "cambio_bp", "provenance_bp", "stats_bp",
]
