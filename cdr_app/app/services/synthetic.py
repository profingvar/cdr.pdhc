"""Is this row synthetic? One rule, used by every place that could let it
leave the platform (#1000).

Synthetic data is labelled with real concepts and looks like real care. Once
it reaches an external party (Cambio) it becomes that party's record and
cannot be taken back from here, so the Cambio queue must never carry it.

A row is synthetic when its writer is a generator, or when it carries the id
of a generation run. Either is sufficient: a cohort promoted by cdr-transfer
keeps its sim_run_id (#821) even if the hop that wrote it was not sim.
"""

#: Services whose every row is generated, never observed.
SYNTHETIC_SOURCES = frozenset({"sim.pdhc"})


def synthetic_reason(*, source_service=None, context_source=None, sim_run_id=None):
    """None for real data; otherwise the reason, in words, for the audit."""
    for src in (source_service, context_source):
        if src and src in SYNTHETIC_SOURCES:
            return f"synthetic: source {src} (#1000)"
    if sim_run_id:
        return f"synthetic: sim_run_id {sim_run_id} (#1000)"
    return None
