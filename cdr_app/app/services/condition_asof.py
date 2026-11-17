"""#984: a diagnosis AS IT STOOD on a date, from the CDR's version history.

This replaces icd11.pdhc's `state_at()`, now that the CDR is the one diagnosis
register (operator, 2026-11-17) and its history the one assertion log. The
question it answers -- "the same follow-up question gives the same answer
before and after a code change" -- needs the replay to be EXACTLY icd11's:

- ordered by (valid_from, version_id). `valid_from` is CLINICAL time: a
  clinician today can state that something became true last year, and a
  question about last year must see it. `version_id` is the order the
  statements were actually made in (icd11's `seq`);
- the last version with `valid_from <= as_of` wins; none -> not yet asserted;
- each version is returned as it was written, so display text and ICD-11
  release frozen at write time replay exactly.

`valid_from` per version: the extension below (icd11.pdhc writes it), else
`recordedDate`, else `onsetDateTime`. A version with none of the three is
treated as valid from the beginning and the answer says so
(`validFromSource: "none"`), never silently dated.
"""
from __future__ import annotations

from datetime import datetime, timezone

VALID_FROM_EXT = "https://cdr.pdhc.se/fhir/StructureDefinition/assertion-valid-from"
_MIN = datetime.min.replace(tzinfo=timezone.utc)


def parse_instant(raw: str) -> datetime:
    """ISO date or date-time -> aware datetime. A bare date is that day's END,
    so "as of 2025-06-01" includes what was valid during that day. A '+'
    sent unencoded arrives as ' ' (icd11 #131 note); that is repaired."""
    s = raw.strip().replace(" ", "+")
    if len(s) == 10:
        return datetime.fromisoformat(s).replace(hour=23, minute=59, second=59,
                                                 microsecond=999999, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("a date-time must carry its time zone")
    return dt


def valid_from(resource: dict) -> tuple[datetime, str]:
    for ext in resource.get("extension") or []:
        if ext.get("url") == VALID_FROM_EXT and ext.get("valueDateTime"):
            return parse_instant(ext["valueDateTime"]), "extension"
    for key in ("recordedDate", "onsetDateTime"):
        if resource.get(key):
            try:
                return parse_instant(resource[key]), key
            except ValueError:
                pass
    return _MIN, "none"


def state_at(versions: list[tuple[int, dict]], as_of: datetime) -> tuple[int, dict, str] | None:
    """versions: [(version_id, raw_json)] in any order. Returns
    (version_id, resource, valid_from_source) or None."""
    keyed = sorted(((valid_from(r), vid, r) for vid, r in versions), key=lambda x: (x[0][0], x[1]))
    best = None
    for (vf, src), vid, r in keyed:
        if vf <= as_of:
            best = (vid, r, src)
        else:
            break
    return best
