"""run_oa_fetch_pass — sibling worker that fetches OA PDFs.

Closes the chase ↔ stub loop. The finding-chase worker creates
stub paper refs (identifiers known, ``pdf_sha256 IS NULL``); this
worker walks the stub backlog and asks **ten OA sources in
cascade** whether an open-access copy exists:

1. **Publisher pattern** — deterministic DOI→PDF URL for publishers
   with a known endpoint (Springer/BMC, PLOS, …). Free; no API.
2. **Elsevier** — Article Retrieval API by DOI. Key-gated
   (``PRECIS_ELSEVIER_API_KEY``); the only route for ScienceDirect.
3. **Wiley** — TDM API by DOI. Token-gated
   (``PRECIS_WILEY_TDM_TOKEN``); direct version-of-record PDF.
4. **Unpaywall** — aggregator of 30+ OA sources, by DOI. Free;
   TOS requires an email parameter.
5. **Crossref** — publisher TDM full-text PDF links, by DOI. Free;
   needs the polite ``mailto`` (= the Unpaywall email).
6. **OpenAlex** — OA ``pdf_url`` locations, by DOI. Free; ``mailto``
   optional. Different coverage to Unpaywall.
7. **Europe PMC** — biomedical OA full-text PDF, DOI→PMCID. Free.
8. **CORE** — green-OA repository copies, by DOI. Key-gated
   (``PRECIS_CORE_API_KEY``); the net for paywalled-publisher papers.
9. **arXiv** — direct PDF download by arXiv ID. Free; no key.
10. **Semantic Scholar** — ``openAccessPdf`` field, by DOI / arXiv /
    S2 id. Free; no key for low volume.

The deterministic + key-gated legs run *first* because they sidestep
the aggregators' common landing-page-as-OA miss on fresh DOIs;
identifier-/credential-less legs are silent no-ops (return ``None``,
no event). When a source yields a PDF the cascade stops; later
sources only run when earlier ones returned ``no_oa_version`` or
``fetch_failed``. Each attempt writes its own ``ref_events`` row
(``source='fetcher:<leg>'``) so the audit trail shows what was tried
and what worked.

When a PDF lands, it goes into the watch inbox →
``precis watch`` triggers ``precis_add`` → C7's
``register_aliases_and_maybe_upgrade`` promotes the stub →
the chase resumes on the next pass.

This is a sibling worker (plain function), not a
``WorkerHandler`` subclass — same pattern as
``precis.workers.segment_toc`` and ``precis.workers.chase``.

Event vocabulary (same shape across all three sources):

- ``fetch_ok``       — PDF downloaded; payload carries url + bytes + license
- ``no_oa_version``  — source confirmed no OA copy
- ``fetch_failed``   — source returned a URL but download failed
- ``rate_limited``   — 429 / equivalent throttle
- ``api_error``      — 5xx / network / unexpected JSON
- ``invalid_identifier`` — the identifier failed format validation
- ``identifier_missing`` — the ref had no usable identifier for this source

Ahead of the cascade, each claimed stub also runs a ``fetch-time
retraction gate`` (see :func:`_apply_retraction_gate`): a cheap,
DOI-only Crossref check, reusing ``precis.ingest.provenance.check_doi``,
that stamps the paper's dominant provenance status before any download
leg runs. It acts on all three statuses, differently:

- ``retracted`` — hard-skip: stamp ``refs.retraction_status='retracted'``,
  write a ``source='fetch_oa'``, ``event='retraction_skip'`` breadcrumb,
  and never run the cascade. :func:`precis.store._stub_predicate.
  stub_predicate_sql` then excludes the ref from every future claim query
  and stub/chase-queue browse — so this fires at most once.
- ``corrected`` / ``expression_of_concern`` — soft flag: stamp the status
  (reader-banner only) and *continue* the fetch (we still want the PDF).
  Emits an ``event='provenance_flag'`` breadcrumb. These stubs stay
  fetch-eligible, so the gate re-checks each pass to catch a later
  ``corrected → retracted`` upgrade; it only re-writes on an actual
  status change, so an unchanged flag is a no-op.

``precis stubs`` (CLI subcommand, Step 4) reads the latest event
per stub to render the backlog.

Pre-existing relative: ``scripts/_doilist.py`` (``doilist scan
--download``) is an *operator-facing* CLI that drives Unpaywall
fetches from a curated ``dois_to_get.md`` file. Different starting
point (file-driven, not stub-driven); different output (``downloads/``
dir, no DB writes); both can coexist.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from psycopg import Connection
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from precis import secrets as _secrets
from precis import settings
from precis.alerts import raise_alert as _raise_alert
from precis.alerts import resolve_stale_alerts as _resolve_alerts
from precis.ingest.fetch_sidecar import read_sidecar, sidecar_path, write_sidecar
from precis.store._stub_predicate import stub_predicate_sql
from precis.workers import activity

log = logging.getLogger(__name__)


_SOURCE_PUBLISHER = "fetcher:publisher"
_SOURCE_ELSEVIER = "fetcher:elsevier"
#: Markup-first sibling of ``_SOURCE_ELSEVIER`` — same endpoint, XML view
#: instead of PDF. See :func:`_try_elsevier_markup`.
_SOURCE_ELSEVIER_XML = "fetcher:elsevier_xml"
_SOURCE_WILEY = "fetcher:wiley"
_SOURCE_UNPAYWALL = "fetcher:unpaywall"
_SOURCE_CROSSREF = "fetcher:crossref"
_SOURCE_OPENALEX = "fetcher:openalex"
_SOURCE_EUROPEPMC = "fetcher:europepmc"
_SOURCE_CORE = "fetcher:core"
_SOURCE_ARXIV = "fetcher:arxiv"
_SOURCE_S2 = "fetcher:s2"
_SOURCE_OPENALEX_CONTENT = "fetcher:openalex_content"

# Markup-first legs (docs/backlog/markup-first-ingest.md). Run *before*
# the PDF cascade when PRECIS_FETCH_MARKUP is set, so a structured
# full-text source is preferred as the chunk source; the PDF cascade
# still runs afterwards to acquire the printable.
_SOURCE_EUROPEPMC_JATS = "fetcher:europepmc_jats"
_SOURCE_ARXIV_HTML = "fetcher:arxiv_html"
_SOURCE_ARXIV_SOURCE = "fetcher:arxiv_source"

# OpenAlex Content API per-file price (their published ~$0.01/file). Recorded
# as ``cost_usd`` on the ``fetch_ok`` event so the paid spend is auditable in
# ``ref_events`` alongside the free legs (which record None).
_OPENALEX_CONTENT_COST_USD = 0.01

# OpenAlex charges the ``content`` endpoint type 100 credits per fetch (their
# published ``credit_costs`` table). We use this to translate the operator's
# "keep enough runway for N paid fetches" floor into a raw-credit threshold.
_OPENALEX_CONTENT_CREDIT_COST = 100

# Default low-balance floor: raise the alert once fewer than this many paid
# content fetches' worth of daily credits remain. 20 × 100 credits = 2000.
_OPENALEX_MIN_FETCHES_DEFAULT = 20

# Browser-ish UA for hosts that Cloudflare-gate non-browser agents
# (CORE's API + many institutional repositories answer the default
# httpx / precis UA with an "error 1010" interstitial).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# The retry-window predicate matches any ``fetcher:%`` source rather
# than one canonical provider: the window must arm after whichever
# provider actually ran. Keying it on a single provider that may be
# disabled (e.g. Unpaywall without an email) silently defeats the
# guard and turns the cascade into a per-pass spin loop.

# Unpaywall public API root. Free; requires ``email=`` per their TOS.
# https://unpaywall.org/products/api
_UNPAYWALL_BASE = "https://api.unpaywall.org/v2"

# Default per-request timeout. Generous — Unpaywall sometimes takes
# a few seconds on cold edges; PDF downloads need their own budget.
_API_TIMEOUT_S = 30.0
_DOWNLOAD_TIMEOUT_S = 120.0

# Disk-fill guard, not a size policy: the wall-clock timeout above bounds a
# stalled download, but a slow-yet-live upstream streaming forever would
# otherwise fill the disk with no cap on total bytes. 512 MiB is generous
# even for book-scale PDFs/markup.
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

# Retry policy on rate-limit + transient API errors. Exponential
# backoff matches the pattern in ``precis.ingest.citations``.
_RETRY_MAX_ATTEMPTS = 4

# Don't re-poke Unpaywall for the same stub within this window. The
# fetcher's claim query honours it via a LEFT JOIN on ref_events.
# This is the *base* window; the claim query widens it exponentially
# per prior fetch attempt (see ``claim_stubs_to_fetch``) so a stub
# with no OA copy anywhere backs off instead of re-polling daily
# forever.
_RETRY_WINDOW_HOURS = 24

# Backoff cap. A stub that's been tried many times (closed-access, no
# OA anywhere) settles to one retry per this window rather than giving
# up entirely — a paper can become OA later, so we never permanently
# stop, we just slow to monthly. 720h = 30 days.
_RETRY_BACKOFF_MAX_HOURS = 720

# gr170366: how long a staged markup trigger + sidecar (or a stray
# ``.part`` partial download) may sit under ``inbox_dir / ".staging"``
# before ``_sweep_stale_staging`` treats it as abandoned. Must comfortably
# exceed the slowest plausible per-stub loop iteration — the markup leg
# plus the full PDF cascade (up to ~11 network legs, each capped at
# ``_DOWNLOAD_TIMEOUT_S`` = 120s) — so the sweep never races a stub that's
# genuinely still mid-flight. A re-claim of the same stub self-heals the
# entry within this window (deterministic filename); the sweep only
# matters for a stub that stops being reclaimed (identifier change,
# manual resolution elsewhere).
_STAGING_GC_MAX_AGE_HOURS = 6.0

# Fraction of each claim batch drawn at random rather than by ``prio``
# (see :func:`_fetch_explore_fraction`). Ranking must never harden into a
# hard filter — a mis-ranked or freshly-registered (unranked) stub still
# needs a chance to surface, so a slice of every claim ignores prio
# entirely.
_FETCH_EXPLORE_FRACTION_DEFAULT = 0.1

# DOI shape validation. Loose but rejects obviously-broken inputs
# before paying for an HTTP roundtrip.
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

# arXiv id shape. Accepts both new-style (``2401.12345`` /
# ``2401.12345v2``) and old-style (``hep-th/9901001``). The
# bare-id regex below is permissive — arXiv normalises its URL.
_ARXIV_ID_RE = re.compile(
    r"^([a-z\-]+/)?\d{4}\.?\d{4,5}(v\d+)?$|^[a-z\-]+/\d{7}(v\d+)?$"
)


# ── Result type ────────────────────────────────────────────────────


@dataclass
class FetchOutcome:
    """Per-stub outcome the runner aggregates into BatchResult."""

    event: str  # 'fetch_ok' | 'no_oa_version' | ...
    payload: dict[str, Any]
    duration_ms: int
    cost_usd: float | None = None  # always None for Unpaywall (free)


# ── Claim query ────────────────────────────────────────────────────


@dataclass(frozen=True)
class StubRef:
    """Stub identifiers the cascade tries each source against.

    At least one of ``doi`` / ``arxiv`` / ``s2_id`` is non-None
    by construction of the claim query — a stub with no usable
    identifier is excluded.
    """

    ref_id: int
    doi: str | None
    arxiv: str | None
    s2_id: str | None
    cite_key: str | None
    #: Current ``refs.retraction_status`` (``None`` / ``corrected`` /
    #: ``expression_of_concern`` on a claimed stub — a ``retracted`` one is
    #: excluded by the claim predicate so it never reaches here). Lets the
    #: retraction gate stamp only on a *change*, so a corrected/EoC stub —
    #: which stays fetch-eligible and re-checks every pass — doesn't re-emit
    #: an identical event each time.
    retraction_status: str | None = None


def _fetch_explore_fraction() -> float:
    """Fraction of each claim batch drawn at random rather than by ``prio``.

    Configurable via ``PRECIS_FETCH_EXPLORE_FRACTION`` (default
    :data:`_FETCH_EXPLORE_FRACTION_DEFAULT` = 0.1). A value ``<= 0``
    disables the exploration slice entirely — the claim then reduces to
    pure ``prio`` order. Tolerant of a non-numeric override (same
    fallback shape as ``_openalex_min_credits``): logs and falls back to
    the default rather than raising and taking the pass down.
    """
    raw = os.environ.get("PRECIS_FETCH_EXPLORE_FRACTION", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            log.warning(
                "fetch_oa: ignoring non-numeric PRECIS_FETCH_EXPLORE_FRACTION=%r",
                raw,
            )
    return _FETCH_EXPLORE_FRACTION_DEFAULT


def claim_stubs_to_fetch(
    conn: Connection,
    *,
    limit: int,
    retry_after_hours: int = _RETRY_WINDOW_HOURS,
    backoff_max_hours: int = _RETRY_BACKOFF_MAX_HOURS,
    explore_fraction: float | None = None,
) -> list[StubRef]:
    """Lock and return up to ``limit`` stubs needing an OA fetch attempt.

    A stub qualifies when ``refs.pdf_sha256 IS NULL`` AND at least one
    of {DOI, arXiv, S2 id} is registered. Excludes stubs tried by *any*
    ``fetcher:%`` source within an **exponentially-widening** window —
    the retry window applies cross-source by convention (if one source
    had nothing N hours ago, the others probably won't either).

    Keying the window on the literal ``fetcher:unpaywall`` source used
    to defeat the whole guard in deployments where Unpaywall is
    disabled (no ``PRECIS_UNPAYWALL_EMAIL``): the cascade then only ran
    arXiv + S2, never wrote an ``unpaywall`` event, so the window never
    armed and every stub re-qualified on every pass — re-polling S2
    hundreds of times a day. Matching ``fetcher:%`` arms the window
    after whichever provider actually ran.

    **Backoff.** A flat window still re-polls a no-OA-anywhere stub
    once per ``retry_after_hours`` *forever*. Instead the effective
    window doubles per prior attempt —
    ``base * 2^(attempts-1)``, capped at ``backoff_max_hours`` — so a
    closed-access paper settles to one retry per ~30 days rather than
    daily. It never gives up entirely (a paper can become OA later),
    it just slows down. Content-duplicate stubs are resolved out of
    the backlog separately, at ingest dedup time (see
    ``precis.ingest.add``), so they don't even reach the cap.

    Ordering: **``prio`` ascending, NULLs last** — the ``stub_rank``
    pass writes ``refs.prio`` (1=hottest .. 10=coldest, ``NULL`` =
    unranked) and the fetcher spends its claim budget on the hottest
    stubs first. Ties (including every unranked stub, which all sort
    to the bottom of the ``prio`` order together) fall back to the
    pre-``prio`` ordering: **explicitly re-queued stubs first**
    (``meta.oa_requeued``), then newest-stub-first (``ref_id DESC``).
    A re-queue — from the ``requeue_stranded_fetches`` heal or an
    operator — is a "try this again *now*" signal; without the
    priority the reset stub would sink behind the whole newest-first
    backlog (hundreds of routine stubs, drained at a few per hour) and
    effectively never retry, which would make the re-queue a no-op.
    Among non-re-queued stubs, newest-first still surfaces a chase
    bottleneck promptly when a chain creates many stubs. ``FOR UPDATE
    OF r SKIP LOCKED`` lets multiple fetcher workers run in parallel.

    **Exploration slice.** ``prio`` must never harden into a hard
    filter — a freshly-registered (unranked) or mis-ranked stub still
    needs a chance to surface, so ``explore_fraction`` (default from
    :func:`_fetch_explore_fraction`, env ``PRECIS_FETCH_EXPLORE_FRACTION``)
    carves off ``max(1, floor(limit * explore_fraction))`` of the batch
    and claims it at random (``ORDER BY random()``) from the same
    eligible set, *before* the ``prio``-ordered claim runs for the
    remainder (excluding whatever the random slice already locked). A
    fraction ``<= 0`` disables the slice — the claim then reduces to
    pure ``prio`` order. Both legs keep ``FOR UPDATE OF r SKIP LOCKED``.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if explore_fraction is None:
        explore_fraction = _fetch_explore_fraction()

    # ``min(id_value)`` (not a bare scalar subquery): a ref can carry
    # more than one identifier of the same kind (two DOIs / cite_keys
    # from a dedup-merge or messy metadata), and a bare scalar subquery
    # returning >1 row raises CardinalityViolation, taking the whole
    # pass down every tick. An aggregate returns exactly one row (NULL
    # if none) and picks a stable representative — any valid id fetches.
    select_cols = """
        SELECT r.ref_id,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'doi')      AS doi,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'arxiv')    AS arxiv,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 's2')       AS s2_id,
               (SELECT min(id_value) FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS cite_key,
               r.retraction_status                                 AS retraction_status
    """
    from_where = f"""
          FROM refs r
          LEFT JOIN LATERAL (
                SELECT count(*) AS attempts, max(e.ts) AS last_ts
                  FROM ref_events e
                 WHERE e.ref_id = r.ref_id
                   AND e.source LIKE 'fetcher:%%'
          ) fe ON TRUE
          -- Quest reweighting (slice 2): a stub that `serves` an active quest
          -- jumps the fetch queue, weighted by that quest's striving weight
          -- ((11 - prio)/10, matching quest.reweight.base_weight). Uses the
          -- served quest's own prio directly — the quest→quest ladder is a
          -- rotation-side refinement, not needed to order acquisitions. NULL
          -- (serves no active quest) → 0, so the ordering is unchanged when no
          -- quest is active.
          LEFT JOIN LATERAL (
                SELECT max((11 - COALESCE(q.prio, 5)) / 10.0) AS qw
                  FROM links l
                  JOIN refs q ON q.ref_id = l.dst_ref_id
                  JOIN ref_tags rt ON rt.ref_id = q.ref_id
                  JOIN tags t ON t.tag_id = rt.tag_id
                 WHERE l.src_ref_id = r.ref_id
                   AND l.relation = 'serves'
                   AND q.kind = 'quest'
                   AND q.deleted_at IS NULL
                   AND t.namespace = 'STATUS' AND t.value = 'active'
          ) qb ON TRUE
         WHERE {stub_predicate_sql("r")}
           AND (
                 fe.last_ts IS NULL
                 OR fe.last_ts < now() - (
                      LEAST(
                        %(retry_after)s::double precision
                          * POWER(2, GREATEST(fe.attempts - 1, 0)),
                        %(backoff_max)s::double precision
                      ) * INTERVAL '1 hour'
                 )
           )
    """
    params: dict[str, Any] = {
        "retry_after": float(retry_after_hours),
        "backoff_max": float(backoff_max_hours),
    }

    explore_n = 0
    if explore_fraction > 0:
        explore_n = min(max(1, math.floor(limit * explore_fraction)), limit)

    explored_rows: list[Any] = []
    if explore_n:
        explore_sql = (
            select_cols
            + from_where
            + " ORDER BY random() LIMIT %(explore_n)s FOR UPDATE OF r SKIP LOCKED"
        )
        explored_rows = conn.execute(
            explore_sql, {**params, "explore_n": explore_n}
        ).fetchall()

    remaining = limit - len(explored_rows)
    main_rows: list[Any] = []
    if remaining > 0:
        exclude_sql = (
            " AND r.ref_id <> ALL(%(explored_ids)s::bigint[])" if explored_rows else ""
        )
        main_sql = (
            select_cols
            + from_where
            + exclude_sql
            + """
         -- jsonb_exists(...) not the `?` operator: `?` collides with the
         -- param placeholder scan in a parameterised query.
         -- Quest striving weight (slice 2) tiers between the re-queue signal
         -- and newest-first: a stub serving a hot active quest jumps ahead.
         -- `prio` (stub_rank pass; 1=hottest..10=coldest, NULL=unranked)
         -- outranks both — the fetcher spends its budget on the hottest
         -- stubs first, falling back to the pre-prio tiebreak within ties.
         ORDER BY r.prio ASC NULLS LAST,
                  jsonb_exists(r.meta, 'oa_requeued') DESC,
                  COALESCE(qb.qw, 0) DESC,
                  r.ref_id DESC
         LIMIT %(remaining)s
           FOR UPDATE OF r SKIP LOCKED
            """
        )
        main_rows = conn.execute(
            main_sql,
            {
                **params,
                "explored_ids": [int(r[0]) for r in explored_rows],
                "remaining": remaining,
            },
        ).fetchall()

    rows = explored_rows + main_rows
    return [
        StubRef(
            ref_id=int(r[0]),
            doi=r[1],
            arxiv=r[2],
            s2_id=r[3],
            cite_key=r[4],
            retraction_status=r[5],
        )
        for r in rows
    ]


# ── Retraction gate ─────────────────────────────────────────────────
#
# Fetch-time retraction check: run once per claimed stub, *before* the
# (expensive) download cascade — a retracted paper has nothing worth
# chasing an OA copy for. Reuses ``precis.ingest.provenance.check_doi``
# (the module's one public entry point) for the actual Crossref fetch +
# severity classification (its ``_UPDATE_TYPE_MAP`` is the taxonomy)
# rather than a second, drifting copy of it.

_RETRACTION_GATE_SOURCE = "fetch_oa"


def _check_stub_retraction(stub: StubRef, *, mailto: str) -> Any:
    """Run the reused Crossref retraction check for one stub's own DOI.

    Returns the :class:`~precis.ingest.provenance.ProvenanceResult`, or
    ``None`` when there's nothing to decide from: no DOI on the stub, a
    malformed DOI, the DOI is unknown to Crossref, or the check itself
    errored (network blip, 5xx, …). The caller treats ``None`` as "not
    known to be retracted" and proceeds with the normal fetch cascade —
    a provenance-check outage must never block acquiring a PDF.

    ``check_doi`` is imported lazily so importing this module doesn't
    require the ``[paper]`` extra's ``habanero`` dependency at module
    load time — only a stub whose retraction actually gets checked
    needs it installed (same lazy-import pattern
    ``check_doi``/``_fetch_crossref_message`` themselves already use,
    for the same reason).

    Runs the ``store=None`` (classification-only) path deliberately: no
    notice-ref ingestion, no ``STATUS:*`` tag, no link write. That
    heavier write-through is an explicit-check concern (a manual
    ``precis provenance check-doi`` run), not something a background OA
    chase should trigger as a side effect on every claimed stub.
    """
    if not stub.doi or not _DOI_RE.match(stub.doi):
        return None
    from precis.ingest.provenance import check_doi

    try:
        result = check_doi(stub.doi, store=None, mailto=mailto or None)
    except Exception as exc:
        log.warning(
            "fetch_oa: retraction check errored for ref_id=%s doi=%s: %s",
            stub.ref_id,
            stub.doi,
            str(exc)[:200],
        )
        return None
    if result.status != "ok":
        return None
    return result


def _apply_retraction_gate(store: Any, stub: StubRef, *, mailto: str) -> bool:
    """Stamp the stub's provenance status; skip the cascade only if retracted.

    Returns ``True`` only when the stub is **retracted** — the caller
    must then skip the download cascade for this pass (a retracted paper
    has nothing worth chasing an OA copy for). ``corrected`` /
    ``expression_of_concern`` are informational: we stamp them so the
    reader banner shows the flag, but return ``False`` — we still want
    the PDF, now annotated. A clean stub (no gradeable notice) also
    returns ``False``.

    On a status *change* it persists ``refs.retraction_status`` via the
    existing ``Store.set_retraction_status`` (never a hand-written
    UPDATE — see ``store/_refs_ops.py``) and drops this worker's own
    ``ref_events`` breadcrumb (``source='fetch_oa'``): ``retraction_skip``
    for a retracted hit (why the cascade never ran) or ``provenance_flag``
    for a soft flag (why the fetch continued with an annotation).

    **Re-check bounding (design decision (a), OPEN-ITEMS).** A retracted
    stub is dropped by :func:`precis.store._stub_predicate.stub_predicate_sql`
    from every future claim — so that branch fires at most once. A
    corrected/EoC stub, by contrast, *stays* fetch-eligible (the predicate
    excludes only ``retracted``) until it acquires a PDF, so it re-runs
    this check on every pass — deliberately, to catch a later
    ``corrected → retracted`` upgrade. To keep that idempotent we compare
    the freshly-detected status against ``stub.retraction_status`` and
    only write + emit an event when it actually changed; an unchanged
    soft flag is a no-op, not a duplicate ``ref_events`` row each pass.

    ``propagate_to_findings`` is left on only for ``retracted``: restoring
    a citing finding to ``tracing`` and clearing its human-verified stamp
    is a *retraction* action — a mere correction/EoC must not taint a
    finding that walked through the paper. The soft-flag downstream
    (search downrank / citation flag) lands separately.
    """
    result = _check_stub_retraction(stub, mailto=mailto)
    if result is None:
        return False

    from precis.ingest.provenance import dominant_status

    statuses = [n.status for n in result.notices if n.status is not None]
    status = dominant_status(statuses)
    if status is None:
        return False

    retracted = status == "retracted"
    if status != stub.retraction_status:
        notices = [n for n in result.notices if n.status == status]
        notice_dois = [n.notice_doi for n in notices if n.notice_doi]
        dated = [n.notice_date for n in notices if n.notice_date is not None]
        store.set_retraction_status(
            stub.ref_id,
            status=status,
            retracted_at=min(dated) if dated else None,
            reason="; ".join(notice_dois) if notice_dois else None,
            url=f"https://doi.org/{notice_dois[0]}" if notice_dois else None,
            propagate_to_findings=retracted,
        )
        store.append_event(
            stub.ref_id,
            source=_RETRACTION_GATE_SOURCE,
            event="retraction_skip" if retracted else "provenance_flag",
            payload={
                "doi": stub.doi,
                "status": status,
                "notice_dois": notice_dois,
            },
        )
    return retracted


# ── Deterministic publisher PDF patterns ───────────────────────────
#
# Some OA publishers serve the full-text PDF at a URL derivable from
# the DOI alone — exactly like arXiv's ``/pdf/<id>.pdf``. The two
# aggregators miss these constantly on fresh DOIs: Unpaywall returns
# the HTML *landing page* as the "OA location" (our ``%PDF-`` guard
# rejects it → ``fetch_failed``) and S2 reports ``no_oa_version`` for
# days after publication. For publishers with a deterministic
# DOI→PDF endpoint we hit it directly and skip the aggregators.
#
# Each entry maps a DOI registrant prefix to a builder returning
# candidate PDF URLs (most-likely first). A prefix miss returns ``[]``
# and the cascade falls through to Unpaywall/arXiv/S2. Even a *hit* is
# only believed if the downloaded bytes start with ``%PDF-`` (the
# downloader's magic-byte guard), so a wrong guess for a paywalled
# article degrades to ``fetch_failed`` → fall-through — never a poison
# ingest. Keep the registry to patterns that are deterministic AND
# verified; a guessy entry only burns a fetch attempt and accelerates
# the stub's backoff.

# PLOS journal-code → site slug. The DOI infix
# (``10.1371/journal.<code>.<n>``) selects the journal subdomain path.
# Only the long-established journals are listed; an unrecognised code
# falls through (the newer PLOS titles can be added once their slug is
# verified — a wrong slug 404s to HTML and wastes an attempt).
_PLOS_JOURNAL_SLUGS = {
    "pone": "plosone",
    "pbio": "plosbiology",
    "pmed": "plosmedicine",
    "pgen": "plosgenetics",
    "pcbi": "ploscompbiol",
    "ppat": "plospathogens",
    "pntd": "plosntds",
}
_PLOS_CODE_RE = re.compile(r"^10\.1371/journal\.([a-z]+)\.")


def _springer_pdf_urls(doi: str) -> list[str]:
    """Springer full-text PDF endpoint.

    Covers BMC / SpringerOpen (``10.1186``, fully OA) and hybrid
    Springer (``10.1007``); for a paywalled Springer article the URL
    serves an HTML interstitial that the magic-byte guard rejects, so
    the hybrid prefix is safe to include — it lands the OA ones and
    falls through on the rest.
    """
    return [f"https://link.springer.com/content/pdf/{doi}.pdf"]


def _plos_pdf_urls(doi: str) -> list[str]:
    """PLOS printable PDF. Journal slug derived from the DOI infix;
    an unrecognised journal code falls through (empty list)."""
    m = _PLOS_CODE_RE.match(doi)
    if not m:
        return []
    slug = _PLOS_JOURNAL_SLUGS.get(m.group(1))
    if not slug:
        return []
    return [f"https://journals.plos.org/{slug}/article/file?id={doi}&type=printable"]


#: DOI registrant prefix → candidate-URL builder.
_PUBLISHER_PDF_PATTERNS: dict[str, Callable[[str], list[str]]] = {
    "10.1186": _springer_pdf_urls,  # BMC / SpringerOpen (OA)
    "10.1007": _springer_pdf_urls,  # Springer (hybrid; guard gates non-OA)
    "10.1371": _plos_pdf_urls,  # PLOS (OA)
}


def _publisher_pdf_urls(doi: str) -> list[str]:
    """Candidate deterministic PDF URLs for ``doi`` (possibly empty).

    Matches on the registrant prefix plus a ``/`` boundary so
    ``10.1186`` doesn't spuriously match ``10.11860/…``.
    """
    for prefix, builder in _PUBLISHER_PDF_PATTERNS.items():
        if doi.startswith(prefix + "/"):
            return builder(doi)
    return []


# ── Elsevier full-text API ─────────────────────────────────────────
#
# ScienceDirect (Elsevier) is the hard case the deterministic patterns
# can't touch: there's no keyless DOI→PDF URL (the PDF endpoint 403s
# bots and the PII isn't in the DOI), and Unpaywall/OpenAlex routinely
# return only the doi.org *landing page* for hybrid-OA Elsevier
# articles. The Article Retrieval API takes the DOI directly and, with
# an ``X-ELS-APIKey`` header + ``Accept: application/pdf``, streams the
# full-text PDF for entitled/OA content. Key-gated (free key from
# https://dev.elsevier.com); the leg is a silent no-op when unset.

#: DOI registrant prefixes routed to the Elsevier API. ``10.1016`` is
#: the dominant ScienceDirect prefix (Cell Press, The Lancet, and the
#: bulk of Elsevier journals all live under it); add legacy imprint
#: prefixes here as they surface.
_ELSEVIER_DOI_PREFIXES = frozenset({"10.1016"})

_ELSEVIER_ARTICLE_BASE = "https://api.elsevier.com/content/article/doi"


def _elsevier_api_key() -> str:
    """Elsevier API key from the env, or '' when unconfigured."""
    return (_secrets.get_secret("PRECIS_ELSEVIER_API_KEY") or "").strip()


def _is_elsevier_doi(doi: str) -> bool:
    """True when ``doi``'s registrant prefix is routed to Elsevier."""
    return any(doi.startswith(p + "/") for p in _ELSEVIER_DOI_PREFIXES)


# ── Wiley TDM API ──────────────────────────────────────────────────
#
# Wiley's Text & Data Mining service streams the full-text PDF by DOI
# for any article the token's institution is entitled to (incl. all
# gold-OA). Token-gated (``PRECIS_WILEY_TDM_TOKEN``); the leg is a
# silent no-op when unset. Like Elsevier it sidesteps the aggregators'
# landing-page miss — a direct, authoritative version-of-record PDF.

#: DOI registrant prefixes routed to Wiley. ``10.1002`` (Wiley core)
#: and ``10.1111`` (the legacy Blackwell imprint) cover the bulk.
_WILEY_DOI_PREFIXES = frozenset({"10.1002", "10.1111"})

_WILEY_TDM_BASE = "https://api.wiley.com/onlinelibrary/tdm/v1/articles"


def _wiley_tdm_token() -> str:
    """Wiley TDM client token from the env, or '' when unconfigured."""
    return (_secrets.get_secret("PRECIS_WILEY_TDM_TOKEN") or "").strip()


def _is_wiley_doi(doi: str) -> bool:
    """True when ``doi``'s registrant prefix is routed to Wiley."""
    return any(doi.startswith(p + "/") for p in _WILEY_DOI_PREFIXES)


# ── CORE green-OA aggregator ───────────────────────────────────────
#
# CORE harvests full-text PDFs from ~10k institutional/subject
# repositories — the green-OA copy of a paper whose publisher is
# paywalled. Key-gated (``PRECIS_CORE_API_KEY``). The search API
# returns per-work ``downloadUrl``s (repository bitstreams); we try
# the ones whose DOI matches. Repositories vary in bot-friendliness,
# so the ``%PDF-`` guard + multi-candidate fall-through earns its keep
# here. Positioned late — a net for what the publisher/OA legs miss.

_CORE_SEARCH_BASE = "https://api.core.ac.uk/v3/search/works/"


def _core_api_key() -> str:
    """CORE API key from the env, or '' when unconfigured."""
    return (_secrets.get_secret("PRECIS_CORE_API_KEY") or "").strip()


# ── OpenAlex Content API (paid, publisher-agnostic full text) ──────
#
# OpenAlex caches full-text content (PDF + GROBID TEI) for ~60M works and
# serves it from ``content.openalex.org`` — **not** the publisher host. That
# is the rescue for the anti-bot wall the free legs all 403 on: MDPI's Akamai,
# Wiley/science.org's Cloudflare. Key-gated (``PRECIS_OPENALEX_CONTENT_KEY``,
# free to obtain) and billed per file (~$0.01), so it is the last leg and only
# pays when OpenAlex reports content actually cached (``has_content``).
#
# One safety gate: the credential itself (silent no-op without it, like
# Elsevier/Wiley/CORE). The key is deliberate spend intent — configuring it on
# the single fetch host opts that host into the paid last-resort leg. (The old
# second gate, ``PRECIS_OPENALEX_CONTENT_AUTO``, was dropped 2026-07-16: it
# only ever bit on the one ``PRECIS_OA_FETCH=1`` host anyway, so it was
# redundant friction over the key.) A low-balance nursery alert
# (:func:`check_openalex_balance`) is the runway warning that replaces it. The
# manual one-shot (``precis fetch-openalex``) still calls the leg directly.

_OPENALEX_CONTENT_HOST = "content.openalex.org"

#: OpenAlex account rate-limit / credits endpoint. Keyed; reports the daily
#: ``credits_remaining`` for the account (content fetch = 100 credits each).
_OPENALEX_RATE_LIMIT_URL = "https://api.openalex.org/rate-limit"

#: Alert source + stable fingerprint for the low-balance condition. Shared
#: between the proactive :func:`check_openalex_balance` poll and the reactive
#: detector in :func:`_try_openalex_content` (gr162141) — same key, so
#: whichever trigger fires, the *other* path's healthy reading still
#: auto-resolves it. Deliberately: OpenAlex's ``/rate-limit`` endpoint
#: reports ``credits_remaining`` for the metered API, but paid content
#: downloads may draw against a separate prepaid USD wallet that endpoint
#: never surfaces — so the wallet can run dry (paid fetches failing) while
#: ``credits_remaining`` still looks healthy. The reactive trigger below is
#: the guaranteed-correct fallback: it fires straight off the paid fetch's
#: own failure, regardless of which balance model OpenAlex is actually
#: enforcing.
_OPENALEX_BALANCE_SOURCE = "fetch_oa:openalex_balance"
_OPENALEX_BALANCE_FINGERPRINT = "openalex-content-credits-low"

#: HTTP status OpenAlex plausibly uses to signal "no more balance" on the
#: paid content endpoint. 402 Payment Required is the *only* status treated
#: as a guaranteed-correct signal on its own — 429 Too Many Requests is
#: generic rate-limiting (the same file's ``_try_unpaywall`` classifies a
#: bare 429 as ``event="rate_limited"``) and must NOT raise a payment/quota
#: alert unless the response body actually says so
#: (:data:`_PAYMENT_QUOTA_SIGNAL_RE`); otherwise a transient throttle would
#: raise a spurious low-balance alert.
_PAYMENT_QUOTA_STATUS_CODES = frozenset({402})

#: Response-body / exception-message words that indicate a payment/quota
#: failure even on a status code we don't special-case above (this is how a
#: bare 429 — or any other status — still counts: only if the body is
#: payment-shaped). Loose but scoped to the vocabulary billing APIs actually
#: use — not so loose it'd match an ordinary 404/500.
_PAYMENT_QUOTA_SIGNAL_RE = re.compile(
    r"insufficient\s+(?:funds|credits?)|\bquota\b|\bpayment\b", re.IGNORECASE
)


def _payment_quota_signal(exc: Exception) -> str | None:
    """Return a short description when ``exc`` looks like a payment/quota
    failure on the paid OpenAlex content leg, else ``None``.

    Checks, in order: (1) an HTTP status of 402 on ``exc.response``
    (:data:`_PAYMENT_QUOTA_STATUS_CODES`) — the guaranteed-correct signal,
    true regardless of which balance model OpenAlex is actually enforcing;
    then (2) the response body (best-effort — a closed/unread stream just
    yields ``""``) or the exception's own message for the words insufficient
    funds/credits, quota, or payment (:data:`_PAYMENT_QUOTA_SIGNAL_RE`). A
    bare 429 (generic rate-limiting) or a plain 404/500/network error matches
    neither and returns ``None`` — the caller must not raise the low-balance
    alert off an unrelated failure or a transient throttle.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in _PAYMENT_QUOTA_STATUS_CODES:
        return f"HTTP {status_code}"
    body = ""
    if response is not None:
        try:
            body = response.text
        except Exception:
            body = ""
    if _PAYMENT_QUOTA_SIGNAL_RE.search(f"{body} {exc}"):
        return "response body mentioned insufficient funds/credits/quota/payment"
    return None


def _openalex_content_key() -> str:
    """OpenAlex Content API key from the env, or '' when unconfigured."""
    return (_secrets.get_secret("PRECIS_OPENALEX_CONTENT_KEY") or "").strip()


def _openalex_min_credits() -> int:
    """Raise the low-balance alert below this many daily content credits.

    Configurable via ``PRECIS_OPENALEX_MIN_CREDITS`` (raw credits); the default
    is ``_OPENALEX_MIN_FETCHES_DEFAULT`` paid fetches' worth (20 × 100 = 2000),
    so the alert fires while there's still runway to acquire papers, not only
    once the account is fully dry.
    """
    raw = os.environ.get("PRECIS_OPENALEX_MIN_CREDITS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            log.warning(
                "fetch_oa: ignoring non-integer PRECIS_OPENALEX_MIN_CREDITS=%r",
                raw,
            )
    return _OPENALEX_MIN_FETCHES_DEFAULT * _OPENALEX_CONTENT_CREDIT_COST


def _query_openalex_credits_remaining(api_key: str) -> int | None:
    """Return the account's daily ``credits_remaining`` from OpenAlex, or None.

    Hits the keyed ``/rate-limit`` endpoint. ``None`` on any failure or a
    missing/malformed number — the caller treats that as "can't read a balance"
    and clears rather than raises a spurious alert.
    """
    with httpx.Client(timeout=_API_TIMEOUT_S) as client:
        resp = client.get(_OPENALEX_RATE_LIMIT_URL, params={"api_key": api_key})
        resp.raise_for_status()
        data = resp.json()
    rl = data.get("rate_limit") or {}
    raw = rl.get("credits_remaining")
    return int(raw) if isinstance(raw, (int, float)) else None


def check_openalex_balance(store: Any, *, api_key: str) -> int | None:
    """Poll OpenAlex's ``credits_remaining`` and raise/clear the low-balance alert.

    Called once per fetch pass on the paid host (best-effort — never raises).
    Returns the observed ``credits_remaining`` (``None`` when the poll failed or
    the endpoint gave no number). When the balance is at or below
    :func:`_openalex_min_credits` a deduped ``warn`` alert is raised (source
    ``fetch_oa:openalex_balance``); when it recovers — or the poll can't read a
    balance — the alert auto-resolves, mirroring the nursery detector pattern.
    """
    if not api_key:
        return None
    remaining: int | None = None
    try:
        remaining = _query_openalex_credits_remaining(api_key)
    except Exception as exc:  # best-effort telemetry, never break the pass
        log.warning("fetch_oa: OpenAlex balance poll failed: %s", str(exc)[:200])

    floor = _openalex_min_credits()
    if remaining is not None and remaining <= floor:
        fetches_left = remaining // _OPENALEX_CONTENT_CREDIT_COST
        _raise_alert(
            store,
            source=_OPENALEX_BALANCE_SOURCE,
            fingerprint=_OPENALEX_BALANCE_FINGERPRINT,
            title=(
                f"OpenAlex content credits low: {remaining} left "
                f"(~{fetches_left} paid fetches)"
            ),
            detail=(
                f"credits_remaining={remaining} ≤ floor={floor}; each paid "
                f"content fetch costs {_OPENALEX_CONTENT_CREDIT_COST} credits. "
                "Top up at https://openalex.org/users or the paid last-resort "
                "leg will start returning fetch_failed / 429."
            ),
            severity="warn",
        )
        return remaining
    # Healthy, or unreadable — clear any open alert (empty live set).
    _resolve_alerts(store, source=_OPENALEX_BALANCE_SOURCE, live_fingerprints=[])
    return remaining


# ── Per-stub logic ─────────────────────────────────────────────────


def _try_publisher(
    stub: StubRef,
    *,
    inbox_dir: Path,
) -> FetchOutcome | None:
    """Try a deterministic publisher PDF URL for one stub.

    Returns ``None`` — a silent fall-through, no event, same contract
    as ``_try_arxiv`` for a missing arXiv id — when the stub has no
    DOI, the DOI is malformed, or its registrant prefix isn't in
    :data:`_PUBLISHER_PDF_PATTERNS`. When a pattern matches, downloads
    the first candidate whose bytes pass the ``%PDF-`` guard and
    records ``fetch_ok``; if every candidate fails the guard, records
    ``fetch_failed`` and the cascade falls through to the aggregators.
    """
    if not stub.doi or not _DOI_RE.match(stub.doi):
        return None
    urls = _publisher_pdf_urls(stub.doi)
    if not urls:
        return None

    t0 = time.perf_counter()
    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for url in urls:
        try:
            size_bytes = _download_pdf(url, target)
        except Exception as exc:
            errors.append(f"{url}: {str(exc)[:120]}")
            continue
        return FetchOutcome(
            event="fetch_ok",
            payload={
                "doi": stub.doi,
                "url": url,
                "size_bytes": size_bytes,
                "host_type": "publisher_pattern",
                "filename": filename,
            },
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_failed",
        payload={"doi": stub.doi, "urls": urls, "error": "; ".join(errors)[:200]},
        duration_ms=_ms(t0),
    )


def _try_elsevier(
    stub: StubRef,
    *,
    inbox_dir: Path,
    api_key: str,
) -> FetchOutcome | None:
    """Try the Elsevier Article Retrieval API for one stub.

    Returns ``None`` (silent fall-through, no event) when there's no
    API key, no DOI, the DOI is malformed, or its prefix isn't an
    Elsevier one. Otherwise hits ``content/article/doi/<doi>`` with the
    API key and ``Accept: application/pdf``; the ``%PDF-`` guard gates
    the result, so a non-entitled / non-OA article (which the API
    answers with an XML error body, not a PDF) degrades to
    ``fetch_failed`` and the cascade continues.
    """
    if not api_key or not stub.doi:
        return None
    if not _DOI_RE.match(stub.doi) or not _is_elsevier_doi(stub.doi):
        return None

    t0 = time.perf_counter()
    url = f"{_ELSEVIER_ARTICLE_BASE}/{stub.doi}"
    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(
            url,
            target,
            extra_headers={"X-ELS-APIKey": api_key, "Accept": "application/pdf"},
        )
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={"doi": stub.doi, "url": url, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_ok",
        payload={
            "doi": stub.doi,
            "url": url,
            "size_bytes": size_bytes,
            "host_type": "elsevier_api",
            "filename": filename,
        },
        duration_ms=_ms(t0),
    )


def _try_elsevier_markup(
    stub: StubRef,
    *,
    inbox_dir: Path,
    api_key: str,
) -> FetchOutcome | None:
    """Try the Elsevier Article Retrieval API for structured full-text XML.

    Markup-first sibling of :func:`_try_elsevier`: same endpoint, same
    key, but ``Accept: text/xml`` instead of ``application/pdf`` — a hit
    lands as an ``elsevier_xml`` markup trigger that
    :func:`precis.ingest.markup.parse_elsevier` walks section-by-section
    with the ``ce:``/``ja:`` tag profile, instead of relying on whatever
    Marker manages to OCR off the PDF view.

    Why this leg exists (gr162364 / gr162363): the PDF view can return a
    *complete, well-formed* ``%PDF-`` response that is nonetheless only
    the entitlement-limited preview — title, affiliations, highlights,
    keywords, a truncated abstract, no article body — with no error and
    nothing to distinguish it from a genuine full-text fetch, so it
    silently ingests as a handful of front-matter chunks. The XML view
    doesn't share that failure mode: a non-entitled DOI answers with an
    XML error/stub body that has no ``<body>`` (or an empty one), which
    :func:`~precis.ingest.markup.parse_elsevier` raises
    :class:`~precis.ingest.markup.MarkupParseError` on — a loud ingest
    failure instead of a silent short paper.

    Returns ``None`` (silent fall-through, no event) under the same
    conditions as :func:`_try_elsevier`: no key, no DOI, or a non-Elsevier
    DOI prefix. Gated by ``PRECIS_FETCH_MARKUP`` at the call site
    (:func:`_run_markup_cascade`) same as every other markup leg — ships
    dark until that flag is flipped.
    """
    if not api_key or not stub.doi:
        return None
    if not _DOI_RE.match(stub.doi) or not _is_elsevier_doi(stub.doi):
        return None

    t0 = time.perf_counter()
    url = f"{_ELSEVIER_ARTICLE_BASE}/{stub.doi}"
    filename = _stub_filename(stub) + ".xml"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_markup(
            url,
            target,
            extra_headers={"X-ELS-APIKey": api_key, "Accept": "text/xml"},
        )
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={"doi": stub.doi, "url": url, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_ok",
        payload={
            "doi": stub.doi,
            "url": url,
            "size_bytes": size_bytes,
            "host_type": "elsevier_api",
            "filename": filename,
            "format": "elsevier_xml",
        },
        duration_ms=_ms(t0),
    )


def _try_wiley(
    stub: StubRef,
    *,
    inbox_dir: Path,
    token: str,
) -> FetchOutcome | None:
    """Try the Wiley TDM API for one stub.

    Returns ``None`` (silent fall-through) without a token, DOI, or a
    Wiley DOI prefix. Otherwise streams ``tdm/v1/articles/<doi>`` with
    the client token; the ``%PDF-`` guard gates the result, so a
    non-entitled DOI (Wiley answers with an HTML / error body) degrades
    to ``fetch_failed`` and the cascade continues.
    """
    if not token or not stub.doi:
        return None
    if not _DOI_RE.match(stub.doi) or not _is_wiley_doi(stub.doi):
        return None

    t0 = time.perf_counter()
    url = f"{_WILEY_TDM_BASE}/{stub.doi}"
    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(
            url, target, extra_headers={"Wiley-TDM-Client-Token": token}
        )
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={"doi": stub.doi, "url": url, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_ok",
        payload={
            "doi": stub.doi,
            "url": url,
            "size_bytes": size_bytes,
            "host_type": "wiley_tdm",
            "filename": filename,
        },
        duration_ms=_ms(t0),
    )


def _try_unpaywall(
    stub: StubRef,
    *,
    inbox_dir: Path,
    email: str,
) -> FetchOutcome | None:
    """Try Unpaywall for one stub.

    Returns ``None`` when there's no DOI to try (the cascade falls
    through to the next provider). Returns a :class:`FetchOutcome`
    otherwise — caller writes the corresponding event and stops the
    cascade on ``fetch_ok``.
    """
    if not stub.doi:
        return None  # no DOI → not Unpaywall's problem
    t0 = time.perf_counter()

    if not _DOI_RE.match(stub.doi):
        return FetchOutcome(
            event="invalid_identifier",
            payload={"doi": stub.doi},
            duration_ms=_ms(t0),
        )

    try:
        data = _query_unpaywall(stub.doi, email=email)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            return FetchOutcome(
                event="rate_limited",
                payload={
                    "doi": stub.doi,
                    "retry_after": exc.response.headers.get("retry-after"),
                },
                duration_ms=_ms(t0),
            )
        return FetchOutcome(
            event="api_error",
            payload={"doi": stub.doi, "status": exc.response.status_code},
            duration_ms=_ms(t0),
        )
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"doi": stub.doi, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )

    oa = (data or {}).get("best_oa_location") or {}
    url = oa.get("url_for_pdf") or oa.get("url")
    if not url:
        return FetchOutcome(
            event="no_oa_version",
            payload={
                "doi": stub.doi,
                "is_oa": (data or {}).get("is_oa"),
                "oa_status": (data or {}).get("oa_status"),
            },
            duration_ms=_ms(t0),
        )

    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(url, target)
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={"doi": stub.doi, "url": url, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )

    return FetchOutcome(
        event="fetch_ok",
        payload={
            "doi": stub.doi,
            "url": url,
            "size_bytes": size_bytes,
            "license": oa.get("license"),
            "host_type": oa.get("host_type"),
            "version": oa.get("version"),
            "filename": filename,
        },
        duration_ms=_ms(t0),
    )


def _try_arxiv(
    stub: StubRef,
    *,
    inbox_dir: Path,
) -> FetchOutcome | None:
    """Try direct arXiv PDF fetch for one stub.

    arXiv hosts every preprint as a PDF at a deterministic URL:
    ``https://arxiv.org/pdf/<id>.pdf`` (also accepts versioned ids
    like ``2401.12345v2``). No API key, no metadata round-trip,
    no rate-limit issues for occasional fetches.

    Returns ``None`` when the stub has no arXiv id (cascade falls
    through). When the id is present, attempts download and writes
    the outcome.
    """
    if not stub.arxiv:
        return None
    t0 = time.perf_counter()
    arxiv_id = stub.arxiv.strip()
    # Accept ``arxiv:`` prefixed values defensively.
    arxiv_id = arxiv_id.removeprefix("arxiv:")
    if not _ARXIV_ID_RE.match(arxiv_id):
        return FetchOutcome(
            event="invalid_identifier",
            payload={"arxiv": stub.arxiv},
            duration_ms=_ms(t0),
        )

    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(url, target)
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={"arxiv": arxiv_id, "url": url, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_ok",
        payload={
            "arxiv": arxiv_id,
            "url": url,
            "size_bytes": size_bytes,
            "license": "arxiv",  # whatever arXiv licence the author chose
            "host_type": "preprint",
            "filename": filename,
        },
        duration_ms=_ms(t0),
    )


def _try_s2(
    stub: StubRef,
    *,
    inbox_dir: Path,
) -> FetchOutcome | None:
    """Try Semantic Scholar's ``openAccessPdf`` for one stub.

    Uses the existing :mod:`semanticscholar` client (already in
    the dep tree via :mod:`precis.ingest.citations`). S2 indexes
    OA PDFs across publishers + repositories with comparable
    coverage to Unpaywall — useful as a fallback when Unpaywall
    returns ``no_oa_version`` (the two sources don't agree 100%
    on what's OA).

    Identifier priority: DOI > arXiv > S2 id (any single one is
    sufficient for ``get_paper``).
    """
    paper_id_for_s2 = None
    if stub.doi:
        paper_id_for_s2 = f"doi:{stub.doi}"
    elif stub.arxiv:
        paper_id_for_s2 = f"ARXIV:{stub.arxiv.removeprefix('arxiv:')}"
    elif stub.s2_id:
        paper_id_for_s2 = stub.s2_id
    if paper_id_for_s2 is None:
        return None

    t0 = time.perf_counter()
    try:
        oa_url = _query_s2_openaccess(paper_id_for_s2)
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"paper_id": paper_id_for_s2, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )

    if not oa_url:
        return FetchOutcome(
            event="no_oa_version",
            payload={"paper_id": paper_id_for_s2},
            duration_ms=_ms(t0),
        )

    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(oa_url, target)
    except Exception as exc:
        return FetchOutcome(
            event="fetch_failed",
            payload={
                "paper_id": paper_id_for_s2,
                "url": oa_url,
                "error": str(exc)[:200],
            },
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_ok",
        payload={
            "paper_id": paper_id_for_s2,
            "url": oa_url,
            "size_bytes": size_bytes,
            "host_type": "s2_openaccess",
            "filename": filename,
        },
        duration_ms=_ms(t0),
    )


def _try_openalex_content(
    stub: StubRef,
    *,
    inbox_dir: Path,
    api_key: str,
    store: Any = None,
    email: str = "",
) -> FetchOutcome | None:
    """Fetch full text from OpenAlex's content cache for one stub.

    Returns ``None`` (silent fall-through) without a key, DOI, or a valid
    DOI. Otherwise reads the **free** work metadata to learn what content
    OpenAlex has cached (``has_content`` / ``content_urls``); when a PDF is
    cached, downloads it from ``content.openalex.org`` (paid, ``?api_key=``)
    into the inbox. Because the bytes come from OpenAlex — not the publisher
    — this is the one leg that clears the MDPI/Akamai (and Wiley/Cloudflare)
    403 wall the free legs die on.

    **Phase 1: PDF only.** OpenAlex also serves GROBID **TEI**
    (``grobid_xml``), which is structurally richer, but its structured-ingest
    seam isn't built yet (OPEN-ITEMS OA #5), so we take the PDF (→ Marker)
    even when TEI exists. When only TEI is cached (no PDF), records
    ``no_oa_version`` for now — Phase 2 will fetch the TEI instead.

    The ``api_key`` rides in the URL query, not the payload — the recorded
    event keeps the clean (keyless) ``pdf_url`` so the credential never lands
    in ``ref_events``.

    **Reactive balance alert (gr162141).** The paid download's failure path
    additionally runs it through :func:`_payment_quota_signal`; a
    402-or-payment-shaped failure raises the *same* ``fetch_oa:openalex_balance``
    alert :func:`check_openalex_balance` uses (same source + fingerprint, so
    either path's healthy reading auto-resolves it), with a detail line that
    says ``"paid fetch failed with payment/quota signal …"`` so it reads as
    the reactive trigger, distinct from the proactive credits-remaining poll.
    A bare 429 with no payment-shaped body does *not* raise this alert — it's
    generic rate-limiting, same as ``_try_unpaywall``'s ``rate_limited``
    classification, not a wallet/balance signal.
    This exists because a paid content download can draw against a separate
    prepaid balance the ``/rate-limit`` endpoint never reports — so
    ``credits_remaining`` can look healthy while that wallet is dry and every
    paid fetch is failing. Best-effort and non-fatal: the fetch still records
    ``fetch_failed`` exactly as before either way.
    """
    if not api_key or not stub.doi or not _DOI_RE.match(stub.doi):
        return None
    t0 = time.perf_counter()
    try:
        content = _query_openalex_content_urls(stub.doi, email=email)
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"doi": stub.doi, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )

    pdf_url = content.get("pdf")
    if not pdf_url:
        # Nothing cached, or only TEI (Phase 2). Either way no PDF to pull.
        return FetchOutcome(
            event="no_oa_version",
            payload={"doi": stub.doi, "cached": sorted(content)},
            duration_ms=_ms(t0),
        )

    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        size_bytes = _download_pdf(_with_api_key(pdf_url, api_key), target)
    except Exception as exc:
        # httpx folds the *full* request URL — with ``?api_key=…`` — into its
        # error message, so scrub the key before it lands in ``ref_events`` /
        # the CLI. The recorded ``url`` is the clean keyless one.
        err = str(exc).replace(api_key, "***") if api_key else str(exc)
        signal = _payment_quota_signal(exc)
        if signal is not None and store is not None:
            # Reactive trigger (gr162141): the paid content download itself
            # failed with a payment/quota shape. Raises the *same* alert key
            # as check_openalex_balance's proactive credits_remaining poll —
            # a separate prepaid wallet can be dry (paid fetches failing)
            # while /rate-limit still reports a healthy credits_remaining, so
            # this is the guaranteed-correct fallback regardless of which
            # balance model is actually gating the account. Best-effort:
            # never raises, and the fetch still records fetch_failed below.
            # ``store`` is None for the manual one-shot CLI
            # (``precis fetch-openalex``), which has no nursery-alert flow —
            # skip the alert there rather than raise on a missing store.
            try:
                _raise_alert(
                    store,
                    source=_OPENALEX_BALANCE_SOURCE,
                    fingerprint=_OPENALEX_BALANCE_FINGERPRINT,
                    title="OpenAlex paid content fetch failed with a payment/quota signal",
                    detail=(
                        f"paid fetch failed with payment/quota signal {signal} "
                        f"(doi={stub.doi}): {err[:200]}. Reactive trigger — "
                        "distinct from the proactive credits_remaining poll "
                        "(check_openalex_balance); a separate prepaid content "
                        "wallet may be dry even though /rate-limit still "
                        "reports a healthy balance. Top up at "
                        "https://openalex.org/users."
                    ),
                    severity="warn",
                )
            except Exception as alert_exc:  # pragma: no cover — defensive
                log.warning(
                    "fetch_oa: reactive openalex_balance alert failed: %s",
                    str(alert_exc)[:200],
                )
        return FetchOutcome(
            event="fetch_failed",
            payload={"doi": stub.doi, "url": pdf_url, "error": err[:200]},
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_ok",
        payload={
            "doi": stub.doi,
            "url": pdf_url,
            "size_bytes": size_bytes,
            "host_type": "openalex_content",
            "filename": filename,
        },
        duration_ms=_ms(t0),
        cost_usd=_OPENALEX_CONTENT_COST_USD,
    )


def _try_crossref(
    stub: StubRef,
    *,
    inbox_dir: Path,
    email: str,
) -> FetchOutcome | None:
    """Try Crossref's TDM full-text links for one stub.

    Crossref metadata carries a ``link[]`` array; entries tagged
    ``intended-application: text-mining`` hold the publisher's own
    full-text URL + content-type. We try the ones whose content-type
    is a PDF — for fully-OA publishers that's a direct, fetchable PDF
    across many imprints we haven't special-cased. Needs the polite
    ``mailto`` (Crossref connection-resets the anonymous pool), so the
    leg is a silent no-op when no email is configured.
    """
    if not email or not stub.doi or not _DOI_RE.match(stub.doi):
        return None
    t0 = time.perf_counter()
    try:
        pdf_urls = _query_crossref_pdf_links(stub.doi, email=email)
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"doi": stub.doi, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )
    if not pdf_urls:
        return FetchOutcome(
            event="no_oa_version",
            payload={"doi": stub.doi},
            duration_ms=_ms(t0),
        )
    return _download_first(
        stub, pdf_urls, inbox_dir=inbox_dir, host_type="crossref_tdm", t0=t0
    )


def _try_openalex(
    stub: StubRef,
    *,
    inbox_dir: Path,
    email: str,
) -> FetchOutcome | None:
    """Try OpenAlex's PDF locations for one stub.

    OpenAlex indexes OA locations like Unpaywall but often surfaces a
    different (working) ``pdf_url`` — a green-OA repository copy where
    Unpaywall returned only the publisher landing page. Keyless;
    ``mailto`` is polite but optional.
    """
    if not stub.doi or not _DOI_RE.match(stub.doi):
        return None
    t0 = time.perf_counter()
    try:
        pdf_urls = _query_openalex_pdf_urls(stub.doi, email=email)
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"doi": stub.doi, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )
    if not pdf_urls:
        return FetchOutcome(
            event="no_oa_version",
            payload={"doi": stub.doi},
            duration_ms=_ms(t0),
        )
    return _download_first(
        stub, pdf_urls, inbox_dir=inbox_dir, host_type="openalex", t0=t0
    )


def _try_europepmc(
    stub: StubRef,
    *,
    inbox_dir: Path,
) -> FetchOutcome | None:
    """Try Europe PMC's OA full-text PDF for one stub.

    Resolves the DOI to a PMCID via the search API, and — when the
    record is in the OA subset — downloads the rendered PDF. High-yield
    for biomedical papers that S2/Unpaywall miss. Keyless.
    """
    if not stub.doi or not _DOI_RE.match(stub.doi):
        return None
    t0 = time.perf_counter()
    try:
        pmcid = _query_europepmc_oa_pmcid(stub.doi)
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"doi": stub.doi, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )
    if not pmcid:
        return FetchOutcome(
            event="no_oa_version",
            payload={"doi": stub.doi},
            duration_ms=_ms(t0),
        )
    url = f"https://europepmc.org/articles/{pmcid}?pdf=render"
    return _download_first(
        stub, [url], inbox_dir=inbox_dir, host_type="europepmc", t0=t0
    )


def _try_core(
    stub: StubRef,
    *,
    inbox_dir: Path,
    api_key: str,
) -> FetchOutcome | None:
    """Try CORE's green-OA repository copies for one stub.

    Returns ``None`` (silent fall-through) without a key or DOI.
    Searches CORE for the DOI and downloads the first repository
    ``downloadUrl`` that passes the ``%PDF-`` guard — repositories
    bot-block unevenly, so a browser UA is sent and candidates are
    tried in turn.
    """
    if not api_key or not stub.doi or not _DOI_RE.match(stub.doi):
        return None
    t0 = time.perf_counter()
    try:
        pdf_urls = _query_core_fulltext_urls(stub.doi, api_key=api_key)
    except Exception as exc:
        return FetchOutcome(
            event="api_error",
            payload={"doi": stub.doi, "error": str(exc)[:200]},
            duration_ms=_ms(t0),
        )
    if not pdf_urls:
        return FetchOutcome(
            event="no_oa_version",
            payload={"doi": stub.doi},
            duration_ms=_ms(t0),
        )
    return _download_first(
        stub,
        pdf_urls,
        inbox_dir=inbox_dir,
        host_type="core",
        t0=t0,
        extra_headers={"User-Agent": _BROWSER_UA},
    )


def _download_first(
    stub: StubRef,
    urls: list[str],
    *,
    inbox_dir: Path,
    host_type: str,
    t0: float,
    extra_headers: dict[str, str] | None = None,
) -> FetchOutcome:
    """Download the first URL whose bytes pass the ``%PDF-`` guard.

    Shared tail for the legs that resolve a list of candidate PDF URLs
    (publisher patterns aside, which inline this). Returns ``fetch_ok``
    on the first good PDF, else ``fetch_failed`` with the per-URL
    errors so the cascade falls through. ``extra_headers`` (e.g. a
    browser UA for repository hosts) is forwarded to every candidate.
    """
    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for url in urls:
        try:
            size_bytes = _download_pdf(url, target, extra_headers=extra_headers)
        except Exception as exc:
            errors.append(f"{url}: {str(exc)[:120]}")
            continue
        return FetchOutcome(
            event="fetch_ok",
            payload={
                "doi": stub.doi,
                "url": url,
                "size_bytes": size_bytes,
                "host_type": host_type,
                "filename": filename,
            },
            duration_ms=_ms(t0),
        )
    return FetchOutcome(
        event="fetch_failed",
        payload={"doi": stub.doi, "urls": urls, "error": "; ".join(errors)[:200]},
        duration_ms=_ms(t0),
    )


# ── Runner ─────────────────────────────────────────────────────────


def _oa_fetch_enabled() -> bool:
    """Env gate for the OA fetcher. Default off.

    Tolerant to whitespace / case so a YAML quoting quirk or trailing
    newline doesn't silently disable the pass (same shape as
    ``fetch_google_patents._is_enabled``).
    """
    return os.environ.get("PRECIS_OA_FETCH", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def run_oa_fetch_pass(
    store: Any,
    *,
    limit: int = 8,
    inbox_dir: Path | str | None = None,
    email: str | None = None,
    api_key: str | None = None,
    wiley_token: str | None = None,
    core_key: str | None = None,
    openalex_content_key: str | None = None,
) -> dict[str, int]:
    """Process up to ``limit`` stubs through the OA fetcher cascade.

    Per stub: first the retraction gate (:func:`_apply_retraction_gate`)
    — a retracted paper skips straight to the next stub, no download leg
    ever runs. Otherwise tries publisher → Elsevier → Wiley → Unpaywall →
    Crossref → OpenAlex → Europe PMC → CORE → arXiv → S2 in order.
    The cascade stops at the first ``fetch_ok``; intermediate
    ``no_oa_version`` / ``fetch_failed`` outcomes still produce an
    audit event and fall through to the next source.
    ``invalid_identifier`` / ``identifier_missing`` outcomes also
    fall through.

    ``api_key`` / ``wiley_token`` / ``core_key`` default to
    ``PRECIS_ELSEVIER_API_KEY`` / ``PRECIS_WILEY_TDM_TOKEN`` /
    ``PRECIS_CORE_API_KEY``; each leg is skipped without its
    credential.

    Each stub is tried independently; an unhandled exception in
    one provider logs ``api_error`` for that source and moves on
    to the next. Returns the BatchResult shape: ``{claimed, ok,
    failed}``. ``ok`` counts stubs the cascade processed without
    a fatal exception (regardless of whether a PDF actually
    landed); ``failed`` counts only unhandled exceptions that
    escaped all three providers.

    ``email`` defaults to ``PRECIS_UNPAYWALL_EMAIL``. Without one,
    Unpaywall is skipped (with an ``identifier_missing``-style
    event) — arXiv and S2 still run.

    ``inbox_dir`` is taken from ``PRECIS_WATCH_INBOX`` when not passed
    explicitly. It **must** be the directory the ``precis watch``
    daemon scans — otherwise the bytes download fine (``fetch_ok``) but
    no watcher ever ingests them and the stub stays claimable,
    re-fetching every pass forever. In the cluster,
    ``PRECIS_WATCH_INBOX`` is wired to ``papers_inbox_path`` (the NAS
    inbox) so fetcher and watcher share one source of truth. There is
    deliberately **no HOME-relative fallback**: an unset
    ``PRECIS_WATCH_INBOX`` used to default to
    ``~/work/new_papers/_oa_fetched`` that no watcher scanned — under
    the daemon's read-only ``HOME=/Users/deploy`` that black-holed
    every download (``fetch_ok`` with ``pdf_sha256`` never set) *and*
    spammed ``[Errno 13] Permission denied`` per cascade leg. We now
    refuse the pass loudly instead (see the guard below).

    Gated by env: when ``PRECIS_OA_FETCH`` is unset or ``"0"`` the
    pass exits immediately with claimed=0. The fetcher only needs to
    run on **one** host (whichever can write the shared inbox) — the
    watchers race the inbox, so a single fetcher feeds them all. This
    mirrors ``gp_fetch``'s ``PRECIS_GP_FETCH`` single-host pin and
    keeps every other node from re-claiming the same stubs.
    """
    if not _oa_fetch_enabled():
        return {"claimed": 0, "ok": 0, "failed": 0}
    email = email or (settings.get_str("contact.polite_email") or "").strip()
    api_key = api_key if api_key is not None else _elsevier_api_key()
    wiley_token = wiley_token if wiley_token is not None else _wiley_tdm_token()
    core_key = core_key if core_key is not None else _core_api_key()
    openalex_content_key = (
        openalex_content_key
        if openalex_content_key is not None
        else _openalex_content_key()
    )
    if inbox_dir is None:
        configured = os.environ.get("PRECIS_WATCH_INBOX", "").strip()
        if not configured:
            # No inbox → no watcher will ever ingest what we download.
            # Refuse rather than black-hole into a HOME-relative dir
            # (the pre-2026-06-19 footgun behind stuck stub #34736).
            log.error(
                "fetch_oa: PRECIS_WATCH_INBOX is unset — skipping pass. "
                "The fetcher must download into the directory `precis "
                "watch` scans; there is no safe default. Set "
                "PRECIS_WATCH_INBOX to the shared watch inbox."
            )
            return {"claimed": 0, "ok": 0, "failed": 0}
        inbox_dir = configured
    inbox_path = Path(inbox_dir)

    # Fail fast on an unwritable inbox (e.g. the NAS is unmounted) with a
    # single clear log line, instead of letting every cascade leg's
    # `inbox_dir.mkdir(...)` raise `[Errno 13] Permission denied` and
    # bury the ref under hundreds of per-leg `api_error` events (the
    # #34736 spam signature).
    try:
        inbox_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error(
            "fetch_oa: watch inbox %s is not writable (%s) — skipping "
            "pass. Downloads would be lost; check the mount / permissions.",
            inbox_path,
            exc,
        )
        return {"claimed": 0, "ok": 0, "failed": 0}

    # gr170366: once per pass, sweep any ``.staging`` entries orphaned by an
    # outer exception that fired between a markup stage and its publish
    # (see _sweep_stale_staging). Best-effort — never blocks the fetch.
    try:
        _sweep_stale_staging(inbox_path)
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("fetch_oa: staging sweep errored: %s", exc)

    with store.pool.connection() as conn:
        stubs = claim_stubs_to_fetch(conn, limit=limit)

    if not stubs:
        return {"claimed": 0, "ok": 0, "failed": 0}

    # Once per working pass, check the paid leg's runway and raise/clear the
    # low-balance alert. Best-effort — a failed poll never blocks the fetch.
    if openalex_content_key:
        check_openalex_balance(store, api_key=openalex_content_key)

    ok = 0
    failed = 0
    total = len(stubs)
    for i, stub in enumerate(stubs, start=1):
        # gr191337-style monopolization is exactly what this pass is prone
        # to (up to ~11 network legs per stub, each capped at
        # ``_DOWNLOAD_TIMEOUT_S``) — a per-stub progress stamp is the whole
        # point of ``precis.workers.activity`` (see its module docstring).
        activity.note(f"stub {i}/{total} ref_id={stub.ref_id} doi={stub.doi or '-'}")
        try:
            # Retraction gate — cheapest possible check, ahead of every
            # other leg (including the markup-first pass): a retracted
            # paper has nothing worth chasing an OA copy for, so decide
            # this before paying for any download attempt.
            if _apply_retraction_gate(store, stub, mailto=email):
                ok += 1
                continue

            # Markup-first pass (gated by PRECIS_FETCH_MARKUP): try for a
            # structured full-text source before the PDF cascade. Best-
            # effort — its failure must never block the PDF fetch, which
            # runs unconditionally afterwards to acquire the printable.
            staged_markup: _StagedMarkup | None = None
            try:
                staged_markup = _run_markup_cascade(store, stub, inbox_path, api_key)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "fetch_oa: markup pass errored for ref_id=%s: %s",
                    stub.ref_id,
                    exc,
                )
            # gr161905: when markup already landed, the companion PDF this
            # cascade drops must never be OCR'd as a body candidate — no
            # matter which host's watcher reaches it first, it's the
            # printable only. See _run_cascade's `printable_only` and
            # FetchSidecar.printable_only.
            pdf_path = _run_cascade(
                store,
                stub,
                inbox_path,
                email,
                api_key,
                wiley_token,
                core_key,
                openalex_content_key,
                printable_only=staged_markup is not None,
            )
            if staged_markup is not None:
                # gr170349: only now — after the companion PDF's fate is
                # known, however long that took — does the markup trigger
                # (and its sidecar) become watcher-visible, atomically.
                companion_name = pdf_path.name if pdf_path is not None else None
                _publish_markup_trigger(staged_markup, companion_name)
            ok += 1
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "fetch_oa: ref_id=%s unhandled error: %s",
                stub.ref_id,
                exc,
                exc_info=True,
            )
            failed += 1
    return {"claimed": len(stubs), "ok": ok, "failed": failed}


def _markup_fetch_enabled() -> bool:
    """Whether the markup-first pass runs. Gated by ``PRECIS_FETCH_MARKUP``.

    Default-off: markup ingest is new; opt in per-host once the stub
    backlog has been exercised. See docs/backlog/markup-first-ingest.md.
    """
    return os.environ.get("PRECIS_FETCH_MARKUP", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


_MARKUP_ACCEPT = (
    "application/xml,text/xml,application/jats+xml,text/html,"
    "application/x-eprint-tar,application/gzip,*/*;q=0.8"
)


def _download_markup(
    url: str, target: Path, *, extra_headers: dict[str, str] | None = None
) -> int:
    """Stream a markup document (XML/HTML/tarball) to ``target``.

    Mirrors :func:`_download_pdf` but without the ``%PDF-`` magic-byte
    guard — the producer validates structure at ingest and falls back to
    OCR on a bad parse, so a landing-page here is caught downstream
    rather than poisoning the corpus. Writes to ``<target>.part`` and
    renames on success. Returns the byte count; raises on an empty body.
    """
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_stream

    tmp = target.with_suffix(target.suffix + ".part")
    size = 0
    headers = {"User-Agent": _user_agent_header(), "Accept": _MARKUP_ACCEPT}
    if extra_headers:
        headers.update(extra_headers)
    with http_client(
        timeout=_DOWNLOAD_TIMEOUT_S,
        headers=headers,
        user_agent=None,
    ) as client:
        with safe_stream(client, "GET", url) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > _MAX_DOWNLOAD_BYTES
            ):
                raise ValueError(
                    f"response too large (Content-Length={declared} bytes, "
                    f"cap={_MAX_DOWNLOAD_BYTES}) — skipped, disk-fill guard"
                )
            over_cap = False
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    fh.write(chunk)
                    size += len(chunk)
                    if size > _MAX_DOWNLOAD_BYTES:
                        over_cap = True
                        break
    if over_cap:
        tmp.unlink(missing_ok=True)
        raise ValueError(
            f"response exceeded {_MAX_DOWNLOAD_BYTES} byte cap mid-stream "
            "(disk-fill guard)"
        )
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise ValueError("empty markup response")
    tmp.rename(target)
    return size


@dataclass(frozen=True)
class _MarkupLeg:
    """One markup-first source: how to build its URL and name its file."""

    source: str
    fmt: str
    #: Filename suffix for the downloaded trigger (drives watcher routing).
    suffix: str
    #: Returns the fetch URL for ``stub``, or ``None`` to skip this leg.
    url_for: Callable[[StubRef], str | None]


def _europepmc_jats_url(stub: StubRef) -> str | None:
    if not stub.doi:
        return None
    pmcid = _query_europepmc_oa_pmcid(stub.doi)
    if not pmcid:
        return None
    return f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"


def _normalize_arxiv_id(raw: str | None) -> str | None:
    """Validated bare arXiv id (``arxiv:`` prefix stripped), else ``None``.

    Mirrors the guard in :func:`_try_arxiv` so the markup legs accept the
    same ids the PDF leg does.
    """
    if not raw:
        return None
    arxiv_id = raw.strip().removeprefix("arxiv:")
    return arxiv_id if _ARXIV_ID_RE.match(arxiv_id) else None


def _arxiv_html_url(stub: StubRef) -> str | None:
    arxiv_id = _normalize_arxiv_id(stub.arxiv)
    return f"https://arxiv.org/html/{arxiv_id}" if arxiv_id else None


def _arxiv_source_url(stub: StubRef) -> str | None:
    arxiv_id = _normalize_arxiv_id(stub.arxiv)
    return f"https://arxiv.org/e-print/{arxiv_id}" if arxiv_id else None


#: Identifier-driven markup legs, tried in this order after Elsevier
#: (key-gated, tried separately — see :func:`_run_markup_cascade`): JATS
#: (best structure) → arXiv HTML (LaTeXML, JATS-class) → arXiv source
#: (flatten-and-chunk fallback).
_MARKUP_LEGS: tuple[_MarkupLeg, ...] = (
    _MarkupLeg(_SOURCE_EUROPEPMC_JATS, "jats", ".xml", _europepmc_jats_url),
    _MarkupLeg(_SOURCE_ARXIV_HTML, "arxiv_html", ".html", _arxiv_html_url),
    _MarkupLeg(_SOURCE_ARXIV_SOURCE, "latex", ".tar.gz", _arxiv_source_url),
)


@dataclass(frozen=True)
class _StagedMarkup:
    """A markup trigger + sidecar written to the ``.staging`` subdir.

    Not yet watcher-visible: ``staged_path`` lives under
    ``inbox_dir / ".staging"``, out of the watched tree. ``final_path`` is
    where it will be atomically published (``os.replace``) once the
    companion PDF's fate is known — see :func:`_publish_markup_trigger`.
    Closes gr170349 (watcher racing a sidecar with ``companion_pdf: null``).
    """

    staged_path: Path
    final_path: Path


def _run_markup_cascade(
    store: Any, stub: StubRef, inbox_dir: Path, elsevier_api_key: str = ""
) -> _StagedMarkup | None:
    """Try each markup leg; on first hit drop the trigger + sidecar into
    ``inbox_dir / ".staging"`` — not yet watcher-visible.

    Returns the staged trigger (so the caller knows the chunk source is
    structured, though it still runs the PDF cascade for the printable —
    and can then atomically publish this trigger, marking the companion
    PDF attach-only and linking it back, gr161905/gr170349). Each leg
    records its own ``ref_events`` row. Best-effort: any leg exception logs
    ``api_error`` and falls through.

    The sidecar carries the stub's ``ref_id`` (the fold target) and the
    leg's ``source_format`` so the watcher builds a ``MarkupInput``; the
    companion PDF the PDF cascade drops later folds into the same ref via
    this same ``ref_id`` and attaches as the printable (see the
    db_writer attach-only guard) — deterministically, never as a second
    body candidate, since the caller tags it ``printable_only``.

    Elsevier is tried first, ahead of the generic ``_MARKUP_LEGS`` list:
    it's key-gated like the PDF leg (not identifier-driven like the other
    three), so it doesn't fit the plain ``url_for(stub)`` shape those
    share. See :func:`_try_elsevier_markup` for why this leg exists.
    """
    if not _markup_fetch_enabled():
        return None

    # Both legs below stage their content download *and* sidecar under
    # ``.staging`` — unwatched — so nothing watcher-visible appears until
    # the caller atomically publishes it once the companion PDF's fate is
    # known (gr170349).
    staging_dir = inbox_dir / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        elsevier_outcome = _try_elsevier_markup(
            stub, inbox_dir=staging_dir, api_key=elsevier_api_key
        )
    except Exception as exc:  # pragma: no cover — defensive
        elsevier_outcome = None
        log.warning(
            "fetch_oa markup: elsevier_xml leg errored for ref_id=%s: %s",
            stub.ref_id,
            exc,
        )
    if elsevier_outcome is not None:
        store.append_event(
            stub.ref_id,
            source=_SOURCE_ELSEVIER_XML,
            event=elsevier_outcome.event,
            payload=elsevier_outcome.payload,
            duration_ms=elsevier_outcome.duration_ms,
        )
        if elsevier_outcome.event == "fetch_ok":
            final_path = inbox_dir / (_stub_filename(stub) + ".xml")
            staged_path = staging_dir / final_path.name
            write_sidecar(
                staged_path,
                ref_id=stub.ref_id,
                identifiers={
                    k: v
                    for k, v in {
                        "doi": stub.doi,
                        "arxiv": stub.arxiv,
                        "s2": stub.s2_id,
                        "cite_key": stub.cite_key,
                    }.items()
                    if v
                },
                source=_SOURCE_ELSEVIER_XML,
                source_format="elsevier_xml",
            )
            log.info(
                "fetch_oa markup: elsevier_xml landed for ref_id=%s (%d bytes)",
                stub.ref_id,
                elsevier_outcome.payload.get("size_bytes", 0),
            )
            return _StagedMarkup(staged_path=staged_path, final_path=final_path)

    log.debug(
        "fetch_oa markup: trying %d legs for ref_id=%s (doi=%s arxiv=%s)",
        len(_MARKUP_LEGS),
        stub.ref_id,
        stub.doi,
        stub.arxiv,
    )
    for leg in _MARKUP_LEGS:
        t0 = time.perf_counter()
        try:
            url = leg.url_for(stub)
        except Exception as exc:
            store.append_event(
                stub.ref_id,
                source=leg.source,
                event="api_error",
                payload={"error": str(exc)[:200]},
                duration_ms=_ms(t0),
            )
            continue
        if url is None:
            continue  # no identifier for this leg — silent skip
        final_path = inbox_dir / (_stub_filename(stub) + leg.suffix)
        staged_path = staging_dir / final_path.name
        try:
            size = _download_markup(url, staged_path)
        except Exception as exc:
            log.warning(
                "fetch_oa markup: %s leg failed for ref_id=%s (%s): %s",
                leg.source,
                stub.ref_id,
                url,
                exc,
            )
            store.append_event(
                stub.ref_id,
                source=leg.source,
                event="fetch_failed",
                payload={"url": url, "error": str(exc)[:200]},
                duration_ms=_ms(t0),
            )
            continue
        write_sidecar(
            staged_path,
            ref_id=stub.ref_id,
            identifiers={
                k: v
                for k, v in {
                    "doi": stub.doi,
                    "arxiv": stub.arxiv,
                    "s2": stub.s2_id,
                    "cite_key": stub.cite_key,
                }.items()
                if v
            },
            source=leg.source,
            source_format=leg.fmt,
        )
        store.append_event(
            stub.ref_id,
            source=leg.source,
            event="fetch_ok",
            payload={"url": url, "bytes": size, "format": leg.fmt},
            duration_ms=_ms(t0),
        )
        log.info(
            "fetch_oa markup: %s landed for ref_id=%s (%s, %d bytes)",
            leg.source,
            stub.ref_id,
            leg.fmt,
            size,
        )
        return _StagedMarkup(staged_path=staged_path, final_path=final_path)
    return None


def _run_cascade(
    store: Any,
    stub: StubRef,
    inbox_dir: Path,
    email: str,
    api_key: str,
    wiley_token: str,
    core_key: str,
    openalex_content_key: str = "",
    printable_only: bool = False,
) -> Path | None:
    """Walk providers in order; stop at first fetch_ok. Returns the PDF path
    on a landed fetch, else ``None``.

    ``printable_only`` (gr161905) marks the sidecar so the watcher never
    runs Marker on this PDF — set when a markup source already landed for
    this stub, so this PDF is only ever the printable companion, never a
    second, order-dependent body candidate.

    Records every attempted provider's outcome via append_event.
    Legs whose credential / identifier is absent are skipped at the
    cascade level (Elsevier/Wiley/CORE without their key, Crossref/
    Unpaywall without an email) or return ``None`` internally (no
    matching identifier), so the remaining legs still run.

    Order favours the *version of record*, cheapest-and-most-reliable
    first, with the green-OA net + preprint as late fallbacks:

    1. ``publisher`` — deterministic DOI→PDF, keyless (Springer/PLOS).
    2. ``elsevier``  — Elsevier API by DOI, key-gated (ScienceDirect).
    3. ``wiley``     — Wiley TDM API by DOI, token-gated.
    4. ``unpaywall`` — OA aggregator by DOI.
    5. ``crossref``  — publisher TDM PDF links by DOI.
    6. ``openalex``  — OA aggregator (different PDF coverage to UPW).
    7. ``europepmc`` — biomedical OA full-text PDF.
    8. ``core``      — green-OA repository copies, key-gated.
    9. ``arxiv``     — deterministic preprint PDF; after the published
       sources so a version of record is preferred over the preprint.
    10. ``s2``       — Semantic Scholar openAccessPdf, last resort (free).
    11. ``openalex_content`` — paid OpenAlex cache (PDF/TEI), publisher-
        agnostic; clears the MDPI-Akamai / Cloudflare 403 wall. Gated by
        ``PRECIS_OPENALEX_CONTENT_KEY`` alone (the key is the spend opt-in), and
        last so it only spends after every free leg fails. A low-balance alert
        (:func:`check_openalex_balance`) warns before the runway runs out.
    """
    providers: list[tuple[str, Any]] = [
        (_SOURCE_PUBLISHER, lambda: _try_publisher(stub, inbox_dir=inbox_dir)),
    ]
    if api_key:
        providers.append(
            (
                _SOURCE_ELSEVIER,
                lambda: _try_elsevier(stub, inbox_dir=inbox_dir, api_key=api_key),
            )
        )
    if wiley_token:
        providers.append(
            (
                _SOURCE_WILEY,
                lambda: _try_wiley(stub, inbox_dir=inbox_dir, token=wiley_token),
            )
        )
    if email:
        providers.append(
            (
                _SOURCE_UNPAYWALL,
                lambda: _try_unpaywall(stub, inbox_dir=inbox_dir, email=email),
            )
        )
        providers.append(
            (
                _SOURCE_CROSSREF,
                lambda: _try_crossref(stub, inbox_dir=inbox_dir, email=email),
            )
        )
    providers.append(
        (
            _SOURCE_OPENALEX,
            lambda: _try_openalex(stub, inbox_dir=inbox_dir, email=email),
        )
    )
    providers.append(
        (_SOURCE_EUROPEPMC, lambda: _try_europepmc(stub, inbox_dir=inbox_dir))
    )
    if core_key:
        providers.append(
            (
                _SOURCE_CORE,
                lambda: _try_core(stub, inbox_dir=inbox_dir, api_key=core_key),
            )
        )
    providers.append((_SOURCE_ARXIV, lambda: _try_arxiv(stub, inbox_dir=inbox_dir)))
    providers.append((_SOURCE_S2, lambda: _try_s2(stub, inbox_dir=inbox_dir)))
    # Paid, publisher-agnostic full text — LAST, so we only spend after every
    # free leg has failed. The credential itself is the opt-in (configuring the
    # key on the fetch host is the budget decision); the low-balance alert warns
    # before the runway runs out.
    if openalex_content_key:
        providers.append(
            (
                _SOURCE_OPENALEX_CONTENT,
                lambda: _try_openalex_content(
                    stub,
                    inbox_dir=inbox_dir,
                    api_key=openalex_content_key,
                    store=store,
                    email=email,
                ),
            )
        )

    for source, runner in providers:
        try:
            outcome = runner()
        except Exception as exc:
            store.append_event(
                stub.ref_id,
                source=source,
                event="api_error",
                payload={"error": str(exc)[:200]},
            )
            continue
        if outcome is None:
            # No identifier for this source — silent skip; no event.
            continue
        store.append_event(
            stub.ref_id,
            source=source,
            event=outcome.event,
            payload=outcome.payload,
            duration_ms=outcome.duration_ms,
            cost_usd=outcome.cost_usd,
        )
        if outcome.event == "fetch_ok":
            # Drop an acquisition manifest next to the PDF so ingest folds
            # into *this* stub (keeping its good title/DOI) instead of
            # re-deriving identity from the bytes and minting a duplicate
            # when Marker's extracted DOI is truncated / missing. Keyed on
            # the deterministic download name, not the payload, so a
            # fetcher that omits ``filename`` still gets a sidecar. See
            # precis.ingest.fetch_sidecar.
            pdf_path = inbox_dir / (_stub_filename(stub) + ".pdf")
            write_sidecar(
                pdf_path,
                ref_id=stub.ref_id,
                identifiers={
                    "doi": stub.doi or "",
                    "arxiv": stub.arxiv or "",
                    "s2": stub.s2_id or "",
                    "cite_key": stub.cite_key or "",
                },
                source=source,
                printable_only=printable_only,
            )
            return pdf_path
    return None


def _publish_markup_trigger(staged: _StagedMarkup, companion_pdf: str | None) -> Path:
    """Atomically publish a staged markup trigger into the watched inbox.

    Rewrites the sidecar (still in .staging) with the now-known
    ``companion_pdf`` name, then os.replace()s both the content file and
    its sidecar from .staging into the real inbox path in one step — so
    the trigger never becomes watcher-visible with an incomplete sidecar
    (gripe:170349). staged.staged_path and staged.final_path are
    guaranteed to be on the same filesystem (same inbox_dir tree), so
    both replaces are atomic.
    """
    sidecar = read_sidecar(staged.staged_path)
    if sidecar is None:
        log.warning(
            "fetch_oa: no staged sidecar found for %s — publishing without "
            "companion linkage",
            staged.staged_path.name,
        )
        os.replace(staged.staged_path, staged.final_path)
        return staged.final_path
    write_sidecar(
        staged.staged_path,
        ref_id=sidecar.ref_id,
        identifiers=sidecar.identifiers,
        source=sidecar.source,
        source_format=sidecar.source_format,
        companion_pdf=companion_pdf,
    )
    # Sidecar before content: a watcher debounces on content stability
    # anyway, but publishing the sidecar first means it's never possibly
    # missing for a trigger a watcher has already seen.
    os.replace(sidecar_path(staged.staged_path), sidecar_path(staged.final_path))
    os.replace(staged.staged_path, staged.final_path)
    return staged.final_path


def _sweep_stale_staging(
    inbox_dir: Path, *, max_age_hours: float = _STAGING_GC_MAX_AGE_HOURS
) -> int:
    """Remove ``.staging`` entries abandoned before :func:`_publish_markup_trigger`
    ever ran (gr170366).

    ``_publish_markup_trigger`` only fires when the per-stub loop in
    :func:`run_oa_fetch_pass` reaches it; an outer, un-caught exception
    between a successful markup stage and that publish step (something in
    ``_run_cascade`` / ``store.append_event`` beyond the per-leg
    try/excepts) silently strands the staged content + sidecar under
    ``inbox_dir / ".staging"`` — nothing else globs or GCs that directory.
    A re-claim of the same stub self-heals the entry (deterministic
    filename), but a stub that stops being reclaimed (identifier change,
    manual resolution elsewhere) would otherwise leak it forever.

    Deliberately files-not-pairs: every direct child of ``.staging`` —
    staged content, its sidecar, or a stray ``.part`` partial from an
    interrupted download — is considered independently by mtime, so no
    pairing logic can miss a half-written orphan. Conservative by
    construction: only entries older than ``max_age_hours`` are removed,
    well past any plausible in-flight duration (see
    :data:`_STAGING_GC_MAX_AGE_HOURS`), so a concurrent stub mid-write is
    never touched. Best-effort — a listing or per-file failure is logged
    and skipped, never raised. Returns the count removed.
    """
    staging_dir = inbox_dir / ".staging"
    if not staging_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    try:
        entries = list(staging_dir.iterdir())
    except OSError as exc:
        log.warning("fetch_oa: staging sweep couldn't list %s: %s", staging_dir, exc)
        return 0
    removed = 0
    for path in entries:
        try:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            path.unlink()
        except OSError as exc:
            log.warning(
                "fetch_oa: staging sweep failed to remove %s: %s", path.name, exc
            )
            continue
        removed += 1
        log.info(
            "fetch_oa: staging sweep removed stale %s (age > %.1fh)",
            path.name,
            max_age_hours,
        )
    return removed


# ── Internals ──────────────────────────────────────────────────────


@retry(
    wait=wait_exponential(min=1, max=30),
    stop=stop_after_attempt(_RETRY_MAX_ATTEMPTS),
    retry=retry_if_exception_type(
        (httpx.TransportError, httpx.ReadTimeout, httpx.WriteTimeout)
    ),
    reraise=True,
)
def _query_unpaywall(doi: str, *, email: str) -> dict[str, Any]:
    """Hit GET /v2/<doi>?email=… and return the parsed JSON.

    Retries on transport / timeout errors; HTTPStatusError (400/404/
    429/5xx) bubbles up to the caller for per-status handling. The
    return shape is documented at https://unpaywall.org/data-format.
    """
    url = f"{_UNPAYWALL_BASE}/{doi}"
    with httpx.Client(timeout=_API_TIMEOUT_S) as client:
        resp = client.get(url, params={"email": email})
        resp.raise_for_status()
        return resp.json()


def _query_crossref_pdf_links(doi: str, *, email: str) -> list[str]:
    """Return Crossref ``link[]`` URLs whose content-type is a PDF.

    ``mailto`` routes us to Crossref's polite pool (the anonymous pool
    connection-resets under load). Non-PDF TDM links (xml/plain, e.g.
    Elsevier's api.elsevier.com mining endpoints) are filtered out —
    those are handled by the dedicated Elsevier leg, not here.
    """
    url = f"https://api.crossref.org/works/{doi}"
    with httpx.Client(
        timeout=_API_TIMEOUT_S, headers={"User-Agent": _user_agent_header(email)}
    ) as client:
        resp = client.get(url, params={"mailto": email})
        resp.raise_for_status()
        data = resp.json()
    links = (data.get("message") or {}).get("link") or []
    out: list[str] = []
    for link in links:
        ct = (link.get("content-type") or "").lower()
        href = link.get("URL")
        if href and "pdf" in ct and href not in out:
            out.append(href)
    return out


def _query_openalex_pdf_urls(doi: str, *, email: str) -> list[str]:
    """Return OpenAlex direct-PDF URLs for ``doi`` (best location first).

    Only ``pdf_url`` fields are collected (the landing-page ``oa_url``
    is skipped — the ``%PDF-`` guard would reject it anyway and it just
    burns a download attempt).
    """
    url = f"https://api.openalex.org/works/doi:{doi}"
    params = {"mailto": email} if email else {}
    with httpx.Client(
        timeout=_API_TIMEOUT_S, headers={"User-Agent": _user_agent_header(email)}
    ) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    out: list[str] = []
    best = data.get("best_oa_location") or {}
    if best.get("pdf_url"):
        out.append(best["pdf_url"])
    for loc in data.get("locations") or []:
        href = loc.get("pdf_url")
        if href and href not in out:
            out.append(href)
    return out


def _query_europepmc_oa_pmcid(doi: str) -> str | None:
    """Resolve a DOI to its Europe PMC PMCID iff in the OA subset.

    Returns the ``PMC…`` id for the first open-access hit, else
    ``None`` (no OA full text to fetch).
    """
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {"query": f'DOI:"{doi}"', "format": "json", "resultType": "core"}
    with httpx.Client(
        timeout=_API_TIMEOUT_S, headers={"User-Agent": _user_agent_header()}
    ) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    results = (data.get("resultList") or {}).get("result") or []
    for rec in results:
        if rec.get("isOpenAccess") == "Y" and rec.get("pmcid"):
            return str(rec["pmcid"])
    return None


def _is_valid_http_url(href: Any) -> bool:
    """Return ``True`` if ``href`` is a string that looks like a public
    http(s) URL.

    CORE's ``downloadUrl`` and ``fullText`` fields can contain non-URL
    values (e.g. a bare work id like ``"587670336"`` or the full text
    body). Light validation lets the fetcher try only the candidates
    that have a scheme and a host.
    """
    if not isinstance(href, str) or not href.startswith(("http://", "https://")):
        return False
    # Reject multi-token values (e.g. full text paragraphs that happen
    # to start with "http") before parsing.
    if any(c in href for c in ("\n", "\t", " ")):
        return False
    parts = urlsplit(href)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


#: URL paths whose extension marks them as clearly-not-a-PDF (CORE's
#: ``fullText`` link can point at a ``.txt``/``.html`` rendering). Skipped so
#: the cascade doesn't burn a download on bytes the ``%PDF-`` guard will reject.
_NON_PDF_URL_SUFFIXES = (".txt", ".html", ".htm", ".xml", ".json")


def _looks_non_pdf_url(href: str) -> bool:
    """True when ``href``'s path ends in a clearly non-PDF text extension."""
    path = urlsplit(href).path.lower()
    return path.endswith(_NON_PDF_URL_SUFFIXES)


def _query_core_fulltext_urls(doi: str, *, api_key: str) -> list[str]:
    """Return CORE full-text URLs (PDF-preferred) for an exact DOI match.

    Only results whose own ``doi`` equals the query DOI are kept (CORE
    fuzzy-matches, so a bare topical hit must not be mistaken for the
    paper). A browser UA dodges CORE's Cloudflare UA gate.

    Looks at the repository ``downloadUrl`` (a PDF bitstream) first, then the
    ``fullText`` link — but a ``fullText`` URL that clearly renders as
    ``.txt``/``.html``/… is dropped (:func:`_looks_non_pdf_url`), since the
    downloader only accepts ``%PDF-`` bytes and would waste the round-trip.
    Every candidate is validated as a real http(s) URL — the earlier bug passed
    a bare CORE work id (``"587670336"``), which the SSRF guard rejected. The
    name reflects the output: full-text links, not guaranteed-PDF ones.
    """
    params: dict[str, str | int] = {"q": f'doi:"{doi}"', "limit": 5}
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": _BROWSER_UA}
    with httpx.Client(timeout=_API_TIMEOUT_S, headers=headers) as client:
        resp = client.get(_CORE_SEARCH_BASE, params=params)
        resp.raise_for_status()
        data = resp.json()
    want = doi.lower()
    out: list[str] = []
    for rec in data.get("results") or []:
        rec_doi = (rec.get("doi") or "").lower()
        if rec_doi != want:
            continue
        # Prefer the repository downloadUrl (a PDF bitstream), then a fullText
        # link — but skip a fullText URL that clearly isn't a PDF.
        for key in ("downloadUrl", "fullText"):
            href = rec.get(key)
            if not _is_valid_http_url(href) or href in out:
                continue
            if key == "fullText" and _looks_non_pdf_url(href):
                continue
            out.append(href)
    return out


def _query_openalex_content_urls(doi: str, *, email: str = "") -> dict[str, str]:
    """Return the ``content_urls`` OpenAlex reports as *cached* for a DOI.

    Keyless metadata call (the work object is free). Keeps only the content
    types ``has_content`` marks true — so a ``{'pdf': '…', 'grobid_xml': '…'}``
    result means both are actually fetchable, and an empty dict means OpenAlex
    has no cached full text (don't spend). ``mailto`` is polite, optional.

    A ``404`` (the DOI simply isn't in OpenAlex) is *not* an error — it's just
    "no content here", so return ``{}`` (→ ``no_oa_version``) rather than
    letting ``raise_for_status`` bubble it up as an ``api_error``.
    """
    url = f"https://api.openalex.org/works/doi:{doi}"
    params = {"mailto": email} if email else {}
    with httpx.Client(
        timeout=_API_TIMEOUT_S, headers={"User-Agent": _user_agent_header(email)}
    ) as client:
        resp = client.get(url, params=params)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()
    has = data.get("has_content") or {}
    urls = data.get("content_urls") or {}
    return {
        kind: href
        for kind, href in urls.items()
        if has.get(kind) and isinstance(href, str)
    }


def _with_api_key(url: str, api_key: str) -> str:
    """Return ``url`` with ``api_key=<key>`` merged into its query string.

    Preserves any existing query params (OpenAlex content URLs are bare, but
    be defensive). Used only for the OpenAlex Content leg — the key travels in
    the query, never in a header or the recorded payload.
    """
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query["api_key"] = api_key
    return urlunparse(parts._replace(query=urlencode(query)))


def _download_pdf(
    url: str, target: Path, *, extra_headers: dict[str, str] | None = None
) -> int:
    """Stream a PDF to ``target`` and return the byte count.

    Streams so a 50 MB PDF doesn't sit in memory. Atomic-ish:
    writes to ``<target>.part`` and renames on success so a crashed
    download doesn't leave a half-file the watcher would try to
    ingest.

    Validates the first 5 bytes are ``%PDF-`` before keeping the
    file — some publishers return HTML interstitial pages with 200
    OK; saving those would poison the watcher (Marker would barf).

    ``extra_headers`` merges over the default UA/Accept headers — used
    by the Elsevier leg to send ``X-ELS-APIKey``. NB these headers are
    set on the client, so they ride along every redirect hop that
    :func:`safe_stream` follows; only pass *credential* headers for a
    trusted fixed-host endpoint (the SSRF guard still caps redirects to
    public hosts, but it can't tell a publisher CDN from an arbitrary
    third party). Elsevier's article API serves the PDF inline (no
    cross-host redirect in practice), so the key stays first-party.
    """
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_stream

    tmp = target.with_suffix(target.suffix + ".part")
    size = 0
    head = b""
    headers = _download_headers()
    if extra_headers:
        headers.update(extra_headers)
    # http_client defaults follow_redirects=False and installs the SSRF
    # pinning backend — safe_stream walks the chain itself, revalidating
    # each Location. Original code set follow_redirects=True with only an
    # is_http_url() shape check on ``url``, so a publisher redirect to
    # 169.254.169.254 / 127.0.0.1 would be followed and the magic-byte
    # check at the end could be defeated by a server that echoes %PDF-.
    with http_client(
        timeout=_DOWNLOAD_TIMEOUT_S,
        headers=headers,
        user_agent=None,
    ) as client:
        with safe_stream(client, "GET", url) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > _MAX_DOWNLOAD_BYTES
            ):
                raise ValueError(
                    f"response too large (Content-Length={declared} bytes, "
                    f"cap={_MAX_DOWNLOAD_BYTES}) — skipped, disk-fill guard"
                )
            over_cap = False
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    if size == 0:
                        head = chunk[:8]
                    fh.write(chunk)
                    size += len(chunk)
                    if size > _MAX_DOWNLOAD_BYTES:
                        over_cap = True
                        break
    if over_cap:
        tmp.unlink(missing_ok=True)
        raise ValueError(
            f"response exceeded {_MAX_DOWNLOAD_BYTES} byte cap mid-stream "
            "(disk-fill guard)"
        )
    if not head.startswith(b"%PDF-"):
        tmp.unlink(missing_ok=True)
        raise ValueError(f"response is not a PDF (got {size} bytes, head={head!r})")
    tmp.rename(target)
    return size


# Polite UA — publishers (PeerJ, arXiv) sometimes 403 the default
# httpx UA. Identify ourselves and include the contact env when
# present so an annoyed sysadmin can ping us rather than blocking.
_USER_AGENT = (
    "precis-mcp/8.0 (+https://github.com/retostamm/precis-mcp; mailto:{email})"
)


def _user_agent_header(email: str | None = None) -> str:
    return _USER_AGENT.format(
        email=email or settings.get_str("contact.polite_email") or "noreply@example.com"
    )


def _download_headers() -> dict[str, str]:
    """Default download headers, built fresh per call (not frozen at import)
    so a DB-set ``contact.polite_email`` takes effect on the next download
    without a process restart."""
    return {
        "User-Agent": _user_agent_header(),
        # Some hosts insist on an Accept header that names PDFs explicitly.
        "Accept": "application/pdf,*/*;q=0.8",
    }


def _ms(t0: float) -> int:
    """Helper: monotonic millisecond delta from ``t0``."""
    return int((time.perf_counter() - t0) * 1000)


def _stub_filename(stub: StubRef) -> str:
    """Filename stem for the downloaded PDF.

    Prefer the cite_key (clean, human-readable); fall back to a
    DOI- or arXiv-derived slug; last resort, ``ref_<id>``.
    """
    if stub.cite_key:
        return _sanitise(stub.cite_key)
    if stub.doi:
        return _sanitise(stub.doi)
    if stub.arxiv:
        return _sanitise(stub.arxiv.removeprefix("arxiv:"))
    return f"ref_{stub.ref_id}"


def _sanitise(s: str) -> str:
    """Strip a string to filename-safe characters, capped at 80 chars."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s.lower())[:80]


def _query_s2_openaccess(paper_id: str) -> str | None:
    """Look up the ``openAccessPdf`` URL for a paper via Semantic Scholar.

    Uses the existing :mod:`semanticscholar` client (no API key
    needed for low volume). Returns the URL or ``None`` when S2
    has no OA PDF on file.

    Lifted up here rather than added to
    :mod:`precis.ingest.semantic_scholar` because the existing
    module's ``_normalize`` shape doesn't carry the
    ``openAccessPdf`` field — splitting the fetcher's S2 surface
    keeps the existing metadata-lookup path untouched.
    """

    from semanticscholar import SemanticScholar

    from precis.utils.rate_limit import acquire as acquire_rate_limit

    api_key = _secrets.get_secret("SEMANTIC_SCHOLAR_API_KEY") or ""
    sch = SemanticScholar(api_key=api_key) if api_key else SemanticScholar()
    acquire_rate_limit("s2")
    paper = sch.get_paper(paper_id, fields=["openAccessPdf"])
    if not paper:
        return None
    oa = getattr(paper, "openAccessPdf", None) or {}
    if isinstance(oa, dict):
        url = oa.get("url")
    else:
        url = getattr(oa, "url", None)
    return str(url) if url else None


__all__ = [
    "FetchOutcome",
    "StubRef",
    "claim_stubs_to_fetch",
    "run_oa_fetch_pass",
]
