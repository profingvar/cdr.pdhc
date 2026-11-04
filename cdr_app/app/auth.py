"""Auth + phase gating for cdr.pdhc.

AUTH_MODE=off  → dev SU user.
AUTH_MODE=sso  → OAuth against sso.pdhc (admin-only, analysis phase).
"""
from __future__ import annotations

import hashlib
import logging
from types import SimpleNamespace
from typing import Optional

import click
import requests
from flask import current_app, g, request, session, redirect, url_for, abort

from app import db
from app.models import User

logger = logging.getLogger(__name__)


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


_DEV_BLOB = {
    "user_guid": "00000000-0000-0000-0000-000000000000",
    "email": "dev@local",
    "display_name": "Dev SU",
    "user_type": "professional",
    "is_su_admin": True,
    "effective_phases": ["analysis"],
    "organization_ids": [],
}


def _phases(blob: dict) -> list:
    """Reform-canonical phases (M0 #416): prefer session_phases (Option C),
    fall back to the dual-emitted legacy effective_phases for pre-reform
    tokens."""
    return blob.get("session_phases") or blob.get("effective_phases") or []


def scope_org_guids(blob: dict) -> list:
    """Zone-1 read scope (M0 #416): affiliations[].care_unit_guid — the exact
    equivalent of the legacy flat organization_ids semantics — with a dual-read
    fallback to organization_ids for tokens issued before the reform blob went
    live. Public so fhir_read._org_filter uses the same derivation. Zone-2
    (parent care organisation) is deliberately NOT folded in here."""
    affs = blob.get("affiliations") or []
    if affs:
        return [a["care_unit_guid"] for a in affs if a.get("care_unit_guid")]
    return list(blob.get("organization_ids") or [])


def _blob_to_user(blob: dict) -> SimpleNamespace:
    return SimpleNamespace(
        guid=blob.get("user_guid"),
        username=blob.get("email") or blob.get("user_guid"),
        is_admin=bool(blob.get("is_su_admin")),
        is_su=bool(blob.get("is_su_admin")),
        org_ids=scope_org_guids(blob),
        blob=blob,
    )


def has_analysis_access(blob: Optional[dict]) -> bool:
    if not blob:
        return False
    if blob.get("is_su_admin"):
        return True
    return (
        blob.get("user_type") == "professional"
        and "analysis" in _phases(blob)
    )


def validate_sso_token(token: str) -> Optional[dict]:
    base = current_app.config.get("SSO_BASE_URL", "").rstrip("/")
    cid = current_app.config.get("SSO_CLIENT_ID", "")
    sec = current_app.config.get("SSO_CLIENT_SECRET", "")
    if not (base and cid and sec):
        return None
    try:
        r = requests.get(
            f"{base}/api/auth/me/service",
            headers={
                "Authorization": f"Bearer {token}",
                "X-SSO-Client-Id": cid,
                "X-SSO-Client-Secret": sec,
            },
            timeout=10,
        )
        return r.json() if r.status_code == 200 else None
    except requests.RequestException:
        return None


def initiate_sso_login(next_url: str, state: str) -> str:
    base = current_app.config.get("SSO_BASE_URL", "").rstrip("/")
    cb = current_app.config.get("SSO_CALLBACK_URL", "")
    return f"{base}/login?next={cb}&state={state}"


def _upsert_local_user(blob: dict) -> None:
    guid = blob.get("user_guid")
    if not guid:
        return
    u = User.query.filter_by(guid=guid).first()
    if not u:
        u = User(guid=guid, username=blob.get("email") or guid,
                 is_admin=bool(blob.get("is_su_admin")), is_su=bool(blob.get("is_su_admin")))
        db.session.add(u)
        db.session.commit()


def _public_path(path: str) -> bool:
    return (
        path.startswith("/auth/")
        or path == "/healthz"
        or path.startswith("/api/v1/health")
        or path.startswith("/api/v1/ingest")
        # /api/v1/observations/<guid>/provenance (per-point lookup,
        # ticket #288). The route handler enforces @require_service_key
        # itself. The bare /api/v1/observations search endpoint moved
        # to dashboard.pdhc in #291.
        or path.startswith("/api/v1/observations")
        or path.startswith("/static/")
    )


# Service-key auth: trusted sibling services may write FHIR via
# /api/v1/fhir/* and read via /api/v1/canonical/* without an SSO
# session. Header pair: X-Source-Service + X-Service-Key.
KNOWN_FHIR_SERVICES = {
    "sim.pdhc":       "SIM_PDHC_SERVICE_KEY",
    "dashboard.pdhc": "DASHBOARD_PDHC_SERVICE_KEY",
    # #541 — analyse.pdhc is the extracted analyse-layer reader (federates
    # CDR2-6 group/population reads), taking over from dashboard.pdhc. Added
    # alongside (not replacing) dashboard.pdhc so the cutover is reversible;
    # dashboard's identity is retired at #543 once analyse serves live.
    # cdr2-5 trusted it from 2026-08; cdr1 did not, so a federated read that
    # worked on every other CDR failed only here — added to cdr1 2026-09-23 so
    # the read wiring is identical across all CDRs. (That history was only in
    # cdr1's deployed copy; recovered into git 2026-10-06 while doing #760.)
    # READ only — see READ_ONLY_SERVICE_SOURCES below (#760).
    "analyse.pdhc":   "ANALYSE_PDHC_SERVICE_KEY",
    # gateway.pdhc is the ingestion gatekeeper; besides observations (via
    # /api/v1/ingest) it upserts the Patient FHIR resource so the care-delivery
    # patient list has a name (demographics resolved from ips at validated
    # report ingestion). Key is the same GATEWAY_PDHC_SERVICE_KEY cdr1 already
    # trusts for ingest — this just admits it on the /api/v1/fhir/* write path.
    "gateway.pdhc":   "GATEWAY_PDHC_SERVICE_KEY",
    # icd11.pdhc projects a FHIR Condition per diagnosis episode. It is the
    # authority for the episode and its append-only assertion log; this
    # repository holds the derived CURRENT state so the rest of the platform
    # reads diagnoses the same way it reads everything else.
    #
    # WRITE, so deliberately NOT in READ_ONLY_SERVICE_SOURCES below.
    #
    # Note for whoever reads this next: condition_history here is NOT the
    # diagnosis history. It records writes, not statements, and it is
    # write-ordered — so replaying "what did we hold on date D" from it gives
    # the wrong answer whenever a clinician states today that something became
    # true earlier. Ask icd11.pdhc for that.
    "icd11.pdhc":     "ICD11_PDHC_SERVICE_KEY",
}

# #293 read-lockdown allow-list: under CDR_READ_LOCKDOWN only these
# analyse-layer reader identities may read (all other trusted services may
# still WRITE). #541 admits analyse.pdhc here alongside dashboard.pdhc.
_ANALYSE_READER_SOURCES = ("dashboard.pdhc", "analyse.pdhc")


def _is_read_path(path: str) -> bool:
    """Read-side paths gated by CDR_READ_LOCKDOWN (#293).

    Ingest paths (/api/v1/ingest*) are NOT read paths — they're
    already in _public_path() above. The provenance + by-source-id
    sub-paths under /api/v1/observations are also public, so this
    function never sees them.
    """
    return (path.startswith("/api/v1/fhir/")
            or path.startswith("/api/v1/canonical/")
            or path.startswith("/api/v1/openehr/")
            or path.startswith("/api/v1/stats")
            or path.startswith("/api/v1/cambio/")
            # #468 — care-delivery read surface for the clinical dashboard.
            or path.startswith("/api/v1/clinical"))


def _service_key_outcome(app):
    """None = no headers (fall through), True = valid, False = bad.

    When CDR_READ_LOCKDOWN is true AND the request targets a read
    path, only the analyse-layer reader identities in
    _ANALYSE_READER_SOURCES (dashboard.pdhc, analyse.pdhc — #541) are
    accepted. The flag is per-CDR-deploy (cdr1 ships false because
    gateway writes to cdr1 directly per SSOT; cdr2-5 + cdr_6 ship
    true). See #293.
    """
    source = request.headers.get("X-Source-Service", "").strip()
    key = request.headers.get("X-Service-Key", "").strip()
    if not source and not key:
        return None
    if not source or not key:
        return False
    cfg_var = KNOWN_FHIR_SERVICES.get(source)
    if not cfg_var:
        return False
    expected = app.config.get(cfg_var, "")
    if not expected or key != expected:
        return False
    if (app.config.get("CDR_READ_LOCKDOWN")
            and _is_read_path(request.path)
            and source not in _ANALYSE_READER_SOURCES):
        return False
    if (source in READ_ONLY_SERVICE_SOURCES
            and request.method not in _SAFE_METHODS):
        logger.warning(
            "service-key caller %s is read-only (#760) and attempted %s %s",
            source, request.method, request.path)
        return WRITE_REFUSED
    g.source_service = source
    return True


# ---------------------------------------------------------------------------
# #760 — a caller that only ever reads must not hold a write grant
# ---------------------------------------------------------------------------
#
# Membership in KNOWN_FHIR_SERVICES is otherwise the WHOLE check: it gates
# `POST /api/v1/fhir/<type>`, `PUT /api/v1/fhir/<type>/<guid>` and
# `POST /api/v1/fhir/Bundle` alongside the GET searches, and the blob minted in
# `_service_blob` below is SU-equivalent and organisation-blind. So "allowed to
# GET /api/v1/fhir/Observation" has meant "allowed to overwrite any resource in
# the repository", for every identity added for a read.
#
# analyse.pdhc was added for reads (#541) and issues GET on all nine of its CDR
# call sites, so this costs it nothing. sim.pdhc and gateway.pdhc write by
# design and are deliberately NOT listed here.
#
# dashboard.pdhc added by #762, on the same evidence: all six of its `fanout()`
# call sites pass method="GET", and `services/cdr1_client.py` has a single
# transport, `_get_json`, built on requests.get. Its only two POSTs go to sso
# (token exchange) and ips — neither is a CDR. It is an active READER, so it is
# narrowed rather than retired the way monitor.pdhc was (#727).
READ_ONLY_SERVICE_SOURCES = frozenset({"analyse.pdhc", "dashboard.pdhc"})

# ---------------------------------------------------------------------------
# #857 / #874 — who may ASSERT a code from a WHOLE-SYSTEM answer space
# ---------------------------------------------------------------------------
#
# plan.pdhc's `$validate-code` answers TRUE for ANY `icd11-mms` code, including
# one that does not exist. That is correct and was ratified: plan holds no copy
# of an 85 000-code classification chosen at the point of care, so the per-code
# question has no meaningful answer there and asking it anyway returns a false
# negative rather than a fact. Measured 2026-10-21:
#
#     system=icd11-mms&code=1A00         -> true
#     system=icd11-mms&code=ZZ-nonsense  -> TRUE
#     system=icd10&code=A00.9            -> false   (the control)
#
# **THAT CONTROL IS NO LONGER TRUE, and its falsification is why #874 exists.**
# #872 flipped `icd10` to a whole-system answer space on 2026-11-02, so the
# third line now reads TRUE in production. The example above is kept as the
# 2026-10-21 measurement it was, not corrected in place, because the argument
# it supports was made on it. The gate, however, must not be: a rule written
# against ONE library's name goes stale the moment a second library is flagged,
# which is exactly what happened here within twelve days.
#
# But it MOVES the existence check rather than removing it. Per-code existence
# now rests entirely on icd11.pdhc validating against WHO's `codeinfo` at
# ENTRY. Inside the designed path that is sound. The exposure is BESIDE it:
# KNOWN_FHIR_SERVICES gates FHIR WRITES as well as reads (#760) and the blob it
# mints is SU-equivalent, so any identity on that list could POST a Condition
# carrying a fabricated ICD-11 code, plan would assent, and this CDR would
# store it with nothing in the chain refusing it.
#
# So the writer is restricted instead, and #874 made the restriction ask
# plan.pdhc WHICH libraries are affected instead of naming one.
#
# **Every flagged library admits NOBODY by default.** Measured 2026-10-31
# across cdr1, cdr2 and cdr3: `fhir_resources` holds ZERO Condition rows — only
# Observations, from sim.pdhc and gateway.pdhc — so no legitimate producer is
# affected. icd11.pdhc is not in KNOWN_FHIR_SERVICES at all today, so "only
# icd11.pdhc may write these" and "nobody may" are the same rule; this is the
# honest spelling of it.
#
# WHEN icd11.pdhc's Condition projection goes live it gets its own service key,
# an entry in KNOWN_FHIR_SERVICES, and its name HERE under `icd11-mms`. Adding
# it to the first list without adding it here will refuse its writes with the
# message below, which is the intended failure: loud, named, and pointing at
# this comment.
#
# `icd10` is deliberately absent too, and for a different reason: ICD-11 has an
# entry-point validator (icd11.pdhc, against WHO `codeinfo`) and ICD-10-SE has
# none, so there is no service that could honestly be trusted to assert one.
#
# This is a narrower control than validating the code against termbank's
# used-concept record (#857 option (a)), which would close the hole whatever
# key the caller holds. That remains the durable fix and is NOT done: it needs
# a termbank HTTP client in this service — there is none, only URI strings —
# and it would put termbank, a loopback-only service with no public subdomain,
# on the clinical write path. Tracked against the #868 gate: before real
# patient data.

#: Per canonical library, the service identities permitted to assert one of its
#: codes. A library ABSENT from this mapping admits nobody — the fail-closed
#: direction, and still the rule for `icd11-mms` and for every library plan
#: flags in future.
#:
#: THIS IS THE FIRST ENTRY THAT ADMITS ANYBODY (#874, operator decision
#: 2026-11-04), so the fail-closed invariant stops being vacuous here. It was
#: empty until now, and the test guarding that emptiness explained why: the only
#: candidate, `icd11.pdhc`, is not in `KNOWN_FHIR_SERVICES` at all, so "only
#: icd11.pdhc may" and "nobody may" were the same rule. `sim.pdhc` IS in
#: `KNOWN_FHIR_SERVICES` on cdr1 (live key since 2026-10-12), so this grant is
#: real rather than theoretical.
#:
#: WHY sim.pdhc, measured 2026-11-04 rather than assumed:
#:
#:  - Its diagnosis codes are HAND-CURATED REAL CODES, not generated. The
#:    manifest (`sim.pdhc/concepts/diabetes_v1.yaml`) pairs a genuine SNOMED CT
#:    concept id with a genuine ICD-10 code per diagnosis: 46635009/E10 type 1
#:    diabetes, 44054006/E11 type 2, 38341003/I10 hypertension. The gate exists
#:    to stop a machine asserting a code nobody verified; here a human did.
#:  - The PRIMARY coding is SNOMED, which plan does NOT flag as a whole-system
#:    answer space. ICD-10 appears only as `icd10_alt`, a dual-coding. So what
#:    tripped the gate was a correct cross-reference on a properly-adopted
#:    resource — `whole_system_assertion_refused_for` walks EVERY coding by
#:    design, which is right, and that is exactly why this entry is needed.
#:  - Everything in cdr1 is synthetic in this phase, by policy.
#:  - Without it no diagnosis data reaches cdr1 at all, and the alerting/MDR
#:    work reads thresholds AGAINST a diagnosis (#917).
#:
#: DELIBERATELY NARROWER than the decision as first drafted, which also granted
#: `icd11-mms`: sim emits ZERO ICD-11 codings (0 references against 22 for
#: icd10, with a control, measured 2026-11-04), so that would have been a right
#: with no consumer. Three of the service identities found on this platform so
#: far named no service at all; the narrowest list that works is the rule.
WHOLE_SYSTEM_ASSERTING_SERVICES: dict[str, frozenset[str]] = {
    "icd10": frozenset({"sim.pdhc"}),
}

#: The libraries this gate refuses even when plan.pdhc cannot be asked. plan is
#: the authority and may WIDEN this set; it may not narrow it. Without a floor,
#: an unreachable (or misconfigured) plan would silently disarm the gate while
#: writes still succeeded — open at exactly the wrong moment. `icd11-mms` is
#: here because it is the level #857 shipped at, so this cannot regress below it.
#:
#: `icd10` was ADDED by #874. #872 flagged it in plan PRODUCTION on 2026-11-02
#: and did not update the floor with it, so until now a plan outage left every
#: service identity free to assert any ICD-10 code — precisely the window the
#: paragraph above exists to close, left open for the one library that had just
#: been flagged. Proven rather than argued: the guarding test failed before
#: this line changed, with "a plan outage disarmed the gate for icd10".
#:
#: Widening the floor does not revoke anyone's grant. The floor decides which
#: libraries are GATED; WHOLE_SYSTEM_ASSERTING_SERVICES decides who may assert
#: one. sim.pdhc passes either way, and a test asserts that.
#:
#: THE RULE THIS SETS for the next library plan flags: add it here in the same
#: commit. A library flagged in plan but missing from the floor is protected
#: only while plan is answering.
WHOLE_SYSTEM_LIBS_FLOOR: frozenset[str] = frozenset({"icd11-mms", "icd10"})

#: A coding ``system`` that identifies a library without spelling its name.
#: WHO's own URI is what appears on a raw FHIR Condition from a real producer,
#: and it is not in plan's table at all.
ICD11_WHO_SYSTEM = "http://id.who.int/icd/release/11/mms"
ICD11_CANONICAL_LIB = "icd11-mms"
WHOLE_SYSTEM_SYSTEM_ALIASES: dict[str, str] = {
    ICD11_WHO_SYSTEM: ICD11_CANONICAL_LIB,
}


def _plan_client():
    """The PlanClient this app already uses, so the gate shares its TTL cache.

    One GET per minute for the whole process, not one per request. Imported
    locally: `auth` is imported by the app factory and `plan_client` reaches
    back into app config, so a module-level import cycles.
    """
    canon = getattr(current_app, "_canonicaliser", None)
    plan = getattr(canon, "plan", None)
    if plan is not None:
        return plan
    if not hasattr(current_app, "_gate_plan_client"):
        from app.services.plan_client import PlanClient
        current_app._gate_plan_client = PlanClient()
    return current_app._gate_plan_client


def flagged_systems() -> dict[str, str]:
    """``system`` spelling -> library name, for every library whose adoption
    plan.pdhc declares at SYSTEM granularity.

    Asks plan; falls back to the floor when plan does not answer. The floor is
    always present, so a plan outage can only make this set SMALLER than
    reality, never empty.
    """
    try:
        published = _plan_client().whole_system_systems()
    except Exception:  # pragma: no cover - defensive; the client already logs
        logger.warning("whole-system adoption flags could not be read; using floor")
        published = None

    systems = dict(WHOLE_SYSTEM_SYSTEM_ALIASES)
    for name in WHOLE_SYSTEM_LIBS_FLOOR:
        systems.setdefault(name, name)
    if published:
        systems.update(published)
    return systems


def _library_of_coding(coding: dict, systems: dict[str, str]) -> str | None:
    """Which flagged library this coding belongs to, or None.

    EXACT matches only. The predecessor used `f"/{lib}/" in system`, and a
    substring test cannot distinguish `icd10` from a system that merely
    contains it — the same defect #804 recorded in gateway's path guard.
    """
    system = (coding.get("system") or "").strip()
    if not system:
        return None
    hit = systems.get(system)
    if hit is not None:
        return hit
    from app.services.plan_client import parse_canonical_system
    name = parse_canonical_system(system)
    if name is not None:
        return systems.get(name)
    return None


def whole_system_assertion_refused_for(fhir: dict) -> str | None:
    """Reason this caller may not write this resource, or None.

    Returns None for every non-service caller: the rule is about machine
    identities holding an SU-equivalent blob, not about a clinician.

    Walks EVERY coding on the resource, not the canonicalised code paths.
    `canonicalise()` only visits `Observation.code`, `Condition.code` and their
    siblings, so a fabricated classification code in `bodySite`, `severity`,
    `stage.summary` or `evidence.code` would never reach that gate. Reading a
    fixed list of paths is how a guard ends up checking the one place the
    producer did not use.
    """
    source = (getattr(g, "access_blob", None) or {}).get("service_source")
    if not source:
        return None
    systems = flagged_systems()
    for coding in _codings_in(fhir):
        lib = _library_of_coding(coding, systems)
        if lib is None:
            continue
        if source in WHOLE_SYSTEM_ASSERTING_SERVICES.get(lib, frozenset()):
            continue
        permitted = sorted(WHOLE_SYSTEM_ASSERTING_SERVICES.get(lib, frozenset()))
        who = ", ".join(f"'{s}'" for s in permitted) if permitted else "no service"
        return (
            f"service '{source}' may not assert a '{lib}' code. "
            f"plan.pdhc answers `true` for every code in '{lib}' because it is a "
            f"whole-system answer space, so that assent is an ADOPTION statement "
            f"about the code system and NOT a check that the code exists "
            f"(#857, #872, #874). Per-code existence has to be verified at entry "
            f"by whoever owns that classification. Permitted here: {who}. See "
            f"WHOLE_SYSTEM_ASSERTING_SERVICES in app/auth.py."
        )
    return None


def _codings_in(node, _depth: int = 0):
    """Every `coding` entry anywhere in a FHIR resource.

    Walks rather than reading known paths: a Condition carries them under
    `code`, but also under `bodySite`, `severity`, `stage.summary` and
    `evidence.code`, and a Bundle entry nests them deeper still. Reading a
    fixed list of paths is how a guard ends up checking the one place the
    producer did not use. Depth-bounded so a cyclic or absurd body cannot
    spin here.
    """
    if _depth > 12:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "coding" and isinstance(value, list):
                for c in value:
                    if isinstance(c, dict):
                        yield c
            else:
                yield from _codings_in(value, _depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _codings_in(item, _depth + 1)


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# A distinct outcome from "bad credentials". The credentials ARE valid; the verb
# is not. Sharing the generic 403 would send whoever hits this to check a key
# that was never the problem.
WRITE_REFUSED = object()


def _service_blob(source_service: str) -> dict:
    """Synthetic access blob for service-key auth path. Marks the
    request as SU-equivalent (organisation-blind) so Rule-24 org
    scoping does not block sim's writes — tagged so downstream code can
    distinguish service writes from human users."""
    return {
        "user_guid": f"00000000-0000-0000-0000-service-{source_service[:8]}",
        "email": f"service:{source_service}",
        "display_name": f"service:{source_service}",
        "user_type": "service",
        "is_su_admin": True,
        "effective_phases": ["analysis", "ingestion"],
        "organization_ids": [],
        "service_source": source_service,
    }


def install_request_loader(app):
    @app.before_request
    def _loader():
        if _public_path(request.path):
            return None
        sk = _service_key_outcome(app)
        if sk is True:
            blob = _service_blob(g.source_service)
            g.access_blob = blob
            g.current_user = _blob_to_user(blob)
            return None
        if sk is WRITE_REFUSED:
            from flask import jsonify
            return jsonify({"error": "this service key is read-only"}), 403
        if sk is False:
            from flask import jsonify
            return jsonify({"error": "Invalid service credentials"}), 403
        mode = app.config.get("AUTH_MODE", "off")
        if mode == "off":
            g.access_blob = _DEV_BLOB
            g.current_user = _blob_to_user(_DEV_BLOB)
            return None
        token = session.get("sso_token")
        if not token:
            session["sso_next"] = request.url
            return redirect(url_for("auth.login"))
        blob = validate_sso_token(token)
        if not blob:
            session.clear()
            session["sso_next"] = request.url
            return redirect(url_for("auth.login"))
        session["access_blob"] = blob
        # Ticket #52 / SSO #43: forced password reset — bounce to SSO's
        # change-password page until SSO clears the flag on the next blob.
        if blob.get("must_change_password"):
            base = app.config.get("SSO_BASE_URL", "").rstrip("/")
            return redirect(f"{base}/change-password")
        if not has_analysis_access(blob):
            abort(403)
        g.access_blob = blob
        g.current_user = _blob_to_user(blob)
        return None


def register_cli(app):
    @app.cli.command("create-su")
    @click.option("--username", required=True)
    @click.option("--password", required=True)
    def create_su(username, password):
        existing = User.query.filter_by(username=username).first()
        if existing:
            existing.is_su = True
            existing.is_admin = True
            existing.password_hash = _hash(password)
        else:
            db.session.add(User(username=username, password_hash=_hash(password), is_su=True, is_admin=True))
        db.session.commit()
        click.echo(f"SU {username} ready")

    @app.cli.command("backfill-live-observations")
    @click.option("--dry-run", is_flag=True,
                  help="Show what would be done without committing.")
    @click.option("--chunk", type=int, default=500,
                  help="Commit every N rows.")
    @click.option("--limit", type=int, default=None,
                  help="Cap the number of rows processed.")
    def backfill_live_observations(dry_run, chunk, limit):
        """SSOT phase 5 follow-up (#295): populate the Live observation
        table from existing fhir_resources rows that have no Live twin.

        The ingest pipeline now creates the Live row on every new
        write (see ingest_pipeline.py step 3.5). This CLI catches the
        7060 historical rows that were forwarded before that change.
        """
        from app.models import FhirResource
        from app.models.resources import live_model
        from app.services.ingest_pipeline import build_live_observation_row

        LiveObs = live_model("Observation")
        if LiveObs is None:
            click.echo("Observation Live model not registered. Aborting.")
            return

        existing_guids = {row[0] for row in
                          db.session.query(LiveObs.guid).all()}
        click.echo(f"Live observation rows already present: {len(existing_guids)}")

        q = (FhirResource.query
             .filter(FhirResource.resource_type == "Observation")
             .order_by(FhirResource.created_at.asc()))
        if limit is not None:
            q = q.limit(limit)

        # Materialise the row set up front. yield_per() opens a named
        # cursor that goes invalid the moment we commit a chunk, which
        # made the first deploy bail after 500 rows. 7061 rows fits
        # comfortably in memory; if this ever scales, switch to offset
        # pagination.
        candidates = q.all()
        click.echo(f"Candidate Observation rows in fhir_resources: {len(candidates)}")

        inserted = 0
        skipped = 0
        batch = 0
        for fr in candidates:
            fhir = fr.resource_json or {}
            fhir_id = fhir.get("id")
            if not fhir_id or fhir_id in existing_guids:
                skipped += 1
                continue
            existing_guids.add(fhir_id)
            if dry_run:
                inserted += 1
                continue
            row = build_live_observation_row(
                fhir,
                patient_guid=fr.patient_guid,
                source_service=fr.source_service,
                ingest_raw_guid=fr.ingest_raw_guid,
            )
            if row is None:
                skipped += 1
                continue
            db.session.add(row)
            inserted += 1
            batch += 1
            if batch >= chunk:
                db.session.commit()
                click.echo(f"  committed {inserted} so far, {skipped} skipped …")
                batch = 0

        if not dry_run and batch:
            db.session.commit()

        prefix = "Dry-run: would insert" if dry_run else "Inserted"
        click.echo(f"{prefix} {inserted} Live Observation rows. "
                   f"Skipped {skipped} (already present or no FHIR id).")

    @app.cli.command("backfill-clinical-context")
    @click.option("--dry-run", is_flag=True)
    @click.option("--chunk", type=int, default=500)
    def backfill_clinical_context(dry_run, chunk):
        """#302: fill the new ClinicalContext columns from
        FhirResource.resource_json for rows the migration's batch
        SQL didn't catch (e.g. late arrivals after the schema bump).
        Idempotent — only updates NULL columns.
        """
        from app.models import FhirResource, ClinicalContext

        rows = (db.session.query(ClinicalContext, FhirResource)
                .join(FhirResource,
                      ClinicalContext.ingest_raw_guid == FhirResource.ingest_raw_guid)
                .filter(FhirResource.resource_type == "Observation")
                .filter((ClinicalContext.service_request_guid.is_(None))
                        | (ClinicalContext.plan_definition_guid.is_(None))
                        | (ClinicalContext.provider_org_guid.is_(None))
                        | (ClinicalContext.contract_guid.is_(None))
                        | (ClinicalContext.requesting_org_guid.is_(None))
                        | (ClinicalContext.concept_guid.is_(None)))
                .all())
        click.echo(f"Candidate rows: {len(rows)}")

        def _basedon(fhir, kind):
            for entry in fhir.get("basedOn") or []:
                # Prefer identifier.value (always the bare guid).
                if entry.get("type") in (None, kind):
                    v = (entry.get("identifier") or {}).get("value")
                    bare = _bare_guid(v)
                    if bare:
                        return bare
                ref = entry.get("reference") or ""
                if ref.startswith(f"{kind}/") or (
                        entry.get("type") == kind and "/" in ref):
                    bare = _bare_guid(ref)
                    if bare:
                        return bare
            return None

        def _performer(fhir):
            for p in fhir.get("performer") or []:
                v = (p.get("identifier") or {}).get("value")
                if v:
                    return v
            return None

        import re
        _guid_re = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE,
        )

        def _bare_guid(value):
            """Strip URL prefixes so a "https://…/Contract/<guid>" or
            a "Contract/<guid>" reduces to just <guid>. Returns None
            if no bare 36-char guid suffix found."""
            if value is None:
                return None
            tail = str(value).rsplit("/", 1)[-1]
            return tail if _guid_re.match(tail) else None

        def _ext(fhir, key):
            # Extension keys can be slash-suffixed (.../<key>) or
            # colon-suffixed (urn:…:<key>) — accept both.
            for ext in fhir.get("extension") or []:
                url = ext.get("url") or ""
                if (url.endswith("/" + key) or url.endswith(":" + key)
                        or url == key):
                    vr = ext.get("valueReference") or {}
                    candidate = None
                    if isinstance(vr, dict):
                        candidate = (vr.get("reference")
                                     or (vr.get("identifier") or {}).get("value"))
                    candidate = (candidate
                                 or ext.get("valueString")
                                 or ext.get("valueUrl"))
                    if candidate is None and not isinstance(vr, dict):
                        candidate = vr
                    return _bare_guid(candidate)
            return None

        filled = 0
        for i, (cc, fr) in enumerate(rows, 1):
            fhir = fr.resource_json or {}
            cc.service_request_guid = (cc.service_request_guid
                                       or _basedon(fhir, "ServiceRequest"))
            cc.plan_definition_guid = (cc.plan_definition_guid
                                       or _basedon(fhir, "PlanDefinition"))
            cc.provider_org_guid    = cc.provider_org_guid    or _performer(fhir)
            cc.contract_guid        = cc.contract_guid        or _ext(fhir, "contract")
            cc.requesting_org_guid  = cc.requesting_org_guid  or _ext(fhir, "requesting-org")
            # The platform's FHIR Observation carries the concept via the
            # `transaction` extension (the concept-resolved transaction).
            cc.concept_guid         = (cc.concept_guid
                                       or _ext(fhir, "concept")
                                       or _ext(fhir, "transaction"))
            filled += 1
            if not dry_run and i % chunk == 0:
                db.session.commit()
                click.echo(f"  committed {i}")
        if not dry_run:
            db.session.commit()
        prefix = "Dry-run: would update" if dry_run else "Updated"
        click.echo(f"{prefix} {filled} ClinicalContext rows.")
