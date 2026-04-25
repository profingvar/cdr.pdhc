"""API blueprints for cdr.pdhc."""
from .health import bp as health_bp
from .ingest import bp as ingest_bp
from .fhir_api import bp as fhir_bp
from .fhir_write import bp as fhir_write_bp
from .fhir_read import bp as fhir_read_bp
from .openehr_api import bp as openehr_bp
from .canonical_api import bp as canonical_bp
from .cambio_api import bp as cambio_bp
from .stats import bp as stats_bp

__all__ = [
    "health_bp", "ingest_bp", "fhir_bp", "fhir_write_bp", "fhir_read_bp",
    "openehr_bp", "canonical_bp", "cambio_bp", "stats_bp",
]
