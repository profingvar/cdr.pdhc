"""Dashboard stats API."""
import re
from flask import Blueprint, current_app, jsonify, request
from app import db
from app.models import IngestRaw, FhirResource, OpenEhrComposition, HealthObservation, DedupeRegistry
from app.models.resources import RESOURCES, live_model

bp = Blueprint("stats", __name__)

# ---------------------------------------------------------------------------
# Concept helpers — deliberately degradable (#733)
# ---------------------------------------------------------------------------

_CANON_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _canon_guid_fallback(code: str | None) -> str | None:
    """Mirror of ``clinical_read._concept_guid_from_canonical`` for CDRs that
    do not carry the clinical read surface. Kept byte-identical in behaviour,
    and a test asserts the two agree wherever both exist."""
    if not code:
        return None
    if code.startswith("urn:pdhc:concept/") or "/api/v1/concepts/" in code:
        tail = code.rsplit("/", 1)[-1]
        return tail if _CANON_GUID_RE.match(tail) else None
    return None


def _concept_helpers():
    """``(canon_guid, resolve_display)`` for this deployment.

    cdr2-cdr5 were deployed WITHOUT ``app/api/clinical_read.py`` — they are a
    pure analysis repository with no clinical read surface — so a module-level
    import of it made the concept inventory a 500 on exactly the CDRs holding
    the most data (2.6M observations each). The guid parse is pure and always
    available; the display lookup needs plan.pdhc and is cosmetic, so where
    the module is absent the inventory still answers, with no display.
    """
    try:
        from app.api.clinical_read import (
            resolve_display, _concept_guid_from_canonical)
        return _concept_guid_from_canonical, resolve_display
    except ImportError:
        return _canon_guid_fallback, lambda _canon: None


# A code-system URI with a code at the tail — the shape termbank publishes,
# e.g. https://termbank.pdhc.se/CodeSystem/loinc/41653-7.
_CODESYSTEM_RE = re.compile(
    r"^(?P<system>https?://[^\s]+?/CodeSystem/(?P<cs>[^/\s]+))/"
    r"(?P<code>[^/\s]+)$")


def _classify(canon: str | None, canon_guid) -> dict:
    """How this code is BOUND, which is what decides whether a researcher can
    ask about it (#733, same vocabulary as #751).

    The first version of this had two buckets, resolvable and not, where
    "resolvable" meant "is a plan.pdhc concept GUID". That is cdr1's shape and
    not the platform's: cdr2 and cdr3 store termbank CodeSystem URIs — LOINC
    codes — for all 2.6M of their observations. Calling those "can never
    resolve" put every concept those two CDRs hold into the unresolved list,
    which analyse does not merge, so the two largest sources read as empty.

    A LOINC code is a STRONGER binding than a local GUID, not a weaker one.
    Three kinds, and only the third is a defect:

    ``plan_concept``  Path-B ``urn:pdhc:concept/<guid>`` — display from plan.pdhc
    ``external_code`` a CodeSystem URI — already interoperable, names itself
    ``unbound``       neither — nothing can resolve it, nobody can ask for it
    """
    guid = canon_guid(canon)
    if guid:
        return {"binding": "plan_concept", "concept_guid": guid}
    m = _CODESYSTEM_RE.match(canon or "")
    if m:
        return {"binding": "external_code", "concept_guid": None,
                "code_system": m.group("system"),
                "code_system_name": m.group("cs"),
                "code": m.group("code")}
    return {"binding": "unbound", "concept_guid": None,
            "why_unresolved": ("neither a plan.pdhc concept GUID nor a "
                               "CodeSystem URI, so nothing can resolve it")}


@bp.get("/stats")
def stats():
    # Per-type FHIR live tables (Phase-1.2 ingest path).
    fhir_per_type = sum(live_model(name).query.count() for name, *_ in RESOURCES)
    # Patient count: prefer the per-type `patient` table; fall back to
    # legacy ingest_raw distinct-by-guid for older CDRs.
    patients = live_model("Patient").query.count() or (
        db.session.execute(
            db.text("SELECT COUNT(DISTINCT patient_guid) FROM ingest_raw")
        ).scalar() or 0
    )
    return jsonify({
        "ingest_raw": IngestRaw.query.count(),
        "fhir_resources": FhirResource.query.count() + fhir_per_type,
        "openehr_compositions": OpenEhrComposition.query.count(),
        "health_observations": HealthObservation.query.count(),
        "dedupe_registry": DedupeRegistry.query.count(),
        "patients": patients,
    }), 200


@bp.get("/recent-observations")
def recent_observations():
    """Latest canonical observations, newest first — the CDR's own
    monitoring feed (line data). Distinct from the clinical read surface
    (/api/v1/clinical/*): this is an operator view of what is arriving in
    this repository, ordered by ingest time. NOTE: it does NOT apply
    spärr/org scoping — it is an operator monitor gated by the CDR's own
    SSO login, not a care-delivery clinical read.
    """
    from app.api.clinical_read import resolve_display  # display via plan.pdhc
    limit = request.args.get("limit", 50, type=int) or 50
    limit = max(1, min(limit, 200))
    Obs = live_model("Observation")
    rows = (Obs.query
            .order_by(Obs.received_at.desc(), Obs.created_at.desc())
            .limit(limit).all())
    disp_cache = {}

    def _disp(code):
        if code not in disp_cache:
            disp_cache[code] = resolve_display(code)
        return disp_cache[code]

    items = []
    for o in rows:
        code = getattr(o, "code_canonical", None)
        vq = getattr(o, "value_quantity", None)
        value = (float(vq) if vq is not None
                 else getattr(o, "value_string", None)
                 or getattr(o, "value_code", None))
        recv = getattr(o, "received_at", None)
        eff = getattr(o, "effective_at", None)
        items.append({
            "received_at": recv.isoformat() if recv else None,
            "effective_at": eff.isoformat() if eff else None,
            "patient_guid": getattr(o, "patient_guid", None),
            "code": code,
            "display": _disp(code),
            "value": value,
            "unit": getattr(o, "value_unit", None),
            "source": getattr(o, "source", None),
            "org_guid": getattr(o, "org_guid", None),
        })
    return jsonify({"items": items, "count": len(items)}), 200


@bp.get("/concept-inventory")
def concept_inventory():
    """What concepts this CDR actually holds — #733.

    `/stats` says HOW MUCH data a CDR has. A researcher writing a cohort
    predicate needs to know WHAT they can ask about, and nothing exposed that:
    the analyse landing page could show counts per source and not one concept
    name. So this is the aggregate counterpart to /stats.

    Aggregate only. No patient guids, no values, no rows.

    ## The k-floor is not optional here

    A per-concept PATIENT COUNT is a disclosure surface in its own right. A
    concept held for two patients, named and counted, is a smaller crowd to
    hide in than any analysis result this platform will publish — so a count
    below ``CONCEPT_INVENTORY_K`` is reported as the SUPPRESSED sentinel rather
    than a number. Getting this wrong would make the inventory a nicer attack
    than the analyses it exists to support.

    Observation counts are NOT floored: they say how much data exists, not how
    many people it describes.

    ## Unresolvable canonicals are listed, not hidden

    A `code_canonical` whose tail is not a GUID can never resolve against
    plan.pdhc (#731 found four such in cdr1 and they have since been deleted).
    Hiding them would make this endpoint quietly disagree with `/stats`, so
    they are reported under ``unresolved`` with their counts.
    """
    from sqlalchemy import func, text

    canon_guid, resolve_display = _concept_helpers()
    k = int(current_app.config.get("CONCEPT_INVENTORY_K", 5))
    Obs = live_model("Observation")
    if Obs is None:
        return jsonify({"concepts": [], "unresolved": [], "distinct": 0,
                        "k_min": k, "error": "no Observation model"}), 200

    rows = (db.session.query(
                Obs.code_canonical.label("canon"),
                func.count().label("observations"),
                func.count(func.distinct(Obs.patient_guid)).label("patients"),
                func.min(Obs.effective_at).label("first_seen"),
                func.max(Obs.effective_at).label("last_seen"))
            .filter(Obs.code_canonical.isnot(None))
            .group_by(Obs.code_canonical)
            .all())

    # Units live in the FHIR body, not in a column, so they come from raw_json.
    #
    # The fast path is Postgres-only (`::jsonb #>>`). The fallback is not a
    # nicety: without it the unit feature is silently empty everywhere the fast
    # path does not run — including the SQLite test path, where the first
    # version of this swallowed the error and returned {} to a test that
    # believed it was checking the feature.
    units: dict[str, dict[str, int]] = {}

    def _add(canon, unit, n):
        if canon and unit:
            d = units.setdefault(canon, {})
            d[unit] = d.get(unit, 0) + int(n)

    table = Obs.__tablename__

    # Pass 1 — the value_unit COLUMN. Portable, and 2.8s on a 2.6M-row CDR
    # where a jsonb cast over every row costs 8s for the same answer. Reading
    # units from raw_json only, as the first version did, reported no unit for
    # anything on cdr2/cdr3: they fill the column and their bodies carry no
    # valueQuantity at all.
    null_unit_canons: set[str] = set()
    for canon, unit, n in db.session.execute(text(
            f"SELECT code_canonical, value_unit, count(*) FROM {table} "
            "WHERE code_canonical IS NOT NULL GROUP BY 1, 2")):
        if unit:
            _add(canon, unit, n)
        else:
            null_unit_canons.add(canon)

    # Pass 2 — raw_json, ONLY for the rows pass 1 could not answer. cdr1 has
    # ~7000 observations whose value_unit is NULL while the body carries the
    # unit, so the column alone hides the unit of its busiest concept. On
    # cdr2/cdr3 there are no such rows and this never runs.
    #
    # The two passes are independent on purpose: when they shared one try/except
    # a failure here re-ran pass 1's work inside the fallback and every unit was
    # counted twice.
    if null_unit_canons:
        try:
            for canon, unit, n in db.session.execute(text(
                    "SELECT code_canonical, raw_json::jsonb #>> "
                    "'{valueQuantity,unit}' AS unit, count(*) "
                    f"FROM {table} WHERE code_canonical IS NOT NULL "
                    "AND value_unit IS NULL GROUP BY 1, 2")):
                _add(canon, unit, n)
        except Exception:                   # noqa: BLE001 — SQLite has no
            db.session.rollback()           # jsonb cast, and without this the
            import json as _json            # unit feature was silently empty
            for canon, raw in db.session.query(   # everywhere the fast path
                    Obs.code_canonical, Obs.raw_json).filter(
                        Obs.code_canonical.isnot(None),
                        Obs.value_unit.is_(None)):
                try:
                    u = ((_json.loads(raw) or {}).get("valueQuantity")
                         or {}).get("unit")
                except Exception:           # noqa: BLE001 — a bad body is not
                    continue                # a reason to fail the inventory
                _add(canon, u, 1)

    concepts, unresolved = [], []
    for r in rows:
        entry = {
            "code_canonical": r.canon,
            "observations": int(r.observations),
            # k-floor — see the docstring.
            "patients": (int(r.patients) if int(r.patients) >= k else f"<{k}"),
            "units": units.get(r.canon) or {},
            "first_seen": r.first_seen.isoformat() if r.first_seen else None,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
        }
        entry.update(_classify(r.canon, canon_guid))
        if entry["binding"] == "unbound":
            unresolved.append(entry)
            continue
        if entry["binding"] == "plan_concept":
            entry["display"] = resolve_display(r.canon)   # cosmetic, fails open
        else:
            # An external code names itself; no sibling call needed.
            entry["display"] = f"{entry['code_system_name']} {entry['code']}"
        concepts.append(entry)

    concepts.sort(key=lambda c: -c["observations"])
    return jsonify({
        "concepts": concepts,
        "unresolved": unresolved,
        "distinct": len(concepts) + len(unresolved),
        "bindings": {b: sum(1 for c in concepts + unresolved
                            if c["binding"] == b)
                     for b in ("plan_concept", "external_code", "unbound")},
        "k_min": k,
    }), 200
