"""#1014: where a datapoint came from — patient-generated or healthcare.

Patient-generated data (PGD) has a different legal status from data produced
or endorsed by healthcare, and the professional reading it must be able to
see which is which. A patient app with provider1's architecture is coming;
this is the field it will set.

OPT-IN (operator decision 2026-11-21). The only value is
`patient_generated`. NULL means produced by, or endorsed by, healthcare.

That is deliberately NOT the #665 rule ("NULL means unknown, never a
default"). For author_org_guid the operator did not decide what absence
means; here they did, so absence carries a meaning by policy. Every row
written before this field existed is healthcare data, which is true: no
patient app has ever submitted.

The weakness is recorded rather than hidden: an opt-in flag is DECLARED, so
nothing yet stops a patient app from omitting it. Once the patient app has
its own credential, gateway must SET the value from the authenticated
client, the way provider_org_guid comes from the PAT (ticket #1014, "known
gap").

Any other value is REFUSED rather than stored or dropped. A producer that
sends `"patient-generated"` or `"healthcare"` has misunderstood the
contract, and silently treating its rows as healthcare would be the worst
possible reading of a typo.
"""
from __future__ import annotations

PATIENT_GENERATED = "patient_generated"
VALUES = (PATIENT_GENERATED,)


def check(value) -> tuple[str | None, str | None]:
    """(stored_value, error). Absent or empty → (None, None): healthcare."""
    if value is None or value == "":
        return None, None
    if value in VALUES:
        return value, None
    return None, (f"invalid_data_origin: {value!r}; the only accepted value "
                  f"is {PATIENT_GENERATED!r} (omit it for healthcare data)")
