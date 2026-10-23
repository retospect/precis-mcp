"""run_oa_fetch_pass — sibling worker that fetches OA PDFs.

Closes the chase↔stub loop: chase creates stub refs (identifiers known,
``pdf_sha256 IS NULL``); this worker cascades through **ten OA sources**
per stub, by DOI/arXiv/S2 id unless noted. No gate listed = free, no key:

1. **Publisher pattern** — deterministic DOI→PDF for known-endpoint
   publishers (Springer/BMC, PLOS).
2. **Elsevier** — Article Retrieval API; key-gated
   (``PRECIS_ELSEVIER_API_KEY``), only route for ScienceDirect.
3. **Wiley** — TDM API; token-gated (``PRECIS_WILEY_TDM_TOKEN``), direct
   version-of-record PDF.
4. **Unpaywall** — 30+-source aggregator; TOS needs an email param.
5. **Crossref** — publisher TDM PDF links; needs polite ``mailto`` (=
   the Unpaywall email).
6. **OpenAlex** — OA ``pdf_url`` locations; ``mailto`` optional,
   different coverage to Unpaywall.
7. **Europe PMC** — biomedical OA full text, DOI→PMCID.
8. **CORE** — green-OA repository copies; key-gated
   (``PRECIS_CORE_API_KEY``), the net for paywalled publishers.
9. **arXiv** — direct PDF by arXiv ID.
10. **Semantic Scholar** — ``openAccessPdf`` field, low-volume no-key.

Deterministic/key-gated legs run first (sidestep aggregators' common
landing-page-as-OA miss on fresh DOIs); identifier-less legs silently
no-op. Cascade stops at the first PDF; later legs run only after
``no_oa_version``/``fetch_failed``. Each attempt writes a ``ref_events``
row (``source='fetcher:<leg>'``).

A landed PDF: watch inbox → ``precis ingest --watch`` → ``precis_add`` →
``register_aliases_and_maybe_upgrade`` (promotes the stub) → chase
resumes.

Sibling worker (plain function), not a ``WorkerHandler`` — same pattern
as ``chase``/``segment_toc``.

Event vocabulary (shared across sources): ``fetch_ok`` (url+bytes+license
payload), ``no_oa_version``, ``fetch_failed`` (URL but download failed),
``rate_limited``, ``api_error``, ``invalid_identifier``,
``identifier_missing``, ``provider_wait`` (leg deferred — its provider's
gate could not admit it before the leg's wall; not a provider miss, the
leg is retried on the stub's next pass; see :class:`_ProviderGate`).
``precis stubs`` reads the latest event per stub
for the backlog.

**Stub states as the readouts see them** (``precis stats --stubs``,
``stub_backlog_count``, the ``/drive`` stub queue): *awaiting* — no
``fetcher:%`` event yet; *no-oa* (cooled) — at least
``store._stub_predicate.NO_OA_MIN_PASSES`` hour-bucketed passes and every
event ``no_oa_version`` (:func:`~precis.store._stub_predicate.
no_oa_bucket_sql`); *retry* — anything else still without a PDF. Cooled
is a readout bucket, not a terminal state: ``claim_stubs_to_fetch`` still
claims such a stub on its monthly backoff (embargoes lift, authors
self-archive), it is only kept out of the pending counts and listed as
the manual-retrieval set (``precis stubs --manual``, the ``/drive``
"Manual retrieval" section) so a human can buy it. An explicit acquire's
``Store.pin_stub_for_fetch`` re-stamps the backoff bypass only once the
previous stamp is consumed and the newest attempt is over 24 h old, so
re-acquiring a cited stub cannot re-arm it (gr453859).

**Fetch-time retraction gate** (:func:`_apply_retraction_gate`) — a cheap
DOI-only Crossref check (``ingest.provenance.check_doi``) ahead of the
cascade: ``retracted`` hard-skips (stamps ``refs.retraction_status``,
breadcrumbs, and — via ``store._stub_predicate.stub_predicate_sql`` —
excludes the ref from every future claim/browse query, firing once);
``corrected``/``expression_of_concern`` soft-flags (stamps status,
breadcrumbs, continues the fetch) and re-checks each pass (writes only on
a status change) to catch a later escalation.

Distinct from ``scripts/_doilist.py`` (``doilist scan --download``): an
operator-facing, file-driven (``dois_to_get.md``) Unpaywall-only CLI, no
DB writes — coexists.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import random
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlsplit

import httpx
from psycopg import Connection
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    stop_after_delay,
    stop_any,
    wait_exponential,
)

from precis import secrets as _secrets
from precis import settings
from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert as _raise_alert
from precis.alerts import resolve_stale_alerts as _resolve_alerts
from precis.ingest.fetch_sidecar import read_sidecar, sidecar_path, write_sidecar
from precis.store._stub_predicate import stub_predicate_sql
from precis.utils.http import (
    Policy,
    call_with_backoff,
    http_client,
    polite_user_agent,
)
from precis.utils.rate_limit import RateLimited
from precis.utils.rate_limit import observe_timing as observe_rate_limit_timing
from precis.utils.rate_limit import require as require_rate_limit
from precis.workers import activity

if TYPE_CHECKING:
    from precis.store.store import Store

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

# Markup-first legs (docs/backlog/dark-features-activation.md). Run *before*
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

# In-pass PDF-download retries on a *transport-class* failure only
# (handshake/connect/read timeout, connection reset, 5xx/429). A single
# deterministic-route timeout used to end the pass as a bare
# ``fetch_failed`` and then eat the cross-pass backoff window; a couple of
# immediate retries clear a transient blip on the fetch host (the incident
# that motivated this: 19 SSL-handshake timeouts in a two-hour window on a
# green arXiv route) before it becomes a next-day wait. Kept small so a
# genuinely-down host still falls through to the next leg promptly. A real
# 4xx / non-PDF body is deterministic and is NOT retried.
_DOWNLOAD_RETRY_ATTEMPTS = 3

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

# ── Throughput knobs (the pass was draining 1–4 of 32 claimed stubs in
# its 300 s wall budget: every leg ran serially per stub and a dead
# provider burned its full timeout×retry product — ~45–360 s — on every
# stub of every pass; prod ref_events 2026-10-20: crossref 363 s,
# europepmc_jats 157 s, core 33 s). Four bounds stack so one dead
# endpoint can no longer hold the pass:
#
# * ``PRECIS_OA_FETCH_WORKERS`` — stubs processed concurrently inside
#   the pass. Per-provider pacing is already cluster-coordinated
#   (``external_rate_limits`` rows, taken atomically per attempt by
#   ``call_with_backoff``/``require_rate_limit``), so N stubs in flight
#   cannot exceed any published rate; publisher downloads pace on their
#   own ``host:<netloc>`` rows.
# * Per-stub wall ``PRECIS_OA_FETCH_STUB_WALL_S`` — no new leg starts
#   once a stub is older than this (a leg in flight is never
#   interrupted; its own timeouts bound it).
# * Per-leg download tail ``PRECIS_OA_FETCH_LEG_WALL_S`` — the retry
#   budget for one leg's download/candidate loop (``deadline=`` on
#   ``call_with_backoff`` stops the retry chain; the in-flight request
#   still gets its full ``_DOWNLOAD_TIMEOUT_S``).
# * Metadata leg ``PRECIS_OA_FETCH_META_WALL_S`` — same deadline shape
#   on the small JSON round-trips that used to retry 5×30 s.
#
# On top of the budgets a per-provider admission gate
# (:class:`_ProviderGate`) makes a sick provider's legs *wait* rather
# than skip (gr500153, Reto 2026-10-20: "skip is not the right thing,
# just wait for it" — the first pass breaker tripped circuit_open ~20/h
# on crossref and wiley, burning real OA copies). Each provider key has
# an in-flight cap (default :data:`_PROVIDER_INFLIGHT_DEFAULT`,
# ``PRECIS_OA_FETCH_INFLIGHT``) so a dead endpoint can hold at most that
# many workers in actual HTTP waits, and a shared full-jitter
# exponential backoff armed by consecutive *slow* failures — a fast
# ``fetch_failed``/``api_error`` is a normal miss and resets it, a
# ≥``_SLOW_FAIL_MS`` one is the dead-endpoint signature. A leg that
# can't be admitted before its own wall *defers* with a
# ``provider_wait`` event — never a provider miss — and the stub is
# retried on its next claim (the claim query's ``attempts`` count
# ignores deferral-containing passes). A slow provider delays only its
# own legs: other providers and other stubs keep going on the other
# workers. 429/Retry-After needs no extra wiring: the leg's own
# ``call_with_backoff``/``require_rate_limit`` already parks the shared
# limiter row for the hinted seconds, so the next attempt's token-take
# waits it out cluster-wide.
_PASS_WORKERS_DEFAULT = 6
_STUB_WALL_S_DEFAULT = 120.0
_LEG_WALL_S_DEFAULT = 150.0
_META_WALL_S_DEFAULT = 45.0
_SLOW_FAIL_MS = 20_000

#: In-flight cap per provider key — how many of the pass's workers may
#: sit inside one provider's legs at once. ``PRECIS_OA_FETCH_INFLIGHT``
#: overrides: a bare int caps all keyed providers, a JSON object caps
#: named ones (``{"crossref": 2, "wiley": 1, "*": 3}``); a value ≤0
#: means *uncapped* for that key (the escape hatch, not a deadlock).
_PROVIDER_INFLIGHT_DEFAULT = 2

#: Shared per-provider backoff after a slow failure: the next leg waits
#: ``uniform(0, min(_PROVIDER_BACKOFF_MAX_S, base * 2**(fails-1)))`` —
#: the same full-jitter shape ``precis.utils.http._delay`` uses. With
#: the defaults a dead provider is probed ~every 5 s at first, decaying
#: to one probe per two minutes while failures keep landing; one healthy
#: outcome resets it. Constants, not env knobs — the leg/stub walls are
#: the operator-facing bounds; this shape only sets the probe cadence.
_PROVIDER_BACKOFF_BASE_S = 5.0
_PROVIDER_BACKOFF_MAX_S = 120.0

#: Poll interval for :meth:`_ProviderGate.acquire` — how often a waiting
#: leg re-checks for a freed slot once the provider's backoff has
#: expired. Small against network-second leg costs; a Condition would
#: save the wakeup but couldn't ride an injected clock in tests.
_GATE_POLL_S = 0.05

#: Outcome events that count as a provider failure for the gate's shared
#: backoff — only when the leg also took at least ``_SLOW_FAIL_MS`` (a
#: dead-endpoint signature). ``fetch_ok``/``no_oa_version``/identifier
#: misses are healthy.
_GATE_FAIL_EVENTS = frozenset({"api_error", "fetch_failed", "rate_limited"})

#: ref_events ``source`` → the external provider a leg is admitted under
#: (and a slow failure charged to). Markup legs share their PDF
#: sibling's provider (the JATS leg queries the same Europe PMC API; the
#: arXiv legs hit the same host). ``fetcher:publisher`` has no key: its
#: hosts vary per DOI, so a dead Springer tells us nothing about PLOS.
_PROVIDER_KEYS: dict[str, str | None] = {
    _SOURCE_PUBLISHER: None,
    _SOURCE_ELSEVIER: "elsevier",
    _SOURCE_ELSEVIER_XML: "elsevier",
    _SOURCE_WILEY: "wiley",
    _SOURCE_UNPAYWALL: "unpaywall",
    _SOURCE_CROSSREF: "crossref",
    _SOURCE_OPENALEX: "openalex",
    _SOURCE_OPENALEX_CONTENT: "openalex-content",
    _SOURCE_EUROPEPMC: "europepmc",
    _SOURCE_EUROPEPMC_JATS: "europepmc",
    _SOURCE_CORE: "core",
    _SOURCE_ARXIV: "arxiv",
    _SOURCE_ARXIV_HTML: "arxiv",
    _SOURCE_ARXIV_SOURCE: "arxiv",
    _SOURCE_S2: "s2",
}


def _provider_inflight_caps() -> dict[str, int]:
    """``PRECIS_OA_FETCH_INFLIGHT`` → ``{provider_key: cap}``; ``"*"`` is
    the fallback for unlisted keys.

    Bare int (JSON or not) caps every keyed provider; a JSON object caps
    named ones. Unset/unparseable → ``{}`` (everything takes
    :data:`_PROVIDER_INFLIGHT_DEFAULT`). Values ≤0 mean uncapped —
    refusing to admit a provider forever would be a worse failure mode
    than an uncapped one. Never raises (same convention as
    :func:`_fetch_explore_fraction`): a typo'd knob must not take the
    pass down.
    """
    raw = os.environ.get("PRECIS_OA_FETCH_INFLIGHT", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = raw
    if isinstance(parsed, dict):
        caps: dict[str, int] = {}
        for k, v in parsed.items():
            try:
                caps[str(k)] = int(v)
            except (TypeError, ValueError):
                log.warning(
                    "fetch_oa: ignoring non-int PRECIS_OA_FETCH_INFLIGHT[%r]=%r",
                    k,
                    v,
                )
        return caps
    try:
        return {"*": int(parsed)}
    except (TypeError, ValueError):
        log.warning("fetch_oa: ignoring unparseable PRECIS_OA_FETCH_INFLIGHT=%r", raw)
        return {}


@dataclass
class _GateLane:
    """One provider key's gate state (mutated under the gate's lock)."""

    in_flight: int = 0
    fails: int = 0
    #: ``perf_counter`` instant the next leg may not start before — the
    #: armed shared backoff (0 while the provider is healthy).
    not_before: float = 0.0
    cap: int = _PROVIDER_INFLIGHT_DEFAULT


class _ProviderGate:
    """Per-provider admission control — wait, not skip (gr500153).

    Replaces the per-pass skip breaker: after
    :data:`_SLOW_FAIL_MS`-class failures a provider's legs used to be
    skipped for the rest of the pass (``circuit_open``), which tripped
    ~20×/hour on crossref/wiley in prod and starved stubs of providers
    that were merely slow. Now a leg that wants a degraded provider
    *waits* — bounded by the caller's deadline (the leg/stub wall) —
    for both:

    * **a free in-flight slot** — at most ``cap`` legs per provider key
      run at once across the pass's worker threads, so a dead endpoint
      can't park every worker inside its timeouts; and
    * **the shared backoff** — a slow failure pushes ``not_before`` out
      by a full-jitter exponential delay, so the whole pass probes a
      dead provider at a decaying cadence instead of every stub paying
      its timeout. A healthy outcome resets it; a fast failure is a
      normal miss, not a sickness signal.

    :meth:`acquire` returns ``False`` when the leg can't be admitted
    before ``deadline`` — the caller defers it (``provider_wait`` event,
    retried next pass, never a miss). Retry-After/429 honouring lives
    one layer down: a hinted response parks the provider's shared
    limiter row inside ``call_with_backoff``, so the *next* attempt's
    token-take waits out the hint for every host — the gate's own
    backoff stacks on top for slow-failure sickness, not per-status
    politeness.

    Thread-safe — stub workers contend on the same lanes. State dies
    with the pass (constructed per :func:`run_oa_fetch_pass`): a
    recovered provider serves the next pass immediately, a still-dead
    one re-earns its backoff one probe at a time. ``now``/``sleep``/
    ``uniform`` are injectable so tests drive a fake clock instead of
    sleeping.
    """

    def __init__(
        self,
        *,
        caps: dict[str, int] | None = None,
        now: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], object] | None = None,
        uniform: Callable[[float, float], float] = random.uniform,
        base_s: float = _PROVIDER_BACKOFF_BASE_S,
        max_s: float = _PROVIDER_BACKOFF_MAX_S,
        slow_ms: int = _SLOW_FAIL_MS,
    ) -> None:
        self._caps = _provider_inflight_caps() if caps is None else caps
        self._now = now
        self._sleep = sleep if sleep is not None else _gate_sleep
        self._uniform = uniform
        self._base_s = base_s
        self._max_s = max_s
        self._slow_ms = slow_ms
        self._lanes: dict[str, _GateLane] = {}
        self._lock = threading.Lock()

    def _lane(self, key: str) -> _GateLane:
        """Fetch-or-create the lane — call only under ``self._lock``."""
        lane = self._lanes.get(key)
        if lane is None:
            lane = _GateLane(
                cap=self._caps.get(key, self._caps.get("*", _PROVIDER_INFLIGHT_DEFAULT))
            )
            self._lanes[key] = lane
        return lane

    def acquire(self, key: str | None, *, deadline: float | None = None) -> bool:
        """Wait for a provider slot; ``False`` if none frees before ``deadline``.

        On ``True`` the caller holds one of the lane's ``cap`` slots and
        MUST :meth:`release` it when the leg ends. A ``None`` key (the
        publisher leg's per-DOI hosts) is never gated. The wait ends
        early when the process starts draining — a parked leg must not
        outlive the SIGTERM budget.
        """
        if key is None:
            return True
        while True:
            with self._lock:
                lane = self._lane(key)
                now = self._now()
                if lane.not_before <= now and (
                    lane.cap <= 0 or lane.in_flight < lane.cap
                ):
                    lane.in_flight += 1
                    return True
                if deadline is not None:
                    # A backoff that outlives the leg's wall can never be
                    # waited out in time — defer now rather than burn the
                    # budget parked on a provider that stays sick.
                    if lane.not_before > now and lane.not_before >= deadline:
                        return False
                    remaining = deadline - now
                    if remaining <= 0:
                        return False
                    wait = min(max(lane.not_before - now, _GATE_POLL_S), remaining)
                else:
                    wait = max(lane.not_before - now, _GATE_POLL_S)
            if self._sleep(wait):
                return False  # draining

    def release(self, key: str | None) -> None:
        """Free the slot an :meth:`acquire` took (a no-op on a ``None`` key)."""
        if key is None:
            return
        with self._lock:
            lane = self._lanes[key]
            lane.in_flight = max(0, lane.in_flight - 1)

    def healthy(self, key: str | None) -> None:
        """A leg answered (any non-sick outcome): clear the provider's backoff."""
        if key is None:
            return
        with self._lock:
            lane = self._lane(key)
            lane.fails = 0
            lane.not_before = 0.0

    def fail(self, key: str | None) -> None:
        """A leg showed the dead-endpoint signature: arm the shared backoff.

        The next leg on this provider may not start before
        ``not_before`` — a full-jitter ``base * 2**(fails-1)`` wait that
        decays the probe cadence while failures keep landing.
        """
        if key is None:
            return
        with self._lock:
            lane = self._lane(key)
            lane.fails += 1
            delay = self._uniform(
                0.0, min(self._max_s, self._base_s * 2 ** (lane.fails - 1))
            )
            lane.not_before = self._now() + delay

    def note_outcome(
        self, key: str | None, event: str, duration_ms: int | None
    ) -> None:
        """Classify a leg outcome: slow failure arms backoff, else healthy."""
        if event in _GATE_FAIL_EVENTS and (duration_ms or 0) >= self._slow_ms:
            self.fail(key)
        else:
            self.healthy(key)


def _gate_sleep(seconds: float) -> bool:
    """Default gate wait — wakes early (``True``) when the process drains."""
    from precis.liveness import drain_sleep

    return drain_sleep(seconds)


def _record_deferral(
    store: Store,
    stub: StubRef,
    source: str,
    key: str | None,
    t0: float,
    *,
    timing: _LegTiming | None = None,
) -> None:
    """Breadcrumb a leg the provider gate couldn't admit before its wall.

    ``provider_wait`` is deliberately **not** a provider miss — nothing
    about the provider's answer was learned — and the claim query's
    ``attempts`` count ignores any pass that contains one, so the stub
    comes back on its base retry instead of backing off as if tried
    (gr500153; the legacy ``circuit_open`` event is excluded from the
    count the same way, which is what returns today's skipped stubs to
    the next-day retry).
    """
    _timed_event(
        store,
        stub.ref_id,
        timing=timing,
        source=source,
        event="provider_wait",
        payload={"provider": key},
        duration_ms=_ms(t0),
    )
    log.info(
        "fetch_oa: %s leg deferred for ref_id=%s — provider %s saturated "
        "or backing off; retried on the stub's next claim",
        source,
        stub.ref_id,
        key,
    )


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


# The default worker batch is 32 refs. One compact summary per ref bounds
# diagnostic log volume while covering the full pass that can hit the deadline.
_TIMING_SAMPLE_REFS = 32
_timing_local = threading.local()


@dataclass
class _LegTiming:
    source: str
    gate_ms: float = 0.0
    limiter_db_ms: float = 0.0
    limiter_sleep_ms: float = 0.0
    request_ms: float = 0.0
    event_db_ms: float = 0.0
    outcome: str = "in_flight"
    limiter_providers: set[str] = field(default_factory=set)
    deadline_remaining_ms: float | None = None

    def record_limiter(self, provider: str, phase: str, elapsed_ms: float) -> None:
        self.limiter_providers.add(provider)
        if phase == "db":
            self.limiter_db_ms += elapsed_ms
        elif phase == "sleep":
            self.limiter_sleep_ms += elapsed_ms


@dataclass
class _RefTiming:
    ref_id: int
    started: float
    claim_ms: float
    pass_deadline: float | None = None
    legs: list[_LegTiming] = field(default_factory=list)
    active_leg: _LegTiming | None = None
    retraction_ms: float = 0.0
    markup_ms: float = 0.0
    done_event_db_ms: float = 0.0


def _current_ref_timing() -> _RefTiming | None:
    return getattr(_timing_local, "ref", None)


def _timed_event(
    store: Store,
    ref_id: int,
    *,
    timing: _LegTiming | None = None,
    **event: Any,
) -> None:
    trace = _current_ref_timing()
    measured_leg = timing or (trace.active_leg if trace is not None else None)
    if trace is None and timing is None:
        store.append_event(ref_id, **event)
        return
    started = time.perf_counter()
    try:
        store.append_event(ref_id, **event)
    finally:
        if trace is not None:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if measured_leg is not None:
                measured_leg.event_db_ms += elapsed_ms
            else:
                trace.done_event_db_ms += elapsed_ms


def _observe_limiter_wait(timing: _LegTiming) -> Callable[[str, str, float], None]:
    return timing.record_limiter


def _log_ref_timing(trace: _RefTiming) -> None:
    def _format_leg(leg: _LegTiming) -> str:
        deadline = (
            f",deadline_remaining={leg.deadline_remaining_ms:.0f}ms"
            if leg.deadline_remaining_ms is not None
            else ""
        )
        return (
            f"{leg.source}:{leg.outcome}"
            f"(gate={leg.gate_ms:.0f}ms"
            f",limiter_db={leg.limiter_db_ms:.0f}ms"
            f",limiter_sleep={leg.limiter_sleep_ms:.0f}ms"
            f",request={leg.request_ms:.0f}ms"
            f",request_minus_limiter="
            f"{max(0.0, leg.request_ms - leg.limiter_db_ms - leg.limiter_sleep_ms):.0f}ms"
            f",event_db={leg.event_db_ms:.0f}ms"
            f"{deadline}"
            f",providers={','.join(sorted(leg.limiter_providers)) or '-'})"
        )

    legs = ";".join(_format_leg(leg) for leg in trace.legs)
    pass_remaining = (
        (trace.pass_deadline - _clock()) * 1000
        if trace.pass_deadline is not None
        else None
    )
    log.info(
        "fetch_oa timing ref_id=%d ref_ms=%.0f claim_db_ms=%.0f "
        "pass_remaining_ms=%s retraction_ms=%.0f markup_ms=%.0f "
        "final_db_ms=%.0f legs=%s",
        trace.ref_id,
        (time.perf_counter() - trace.started) * 1000,
        trace.claim_ms,
        f"{pass_remaining:.0f}" if pass_remaining is not None else "-",
        trace.retraction_ms,
        trace.markup_ms,
        trace.done_event_db_ms,
        legs or "-",
    )


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
    of {DOI, arXiv, S2 id} is registered. **Escape hatch:** a ref that
    DOES carry a PDF but is stamped ``meta.markup_refetch`` also
    qualifies (:func:`~precis.store._stub_predicate.stub_predicate_sql`'s
    ``pin_meta_key``) — that pin marks a front-matter-only Elsevier
    preview (gr372781 item 3: an entitlement-limited ``%PDF-`` page 1
    that ingested silently as a handful of chunks), which is worse than
    no body at all but looks "done" to the plain ``pdf_sha256 IS NULL``
    predicate; :func:`precis.ingest.paper_hygiene.
    requeue_front_matter_only_papers` stamps the pin, and
    :func:`precis.ingest.db_writer.register_aliases_and_maybe_upgrade`
    is what actually replaces the body once the re-fetch lands. Excludes
    stubs tried by *any* ``fetcher:%`` source within an
    **exponentially-widening** window —
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
    window doubles per prior **pass** —
    ``base * 2^(attempts-1)``, capped at ``backoff_max_hours`` — so a
    closed-access paper settles to one retry per ~30 days rather than
    daily. It never gives up entirely (a paper can become OA later),
    it just slows down. Content-duplicate stubs are resolved out of
    the backlog separately, at ingest dedup time (see
    ``precis.ingest.add``), so they don't even reach the cap.

    ``attempts`` counts distinct fetch *passes*, not raw ``fetcher:%``
    leg-events: one cascade writes one event per leg, so a naive
    ``count(*)`` jumped the exponent to double digits on the very first
    pass and pinned the window at the cap immediately — a single
    transient timeout then cost a 30-day wait. Events are collapsed to
    hour-buckets (same convention as :meth:`Store.stub_backlog`'s
    ``attempts`` column) to recover the pass count, so the first retry
    lands the next day.

    One escape hatch: a ``meta.oa_requeued.at`` stamp *newer than the
    last fetch attempt* (an operator re-queue or an explicit-acquire
    pin — ``Store.pin_stub_for_fetch``) bypasses the window for exactly
    one retry; the attempt it triggers dates the stamp, so backoff
    resumes unless someone stamps again. The pin itself declines to
    re-stamp while a stamp is unconsumed or the last attempt is under
    24 h old, so repeated acquires of one cite do not chain bypasses.

    Ordering: **``prio`` ascending, NULLs last** — the ``stub_rank``
    pass writes ``refs.prio`` (1=hottest .. 10=coldest, ``NULL`` =
    unranked) and the fetcher spends its claim budget on the hottest
    stubs first. An *explicit* acquire pre-empts that ranking entirely:
    ``Store.pin_stub_for_fetch`` pins ``prio = 1`` with
    ``meta.prio_by = 'acquire'`` at request time, so a hand-requested
    paper claims on the very next pass instead of waiting unranked
    (``NULL`` sorts behind every ranked stub) for ``stub_rank``'s
    enrich→embed→rank cycle. Ties (including every unranked stub, which all sort
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
                -- ``attempts`` = distinct fetch *passes*, not raw leg-events.
                -- One ``_run_cascade`` run appends 10-14 ``fetcher:%%`` events
                -- (one per leg) within seconds-to-minutes; counting rows made
                -- ``attempts`` jump to double digits on the very first pass, so
                -- ``base * 2^(attempts-1)`` saturated at the 720h cap after a
                -- single pass -- a lone transient timeout cost a 30-day wait.
                -- Collapsing to hour-buckets (passes are >=24h apart under the
                -- backoff, a pass's own legs land within the same hour) recovers
                -- the pass count so the first retry is next-day, not next-month.
                --
                -- A pass containing a deferral (``provider_wait``, and the
                -- retired skip-breaker's ``circuit_open``) didn't finish trying
                -- the stub — some provider's legs were parked, not answered —
                -- so its bucket is excluded from ``attempts`` entirely
                -- (gr500153): a stub whose providers couldn't be reached stays
                -- on the base window instead of backing off as if tried. The
                -- exclusion is retroactive through ref_events, which is what
                -- returns today's circuit-skipped stubs to the next-day retry.
                -- ``last_ts`` still sees those events — the pass did happen —
                -- so the window anchors at the most recent contact attempt.
                SELECT count(*) FILTER (WHERE NOT b.deferred) AS attempts,
                       max(b.last_ts) AS last_ts
                  FROM (
                        SELECT date_trunc('hour', e.ts) AS bucket,
                               bool_or(e.event IN
                                       ('circuit_open', 'provider_wait'))
                                 AS deferred,
                               max(e.ts) AS last_ts
                          FROM ref_events e
                         WHERE e.ref_id = r.ref_id
                           AND e.source LIKE 'fetcher:%%'
                         GROUP BY 1
                  ) b
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
                   AND q.retired_at IS NULL
                   AND t.namespace = 'STATUS' AND t.value = 'active'
          ) qb ON TRUE
         WHERE {stub_predicate_sql("r", pin_meta_key="markup_refetch")}
           AND (
                 fe.last_ts IS NULL
                 OR fe.last_ts < now() - (
                      LEAST(
                        %(retry_after)s::double precision
                          * POWER(2, GREATEST(fe.attempts - 1, 0)),
                        %(backoff_max)s::double precision
                      ) * INTERVAL '1 hour'
                 )
                 -- Re-queued-since-last-attempt bypasses the backoff once:
                 -- an operator/explicit-acquire stamp (`meta.oa_requeued.at`,
                 -- see Store.pin_stub_for_fetch) newer than the last fetcher
                 -- event means "try again NOW"; after that attempt the stamp
                 -- is older than last_ts and normal backoff resumes. Legacy
                 -- stamps without `at`, or none at all, yield NULL → no bypass.
                 OR (r.meta #>> '{{oa_requeued,at}}')::timestamptz > fe.last_ts
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
         -- A `markup_refetch` pin is an operator's explicit re-fetch
         -- request; within a prio it outranks the acquire backlog, whose
         -- newest-first tiebreak would otherwise starve the old refs a
         -- re-fetch batch targets (td461154).
         ORDER BY r.prio ASC NULLS LAST,
                  jsonb_exists(r.meta, 'markup_refetch') DESC,
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


def _meta_wait_deadline(stop_at: float | None) -> float | None:
    """Wait bound for an *optional* provider call (retraction gate, DOI→arXiv
    resolve): the earlier of the metadata wall and the stub wall. These
    checks enrich but never gate the fetch, so they wait for a degraded
    provider only as long as one metadata leg would take, then skip."""
    wall = _meta_wall_s()
    d = time.perf_counter() + wall if wall > 0 else None
    if stop_at is not None and (d is None or stop_at < d):
        return stop_at
    return d


def _apply_retraction_gate(
    store: Store,
    stub: StubRef,
    *,
    mailto: str,
    gate: _ProviderGate | None = None,
    stop_at: float | None = None,
) -> bool:
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

    ``gate`` is the pass provider gate: the check is a Crossref call,
    so it takes a ``crossref`` lane slot like any leg. When Crossref is
    saturated or backing off past the metadata wait bound
    (:func:`_meta_wait_deadline`) the check skips outright — the
    docstring contract is already "a provenance-check outage must never
    block acquiring a PDF", so skipping produces the same proceed
    decision without parking a worker on a dead endpoint. A slow
    ``None`` result — the timeout signature — arms the lane's backoff.
    """
    acquired = False
    if gate is not None:
        acquired = gate.acquire("crossref", deadline=_meta_wait_deadline(stop_at))
        if not acquired:
            log.info(
                "fetch_oa: retraction gate deferred for ref_id=%s — "
                "crossref lane busy; proceeding without the check",
                stub.ref_id,
            )
            return False
    gate_t0 = time.perf_counter()
    try:
        result = _check_stub_retraction(stub, mailto=mailto)
    finally:
        if acquired and gate is not None:
            gate.release("crossref")
    if gate is not None and acquired:
        gate_ms = _ms(gate_t0)
        # A fast ``None`` is ambiguous (no DOI, unknown DOI, or a fast
        # swallowed error) — it carries no health signal, so it neither
        # arms nor resets the backoff. A slow ``None`` is the
        # dead-endpoint signature; a real result is a healthy answer.
        if result is None and gate_ms >= _SLOW_FAIL_MS:
            gate.fail("crossref")
        elif result is not None:
            gate.healthy("crossref")
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
        _timed_event(
            store,
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
_OPENALEX_BALANCE_SOURCE = RULES_BY_ID[
    "fetch_oa:openalex_balance/openalex-content-credits-low"
].rule
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
    require_rate_limit("openalex")
    with http_client(timeout=_API_TIMEOUT_S, user_agent=None) as client:
        resp = client.get(_OPENALEX_RATE_LIMIT_URL, params={"api_key": api_key})
        resp.raise_for_status()
        data = resp.json()
    rl = data.get("rate_limit") or {}
    raw = rl.get("credits_remaining")
    return int(raw) if isinstance(raw, (int, float)) else None


def check_openalex_balance(store: Store, *, api_key: str) -> int | None:
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
    deadline: float | None = None,
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
    for i, url in enumerate(urls):
        if i and deadline is not None and time.perf_counter() >= deadline:
            errors.append("remaining candidates skipped: leg deadline reached")
            break
        try:
            size_bytes = _download_pdf(url, target, deadline=deadline)
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


def _validate_elsevier_body(target: Path) -> None:
    """Require structured article body, not a valid HTTP 200 preview.

    OA=false is not a refusal: institutional keys can retrieve closed full
    text. Body structure, rather than byte/character count, is the gate.
    """
    from precis.ingest.markup import parse_elsevier

    parse_elsevier(target.read_bytes())


def _try_elsevier(
    stub: StubRef,
    *,
    inbox_dir: Path,
    api_key: str,
    deadline: float | None = None,
) -> FetchOutcome | None:
    """Try the Elsevier Article Retrieval API for one stub.

    Returns ``None`` (silent fall-through, no event) when there's no
    API key, no DOI, the DOI is malformed, or its prefix isn't an
    Elsevier one. Otherwise hits ``content/article/doi/<doi>`` with the
    API key. An XML preflight must contain a real article body before
    requesting ``Accept: application/pdf``: valid PDF magic alone also
    admits entitlement-limited previews. Missing body records
    ``fetch_failed`` with reason ``entitlement`` and continues the cascade.
    OA=false is allowed when the key can retrieve full text.
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
    # The watcher ignores .staging; preflight XML must never be an intake.
    preflight = inbox_dir / ".staging" / target.with_suffix(".entitlement.xml").name
    preflight.parent.mkdir(parents=True, exist_ok=True)
    try:
        _download_markup(
            url,
            preflight,
            extra_headers={"X-ELS-APIKey": api_key, "Accept": "text/xml"},
            deadline=deadline,
        )
        _validate_elsevier_body(preflight)
        size_bytes = _download_pdf(
            url,
            target,
            extra_headers={"X-ELS-APIKey": api_key, "Accept": "application/pdf"},
            deadline=deadline,
        )
    except Exception as exc:
        from precis.ingest.markup import MarkupParseError

        return FetchOutcome(
            event="fetch_failed",
            payload={
                "doi": stub.doi,
                "url": url,
                "error": str(exc)[:200],
                **(
                    {"reason": "entitlement"}
                    if isinstance(exc, MarkupParseError)
                    else {}
                ),
            },
            duration_ms=_ms(t0),
        )
    finally:
        preflight.unlink(missing_ok=True)
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
    deadline: float | None = None,
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
    failure instead of a silent short paper. Validate before recording
    ``fetch_ok`` or publishing the trigger, so that failure continues the
    acquisition cascade rather than stranding the paper downstream.

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
            deadline=deadline,
        )
        _validate_elsevier_body(target)
    except Exception as exc:
        from precis.ingest.markup import MarkupParseError

        target.unlink(missing_ok=True)
        return FetchOutcome(
            event="fetch_failed",
            payload={
                "doi": stub.doi,
                "url": url,
                "error": str(exc)[:200],
                **(
                    {"reason": "entitlement"}
                    if isinstance(exc, MarkupParseError)
                    else {}
                ),
            },
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
    deadline: float | None = None,
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
            url,
            target,
            extra_headers={"Wiley-TDM-Client-Token": token},
            deadline=deadline,
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
    deadline: float | None = None,
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
    except RateLimited:
        return _local_rate_limited(stub, t0, provider="unpaywall")
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
        size_bytes = _download_pdf(url, target, deadline=deadline)
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


def _arxiv_id_from_url(url: str | None) -> str | None:
    """Bare arXiv id out of an ``arxiv.org/abs|pdf/<id>`` URL, else ``None``."""
    if not url:
        return None
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in ("arxiv.org", "www.arxiv.org"):
        return None
    m = re.match(r"^/(?:abs|pdf)/(.+?)(?:\.pdf)?/?$", parts.path)
    return _normalize_arxiv_id(m.group(1)) if m else None


def _query_openalex_arxiv_id(doi: str, *, email: str) -> str | None:
    """arXiv id of ``doi``'s preprint per OpenAlex location metadata, else ``None``.

    Scans ``best_oa_location`` / ``locations`` for an ``arxiv.org``
    ``pdf_url`` or ``landing_page_url``. Fixed-host API call through
    :func:`safe_get`.
    """
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_get

    require_rate_limit("openalex")
    params = {"mailto": email} if email else {}
    with http_client(
        timeout=_API_TIMEOUT_S, headers={"User-Agent": _user_agent_header(email)}
    ) as client:
        resp = safe_get(
            client, f"https://api.openalex.org/works/doi:{doi}", params=params
        )
        resp.raise_for_status()
        data = resp.json()
    locs = [data.get("best_oa_location") or {}, *(data.get("locations") or [])]
    for loc in locs:
        for key in ("pdf_url", "landing_page_url"):
            found = _arxiv_id_from_url(loc.get(key))
            if found:
                return found
    return None


def _resolve_arxiv_id(stub: StubRef, *, email: str) -> str | None:
    """arXiv id for a DOI-only stub, from the DOI itself or OpenAlex metadata.

    A stub registered with only a DOI can still have a free arXiv copy
    (gr453862: SCAN, closed APS DOI, preprint 1504.03028). arXiv-issued
    DOIs (``10.48550/arXiv.<id>``) carry the id directly; otherwise ask
    OpenAlex. Never raises — a lookup failure just leaves the stub as is.
    """
    if stub.arxiv or not stub.doi or not _DOI_RE.match(stub.doi):
        return None
    m = re.match(r"^10\.48550/arxiv\.(.+)$", stub.doi, re.IGNORECASE)
    if m:
        return _normalize_arxiv_id(m.group(1))
    try:
        return _query_openalex_arxiv_id(stub.doi, email=email)
    except Exception as exc:
        log.info("fetch_oa: arxiv id lookup failed for %s: %s", stub.doi, exc)
        return None


def _try_arxiv(
    stub: StubRef,
    *,
    inbox_dir: Path,
    deadline: float | None = None,
) -> FetchOutcome | None:
    """Try direct arXiv PDF fetch for one stub.

    arXiv hosts every preprint as a PDF at a deterministic URL:
    ``https://arxiv.org/pdf/<id>.pdf`` (also accepts versioned ids
    like ``2401.12345v2``). No API key, no metadata round-trip. The
    shared ``arxiv`` limiter paces the download (~1 request per 3s
    cluster-wide); a refusal returns ``rate_limited`` without fetching.

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
        # Service "arxiv": every attempt (not just the first) takes a token
        # from the shared arxiv limiter and follows its backoff policy.
        size_bytes = _download_pdf(url, target, service="arxiv", deadline=deadline)
    except RateLimited:
        return FetchOutcome(
            event="rate_limited",
            payload={"arxiv": arxiv_id, "provider": "arxiv", "limiter": "shared"},
            duration_ms=_ms(t0),
        )
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
    deadline: float | None = None,
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
        size_bytes = _download_pdf(oa_url, target, deadline=deadline)
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
    store: Store | None = None,
    email: str = "",
    deadline: float | None = None,
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
    except RateLimited:
        return _local_rate_limited(stub, t0, provider="openalex")
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
        size_bytes = _download_pdf(
            _with_api_key(pdf_url, api_key),
            target,
            service="openalex-content",
            deadline=deadline,
        )
    except RateLimited:
        return _local_rate_limited(stub, t0, provider="openalex-content")
    except Exception as exc:
        # httpx folds the *full* request URL — with ``api_key=<key>`` — into its
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
    deadline: float | None = None,
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
    except RateLimited:
        return _local_rate_limited(stub, t0, provider="crossref")
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
        host_type="crossref_tdm",
        t0=t0,
        deadline=deadline,
    )


def _try_openalex(
    stub: StubRef,
    *,
    inbox_dir: Path,
    email: str,
    deadline: float | None = None,
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
    except RateLimited:
        return _local_rate_limited(stub, t0, provider="openalex")
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
        host_type="openalex",
        t0=t0,
        deadline=deadline,
    )


def _try_europepmc(
    stub: StubRef,
    *,
    inbox_dir: Path,
    deadline: float | None = None,
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
    except RateLimited:
        return _local_rate_limited(stub, t0, provider="europepmc")
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
        stub,
        [url],
        inbox_dir=inbox_dir,
        host_type="europepmc",
        t0=t0,
        deadline=deadline,
    )


def _try_core(
    stub: StubRef,
    *,
    inbox_dir: Path,
    api_key: str,
    deadline: float | None = None,
) -> FetchOutcome | None:
    """Try CORE's green-OA repository copies for one stub.

    Returns ``None`` (silent fall-through) without a key or DOI.
    Searches CORE for the DOI and downloads the first repository
    ``downloadUrl`` that passes the ``%PDF-`` guard — repositories
    bot-block unevenly, so candidates (sent with the honest precis UA) are
    tried in turn.
    """
    if not api_key or not stub.doi or not _DOI_RE.match(stub.doi):
        return None
    t0 = time.perf_counter()
    try:
        pdf_urls = _query_core_fulltext_urls(stub.doi, api_key=api_key)
    except RateLimited as exc:
        return FetchOutcome(
            event="rate_limited",
            payload={"doi": stub.doi, "provider": exc.provider, "limiter": "shared"},
            duration_ms=_ms(t0),
        )
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
        extra_headers={"User-Agent": _user_agent_header()},
        deadline=deadline,
    )


def _download_first(
    stub: StubRef,
    urls: list[str],
    *,
    inbox_dir: Path,
    host_type: str,
    t0: float,
    extra_headers: dict[str, str] | None = None,
    deadline: float | None = None,
) -> FetchOutcome:
    """Download the first URL whose bytes pass the ``%PDF-`` guard.

    Shared tail for the legs that resolve a list of candidate PDF URLs
    (publisher patterns aside, which inline this). Returns ``fetch_ok``
    on the first good PDF, else ``fetch_failed`` with the per-URL
    errors so the cascade falls through. ``extra_headers`` (e.g. a
    custom UA for repository hosts) is forwarded to every candidate.
    ``deadline`` (``perf_counter``) caps the whole tail: the first
    candidate always runs, later candidates are skipped once it passes,
    and each download's retry chain stops there too.
    """
    filename = _stub_filename(stub) + ".pdf"
    target = inbox_dir / filename
    inbox_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for i, url in enumerate(urls):
        if i and deadline is not None and time.perf_counter() >= deadline:
            errors.append("remaining candidates skipped: leg deadline reached")
            break
        try:
            size_bytes = _download_pdf(
                url, target, extra_headers=extra_headers, deadline=deadline
            )
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
    """Dark switch for the OA fetcher. Default off.

    Tolerant to whitespace / case so a YAML quoting quirk or trailing
    newline doesn't silently disable the pass (same shape as
    ``fetch_google_patents._is_enabled``).
    """
    return os.environ.get("PRECIS_OA_FETCH", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )


#: Clock for the pass wall budget; a module attribute so tests can drive it
#: without touching the process-wide ``time.monotonic``.
_clock = time.monotonic


def _pass_wall_s() -> float:
    """``PRECIS_OA_FETCH_PASS_WALL_S`` — default **300**.

    Wall-time budget for one :func:`run_oa_fetch_pass`: no new stub is
    started once this much time has elapsed since the stub loop began (the
    first stub always runs). A stub in flight is not interrupted; its legs
    carry their own timeouts. Unstarted stubs hold no lease — the claim's
    row locks end with its connection — so they stay claimable next pass.
    gr479615: on melchior a 32-stub batch took ~31 min (TLS-handshake
    timeouts × retries), holding the serial worker loop ~60 min and making
    every hourly cadence it owns fire 1–2 h apart. Same shape as
    ``hub_refine._pass_wall_s``. ``0`` or less disables the budget.
    """
    try:
        return float(os.environ.get("PRECIS_OA_FETCH_PASS_WALL_S", "300"))
    except ValueError:
        return 300.0


def _env_seconds(name: str, default: float) -> float:
    """Seconds-valued env knob; a non-numeric value falls back to ``default``.

    ``0`` or less disables the bound it feeds — same convention as
    :func:`_pass_wall_s`.
    """
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _pass_workers() -> int:
    """``PRECIS_OA_FETCH_WORKERS`` — default :data:`_PASS_WORKERS_DEFAULT` (6).

    Stubs processed concurrently inside one pass; ``1`` restores the
    original strictly-serial loop. Pacing is unaffected: every leg takes
    its tokens from the shared ``external_rate_limits`` rows regardless
    of which worker thread asks.
    """
    try:
        return max(
            1,
            int(os.environ.get("PRECIS_OA_FETCH_WORKERS", "")) or _PASS_WORKERS_DEFAULT,
        )
    except ValueError:
        return _PASS_WORKERS_DEFAULT


def _stub_wall_s() -> float:
    """``PRECIS_OA_FETCH_STUB_WALL_S`` — default :data:`_STUB_WALL_S_DEFAULT`.

    Per-stub wall budget: no new cascade leg starts past it. ``0``
    disables. See the throughput-knobs comment near
    :data:`_PASS_WORKERS_DEFAULT`.
    """
    return _env_seconds("PRECIS_OA_FETCH_STUB_WALL_S", _STUB_WALL_S_DEFAULT)


def _leg_wall_s() -> float:
    """``PRECIS_OA_FETCH_LEG_WALL_S`` — default :data:`_LEG_WALL_S_DEFAULT`.

    Retry budget for one leg's download tail. ``0`` disables (each
    download keeps its full ``_DOWNLOAD_POLICY`` retry shape).
    """
    return _env_seconds("PRECIS_OA_FETCH_LEG_WALL_S", _LEG_WALL_S_DEFAULT)


def _meta_wall_s() -> float:
    """``PRECIS_OA_FETCH_META_WALL_S`` — default :data:`_META_WALL_S_DEFAULT`.

    Retry budget for one metadata round-trip (Crossref/Europe PMC/
    Unpaywall queries — the JSON calls that retried 4–5× a 30 s timeout).
    ``0`` disables.
    """
    return _env_seconds("PRECIS_OA_FETCH_META_WALL_S", _META_WALL_S_DEFAULT)


def _meta_deadline() -> float | None:
    """``perf_counter`` deadline for one metadata call, ``None`` when disabled."""
    wall = _meta_wall_s()
    return time.perf_counter() + wall if wall > 0 else None


def run_oa_fetch_pass(
    store: Store,
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

    Stubs are processed by ``PRECIS_OA_FETCH_WORKERS`` (default 6)
    concurrent worker threads pulling from the claimed list until the
    pass wall budget is spent; the leg cascade inside a stub stays
    serial and provider-ordered. All pacing rides the shared
    ``external_rate_limits`` rows, so concurrency can't exceed any
    provider's published rate. ``1`` restores the serial loop.

    ``inbox_dir`` is taken from ``PRECIS_WATCH_INBOX`` when not passed
    explicitly. It **must** be the directory the ``precis ingest --watch``
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

    Deployment (Reto 2026-10-21, option A): the pass runs in its own
    daemon — ``precis worker --only fetch``, rendered as
    ``com.precis.worker-fetch`` / ``precis-worker-fetch.service`` by
    ``deploy/playbooks/20b-precis-worker-collapsed.yml`` on the
    ``precis_capabilities.oa_fetch`` host — rather than one slot inside
    the collapsed worker's serial rotation. While
    ``precis_worker_fetch_dedicated`` holds, every non-lane unit renders
    ``PRECIS_OA_FETCH=0`` explicitly, so ``'1'`` lives in exactly one
    process. ``--only`` bypasses the ``service_config`` pass_gate, so
    this env is the lane's only switch.
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

    # SI pass (docs/backlog/si-attachments.md): papers someone flagged with
    # ``put(mode='fetch-si')`` go BEFORE the stub claim — an attention-queued
    # SI fetch is a priority bump over the backlog, not a sweep. Best-effort:
    # it must never block the stub fetch.
    try:
        from precis.workers.si_fetch import run_si_pass

        si_res = run_si_pass(store, inbox_path, email=email)
        if si_res.get("claimed"):
            log.info("fetch_oa: si pass %s", si_res)
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("fetch_oa: si pass errored: %s", exc)

    claim_started = time.perf_counter()
    with store.pool.connection() as conn:
        stubs = claim_stubs_to_fetch(conn, limit=limit)
    claim_ms = (time.perf_counter() - claim_started) * 1000

    if not stubs:
        return {"claimed": 0, "ok": 0, "failed": 0}

    # Once per working pass, check the paid leg's runway and raise/clear the
    # low-balance alert. Best-effort — a failed poll never blocks the fetch.
    if openalex_content_key:
        check_openalex_balance(store, api_key=openalex_content_key)

    total = len(stubs)
    wall_s = _pass_wall_s()
    started = _clock()
    pass_deadline = started + wall_s if wall_s > 0 else None
    gate = _ProviderGate()
    stub_iter = iter(enumerate(stubs, start=1))
    counts = {"attempted": 0, "ok": 0, "failed": 0}
    pull_lock = threading.Lock()
    budget_logged = threading.Event()

    def _worker() -> None:
        """Pull stubs until the list or the pass wall budget runs out.

        The wall check happens under ``pull_lock`` together with the
        ``next()`` so the "first stub always runs" and the attempted
        count stay exact under N workers. A stub in flight is never
        interrupted — same contract as the serial loop — but its own
        per-stub/leg deadlines keep the tail bounded.
        """
        while True:
            with pull_lock:
                if wall_s > 0 and counts["attempted"] and _clock() - started >= wall_s:
                    if not budget_logged.is_set():
                        budget_logged.set()
                        log.info(
                            "fetch_oa: pass wall budget %.0fs spent after "
                            "%d/%d stubs; the rest stay claimable for the "
                            "next pass",
                            wall_s,
                            counts["attempted"],
                            total,
                        )
                    return
                item = next(stub_iter, None)
                if item is None:
                    return
                counts["attempted"] += 1
            i, stub = item
            trace = (
                _RefTiming(
                    ref_id=stub.ref_id,
                    started=time.perf_counter(),
                    claim_ms=claim_ms,
                    pass_deadline=pass_deadline,
                )
                if i <= _TIMING_SAMPLE_REFS
                else None
            )
            previous = getattr(_timing_local, "ref", None)
            _timing_local.ref = trace
            try:
                outcome = _process_stub(
                    store,
                    stub,
                    i,
                    total,
                    inbox_path,
                    email,
                    api_key,
                    wiley_token,
                    core_key,
                    openalex_content_key,
                    gate,
                )
            finally:
                _timing_local.ref = previous
                if trace is not None:
                    _log_ref_timing(trace)
            with pull_lock:
                counts[outcome] += 1

    workers = min(_pass_workers(), total)
    if workers <= 1:
        _worker()
    else:
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="oa-fetch"
        ) as pool:
            for _ in range(workers):
                pool.submit(_worker)
    return {
        "claimed": counts["attempted"],
        "ok": counts["ok"],
        "failed": counts["failed"],
    }


def _process_stub(
    store: Store,
    stub: StubRef,
    i: int,
    total: int,
    inbox_path: Path,
    email: str,
    api_key: str,
    wiley_token: str,
    core_key: str,
    openalex_content_key: str,
    gate: _ProviderGate,
) -> str:
    """Run one claimed stub through the gate + markup + PDF cascades.

    Returns ``"ok"`` when the stub was processed (regardless of whether a
    PDF landed) and ``"failed"`` when an unhandled exception escaped all
    per-leg guards — same accounting the serial loop kept.

    The stub wall (``_stub_wall_s``) bounds the whole stub: no *new* leg
    starts past it; a leg in flight finishes on its own timeouts.
    ``gate`` is the shared per-pass provider gate.
    """
    # gr191337-style monopolization is exactly what this pass is prone
    # to (up to ~11 network legs per stub, each capped at
    # ``_DOWNLOAD_TIMEOUT_S``) — a per-stub progress stamp is the whole
    # point of ``precis.workers.activity`` (see its module docstring).
    activity.note(f"stub {i}/{total} ref_id={stub.ref_id} doi={stub.doi or '-'}")
    stub_wall = _stub_wall_s()
    stop_at = time.perf_counter() + stub_wall if stub_wall > 0 else None
    try:
        # Retraction gate — cheapest possible check, ahead of every
        # other leg (including the markup-first pass): a retracted
        # paper has nothing worth chasing an OA copy for, so decide
        # this before paying for any download attempt.
        trace = _current_ref_timing()
        retraction_timing = (
            _LegTiming("retraction_check") if trace is not None else None
        )
        previous_leg = trace.active_leg if trace is not None else None
        if trace is not None and retraction_timing is not None:
            trace.legs.append(retraction_timing)
            trace.active_leg = retraction_timing
        retraction_started = time.perf_counter()
        retracted = False
        try:
            timing_scope = (
                observe_rate_limit_timing(_observe_limiter_wait(retraction_timing))
                if retraction_timing is not None
                else nullcontext()
            )
            with timing_scope:
                retracted = _apply_retraction_gate(
                    store, stub, mailto=email, gate=gate, stop_at=stop_at
                )
        except Exception:
            if retraction_timing is not None:
                retraction_timing.outcome = "error"
            raise
        else:
            if retraction_timing is not None:
                retraction_timing.outcome = "skip" if retracted else "continue"
        finally:
            if retraction_timing is not None:
                retraction_timing.request_ms = (
                    time.perf_counter() - retraction_started
                ) * 1000
                if stop_at is not None:
                    retraction_timing.deadline_remaining_ms = (
                        stop_at - time.perf_counter()
                    ) * 1000
            if trace is not None:
                trace.active_leg = previous_leg
                trace.retraction_ms = (time.perf_counter() - retraction_started) * 1000
        if retracted:
            return "ok"

        # Markup-first pass (gated by PRECIS_FETCH_MARKUP): try for a
        # structured full-text source before the PDF cascade. Best-
        # effort — its failure must never block the PDF fetch, which
        # runs unconditionally afterwards to acquire the printable.
        staged_markup: _StagedMarkup | None = None
        markup_timing = _LegTiming("markup_bundle") if trace is not None else None
        previous_leg = trace.active_leg if trace is not None else None
        if trace is not None and markup_timing is not None:
            trace.legs.append(markup_timing)
            trace.active_leg = markup_timing
        markup_started = time.perf_counter()
        try:
            timing_scope = (
                observe_rate_limit_timing(_observe_limiter_wait(markup_timing))
                if markup_timing is not None
                else nullcontext()
            )
            with timing_scope:
                staged_markup = _run_markup_cascade(
                    store,
                    stub,
                    inbox_path,
                    api_key,
                    gate=gate,
                    stop_at=stop_at,
                )
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "fetch_oa: markup pass errored for ref_id=%s: %s",
                stub.ref_id,
                exc,
            )
        finally:
            if markup_timing is not None:
                markup_timing.request_ms = (time.perf_counter() - markup_started) * 1000
                markup_timing.outcome = (
                    "staged" if staged_markup is not None else "none"
                )
                if stop_at is not None:
                    markup_timing.deadline_remaining_ms = (
                        stop_at - time.perf_counter()
                    ) * 1000
            if trace is not None:
                trace.active_leg = previous_leg
                trace.markup_ms = (time.perf_counter() - markup_started) * 1000
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
            gate=gate,
            stop_at=stop_at,
        )
        if staged_markup is not None:
            # gr170349: only now — after the companion PDF's fate is
            # known, however long that took — does the markup trigger
            # (and its sidecar) become watcher-visible, atomically.
            companion_name = pdf_path.name if pdf_path is not None else None
            _publish_markup_trigger(staged_markup, companion_name)
        return "ok"
    except Exception as exc:  # pragma: no cover — defensive
        log.warning(
            "fetch_oa: ref_id=%s unhandled error: %s",
            stub.ref_id,
            exc,
            exc_info=True,
        )
        return "failed"


def _markup_fetch_enabled() -> bool:
    """Whether the markup-first pass runs. Gated by ``PRECIS_FETCH_MARKUP``.

    Default-off: markup ingest is new; opt in per-host once the stub
    backlog has been exercised. See docs/backlog/dark-features-activation.md.
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
    url: str,
    target: Path,
    *,
    extra_headers: dict[str, str] | None = None,
    deadline: float | None = None,
) -> int:
    """Stream a markup document (XML/HTML/tarball) to ``target``.

    Mirrors :func:`_download_pdf` but without the ``%PDF-`` magic-byte
    guard — the producer validates structure at ingest and falls back to
    OCR on a bad parse, so a landing-page here is caught downstream
    rather than poisoning the corpus. Writes to ``<target>.part`` and
    renames on success. Returns the byte count; raises on an empty body.

    Runs under the same politeness core as :func:`_download_pdf`: one
    ``host:<netloc>`` limiter token per attempt (the publisher hosts are
    shared with the PDF leg), full-jitter backoff on transport/429/5xx
    only, ``Retry-After`` parks the host row. Any other 4xx is final.
    """
    netloc = urlparse(url).netloc.lower()
    return call_with_backoff(
        lambda: _download_markup_once(url, target, extra_headers),
        service=f"pdf:{netloc}",
        policy=_DOWNLOAD_POLICY,
        url=url,
        retry_if=_is_transient_download_error,
        deadline=deadline,
        now=time.perf_counter,
    )


def _download_markup_once(
    url: str, target: Path, extra_headers: dict[str, str] | None
) -> int:
    """One download attempt for :func:`_download_markup`."""
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


def _looks_like_tex(data: bytes) -> bool:
    """True when ``data`` is plain-text TeX (not merely ``%``/``\\``-led).

    Decodes as UTF-8 or latin-1 with no NUL in the first 4 KiB, and the
    first 64 KiB names a document (``\\documentclass`` / ``\\documentstyle``
    / ``\\begin{document}``).
    """
    if b"\x00" in data[:4096]:
        return False
    head = data[:65536]
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        text = head.decode("latin-1")
    return any(
        marker in text
        for marker in ("\\documentclass", "\\documentstyle", "\\begin{document}")
    )


def _classify_eprint(path: Path) -> tuple[str | None, bytes | None]:
    """Classify an arXiv e-print payload as ``(skip_reason, tex_bytes)``.

    PDF-only submissions serve the PDF itself at ``/e-print/<id>`` (gr465473).
    ``(None, None)``: a tar or gzipped tar bundle, stage as-is. ``(None,
    data)``: a single TeX file (bare, or gzipped -- arXiv's usual form for
    single-file submissions), to be staged as ``.tex`` so the LaTeX reader's
    single-file branch handles it. Gzip is inflated in memory under the
    LaTeX reader's total-bytes cap (``eprint_too_large`` past it).
    """
    import gzip
    import zlib

    from precis.ingest import markup

    with path.open("rb") as fh:
        head = fh.read(512)
    if head[257:262] == b"ustar":
        return None, None
    if head.startswith(b"%PDF-"):
        return "eprint_is_pdf", None
    cap = markup._LATEX_MAX_TOTAL_BYTES
    if head.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(path, "rb") as gz:
                inner_head = gz.read(512)
                if inner_head[257:262] == b"ustar":
                    return None, None
                chunks = [inner_head]
                total = len(inner_head)
                while total <= cap:
                    chunk = gz.read(1 << 20)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
        except (OSError, EOFError, zlib.error):
            return "eprint_unrecognised", None
        if total > cap:
            return "eprint_too_large", None
        data = b"".join(chunks)
    else:
        data = path.read_bytes()
        if len(data) > cap:
            return "eprint_too_large", None
    if _looks_like_tex(data):
        return None, data
    return "eprint_unrecognised", None


@dataclass(frozen=True)
class _MarkupLeg:
    """One markup-first source: how to build its URL and name its file."""

    source: str
    fmt: str
    #: Filename suffix for the downloaded trigger (drives watcher routing).
    suffix: str
    #: Returns the fetch URL for ``stub``, or ``None`` to skip this leg.
    url_for: Callable[[StubRef], str | None]
    #: Shared rate-limiter key the download must clear, or ``None``.
    limiter: str | None = None


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
    _MarkupLeg(
        _SOURCE_ARXIV_HTML, "arxiv_html", ".html", _arxiv_html_url, limiter="arxiv"
    ),
    _MarkupLeg(
        _SOURCE_ARXIV_SOURCE, "latex", ".tar.gz", _arxiv_source_url, limiter="arxiv"
    ),
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
    store: Store,
    stub: StubRef,
    inbox_dir: Path,
    elsevier_api_key: str = "",
    *,
    gate: _ProviderGate | None = None,
    stop_at: float | None = None,
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

    ``gate``/``stop_at`` are the same per-pass provider gate and stub
    wall :func:`_run_cascade` honours: a leg the gate can't admit before
    its wall defers with a ``provider_wait`` event (never a provider
    miss — retried on the stub's next claim), and no new leg starts past
    ``stop_at``.
    """
    if not _markup_fetch_enabled():
        return None

    def _leg_deadline() -> float | None:
        leg_wall = _leg_wall_s()
        d = time.perf_counter() + leg_wall if leg_wall > 0 else None
        if stop_at is not None and (d is None or stop_at < d):
            return stop_at
        return d

    # Both legs below stage their content download *and* sidecar under
    # ``.staging`` — unwatched — so nothing watcher-visible appears until
    # the caller atomically publishes it once the companion PDF's fate is
    # known (gr170349).
    staging_dir = inbox_dir / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    def _expired() -> bool:
        return stop_at is not None and time.perf_counter() >= stop_at

    if gate is None:
        gate = _ProviderGate()

    elsevier_outcome: FetchOutcome | None = None
    elsevier_key = _PROVIDER_KEYS.get(_SOURCE_ELSEVIER_XML)
    if not _expired():
        elsevier_t0 = time.perf_counter()
        if not gate.acquire(elsevier_key, deadline=_leg_deadline()):
            _record_deferral(
                store, stub, _SOURCE_ELSEVIER_XML, elsevier_key, elsevier_t0
            )
        else:
            try:
                elsevier_outcome = _try_elsevier_markup(
                    stub,
                    inbox_dir=staging_dir,
                    api_key=elsevier_api_key,
                    deadline=_leg_deadline(),
                )
            except Exception as exc:  # pragma: no cover — defensive
                elsevier_outcome = None
                gate.note_outcome(elsevier_key, "api_error", _ms(elsevier_t0))
                log.warning(
                    "fetch_oa markup: elsevier_xml leg errored for ref_id=%s: %s",
                    stub.ref_id,
                    exc,
                )
            finally:
                gate.release(elsevier_key)
            if elsevier_outcome is not None:
                gate.note_outcome(
                    elsevier_key,
                    elsevier_outcome.event,
                    elsevier_outcome.duration_ms,
                )
    if elsevier_outcome is not None:
        _timed_event(
            store,
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
    # Limiter keys already refused this stub: a second leg on the same key
    # would only sit out another bounded wait to be refused again.
    refused: set[str] = set()
    for leg in _MARKUP_LEGS:
        if _expired():
            break
        key = _PROVIDER_KEYS.get(leg.source)
        t0 = time.perf_counter()
        if not gate.acquire(key, deadline=_leg_deadline()):
            _record_deferral(store, stub, leg.source, key, t0)
            continue
        # The slot is held through the leg's whole tail and released in
        # the finally — an unexpected raise mid-leg (classify, sidecar
        # write, event append) must not leak it and silently shrink the
        # provider's cap for the rest of the pass.
        try:
            try:
                url = leg.url_for(stub)
            except Exception as exc:
                leg_ms = _ms(t0)
                gate.note_outcome(key, "api_error", leg_ms)
                _timed_event(
                    store,
                    stub.ref_id,
                    source=leg.source,
                    event="api_error",
                    payload={"error": str(exc)[:200]},
                    duration_ms=leg_ms,
                )
                continue
            if url is None:
                continue  # no identifier for this leg — silent skip
            if leg.limiter is not None:
                try:
                    if leg.limiter in refused:
                        raise RateLimited(leg.limiter)
                    require_rate_limit(leg.limiter)
                except RateLimited:
                    refused.add(leg.limiter)
                    leg_ms = _ms(t0)
                    gate.note_outcome(key, "rate_limited", leg_ms)
                    _timed_event(
                        store,
                        stub.ref_id,
                        source=leg.source,
                        event="rate_limited",
                        payload={
                            "url": url,
                            "provider": leg.limiter,
                            "limiter": "shared",
                        },
                        duration_ms=leg_ms,
                    )
                    continue
            final_path = inbox_dir / (_stub_filename(stub) + leg.suffix)
            staged_path = staging_dir / final_path.name
            try:
                size = _download_markup(url, staged_path, deadline=_leg_deadline())
                if leg.source == _SOURCE_ARXIV_SOURCE:
                    skip_reason, tex_bytes = _classify_eprint(staged_path)
                    if skip_reason is not None:
                        staged_path.unlink(missing_ok=True)
                        gate.note_outcome(key, "no_oa_version", _ms(t0))
                        _timed_event(
                            store,
                            stub.ref_id,
                            source=leg.source,
                            event="no_oa_version",
                            payload={
                                "url": url,
                                "reason": skip_reason,
                                "bytes": size,
                            },
                            duration_ms=_ms(t0),
                        )
                        continue
                    if tex_bytes is not None:
                        # Single-file TeX: restage under a ``.tex`` name so
                        # the watcher's reader takes its single-file branch
                        # (the sidecar and publish step key on the staged
                        # filename).
                        staged_path.unlink(missing_ok=True)
                        final_path = inbox_dir / (_stub_filename(stub) + ".tex")
                        staged_path = staging_dir / final_path.name
                        staged_path.write_bytes(tex_bytes)
                        size = len(tex_bytes)
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
            except Exception as exc:
                log.warning(
                    "fetch_oa markup: %s leg failed for ref_id=%s (%s): %s",
                    leg.source,
                    stub.ref_id,
                    url,
                    exc,
                )
                leg_ms = _ms(t0)
                gate.note_outcome(key, "fetch_failed", leg_ms)
                _timed_event(
                    store,
                    stub.ref_id,
                    source=leg.source,
                    event="fetch_failed",
                    payload={"url": url, "error": str(exc)[:200]},
                    duration_ms=leg_ms,
                )
                continue
            gate.healthy(key)
            _timed_event(
                store,
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
        finally:
            gate.release(key)
    return None


def _run_cascade(
    store: Store,
    stub: StubRef,
    inbox_dir: Path,
    email: str,
    api_key: str,
    wiley_token: str,
    core_key: str,
    openalex_content_key: str = "",
    printable_only: bool = False,
    gate: _ProviderGate | None = None,
    stop_at: float | None = None,
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

    ``gate`` is the pass provider gate: a leg the gate can't admit
    before its wall defers with a ``provider_wait`` event — never a
    provider miss — and the stub is retried on its next claim; a healthy
    provider admits immediately, so nothing is skipped outright.
    ``stop_at`` is a ``perf_counter`` instant (the stub wall): no new
    leg starts past it and it caps each leg's download-retry tail.
    """
    if gate is None:
        gate = _ProviderGate()
    # A DOI-only stub may still have a free arXiv copy: resolve the id from
    # metadata so the arxiv leg runs instead of the stub falling to
    # ``no_oa_version`` (gr453862).
    if not stub.arxiv:
        if gate.acquire("openalex", deadline=_meta_wait_deadline(stop_at)):
            arxiv_t0 = time.perf_counter()
            try:
                resolved_arxiv = _resolve_arxiv_id(stub, email=email)
            finally:
                gate.release("openalex")
            arxiv_ms = _ms(arxiv_t0)
            if resolved_arxiv:
                stub = dataclasses.replace(stub, arxiv=resolved_arxiv)
                gate.healthy("openalex")
            elif arxiv_ms >= _SLOW_FAIL_MS:
                gate.fail("openalex")
            # A fast ``None`` is ambiguous (no arXiv location, or a fast
            # swallowed error) — no health signal, like the gate above.
        else:
            log.info(
                "fetch_oa: arxiv id resolve deferred for ref_id=%s — "
                "openalex lane busy",
                stub.ref_id,
            )
    providers: list[tuple[str, Any]] = [
        (
            _SOURCE_PUBLISHER,
            lambda dl: _try_publisher(stub, inbox_dir=inbox_dir, deadline=dl),
        ),
    ]
    if api_key:
        providers.append(
            (
                _SOURCE_ELSEVIER,
                lambda dl: _try_elsevier(
                    stub, inbox_dir=inbox_dir, api_key=api_key, deadline=dl
                ),
            )
        )
    if wiley_token:
        providers.append(
            (
                _SOURCE_WILEY,
                lambda dl: _try_wiley(
                    stub, inbox_dir=inbox_dir, token=wiley_token, deadline=dl
                ),
            )
        )
    if email:
        providers.append(
            (
                _SOURCE_UNPAYWALL,
                lambda dl: _try_unpaywall(
                    stub, inbox_dir=inbox_dir, email=email, deadline=dl
                ),
            )
        )
        providers.append(
            (
                _SOURCE_CROSSREF,
                lambda dl: _try_crossref(
                    stub, inbox_dir=inbox_dir, email=email, deadline=dl
                ),
            )
        )
    providers.append(
        (
            _SOURCE_OPENALEX,
            lambda dl: _try_openalex(
                stub, inbox_dir=inbox_dir, email=email, deadline=dl
            ),
        )
    )
    providers.append(
        (
            _SOURCE_EUROPEPMC,
            lambda dl: _try_europepmc(stub, inbox_dir=inbox_dir, deadline=dl),
        )
    )
    if core_key:
        providers.append(
            (
                _SOURCE_CORE,
                lambda dl: _try_core(
                    stub, inbox_dir=inbox_dir, api_key=core_key, deadline=dl
                ),
            )
        )
    providers.append(
        (
            _SOURCE_ARXIV,
            lambda dl: _try_arxiv(stub, inbox_dir=inbox_dir, deadline=dl),
        )
    )
    providers.append(
        (
            _SOURCE_S2,
            lambda dl: _try_s2(stub, inbox_dir=inbox_dir, deadline=dl),
        )
    )
    # Paid, publisher-agnostic full text — LAST, so we only spend after every
    # free leg has failed. The credential itself is the opt-in (configuring the
    # key on the fetch host is the budget decision); the low-balance alert warns
    # before the runway runs out.
    if openalex_content_key:
        providers.append(
            (
                _SOURCE_OPENALEX_CONTENT,
                lambda dl: _try_openalex_content(
                    stub,
                    inbox_dir=inbox_dir,
                    api_key=openalex_content_key,
                    store=store,
                    email=email,
                    deadline=dl,
                ),
            )
        )

    for source, runner in providers:
        if stop_at is not None and time.perf_counter() >= stop_at:
            break
        trace = _current_ref_timing()
        timing = _LegTiming(source) if trace is not None else None
        if trace is not None and timing is not None:
            trace.legs.append(timing)
        key = _PROVIDER_KEYS.get(source)
        leg_t0 = time.perf_counter()
        # The leg's retry tail ends at the earlier of its own wall and the
        # stub's — the in-flight request still gets its full timeout.
        leg_wall = _leg_wall_s()
        deadline = leg_t0 + leg_wall if leg_wall > 0 else None
        if stop_at is not None and (deadline is None or stop_at < deadline):
            deadline = stop_at
        # A leg the gate can't admit before its wall defers — the
        # provider is saturated or backing off, and this stub's leg is
        # retried on its next claim rather than recorded as a miss.
        gate_t0 = time.perf_counter()
        admitted = gate.acquire(key, deadline=deadline)
        if timing is not None:
            timing.gate_ms = (time.perf_counter() - gate_t0) * 1000
            if deadline is not None:
                timing.deadline_remaining_ms = (deadline - time.perf_counter()) * 1000
        if not admitted:
            if timing is not None:
                timing.outcome = "provider_wait"
            _record_deferral(store, stub, source, key, leg_t0, timing=timing)
            continue
        # Held for the leg's whole tail; the finally's release keeps an
        # unexpected raise from shrinking the provider's cap this pass.
        try:
            try:
                request_t0 = time.perf_counter()
                if timing is None:
                    outcome = runner(deadline)
                else:
                    with observe_rate_limit_timing(_observe_limiter_wait(timing)):
                        outcome = runner(deadline)
                if timing is not None:
                    timing.request_ms = (time.perf_counter() - request_t0) * 1000
                    if deadline is not None:
                        timing.deadline_remaining_ms = (
                            deadline - time.perf_counter()
                        ) * 1000
            except Exception as exc:
                if timing is not None:
                    timing.request_ms = (time.perf_counter() - request_t0) * 1000
                    timing.outcome = "api_error"
                    if deadline is not None:
                        timing.deadline_remaining_ms = (
                            deadline - time.perf_counter()
                        ) * 1000
                leg_ms = _ms(leg_t0)
                gate.note_outcome(key, "api_error", leg_ms)
                _timed_event(
                    store,
                    stub.ref_id,
                    timing=timing,
                    source=source,
                    event="api_error",
                    payload={"error": str(exc)[:200]},
                    duration_ms=leg_ms,
                )
                continue
            if outcome is None:
                # No identifier for this source — silent skip; no event.
                if timing is not None:
                    timing.outcome = "no_identifier"
                continue
            if timing is not None:
                timing.outcome = outcome.event
            gate.note_outcome(key, outcome.event, outcome.duration_ms)
            _timed_event(
                store,
                stub.ref_id,
                timing=timing,
                source=source,
                event=outcome.event,
                payload=outcome.payload,
                duration_ms=outcome.duration_ms,
                cost_usd=outcome.cost_usd,
            )
            if outcome.event != "fetch_ok":
                continue
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
        finally:
            gate.release(key)
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


#: Unpaywall's tenacity decorator is built at import, so its delay bound
#: is read once here; a disabled metadata wall (≤0) must not zero it.
_UNPAYWALL_META_DELAY_S = _meta_wall_s() if _meta_wall_s() > 0 else float("inf")


@retry(
    wait=wait_exponential(min=1, max=30),
    stop=stop_any(
        stop_after_attempt(_RETRY_MAX_ATTEMPTS),
        stop_after_delay(_UNPAYWALL_META_DELAY_S),
    ),
    retry=retry_if_exception_type(
        (httpx.TransportError, httpx.ReadTimeout, httpx.WriteTimeout)
    ),
    reraise=True,
)
def _query_unpaywall(doi: str, *, email: str) -> dict[str, Any]:
    """Hit GET /v2/<doi>email=<address> and return the parsed JSON.

    Retries on transport / timeout errors; HTTPStatusError (400/404/
    429/5xx) bubbles up to the caller for per-status handling. The
    return shape is documented at https://unpaywall.org/data-format.
    Raises :class:`RateLimited` without calling out when the shared
    ``unpaywall`` limiter refuses (100k/day quota spent); not retried.
    The retry chain also stops at ``_meta_wall_s`` (read once at
    import, like every env knob consumed by a decorator) — 4 attempts
    of a 30 s timeout used to cost a stub ~2 min on a dead endpoint.
    """
    require_rate_limit("unpaywall")
    url = f"{_UNPAYWALL_BASE}/{doi}"
    with http_client(timeout=_API_TIMEOUT_S, user_agent=None) as client:
        resp = client.get(url, params={"email": email})
        resp.raise_for_status()
        return resp.json()


def _query_crossref_pdf_links(doi: str, *, email: str) -> list[str]:
    """Return Crossref ``link[]`` URLs whose content-type is a PDF.

    ``mailto`` routes us to Crossref's polite pool (the anonymous pool
    connection-resets under load). Non-PDF TDM links (xml/plain, e.g.
    Elsevier's api.elsevier.com mining endpoints) are filtered out —
    those are handled by the dedicated Elsevier leg, not here. Raises
    :class:`RateLimited` without calling out when the shared ``crossref``
    limiter refuses.
    """
    url = f"https://api.crossref.org/works/{doi}"

    def _fetch() -> Any:
        with http_client(
            timeout=_API_TIMEOUT_S, headers={"User-Agent": _user_agent_header(email)}
        ) as client:
            resp = client.get(url, params={"mailto": email})
            resp.raise_for_status()
            return resp.json()

    # Politeness core, service "crossref": a shared token per attempt,
    # 429/5xx and transport errors retried with Retry-After — but no
    # retry starts past the metadata deadline: a dead Crossref API
    # used to cost a stub 5×30 s before this leg even saw a URL.
    data = call_with_backoff(
        _fetch, service="crossref", deadline=_meta_deadline(), now=time.perf_counter
    )
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
    require_rate_limit("openalex")
    url = f"https://api.openalex.org/works/doi:{doi}"
    params = {"mailto": email} if email else {}
    with http_client(
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

    def _search() -> Any:
        with http_client(
            timeout=_API_TIMEOUT_S, headers={"User-Agent": _user_agent_header()}
        ) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    # Politeness core, service "europepmc": a shared token per attempt,
    # 429/5xx and transport errors retried with Retry-After; a 4xx raises.
    # The deadline caps the retry chain — the JATS leg's 150 s api_errors
    # on a dead endpoint were this call exhausting all 5 default attempts.
    data = call_with_backoff(
        _search, service="europepmc", deadline=_meta_deadline(), now=time.perf_counter
    )
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

    Takes a shared ``core`` limiter token before the API request; a refusal
    is returned as ``rate_limited`` by :func:`_try_core`, not mistaken for
    an API failure or counted as provider traffic.

    Only results whose own ``doi`` equals the query DOI are kept (CORE
    fuzzy-matches, so a bare topical hit must not be mistaken for the
    paper).

    Looks at the repository ``downloadUrl`` (a PDF bitstream) first, then the
    ``fullText`` link — but a ``fullText`` URL that clearly renders as
    ``.txt``/``.html``/… is dropped (:func:`_looks_non_pdf_url`), since the
    downloader only accepts ``%PDF-`` bytes and would waste the round-trip.
    Every candidate is validated as a real http(s) URL — the earlier bug passed
    a bare CORE work id (``"587670336"``), which the SSRF guard rejected. The
    name reflects the output: full-text links, not guaranteed-PDF ones.
    """
    require_rate_limit("core")
    params: dict[str, str | int] = {"q": f'doi:"{doi}"', "limit": 5}
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": _user_agent_header()}
    with http_client(timeout=_API_TIMEOUT_S, headers=headers) as client:
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
    require_rate_limit("openalex")
    url = f"https://api.openalex.org/works/doi:{doi}"
    params = {"mailto": email} if email else {}
    with http_client(
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


def _is_transient_download_error(exc: BaseException) -> bool:
    """True for a transport-class failure worth an immediate in-pass retry.

    Covers handshake/connect/read timeouts and connection resets — httpx
    surfaces an SSL handshake timeout as ``ConnectTimeout`` / ``ConnectError``,
    both subclasses of ``httpx.TransportError`` — plus ``5xx`` / ``429``
    responses (from ``raise_for_status``). A genuine ``4xx`` (403/404 = the
    copy isn't there for us) or a non-PDF body (``ValueError`` from the
    ``%PDF-`` guard, or the disk-fill cap) is *deterministic*: not retried,
    so the cascade falls through to the next leg without burning the retry
    budget. ``SsrfBlocked`` (a refused host/redirect) is likewise terminal.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or 500 <= code < 600
    return isinstance(exc, httpx.TransportError)


#: Backoff shape for one in-pass PDF download (three tries, waits capped at
#: 8 s). The limiter row and the breaker come from the call's ``url=``:
#: ``host:<netloc>`` per publisher host, so each host is paced and a 429's
#: Retry-After parks only that host's row.
_DOWNLOAD_POLICY = Policy(attempts=_DOWNLOAD_RETRY_ATTEMPTS, base=1.0, cap=8.0)


def _download_pdf(
    url: str,
    target: Path,
    *,
    extra_headers: dict[str, str] | None = None,
    service: str | None = None,
    deadline: float | None = None,
) -> int:
    """Stream a PDF to ``target`` and return the byte count.

    Runs under the shared politeness core
    (:func:`precis.utils.http.call_with_backoff`): one ``host:<netloc>``
    limiter token per attempt, full-jitter backoff, and a 429/503
    ``Retry-After`` parks that host's limiter row. The limiter fails open
    (logged) when its store is unreachable.

    Retries in-pass on a transport-class failure (see
    :func:`_is_transient_download_error`) so a single handshake/connect
    timeout on a deterministic identifier route (arXiv, publisher) doesn't
    end the leg as a spurious ``fetch_failed`` — the reported incident was a
    free arXiv PDF declared ``no_oa_version`` after one SSL-handshake blip.
    A deterministic error (4xx, non-PDF body, SSRF refusal) is not retried
    and falls through to the next leg immediately.

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

    ``service`` names a registered politeness policy (``arxiv``,
    ``openalex-content``): every attempt then takes a token from that
    service's limiter row and follows its backoff/breaker, instead of the
    per-host default. :class:`RateLimited` propagates for the caller to map
    to a ``rate_limited`` outcome.

    ``deadline`` is a ``perf_counter`` instant (the leg's wall budget):
    no retry starts past it. The attempt already running is never cut —
    it keeps its full ``_DOWNLOAD_TIMEOUT_S``, so a genuinely slow but
    working host can still land on its one try.
    """
    if service is not None:
        return call_with_backoff(
            lambda: _download_pdf_once(url, target, extra_headers),
            service=service,
            url=url,
            retry_if=_is_transient_download_error,
            deadline=deadline,
            now=time.perf_counter,
        )
    netloc = urlparse(url).netloc.lower()
    return call_with_backoff(
        lambda: _download_pdf_once(url, target, extra_headers),
        service=f"pdf:{netloc}",
        policy=_DOWNLOAD_POLICY,
        url=url,
        retry_if=_is_transient_download_error,
        deadline=deadline,
        now=time.perf_counter,
    )


def _download_pdf_once(
    url: str, target: Path, extra_headers: dict[str, str] | None
) -> int:
    """One download attempt for :func:`_download_pdf`."""
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


def _user_agent_header(email: str | None = None) -> str:
    """Honest UA for every fetch_oa request: the shared
    :func:`precis.utils.http.polite_user_agent` (product, project URL, and
    the polite-pool contact — ``email`` or the ``contact.polite_email``
    setting, else the maintainer fallback). Publishers (PeerJ, arXiv)
    sometimes 403 the default httpx UA; this lets a sysadmin ping us."""
    return polite_user_agent(email)


def _download_headers() -> dict[str, str]:
    """Default download headers, built fresh per call (not frozen at import)
    so a DB-set ``contact.polite_email`` takes effect on the next download
    without a process restart."""
    return {
        "User-Agent": _user_agent_header(),
        # Some hosts insist on an Accept header that names PDFs explicitly.
        "Accept": "application/pdf,*/*;q=0.8",
    }


def _local_rate_limited(stub: StubRef, t0: float, *, provider: str) -> FetchOutcome:
    """``rate_limited`` outcome for a request the shared limiter refused
    before it was sent (daily quota spent or rate-starved). Same event as
    ``_try_unpaywall``'s 429; ``limiter`` tells the two apart."""
    return FetchOutcome(
        event="rate_limited",
        payload={"doi": stub.doi, "provider": provider, "limiter": "shared"},
        duration_ms=_ms(t0),
    )


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

    from precis.ingest.semantic_scholar import _client
    from precis.utils.rate_limit import acquire as acquire_rate_limit

    sch = _client()
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
