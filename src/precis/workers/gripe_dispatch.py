"""``gripe_dispatch`` — turn fixable gripes into ``gripe-fix-direct`` chains.

The dark factory's intake (``docs/backlog/dark-factory.md``, td564019 cut
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

* **Supply.** Open chains = live plan roots ``related-to`` a gripe whose
  STATUS is not terminal **and whose build has not landed**: an ``op``
  node of a :data:`_BUILD_OPS` type under the root, non-terminal, whose
  ``task_class`` resolves to ``code``. A chain parked on the sha-in-prod
  gate, on validate or on the non-blocking review holds no slot
  (gr584410). Capacity = the summed ``cap`` of the enabled lanes that
  fit the ``code`` task class (``plans/lanes.py``). Slots = capacity
  minus held slots — one tick may fill every free build slot, and a
  chain whose build never lands holds its slot.
* **Candidates** (:func:`candidates`): live gripes, STATUS ``open`` or
  ``triaged``, tagged any of :data:`FIX_CLASS_TAGS` (``fix:code``,
  ``fix:skill``, ``fix:docs``, ``fix:web``, ``fix:mcp``; Reto
  2026-11-10), waiting :data:`_MIN_AGE_MINUTES`
  since the latest ``STATUS:triaged`` or fix-class attachment (filing
  time only if neither timestamp is known), with no live todo
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
  model"): a picked gripe whose report names a deny path or is
  prod-graph only mints no chain — work no builder lane may write skips
  the builders (:mod:`precis.plans.route`, the same classifier
  ``plan_dispatch._step_dispatch`` runs on each todo's brief). One
  ``ask-coordinator`` ask carries the report for the coordinator — the
  ruling's "with the diff/brief attached" — and the gripe holds
  ``hold:no-dispatch``; removing the tag returns it to dispatch.
* **Mint** through :func:`precis.plans.templates.mint_gripe_fix_direct`
  (unpinned: the build picks its lane at dispatch, devin first by table
  order) and one ``dispatch:`` comment on the gripe naming the root. The
  ``related-to`` link the template writes is the idempotency marker: a
  re-fire sees the gripe linked and skips it, so a doubled tick converges.

The pass bills no model call and holds no transaction across the mint
(each mint is the template's own transaction).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from precis import settings as psettings
from precis.corpus_layout import host_name
from precis.plans import lanes as _lanes
from precis.plans import route as _route
from precis.plans.nodes import finish_mint
from precis.plans.templates import mint_gripe_fix_direct
from precis.principal import worker_comment_meta
from precis.store import Store
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES
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
#: Registry key of the filer-trust switch (:func:`gripe_trust`).
TRUST_KEY = "factory.gripe_trust"
#: ``factory.gripe_trust`` values: ``all`` skips the filer check,
#: ``filer`` keeps the Claude Code / human / ``ci.sig`` rule.
TRUST_ALL = "all"
TRUST_FILER = "filer"


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
    """Summed cap of the enabled lanes that fit the build's task class."""
    table = _lanes.load_lanes(store) if lanes is None else lanes
    return sum(ln.cap for ln in table if ln.enabled and ln.fits(_BUILD_TASK_CLASS))


def open_chain_count(store: Store) -> int:
    """Live plan roots ``related-to`` a gripe still holding a build slot.

    A chain holds a slot only while its build is unlanded: an ``op``
    node of a :data:`_BUILD_OPS` type under the root (every plan node
    carries the ``plan:<root>`` tag) whose ``meta.task_class`` resolves
    to :data:`_BUILD_TASK_CLASS` — unset reads ``code``, as
    ``plan_dispatch`` resolves it — and whose STATUS is not terminal.
    Once the build has landed the chain tail (gate, validate, review)
    may run on for hours without holding a lane slot (gr584410)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(DISTINCT r.ref_id)
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
               AND EXISTS (SELECT 1
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
            """,
            (
                list(_TERMINAL_STATUSES),
                list(_BUILD_OPS),
                _BUILD_TASK_CLASS,
                _BUILD_TASK_CLASS,
                sorted(TERMINAL_TODO_STATUSES),
            ),
        ).fetchone()
    return int(row[0]) if row else 0


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


def run_gripe_dispatch_pass(store: Store, *, host: str | None = None) -> BatchResult:
    """One fire: free slots → candidates → one chain each. ``claimed`` is
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
    capacity = lane_capacity(store)
    open_chains = open_chain_count(store)
    slots = max(capacity - open_chains, 0)
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
        *_candidate_picks(store, slots, supported_projects=True, waiver=False),
    ]
    extra: dict[str, Any] = {
        "lane_capacity": capacity,
        "open_chains": open_chains,
        "slots": slots,
        "waivers": [w.gripe_id for w in waivers],
        "minted": [],
    }
    ok = failed = 0
    deny = _lanes.deny_paths_for(store)
    for c in picks:
        # gr645163: work no builder lane may write — the report names a
        # deny path, or the fix is prod-graph only — skips the chain and
        # files the one coordinator ask with the report attached. A
        # classifier blip must not lose the mint, so it fails open.
        report = ""
        try:
            report = _route_report(store, c)
            routed = _route.coordinator_route(report, deny)
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
    log.info(
        "gripe_dispatch: capacity=%d open=%d slots=%d minted=%d failed=%d",
        capacity,
        open_chains,
        slots,
        ok,
        failed,
    )
    return BatchResult(
        handler=SERVICE_NAME, claimed=len(picks), ok=ok, failed=failed, extra=extra
    )


def _route_report(store: Store, pick: DispatchPick) -> str:
    """The text the mint-time coordinator route judges for a gripe
    (gr645163): title plus every chunk — the same material
    ``mint_gripe_fix_direct`` embeds in the build body."""
    parts = [
        str(c.text) for c in store.chunks.list_chunks_for_ref(pick.gripe_id) if c.text
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
    "SERVICE_NAME",
    "TRUST_KEY",
    "WAIVER_TAG_PREFIX",
    "DispatchPick",
    "candidates",
    "gripe_dispatch_enabled",
    "gripe_trust",
    "lane_capacity",
    "open_chain_count",
    "run_gripe_dispatch_pass",
]
