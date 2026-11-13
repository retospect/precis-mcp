"""Lanes — the scheduler slots ``run_slice`` launches a slice on.

Spec: ``docs/backlog/dark-factory.md`` § "Lanes: capacity shapes". A lane
is an adapter plus a credential pool plus a concurrency cap (Reto
2026-10-30, td541982). Model, effort and temperature are per-job choices
inside a lane; the lane itself is a field on the job (``meta.lane``),
never prose on a todo. The lane pins the model ids its adapter accepts
(``Lane.models``, cheapest first — the list a generator's brief names so
the pick lands in ``meta.op.params.model``, gr549046) and the model an
unpinned job gets (``Lane.default_model``: the cheapest of ``models``
that can still write code, so an unpinned child never drifts to the
frontier pin).

The table derives from ``llm_account`` rows at call time (td568401 S1c:
:func:`load_lanes` → :func:`derive_lanes`; ``lane_rank`` carries the
preference order the code seed held by position). ``devin``, ``codex``
and ``claude-sub`` are enabled (Reto 2026-11-03: "ok both devin and codex
are ok. Lets make them go."; devin on once its CLI login on the slice
host was confirmed, 2026-11-06, td562568). ``kimi`` derives a lane since
td573681 (``kimi_tree`` is launchable) but stays disabled until an
operator lights the row; ``metered`` stays an account row — an account on
an adapter the builder cannot launch (``api``;
:data:`LAUNCHABLE_ADAPTERS`) never becomes a lane, so enabling
an api-adapter account can never mint a lane a submit would die on at
``get_adapter``. :func:`pick_lane` never returns a disabled lane.

Pick (:func:`pick_lane`): fit for the task class first, then the
cheapest fit lane under its cap — cost class (:attr:`Lane.cost_class`,
free before subscription before metered, gr573563: free compute first)
before the fewest live ``run_slice`` jobs carrying its ``meta.lane``.
With ``paid_ok=False`` a fit free lane holds the paid lanes out of the
pick entirely — a free lane at its cap is a wait, not a paid launch.
Pacing (the lane most behind its window target) replaces the live-count
order once the registry carries the window numbers.

Backoff (:func:`lane_backoffs`): a launch the lane's host refuses (the
adapter preflight — binary, login, trust seed) stamps
``meta.host_refusal = {lane, until, why}`` on the failed job and backs the
lane off :data:`HOST_REFUSAL_BACKOFF` (gr571395). A lane is backed off
while its newest ``run_slice`` job carries a refusal ``until`` in the
future; ``pick_lane`` skips it, the first submit after the window is the
probe, and a probe job without the mark clears the lane by being newer.
A broken host then costs one failed probe per window instead of a
retry-cap ask per todo.

Retry (:func:`ladder`): the table in order is also the escalation chain
``plan_dispatch`` climbs when a slice fails — each enabled fit lane's
models from its default upward, free vendor lanes before the Claude
subscription (Reto td559457).

Park (gr588221): the account row's ``parked_reason`` set holds the lane
out of every pick — ``pick_lane``, ``ladder`` and a ``params.lane`` pin
skip it exactly like ``enabled=False`` — while ``enabled`` stays the
human switch no writer here touches. A run_slice auth failure parks the
lane itself (:func:`park_lane`); the hourly ``lane_health`` probe's
green result unparks it (:func:`unpark_lane`) — the lane self-heals.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

#: Matches every task class in :attr:`Lane.task_classes`.
ANY_CLASS = "*"

#: The job type whose live rows count against a lane's cap.
RUN_SLICE = "run_slice"

#: Prefix of the ``wake_when.payload.why`` a lane-full park writes (gr611299).
#: :func:`wake_lane_waiter` requires it, so a job that ran and was later
#: parked for another reason (a quota window) is never woken as a lane wait.
LANE_PARK_WHY = "lane-wait: "

#: The cost classes a lane falls in — what :func:`pick_lane` minimises
#: before the live count (gr573563: free builder capacity first, paid
#: only once the free wait has run out, td573580).
COST_FREE = "free"
COST_SUBSCRIPTION = "subscription"
COST_METERED = "metered"
COST_ORDER: tuple[str, ...] = (COST_FREE, COST_SUBSCRIPTION, COST_METERED)

#: How long a lane stays out of the fit set after its host refused a
#: launch (gr571395): long enough that a broken host costs one failed
#: probe per window, short enough that a fixed login is picked up the
#: same hour.
HOST_REFUSAL_BACKOFF = timedelta(minutes=30)

#: Job statuses that no longer hold a lane slot (mirrors
#: ``precis.workers.executors._common.TERMINAL``; restated so this
#: package stays below the workers layer).
_TERMINAL_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled")

#: Repo paths a slice session may not write (spec § "7. Alerts as slices":
#: a fixer never touches the rule that fired, the factory never lands its
#: own controller; Reto's ruling on td542236). Posix globs, repo-relative;
#: ``**`` spans directories. ``run_slice`` passes them to the session as
#: deny rules and fails the attempt when the branch touches one anyway.
FACTORY_DENY_WRITE_PATHS: tuple[str, ...] = (
    # the ship harness
    "scripts/ship",
    "scripts/test",
    "scripts/lib/**",
    ".github/**",
    # the factory's own modules
    "src/precis/plans/**",
    "src/precis/workers/job_types/run_slice.py",
    "src/precis/workers/job_types/plan_dispatch.py",
    "src/precis/workers/job_types/gripe_validate.py",
    # the dark release pass: the approval guard, the ops and the script
    # (named singly although plans/** already spans two of them: the
    # release path must stay denied if the glob above is ever narrowed)
    "src/precis/plans/release.py",
    "src/precis/plans/lanes.py",
    "src/precis/workers/job_types/release_cut.py",
    "src/precis/workers/job_types/ci_verdict_poll.py",
    "src/precis/workers/job_types/ci_red_digest.py",
    "src/precis/workers/job_types/ci_rerun.py",
    "src/precis/workers/job_types/release_poll.py",
    "src/precis/workers/job_types/release_deploy.py",
    "src/precis/workers/job_types/release_confirm.py",
    "src/precis/workers/job_types/land_slice.py",
    "scripts/release",
    # fires a CI rerun (a write) — denied like scripts/release
    "scripts/ci-rerun",
    # alert rules, detectors and monitors
    "src/precis/alert_ids.py",
    "src/precis/alerts.py",
    "src/precis/workers/nursery.py",
    "deploy/roles/monitoring/**",
)

#: Repo paths only a human may write (td574937 E1, Reto: "I am happy taking
#: the risk, we have backups") — secret-placing deploy files, inventories
#: and vault material, and the harness's own rules. Never relaxable by a
#: setting or a lane field: when ``factory.deny_relaxed`` shrinks the deny
#: set these stay denied, and a slice touching one still fails the
#: attempt. Same glob syntax as :data:`FACTORY_DENY_WRITE_PATHS`, but the
#: match folds case (:func:`human_only_hits`).
HUMAN_ONLY_PATHS: tuple[str, ...] = (
    # inventories and vault material
    "deploy/inventory*/**",
    "deploy/*vault*",
    "deploy/*group_vars*",
    "deploy/*host_vars*",
    "deploy/ansible.cfg",
    "deploy/group_vars/all.yml",
    # hardening and the MCP edge
    "deploy/roles/hardening/**",
    "deploy/playbooks/10-hardening.yml",
    "deploy/mcp-http/**",
    # playbooks that place credentials or slice tokens
    "deploy/playbooks/20b-precis-worker-collapsed.yml",
    "deploy/playbooks/20c-precis-heartbeat-serving.yml",
    "deploy/playbooks/20f-precis-worker-oasnap.yml",
    "deploy/playbooks/20g-precis-worker-slices.yml",
    "deploy/playbooks/22-papers-sync.yml",
    "deploy/run-*.yml",
    # secret-placing role files — file-level globs; a role is named whole
    # only when every file in it places a credential
    "deploy/roles/asa_bot/templates/claude_mcp.json.j2",
    "deploy/roles/asa_bot/templates/claude_settings.json.j2",
    "deploy/roles/asa_bot/templates/com.asa.bot.plist.j2",
    "deploy/roles/asa_bot/templates/config.yaml.j2",
    "deploy/roles/asa_slack/templates/com.asa.slack.plist.j2",
    "deploy/roles/asa_slack/templates/config.yaml.j2",
    "deploy/roles/backups/templates/b2_sync.sh.j2",
    "deploy/roles/db_exporters/defaults/main.yml",
    "deploy/roles/db_exporters/tasks/main.yml",
    "deploy/roles/db_exporters/templates/db-exporters.env.j2",
    "deploy/roles/db_exporters/templates/launcher.sh.j2",
    "deploy/roles/extract_watch/templates/extract-watch.plist.j2",
    "deploy/roles/monitoring/defaults/main.yml",
    "deploy/roles/monitoring/templates/precis-alertmanager-receiver.service.j2",
    "deploy/roles/nginx/tasks/main.yml",
    "deploy/roles/pgbouncer/templates/pgbouncer.ini.j2",
    "deploy/roles/pgbouncer/templates/userlist.txt.j2",
    "deploy/roles/pgpass/templates/pgpass.j2",
    "deploy/roles/postgres/tasks/main.yml",
    "deploy/roles/postgres/templates/channel_prompts.sql.j2",
    "deploy/roles/postgres/templates/pg_hba.conf.j2",
    "deploy/roles/precis_embedder/templates/precis-embedder.plist.j2",
    "deploy/roles/precis_embedder/templates/precis-embedder.service.j2",
    "deploy/roles/precis_watch/defaults/main.yml",
    "deploy/roles/precis_watch/templates/precis-watch.plist.j2",
    "deploy/roles/precis_watch/templates/precis-watch.service.j2",
    "deploy/roles/precis_web/defaults/main.yml",
    "deploy/roles/precis_web/tasks/main.yml",
    "deploy/roles/precis_web/tasks/migrate.yml",
    "deploy/roles/precis_web/tasks/public-mcp.yml",
    "deploy/roles/precis_web/templates/precis-web.plist.j2",
    "deploy/roles/precis_worker/tasks/provision.yml",
    "deploy/roles/precis_worker/templates/precis-worker.plist.j2",
    "deploy/roles/precis_worker/templates/precis-worker.service.j2",
    "deploy/roles/precis_worker_agent/tasks/provision.yml",
    "deploy/roles/precis_worker_agent/templates/com.precis.colima.plist.j2",
    "deploy/roles/precis_worker_agent/templates/precis-worker-agent.plist.j2",
    "deploy/roles/precis_worker_agent/templates/precis-worker-agent.service.j2",
    "deploy/roles/service_unit/templates/launchd.plist.j2",
    "deploy/roles/service_unit/templates/systemd.service.j2",
    "deploy/roles/tailscale/tasks/main.yml",
    "deploy/roles/tcc_profile/**",
    "deploy/roles/users/**",
    # the harness's own rules — every agent surface's, plus rule files
    # nested at any depth (fnmatch ``*`` spans ``/``; a ``*/`` prefix
    # still wants one directory, so nested .claude trees and
    # CLAUDE.local.md take their own globs, td574960)
    ".claude/**",
    "*/.claude/**",
    ".codex/**",
    ".devin/**",
    ".kimi-code/**",
    ".mcp.json",
    "*/CLAUDE.md",
    "*/AGENTS.md",
    "CLAUDE.local.md",
    "*/CLAUDE.local.md",
    "scripts/hooks/**",
    "CLAUDE.md",
    "AGENTS.md",
)

#: :data:`HUMAN_ONLY_PATHS` lowercased once — the human-only match folds
#: case (:func:`human_only_hits`) because the slice hosts' filesystems
#: do (macOS): a case-mangled write lands on the same file.
_HUMAN_ONLY_LOWER: tuple[str, ...] = tuple(g.lower() for g in HUMAN_ONLY_PATHS)

#: What stays denied alongside :data:`HUMAN_ONLY_PATHS` while the
#: security-review hold (td574934 A3) is undeployed — the slice that
#: deploys it empties this tuple. Reto's ruling (td574937, 2026-11-08):
#: the engine python is factory-buildable now — the review-before-cut
#: gate (``release_poll`` refuses to cut while an engine land's reviewer
#: verdict is FIX or missing) is the guard. What a builder must never
#: edit is its own fence and the ship/test/deploy harness: this module,
#: ``scripts/ship`` and the helpers it invokes from the slice tree, and
#: the deploy/round verbs.
INTERIM_DENY: tuple[str, ...] = (
    "src/precis/plans/lanes.py",
    "scripts/ship",
    "scripts/test",
    # the helpers ship invokes from the SLICE tree — a slice could
    # otherwise empty its own --checked forcing through them
    "scripts/impacted-tests",
    "scripts/migration-check",
    "scripts/mutate-diff",
    "scripts/gen-devin-hooks",
    # the deploy script and the round verbs stay builder-proof too
    "scripts/deploy",
    "scripts/round",
    "scripts/lib/**",
    ".github/**",
)

#: The deny globs that remain engine-buildable once the list relaxes —
#: the deduped union of :data:`FACTORY_DENY_WRITE_PATHS` and
#: :data:`INTERIM_DENY` minus :data:`HUMAN_ONLY_PATHS`. INTERIM entries
#: outside FACTORY (``scripts/impacted-tests`` and friends) must still
#: stamp a land engine, so the union is the stamp set; a human-only file
#: sitting under an engine glob (``deploy/roles/monitoring/**``) still
#: escapes via :func:`engine_hits`' human-only filter.
ENGINE_PATHS: tuple[str, ...] = tuple(
    dict.fromkeys(
        g for g in FACTORY_DENY_WRITE_PATHS + INTERIM_DENY if g not in HUMAN_ONLY_PATHS
    )
)

#: :data:`ENGINE_PATHS` lowercased — the stamp match folds case like the
#: human-only one: a case-mangled write is the same file.
_ENGINE_PATHS_LOWER: tuple[str, ...] = tuple(g.lower() for g in ENGINE_PATHS)

#: The deny-write set when ``factory.deny_relaxed`` is off, unreadable or
#: absent — the full factory list plus the never-relaxable human-only
#: set (td574960: the session's deny text, the post-check and the land
#: check name the same set in every mode).
STRICT_DENY: tuple[str, ...] = tuple(
    dict.fromkeys(FACTORY_DENY_WRITE_PATHS + HUMAN_ONLY_PATHS)
)


def human_only_hits(paths: Iterable[str]) -> list[str]:
    """The ``paths`` touching a :data:`HUMAN_ONLY_PATHS` glob, matched
    case-insensitively — the slice hosts' filesystems fold case, so a
    ``Deploy/Inventory`` write lands on ``deploy/inventory``."""
    return sorted(
        {
            p
            for p in paths
            if any(fnmatch.fnmatchcase(p.lower(), g) for g in _HUMAN_ONLY_LOWER)
        }
    )


def engine_hits(paths: Iterable[str]) -> list[str]:
    """The ``paths`` touching an :data:`ENGINE_PATHS` glob, human-only
    winning: a path matching both an engine and a
    :data:`HUMAN_ONLY_PATHS` glob (the two credential files under
    ``deploy/roles/monitoring/**``) does not count as engine."""
    return sorted(
        {
            p
            for p in paths
            if any(fnmatch.fnmatchcase(p.lower(), g) for g in _ENGINE_PATHS_LOWER)
            and not any(fnmatch.fnmatchcase(p.lower(), g) for g in _HUMAN_ONLY_LOWER)
        }
    )


def deny_paths_for(store: Any) -> tuple[str, ...]:
    """The deny-write glob set for a slice, resolved at call time.

    ``store is None``, an unreadable store or the row absent/off all
    return :data:`STRICT_DENY` — the full factory list plus the
    never-relaxable human-only set; nothing turns on by default and the
    coordinator flips ``factory.deny_relaxed`` after this lands. The
    stored ``app_settings`` row is the ONLY input — the registry default
    and any env override are ignored, so a slice editing
    ``src/precis/settings.py`` cannot relax its own deny list. Relaxed,
    the set is :data:`HUMAN_ONLY_PATHS` ∪ :data:`INTERIM_DENY` —
    and INTERIM_DENY now holds only the fence itself (this module) and
    the ship/test/deploy harness; the engine python relaxed out of it
    rides the review-before-cut gate instead (td574937).
    """
    if store is None:
        return STRICT_DENY
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                ("factory.deny_relaxed",),
            ).fetchone()
    except Exception:
        log.warning("lanes.deny_paths_for: setting unreadable; strict deny list")
        return STRICT_DENY
    if row is not None and str(row[0]).strip().lower() in {"1", "true", "yes", "on"}:
        return HUMAN_ONLY_PATHS + INTERIM_DENY
    return STRICT_DENY


#: The repo a slice builds in when neither its todo nor an ancestor
#: carries a ``repo:`` tag.
DEFAULT_REPO = "precis"

#: Repos ``run_slice`` has a build profile for (worktree root, gates,
#: lander). A ``repo:`` value outside this set is refused however the
#: setting reads: without a profile the slice would be built in a precis
#: worktree (td659522 step 0; the catpath profile joins in step 1).
KNOWN_REPOS: tuple[str, ...] = (DEFAULT_REPO,)

_REPO_TAG = "repo:"


def repo_tags(tags: Iterable[Any]) -> list[str]:
    """The ``repo:<name>`` values among ``tags`` (tag objects or their
    string form), in order, deduplicated."""
    names = [
        str(t)[len(_REPO_TAG) :].strip() for t in tags if str(t).startswith(_REPO_TAG)
    ]
    return list(dict.fromkeys(names))


def open_repos(store: Any) -> frozenset[str]:
    """The repos the factory may build in, resolved at call time:
    :data:`DEFAULT_REPO` always, plus each :data:`KNOWN_REPOS` name the
    DB-only setting ``factory.open_repos`` lists (comma-separated).

    ``store is None``, an unreadable store or the row absent all return
    the default alone. Like :func:`deny_paths_for`, the stored
    ``app_settings`` row is the ONLY input, so a slice editing
    ``src/precis/settings.py`` cannot open a repo for itself.
    """
    default = frozenset({DEFAULT_REPO})
    if store is None:
        return default
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                ("factory.open_repos",),
            ).fetchone()
    except Exception:
        log.warning("lanes.open_repos: setting unreadable; default repo only")
        return default
    if row is None:
        return default
    listed = {part.strip() for part in str(row[0]).split(",")}
    return default | (listed & set(KNOWN_REPOS))


def repo_refusal(repos: Iterable[str], store: Any) -> str | None:
    """Why a slice whose todo resolves to the ``repo:`` values ``repos``
    may not be built, or ``None`` when it may.

    No tag or ``repo:precis`` builds here. Anything else fails closed:
    more than one value, a value without a build profile
    (:data:`KNOWN_REPOS`), or a known repo ``factory.open_repos`` does
    not list.
    """
    names = list(dict.fromkeys(repos))
    if not names or names == [DEFAULT_REPO]:
        return None
    if len(names) > 1:
        return (
            f"the todo carries {len(names)} repo: tags "
            f"({', '.join(names)}); next: split it into one todo per repo"
        )
    name = names[0]
    if name not in KNOWN_REPOS:
        return (
            f"repo:{name} has no factory build profile "
            f"(known: {', '.join(KNOWN_REPOS)}); next: build it by hand in that "
            "repo, or retag the todo"
        )
    if name not in open_repos(store):
        return (
            f"repo:{name} is not opened for the factory; next: list it in "
            "the DB setting factory.open_repos"
        )
    return None


@dataclass(frozen=True)
class Lane:
    """One scheduler slot: adapter + credential pool + concurrency cap.

    ``adapter`` names how a slice is launched (``claude_p`` is
    ``claude -p`` in a worktree; ``codex_exec``, ``kimi_tree`` and
    ``devin_tree`` are the vendor CLIs; ``api`` is declared, not built).
    ``task_classes``
    is the fit set; :data:`ANY_CLASS` fits every class. An MCP-less lane
    takes ``code``, plus ``review`` and ``validate`` whose one verdict rides
    the session's final message (``handoff: review: …``, ``validated: …``;
    td564019 cut 1); the templates class every node that mints or posts
    through the precis MCP by its step (``triage``, ``brief``, ``pcb``;
    :mod:`precis.plans.templates`) so it never lands on such a lane.
    ``deny_write_paths`` are the repo globs a slice on this lane may not
    write (:data:`STRICT_DENY` for every seed lane).
    ``vendor`` is the model vendor behind the lane (``''`` = unknown); a
    review is picked with ``exclude_vendor`` = the builder lane's vendor.
    ``models`` are the model ids the lane's adapter accepts, cheapest
    first (empty while the adapter is unbuilt). ``default_model`` is what
    an unpinned ``run_slice`` job on this lane gets; ``None`` falls to
    the router's frontier pin. ``model_prefix`` widens :meth:`accepts_model`
    past ``models``: the ``claude -p`` CLI takes any ``claude-`` id, so a
    triage pin like ``claude-sonnet-5-5`` still routes to the Claude lane
    (``''`` = ``models`` is exhaustive; a lane with neither accepts no pin,
    so a pinned model never lands on a lane that cannot run it).
    ``cost_class`` is what a launch on the lane costs (one of
    :data:`COST_ORDER`, cheapest first); :func:`pick_lane` spends free
    capacity before paid.
    ``parked_reason`` is the lane-health park (gr588221): set, the lane
    is held out of dispatch like ``enabled=False`` without touching the
    human ``enabled`` switch; ``parked_at`` the park's first sighting.
    """

    name: str
    adapter: str
    credential_pool: str
    cap: int
    task_classes: frozenset[str]
    enabled: bool = True
    deny_write_paths: tuple[str, ...] = STRICT_DENY
    vendor: str = ""
    models: tuple[str, ...] = ()
    default_model: str | None = None
    model_prefix: str = ""
    cost_class: str = COST_SUBSCRIPTION
    parked_reason: str | None = None
    parked_at: datetime | None = None

    @property
    def parked(self) -> bool:
        """``True`` while the lane-health park holds this lane out of
        dispatch — skipped like ``enabled=False``, the human switch
        untouched."""
        return self.parked_reason is not None

    def fits(self, task_class: str) -> bool:
        """``True`` when this lane takes ``task_class`` work."""
        return ANY_CLASS in self.task_classes or task_class in self.task_classes

    def accepts_model(self, model: str) -> bool:
        """``True`` when a slice pinned to ``model`` may launch here: the id
        is in ``models``, or starts with ``model_prefix``."""
        if model in self.models:
            return True
        return bool(self.model_prefix) and model.startswith(self.model_prefix)


#: ``model_prefix`` per adapter — the one fact the code seed carried that
#: no ``llm_account`` column holds: ``claude -p`` accepts every
#: ``claude-`` id, so a triage pin outside ``models`` still routes to the
#: lane.
_ADAPTER_MODEL_PREFIX: dict[str, str] = {"claude_p": "claude-"}

#: Adapters a ``run_slice`` submit can launch — mirrors
#: ``precis.workers.job_types._slice_adapters.ADAPTERS`` (restated so
#: this package stays below the workers layer, like
#: ``_TERMINAL_STATUSES``; a drift test pins the two equal). An account
#: on any other adapter — ``api`` (metered) — derives to no lane,
#: whatever its ``enabled`` flag.
LAUNCHABLE_ADAPTERS: frozenset[str] = frozenset(
    {"claude_p", "codex_exec", "devin_tree", "kimi_tree"}
)


def get_lane(name: str, store: Any) -> Lane | None:
    """The account-derived lane called ``name``, or ``None``."""
    for lane in load_lanes(store):
        if lane.name == name:
            return lane
    return None


def _cost_class(a: Any) -> str:
    """The lane's cost class from the account row (gr573563): a
    ``free-promo`` plan is free while ``promo_expiry`` is unset or not
    past, ``metered`` is metered, everything else — an expired promo
    included — is subscription."""
    if a.plan == "free-promo" and (
        a.promo_expiry is None or a.promo_expiry >= datetime.now(UTC).date()
    ):
        return COST_FREE
    if a.plan == "metered":
        return COST_METERED
    return COST_SUBSCRIPTION


def derive_lanes(
    rows: Iterable[Any], *, deny_paths: tuple[str, ...] = STRICT_DENY
) -> tuple[Lane, ...]:
    """The lane table from ``llm_account`` rows (td568401 S1c).

    Order is ``(lane_rank, name)`` — the column carries the preference
    order the seed tuple held by position (:func:`list_accounts` is
    name-ordered), which :func:`pick_lane` ties and :func:`ladder` keep.
    ``concurrency`` is the cap, ``plan`` the credential_pool and, with
    ``promo_expiry``, the lane's :func:`_cost_class`, ``task_classes``
    the fit set (``*`` = :data:`ANY_CLASS`), ``model_prefix`` the
    adapter's (:data:`_ADAPTER_MODEL_PREFIX`), ``deny_paths`` the
    resolved deny set (:func:`load_lanes` passes
    :func:`deny_paths_for`'s).
    Rows on an adapter outside :data:`LAUNCHABLE_ADAPTERS` derive to
    nothing — an enabled api-adapter account is never a builder lane."""
    return tuple(
        Lane(
            name=a.name,
            adapter=a.adapter,
            credential_pool=a.plan,
            cap=a.concurrency,
            task_classes=frozenset(a.task_classes),
            enabled=a.enabled,
            deny_write_paths=deny_paths,
            vendor=a.vendor,
            models=tuple(a.models),
            default_model=a.default_model,
            model_prefix=_ADAPTER_MODEL_PREFIX.get(a.adapter, ""),
            cost_class=_cost_class(a),
            parked_reason=getattr(a, "parked_reason", None),
            parked_at=getattr(a, "parked_at", None),
        )
        for a in sorted(rows, key=lambda a: (a.lane_rank, a.name))
        if a.adapter in LAUNCHABLE_ADAPTERS
    )


def load_lanes(store: Any) -> tuple[Lane, ...]:
    """The lane table read from ``llm_account`` at call time.

    The accounts module is imported lazily: ``precis.utils.llm`` is the
    routing layer and pulling it in at module top would sit the whole
    router under every caller of this leaf. ``accounts.list_accounts``
    goes through the module attribute so tests patch the seam instead
    of needing a DB. ``deny_write_paths`` is :func:`deny_paths_for`'s
    resolved set."""
    from precis.utils.llm import accounts

    return derive_lanes(accounts.list_accounts(store), deny_paths=deny_paths_for(store))


def live_lane_counts(store: Any) -> dict[str, int]:
    """``{lane name: live run_slice jobs carrying it}`` from the DB.

    Live = a ``run_slice`` job, not retired, with ``meta.lane`` set and no
    terminal ``STATUS``. A job stamps ``meta.lane`` at submit, so a job
    still queued with no lane yet does not hold a slot.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.meta->>'lane', count(*)
              FROM refs r
             WHERE r.kind = 'job'
               AND r.retired_at IS NULL
               AND r.meta->>'job_type' = %s
               AND r.meta ? 'lane'
               AND NOT EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = ANY(%s)
                   )
             GROUP BY 1
            """,
            (RUN_SLICE, list(_TERMINAL_STATUSES)),
        ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows if r[0] is not None}


def lane_backoffs(store: Any, now: datetime | None = None) -> dict[str, datetime]:
    """``{lane name: backoff end}`` for lanes backed off by a host refusal.

    A lane is backed off while its NEWEST ``run_slice`` job (by ref id,
    per ``meta.lane``) carries ``meta.host_refusal.until`` in the future
    (gr571395): a later job on the lane without the mark — a probe that
    launched — clears the backoff by being newer, so no second write
    path is needed.
    """
    now = now or datetime.now(UTC)
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (r.meta->>'lane')
                   r.meta->>'lane', r.meta->'host_refusal'->>'until'
              FROM refs r
             WHERE r.kind = 'job'
               AND r.retired_at IS NULL
               AND r.meta->>'job_type' = %s
               AND r.meta ? 'lane'
             ORDER BY r.meta->>'lane', r.ref_id DESC
            """,
            (RUN_SLICE,),
        ).fetchall()
    out: dict[str, datetime] = {}
    for lane, raw in rows:
        if not raw:
            continue
        try:
            until = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        if until > now:
            out[str(lane)] = until
    return out


def missing_class_reason(lanes: Iterable[Lane], task_class: str) -> str | None:
    """A configuration gap, independent of capacity and temporary holds.

    A health-parked or backed-off lane still offers its configured classes;
    only an absent or disabled offering needs a coordinator to change it.
    """
    if any(lane.enabled and lane.fits(task_class) for lane in lanes):
        return None
    return (
        f"no enabled lane for class {task_class}; "
        "next: enable a lane or retarget the class"
    )


def freed_lane(conn: Any, ref_id: int) -> str | None:
    """The lane a still-live ``run_slice`` job holds a slot on, else ``None``.

    Read BEFORE the job's terminal ``STATUS`` write: a job already terminal
    holds no slot, so a second terminal write on it frees nothing and must
    not wake a second waiter (gr611299)."""
    row = conn.execute(
        """
        SELECT r.meta->>'lane'
          FROM refs r
         WHERE r.ref_id = %s
           AND r.kind = 'job'
           AND r.meta->>'job_type' = %s
           AND r.meta ? 'lane'
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'STATUS'
                    AND t.value = ANY(%s)
               )
        """,
        (ref_id, RUN_SLICE, list(_TERMINAL_STATUSES)),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def wake_lane_waiter(conn: Any, lane: str, now: datetime | None = None) -> int | None:
    """Wake the oldest ``run_slice`` job parked for ``lane``'s capacity.

    A slot just freed on ``lane`` (gr611299): the oldest (lowest ref id)
    ``waiting_time`` run_slice job whose ``meta.lane_wait`` names the lane
    and whose ``at_time`` wake is still in the future gets that wake pulled
    to ``now`` — the ``wake_runner`` re-queues it on its next pass instead of
    after :data:`~precis.workers.job_types.run_slice.LANE_FULL_BACKOFF`.
    ``meta.lane_woken`` marks the early wake so the job's next park, if it
    loses the race for the slot, does not count a full backoff of waiting.

    One UPDATE, one waiter per freed slot (``SKIP LOCKED`` so concurrent
    finishes wake different jobs, never a herd). A job already woken has
    ``ts <= now`` and is not picked again. Returns the woken job's ref id,
    or ``None`` when nothing is parked for the lane. Caller commits."""
    ts = int((now or datetime.now(UTC)).timestamp())
    row = conn.execute(
        """
        UPDATE refs SET meta = jsonb_set(
                   jsonb_set(meta, '{wake_when,payload,ts}', to_jsonb(%(ts)s::bigint)),
                   '{lane_woken}', 'true')
         WHERE ref_id = (
               SELECT r.ref_id
                 FROM refs r
                WHERE r.kind = 'job'
                  AND r.retired_at IS NULL
                  AND r.meta->>'job_type' = %(job_type)s
                  AND r.meta->'lane_wait' ? %(lane)s
                  AND r.meta->'wake_when'->'payload'->>'why' LIKE %(why)s
                  AND r.meta->'wake_when'->>'kind' = 'at_time'
                  AND (r.meta->'wake_when'->'payload'->>'ts')::bigint > %(ts)s
                  AND EXISTS (
                        SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                         WHERE rt.ref_id = r.ref_id
                           AND t.namespace = 'STATUS'
                           AND t.value = 'waiting_time'
                      )
                ORDER BY r.ref_id
                LIMIT 1
                  FOR UPDATE OF r SKIP LOCKED
           )
        RETURNING ref_id
        """,
        {"ts": ts, "job_type": RUN_SLICE, "lane": lane, "why": f"{LANE_PARK_WHY}%"},
    ).fetchone()
    return int(row[0]) if row else None


def pick_lane(
    store: Any,
    task_class: str,
    *,
    lanes: tuple[Lane, ...] | None = None,
    counts: Mapping[str, int] | None = None,
    exclude_vendor: str | None = None,
    model: str | None = None,
    backoff: Mapping[str, datetime] | None = None,
    paid_ok: bool = True,
) -> Lane | None:
    """The lane a ``task_class`` slice launches on, or ``None`` when every
    fit lane is full (or none fits).

    Fit first (enabled lanes whose ``task_classes`` take the class and,
    with ``model`` -- the slice's ``params.model`` pin -- whose
    :meth:`Lane.accepts_model` takes it: a triage-pinned claude id never
    lands on the codex lane, which cannot run it), then cost class --
    :data:`COST_ORDER` order, free before paid (gr573563) -- then the
    fewest live ``run_slice`` jobs carrying that ``meta.lane``, only
    lanes under their cap; ties keep table order. ``lanes`` defaults to
    the ``llm_account`` table read at call time (like :func:`ladder`),
    so a row edit is seen;
    ``counts`` skips the live-count DB read (tests, or a caller that
    already has them). Lanes
    backed off by a host refusal leave the fit set: ``backoff`` carries
    them explicitly, else they are read with :func:`lane_backoffs` — only
    when ``counts`` is ``None`` (the same DB rule as ``counts``, so a
    DB-free caller stays DB-free).

    ``paid_ok=False`` holds the paid lanes out of the pick while at
    least one fit free lane exists: ``None`` (a wait) when the free
    lanes are all at cap. Once every fit free lane is gone — filtered by
    a model pin it cannot run, backed off — the paid lanes stay in the
    set.

    ``exclude_vendor`` (the review rule: the reviewer never comes from the
    builder's vendor) drops fit lanes of that vendor. When no fit lane of
    another vendor is enabled the pick falls back to the full fit set --
    the caller sees ``lane.vendor == exclude_vendor`` and says so; once a
    second vendor's lane is enabled the rule holds (a full one is waited
    for, not bypassed).
    """
    table = load_lanes(store) if lanes is None else lanes
    fit = [
        lane
        for lane in table
        if lane.enabled and not lane.parked and lane.fits(task_class)
    ]
    if model:
        fit = [lane for lane in fit if lane.accepts_model(model)]
    backed = (
        {name for name, end in backoff.items() if end > datetime.now(UTC)}
        if backoff is not None
        else (set(lane_backoffs(store)) if counts is None else set())
    )
    if backed:
        fit = [lane for lane in fit if lane.name not in backed]
    if exclude_vendor:
        other = [lane for lane in fit if lane.vendor != exclude_vendor]
        fit = other or fit
    if not paid_ok and any(lane.cost_class == COST_FREE for lane in fit):
        fit = [lane for lane in fit if lane.cost_class == COST_FREE]
    if not fit:
        return None
    live = dict(counts) if counts is not None else live_lane_counts(store)
    open_lanes = [lane for lane in fit if live.get(lane.name, 0) < lane.cap]
    if not open_lanes:
        return None
    # ``min`` keeps the first of equal keys, so ties keep table order.
    return min(
        open_lanes,
        key=lambda lane: (COST_ORDER.index(lane.cost_class), live.get(lane.name, 0)),
    )


@dataclass(frozen=True)
class Rung:
    """One step of the retry ladder: a lane and the model pinned on it
    (``None`` = the lane declares no models; its adapter runs the
    account default)."""

    lane: str
    model: str | None


def ladder(
    task_class: str,
    *,
    store: Any = None,
    lanes: tuple[Lane, ...] | None = None,
) -> tuple[Rung, ...]:
    """The cross-lane retry ladder for ``task_class`` (Reto td559457: a
    failed slice climbs before the cap asks): every enabled fit lane in
    table order, each contributing its ``models`` from ``default_model``
    upward, or one model-less rung when it declares none. With today's
    table that is devin high → devin max → sonnet → opus (codex carries
    no ``code`` class — a lane that cannot run the test gate, gr573563).
    The table is ``lanes`` or the ``llm_account`` read — one of them is
    required."""
    if lanes is None:
        if store is None:
            raise TypeError("lanes.ladder: pass store or lanes")
        lanes = load_lanes(store)
    out: list[Rung] = []
    for lane in lanes:
        if not lane.enabled or lane.parked or not lane.fits(task_class):
            continue
        if not lane.models:
            out.append(Rung(lane.name, None))
            continue
        start = (
            lane.models.index(lane.default_model)
            if lane.default_model in lane.models
            else 0
        )
        out.extend(Rung(lane.name, m) for m in lane.models[start:])
    return tuple(out)


def rung_index(
    rungs: tuple[Rung, ...], lane: str | None, model: str | None
) -> int | None:
    """Where a run on ``lane`` with ``model`` sits on ``rungs``: the exact
    (lane, model) rung, else the lane's model-less rung, else the lane's
    first rung (a model the lane does not list, e.g. a resolver fallback),
    else ``None``."""
    if not lane:
        return None
    exact = next(
        (i for i, r in enumerate(rungs) if r.lane == lane and r.model == model), None
    )
    if exact is not None:
        return exact
    return next((i for i, r in enumerate(rungs) if r.lane == lane), None)


# ── lane-health writers (gr588221) ──────────────────────────────────
#
# These three write ONLY the health columns (0288) — ``enabled`` is the
# human switch and no writer in this module touches it.


def park_lane(store: Any, name: str, reason: str, now: datetime) -> bool:
    """Park the lane named ``name`` — ``False`` when no such account row.

    Stamps ``parked_reason`` (a short ``<class>:<key>`` mark — the probe's
    outcome prefix becomes ``health_outcome``), ``parked_at`` on first
    sighting only (a re-park keeps the original instant so a flap does not
    extend the >6h alert clock) and the probe stamp. Never writes
    ``enabled``.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "UPDATE llm_account SET parked_reason = %s, "
            "parked_at = COALESCE(parked_at, %s), "
            "health_checked_at = %s, health_outcome = %s, "
            "updated_at = now() WHERE name = %s RETURNING name",
            (reason, now, now, reason.split(":", 1)[0], name),
        ).fetchone()
    return row is not None


def unpark_lane(store: Any, name: str, now: datetime) -> bool:
    """Clear the park on the lane named ``name`` — ``False`` when no such
    account row. Records the green outcome; ``enabled`` is untouched."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "UPDATE llm_account SET parked_reason = NULL, parked_at = NULL, "
            "health_checked_at = %s, health_outcome = 'ok', "
            "updated_at = now() WHERE name = %s RETURNING name",
            (now, name),
        ).fetchone()
    return row is not None


def record_lane_health(store: Any, name: str, outcome: str, now: datetime) -> bool:
    """Stamp the probe's outcome on the lane named ``name`` — ``False``
    when no such account row. Moves the stamp only; the park state is
    untouched (a still-red lane stays parked)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "UPDATE llm_account SET health_checked_at = %s, "
            "health_outcome = %s, updated_at = now() "
            "WHERE name = %s RETURNING name",
            (now, outcome, name),
        ).fetchone()
    return row is not None


__all__ = [
    "ANY_CLASS",
    "COST_FREE",
    "COST_METERED",
    "COST_ORDER",
    "COST_SUBSCRIPTION",
    "ENGINE_PATHS",
    "FACTORY_DENY_WRITE_PATHS",
    "HOST_REFUSAL_BACKOFF",
    "HUMAN_ONLY_PATHS",
    "INTERIM_DENY",
    "LANE_PARK_WHY",
    "LAUNCHABLE_ADAPTERS",
    "RUN_SLICE",
    "STRICT_DENY",
    "Lane",
    "Rung",
    "deny_paths_for",
    "derive_lanes",
    "engine_hits",
    "freed_lane",
    "get_lane",
    "human_only_hits",
    "ladder",
    "lane_backoffs",
    "live_lane_counts",
    "load_lanes",
    "missing_class_reason",
    "park_lane",
    "pick_lane",
    "record_lane_health",
    "rung_index",
    "unpark_lane",
    "wake_lane_waiter",
]
