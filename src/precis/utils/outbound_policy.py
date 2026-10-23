"""One outbound policy, resolved once per call — docs/backlog/outbound-policy.md.

Every outbound call resolves a single :class:`EffectivePolicy` describing
how the call is admitted: whether it takes a limiter token (``rate``),
whether it must hold a fleet-wide concurrency slot (``concurrency``), how
long admission may wait (``max_wait_s``), what happens when capacity is
refused (``on_refuse``), and the retry/timeouts the call should use.

Merge order per field, later non-NULL wins (decision D3):

1. ``CLASS_DEFAULTS[cls]`` — the class baseline (§2 of the spec).
2. ``POLICIES[service]`` — the code layer; the new ``Policy`` fields
   (``max_wait_s``, ``connect_s``, ``read_s``, ``total_s``, ``on_refuse``,
   ``concurrency``) are ``None`` = inherit; existing backoff fields
   (``attempts``/``base``/``cap``, and ``limiter_wait_s`` as the
   ``max_wait_s`` fallback) keep their values.
3. The ``external_rate_limits`` row's override columns — the only layer
   an operator can change without a deploy.
4. The ambient deadline (:func:`precis.utils.deadline.deadline_at`),
   which only shortens
   ``max_wait_s``.

Class pick: the row's ``class`` column, else ``Policy.cls``, else
``internal`` for :func:`~precis.utils.http.is_internal_host`,
``provider-api`` for a named provider row, ``scraped-host`` otherwise.

The DB layer is a per-process snapshot of the rows that can matter — one
``SELECT`` of the rows with any override column set or an active park,
refreshed at most every :data:`_SNAPSHOT_TTL_S` seconds. A row absent
from the snapshot has no overrides.

**Fail-open.** A resolver fault (no store, missing migration columns, any
DB error) returns the code layers alone — today's behaviour — and warns
once per provider per hour via :func:`rate_limit._warn_unpaced`. The
resolver must never wedge an outbound call.

The deadline merge reads the one ambient source,
:func:`precis.utils.deadline.deadline_at` (S2a's ContextVar, published by
``call_with_deadline``/``deadline_scope``); :func:`deadline_remaining` is
the one helper every wait path shortens through.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urlparse

from precis.utils.deadline import deadline_at

if TYPE_CHECKING:
    from precis.utils.http import Policy

log = logging.getLogger(__name__)


class RateLane(NamedTuple):
    """A token-bucket lane on the limiter row.

    ``EffectivePolicy.rate is None`` marks a rate-less class (``llm-api``,
    ``llm-local``, ``internal``) — the caller must not take a token. The
    numeric members describe the bucket when the resolver can see them:
    a class seed (``scraped-host``'s 1 req/s) or a snapshot row's columns.
    ``None`` members mean the parameters live only in the row — the
    consume path reads them atomically at grant time.
    """

    capacity: int | None
    refill_per_sec: float | None
    daily_cap: int | None


@dataclass(frozen=True)
class _ClassDefault:
    """Baseline for a policy class; every field the merge starts from."""

    rate: RateLane | None
    concurrency: str | None
    max_wait_s: float
    connect_s: float | None
    read_s: float | None
    total_s: float | None
    on_refuse: str


#: Class baselines (spec §2). ``concurrency`` for ``llm-api`` is
#: per-provider (``llm-api:<provider>``) and so is computed in
#: :func:`_resolve`, not stored here; ``llm-local`` slot division is owned
#: by ``llm-lane-scheduler.md``/``local_serving``, so it stays ``None``.
CLASS_DEFAULTS: dict[str, _ClassDefault] = {
    "scraped-host": _ClassDefault(
        rate=RateLane(1, 1.0, None),
        concurrency=None,
        max_wait_s=30.0,
        connect_s=None,
        read_s=None,
        total_s=None,
        on_refuse="wait",
    ),
    "provider-api": _ClassDefault(
        rate=RateLane(None, None, None),
        concurrency=None,
        max_wait_s=30.0,
        connect_s=None,
        read_s=None,
        total_s=None,
        on_refuse="wait",
    ),
    "llm-api": _ClassDefault(
        rate=None,
        concurrency=None,  # filled per-URL as llm-api:<provider> in _resolve
        max_wait_s=30.0,
        connect_s=None,
        read_s=None,
        total_s=None,
        on_refuse="defer",
    ),
    "llm-local": _ClassDefault(
        rate=None,
        concurrency=None,
        max_wait_s=30.0,
        connect_s=None,
        read_s=None,
        total_s=None,
        on_refuse="wait",
    ),
    "internal": _ClassDefault(
        # S1 deviation (spec §2, post-review): internal URLs keep the
        # legacy 1/s token lane until a caller opts in — a rate-less
        # default would silently drop pacing on today's internal calls.
        rate=RateLane(1, 1.0, None),
        concurrency=None,
        max_wait_s=30.0,
        connect_s=None,
        read_s=None,
        total_s=None,
        on_refuse="wait",
    ),
}


@dataclass(frozen=True)
class EffectivePolicy:
    """The resolved admission policy for one outbound call."""

    service: str
    cls: str
    #: ``external_rate_limits`` row this call's lane lives on, if any.
    row: str | None
    #: Token-bucket lane, or ``None`` for a rate-less class (no acquire).
    rate: RateLane | None
    #: ``resource_slots`` resource to hold for the call's duration, if any.
    concurrency: str | None
    #: Longest admission wait (token or slot), after the deadline merge.
    max_wait_s: float
    #: Per-phase/overall timeout overrides; ``None`` = caller's own timeout.
    connect_s: float | None
    read_s: float | None
    total_s: float | None
    #: ``wait`` (block bounded, then proceed unpaced) | ``defer``
    #: (:class:`~precis.utils.rate_limit.RateLimited`, caller requeues) |
    #: ``fail`` (refuse immediately).
    on_refuse: str
    #: ``(attempts, base, cap)`` from the ``Policy`` backoff fields.
    retry: tuple[int, float, float]
    #: Active Retry-After park end on the row, if the snapshot saw one.
    parked_until: datetime | None = None
    #: Which merge layer supplied each field — ``class`` (the
    #: :data:`CLASS_DEFAULTS` baseline, including the ``llm-api``
    #: per-provider resource name derived from it), ``code``
    #: (``POLICIES``), ``db`` (the row's override columns), ``inferred``
    #: (``cls`` picked from the URL/row shape) or ``deadline`` (the
    #: ambient deadline shortened ``max_wait_s``). For the ``/limits``
    #: view and ``precis limits set --dry-run`` (S4); empty for callers
    #: that never asked for provenance.
    layers: dict[str, str] = field(default_factory=dict)


def deadline_remaining() -> float | None:
    """Seconds left under the ambient deadline — the ONE deadline source:
    :func:`precis.utils.deadline.deadline_at`, published by
    ``call_with_deadline``/``deadline_scope`` (slice S2a). ``None`` when
    unbounded. Every admission wait only shortens under it."""
    deadline = deadline_at()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


#: Per-process snapshot refresh cadence (D3): one query per process per
#: 30 s, so an operator edit to an override column lands within a minute.
_SNAPSHOT_TTL_S = 30.0

#: Override/state columns the DB layer can set. Any row with one non-NULL
#: override — or an active park — is pulled into the snapshot.
_SNAPSHOT_SQL = """
SELECT provider, "class", max_wait_s, connect_s, read_s, total_s,
       on_refuse, concurrency, parked_until,
       capacity, refill_per_sec, daily_cap
  FROM external_rate_limits
 WHERE "class" IS NOT NULL
    OR max_wait_s IS NOT NULL
    OR connect_s IS NOT NULL
    OR read_s IS NOT NULL
    OR total_s IS NOT NULL
    OR on_refuse IS NOT NULL
    OR concurrency IS NOT NULL
    OR parked_until > now()
"""

_snapshot_lock = threading.Lock()
#: DB ``class`` values outside :data:`CLASS_DEFAULTS` — warned once each.
_warned_unknown_classes: set[str] = set()
_snapshot: dict[str, dict[str, Any]] = {}
_snapshot_at = 0.0


def _refresh_snapshot() -> dict[str, dict[str, Any]] | None:
    """The cached override rows; ``None`` on a resolver fault.

    Refreshes at most once per :data:`_SNAPSHOT_TTL_S`. A failed refresh
    keeps the TTL discipline (no hammering a sick DB) and reports the
    fault so :func:`_resolve` serves the code layers alone.
    """
    global _snapshot, _snapshot_at
    now = time.monotonic()
    if now - _snapshot_at < _SNAPSHOT_TTL_S:
        return _snapshot
    if not _snapshot_lock.acquire(blocking=False):
        return _snapshot  # a refresh is in flight — serve the stale copy
    try:
        now = time.monotonic()
        if now - _snapshot_at < _SNAPSHOT_TTL_S:
            return _snapshot
        try:
            from precis.budget import meter

            store = meter.active_store()
            if store is None:
                raise RuntimeError("no bound store")
            with store.pool.connection() as conn:
                rows = conn.execute(_SNAPSHOT_SQL).fetchall()
            _snapshot = {
                str(r[0]): {
                    "class": r[1],
                    "max_wait_s": float(r[2]) if r[2] is not None else None,
                    "connect_s": float(r[3]) if r[3] is not None else None,
                    "read_s": float(r[4]) if r[4] is not None else None,
                    "total_s": float(r[5]) if r[5] is not None else None,
                    "on_refuse": r[6],
                    "concurrency": r[7],
                    "parked_until": r[8],
                    "capacity": r[9],
                    "refill_per_sec": r[10],
                    "daily_cap": r[11],
                }
                for r in rows
            }
        except Exception:
            _snapshot = {}
            _snapshot_at = now
            log.debug("outbound_policy: snapshot refresh failed", exc_info=True)
            return None
        _snapshot_at = now
        return _snapshot
    finally:
        _snapshot_lock.release()


def reset_cache() -> None:
    """Drop the snapshot cache — tests, and after a direct override write."""
    global _snapshot, _snapshot_at
    with _snapshot_lock:
        _snapshot = {}
        _snapshot_at = 0.0


#: Public suffixes made of two labels, so ``parts[-2]`` is not the
#: registrable name (``api.example.co.uk`` → ``example``).
_MULTI_LABEL_TLDS = frozenset(
    {"co.uk", "org.uk", "com.au", "com.br", "com.cn", "co.jp", "co.kr", "co.nz"}
)


def _provider_name(url: str | None, provider: str | None = None) -> str | None:
    """The ``llm-api:<provider>`` suffix: the policy's named provider when
    set, else the netloc's registrable label (``openrouter.ai`` →
    ``openrouter``, ``api.openai.com`` → ``openai``)."""
    if provider:
        return provider
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return None
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_LABEL_TLDS:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else host


def _resolve(
    service: str,
    policy: Policy,
    row: str | None,
    url: str | None,
) -> EffectivePolicy:
    snap = _refresh_snapshot()
    if snap is None:
        from precis.utils import rate_limit

        rate_limit._warn_unpaced(
            row or service, "policy snapshot unavailable — code defaults only"
        )
        snap = {}
    overrides = snap.get(row) if row is not None else None
    return _merge(service, policy, row, url, overrides or {}, apply_deadline=True)


def _merge(
    service: str,
    policy: Policy,
    row: str | None,
    url: str | None,
    overrides: dict[str, Any],
    *,
    apply_deadline: bool,
) -> EffectivePolicy:
    """The field-by-field merge (D3), recording each winner in
    ``EffectivePolicy.layers``. ``apply_deadline=False`` (the ``/limits``
    view, ``precis limits set --dry-run``) skips the ambient-deadline
    merge — the result then describes the policy, not one call's wait
    budget."""
    layers: dict[str, str] = {}

    db_cls = overrides.get("class")
    if db_cls is not None and db_cls not in CLASS_DEFAULTS:
        if db_cls not in _warned_unknown_classes:
            _warned_unknown_classes.add(db_cls)
            log.warning(
                "outbound_policy: %s has unknown class %r — the DB override "
                "is ignored; valid: %s",
                row,
                db_cls,
                ", ".join(sorted(CLASS_DEFAULTS)),
            )
        db_cls = None
    cls = db_cls or policy.cls
    if cls is not None:
        layers["cls"] = "db" if db_cls is not None else "code"
    else:
        layers["cls"] = "inferred"
        if url and _is_internal(url):
            cls = "internal"
        elif row is not None and not row.startswith("host:"):
            cls = "provider-api"
        else:
            cls = "scraped-host"
    base = CLASS_DEFAULTS.get(cls, CLASS_DEFAULTS["scraped-host"])

    rate = base.rate
    layers["rate"] = "class"
    if cls == "provider-api" and overrides.get("capacity") is not None:
        rate = RateLane(
            int(overrides["capacity"]),
            float(overrides["refill_per_sec"]),
            overrides["daily_cap"],
        )
        layers["rate"] = "db"

    concurrency = base.concurrency
    layers["concurrency"] = "class"
    if cls == "llm-api":
        name = _provider_name(url, policy.provider)
        concurrency = f"llm-api:{name}" if name else concurrency
    if policy.concurrency is not None:
        concurrency = policy.concurrency
        layers["concurrency"] = "code"
    if overrides.get("concurrency") is not None:
        concurrency = overrides["concurrency"]
        layers["concurrency"] = "db"

    max_wait_s = base.max_wait_s
    layers["max_wait_s"] = "class"
    if policy.max_wait_s is not None:
        max_wait_s = policy.max_wait_s
        layers["max_wait_s"] = "code"
    elif policy.limiter_wait_s is not None:
        max_wait_s = policy.limiter_wait_s
        layers["max_wait_s"] = "code"
    if overrides.get("max_wait_s") is not None:
        max_wait_s = overrides["max_wait_s"]
        layers["max_wait_s"] = "db"
    if apply_deadline:
        remaining = deadline_remaining()
        if remaining is not None:
            max_wait_s = min(max_wait_s, remaining)
            layers["max_wait_s"] = "deadline"

    def _timeout(field: str) -> float | None:
        value = getattr(policy, field)
        layers[field] = "code" if value is not None else "class"
        if overrides.get(field) is not None:
            value = overrides[field]
            layers[field] = "db"
        return value

    on_refuse = base.on_refuse
    layers["on_refuse"] = "class"
    if policy.on_refuse is not None:
        on_refuse = policy.on_refuse
        layers["on_refuse"] = "code"
    if overrides.get("on_refuse") is not None:
        on_refuse = overrides["on_refuse"]
        layers["on_refuse"] = "db"

    layers["retry"] = "code"

    return EffectivePolicy(
        service=service,
        cls=cls,
        row=row,
        rate=rate,
        concurrency=concurrency,
        max_wait_s=max_wait_s,
        connect_s=_timeout("connect_s"),
        read_s=_timeout("read_s"),
        total_s=_timeout("total_s"),
        on_refuse=on_refuse,
        retry=(policy.attempts, policy.base, policy.cap),
        parked_until=overrides.get("parked_until"),
        layers=layers,
    )


def _is_internal(url: str) -> bool:
    try:
        from precis.utils.http import is_internal_host

        return is_internal_host(url)
    except Exception:  # pragma: no cover — classification must not wedge calls
        return False


def resolve(service: str, *, url: str | None = None) -> EffectivePolicy:
    """Resolve the :class:`EffectivePolicy` for ``service`` (a
    :data:`~precis.utils.http.POLICIES` key) and optional target ``url``.

    The limiter row key is ``Policy.provider`` when the policy names one,
    else ``host:<netloc>`` of ``url``. Never raises for a DB fault — the
    code layers alone are returned instead.
    """
    from precis.utils.http import policy_for

    policy = policy_for(service)
    row = policy.provider
    if row is None and url:
        from precis.utils.http import netloc_provider

        row = netloc_provider(url)
    return _resolve(service, policy, row, url)


def resolve_for(
    service: str,
    policy: Policy,
    row: str | None,
    url: str | None,
) -> EffectivePolicy:
    """Resolve around an explicit ``policy``/``row`` pair — for callers
    inside :mod:`precis.utils.http` that already ran
    ``_resolve_provider`` (explicit ``provider=`` overrides, or a policy
    passed beside a host-named ``service`` label)."""
    return _resolve(service, policy, row, url)


def explain(
    service: str,
    policy: Policy,
    row: str | None,
    url: str | None,
    overrides: dict[str, Any],
) -> EffectivePolicy:
    """Resolve against an explicit ``overrides`` map — a row's own
    columns, or a pending ``precis limits set`` write — instead of the
    process snapshot. The ambient deadline does NOT merge
    (``apply_deadline=False``): the result describes the policy, not one
    call's wait budget. Same override keys as the snapshot row
    (``class``, ``max_wait_s``, ``connect_s``, ``read_s``, ``total_s``,
    ``on_refuse``, ``concurrency``, ``parked_until``, and the lane's
    ``capacity``/``refill_per_sec``/``daily_cap``). ``numeric`` columns
    arrive as :class:`decimal.Decimal`; they are coerced to ``float`` so
    the merged fields stay typed."""
    coerced = {
        k: (float(v) if isinstance(v, Decimal) else v) for k, v in overrides.items()
    }
    return _merge(service, policy, row, url, coerced, apply_deadline=False)


def row_resolutions(
    row: str, *, llm_api_carriers: set[str] | frozenset[str] = frozenset()
) -> list[tuple[str, Policy, str | None]]:
    """The ``(label, policy, url)`` interpretations that land on one
    ``external_rate_limits`` row.

    A named row resolves under every :data:`~precis.utils.http.POLICIES`
    service that names it; identical policies collapse into one entry
    with the service names joined in the label (``openalex`` and
    ``openalex-content`` share a policy). A ``host:<netloc>`` row
    resolves under the provider-less default — plus ``openai-tools``
    when ``llm_api_carriers`` names its ``llm-api:<provider>`` resource
    (the row is then an llm-api carrier: ``host:openrouter.ai`` for
    ``llm-api:openrouter``). A named row no POLICIES entry claims
    resolves under the default policy.
    """
    from precis.utils.http import DEFAULT_POLICY, POLICIES

    if row.startswith("host:"):
        url = f"https://{row[5:]}/"
        out: list[tuple[str, Policy, str | None]] = [
            ("provider-less", DEFAULT_POLICY, url)
        ]
        name = _provider_name(url)
        if name and f"llm-api:{name}" in llm_api_carriers:
            out.append(("openai-tools", POLICIES["openai-tools"], url))
        return out
    by_policy: dict[Policy, list[str]] = {}
    for service, pol in POLICIES.items():
        if pol.provider == row:
            by_policy.setdefault(pol, []).append(service)
    if by_policy:
        return [
            (",".join(sorted(services)), pol, None)
            for pol, services in by_policy.items()
        ]
    return [(row, DEFAULT_POLICY, None)]


def format_effective(eff: EffectivePolicy) -> str:
    """One compact ``field=value (layer)`` render of a resolved policy —
    the shared display for the ``/limits`` view and
    ``precis limits set --dry-run``. Every field names the layer it came
    from (spec S4 acceptance)."""

    def _lay(field: str) -> str:
        return eff.layers.get(field, "?")

    def _num(value: float | None) -> str:
        return "-" if value is None else f"{float(value):g}"

    if eff.rate is None:
        rate = "-"
    else:
        daily = "-" if eff.rate.daily_cap is None else str(int(eff.rate.daily_cap))
        rate = (
            f"{_num(eff.rate.capacity)}/{_num(eff.rate.refill_per_sec)}/s daily={daily}"
        )
    attempts, base, cap = eff.retry
    return (
        f"cls={eff.cls} ({_lay('cls')})  "
        f"rate={rate} ({_lay('rate')})  "
        f"concurrency={eff.concurrency or '-'} ({_lay('concurrency')})  "
        f"max_wait_s={eff.max_wait_s:g} ({_lay('max_wait_s')})  "
        f"connect_s={_num(eff.connect_s)} ({_lay('connect_s')})  "
        f"read_s={_num(eff.read_s)} ({_lay('read_s')})  "
        f"total_s={_num(eff.total_s)} ({_lay('total_s')})  "
        f"on_refuse={eff.on_refuse} ({_lay('on_refuse')})  "
        f"retry={attempts}x {base:g}..{cap:g}s ({_lay('retry')})"
    )


_FROM_EFFECTIVE = object()


def parked_gate(
    effective: EffectivePolicy,
    *,
    parked_until: Any = _FROM_EFFECTIVE,
    sleep: Any = None,
) -> None:
    """The rate-less pre-call park gate (D1): if ``parked_until`` is
    active, wait ``min(park left, max_wait_s, deadline)`` or refuse per
    ``on_refuse``.

    Runs BEFORE any concurrency hold so a long park never sleeps on a
    held slot. ``parked_until`` should be the fresh value returned by
    :func:`touch_rateless_row`; the snapshot copy on ``effective`` is the
    fallback. Raises :class:`~precis.utils.rate_limit.RateLimited` on a
    refusal.
    """
    from precis.utils import rate_limit

    if parked_until is _FROM_EFFECTIVE:
        parked_until = effective.parked_until
    if effective.row is None or parked_until is None:
        return
    if parked_until.tzinfo is None:
        parked_until = parked_until.replace(tzinfo=UTC)
    left = (parked_until - datetime.now(UTC)).total_seconds()
    if left <= 0:
        return
    budget = effective.max_wait_s
    remaining = deadline_remaining()
    if remaining is not None:
        budget = min(budget, remaining)
    if effective.on_refuse == "fail" or left > budget:
        reason = (
            "parked (on_refuse=fail)"
            if effective.on_refuse == "fail"
            else f"Retry-After park has {left:.0f}s left, past the {budget:g}s wait budget"
        )
        rate_limit._record_refusal(effective.row, reason)
        raise rate_limit.RateLimited(effective.row)
    from precis.liveness import drain_sleep

    if (sleep or drain_sleep)(left):
        raise rate_limit.RateLimited(effective.row)  # draining mid-park


#: Stamp ``last_used_at`` — the rate-less row never moves ``last_refill``,
#: and this is what keeps a busy rate-less row visible to the idle
#: eviction re-key landing in S4.
_TOUCH_SQL = (
    "UPDATE external_rate_limits SET last_used_at = now() "
    "WHERE provider = %s RETURNING parked_until"
)


def touch_rateless_row(
    effective: EffectivePolicy, conn: Any, seed_row: tuple[int, float] = (1, 1.0)
) -> datetime | None:
    """Seed the carrier row (still at the default ``(1, 1.0)`` tokens —
    unused, the row only carries ``parked_until``/overrides per D1) and
    stamp ``last_used_at``. Call inside the caller's transaction. Returns
    the row's FRESH ``parked_until`` — the value the park gate must read
    (the snapshot copy can be up to 30 s stale, and a stale park would let
    calls through a live 429, defeating D1)."""
    from precis.utils import rate_limit

    if effective.row is None:
        return None
    if not rate_limit.s1_columns_present(conn):
        # Un-migrated DB — the caller takes the pre-S1 path (no touch, no
        # fresh park, and no resource_slots row to hold either).
        return None
    rate_limit._seed_host_row(conn, effective.row, seed_row)
    row = conn.execute(_TOUCH_SQL, (effective.row,)).fetchone()
    return row[0] if row else None


def record_park(row: str | None, seconds: float) -> None:
    """Write a just-issued Retry-After park through to the local snapshot,
    so this process's very next :func:`resolve` honours it without
    waiting out the 30 s TTL. Other processes learn it through the
    ``touch_rateless_row`` RETURNING read."""
    if not row or seconds <= 0:
        return
    if not _snapshot_lock.acquire(blocking=False):
        return  # a refresh is in flight — best-effort, drop it
    try:
        _snapshot.setdefault(row, {})["parked_until"] = datetime.now(UTC) + timedelta(
            seconds=float(seconds)
        )
    finally:
        _snapshot_lock.release()


__all__ = [
    "CLASS_DEFAULTS",
    "EffectivePolicy",
    "RateLane",
    "deadline_remaining",
    "explain",
    "format_effective",
    "parked_gate",
    "record_park",
    "reset_cache",
    "resolve",
    "resolve_for",
    "row_resolutions",
    "touch_rateless_row",
]
