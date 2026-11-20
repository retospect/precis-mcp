"""``gripe_dispatch`` — turn fixable gripes into ``gripe-fix-direct`` chains.

The dark factory's intake (`me692430`, td564019 cut
3; Reto 2026-11-04: "the codex chain should run indep of claude"): until
now a gripe reached a build lane only when someone typed
``put(kind='todo', mode='plan', args={'template': 'gripe-fix-direct',
'gripe_id': N})``. This scheduled pass mints that chain itself, bounded
by what the build lanes can take, so an agent-filed code gripe becomes a
codex build with no human and no Claude step in front.

Dark unless ``service_config`` lights ``gripe_dispatch`` for the calling
host (:func:`gripe_dispatch_enabled`, the ``gripe_triage`` gate shape:
an exact-host row beats ``*``; prio 0 or no row reads dark). Lighting it
is an enablement and rides Reto's go, not a code change.

Each fire:

* **Supply.** Unlanded chains = live plan roots ``related-to`` a gripe
  whose STATUS is not terminal **and whose build has not landed**: an
  ``op`` node of a :data:`_BUILD_OPS` type under the root, non-terminal,
  whose ``task_class`` resolves to ``code``. A chain parked on the
  sha-in-prod gate, on validate or on the non-blocking review holds no
  slot (gr584410). Only running jobs and explicit claims count as live
  in the census. Every doable node is queued, including builds waiting
  on file overlap; the executor skips held builds and takes the next
  job by score. Capacity is the free executor supply on live code-capable
  hosts, using the claim loop's caps and reservations. Mint up to
  ``factory.gripe_dispatch.queue_depth`` (default 2) queued builds per
  free slot, less already queued builds (gr702610). This bounds how much
  gripe text is frozen ahead of execution. Parked work costs no seat and
  is never thrown away by a timer (Reto 2026-11-16: "put a todo on the
  thing, park the branch, use the slot otherwise"), so the count of
  parked chains is no longer a brake: :data:`MAX_PARKED_KEY` (default
  :data:`DEFAULT_MAX_PARKED`, 500) is a last-resort backstop only — at
  it the pass mints nothing (release-waiver chains excepted) and logs
  why. What stops the factory is a *cause*: the breaker below.
* **Breaker** (:mod:`precis.plans.lane_breaker`, before minting). When the
  newest ``K`` (setting ``factory.gripe_dispatch.breaker_parks``, default
  5) ended builds on one lane — or one host — all failed of the same
  engine cause (the lane-auth allowlist, an HTTP 401/403, the router's
  quota/rate/budget/transport classes, ``failure_class=infra``, a host
  refusal) across at least two build nodes, ONE ask-user todo names the
  lane or host, the cause and the job ids, and new builds stop going to
  it: ``lanes.lane_backoffs`` holds it out of every lane pick and its
  host out of the claim. Every other lane and host keeps
  dispatching. Closing the ask resets the breaker.
* **Stall sweep** (:func:`sweep_stalled`, before minting). A parked chain
  whose whole plan has stalled — no node under the root is live (a build
  blocked-by a running review, or held behind another build's files, is
  waiting, not stalled), nothing has moved on any plan node
  (``updated_at``, tag stamps, jobs) for setting :data:`STALL_HOURS_KEY`
  hours (default 6) and no ask is open in the plan — is NOT closed. It
  gets ONE ask-user todo, ``td<root>: chain stalled <age> — resume, start
  over, or drop?``, blocking its build nodes, naming the state found and
  the parked branch/host (``meta.parked``, ``plan_dispatch``'s park
  stamp) if any. The open ask keeps the chain out of the sweep and out of
  dispatch; closing it re-arms the build (``plan_dispatch._step_rearm``:
  resume from the saved branch, or ``start over``); dropping it is
  closing the root ``won't-do``. The gripe's open tag ``chain-stalls:<n>``
  counts the asks. At most :data:`_STALL_BATCH` asks a pass.
* **Candidates** (:func:`candidates`): live gripes, STATUS ``open`` or
  ``triaged``, tagged any of :data:`FIX_CLASS_TAGS` (``fix:code``,
  ``fix:skill``, ``fix:docs``, ``fix:web``, ``fix:mcp``; Reto
  2026-11-10), waiting :data:`_MIN_AGE_MINUTES`
  since the latest ``STATUS:triaged`` or fix-class attachment (filing
  time only if neither timestamp is known), with no open ``blocked-by``
  target of any kind — a todo or gripe blocker that has not reached a
  blocker-terminal STATUS holds the gripe out of dispatch, and it
  returns once the blocker closes (gr656970) —, with no live todo
  ``related-to`` them (a chain, hand-minted or ours, already exists — a
  linked todo carrying a terminal STATUS is closed work and no longer
  holds the gripe out, gr609044), none
  of the hold tags (``fix:reto``, ``reto:ask``, ``disagreement``,
  ``flood``, ``hold:no-dispatch``, ``hold:no-autoclose``,
  ``rescue:intake``), and the **filer-trust rule** below.
* **Filer trust** (setting :data:`TRUST_KEY` = ``factory.gripe_trust``).
  ``all`` (the default) skips the filer check: every filer is trusted
  (Reto 2026-11-10: "This is a legacy issue, they are all trusted for
  now, let them go. We turn on more serious trust in a bit."); the hold
  tags, the live-linked-todo check, the age floor and the lane capacity
  still apply. ``filer`` restores BUILDER's condition (2026-11-04): only
  a gripe **filed by a Claude Code client or a human account, or
  machine-filed with a ``ci.sig``** is a candidate, because the gripe
  text rides the build body verbatim and lands on main with no triage in
  front, so a reader-intake or worker-filed gripe never reaches a lane by
  this door. ``meta.filed_by`` carries the stamp
  (:mod:`precis.filing_origin`); under ``filer`` a gripe without one is
  skipped. An unrecognised value reads as ``filer`` (fail closed). The
  ``ci.sig`` exception is the ci_watch door: a gripe carrying
  ``meta.ci.sig`` and the worker stamp ``filed_by.principal =
  worker:ci_watch`` is machine-filed from the repo's own CI output about
  its own code — not reader intake — so the untrusted-text exclusion
  does not apply. The principal key is the door, not the client: a
  stdio ``precis serve`` client (an agent host's spawned server, a
  container's local serve) gets no bound principal, so every write of
  its run falls through to ``worker:<PRECIS_CURRENT_SOURCE|cli>`` —
  only the ``job_inproc`` tick's own bound source
  (``executors/job_inproc.py`` sets ``tick_context(source = spec.name)``)
  lands :data:`precis.workers.job_types.ci_watch.WORKER_PRINCIPAL`, the
  one constant both sides name. Order: ``dispatch_score`` (higher is
  hotter, :mod:`precis.utils.dispatch_score`, gr604593) then oldest.
  Every ``project:`` tag must name a project in :data:`_LANDABLE_PROJECTS`;
  no project tag means ``precis``. Unsupported projects receive one skip
  comment and consume no build slots: the factory cannot land their code.
* **Release-waiver gripes** (tag :data:`WAIVER_TAG_PREFIX`
  ``release-waiver:r<N>`` plus ``meta.release_waiver``, filed by
  :mod:`precis.plans.red_waiver` when the release gate deployed past a
  known-red test — Reto 2026-11-10: "treated as a broken build") are
  picked ahead of the rest: no :data:`_MIN_AGE_MINUTES` settle delay, no
  filer-trust check, minted before — and not counted against — the lane
  slots, and the chain root carries ``meta.claim_band = release`` so its
  build and land jobs claim at the release legs' class
  (``executors/_common._CLASS_RANK_CASE``), ahead of ordinary builds.
  The hold tags and a live linked todo still apply.
* **Coordinator route** (gr645163, Reto 2026-11-12 "escalate to next
  model"): a picked gripe whose report asks to write a deny path or is
  prod-graph only mints no chain — work no builder lane may write skips
  the builders (:mod:`precis.plans.route`, the same classifier
  ``plan_dispatch._step_dispatch`` runs on each todo's brief; a bare
  mention of a deny path does not route, gr685970). One
  ``ask-coordinator`` ask carries the report for the coordinator — the
  ruling's "with the diff/brief attached" — and the gripe holds
  ``hold:no-dispatch``; removing the tag returns it to dispatch. A
  gripe tagged :data:`ROUTE_BUILDER_TAG` (``route:builder``) skips the
  deny-path leg of the route — the coordinator's read-the-report
  override for an incidental mention; a graph-only report still routes,
  and the mint/write gates downstream still refuse a build that
  actually writes a deny path (gr645524).
* **Mint** through :func:`precis.plans.templates.mint_gripe_fix_direct`
  (unpinned: the build picks its cheapest fitting free lane at claim)
  and one ``dispatch:`` comment on the gripe naming the root. The
  ``related-to`` link the template writes is the idempotency marker: a
  re-fire sees the gripe linked and skips it, so a doubled tick
  converges. The pick's ``blocked-by`` read re-runs at each mint — a
  blocker linked between the candidate query and the mint holds the
  chain back (gr656970) — and again on the queued build's claim, which
  follows the ``related-to`` edge back to the gripe
  (:func:`precis.workers.executors._common.build_blocked_holds`).

The pass bills no model call and holds no transaction across the mint
(each mint is the template's own transaction).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from precis import settings as psettings
from precis.corpus_layout import host_name
from precis.plans import governs as _governs
from precis.plans import lane_breaker as _lane_breaker
from precis.plans import lanes as _lanes
from precis.plans import route as _route
from precis.plans.nodes import finish_mint
from precis.plans.templates import gripe_report_texts, mint_gripe_fix_direct
from precis.principal import worker_comment_meta
from precis.store import Store
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES, blocker_terminal_sql
from precis.todo.blocked import open_blocked_by
from precis.todo.guards import check_ask_meta
from precis.utils import dispatch_score as _dscore
from precis.workers.job_types.ci_watch import WORKER_PRINCIPAL as _CI_WATCH_PRINCIPAL
from precis.workers.runner import BatchResult
from precis.workers.service_config import ALL_HOSTS, ServiceConfigResolver

log = logging.getLogger(__name__)

SERVICE_NAME = "gripe_dispatch"
#: Wait after triage, not filing: an old report may only just have been
#: classified as code work, and the filer needs time to retract that decision.
_MIN_AGE_MINUTES = 15
#: ``project:`` values the factory can land; untagged gripes belong to precis.
#: ``precis-mcp`` is this repo's own name and ``dark-factory`` its factory
#: code. catpath/autocatpath is a separate checkout (gr611473).
_LANDABLE_PROJECTS: frozenset[str] = frozenset({"precis", "precis-mcp", "dark-factory"})
_PROJECT_SKIP_PREFIX = "dispatch: skipped unsupported project:"
#: Gripe tags that mark fixable work: code, skill, docs, web or MCP-surface
#: changes (Reto 2026-11-10). All of them land through the same
#: ``gripe-fix-direct`` chain and its repo-wide ``scripts/ship`` land.
FIX_CLASS_TAGS: tuple[str, ...] = (
    "fix:code",
    "fix:skill",
    "fix:docs",
    "fix:web",
    "fix:mcp",
)
#: Task class the chain's build carries (templates leave it unset → code).
#: Every :data:`FIX_CLASS_TAGS` class mints it: a ``docs`` class fits only
#: the kimi lane (and ``*`` lanes), while a ``code`` build fits every
#: code lane (devin, kimi, claude-sub), so mapping fix:docs/fix:skill to
#: ``docs`` would narrow capacity and starve them. Slot supply and
#: demand stay on the one class.
_BUILD_TASK_CLASS = "code"
#: ``meta.op`` types whose live node holds a build slot — the slice run
#: itself and its land (gr584410). The chain tail — the ``sha-in-prod``
#: gate, the ``validate``-classed validate, the ``generate`` reviewer —
#: carries none of these ops or a non-code ``task_class``, so a chain
#: that has landed stops holding a slot while it waits on them.
_BUILD_OPS: tuple[str, ...] = ("run_slice", "land_slice")
#: Tags that keep a gripe out of the lane by this door. ``hold:no-autoclose``
#: marks Reto's never-close gripes (me547530): a land would write
#: ``resolved:`` on one, so they never enter a chain by this door (BUILDER
#: 2026-11-05). ``rescue:intake`` marks a gripe that failed three mints —
#: rescue state, not dispatchable work (td574946 S1, ruling 10).
#: ``cluster:dark-factory`` is deliberately absent (gr591017): it is also
#: the cluster label humans, agents and ``gripe_triage`` put on every
#: engine design gripe, while the rescue-filed gripes it was meant to
#: hold never carry ``fix:code`` (``factory_rescue._file_sig_gripe``) —
#: holding it starved the factory's own engine fixes and guarded nothing.
HOLD_TAGS: tuple[str, ...] = (
    "fix:reto",
    "reto:ask",
    "disagreement",
    "flood",
    "hold:no-dispatch",
    "hold:no-autoclose",
    "rescue:intake",
)
#: STATUS values a gripe may carry to be dispatched.
_DISPATCH_STATUSES: tuple[str, ...] = ("open", "triaged")
#: Plan-root STATUS values that no longer hold a lane slot.
_TERMINAL_STATUSES: tuple[str, ...] = ("done", "won't-do")
#: ``meta.filed_by.client`` prefix of a Claude Code session.
_AGENT_CLIENT_PREFIX = "claude-code/"
#: ``meta.filed_by.actor`` prefix of a human account
#: (:mod:`precis.acting_user`).
_HUMAN_ACTOR_PREFIX = "user:"
#: Tag prefix of a release-waiver gripe (``release-waiver:r<N>``,
#: ``plans/red_waiver``) — a broken build: it skips the settle delay and
#: the filer-trust check, mints ahead of the lane slots, and its chain
#: claims in the release band. Honored only with ``meta.release_waiver``
#: too (a JSON object, not null), which the waiver writer alone sets.
WAIVER_TAG_PREFIX = "release-waiver:"
_WAIVER_META_KEY = "release_waiver"
#: The predicate over ``refs r`` — module constants only, never row data.
_WAIVER_SQL = (
    "(EXISTS (SELECT 1 FROM ref_tags wt JOIN tags w ON w.tag_id = wt.tag_id "
    "WHERE wt.ref_id = r.ref_id AND w.namespace IN ('OPEN', 'FLAG') "
    f"AND w.value LIKE '{WAIVER_TAG_PREFIX}%%') "
    f"AND jsonb_typeof(r.meta->'{_WAIVER_META_KEY}') = 'object')"
)
#: Waiver gripes minted per fire, over and above the lane slots.
_WAIVER_BATCH = 5
#: Open tag a coordinator sets on a gripe after reading its report
#: (gr685970): the deny-path mention is incidental — the gripe skips
#: the deny-path coordinator route and mints its chain. It is NOT a
#: hold: it must stay out of :data:`HOLD_TAGS` so the gripe remains a
#: candidate. The mint/write gates downstream still refuse a build that
#: actually writes a deny path (gr645524).
ROUTE_BUILDER_TAG = "route:builder"
#: Registry key of the filer-trust switch (:func:`gripe_trust`).
TRUST_KEY = "factory.gripe_trust"
#: ``factory.gripe_trust`` values: ``all`` skips the filer check,
#: ``filer`` keeps the Claude Code / human / ``ci.sig`` rule.
TRUST_ALL = "all"
TRUST_FILER = "filer"
#: Registry key and default of the parked-chain backstop
#: (:func:`max_parked`): no longer a brake on ordinary pile-ups (parked
#: work costs no seat), only the last resort against a runaway.
MAX_PARKED_KEY = "factory.gripe_dispatch.max_parked"
DEFAULT_MAX_PARKED = 500
#: Registry key and default of the stall window (:func:`stall_hours`).
STALL_HOURS_KEY = "factory.gripe_dispatch.stall_hours"
DEFAULT_STALL_HOURS = 6
#: Stalled chains asked about per pass.
_STALL_BATCH = 10
#: Open tag counting the asks a gripe's stalled chains raised
#: (``chain-stalls:<n>``). It only counts: the open ask itself keeps the
#: chain out of dispatch and the sweep.
STALL_TAG_PREFIX = "chain-stalls:"
#: Options of the stall ask: resume re-arms the build on its saved branch,
#: ``start over`` drops the branch and rebuilds from the base, drop is
#: closing the root ``won't-do``.
_STALL_OPTIONS = ["resume", "start over", "drop"]


def gripe_trust(store: Store) -> str:
    """The resolved ``factory.gripe_trust`` mode: :data:`TRUST_ALL` (the
    registry default) or :data:`TRUST_FILER`. An unrecognised stored
    value reads as ``filer`` — a typo tightens the rule, never loosens it."""
    raw = psettings.get_str(TRUST_KEY, store=store)
    mode = (raw or TRUST_ALL).strip().lower()
    if mode in (TRUST_ALL, TRUST_FILER):
        return mode
    log.warning(
        "gripe_dispatch: %s=%r unrecognised; using %r", TRUST_KEY, raw, TRUST_FILER
    )
    return TRUST_FILER


def gripe_dispatch_enabled(
    store: Store, *, conn: Any = None, host: str | None = None
) -> bool:
    """True iff ``service_config`` lights ``gripe_dispatch`` for ``host``
    (default: this host): exact-host row over ``*``, prio 0 or no row
    reads dark. Never raises: a blip reads as dark."""
    h = host or host_name()
    try:
        if conn is None:
            return ServiceConfigResolver(store, h).enabled(
                SERVICE_NAME, default_on=False
            )
        row = conn.execute(
            "SELECT prio FROM service_config "
            "WHERE service = %s AND host IN (%s, %s) "
            "ORDER BY (host = %s) DESC LIMIT 1",
            (SERVICE_NAME, h, ALL_HOSTS, h),
        ).fetchone()
        return bool(row) and int(row[0]) > 0
    except Exception:
        log.warning("gripe_dispatch: service_config lookup failed", exc_info=True)
        return False


@dataclass(frozen=True, slots=True)
class DispatchPick:
    gripe_id: int
    title: str
    score: float
    #: A release-waiver gripe (:data:`WAIVER_TAG_PREFIX`).
    waiver: bool = False


def lane_capacity(
    store: Store | None, lanes: tuple[_lanes.Lane, ...] | None = None
) -> int:
    """Summed cap of the enabled lanes that fit the build's task class —
    no longer the minting capacity (free executor slots are:
    :func:`build_queue_supply`); kept for diagnostics —
    less a lane the breaker holds out (:mod:`precis.plans.lane_breaker`:
    its newest builds all died of one engine cause and an ask is open)."""
    table = _lanes.load_lanes(store) if lanes is None else lanes
    held = _lane_breaker.tripped_lanes(store) if store is not None else set()
    return sum(
        ln.cap
        for ln in table
        if ln.enabled and ln.fits(_BUILD_TASK_CLASS) and ln.name not in held
    )


@dataclass(frozen=True, slots=True)
class Chain:
    """A live plan root ``related-to`` a gripe whose build is unlanded."""

    root_id: int
    gripe_ids: tuple[int, ...]
    #: Non-terminal :data:`_BUILD_OPS` nodes of the chain (task class code).
    build_ids: tuple[int, ...]
    #: Some build node is live (:func:`precis.workers.job_types.
    #: plan_dispatch.live_nodes`): a running job or an explicit claim. A
    #: queued, undispatched or file-overlap-held build is not live; it
    #: counts as parked in the census (:data:`MAX_PARKED_KEY`).
    live: bool
    #: Every todo of the plan (tagged ``plan:<root>``, root included).
    plan_ids: tuple[int, ...] = ()
    #: Some node anywhere in the plan is live or waiting in the
    #: file-overlap hold (:func:`plan_dispatch.live_and_held_nodes`): the
    #: chain is moving, whatever its build is doing.
    plan_live: bool = True


def _plan_nodes(store: Store, roots: list[int]) -> dict[int, tuple[int, ...]]:
    """``{root: every live todo tagged plan:<root>}``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, n.ref_id
              FROM refs r
              JOIN tags p ON p.namespace = 'OPEN' AND p.value = 'plan:' || r.ref_id
              JOIN ref_tags pt ON pt.tag_id = p.tag_id
              JOIN refs n ON n.ref_id = pt.ref_id
             WHERE r.ref_id = ANY(%s)
               AND n.kind = 'todo' AND n.retired_at IS NULL
             ORDER BY r.ref_id, n.ref_id
            """,
            (roots,),
        ).fetchall()
    out: dict[int, list[int]] = {}
    for root, node in rows:
        out.setdefault(int(root), []).append(int(node))
    return {k: tuple(v) for k, v in out.items()}


def _unlanded_chains(
    store: Store,
) -> list[tuple[int, tuple[int, ...], tuple[int, ...]]]:
    """``(root, gripes, build nodes)`` of every live plan root
    ``related-to`` a gripe, with its non-terminal build nodes.

    A chain is unlanded while an ``op`` node of a :data:`_BUILD_OPS` type
    under the root (every plan node carries the ``plan:<root>`` tag)
    whose ``meta.task_class`` resolves to :data:`_BUILD_TASK_CLASS` —
    unset reads ``code``, as ``plan_dispatch`` resolves it — has a
    STATUS that is not terminal. Once the build has landed the chain
    tail (gate, validate, review) may run on for hours without holding
    a lane slot (gr584410)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id,
                   array_agg(DISTINCT g.ref_id),
                   (SELECT array_agg(bn.ref_id ORDER BY bn.ref_id)
                      FROM refs bn
                      JOIN ref_tags bpt ON bpt.ref_id = bn.ref_id
                      JOIN tags bp ON bp.tag_id = bpt.tag_id
                                 AND bp.namespace = 'OPEN'
                                 AND bp.value = 'plan:' || r.ref_id
                     WHERE bn.kind = 'todo' AND bn.retired_at IS NULL
                       AND COALESCE(bn.meta->'op'->>'type', bn.meta->>'op')
                           = ANY(%s)
                       AND COALESCE(bn.meta->>'task_class', %s) = %s
                       AND NOT EXISTS (SELECT 1 FROM ref_tags bst
                                         JOIN tags bs ON bs.tag_id = bst.tag_id
                                        WHERE bst.ref_id = bn.ref_id
                                          AND bs.namespace = 'STATUS'
                                          AND bs.value = ANY(%s)))
              FROM refs r
              JOIN links l ON l.src_ref_id = r.ref_id AND l.relation = 'related-to'
              JOIN refs g ON g.ref_id = l.dst_ref_id AND g.kind = 'gripe'
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND EXISTS (SELECT 1 FROM ref_tags pt JOIN tags p ON p.tag_id = pt.tag_id
                            WHERE pt.ref_id = r.ref_id AND p.namespace = 'OPEN'
                              AND p.value = 'plan:' || r.ref_id)
               AND NOT EXISTS (SELECT 1 FROM ref_tags st JOIN tags s ON s.tag_id = st.tag_id
                                WHERE st.ref_id = r.ref_id AND s.namespace = 'STATUS'
                                  AND s.value = ANY(%s))
             GROUP BY r.ref_id
             ORDER BY r.ref_id
            """,
            (
                list(_BUILD_OPS),
                _BUILD_TASK_CLASS,
                _BUILD_TASK_CLASS,
                sorted(TERMINAL_TODO_STATUSES),
                list(_TERMINAL_STATUSES),
            ),
        ).fetchall()
    return [
        (int(r[0]), tuple(int(x) for x in r[1]), tuple(int(x) for x in r[2]))
        for r in rows
        if r[2]
    ]


def chains(store: Store) -> list[Chain]:
    """Every unlanded chain, marked running or waiting/parked.

    A load failure conservatively reads every chain live. Queue supply is
    counted separately, so a waiting chain never consumes an executor slot.
    """
    rows = _unlanded_chains(store)
    if not rows:
        return []
    from precis.workers.job_types.plan_dispatch import live_and_held_nodes

    roots = [r[0] for r in rows]
    try:
        live, held = live_and_held_nodes(store, roots)
        plans = _plan_nodes(store, roots)
    except Exception:
        log.warning(
            "gripe_dispatch: build liveness read failed; counting every chain live",
            exc_info=True,
        )
        live = {b for r in rows for b in r[2]}
        held = set()
        plans = {}
    return [
        Chain(
            root,
            gids,
            builds,
            bool(live & set(builds)),
            plans.get(root, ()),
            # No plan read means no evidence of a stall: read it moving.
            # An overlap-held node is waiting on another build's land,
            # not stalled: it keeps the plan moving though it holds no slot.
            root not in plans or bool((live | held) & set(plans[root])),
        )
        for root, gids, builds in rows
    ]


def open_chain_count(store: Store) -> int:
    """Chains holding a build slot: unlanded chains whose build is live.

    A parked build (``ask-user``, ``waiting-for:*``, ``child-failed:*``,
    paused, blocked, file-overlap held, claim-less and job-less) holds no
    slot — its branch is saved and :func:`sweep_stalled` asks about it
    once the plan has stood still (gr687946)."""
    return sum(1 for c in chains(store) if c.live)


def parked_chain_count(store: Store) -> int:
    """Unlanded chains whose build is not live."""
    return sum(1 for c in chains(store) if not c.live)


def max_parked(store: Store) -> int:
    """``factory.gripe_dispatch.max_parked`` — the last-resort backstop on
    parked chains; unset or below 1 reads :data:`DEFAULT_MAX_PARKED`."""
    raw = psettings.get_int(MAX_PARKED_KEY, store=store)
    return raw if raw is not None and raw >= 1 else DEFAULT_MAX_PARKED


def stall_hours(store: Store) -> int:
    """``factory.gripe_dispatch.stall_hours``; unset or below 1 reads
    :data:`DEFAULT_STALL_HOURS`."""
    raw = psettings.get_int(STALL_HOURS_KEY, store=store)
    return raw if raw is not None and raw >= 1 else DEFAULT_STALL_HOURS


def _idle_hours(store: Store, node_ids: tuple[int, ...]) -> float:
    """Hours since anything moved on ``node_ids`` (the whole plan): a
    node's ``updated_at``, any tag stamp on it (a STATUS or hold tag), or
    a job under it."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH nodes AS (SELECT ref_id, updated_at FROM refs WHERE ref_id = ANY(%s))
            SELECT extract(epoch FROM now() - max(g)) / 3600.0 FROM (
                SELECT updated_at AS g FROM nodes
                UNION ALL
                SELECT rt.created_at FROM nodes
                  JOIN ref_tags rt ON rt.ref_id = nodes.ref_id
                UNION ALL
                SELECT j.updated_at FROM refs j
                  JOIN nodes ON j.parent_id = nodes.ref_id
                 WHERE j.kind = 'job'
            ) a
            """,
            (list(node_ids),),
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _open_ask(store: Store, root_id: int) -> bool:
    """A non-terminal ask todo (``ask-user`` tag or ``meta.ask``) sits in
    the plan: a human may be waiting, so the chain is not stalled."""
    with store.pool.connection() as conn:
        return (
            conn.execute(
                """
                SELECT 1 FROM refs a
                  JOIN ref_tags pt ON pt.ref_id = a.ref_id
                  JOIN tags p ON p.tag_id = pt.tag_id
                             AND p.namespace = 'OPEN' AND p.value = %s
                 WHERE a.kind = 'todo' AND a.retired_at IS NULL
                   AND (jsonb_typeof(a.meta->'ask') = 'object'
                        OR EXISTS (SELECT 1 FROM ref_tags xt
                                     JOIN tags x ON x.tag_id = xt.tag_id
                                    WHERE xt.ref_id = a.ref_id
                                      AND x.namespace = 'OPEN'
                                      AND x.value LIKE 'ask-user%%'))
                   AND NOT EXISTS (SELECT 1 FROM ref_tags st
                                     JOIN tags s ON s.tag_id = st.tag_id
                                    WHERE st.ref_id = a.ref_id
                                      AND s.namespace = 'STATUS'
                                      AND s.value = ANY(%s))
                 LIMIT 1
                """,
                (f"plan:{root_id}", sorted(TERMINAL_TODO_STATUSES)),
            ).fetchone()
            is not None
        )


def _stall_count(store: Store, gripe_id: int) -> tuple[int, list[str]]:
    """The gripe's ``chain-stalls:<n>`` count and the tag values held."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'OPEN' AND t.value LIKE %s",
            (gripe_id, STALL_TAG_PREFIX + "%"),
        ).fetchall()
    values = [str(r[0]) for r in rows]
    count = 0
    for v in values:
        tail = v[len(STALL_TAG_PREFIX) :]
        if tail.isdigit():
            count = max(count, int(tail))
    return count, values


def _comment_todo(store: Store, todo_id: int, text: str) -> None:
    """A worker-stamped ``todo_comment`` on a plan root."""
    existing = store.chunks.list_chunks_for_ref(todo_id)
    ord_ = max((int(c.ord) for c in existing), default=0) + 1
    store.chunks.insert_chunks(
        todo_id,
        [
            ChunkInsert(
                ord=ord_,
                text=text,
                meta={"chunk_kind": "todo_comment", **worker_comment_meta()},
            )
        ],
    )


def sweep_stalled(
    store: Store, parked: list[Chain], *, limit: int = _STALL_BATCH
) -> list[dict[str, Any]]:
    """Ask about chains stalled with no human waiting (gr687946; parked
    work is saved, never thrown away — Reto 2026-11-16).

    A parked chain — build not live, no node of its plan live or held in
    the file-overlap hold (a build blocked-by a running review, or
    waiting on another build's land, is waiting, not stalled), no open ask
    in the plan, and nothing moved on ANY plan node (``updated_at``, tag
    stamps, jobs) for :func:`stall_hours` — gets ONE ask-user todo,
    ``td<root>: chain stalled <age> — resume, start over, or drop?``,
    blocking its build nodes and naming the state found and the parked
    branch/host (``meta.parked``). The chain is never closed: the open ask
    keeps it out of dispatch and of this sweep (an open ask in the plan
    shields it), and closing the ask re-arms the build —
    ``plan_dispatch._step_rearm``, which resumes from the saved branch or,
    on ``start over``, drops it. Each gripe carries ``chain-stalls:<n>``
    (replaced per ask); the count no longer holds the gripe — the open ask
    does. At most ``limit`` chains are asked about per pass, idlest first.
    Returns one dict per ask: ``root``, ``gripes``, ``idle_hours``,
    ``stalls``, ``ask``."""
    hours = stall_hours(store)
    due: list[tuple[float, Chain]] = []
    for c in parked:
        try:
            # Stalled is a property of the whole plan: nothing live in
            # it (an upstream review or brief the build is blocked-by
            # may legitimately run long), nothing touched for the window.
            if c.plan_live or not c.plan_ids:
                continue
            idle = _idle_hours(store, c.plan_ids)
            if idle >= hours and not _open_ask(store, c.root_id):
                due.append((idle, c))
        except Exception:
            log.warning(
                "gripe_dispatch: stall check on td%d failed", c.root_id, exc_info=True
            )
    due.sort(key=lambda x: (-x[0], x[1].root_id))
    asked: list[dict[str, Any]] = []
    for idle, c in due[:limit]:
        try:
            result = _ask_stalled(store, c, idle, hours)
            if result is not None:
                asked.append(result)
        except Exception:
            log.warning(
                "gripe_dispatch: asking about stalled td%d failed",
                c.root_id,
                exc_info=True,
            )
    if asked:
        log.info(
            "gripe_dispatch: asked about %d stalled chain(s): %s",
            len(asked),
            ", ".join(f"td{z['root']}" for z in asked),
        )
    return asked


def _ask_stalled(
    store: Store, c: Chain, idle: float, hours: int
) -> dict[str, Any] | None:
    from precis.store.advisory import try_xact_advisory_lock
    from precis.workers.job_types.plan_dispatch import (
        TICK_LOCK_IDLE_TIMEOUT,
        TICK_LOCK_KEY,
    )

    # The census is only a shortlist. A tick can re-arm or dispatch the
    # plan before we get here; exclude ticks through the ask writes.
    if store.dsn is None:
        return None
    with try_xact_advisory_lock(
        store.dsn, TICK_LOCK_KEY, idle_timeout=TICK_LOCK_IDLE_TIMEOUT
    ) as got:
        if not got:
            return None
        current = next((x for x in chains(store) if x.root_id == c.root_id), None)
        if current is None or current.live or current.plan_live or not current.plan_ids:
            return None
        fresh_idle = _idle_hours(store, current.plan_ids)
        if fresh_idle < hours or _open_ask(store, current.root_id):
            return None
        return _write_stalled_ask(store, current, fresh_idle)


def _chain_state(store: Store, c: Chain) -> tuple[str, list[str]]:
    """``(state, parked lines)`` of the chain's build nodes: STATUS and
    the park-ish tags each carries, and where its saved branch is
    (``meta.parked``: host, worktree, head)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta->'parked',
                   (SELECT string_agg(t.value, ', ' ORDER BY t.value)
                      FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'),
                   (SELECT string_agg(t.value, ', ' ORDER BY t.value)
                      FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                       AND (t.value LIKE 'rescue:%%' OR t.value LIKE 'child-failed:%%'
                            OR t.value LIKE 'waiting-for:%%' OR t.value LIKE 'hold:%%'
                            OR t.value = 'halt'))
              FROM refs r WHERE r.ref_id = ANY(%s) ORDER BY r.ref_id
            """,
            (list(c.build_ids),),
        ).fetchall()
    states: list[str] = []
    saved: list[str] = []
    for ref_id, parked, status, tags in rows:
        states.append(
            f"td{ref_id} STATUS:{status or 'open'}" + (f" [{tags}]" if tags else "")
        )
        if isinstance(parked, dict) and parked.get("head"):
            saved.append(
                f"td{ref_id}: branch slice/{ref_id} saved on `{parked.get('host')}` "
                f"(worktree `{parked.get('worktree')}`, head "
                f"`{str(parked['head'])[:12]}`, pushed to "
                f"`refs/factory/slice/{ref_id}`)"
            )
    return "; ".join(states) or "no build node found", saved


def _mint_stalled_ask(
    store: Store, c: Chain, title: str, body: str, meta: dict[str, Any]
) -> int:
    """The ask todo itself, linked ``blocked-by`` from every build node of
    the chain — one transaction, ending in ``finish_mint``."""
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=c.root_id,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{c.root_id}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        for build in c.build_ids:
            store.add_link(
                src_ref_id=build,
                dst_ref_id=ref.id,
                relation="blocked-by",
                conn=conn,
            )
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _write_stalled_ask(store: Store, c: Chain, idle: float) -> dict[str, Any]:
    state, saved = _chain_state(store, c)
    age = f"{idle:.0f} h"
    handle = f"td{c.root_id}"
    title = f"{handle}: chain stalled {age} — resume, start over, or drop?"
    found = (
        f"build node(s) not live (no live job, claim or doable state) and no "
        f"live node in the plan, plan untouched for {age}, no open ask. "
        f"State: {state}."
    )
    where = (
        " ".join(saved)
        if saved
        else "No saved branch is stamped on the build (it never reached "
        "park-save, or nothing was uncommitted or ahead of the base)."
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_STALL_OPTIONS),
            "blocks": f"td{c.build_ids[0]}",
            "context": f"{handle} stalled {age}: {state}"[:600],
        }
    }
    check_ask_meta(meta)
    body = (
        f"The plan under {handle} (gripe {', '.join(f'gr{g}' for g in c.gripe_ids)}) "
        f"has stood still for {age}: {found}\n\n{where}\n\n"
        "Nothing was closed and nothing is thrown away. Answer with the "
        "option you pick and close this todo: `resume` re-arms the build to "
        "carry on from the saved branch (on any host); `start over` drops "
        "the saved branch and rebuilds from the base; `drop` — close "
        f"{handle} STATUS:won't-do. `resume` lifts STATUS:paused on the "
        "build; a `waiting-for:*` hold (waiting on files) stays until lifted "
        "by hand."
    )
    ask = _mint_stalled_ask(store, c, title, body, meta)
    _comment_todo(
        store,
        c.root_id,
        f"gripe_dispatch: chain stalled {age} — ask td{ask} (resume, start over, "
        f"or drop). {found}",
    )
    stalls = 0
    for gid in c.gripe_ids:
        prev, held = _stall_count(store, gid)
        stalls = prev + 1
        for v in held:
            store.remove_tag(gid, Tag.open(v))
        store.add_tag(gid, Tag.open(f"{STALL_TAG_PREFIX}{stalls}"), set_by="system")
        _append_comment(
            store,
            gid,
            f"dispatch: chain {handle} stalled {age} — ask td{ask} "
            f"(resume, start over, or drop); stall {stalls}.",
        )
    return {
        "root": c.root_id,
        "gripes": list(c.gripe_ids),
        "idle_hours": round(idle, 1),
        "stalls": stalls,
        "ask": ask,
    }


def candidates(store: Store, limit: int) -> list[DispatchPick]:
    """The next ``limit`` dispatchable gripes, release-waiver gripes first,
    then hottest score then oldest."""
    return _candidate_picks(store, limit, supported_projects=True)


def _candidate_picks(
    store: Store,
    limit: int,
    *,
    supported_projects: bool,
    waiver: bool | None = None,
) -> list[DispatchPick]:
    """Select supported gripes or unsupported ones still needing a note.
    ``waiver`` ``True`` keeps only release-waiver gripes, ``False`` drops
    them, ``None`` keeps both (waivers sort first either way)."""
    if limit <= 0:
        return []
    waiver_filter = {None: "TRUE", True: _WAIVER_SQL, False: f"NOT {_WAIVER_SQL}"}[
        waiver
    ]
    sql = f"""
        SELECT r.ref_id, r.title, {_dscore.score_sql()} AS score,
               {_WAIVER_SQL} AS is_waiver
          FROM refs r
          JOIN ref_tags st ON st.ref_id = r.ref_id
          JOIN tags s ON s.tag_id = st.tag_id
                     AND s.namespace = 'STATUS' AND s.value = ANY(%s)
         WHERE r.kind = 'gripe' AND r.retired_at IS NULL
           AND {waiver_filter}
           AND ({_WAIVER_SQL} OR COALESCE((SELECT max(tt.created_at)
                           FROM ref_tags tt JOIN tags t ON t.tag_id = tt.tag_id
                          WHERE tt.ref_id = r.ref_id
                            AND ((t.namespace = 'STATUS' AND t.value = 'triaged')
                                 OR (t.namespace IN ('OPEN', 'FLAG')
                                     AND t.value = ANY(%s)))),
                        r.created_at) < now() - %s * interval '1 minute')
           AND (%s OR {_WAIVER_SQL} OR r.meta->'filed_by'->>'client' LIKE %s
                OR r.meta->'filed_by'->>'actor' LIKE %s
                OR (r.meta->'ci'->>'sig' IS NOT NULL
                    AND r.meta->'filed_by'->>'principal' = %s))
           AND EXISTS (SELECT 1 FROM ref_tags ft JOIN tags f ON f.tag_id = ft.tag_id
                        WHERE ft.ref_id = r.ref_id AND f.namespace IN ('OPEN', 'FLAG')
                          AND f.value = ANY(%s))
           AND COALESCE((SELECT bool_and(substr(p.value, 9) = ANY(%s))
                           FROM ref_tags pt JOIN tags p ON p.tag_id = pt.tag_id
                          WHERE pt.ref_id = r.ref_id AND p.namespace IN ('OPEN', 'FLAG')
                            AND p.value LIKE 'project:%%'), %s) = %s
           AND (%s OR NOT EXISTS (
                SELECT 1 FROM chunks c WHERE c.ref_id = r.ref_id
                  AND c.chunk_kind = 'gripe_comment' AND c.text LIKE %s))
           AND NOT EXISTS (SELECT 1 FROM ref_tags ht JOIN tags h ON h.tag_id = ht.tag_id
                            WHERE ht.ref_id = r.ref_id AND h.namespace IN ('OPEN', 'FLAG')
                              AND h.value = ANY(%s))
           AND NOT EXISTS (SELECT 1 FROM links bl JOIN refs bt ON bt.ref_id = bl.dst_ref_id
                            WHERE bl.src_ref_id = r.ref_id AND bl.relation = 'blocked-by'
                              AND bt.retired_at IS NULL
                              AND COALESCE((SELECT t.value FROM ref_tags rt
                                                    JOIN tags t ON t.tag_id = rt.tag_id
                                             WHERE rt.ref_id = bt.ref_id
                                               AND t.namespace = 'STATUS' LIMIT 1),
                                           'open') NOT IN {blocker_terminal_sql()})
           AND NOT EXISTS (SELECT 1 FROM links l JOIN refs t ON t.ref_id = l.src_ref_id
                            WHERE l.dst_ref_id = r.ref_id AND l.relation = 'related-to'
                              AND t.kind = 'todo' AND t.retired_at IS NULL
                              AND NOT EXISTS (SELECT 1 FROM ref_tags lt
                                                JOIN tags ls ON ls.tag_id = lt.tag_id
                                               WHERE lt.ref_id = t.ref_id
                                                 AND ls.namespace = 'STATUS'
                                                 AND ls.value = ANY(%s)))
         ORDER BY {_WAIVER_SQL} DESC, {_dscore.score_sql()} DESC,
                  r.created_at, r.ref_id
         LIMIT %s
    """
    trust_all = gripe_trust(store) == TRUST_ALL
    fix_tags = list(FIX_CLASS_TAGS)
    with store.pool.connection() as conn:
        rows = conn.execute(
            sql,
            (
                list(_DISPATCH_STATUSES),
                fix_tags,
                _MIN_AGE_MINUTES,
                trust_all,
                _AGENT_CLIENT_PREFIX + "%",
                _HUMAN_ACTOR_PREFIX + "%",
                _CI_WATCH_PRINCIPAL,
                fix_tags,
                sorted(_LANDABLE_PROJECTS),
                "precis" in _LANDABLE_PROJECTS,
                supported_projects,
                supported_projects,
                _PROJECT_SKIP_PREFIX + "%",
                list(HOLD_TAGS),
                sorted(TERMINAL_TODO_STATUSES),
                limit,
            ),
        ).fetchall()
    return [
        DispatchPick(int(r[0]), str(r[1] or ""), float(r[2]), bool(r[3])) for r in rows
    ]


def build_queue_supply(store: Store) -> tuple[int, int]:
    """Free host executor slots and queued builds; lane caps are not capacity.

    Use the claimer's live host advertisements, executor cap and reserved
    bands. An unavailable host contributes nothing, and a queued or held
    build consumes no running slot.
    """
    from precis.workers.executors import _common as claims
    from precis.workers.service_config import ServiceConfigResolver

    with store.pool.connection() as conn:
        advertised = claims.advertised_by_host(conn)
        build_cap = claims.job_type_running_caps(conn).get("run_slice")
        free = 0
        for host, tokens in advertised.items():
            if "builder" not in tokens:
                continue
            if not ServiceConfigResolver(store, host).enabled(
                "job_ssh_node", default_on=True
            ):
                continue
            if claims.reserve_active(conn, host):
                continue
            cap, reserve = claims.executor_lane_limits(conn, "ssh_node", host)
            if cap is None:
                continue
            rows = conn.execute(
                "SELECT r.meta->>'job_type', count(*) FROM refs r "
                "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
                "JOIN tags t USING (tag_id) WHERE r.kind = 'job' "
                "AND r.retired_at IS NULL AND r.meta->>'executor' = 'ssh_node' "
                "AND r.meta->>'lease_host' = %s "
                "AND t.namespace = 'STATUS' AND t.value = 'running' GROUP BY 1",
                (host,),
            ).fetchall()
            running = {str(jt): int(n) for jt, n in rows}
            total = sum(running.values())
            own = reserve.get("run_slice", 0)
            available = max(cap - (sum(reserve.values()) - own) - total, 0)
            available = max(available, own - running.get("run_slice", 0))
            available = max(0, min(cap - total, available))
            if build_cap is not None:
                available = min(
                    available, max(build_cap - running.get("run_slice", 0), 0)
                )
            free += available
        row = conn.execute(
            "SELECT count(*) FROM refs r JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t USING (tag_id) WHERE r.kind = 'job' "
            "AND r.retired_at IS NULL AND r.meta->>'job_type' = 'run_slice' "
            "AND EXISTS (SELECT 1 FROM refs b WHERE b.ref_id = r.parent_id "
            "AND b.retired_at IS NULL AND NOT COALESCE(b.meta ? 'generate', FALSE) "
            "AND NOT EXISTS (SELECT 1 FROM ref_tags bt JOIN tags s USING (tag_id) "
            "WHERE bt.ref_id = b.ref_id AND s.namespace = 'STATUS' "
            "AND s.value = ANY(%s))) "
            "AND t.namespace = 'STATUS' AND t.value = 'queued'",
            (sorted(TERMINAL_TODO_STATUSES),),
        ).fetchone()
    return free, int(row[0]) if row else 0


def run_gripe_dispatch_pass(store: Store, *, host: str | None = None) -> BatchResult:
    """One fire: queue deficit → candidates → one chain each. ``claimed`` is
    the number of gripes the tick tried, ``ok`` the chains minted."""
    if not gripe_dispatch_enabled(store, host=host):
        log.info("gripe_dispatch: dark — no live service_config row; no-op")
        return BatchResult(handler=SERVICE_NAME, claimed=0, ok=0, failed=0)
    from precis.workers.job_types.plan_dispatch import read_only_reason

    reason = read_only_reason(store)
    if reason is not None:
        log.info("gripe_dispatch: read-only (%s) — minting nothing", reason)
        return BatchResult(
            handler=SERVICE_NAME,
            claimed=0,
            ok=0,
            failed=0,
            extra={"read_only": reason},
        )
    # Cause-based breaker first: a lane or host whose newest builds all died
    # of one engine cause stops taking builds (its ask is the only record),
    # before the capacity below is read.
    breaker = _run_breaker(store)
    from precis.workers.job_types.plan_dispatch import enqueue_roots

    census = chains(store)
    # gr687946: ask about stalled chains — nothing is closed, nothing thrown
    # away; the ask keeps the chain out of the next sweep. Before the
    # enqueue below: its fresh job stamps would read as plan movement.
    stalled = sweep_stalled(store, [c for c in census if not c.live])
    enqueue_roots(store, [c.root_id for c in census])
    capacity, queued = build_queue_supply(store)
    open_chains = sum(1 for c in census if c.live)
    parked = sum(1 for c in census if not c.live)
    cap_parked = max_parked(store)
    depth = max(
        1, psettings.get_int("factory.gripe_dispatch.queue_depth", store=store) or 2
    )
    slots = max(depth * capacity - queued, 0)
    parked_full = parked >= cap_parked
    if parked_full:
        log.info(
            "gripe_dispatch: parked=%d reached the backstop %s=%d — minting no "
            "new chains (waiver chains excepted) until the parked ones are ruled",
            parked,
            MAX_PARKED_KEY,
            cap_parked,
        )
    _note_unsupported_projects(store)
    # A broken build (release-waiver gripe) mints at once and ahead of the
    # slot count: no settle delay, no slot needed — its chain claims in the
    # release band, so the lanes take it before ordinary builds.
    waivers = _candidate_picks(
        store, _WAIVER_BATCH, supported_projects=True, waiver=True
    )
    slots = max(slots - len(waivers), 0)
    picks = [
        *waivers,
        *(
            []
            if parked_full
            else _candidate_picks(store, slots, supported_projects=True, waiver=False)
        ),
    ]
    extra: dict[str, Any] = {
        "lane_capacity": capacity,
        "free_executor_slots": capacity,
        "queued_builds": queued,
        "queue_depth": depth,
        "open_chains": open_chains,
        "parked": parked,
        "max_parked": cap_parked,
        "stalled": stalled,
        "breaker": breaker,
        "slots": slots,
        "waivers": [w.gripe_id for w in waivers],
        "minted": [],
    }
    ok = failed = 0
    # gr657066: governs:factory skills route to the coordinator like
    # deny-path text; the scan failing open matches the classifier's
    # own posture (the mint gates fail closed downstream).
    deny = _lanes.deny_paths_for(store) + _governs.governed_skill_paths()
    for c in picks:
        # gr656970: the candidate read judged ``blocked-by`` at pick
        # time — a blocker linked between then and the mint still holds
        # the chain; the read re-runs here, and ``candidates`` skips the
        # gripe next pass until the blocker closes. The claim re-gates
        # it too (``executors._common.build_blocked_holds`` follows the
        # chain's ``related-to`` edge back to the gripe).
        if open_blocked_by(store, [c.gripe_id]):
            log.info(
                "gripe_dispatch: gr%d blocked since the pick — no mint",
                c.gripe_id,
            )
            extra.setdefault("held", []).append(c.gripe_id)
            continue
        # gr645163: work no builder lane may write — the report asks to
        # write a deny path, or the fix is prod-graph only — skips the
        # chain and files the one coordinator ask with the report
        # attached. A classifier blip must not lose the mint, so it
        # fails open.
        report = ""
        try:
            report = _route_report(store, c)
            # gr685970: ``route:builder`` skips the deny-path leg only
            # (an empty deny set) — a graph-only report still routes.
            deny_set = () if _route_override(store, c.gripe_id) else deny
            routed = _route.coordinator_route(report, deny_set)
        except Exception:
            log.warning(
                "gripe_dispatch: route check on gr%d failed; minting anyway",
                c.gripe_id,
                exc_info=True,
            )
            routed = None
        if routed is not None:
            try:
                ask_id = _route_to_coordinator(store, c, routed, report)
            except Exception:
                failed += 1
                log.warning(
                    "gripe_dispatch: coordinator route for gr%d failed",
                    c.gripe_id,
                    exc_info=True,
                )
                continue
            ok += 1
            extra.setdefault("routed", []).append({"gripe": c.gripe_id, "ask": ask_id})
            continue
        try:
            mint = mint_gripe_fix_direct(
                store,
                gripe_id=c.gripe_id,
                claim_band=_dscore.RELEASE_CLAIM_BAND if c.waiver else None,
            )
        except Exception as exc:
            failed += 1
            log.warning("gripe_dispatch: mint for gr%d failed: %s", c.gripe_id, exc)
            try:
                fails = _record_mint_failure(store, c.gripe_id, exc)
            except Exception:
                log.warning(
                    "gripe_dispatch: failure note on gr%d failed",
                    c.gripe_id,
                    exc_info=True,
                )
                fails = []
            # Three failed mints rescue the gripe (td574946 S1): without
            # this the stateless pass retried the same mint forever.
            if len(fails) >= 3:
                try:
                    _rescue_intake(store, c.gripe_id, exc, fails)
                except Exception:
                    log.warning(
                        "gripe_dispatch: rescue for gr%d failed",
                        c.gripe_id,
                        exc_info=True,
                    )
            continue
        ok += 1
        extra["minted"].append({"gripe": c.gripe_id, "root": mint.root_id})
        try:
            _comment(store, c.gripe_id, mint.root_id)
        except Exception:  # pragma: no cover — the chain exists either way
            log.warning(
                "gripe_dispatch: comment on gr%d failed", c.gripe_id, exc_info=True
            )
    enqueue_roots(store, [m["root"] for m in extra["minted"]])
    log.info(
        "gripe_dispatch: capacity=%d open=%d parked=%d queued=%d slots=%d minted=%d failed=%d",
        capacity,
        open_chains,
        parked,
        queued,
        slots,
        ok,
        failed,
    )
    return BatchResult(
        handler=SERVICE_NAME, claimed=len(picks), ok=ok, failed=failed, extra=extra
    )


def _run_breaker(store: Store) -> list[dict[str, Any]]:
    """Open the one ask per newly tripped lane/host
    (:func:`precis.plans.lane_breaker.detect`). A blip never stops the
    pass: it logs and reports nothing."""
    opened: list[dict[str, Any]] = []
    try:
        k = _lane_breaker.breaker_parks(store)
        for trip in _lane_breaker.detect(store, k):
            ask = _lane_breaker.open_trip(store, trip, k)
            opened.append(
                {
                    "scope": trip.scope,
                    "name": trip.name,
                    "cause": trip.cause,
                    "jobs": list(trip.jobs),
                    "lanes": list(trip.lanes),
                    "ask": ask,
                }
            )
    except Exception:
        log.warning("gripe_dispatch: breaker pass failed", exc_info=True)
    return opened


def _route_override(store: Store, gripe_id: int) -> bool:
    """True when the gripe carries :data:`ROUTE_BUILDER_TAG` — the
    coordinator's read-the-report override that skips the deny-path
    route (gr685970)."""
    with store.pool.connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = %s AND t.namespace = 'OPEN' "
                "AND t.value = %s LIMIT 1",
                (gripe_id, ROUTE_BUILDER_TAG),
            ).fetchone()
            is not None
        )


def _route_report(store: Store, pick: DispatchPick) -> str:
    """The text the mint-time coordinator route judges for a gripe
    (gr645163): title plus every visible chunk — the same material
    ``mint_gripe_fix_direct`` embeds in the build body — minus routing
    notes (gr653037): the dispatcher's own ``dispatch: routes to the
    coordinator …`` comment quotes the deny path it routed on, and a
    coordinator note is about the routing, not the work."""
    parts = [
        t
        for t in gripe_report_texts(store, pick.gripe_id)
        if not _route.routing_note(t)
    ]
    return "\n\n".join([pick.title, *parts])


def _route_to_coordinator(
    store: Store, pick: DispatchPick, why: str, report: str
) -> int:
    """File the one ``ask-coordinator`` ask a routed gripe becomes
    (gr645163): the report rides the ask body — the ruling's "with the
    diff/brief attached" — the ask's ``related-to`` link keeps the gripe
    out of ``candidates`` while it is open, and ``hold:no-dispatch``
    keeps it out after, until a human re-judges the work (the
    coordinator removes the tag to release the gripe back to dispatch)."""
    handle = f"gr{pick.gripe_id}"
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": ["acknowledged"],
            "blocks": "nothing",
            "context": f"{handle}: {why}"[:600],
        }
    }
    check_ask_meta(meta)
    body = (
        f"gripe_dispatch classified {handle} ({pick.title!r}) as work no "
        f"builder lane may do: the report {why}. No gripe-fix chain was "
        "minted — the builder that ran it would refuse as won't-do or end "
        "with no commit (gr645163, the td643078/td638980 stalls). The "
        "report follows; a coordinator call: land it by hand, mint a "
        "builder outside the factory's deny rules, or drop the gripe — "
        "removing `hold:no-dispatch` returns it to dispatch.\n\n---\n\n"
        + report.strip()[:4000]
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"{handle}: {why} — a coordinator call",
            meta=meta,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-coordinator", "ask-user", "showstopper"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=ref.id,
            dst_ref_id=pick.gripe_id,
            relation="related-to",
            conn=conn,
        )
        store.add_tag(
            pick.gripe_id,
            Tag.open("hold:no-dispatch"),
            set_by="system",
            conn=conn,
        )
        finish_mint(store, ref.id, conn=conn)
    ask_id = int(ref.id)
    _append_comment(
        store,
        pick.gripe_id,
        f"dispatch: routes to the coordinator — report {why}; ask td{ask_id}. "
        "No chain minted; `hold:no-dispatch` holds the gripe — remove the "
        "tag to release it back to dispatch.",
    )
    return ask_id


def _note_unsupported_projects(store: Store) -> None:
    """Bound skip-note work per tick; noted gripes leave this queue."""
    allowed = ", ".join(f"project:{p}" for p in sorted(_LANDABLE_PROJECTS)) or "none"
    for pick in _candidate_picks(store, 100, supported_projects=False):
        try:
            _append_comment(
                store,
                pick.gripe_id,
                f"{_PROJECT_SKIP_PREFIX} factory can only land {allowed}; "
                "this gripe's project is outside that allowlist. No chain minted.",
                once_prefix=_PROJECT_SKIP_PREFIX,
            )
        except Exception:
            log.warning(
                "gripe_dispatch: project skip note on gr%d failed",
                pick.gripe_id,
                exc_info=True,
            )


def _append_comment(
    store: Store, gripe_id: int, text: str, *, once_prefix: str | None = None
) -> None:
    from precis.store.types import ChunkInsert

    with store.tx() as conn:
        if once_prefix is not None:
            # Serialize the check and append so overlapping ticks also converge.
            conn.execute(
                "SELECT ref_id FROM refs WHERE ref_id = %s FOR UPDATE", (gripe_id,)
            )
            if conn.execute(
                "SELECT 1 FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'gripe_comment' AND text LIKE %s LIMIT 1",
                (gripe_id, once_prefix + "%"),
            ).fetchone():
                return
        ords = conn.execute(
            "SELECT coalesce(max(ord), -1) + 1 FROM chunks WHERE ref_id = %s",
            (gripe_id,),
        ).fetchone()
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=int(ords[0]) if ords else 0,
                    text=text,
                    meta={
                        "chunk_kind": "gripe_comment",
                        # worker-stamped — the G2 trust boundary (td575705).
                        **worker_comment_meta(),
                    },
                )
            ],
            conn=conn,
        )


def _comment(store: Store, gripe_id: int, root_id: int) -> None:
    _append_comment(
        store,
        gripe_id,
        f"dispatch: minted gripe-fix-direct td{root_id} (build picks its lane at "
        "dispatch, devin first; the land writes `resolved: <sha>` here)",
    )


#: Prefix of the mint-failure comments; the count of comments carrying
#: it is the failure count (the pass is stateless — the gripe's own
#: comment trail is the only ledger).
_MINT_FAIL_PREFIX = "dispatch: mint failed:"


def _record_mint_failure(store: Store, gripe_id: int, exc: Exception) -> list[str]:
    """Append this failure as a ``gripe_comment`` and return every
    ``dispatch: mint failed:`` comment text on the gripe, oldest first."""
    _append_comment(store, gripe_id, f"{_MINT_FAIL_PREFIX} {exc}")
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'gripe_comment' AND text LIKE %s ORDER BY ord",
            (gripe_id, _MINT_FAIL_PREFIX + "%"),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _rescue_intake(
    store: Store, gripe_id: int, exc: Exception, fails: list[str]
) -> None:
    """A gripe that failed three mints enters rescue itself (class
    ``intake``): it is its own signature gripe and ``rescue:intake`` is a
    hold tag, so it leaves ``candidates``."""
    from precis.workers import factory_rescue

    factory_rescue.enter_rescue(
        store,
        gripe_id,
        "intake",
        factory_rescue.RescueEvidence(
            step="gripe_dispatch mint",
            error_line=f"{type(exc).__name__}: {exc}",
            log_tail="\n".join(fails),
        ),
    )


__all__ = [
    "FIX_CLASS_TAGS",
    "HOLD_TAGS",
    "ROUTE_BUILDER_TAG",
    "SERVICE_NAME",
    "TRUST_KEY",
    "WAIVER_TAG_PREFIX",
    "Chain",
    "DispatchPick",
    "candidates",
    "chains",
    "gripe_dispatch_enabled",
    "gripe_trust",
    "lane_capacity",
    "open_chain_count",
    "parked_chain_count",
    "run_gripe_dispatch_pass",
    "sweep_stalled",
]
