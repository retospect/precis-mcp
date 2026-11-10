"""``run_slice`` — launch one slice headless on a lane (dark factory step 6).

Spec: ``docs/backlog/dark-factory.md`` § "Next steps" 6, § "Lanes:
capacity shapes", § "Engine: ``claude -p`` or direct access?" and § "Job
lifecycle per task". The todo holds the work state (what, prio, lease,
loop comments); this job holds the run state. Tie it to its slice with
``link='todo:<id>', rel='builds'``.

Detached submit/poll pair under ``ssh_node`` (the protocol
:class:`precis.workers.job_types.JobTypeSpec` documents; pin the host
that holds the lane's credentials with ``params.target_node``):

* :func:`submit` renders the brief (todo title + body + the newest
  ``brief:``/``handoff:`` comment + the commit-and-hand-off instruction),
  picks the lane
  (:func:`precis.plans.lanes.pick_lane`) unless ``params.lane`` names one
  (a pinned lane at its cap parks the job like a full pick does) —
  ``params.lane_capability`` restricts the pick to lanes whose adapter
  declares it and re-picks a dead or incapable pin rather than failing —
  stamps ``meta.lane``/``meta.adapter``/``meta.model``/``meta.effort`` on
  the job, takes the lease on the todo (``claimed-by:lane-<lane>`` + ``STATUS:doing``),
  cuts worktree ``slice-td<id>`` on branch ``slice/<id>`` from
  ``origin/main`` under the checkout root (:func:`slice_root`), writes
  ``scratch/brief.md`` there and launches the lane's adapter (``claude_p``,
  ``codex_exec``, ``devin_tree`` or ``kimi_tree``) detached. Claude's argv
  and MCP grant are unchanged; the other vendors receive no precis MCP
  token. A
  ``devin_tree`` launch is gated by :func:`_devin_preflight` (the binary
  resolves, ``devin auth status`` reports a login, and the checkout's
  ``.claude/worktrees`` is seeded into Devin's trusted workspaces —
  gr570688): a refusal fails the job in seconds with the next step —
  a host problem (:class:`_HostRefusal`), so the todo's lease goes back
  clean with no ``attempt:`` burned — rather than burning a session that
  dies at spawn or stalls on the trust prompt. A ``kimi_tree`` launch is
  gated the same way (:func:`_kimi_preflight` + :func:`_kimi_launch_env`,
  td573681): the binary resolves and the ``KIMI_CODE_API_KEY`` membership
  key lands on the session env as a synthesized provider. The refusal also stamps
  ``meta.host_refusal`` on the failed job, backing the lane off
  :data:`precis.plans.lanes.HOST_REFUSAL_BACKOFF` in the pick, raising
  one deduped alert, and counting as no attempt at the retry cap
  (gr571395). A ``codex_exec`` launch grants its sandbox writable
  roots beyond the tree (:func:`_codex_writable_roots`): the shared git
  common dir — worktree index/refs, objects, and the gate queue the
  session's ``scripts/test`` writes — plus the Docker socket's parent
  (gr570533). The env is
  an allowlist (:func:`slice_env`): no DB DSN, no vault variables reach
  the session. The handle is ``{pid, pgid, worktree, log, exit_file,
  started_at}`` plus ``boot_id``/``start_time`` (the Linux process
  identity ssh_node's orphan reaper checks) when ``/proc`` has them.
  On Darwin it carries ``ps_identity`` (start time + user); polling
  requires a recorded identity to match, since a reused pid can pass
  signal 0. Legacy Darwin handles cannot use EPERM as evidence of liveness.
* :func:`poll` waits for the exit file (or a dead pid AND an emptied
  group — a TERM can kill the wrapping ``sh`` while a vendor process
  keeps the group open, td602769 — and an exit file past which the
  group still answers holds the poll while a bound applies, so a
  stop's escalation and an untripped bound run until the group is
  gone, td603296), checks the branch
  against the lane's ``deny_write_paths`` (:func:`deny_violations`),
  stamps the vendor's reported usage on ``meta.usage`` each pass —
  devin's ATIF ``--export`` file, live per turn; claude's result
  envelope; codex's ``turn.completed``. A job minted with
  ``params.budget_usd`` (the rung's remaining allowance,
  ``plan_dispatch._escalate``) is bound two ways (td601015, td601426):
  a :attr:`Adapter.budget_capable` lane — claude's
  ``--max-budget-usd`` — is enforced vendor-side at submit, a launch on
  any other adapter is refused outright (devin's paid ATIF carries
  tokens only, codex/kimi report no spend — the bound would read zero
  while real spend accrued), and the poll's watch SIGTERMs the group
  once live usage meets the bound, escalating to SIGKILL while the
  group still answers past the grace. A budget stop is
  always a failed attempt — even on a clean exit, it never mints a
  land;
  posts the session's last message as a ``handoff:`` comment on the todo,
  records the branch state (dirty count, commits ahead of
  ``origin/main``) in the job summary. On a clean exit (0, no claude
  error, no deny hit) with commits on the branch it mints one
  ``land_slice`` job (:mod:`precis.workers.job_types.land_slice`; idem key
  ``land_slice:td<id>``, a child of the todo, params ``{todo_id, worktree,
  branch, lane, target_node}``) and ends ``succeeded``; the lease stays
  until the land. A clean exit also finishes the ``waiting-for:mint``
  holds on the settled todos its job window minted under the plan
  (:func:`_release_mint_holds`, gr603945 — the mint barrier's hold,
  td603429, would otherwise idle every child a generator or planner
  session put for the 10-minute hygiene sweep). A clean exit that left
  no commit is also a success —
  no land — when the node's work is a document, not a branch
  (:func:`_no_commit_kind`: ``task_class`` off ``code``, or a
  ``_NO_COMMIT_ROLES`` generate role; gr570534): the todo closes done
  with no attempt burned, as a ``no-op:``/``validated:`` first-line
  verdict does on a code node. A code node's ``moot:`` claim — first
  line or anywhere in the result — must VERIFY before it may close
  (:func:`_verified_moot`, td584381 S1d): after a best-effort ``git
  fetch origin main`` it names a sha ``git merge-base --is-ancestor``
  confirms on ``origin/main`` AND whose own commit message (``git show
  -s --format=%B``) names the plan's gripe — its ``gr<N>`` or a
  ``td<M>`` of the plan's chain — as a whole token; a line naming a
  different ``gr<N>`` is rejected, and every ``tests/…​.py`` the plan
  gripe's text names must exist on main.
  Verified, the todo closes done tagged ``verdict:moot`` and the job
  carries ``meta.moot_verified = {sha, line}`` so the tick resolves the
  plan's gripe on that sha exactly as a land does. Unverified, the
  verdict still closes — the gates above only decide whether the gripe
  may be stamped ``resolved:`` (gr602896: a cross-plan fix names its
  own plan's td, never this one's gripe, so a true claim can never
  pass — jo602411's ``moot:`` read as a builder failure and escalated
  four artefacts): any ``no-op:``/``moot:``/``validated:`` line in the
  result or the newest comments closes the todo done, a ``moot:``
  tagged ``verdict:moot`` so the tick drops the plan and notes the
  verdict on the gripe — except a ``moot:`` pinned to a different
  ``gr<N>`` than the plan's, which is never this plan's verdict. Only
  a no-commit exit carrying no verdict anywhere is the failure below.
  The release-fix loop's fix
  node (``blocked-by`` a live ``ci_red_digest`` under a
  ``release-pass`` root) gets a weaker ruling INSTEAD
  (:func:`_fix_moot`, gr597599 — the r256 stall: the fix already on
  main had landed under another plan's subject, so the message gate
  above could never pass and the red loop re-dispatched until a human
  closed it): ``moot: <sha>`` needs only the ancestor check and
  ``verified: <ids> green on origin/main`` needs none; the node closes
  done untagged — the release plan itself is not moot — and the loop's
  next cut is the check a false claim fails. Otherwise (non-zero exit, a
  deny-path hit, a code node with no commit to land) it ends ``failed``
  with the log tail and releases the lease; a deny hit on a
  :data:`precis.plans.lanes.HUMAN_ONLY_PATHS` glob also stamps
  ``meta.human_only_hit`` on the job, so the tick asks for a human edit
  instead of retrying (td584381 S1a), and a code node that left no
  commit stamps ``meta.no_commit`` — one such attempt is the last, the
  tick sends it straight to rescue. Log text is secret-scrubbed
  before anything stores it (:func:`_child_env_secrets` +
  :func:`_redact`) — a session that echoes its env key must not leak it
  into the failure record or the handoff comment; a key value the poll
  cannot name (the credential unreadable at poll, or the submit-time
  value lost on a worker restart) drops the env-naming lines from the
  tail and withholds the raw handoff for a fixed marker. The
  claim tag goes, the todo returns to ``STATUS:open`` and gains
  ``attempt:<n>``.
* :func:`kill` SIGTERMs the session's process group. :func:`release`
  revokes the MCP grant and releases the todo even when the poison
  guard cannot safely signal a process (poll may never run).

Deny paths are enforced twice for Claude: the session gets them as ``Edit``/
``Write``/``MultiEdit``/``NotebookEdit`` deny rules through ``--settings``
(they hold under ``bypassPermissions``), and :func:`poll` re-checks the
branch diff and the working tree, which also catches a write made through
Bash. The coordinator verbs ride the same ``--settings`` as ``Bash``
denies (:data:`_slice_adapters.DENY_BASH`, gr574884: a builder ran
``scripts/round in <sha>`` itself — the round mark is the coordinator's,
and the brief says so too for the vendors no settings reach). Every
vendor receives the paths in its brief and gets the poll check. The
poll check is pre-land: the session only commits on its branch, and
nothing is landed until the check passed.

The land is the lander's: the brief ends with "commit on the branch, leave
the tree clean, stop", and ``land_slice`` runs ``scripts/ship``, whose
land_notify closes the todo. A generator node is the exception: its brief
ends with "create the child todos", no land job is minted, and the plan
tick closes it once its children are closed (it reopens one a session
marked done too early). A child-minting generator that exits 0 with no
children is a failed attempt (lease released), not a success.

Guard: ``PRECIS_FACTORY=off`` refuses every submit (:func:`validate_submit`)
and every launch (:func:`submit` re-checks). Subprocess, git and process
probes go through the module-level seams (:func:`_git`, :func:`_launch`,
:func:`_process_alive`, :func:`_signal_group`) so tests never spawn
claude or touch git.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert, resolve_alert_by_fingerprint
from precis.plans import lanes as _lanes
from precis.plans import release as _release
from precis.plans.nodes import MOOT_TAG, any_verdict, generator_verdict, node_kind
from precis.principal import worker_comment_meta
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.workers.job_types._slice_adapters import (
    ADAPTERS,
    PERMISSION_MODE,
    adapter_bin,
    deny_settings,
    devin_bin,
    get_adapter,
    kimi_bin,
)
from precis.workers.job_types._slice_adapters import (
    parse_claude_result as parse_result,
)

log = logging.getLogger(__name__)

NAME = "run_slice"

#: The todo's comment chunk kind (``handlers.todo._COMMENT_KIND``;
#: restated — workers never import handlers).
_COMMENT_KIND = "todo_comment"

#: Comment prefixes that carry a brief for the next session.
_BRIEF_PREFIXES: tuple[str, ...] = ("brief:", "handoff:")

#: Todo statuses that refuse a launch.
_CLOSED_STATUSES = frozenset({"done", "won't-do"})

_DEFAULT_MAX_TURNS = 300
_DEFAULT_WALL_S = 4 * 3600
_DEFAULT_TASK_CLASS = "code"
_DEFAULT_EFFORT = "high"

#: Park horizon when every fit lane is at its cap (gr561491). Slot
#: saturation frees when a sibling slice ends, minutes later; without an
#: instant ``ctx.defer`` falls to the executor's two-hour quota-window
#: backoff, which idled a triage 86 minutes behind two finished builds.
LANE_FULL_BACKOFF = timedelta(minutes=10)

#: Job-meta key marking a failure that burned no attempt, its value the
#: reason (``"lane-wait"`` here; ``land_slice`` writes ``"infra-land"``).
#: ``plan_dispatch._attempts`` skips a job carrying it (gr570187,
#: gr572118); the key is restated there — the dependency stays one-way.
ATTEMPT_FREE_KEY = "attempt_free"

#: Job-meta key listing the :data:`precis.plans.lanes.HUMAN_ONLY_PATHS`
#: globs the failed attempt's branch touched (td584381 S1a). A stamped
#: failure is a human call — no lane may write those paths — so the tick
#: asks at once instead of climbing the ladder; the key is restated in
#: ``plan_dispatch`` (the same one-way rule as ``ATTEMPT_FREE_KEY``).
HUMAN_ONLY_KEY = "human_only_hit"

#: Job-meta flag a clean-exit failure that left no commit on the slice
#: branch carries (td584381 S1a F4): one such attempt is the last —
#: no ladder rung teaches a session to write what it chose not to, so
#: the tick sends the node straight to rescue. Restated in
#: ``plan_dispatch`` (the same one-way rule).
NO_COMMIT_KEY = "no_commit"

#: Job-meta record ``poll`` stamps on a code task whose no-commit
#: ``moot:`` line it could VERIFY (``{"sha", "line"}``; :func:`_verified_moot`,
#: td584381 S1d): the tick resolves the plan's gripe on that sha exactly
#: as a land does. Restated in ``plan_dispatch`` (the same one-way rule).
MOOT_VERIFIED_KEY = "moot_verified"

#: Job-meta record ``poll`` stamps on the release-fix loop's fix node a
#: ``moot:``/``verified:`` marker let close with no commit
#: (:func:`_fix_moot`, gr597599): ``{"kind", "line"}`` plus ``"sha"``
#: for a moot — WHY a code node closed done with no land and no
#: ``verdict:moot`` tag (the release plan itself is not moot).
FIX_VERIFIED_KEY = "fix_verified"

#: Job-meta record ``poll`` stamps on a release-fix ``run_slice`` whose
#: clean exit carried a ``validated:`` first line and no commit
#: (:func:`_fix_norepro`, gr588028 — the r248 hand rerun): the red legs
#: did not reproduce, so the node parks on :data:`NOREPRO_WAIT_TAG` for
#: the tick's once-per-run-id rerun instead of burning the attempt into
#: rescue. Restated in ``plan_dispatch`` (the same one-way rule).
VALIDATED_NOREPRO_KEY = "validated_norepro"

#: The open tag that parks the fix node: a ``waiting-for:`` prefix is a
#: ``_DOABLE_EXCLUSION_TAGS`` exclusion, so the tick's dispatch step
#: leaves the node alone while its ``_step_ci_rerun`` owns it.
NOREPRO_WAIT_TAG = "waiting-for:ci-rerun"

#: Lane-wait parks before the wait is given up (~6 h at
#: :data:`LANE_FULL_BACKOFF` each). Lane waits count under
#: ``meta.wait_parks``, their own budget — NOT ``pause_parks``: the shared
#: ``MAX_PAUSE_PARKS`` cap is six, so a lane busy for an hour once read the
#: seventh park as a failed attempt on a job that never ran (gr572118;
#: the ``paper_reextract`` ``placement_parks`` shape).
LANE_WAIT_PARK_CAP = 36

#: The ``params.lane_capability`` values — :class:`Adapter` flags a mint
#: can require of its lane (:func:`factory_rescue._resolve_lane_pin`
#: stamps ``"mcp"`` when it cannot hold its pin, so a wait never relaxes
#: the requirement onto a lane with no write MCP; td610544).
_LANE_CAPABILITIES: frozenset[str] = frozenset({"mcp", "read_mcp", "budget_capable"})

#: ``raise_alert`` source of the lane host-refusal alert (gr571395),
#: bound to the registry: ``run_slice/lane-host-refusal:{lane}``.
_REFUSAL_ALERT_SOURCE = RULES_BY_ID["run_slice/lane-host-refusal:{lane}"].rule
#: Cap on the handoff comment and the failure tail.
_HANDOFF_CAP = 6000
#: Head kept when a handoff is clipped: a first-line verdict rides the
#: head (``moot:``, ``review:`` on the claude lane), a verdict glued to
#: the end of a devin transcript rides the tail (td573578).
_HANDOFF_HEAD = 1500
_TAIL_CAP = 2000

_ATTEMPT_RE = re.compile(r"^attempt:(\d+)$")

#: The marker the brief asks the builder to END its final message with
#: (gr573563): ``tests: ran`` or ``tests: NOT RUN — <which step, why>``.
#: The first word after ``tests:`` is the verdict — only ``ran`` is a
#: receipt the quick lane trusts (Reto on gr573555, td573580 review:
#: no receipt or a red one lands checked).
_TESTS_MARKER_RE = re.compile(r"\btests:\s*(\w+)", re.IGNORECASE)

#: Prefix of the ``_commit_for_session`` note meaning the worker — not the
#: builder — committed the tree, so the gate's test evidence is suspect
#: and the land runs checked (gr573563). The "worker commit
#: skipped/failed" notes deliberately do not start with it.
WORKER_COMMITTED = "worker committed"

#: Parent-env names the session inherits; everything else is dropped, so
#: no ``PRECIS_DB_*``/``PG*``/``DATABASE_URL`` DSN and no vault variable
#: reaches it. ``PRECIS_MCP_TOKEN`` is how the session reaches the precis
#: MCP (``.mcp.json``'s ``bearerTokenEnvVar``).
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TZ",
    "SSH_AUTH_SOCK",
    "PRECIS_MCP_TOKEN",
    "PRECIS_FACTORY",
    "PRECIS_CLAUDE_BIN",
    "CLAUDE_CONFIG_DIR",
    # The subscription login. A launchd worker has no shell hook to export
    # it, so :func:`slice_env` fills it from the vault when absent
    # (``utils/claude_oauth``); an env value wins, as everywhere.
    "CLAUDE_CODE_OAUTH_TOKEN",
    "PRECIS_RUN_SLICE_MODEL",
    "PRECIS_RUN_SLICE_MAX_TURNS",
    "PRECIS_RUN_SLICE_WALL_S",
    # How ``docker`` inside the session (the gate in ``scripts/test``)
    # finds the socket on the slice host — colima under the unit's HOME.
    "DOCKER_HOST",
)

#: Branch name an earlier attempt cut before the ``slice/<id>`` spelling.
_LEGACY_BRANCH = "slice-td{todo_id}"


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The slice: a todo with a brief.
        "todo_id": {"type": "integer"},
        # Lane name from precis.plans.lanes; unset → pick_lane.
        "lane": {"type": "string"},
        # A capability the lane's adapter must declare — one of
        # _LANE_CAPABILITIES ("mcp": the write-capable precis MCP a
        # diagnose/audit mint files through, gr604579/td610544). The pin
        # becomes a preference: a dead or incapable params.lane re-picks,
        # an unpinned pick sees offering lanes only and waits on one.
        "lane_capability": {"type": "string"},
        # Model id/alias; unset → PRECIS_RUN_SLICE_MODEL, else the lane's
        # default_model, else FRONTIER (Claude) or the account default.
        "model": {"type": "string"},
        # Recorded on meta.effort and exported as PRECIS_SLICE_EFFORT.
        "effort": {"type": "string"},
        # ssh_node's host pin (the host holding the lane's credentials).
        "target_node": {"type": "string"},
        # The PR head sha the intake classified (the 'land after fixes'
        # slice); the brief merges that sha. Informational for the job.
        "head_sha": {"type": "string"},
        # The node's remaining USD allowance — plan_dispatch._escalate
        # stamps cap minus recorded spend when climbing onto a rung_caps
        # rung; the launch is refused on an adapter with no spend
        # accounting, else the vendor/the poll kills the session once
        # its usage meets the bound (td601015, td601426).
        "budget_usd": {"type": "number"},
        # Shared job budget key; sizes the lease and the kill deadline.
        "resources": {
            "type": "object",
            "properties": {"wall_seconds": {"type": "integer"}},
        },
    },
    "required": ["todo_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})

#: Empty: ssh_node's static PROVIDES is ``{has_gpaw}``; the vendor binary
#: and the checkout are the pinned host's, checked at submit.
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Launch one slice (a todo with a brief) through its lane's adapter in "
    "its own worktree slice-td<id> on branch slice/<id> cut from origin/main, "
    "meta.lane/adapter/model/effort on the job, the last message posted as "
    "a handoff: comment on the todo; a clean exit mints a land_slice job "
    "that lands the branch, a branch touching the lane's deny_write_paths "
    "fails the attempt. Refused while PRECIS_FACTORY=off."
)


# ── Config ─────────────────────────────────────────────────────────


def factory_off() -> bool:
    """The kill switch: ``PRECIS_FACTORY=off`` refuses every launch."""
    return os.environ.get("PRECIS_FACTORY", "").strip().lower() == "off"


def _max_turns() -> int:
    try:
        n = int(os.environ.get("PRECIS_RUN_SLICE_MAX_TURNS", ""))
    except ValueError:
        return _DEFAULT_MAX_TURNS
    return n if n > 0 else _DEFAULT_MAX_TURNS


def wall_seconds() -> float:
    """Type cap for ssh_node's lease and kill deadline
    (``PRECIS_RUN_SLICE_WALL_S``, default 4 h)."""
    try:
        n = int(os.environ.get("PRECIS_RUN_SLICE_WALL_S", ""))
    except ValueError:
        return float(_DEFAULT_WALL_S)
    return float(n if n > 0 else _DEFAULT_WALL_S)


def resolve_model(
    params: dict[str, Any], lane: _lanes.Lane | None = None
) -> str | None:
    """``params.model``, else ``PRECIS_RUN_SLICE_MODEL`` (a claude id, so
    it applies to ``claude_p`` lanes only), else the lane's
    ``default_model``. Only Claude falls back to the router's
    ``Tier.FRONTIER`` pin; other vendors use their account default."""
    model = params.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    env = os.environ.get("PRECIS_RUN_SLICE_MODEL", "").strip()
    if env and (lane is None or lane.adapter == "claude_p"):
        return env
    if lane is not None and lane.default_model:
        return lane.default_model
    if lane is not None and lane.adapter != "claude_p":
        return None
    from precis.utils.llm.router import Tier
    from precis.utils.llm.router import resolve_model as _router_model

    return _router_model(Tier.FRONTIER)


def slice_root() -> Path:
    """The checkout worktrees are cut under: ``PRECIS_SLICE_ROOT``, else the
    main checkout of the repo this module runs from (the parent of git's
    common dir, so a worker running inside a worktree still cuts beside
    it, not inside it). Raises ``RuntimeError`` when neither resolves."""
    env = os.environ.get("PRECIS_SLICE_ROOT", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    proc = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], here)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            f"no git checkout around {here} (git: {proc.stderr.strip()[:200]}); "
            "next: set PRECIS_SLICE_ROOT to the repo root on this host"
        )
    common = Path(proc.stdout.strip())
    return common.parent if common.name == ".git" else common


def slice_name(todo_id: int) -> str:
    """Worktree directory name of a slice: ``slice-td<id>``."""
    return f"slice-td{todo_id}"


def worktrees_dir(root: Path) -> Path:
    """The dir slice worktrees are cut under: ``<root>/.claude/worktrees``
    — slices themselves, and land_slice's throwaway ``slice-main-td<id>``
    probe trees."""
    return root / ".claude" / "worktrees"


def ops_root() -> Path:
    """The worker-owned dir slice run state lives under:
    ``<tempdir>/precis-slice-ops/`` (``PRECIS_SLICE_OPS_DIR`` overrides it —
    tests). Created on demand; owned by whichever user runs the worker, so
    everything the session must be able to write goes here rather than
    under a home dir the service account may not own."""
    base = os.environ.get("PRECIS_SLICE_OPS_DIR", "").strip()
    root = Path(base) if base else Path(tempfile.gettempdir()) / "precis-slice-ops"
    root.mkdir(parents=True, exist_ok=True)
    return root


def run_dir(todo_id: int) -> Path:
    """Where a slice's session log and exit record live:
    ``<ops_root>/td<id>/`` — outside the worktree, so a tree that is removed
    while the session runs (a reaper, a hand `git worktree remove`) still
    leaves the poll an exit code and a log to read (jo544355 lost both).
    Created on demand."""
    path = ops_root() / f"td{todo_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def uv_cache_dir() -> Path:
    """``UV_CACHE_DIR`` for the session: ``<ops_root>/uv-cache``. The
    checkout's hooks run through ``uv``, and uv's default cache is under
    the user's home — jo546987 ran as the launchd ``deploy`` user whose
    ``~/.cache/uv`` a root-run bootstrap had left root-owned, so every Bash
    PreToolUse hook died on ``Permission denied`` and the session had no
    shell at all. Created on demand."""
    path = ops_root() / "uv-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def slice_branch(todo_id: int) -> str:
    """Branch of a slice: ``slice/<id>`` (spec § "Branches")."""
    return f"slice/{todo_id}"


def slice_env(
    todo_id: int, effort: str, *, mcp_token: str | None = None, mcp: bool = True
) -> dict[str, str]:
    """The session's environment: :data:`ENV_ALLOWLIST` names present in
    the worker's env, the MCP token (env, else the mounted secret), the
    subscription OAuth token (env, else the vault — the only store for it;
    jo544333 ran as the launchd ``deploy`` user and ``claude`` answered
    ``Not logged in``), and ``PRECIS_SLICE_ID``/``PRECIS_SLICE_EFFORT``.
    Nothing else — in particular no ``ANTHROPIC_API_KEY``, so the session
    cannot fall onto the per-token billed path.

    ``mcp_token`` is the per-slice bearer :func:`_mint_slice_token`
    minted (gr543694); given, it replaces the shared token outright so
    the server attributes the session's calls to this slice. Non-MCP
    vendors receive neither precis nor Claude credentials and never
    consult their secret stores — except a scoped grant (gr574082):
    under ``mcp=False`` a ``mcp_token`` still lands on
    ``PRECIS_MCP_TOKEN`` (beating an inherited one) while the Claude
    OAuth login stays popped, so the vendor's precis reads ride its own
    bearer and its own account login. The other named exception is
    ``kimi_tree`` (td573681): :func:`_kimi_launch_env` adds the
    synthesized provider env, key included, after this returns."""
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if os.environ.get(k)}
    if mcp:
        from precis.utils.claude_oauth import ensure_oauth_token

        ensure_oauth_token(env)
        if mcp_token:
            env["PRECIS_MCP_TOKEN"] = mcp_token
        elif "PRECIS_MCP_TOKEN" not in env:
            try:
                from precis.secrets import mounted_secret

                token = mounted_secret("PRECIS_MCP_TOKEN")
            except Exception:  # pragma: no cover — no secret store on this host
                token = None
            if token:
                env["PRECIS_MCP_TOKEN"] = token
    else:
        env.pop("PRECIS_MCP_TOKEN", None)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        if mcp_token:
            env["PRECIS_MCP_TOKEN"] = mcp_token
    env["PRECIS_SLICE_ID"] = f"td{todo_id}"
    env["PRECIS_SLICE_EFFORT"] = effort
    # The checkout's SessionEnd hook (scripts/hooks/session-end-reap.sh)
    # removes a clean, merged, sessionless worktree when the session ends —
    # which a triage slice's tree is. jo544355: it reaped slice-td543632 under
    # the poll, the exit record was never written, and the job sat until the
    # crash-loop guard. The factory owns slice trees (the lander deletes them).
    env["PRECIS_NO_AUTOREAP"] = "1"
    # uv's cache under a dir this worker owns (see uv_cache_dir).
    env["UV_CACHE_DIR"] = str(uv_cache_dir())
    return env


def deny_violations(paths: list[str], deny_paths: tuple[str, ...]) -> list[str]:
    """The ``paths`` (repo-relative, posix) matching any ``deny_paths``
    glob (``fnmatch``: ``*`` spans ``/``, so ``dir/**`` is the subtree),
    plus any path touching :data:`lanes.HUMAN_ONLY_PATHS` — those are
    never relaxable, so they hold whatever set the lane carries, matched
    case-insensitively."""
    return sorted(
        {p for p in paths if any(fnmatch.fnmatchcase(p, g) for g in deny_paths)}
        | set(_lanes.human_only_hits(paths))
    )


# ── Seams (tests monkeypatch these) ────────────────────────────────


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd``; never raises on a non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=300,
    )


def _launch(
    argv: list[str],
    *,
    cwd: Path,
    stdin: Path | None,
    log: Path,
    exit_file: Path,
    env: dict[str, str],
) -> int:
    """Start ``argv`` detached in its own session: stdin from ``stdin``,
    stdout+stderr to ``log``, its exit code written to ``exit_file`` by the
    wrapping shell (a restarted worker, no longer the parent, still reads
    it). ``stdin=None`` feeds ``/dev/null``. Returns the process group pid."""
    script = '"$@" < "$RS_IN" > "$RS_LOG" 2>&1; echo $? > "$RS_EXIT"'
    full_env = {
        **env,
        "RS_IN": str(stdin) if stdin is not None else os.devnull,
        "RS_LOG": str(log),
        "RS_EXIT": str(exit_file),
    }
    # Fixed script; argv rides as positional args, never interpolated.
    proc = subprocess.Popen(
        ["/bin/sh", "-c", script, "run_slice", *argv],
        cwd=str(cwd),
        env=full_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return int(proc.pid)


def _process_alive(pid: int, *, identity_matches: bool = False) -> bool:
    """Signal 0 liveness; Darwin EPERM needs a matching identity."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return sys.platform != "darwin" or identity_matches
    except OSError:
        return False
    return True


def _darwin_process_identity(pid: int) -> str | None:
    """Start time + user from ps; Darwin has no Linux /proc identity."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=,user=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    identity = " ".join(result.stdout.split())
    return identity if result.returncode == 0 and identity else None


def _handle_pgid(handle: dict[str, Any]) -> int:
    """The session's process group, 0 when the handle names none — never
    our own group (ssh_node's ``_detached_pgid`` rule: a record pointing
    at the poller's group must be unsignallable, never enforceable)."""
    pgid = int(handle.get("pgid") or handle.get("pid") or 0)
    if pgid <= 1 or pgid == os.getpgrp():
        return 0
    return pgid


def _group_alive(pgid: int) -> bool:
    """Signal-0 on the group: a member still answers. ESRCH means gone;
    EPERM names a group we cannot signal — not our session's survivors. A
    group id cannot be recycled while any member lives, so an answering
    group is this session's — unless its leader pid was recycled into a
    NEW group's leader after ours emptied (``os.getpgid`` resolving the
    slot back to this group), in which case every member is foreign."""
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    try:
        if os.getpgid(pgid) == pgid:
            # Reaching here means the recorded leader failed liveness, so
            # the process now leading this group is not ours — the slot
            # was recycled, and escalation must not chase it.
            return False
    except OSError:
        pass  # pid slot dead: the answering members are the survivors
    return True


def _session_alive(handle: dict[str, Any]) -> bool:
    pid = int(handle.get("pid") or 0)
    if pid > 1:
        if "ps_identity" in handle:
            expected = handle.get("ps_identity")
            if expected and _darwin_process_identity(pid) == expected:
                if _process_alive(pid, identity_matches=True):
                    return True
        # If ps failed at submit there is no ps_identity, so Darwin falls
        # back to the legacy rule: EPERM without matching identity is not
        # ours.
        elif _process_alive(pid):
            return True
    # A dead or unidentifiable leader does not prove the session ended:
    # the budget TERM can kill the wrapping ``sh`` while a vendor process
    # that ignores the signal keeps the group open (td602769), so the
    # watch — and the unknown-exit wait — runs against the group.
    return _group_alive(_handle_pgid(handle))


def _signal_group(pgid: int, sig: int) -> None:
    """Send ``sig`` to the process group ``pgid``."""
    os.killpg(pgid, sig)


def _mint_land(
    ctx: Any, todo_id: int, tree: Path, lane: Any, checked: bool = False
) -> int:
    """Mint the ``land_slice`` job for the slice (idem key
    ``land_slice:td<id>``, a child of the todo, pinned to this run's host so
    the claimant holds the worktree). ``checked`` sets
    ``params["checked"]`` — what ``land_slice.ship_flag`` reads as
    ``--checked`` when the builder's test evidence is missing (gr573563).
    Returns its id."""
    from precis.workers._mint import mint_job_once

    meta = ctx.meta or {}
    run_params = meta.get("params") or {}
    params: dict[str, Any] = {
        "todo_id": todo_id,
        "worktree": str(tree),
        "branch": slice_branch(todo_id),
    }
    if lane:
        params["lane"] = str(lane)
    if checked:
        params["checked"] = True
    node = run_params.get("target_node") or os.environ.get("PRECIS_NODE")
    if node:
        params["target_node"] = str(node)
    extra = {"plan_root": meta["plan_root"]} if meta.get("plan_root") else None
    store = ctx.store
    with store.tx() as conn:
        job_id, _reused = mint_job_once(
            store,
            conn=conn,
            job_type="land_slice",
            executor="ssh_node",
            title=f"land_slice td{todo_id}: {slice_branch(todo_id)}",
            params=params,
            parent_id=todo_id,
            idem_key=f"land_slice:td{todo_id}",
            meta_extra=extra,
        )
    return int(job_id)


def _process_identity(pid: int) -> tuple[str, int] | None:
    """``(kernel boot_id, process start ticks)`` of ``pid`` from ``/proc``
    (the identity ssh_node's orphan reaper matches before a kill, same
    read as ``precis_pathway.runner._spawn_detached``); ``None`` when
    ``/proc`` lacks them (not Linux, or the process already exited)."""
    try:
        boot_id = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
        # comm (field 2) can contain spaces and ')'; starttime (field 22)
        # is offset 19 after its final ')'.
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        start_time = int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError, UnicodeError):
        return None
    return boot_id, start_time


# ── Todo reads and writes (store-level; workers never import handlers) ──


def _todo_status(store: Any, todo_id: int) -> str | None:
    for t in store.tags_for(todo_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _is_generator(store: Any, todo_id: int) -> bool:
    ref = store.get_ref(kind="todo", id=todo_id)
    return ref is not None and node_kind(ref.meta) == "generator"


def _mints_children(store: Any, todo_id: int) -> bool:
    """A generator whose brief ends with :func:`_generator_close`: its
    deliverable is child todos. A document role
    (:data:`_NO_COMMIT_ROLES`: reviewer, analyst, brief) delivers a
    comment or a written chunk instead and never mints."""
    ref = store.get_ref(kind="todo", id=todo_id)
    if ref is None or node_kind(ref.meta) != "generator":
        return False
    gen = (ref.meta or {}).get("generate")
    role = gen.get("role") if isinstance(gen, dict) else None
    return role not in _NO_COMMIT_ROLES


def _no_commit_kind(store: Any, todo_id: int) -> str | None:
    """Why a clean exit that left no commit is this node's success
    (gr570534, jo570327): ``task_class '<c>'`` when the class is off
    ``code`` — a brief-writer's work is the ``brief:`` comment it posts,
    not a branch — else ``generate role '<r>'`` for a
    :data:`_NO_COMMIT_ROLES` role. ``None`` for a plain code node: its
    no-commit exit stays a failed attempt."""
    ref = store.get_ref(kind="todo", id=todo_id)
    meta = (ref.meta if ref is not None else None) or {}
    cls = str(meta.get("task_class") or _DEFAULT_TASK_CLASS)
    if cls != _DEFAULT_TASK_CLASS:
        return f"task_class {cls!r}"
    role = _generate_role(meta)
    if role in _NO_COMMIT_ROLES:
        return f"generate role {role!r}"
    return None


def _child_todo_count(store: Any, todo_id: int) -> int:
    """Live todos parented to ``todo_id`` (the children a generator minted)."""
    return len(store.recent_refs(["todo"], parent_id=todo_id, limit=1000))


def _open_tag_values(store: Any, todo_id: int) -> list[str]:
    return [str(t.value) for t in store.tags_for(todo_id) if t.namespace == "open"]


def _todo_chunks(store: Any, todo_id: int) -> list[Any]:
    return list(store.chunks.list_chunks_for_ref(todo_id))


def _append_comment(store: Any, todo_id: int, text: str) -> None:
    """Append ``text`` as the todo's next comment chunk (the
    ``put(kind='todo', id=N, text=...)`` shape: ord = max ord + 1)."""
    existing = _todo_chunks(store, todo_id)
    ord_ = max((int(c.ord) for c in existing), default=0) + 1
    store.chunks.insert_chunks(
        todo_id,
        [
            ChunkInsert(
                ord=ord_,
                text=text,
                meta={
                    "chunk_kind": _COMMENT_KIND,
                    # worker-stamped — the G2 trust boundary (td575705).
                    **worker_comment_meta(),
                },
            )
        ],
    )


def _claim_todo(store: Any, todo_id: int, lane: str) -> None:
    """Take the lease: ``claimed-by:lane-<lane>`` + ``STATUS:doing``."""
    store.add_tag(todo_id, Tag.open(f"claimed-by:lane-{lane}"), set_by="system")
    store.add_tag(
        todo_id, Tag.closed("STATUS", "doing"), set_by="system", replace_prefix=True
    )


def release_todo(store: Any, todo_id: int, lane: str | None) -> int:
    """Release the lease after a failed run: drop the claim, return the
    todo to ``STATUS:open`` and bump ``attempt:<n>``. Returns ``n``."""
    if lane:
        store.remove_tag(todo_id, Tag.open(f"claimed-by:lane-{lane}"))
    prior = 0
    for value in _open_tag_values(store, todo_id):
        m = _ATTEMPT_RE.match(value)
        if m:
            prior = max(prior, int(m.group(1)))
            store.remove_tag(todo_id, Tag.open(value))
    attempt = prior + 1
    store.add_tag(todo_id, Tag.open(f"attempt:{attempt}"), set_by="system")
    store.add_tag(
        todo_id, Tag.closed("STATUS", "open"), set_by="system", replace_prefix=True
    )
    return attempt


def unclaim_todo(store: Any, todo_id: int, lane: str | None) -> None:
    """Give the lease back with no attempt burned: the lane's window failed
    (a quota or rate notice), not the task (gr565359)."""
    if lane:
        store.remove_tag(todo_id, Tag.open(f"claimed-by:lane-{lane}"))
    store.add_tag(
        todo_id, Tag.closed("STATUS", "open"), set_by="system", replace_prefix=True
    )


def close_todo(
    store: Any, todo_id: int, lane: str | None, *, tags: tuple[str, ...] = ()
) -> None:
    """Close a generator that correctly minted nothing (a ``no-op:`` /
    ``moot:`` verdict): drop the claim, add ``tags``, ``STATUS:done``. The
    tick leaves a childless done generator closed (``_step_reopen`` only
    reopens one with open children)."""
    if lane:
        store.remove_tag(todo_id, Tag.open(f"claimed-by:lane-{lane}"))
    for value in tags:
        store.add_tag(todo_id, Tag.open(value), set_by="system")
    store.add_tag(
        todo_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )


# ── Brief ──────────────────────────────────────────────────────────


def _generate_role(meta: Any) -> str | None:
    """``meta.generate.role`` of a todo, else ``None``."""
    gen = (meta or {}).get("generate")
    role = gen.get("role") if isinstance(gen, dict) else None
    return str(role) if role else None


def _generator_close(todo_id: int, models: tuple[str, ...] = ()) -> list[str]:
    """Closing steps of a generator brief: mint children, never close.

    ``models`` is the lane's accepted model ids; listed, the close tells
    the generator to pin each ``run_slice`` child's model and effort in
    ``meta.op.params`` — a model named only in the child's prose is lost
    at dispatch (gr549046)."""
    pin = (
        " Pin the child's model and effort in meta, never in prose — "
        "dispatch reads meta, not the body: `'op': {'type': 'run_slice', "
        "'params': {'model': '<id>', 'effort': '<low|medium|high>'}}` "
        f"with `<id>` one of this lane's accepted ids — {', '.join(models)} — "
        "the cheapest that can still write code, `effort` the smallest "
        "that fits the child."
        if models
        else ""
    )
    return [
        "",
        "## When done: create the child todos",
        "",
        "This todo is a generator: your work is the child todos, not a land.",
        "",
        "1. Create each child with `put(kind='todo', text=..., body=..., "
        "meta={'llm_tier': None, ...}, "
        f"args={{'parent_id': {todo_id}}})`.{pin} Title each child in the "
        "summary-title house style — `<object> · <behaviour> · <role>`, "
        "middot-separated, telegraphic.",
        "2. Add `blocked-by` links between the children for any sequence. "
        "Order lives in those links alone: never write sequencing prose "
        "(`depends on`, `step N of M`, `after <sibling>`) into a title or "
        "body.",
        "3. Then stop. Do not tag this todo done: the dispatcher closes it "
        "when its children finish.",
        "",
        "Nothing to mint? Say so as the FIRST line of your final message: "
        "`moot: <why>` when the source is already resolved, closed or gone "
        "(the plan closes on it), `no-op: <why>` when there is nothing to add "
        "and the plan should go on. Any other childless exit counts as a "
        "failed attempt and is retried.",
        "",
        "Your final message is posted on the todo as the `handoff:` comment: "
        "the children you created (ids) and anything left open.",
    ]


def _reviewer_close(todo_id: int, mcp: bool = True) -> list[str]:
    """Closing steps of a reviewer generator's brief: one verdict, nothing
    minted, nothing committed, nothing landed. The grammar is the one
    ``plan_dispatch._VERDICT_RE`` parses; it reads a ``handoff: review: …``
    the same as a bare comment, so a lane without the precis MCP (``mcp``
    false) delivers the verdict as the first line of its final message."""
    from precis.plans.templates import REVIEW_READONLY_MARKER

    where = (
        f"Post exactly one comment on td{todo_id} (the node you were given), "
        "or make it the FIRST line of your final message (posted as the "
        "`handoff:` comment; the dispatcher reads the verdict there), one of:"
        if mcp
        else "Make your verdict the FIRST line of your final message (it is "
        f"posted on td{todo_id} as the `handoff:` comment and the dispatcher "
        "reads it there; this lane has no MCP to post with), one of:"
    )
    return [
        "",
        "## When done: post your verdict",
        "",
        "This todo is a reviewer: your work is one verdict, not a land and "
        "not child todos.",
        "",
        where,
        "",
        "- `review: LAND`",
        "- `review: FIX — <list of findings>`",
        "- `review: REVERT — <why>`",
        "",
        "`FIX` and `REVERT` need the list / the why after the dash. Mint "
        "nothing: no child todos, no fix task, no revert task; the "
        "dispatcher turns your verdict into those. Commit nothing. Do not "
        "land, push or ship. Do not tag this todo done: the dispatcher "
        "closes it on your verdict.",
        "",
        "You are read-only: run no tests, no typecheck, no lint and no "
        "`uv` or `scripts/test` command — read the squash diff "
        "(`git show <sha>`) and the files. End your verdict line with "
        f"`{REVIEW_READONLY_MARKER}`.",
        "",
        "The rest of your final message: what you checked, in a line or two.",
    ]


def _analyst_close(todo_id: int, mcp: bool = True) -> list[str]:
    """Closing steps of an analyst generator's brief (PR analysis): one
    ``analysis:`` comment, nothing minted, nothing committed, nothing run.
    The prefix is the one ``plan_dispatch._analysis_posted`` looks for (a
    ``handoff:`` carrying it counts, so an MCP-less lane puts it first in
    its final message)."""
    from precis.plans.templates import ANALYSIS_PREFIX

    where = (
        f"Post exactly one comment on td{todo_id} (the node you were given), "
        f"starting with `{ANALYSIS_PREFIX}`, or make that the FIRST line of "
        "your final message (posted as the `handoff:` comment),"
        if mcp
        else f"Make `{ANALYSIS_PREFIX}` the FIRST line of your final message (it "
        f"is posted on td{todo_id} as the `handoff:` comment; this lane has no "
        "MCP to post with),"
    )
    return [
        "",
        "## When done: post your analysis",
        "",
        "This todo is an analyst: your work is one analysis, not a land and "
        "not child todos.",
        "",
        f"{where} with three parts: (a) threats, (b) fit, (c) size.",
        "",
        "Mint nothing: no child todos, no fix task. Commit nothing. Never "
        "run `uv sync`, tests or any file from the PR: read it with "
        "`git fetch` and `git diff` only. Do not land, push or ship. Do not "
        "tag this todo done: the dispatcher closes it once your comment is "
        "posted.",
        "",
        "The rest of your final message: what you read, in a line or two.",
    ]


#: Generator role -> closing-section builder ``(todo_id, mcp)``; a role not
#: listed gets :func:`_generator_close` (create the child todos). A third
#: role is one line.
_ROLE_CLOSE: dict[str, Callable[[int, bool], list[str]]] = {
    "reviewer": _reviewer_close,
    "analyst": _analyst_close,
}

#: ``meta.generate.role``s whose session's deliverable is a document or a
#: single comment — never child todos and never a commit (gr570534): the
#: ``_ROLE_CLOSE`` roles, plus ``brief`` (it posts the ``brief:`` chunk it
#: was asked to write). A clean exit that delivered it is the node's
#: success — no children, no commit, no attempt burned.
_NO_COMMIT_ROLES = frozenset({*_ROLE_CLOSE, "brief"})

_TD_FROM_RE = re.compile(r"td(\d+)")
_LANDED_RE = re.compile(r"^\s*landed:\s*([0-9a-f]{7,40})\b", re.IGNORECASE)
#: Cap on the reviewed todo's brief inlined into a reviewer's brief.
_TARGET_CAP = 6000


def _review_target(store: Any, meta: Any) -> str | None:
    """What a reviewer judges, inlined so a lane without the precis MCP can
    review (td564019 cut 1): the reviewed todo's title, its newest
    ``landed: <sha>`` comment and its brief (body plus the newest
    ``brief:``/``handoff:``), capped. ``None`` for any other node."""
    if _generate_role(meta) != "reviewer":
        return None
    m = _TD_FROM_RE.fullmatch(str((meta.get("generate") or {}).get("from") or ""))
    if m is None:
        return None
    target_id = int(m.group(1))
    ref = store.get_ref(kind="todo", id=target_id)
    if ref is None:
        return None
    landed = None
    for c in reversed(_todo_chunks(store, target_id)):
        if c.chunk_kind != _COMMENT_KIND:
            continue
        hit = _LANDED_RE.match(str(c.text or ""))
        if hit:
            landed = hit.group(1)
            break
    body, latest = _brief_parts(store, target_id)
    lines = [f"td{target_id}: {str(ref.title or '').strip()}", ""]
    if landed:
        lines.append(
            f"Landed squash: `{landed}` — read it with `git show {landed}` in "
            "this worktree (it is on origin/main)."
        )
    else:
        lines.append(
            f"td{target_id} has no `landed:` comment yet: nothing is on main to "
            "review. End with `no-op: nothing landed yet` as the first line of "
            "your final message."
        )
    text = (body or "").strip()
    if latest:
        text = f"{text}\n\n{latest.strip()}".strip()
    if len(text) > _TARGET_CAP:
        text = text[:_TARGET_CAP].rstrip() + "\n\n[brief cut here]"
    if text:
        lines += ["", "Its brief:", "", text]
    return "\n".join(lines)


#: The lint the land runs on the whole tree before it merges, in the
#: order scripts/ship's pre-qland lint runs it (``QLAND_CMD`` —
#: ``QLAND_MYPY``/``QLAND_TAIL``/``SECRET_SCAN``; the
#: ``test_brief_lint_steps_match_ship`` test pins each step to it). A
#: build session runs the same steps so a red the land would report is
#: fixed before the commit, not after a failed land (gr573347).
#:
#: ``_SHIP_LINT_STEPS`` is what the lander's lint runs. The pytest members
#: after the secret scan are the DB-free guards of the land's
#: ``QUICK_GUARDS`` (the whole ``test_skill_ingest.py``, all DB-free, where
#: the land names five of its node ids) plus ``tests/test_skill_size.py``
#: (``CODE_HYGIENE``; otherwise only the release gate runs it).
#: ``test_land_lint_steps_cover_every_quick_guard`` pins each
#: ``QUICK_GUARDS`` file to these steps (gr610185: builders landed skill
#: edits past the 4000-char H2 cap and cycled land → red). ``_PRELAND_STEPS``
#: are checks the lander makes later or in its lock (squawk on a new
#: migration, a conflict with a moved ``origin/main``): run here, the
#: builder gets the finding, not the lander.
_SHIP_LINT_STEPS: tuple[str, ...] = (
    "uv run ruff check --fix .",
    "uv run ruff format .",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy src tests",
    "uv run lint-imports",
    "uv run pytest -q -n0 -p no:cacheprovider tests/test_deploy_tree_no_secrets.py",
    "uv run pytest -q -n0 -p no:cacheprovider -rfE"
    " tests/test_skill_ingest.py tests/test_skill_vocabulary.py"
    " tests/test_render_dry_guard.py tests/test_type_ignore_ratchet.py"
    " tests/test_mcp_raise_next_ratchet.py tests/test_next_literal_ratchet.py"
    " tests/test_ruff_scripts_excludes.py tests/test_scripts_use_uv.py",
    "uv run pytest -q -n0 -p no:cacheprovider tests/test_skill_size.py",
)

#: Squawk on the NEW migration files only (``scripts/ship``'s
#: ``_squawk_new_migrations``: same pinned ``squawk-cli``, ``.squawk.toml``
#: picked up from the repo root); a no-op when the branch adds none. Then
#: the merge dry run against a fresh ``origin/main``: exit 1 lists the
#: conflicting paths (``--name-only``); git reads committed work only.
_PRELAND_STEPS: tuple[str, ...] = (
    "git fetch -q origin main && git diff --name-only --no-renames"
    " --diff-filter=A origin/main... -- '*/migrations/*.sql'"
    " | xargs -r uvx -q --from 'squawk-cli>=2' squawk",
    "git merge-tree --write-tree --name-only origin/main HEAD",
)

_LAND_LINT_STEPS: tuple[str, ...] = _SHIP_LINT_STEPS + _PRELAND_STEPS


def render_brief(
    *,
    todo_id: int,
    title: str,
    body: str,
    latest: str | None,
    lane: str,
    model: str | None,
    effort: str,
    generator: bool = False,
    role: str | None = None,
    mcp: bool = True,
    read_mcp: str | None = None,
    deny_paths: tuple[str, ...] | None = None,
    lane_models: tuple[str, ...] = (),
    target: str | None = None,
) -> str:
    """The prompt a slice session starts from. Ends with the commit-and-
    hand-off instruction (the ``land_slice`` job lands the branch); a
    ``generator`` brief ends with the child-todo instruction instead (the
    plan tick closes it), and a generator whose ``role`` is in ``_ROLE_CLOSE``
    (reviewer: one verdict comment; analyst: one analysis comment) ends with
    that role's instruction, worded for a lane with or without the MCP.
    ``target`` (:func:`_review_target`) is the reviewed todo inlined for a
    reviewer. Names the ``deny_paths`` the session may not write.
    ``read_mcp`` (:func:`_read_mcp_scope`) swaps the no-MCP paragraph for
    the scope's: ``reader`` reads only, ``triage`` also names its write
    set (gr574082)."""
    name = slice_name(todo_id)
    lines = [
        f"# Slice td{todo_id}: {title.strip()}",
        "",
        f"You are a headless build session on lane `{lane}` "
        f"(model `{model or 'account default'}`, "
        f"effort `{effort}`), in git worktree `{name}` on branch "
        f"`{slice_branch(todo_id)}`, cut from `origin/main`. Work only in this "
        "worktree. Read CLAUDE.md and AGENTS.md here before editing; their "
        "rules bind you — except the round rule: a slice is not a round "
        "peer, so never run `scripts/round` (any verb); the mark belongs "
        "to the coordinator.",
    ]
    if not mcp:
        if read_mcp == "reader":
            lines += [
                "",
                "This lane has a read-only precis MCP (get and search). "
                "Before grepping or reading a whole .py file: "
                "search(kind='python', mode='pattern', q='<name>'), then "
                "get(kind='python', id='main::<qualname>', view='source'). "
                "wt-slice-td<id>::<qualname> reads your own tree if it "
                "resolves; main:: is the shared checkout and may lag your "
                "branch. Repo prose: get(kind='md', id='main/docs/...'). "
                "Your task's comments: get(kind='todo', id='td<id>', "
                "view='comments'). Skills: search(kind='skill', q='<task>'). "
                "Nothing else is served and nothing is writable; your "
                "hand-off is still your final message.",
            ]
        elif read_mcp == "triage":
            lines += [
                "",
                "This lane's precis MCP is scoped to structure reads and "
                "gripe triage. Reads: search(kind='python', mode='pattern', "
                "q='<name>'), then get(kind='python', id='main::<qualname>', "
                "view='source'); wt-slice-td<id>::<qualname> reads your own "
                "tree if it resolves; main:: is the shared checkout and may "
                "lag your branch. Repo prose: get(kind='md', "
                "id='main/docs/...'). Your task's comments: get(kind='todo', "
                "id='td<id>', view='comments'). Skills: search(kind='skill', "
                "q='<task>'). Gripes: search(kind='gripe', q='STATUS:open') "
                "and get(kind='gripe', id='gr<id>') on any gripe to pick one. "
                "Writes: put(kind='todo', id='td<id>', text='…') comments on "
                "your own todo and put(kind='gripe', id='gr<id>', text='…') "
                "on any gripe; tag(kind=..., id=..., add=[...]) with "
                "STATUS: or claimed-by: values on those; and "
                "link(kind='todo', id='td<id>', target='gr<id>', "
                "rel='fixes') for the gripe you pick. Everything else "
                "refuses; your hand-off is still your final message.",
            ]
        else:
            lines += [
                "",
                "This lane has no precis MCP. Do not try to reach it. "
                "Everything you want recorded goes in your final message.",
            ]
    if deny_paths is None:
        deny_paths = _lanes.deny_paths_for(None)
    if deny_paths:
        lines += [
            "",
            "## Paths you may not write",
            "",
            "Do not create, edit or delete these paths by any means (Bash "
            "included). A branch or working tree that touches one fails this "
            "attempt and reopens the todo. If the work needs a change there, "
            "stop and say so in your final message.",
            "",
            *[f"- `{p}`" for p in deny_paths],
        ]
    lines += [
        "",
        "## Brief",
        "",
        body.strip() or "(the todo has no body; the title is the brief)",
    ]
    if latest:
        lines += ["", "## Latest brief / handoff on the todo", "", latest.strip()]
    if target:
        lines += ["", "## Review target", "", target.strip()]
    if generator:
        close = _ROLE_CLOSE.get(role or "")
        if close is not None:
            lines += close(todo_id, mcp)
        else:
            lines += _generator_close(todo_id, lane_models)
        return "\n".join(lines) + "\n"
    n = len(_LAND_LINT_STEPS)
    lines += [
        "",
        "## When done: commit on the branch and hand off",
        "",
        "Land lint — the land runs these on the whole tree, in this "
        "order, and a red reopens this todo (td565331 came back for six "
        "missing test annotations). Run each before you hand off:",
        "",
        *[f"{i}. `{step}`" for i, step in enumerate(_LAND_LINT_STEPS, 1)],
        f"{n + 1}. Run the tests you added or changed with `scripts/test "
        "<paths>` — a diff under `src/precis/data/skills/` adds "
        "`tests/test_skill_ingest.py`: the land's lint fails a skill whose "
        "H2 section runs past 4000 chars (gr610185: three builds cycled "
        "land → red on that cap alone). The last two steps read committed "
        "work only (`git diff origin/main...`, `git merge-tree`): commit "
        "first (amend after any fix). A `merge-tree` exit 1 lists the paths "
        "that conflict with `origin/main` — merge or rebase `origin/main`, "
        "resolve them, re-run the lint, before you hand off.",
        "",
        "Fix every report and re-run until all are clean — do not commit "
        "while any step is red. End your final message with exactly one "
        "line naming the gate's outcome: `tests: ran`, or `tests: NOT RUN "
        "— <which step, why>` when a step could not run in this sandbox "
        "(a first-line `validated:`/`no-op:`/`moot:` verdict still comes "
        "first).",
        "",
        f"{n + 2}. Commit on `{slice_branch(todo_id)}` with a conventional "
        f"subject (`type(scope): what (td{todo_id})`, at most 72 bytes), "
        "message via `git commit -F scratch/commit-msg.txt`. One commit "
        "is best: the lander squashes the branch under that subject — or "
        "under a `subject: type(scope): what` line in your final message "
        "when you state one (it wins over every commit subject).",
        f"{n + 3}. Leave the working tree clean (`git status --porcelain` "
        "prints nothing). If your sandbox refuses `git commit` (a "
        "read-only `.git`), stop with the changes in the working tree and "
        "`scratch/commit-msg.txt` written: the worker commits them with "
        "that message after you exit.",
        f"{n + 4}. Stop. Do not land, push, comment `landed:` or tag this "
        "todo done: once you exit cleanly, the lander lands every commit "
        "on the branch, so commit only work fit to land.",
        "",
        "Nothing to commit on purpose? Say so as the FIRST line of your final "
        "message: `validated: <why>` when the check you were asked for passed, "
        "`no-op: <why>` when there is nothing to change and the plan should go "
        "on, `moot: gr<N> already fixed by <sha>` when the plan's reason is "
        "gone — `<sha>` the commit on origin/main that fixed it, whose own "
        "message names the gripe; a bare `moot: <why>` is not accepted for a "
        "code build (it goes to rescue). The todo then closes done. Any other "
        "commit-less exit counts as a failed attempt and is retried.",
        "",
        "Your final message is posted on the todo as the `handoff:` comment the "
        "next session starts from: what you committed, what is left, what you "
        "would do next.",
    ]
    return "\n".join(lines) + "\n"


def _brief_parts(store: Any, todo_id: int) -> tuple[str, str | None]:
    """``(body, newest brief:/handoff: comment)`` of the todo."""
    chunks = _todo_chunks(store, todo_id)
    body = "\n\n".join(
        str(c.text) for c in chunks if c.chunk_kind == TODO_BODY_KIND and c.text
    )
    latest: str | None = None
    latest_ord = -1
    for c in chunks:
        if c.chunk_kind != _COMMENT_KIND:
            continue
        text = str(c.text or "").lstrip()
        if text.lower().startswith(_BRIEF_PREFIXES) and int(c.ord) > latest_ord:
            latest, latest_ord = text, int(c.ord)
    return body, latest


# ── Submit gate ────────────────────────────────────────────────────


def _todo_rejection(store: Any, todo_id: Any) -> str | None:
    if not isinstance(todo_id, int) or isinstance(todo_id, bool) or todo_id <= 0:
        return "run_slice: params.todo_id must be a positive integer (the slice todo)"
    if store is None:
        return None
    ref = store.get_ref(kind="todo", id=todo_id)
    if ref is None:
        return (
            f"run_slice: todo {todo_id} not found (missing or retired); "
            "next: pass the id of a live slice todo"
        )
    status = _todo_status(store, todo_id)
    if status in _CLOSED_STATUSES:
        return (
            f"run_slice: todo {todo_id} is closed (STATUS:{status}); next: "
            f"reopen it with tag(kind='todo', id={todo_id}, add=['STATUS:open']) "
            "or launch another slice"
        )
    return None


def validate_submit(
    store: Any,
    *,
    gripe_id: int | None = None,
    params: dict[str, Any],
) -> str | None:
    """Submit-time gate. Refuses while ``PRECIS_FACTORY=off``, on a
    missing/closed todo, and on an unknown or disabled ``params.lane``.

    ``gripe_id`` is the link target (the todo for ``rel='builds'``); the
    slice is ``params.todo_id`` either way. ``store=None`` (tests) skips the
    DB reads.
    """
    del gripe_id
    if factory_off():
        return (
            "run_slice: the factory is off (PRECIS_FACTORY=off); next: unset it "
            "on the worker host to launch slices"
        )
    reason = _todo_rejection(store, params.get("todo_id"))
    if reason is not None:
        return reason
    cap = params.get("lane_capability")
    if cap is not None and cap not in _LANE_CAPABILITIES:
        return (
            f"run_slice: unknown params.lane_capability {cap!r}; "
            f"known capabilities: {sorted(_LANE_CAPABILITIES)}"
        )
    lane_name = params.get("lane")
    # With a capability stamped the pin is a preference — submit
    # re-picks a dead or incapable one — so only a capability-free pin
    # is refused for naming a lane that cannot serve.
    if lane_name is not None and store is not None and cap is None:
        lane_table = _lanes.load_lanes(store)
        lane = next((ln for ln in lane_table if ln.name == str(lane_name)), None)
        known = [ln.name for ln in lane_table if ln.enabled]
        if lane is None:
            return f"run_slice: unknown lane {lane_name!r}; enabled lanes: {known}"
        if not lane.enabled:
            return f"run_slice: lane {lane_name!r} is disabled; enabled lanes: {known}"
    return None


# ── Detached protocol ──────────────────────────────────────────────


def _branch_exists(root: Path, branch: str) -> bool:
    proc = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root)
    return proc.returncode == 0


def _adopt_legacy_branch(root: Path, todo_id: int) -> None:
    """Rename an earlier attempt's ``slice-td<id>`` branch to
    ``slice/<id>`` (its commits carry over; a checked-out branch renames
    in place). No-op when ``slice/<id>`` exists or there is no legacy one."""
    branch = slice_branch(todo_id)
    legacy = _LEGACY_BRANCH.format(todo_id=todo_id)
    if _branch_exists(root, branch) or not _branch_exists(root, legacy):
        return
    move = _git(["branch", "-m", legacy, branch], root)
    if move.returncode != 0:
        raise RuntimeError(
            f"git branch -m {legacy} {branch} failed: {move.stderr.strip()}"
        )


def _ensure_worktree(root: Path, todo_id: int) -> Path:
    """Worktree ``<root>/.claude/worktrees/slice-td<id>`` on branch
    ``slice/<id>``, cut from ``origin/main``. A retry reuses an existing
    tree and branch (the branch stays owned by its slice until it lands or
    is abandoned); a legacy ``slice-td<id>`` branch is renamed first."""
    tree = worktrees_dir(root) / slice_name(todo_id)
    branch = slice_branch(todo_id)
    _adopt_legacy_branch(root, todo_id)
    if tree.exists():
        return tree
    fetch = _git(["fetch", "origin", "main"], root)
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch origin main failed: {fetch.stderr.strip()}")
    add = _git(["worktree", "add", "-b", branch, str(tree), "origin/main"], root)
    if add.returncode != 0:
        # The branch outlived its tree (an earlier attempt): re-attach it.
        add = _git(["worktree", "add", str(tree), branch], root)
        if add.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {add.stderr.strip()}")
    return tree


#: The MCP preflight's HTTP call — a seam for tests.
_mcp_preflight_open = urllib.request.urlopen


def _mcp_url(tree: Path) -> str | None:
    """The precis MCP URL the session will connect to: the checkout's
    ``.mcp.json`` ``mcpServers.precis.url``. ``None`` when the checkout
    names no such server (nothing to preflight)."""
    try:
        data = json.loads((tree / ".mcp.json").read_text(encoding="utf-8"))
        url = data["mcpServers"]["precis"]["url"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return url if isinstance(url, str) and url.startswith("http") else None


def _preflight_mcp(tree: Path, token: str | None) -> None:
    """One ``initialize`` with the session's bearer against the checkout's
    precis MCP, before the session is launched. Anything but a 2xx raises,
    which fails the attempt in seconds: jo546987 and jo547598 each spent a
    40-minute session on a bearer the server rejected at startup (a shared
    token the service account lacked, then a grant file the server could
    not read), and the 401 surfaced only in the handoff. No precis server
    in ``.mcp.json`` or no token: nothing to check (the session then runs
    without the MCP, as before)."""
    url = _mcp_url(tree)
    if url is None or not token:
        return
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "run_slice-preflight", "version": "1"},
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with _mcp_preflight_open(req, timeout=15) as resp:
            status = int(resp.status)
    except urllib.error.HTTPError as e:
        status = e.code
    except OSError as e:
        raise RuntimeError(
            f"precis MCP preflight: no answer from {url} ({e})"
        ) from None
    if not 200 <= status < 300:
        hint = (
            " — the server rejected the session's bearer: a slice grant it "
            "cannot read, or a shared token it does not hold"
            if status == 401
            else ""
        )
        raise RuntimeError(f"precis MCP preflight: HTTP {status} from {url}{hint}")


def _read_mcp_scope() -> str | None:
    """``PRECIS_SLICE_READ_MCP`` — the scope a ``read_mcp`` lane mints:
    ``reader`` or ``triage``. Unset, empty or any other value is today's
    no-MCP behaviour exactly — the switch ships dark (gr574082)."""
    from precis import slice_tokens

    raw = os.environ.get("PRECIS_SLICE_READ_MCP", "").strip().lower()
    if not raw:
        return None
    if raw in slice_tokens.SCOPES:
        return raw
    log.warning(
        "run_slice: PRECIS_SLICE_READ_MCP=%r is not one of %s; no MCP",
        raw,
        sorted(slice_tokens.SCOPES),
    )
    return None


def _fixes_refs(store: Any, todo_id: int) -> tuple[str, ...]:
    """The refs a scoped grant names: the todo itself plus the gripes
    its ``fixes`` links resolve to (``store.links_for``, the same read
    ``_review_target`` rides). A store without link reads — or a link
    read that throws — narrows to the todo itself."""
    own = f"td{todo_id}"
    try:
        links = store.links_for(todo_id, direction="out", relation="fixes")
        gripes = [
            f"gr{link.dst_ref_id}"
            for link in links or ()
            if isinstance(getattr(link, "dst_ref_id", None), int)
            and store.get_ref(kind="gripe", id=link.dst_ref_id) is not None
        ]
    except Exception:
        return (own,)
    return (own, *gripes)


def _devin_mcp_config(tree: Path, url: str, token: str) -> Path:
    """Write ``<tree>/.devin/mcp_config.local.json`` — the Devin CLI's
    per-tree MCP server entry carrying the minted scoped bearer at mode
    0600 (``devin mcp add`` persists exactly this shape). Gitignored by
    ``.devin/*``; unlinked wherever the grant is revoked, so the token
    never outlives it."""
    path = tree / ".devin" / "mcp_config.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "mcpServers": {
                        "precis": {
                            "type": "http",
                            "url": url,
                            "headers": {"Authorization": f"Bearer {token}"},
                        }
                    }
                }
            )
        )
    # O_CREAT's mode applies only to a fresh file — pin it so a
    # pre-existing wider-mode entry narrows too.
    path.chmod(0o600)
    return path


def _codex_writable_roots(tree: Path) -> tuple[Path, ...]:
    """Extra ``--add-dir`` writable roots for the codex sandbox
    (gr570533): the shared git common dir — the worktree's index and
    refs under ``worktrees/<name>``, the object store, loose/packed
    refs, and the gate queue + slot locks + hold stats ``scripts/test``
    writes — plus the Docker socket's parent dir so ``docker`` inside
    the gate connects. Without them ``git commit`` and ``scripts/test``
    die on the sandbox and the slice exits with work but no commit."""
    roots: list[Path] = []
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], tree)
    if common.returncode == 0 and common.stdout.strip():
        roots.append(Path(common.stdout.strip()))
    docker = os.environ.get("DOCKER_HOST", "")
    sock_dir: Path | None
    if docker.startswith("unix://"):
        # The declared socket path's parent — a configured root holds
        # even when the VM is down (docker still fails on its own).
        sock_dir = Path(docker.removeprefix("unix://")).parent
    else:
        # No DOCKER_HOST: colima's default socket dir under the worker's
        # HOME, when it exists — a missing dir grants no phantom root.
        candidate = Path.home() / ".colima"
        sock_dir = candidate if candidate.is_dir() else None
    if sock_dir is not None:
        home = Path.home()
        if sock_dir == home or home.is_relative_to(sock_dir):
            # /, $HOME or an ancestor of $HOME would widen the sandbox to
            # the worker's whole world for one socket — log and skip
            # (review 1); the gate's docker fails on its own either way.
            log.warning(
                "run_slice: docker socket parent %s is unsafe to grant "
                "writable — skipping the --add-dir",
                sock_dir,
            )
        elif sock_dir not in roots:
            roots.append(sock_dir)
    return tuple(roots)


def _devin_bin_ok(bin: str) -> bool:
    """The devin binary resolves: an explicit ``PRECIS_DEVIN_BIN`` path is
    an executable file; a bare name is on ``PATH``."""
    if os.sep in bin or (os.altsep and os.altsep in bin):
        return os.path.isfile(bin) and os.access(bin, os.X_OK)
    return shutil.which(bin) is not None


def _devin_auth_status(bin: str) -> subprocess.CompletedProcess[str]:
    """``<bin> auth status``; never raises on a non-zero exit — a missing
    or crashing binary answers rc 127, a hang answers rc 124."""
    try:
        return subprocess.run(
            [bin, "auth", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            [bin], 124, stdout="", stderr="auth status timed out"
        )
    except OSError as e:
        return subprocess.CompletedProcess([bin], 127, stdout="", stderr=str(e))


def _devin_seed_trust(under: Path) -> str | None:
    """Seed ``under`` — the checkout's ``.claude/worktrees`` — into the
    Devin CLI's trusted workspaces; the refusal reason on failure,
    ``None`` when the entry holds. Without it Devin stops at its
    directory-trust prompt, which a headless session cannot answer."""
    from precis.utils import devin_trust

    try:
        devin_trust.trust(str(under))
    except Exception as e:
        return str(e)
    return None


class _HostRefusal(RuntimeError):
    """A submit refusal that is the HOST's, not the task's (review ruling
    on gr570688): a missing binary, a dead login or a failed trust seed
    burns no ``attempt:`` and just gives the lease back — the way the
    lane-table error path refuses before claiming."""


def _devin_preflight(under: Path) -> None:
    """Refuse a devin slice launch the session could only die inside
    (gr570688): no binary, no login, or an untrusted checkout. Raises
    :class:`_HostRefusal` naming the failed check and its next step — the
    failure lands in seconds, never as a dead or prompting session."""
    binary = devin_bin()
    if not _devin_bin_ok(binary):
        raise _HostRefusal(
            f"devin preflight: binary {binary!r} is not executable or not on "
            "PATH; next: install the Devin CLI for the worker's user or "
            "point PRECIS_DEVIN_BIN at it"
        )
    status = _devin_auth_status(binary)
    out = f"{status.stdout}\n{status.stderr}".lower()
    if status.returncode != 0 or "not logged in" in out or "logged in" not in out:
        detail = (status.stdout or status.stderr).strip().splitlines()
        tail = f" ({detail[-1].strip()[:200]})" if detail else ""
        raise _HostRefusal(
            f"devin preflight: `devin auth status` reports no login{tail}; "
            "next: `devin auth login` as the worker's user (the deploy-user "
            "login is a stopgap — Reto 2026-11-05)"
        )
    why = _devin_seed_trust(under)
    if why is not None:
        raise _HostRefusal(
            f"devin preflight: seeding {under} into Devin's trusted "
            f"workspaces failed ({why}); next: run "
            f"scripts/lib/devin_trust.py {under} by hand and fix what it names"
        )


def _kimi_bin_ok(bin: str) -> bool:
    """The kimi binary resolves — an explicit ``PRECIS_KIMI_BIN`` path is
    an executable file; a bare name is on ``PATH`` (same rule as
    :func:`_devin_bin_ok`, its own seam so tests patch each lane alone)."""
    if os.sep in bin or (os.altsep and os.altsep in bin):
        return os.path.isfile(bin) and os.access(bin, os.X_OK)
    return shutil.which(bin) is not None


def _kimi_preflight() -> None:
    """Refuse a kimi slice launch the session could only die inside
    (td573681): no binary. The membership key is checked in
    :func:`_kimi_launch_env`; both are :class:`_HostRefusal` — host
    problems, so the todo's lease goes back clean with no ``attempt:``
    burned."""
    binary = kimi_bin()
    if not _kimi_bin_ok(binary):
        raise _HostRefusal(
            f"kimi preflight: binary {binary!r} is not executable or not on "
            "PATH; next: install the Kimi Code CLI for the worker's user or "
            "point PRECIS_KIMI_BIN at it"
        )


def _kimi_launch_env(model: str | None, *, store: Any) -> dict[str, str]:
    """The ``KIMI_*`` env a ``kimi_tree`` session authenticates with
    (td573681).

    ``KIMI_MODEL_API_KEY``/``KIMI_MODEL_BASE_URL``/``KIMI_MODEL_NAME``/
    ``KIMI_MODEL_PROVIDER_TYPE`` synthesize an in-memory provider — the
    only env channel for the key — so the launch never edits the user's
    Kimi config. The model id rides ``KIMI_MODEL_NAME`` (never ``-m``,
    which would rebind to the config's managed/OAuth provider), and
    ``KIMI_CODE_TRUST_WORKSPACE=1`` answers the folder-trust prompt a
    headless session cannot. The base URL is :func:`kimi.base_url`'s —
    the profile's ``base_url`` — so launch and probe hit the same host.
    The key resolves from the worker's secret store; a miss is a
    :class:`_HostRefusal`. Nothing else is set: no User-Agent, identity
    or telemetry override — the CLI ships as shipped (the subscription
    terms forbid tampering). Names verified against the installed 2.3.3
    binary by the author only; confirm at the melchior preflight."""
    from precis import secrets
    from precis.errors import BadInput
    from precis.utils.llm import kimi

    try:
        url = kimi.base_url()
    except BadInput as exc:
        # A profile base_url off the resolver's allowlist: a config/host
        # problem — the key must never leave the process for an unlisted
        # host, so the refusal precedes the vault read.
        raise _HostRefusal(f"kimi launch refused: {exc}") from exc
    key = secrets.get_secret(kimi.API_KEY_REF, store=store)
    if not key:
        raise _HostRefusal(
            f"kimi preflight: {kimi.API_KEY_REF} is not set; next: store the "
            "Kimi Code membership key for the worker's user (the /secrets "
            "page entry) or export it in the worker's environment"
        )
    return {
        "KIMI_MODEL_API_KEY": key,
        "KIMI_MODEL_BASE_URL": url,
        "KIMI_MODEL_NAME": model or kimi.MODELS[0],
        "KIMI_MODEL_PROVIDER_TYPE": "kimi",
        "KIMI_CODE_TRUST_WORKSPACE": "1",
    }


def _redact(text: str, secrets: Iterable[tuple[str, str]]) -> str:
    """Replace every occurrence of each secret value with
    ``[redacted:<NAME>]`` — the plain spelling plus its URL-encoded and
    JSON-escaped forms, so a key a session echoed (or a stream-json
    record embedded) never reaches a stored surface. Values shorter
    than 8 chars are skipped: a short string's false-positive cost is
    too high."""
    for name, value in secrets:
        if not value or len(value) < 8:
            continue
        forms = {
            value,
            json.dumps(value)[1:-1],
            urllib.parse.quote(value, safe=""),
        }
        for form in sorted(forms, key=len, reverse=True):
            if form:
                text = text.replace(form, f"[redacted:{name}]")
    return text


#: The kimi key each in-flight job's child env actually received at
#: submit, keyed by job ref id — process memory only, never the job meta,
#: the handle or a log. The terminal poll pops it in
#: :func:`_child_env_secrets` to scrub the submit-time value the vault no
#: longer holds after a mid-run rotation; every exit path that skips the
#: poll pops it in :func:`_drop_submit_key`, so the memory dies with the
#: run. A restart (or an early drop followed by a late poll) loses it, in
#: which case the poll treats that value like any unreadable secret —
#: env-naming lines drop and the raw handoff is withheld.
_KIMI_SUBMIT_KEYS: dict[int, str] = {}

#: The comment a poll posts instead of the raw handoff when a key value
#: the child may have carried could not be re-read (review round 2): the
#: session's last message is session text, so it stays off the todo.
#: Deliberately not a ``handoff:``-prefixed line — the next session's
#: brief must not inherit it.
_HANDOFF_WITHHELD = (
    "handoff withheld: credential unreadable at poll; log tail retained redacted"
)


def _drop_submit_key(ctx: Any) -> None:
    """Drop this job's :data:`_KIMI_SUBMIT_KEYS` entry on an exit path
    that never reaches the terminal poll — a refused or failed submit, a
    malformed handle, a kill, a release. The dict is process memory: an
    entry left behind holds a credential longer than the run that needed
    it. A terminal poll that still runs afterwards reads the missing
    entry like a worker restart — the stored tail drops env-naming
    lines and the handoff is withheld (the safe side)."""
    job = getattr(ctx, "ref_id", None)
    if isinstance(job, int) and not isinstance(job, bool):
        _KIMI_SUBMIT_KEYS.pop(job, None)


def _child_env_secrets(
    adapter_name: str, store: Any, *, job: int | None = None
) -> tuple[list[tuple[str, str]], bool]:
    """``(name/value pairs to scrub, unscrubbed)`` — the secret values a
    slice's child env carried, for the poll to redact from the log before
    any stored surface sees it. Never taken from job meta or the handle.

    ``unscrubbed`` marks a value the poll cannot name: the credential
    unreadable at poll, or the submit-time value lost (worker restart —
    :data:`_KIMI_SUBMIT_KEYS` is process memory). The stored tail then
    drops the lines that could carry a key and the raw handoff is
    withheld. ``kimi_tree`` is the only lane whose env holds a vault/env
    credential (``KIMI_MODEL_API_KEY``); it scrubs BOTH the value
    re-read at poll and the submit-time value a mid-run rotation would
    otherwise leave visible. Other lanes authenticate by host login or
    the minted MCP grant, which dies with its file at poll and is never
    a ``get_secret`` name.
    """
    if adapter_name != "kimi_tree":
        return [], False
    from precis import secrets
    from precis.utils.llm import kimi

    submitted = _KIMI_SUBMIT_KEYS.pop(job, None) if job is not None else None
    try:
        current = secrets.get_secret(kimi.API_KEY_REF, store=store)
    except Exception:
        log.warning(
            "run_slice: could not re-read %s for the log scrub",
            kimi.API_KEY_REF,
            exc_info=True,
        )
        current = None
    values = {v for v in (current, submitted) if isinstance(v, str) and v}
    pairs = [("KIMI_MODEL_API_KEY", v) for v in sorted(values)]
    return pairs, not current or submitted is None


#: Env-var names a stored line must not carry when the key behind them
#: could not be re-read (a line naming the variable may hold its value).
_KIMI_ENV_NAMES: tuple[str, ...] = ("KIMI_MODEL_", "KIMI_CODE_API_KEY")


def _drop_secret_lines(text: str) -> str:
    """The unreadable-secret fallback for the stored tail: keep only the
    lines before the first that names a kimi env var."""
    kept: list[str] = []
    for line in text.splitlines():
        if any(name in line for name in _KIMI_ENV_NAMES):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _mint_slice_token(
    ctx: Any,
    todo_id: int,
    *,
    model: str | None,
    brief: str,
    scope: str | None = None,
    refs: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Mint the session's per-slice bearer + its agentlog (gr543694).

    The grant file goes into :func:`precis.slice_tokens.registry_dir` —
    ``None`` on a host whose shared server was never upgraded (no dir,
    no ``PRECIS_MCP_SLICE_TOKEN_DIR``), which keeps the slice on the
    shared token rather than breaking the factory on an un-deployed
    host. A failed ``open_run`` still mints (attribution then rides the
    ``slice-td<id>-`` session-id prefix and ``meta.slice`` only); a
    failed mint abandons the log and returns ``None``.

    The file is named ``td<todo_id>j<job_id>`` (``a<agentlog_id>`` when
    no job id is around, bare ``td<todo_id>`` when neither is): two
    jobs can run sessions for one todo at once — a bare ``td<id>`` file
    let the resubmit overwrite the live grant and the older run's revoke
    then deleted the new grant.

    ``scope``/``refs`` mint a scoped grant (reader/triage, gr574082):
    the grant file carries them and the server bounds the session's
    calls to the scope's allowlist.
    """
    from precis import agentlog, slice_tokens

    directory = slice_tokens.registry_dir()
    if directory is None:
        return None
    log_id = agentlog.open_run(
        ctx.store,
        source="run_slice",
        title=f"run_slice td{todo_id}",
        model=model,
        prompt=brief,
        parent_ref_id=todo_id,
        job_ref_id=getattr(ctx, "ref_id", None),
        meta_extra={"slice": f"td{todo_id}"},
    )
    job = getattr(ctx, "ref_id", None)
    if isinstance(job, int) and not isinstance(job, bool) and job > 0:
        name = f"td{todo_id}j{job}"
    elif isinstance(log_id, int) and log_id > 0:
        name = f"td{todo_id}a{log_id}"
    else:
        name = f"td{todo_id}"
    try:
        token = slice_tokens.mint(
            directory, name, agentlog_id=log_id, scope=scope, refs=refs
        )
    except Exception:
        log.warning(
            "run_slice: slice-token mint for td%d failed; launching on the "
            "shared token",
            todo_id,
            exc_info=True,
        )
        agentlog.close_run(ctx.store, log_id, status="aborted")
        return None
    return {
        "token": token,
        "dir": str(directory),
        "agentlog_id": log_id,
        "name": name,
    }


def _handle_grant(handle: dict[str, Any]) -> dict[str, Any] | None:
    """The grant fields the handle persisted for a later revoke, or
    ``None`` when the submit minted nothing."""
    if not isinstance(handle, dict) or not handle.get("slice_token_dir"):
        return None
    return {
        "dir": handle.get("slice_token_dir"),
        "agentlog_id": handle.get("agentlog_id"),
        # Handles written before the job-qualified name carry no ``name``;
        # their file is the bare td<todo_id>.
        "name": handle.get("slice_token_name"),
        "devin_config": handle.get("devin_config"),
    }


def _revoke_slice_grant(ctx: Any, todo_id: int, grant: Any, status: str) -> None:
    """Delete the grant file (the token 401s from the next request on)
    and stamp the run's agentlog closed. Best-effort — the job outcome
    lands either way."""
    if not grant:
        return
    from precis import agentlog, slice_tokens

    try:
        slice_tokens.revoke(
            Path(str(grant.get("dir") or "")),
            str(grant.get("name") or f"td{todo_id}"),
        )
    except Exception:  # pragma: no cover — defensive
        log.warning("run_slice: slice-token revoke for td%d failed", todo_id)
    devin_config = grant.get("devin_config")
    if devin_config:
        # The per-tree Devin MCP entry holds the same bearer — unlink it
        # wherever the grant is revoked (poll, kill, release, a failed
        # launch, a host refusal) so the token dies with its grant.
        try:
            Path(str(devin_config)).unlink(missing_ok=True)
        except OSError:  # pragma: no cover — best-effort
            pass
    agentlog.close_run(ctx.store, grant.get("agentlog_id"), status=status)


def _fail_submit(ctx: Any, todo_id: int, lane: str | None, reason: str) -> None:
    _drop_submit_key(ctx)
    attempt = None
    try:
        attempt = release_todo(ctx.store, todo_id, lane)
    except Exception:  # pragma: no cover — defensive; the job still fails
        log.warning("run_slice: lease release on td%d failed", todo_id, exc_info=True)
    suffix = f" (todo td{todo_id} back to open, attempt:{attempt})" if attempt else ""
    ctx.record_failure(f"run_slice: {reason}{suffix}")


def _raise_lane_refusal(store: Any, lane: str, why: str, until: datetime) -> None:
    """One deduped warn alert per lane on a host refusal (gr571395), so a
    broken host reaches a human once — not once per parked todo.
    Best-effort: an alert failure never masks the refusal."""
    try:
        raise_alert(
            store,
            source=_REFUSAL_ALERT_SOURCE,
            fingerprint=f"lane-host-refusal:{lane}",
            title=f"lane {lane} refused a launch (host)",
            detail=(
                f"{why} Lane skipped until {until.strftime('%Y-%m-%dT%H:%M:%SZ')}."
            ),
            severity="warn",
        )
    except Exception:
        log.warning(
            "run_slice: host-refusal alert for lane %s failed", lane, exc_info=True
        )


def _resolve_lane_refusal(store: Any, lane: str) -> None:
    """A launch on a lane that once refused clears its open alert.
    Best-effort: an alert failure never masks the launch."""
    try:
        resolve_alert_by_fingerprint(
            store,
            source=_REFUSAL_ALERT_SOURCE,
            fingerprint=f"lane-host-refusal:{lane}",
            resolved_by="run_slice launch",
        )
    except Exception:
        log.warning(
            "run_slice: host-refusal resolve for lane %s failed", lane, exc_info=True
        )


def _lane_offers(lane: _lanes.Lane, capability: str) -> bool:
    """``True`` when the lane's adapter declares ``capability`` — the
    ``params.lane_capability`` requirement the pick enforces."""
    adapter = ADAPTERS.get(lane.adapter)
    return adapter is not None and bool(getattr(adapter, capability, False))


def _free_lane_wait_min(store: Any) -> int:
    """Minutes a submit parks for a free lane at its cap before a paid
    lane may take the pick — ``factory.free_lane_wait_min``, default 20
    (gr573563). Unset or unparsable resolves 20; a negative setting is
    0 (pay immediately)."""
    from precis import settings

    wait = settings.get_int("factory.free_lane_wait_min", store=store)
    if wait is None:
        return 20
    return max(0, wait)


def _park_lane_full(
    ctx: Any,
    note: str,
    until: datetime | None = None,
    lanes: Sequence[str] = (),
) -> None:
    """Park the job ``until`` (default :data:`LANE_FULL_BACKOFF` ahead —
    gr561491) when ``ctx`` can defer and the wait is under
    :data:`LANE_WAIT_PARK_CAP`; fail it attempt-free otherwise. The todo is
    not claimed yet, so no release and no attempt is burned either way.

    Each park resets ``pause_parks`` and counts ``wait_parks`` instead:
    the shared pause budget exists for window-scoped pauses, and a lane
    wait is not one (gr572118). Past the cap — or with no ``defer``, or a
    refused park — the job fails with ``meta.attempt_free = "lane-wait"``
    so ``plan_dispatch`` re-queues the todo without counting an attempt.

    ``lanes`` are the lanes whose freeing slot would unblock this job; they
    land on ``meta.lane_wait`` so the finishing job on one of them wakes the
    oldest such waiter at once (:func:`precis.plans.lanes.wake_lane_waiter`,
    gr611299) and the timer is only the fallback. The park's ``why`` carries
    :data:`~precis.plans.lanes.LANE_PARK_WHY` — the marker the wake requires,
    so a later quota park of the same job is never woken as a lane wait. A
    park that follows an early wake (``meta.lane_woken``: the job lost the
    race for the slot) adds the real elapsed seconds to ``meta.early_wait_s``
    instead of a ``wait_parks`` step, so the cap and the free-lane wait clock
    keep measuring wall-clock time (:func:`_lane_waited`)."""
    meta = ctx.meta or {}
    parks = int(meta.get("wait_parks") or 0)
    now = datetime.now(UTC)
    early_s = float(meta.get("early_wait_s") or 0)
    early = bool(meta.get("lane_woken"))
    if early:
        parked_at = float(meta.get("lane_park_at") or now.timestamp())
        early_s += max(0.0, now.timestamp() - parked_at)
    waited = _lane_waited({"wait_parks": parks, "early_wait_s": early_s})
    defer = getattr(ctx, "defer", None)
    if waited < LANE_WAIT_PARK_CAP * LANE_FULL_BACKOFF and defer is not None:
        wake = until if until is not None else now + LANE_FULL_BACKOFF
        ctx.set_meta(
            pause_parks=0,
            wait_parks=parks if early else parks + 1,
            early_wait_s=early_s,
            lane_wait=list(lanes),
            lane_woken=False,
            lane_park_at=int(now.timestamp()),
        )
        if defer(wake, f"{_lanes.LANE_PARK_WHY}{note}"):
            return
    ctx.set_meta(**{ATTEMPT_FREE_KEY: "lane-wait"})
    ctx.record_failure(
        f"{note} — no lane slot for {int(waited.total_seconds() // 60)} min"
    )


def _lane_waited(meta: Any) -> timedelta:
    """Wall-clock time a job has spent lane-parked: ``wait_parks`` timer
    parks of :data:`LANE_FULL_BACKOFF` plus ``early_wait_s`` seconds spent
    in parks an early wake (gr611299) cut short."""
    return int(meta.get("wait_parks") or 0) * LANE_FULL_BACKOFF + timedelta(
        seconds=float(meta.get("early_wait_s") or 0)
    )


def submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half (see the module docstring). Returns the handle,
    or ``None`` after driving the job terminal (or parking it when every
    fit lane, or the lane ``params.lane`` pins, is at its cap)."""
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    store = ctx.store
    todo_id = params.get("todo_id")
    if factory_off():
        ctx.record_failure("run_slice: the factory is off (PRECIS_FACTORY=off)")
        return None
    reason = _todo_rejection(store, todo_id)
    if reason is not None:
        ctx.record_failure(reason)
        return None
    assert isinstance(todo_id, int)
    todo = store.get_ref(kind="todo", id=todo_id)

    task_class = str((todo.meta or {}).get("task_class") or _DEFAULT_TASK_CLASS)
    # A relaunch after a lane's quota/rate window (gr565359) skips that
    # vendor outright; the table shrinks, it does not fall back.
    exclude = str((ctx.meta or {}).get("exclude_vendor") or "")
    lane_table = _lanes.load_lanes(store)
    # ``params.lane_capability`` — the requirement a factory mint stamps
    # when it cannot hold its pin (factory_rescue._resolve_lane_pin,
    # td610544): the pin becomes a preference and the capability the
    # contract — a dead or incapable pin re-picks, an unpinned pick sees
    # offering lanes only and waits on one rather than launching without
    # the capability the task was minted for (an audit files its gripes
    # through a write MCP; a devin/codex slot has none).
    cap = params.get("lane_capability")
    if cap is not None and cap not in _LANE_CAPABILITIES:
        # A mint bug, not capacity: parking would wait forever.
        ctx.record_failure(
            f"run_slice: unknown params.lane_capability {cap!r}; "
            f"known capabilities: {sorted(_LANE_CAPABILITIES)}"
        )
        return None
    pinned = (
        next((ln for ln in lane_table if ln.name == str(params["lane"])), None)
        if params.get("lane")
        else None
    )
    left_pin = pinned is not None and bool(exclude) and pinned.vendor == exclude
    if left_pin:
        # The ladder's pin (plan_dispatch._escalate) on the vendor whose
        # window closed: the park chose to leave it, so this launch picks
        # by class; the rung's model belongs to that lane and goes with it.
        ctx.append_chunk(
            "job_event",
            f"run_slice: pinned lane {params['lane']!r} is vendor {exclude!r}, "
            "whose window closed; picking another vendor's lane",
        )
    lane: _lanes.Lane | None = pinned if (params.get("lane") and not left_pin) else None
    if params.get("lane") and not left_pin:
        dead = lane is None or not lane.enabled or lane.parked
        if dead and cap is None:
            ctx.record_failure(
                f"run_slice: lane {params['lane']!r} unknown or disabled"
            )
            return None
        if dead or (
            cap is not None and lane is not None and not _lane_offers(lane, cap)
        ):
            # The pin went dead between mint and submit, or a retry
            # climb pinned a lane that never offered the capability —
            # re-pick instead of the 0-second failure a hard pin died
            # with (gr604579).
            ctx.append_chunk(
                "job_event",
                f"run_slice: params.lane {params['lane']!r} "
                + ("is unknown, disabled or parked" if dead else f"offers no {cap!r}")
                + f"; params.lane_capability {cap!r} re-picks",
            )
            lane = None
    if lane is not None:
        # A pinned lane (an escalated retry, a reviewer pick) is held to
        # its cap like a picked one (td562568): before this an escalated
        # retry ran a free lane over its cap of 1. A lane backed off by a
        # host refusal (gr571395) parks until the window ends — the next
        # launch on it is the probe.
        until = _lanes.lane_backoffs(store).get(lane.name)
        if until is not None:
            # Per-todo jitter (≤5 min) so a fleet of jobs parked on the
            # same refusal does not probe the lane in one burst.
            wake = until + timedelta(seconds=int(todo_id) * 37 % 300)
            _park_lane_full(
                ctx,
                f"run_slice: lane {lane.name!r} pinned by params.lane is "
                f"backed off until {until.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                "after a host refusal",
                until=wake,
            )
            return None
        live = _lanes.live_lane_counts(store).get(lane.name, 0)
        if live >= lane.cap:
            _park_lane_full(
                ctx,
                f"run_slice: lane {lane.name!r} pinned by params.lane is at "
                f"its cap ({live}/{lane.cap})",
                lanes=(lane.name,),
            )
            return None
    else:
        gap = _lanes.missing_class_reason(lane_table, task_class)
        if gap is not None:
            # Configuration may change after dispatch. End this job without
            # parking; the next plan tick blocks the node on a coordinator
            # ask. A live pin above bypasses class fit, so it is not gated.
            ctx.record_failure(f"run_slice: {gap}")
            return None
        # A triage-pinned model narrows the pick to lanes that run it
        # (``Lane.accepts_model``): before this a build carrying a claude id
        # (td543634) was codex-fit and would have launched with a model the
        # codex CLI rejects.
        pin = None if left_pin else params.get("model")
        model_pin = pin.strip() if isinstance(pin, str) and pin.strip() else None
        capable = (
            tuple(ln for ln in lane_table if _lane_offers(ln, cap))
            if cap is not None
            else lane_table
        )
        table = tuple(ln for ln in capable if not exclude or ln.vendor != exclude)
        backoffs = _lanes.lane_backoffs(store)
        # Free compute first (gr573563): while a fit free lane exists the
        # pick holds paid lanes out; the build waits on the free lane's
        # cap and falls through to paid only once wait_parks has covered
        # factory.free_lane_wait_min of LANE_FULL_BACKOFF parks.
        wait_min = _free_lane_wait_min(store)
        waited = _lane_waited(ctx.meta or {})
        paid_ok = waited >= timedelta(minutes=wait_min)
        lane = _lanes.pick_lane(
            store,
            task_class,
            lanes=table,
            model=model_pin,
            backoff=backoffs,
            paid_ok=paid_ok,
        )
        if lane is None:
            if model_pin and not any(
                ln.enabled
                and not ln.parked
                and ln.fits(task_class)
                and ln.accepts_model(model_pin)
                for ln in capable
            ):
                # A config error, not capacity: parking would wait forever.
                ctx.record_failure(
                    f"run_slice: no enabled lane fit for task class "
                    f"{task_class!r} accepts model {model_pin!r}; pin a lane "
                    "via params.lane or drop params.model"
                )
                return None
            fit_lanes = [
                ln
                for ln in table
                if ln.enabled
                and not ln.parked
                and ln.fits(task_class)
                and (model_pin is None or ln.accepts_model(model_pin))
            ]
            fit_names = [ln.name for ln in fit_lanes]
            backed = sorted(set(fit_names) & set(backoffs))
            why = "no enabled lane" if not fit_names else "every fit lane at its cap"
            # The lanes whose freeing slot would let this job launch
            # (gr611299): fit and not backed off, and while the free-lane
            # wait runs (not paid_ok) only the free ones — a paid lane
            # freeing would wake a job that cannot take it.
            wake_lanes = [ln for ln in fit_lanes if ln.name not in backoffs]
            if not paid_ok:
                held_free = [
                    ln.name
                    for ln in fit_lanes
                    if ln.cost_class == _lanes.COST_FREE and ln.name not in backoffs
                ]
                if held_free:
                    wake_lanes = [ln for ln in wake_lanes if ln.name in held_free]
                held_paid = [
                    ln.name
                    for ln in fit_lanes
                    if ln.cost_class != _lanes.COST_FREE and ln.name not in backoffs
                ]
                if held_free and held_paid:
                    why = (
                        f"free lane {', '.join(held_free)} at its cap; "
                        f"a paid lane after {wait_min} min"
                    )
            if backed:
                why = f"lanes {', '.join(backed)} backed off after a host refusal"
                if len(backed) < len(fit_names):
                    why += "; the rest at their caps"
            _park_lane_full(
                ctx,
                f"run_slice: no {f'{cap} ' if cap else ''}lane free for "
                f"task class {task_class!r} ({why})",
                lanes=[ln.name for ln in wake_lanes],
            )
            return None

    try:
        adapter = get_adapter(lane.adapter)
    except KeyError:
        # A lane-table error, not the slice's: the todo is not claimed yet,
        # so no release and no attempt:<n> burned on it.
        ctx.record_failure(
            f"run_slice: lane {lane.name!r} has unknown adapter {lane.adapter!r}"
        )
        return None
    if not adapter.mcp and _mints_children(store, todo_id):
        # A child-minting generator works through the precis MCP; a lane
        # without one ends with nothing minted. The templates class such
        # nodes off ``code`` (plans/templates.py) so the pick never lands
        # here; a hand-minted generator can. Not claimed yet: no attempt
        # burned. A reviewer or analyst is not refused: its one verdict
        # rides the final message (``handoff: review: …``), which
        # plan_dispatch reads (td564019 cut 1).
        ctx.record_failure(
            f"run_slice: td{todo_id} is a generator (mints children through "
            f"the precis MCP) but lane {lane.name!r} has no MCP; set "
            "meta.task_class off 'code' or pin an MCP lane via params.lane"
        )
        return None
    model = resolve_model(
        {k: v for k, v in params.items() if k != "model"} if left_pin else params, lane
    )
    effort = str(params.get("effort") or _DEFAULT_EFFORT)
    # Leaving the lane-full park (gr611299): the lane wait marks must not
    # outlive it, or a later quota park of this job would read as one.
    ctx.set_meta(
        lane=lane.name,
        adapter=adapter.name,
        model=model,
        effort=effort,
        lane_wait=None,
        lane_woken=False,
    )
    budget = params.get("budget_usd")
    budget_usd = (
        float(budget)
        if isinstance(budget, (int, float))
        and not isinstance(budget, bool)
        and math.isfinite(float(budget))
        and float(budget) > 0
        else None
    )
    if budget_usd is not None and not adapter.budget_capable:
        # A bound the lane cannot account is a refused launch, not a
        # session that runs unbounded (td601426 review): devin's paid
        # ATIF carries tokens only, codex/kimi report no spend. The
        # failure counts as an attempt, so the climb never re-picks the
        # same unenforceable rung; the todo was never claimed.
        ctx.record_failure(
            f"run_slice: params.budget_usd ${budget_usd:.2f} is not "
            f"enforceable on lane {lane.name!r} — adapter {adapter.name} "
            "reports no spend the bound can ride; cap only a lane whose "
            "adapter enforces a budget"
        )
        return None
    _claim_todo(store, todo_id, lane.name)

    body, latest = _brief_parts(store, todo_id)
    is_generator = node_kind(todo.meta) == "generator"
    # The scoped read grant is a code-class affordance of the read lanes
    # (codex_exec, devin_tree — gr574082): PRECIS_SLICE_READ_MCP names the
    # scope to mint, anything else is today's no-MCP launch (ships dark).
    read_scope = (
        _read_mcp_scope()
        if adapter.read_mcp and task_class == _DEFAULT_TASK_CLASS and not is_generator
        else None
    )
    brief_kwargs: dict[str, Any] = dict(
        todo_id=todo_id,
        title=str(todo.title or ""),
        body=body,
        latest=latest,
        lane=lane.name,
        model=model,
        effort=effort,
        generator=is_generator,
        role=_generate_role(todo.meta),
        mcp=adapter.mcp,
        deny_paths=lane.deny_write_paths,
        lane_models=lane.models,
        target=_review_target(store, todo.meta),
    )
    brief = render_brief(**brief_kwargs, read_mcp=read_scope)
    grant: dict[str, Any] | None = None
    mint_error: str | None = None
    read_url: str | None = None
    try:
        tree = _ensure_worktree(slice_root(), todo_id)
        scratch = tree / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        brief_path = scratch / "brief.md"
        brief_path.write_text(brief, encoding="utf-8")
        ops = run_dir(todo_id)
        log_path = ops / "run_slice.log"
        exit_path = ops / "run_slice.exit"
        exit_path.unlink(missing_ok=True)
        if adapter.mcp:
            assert model is not None
            grant = _mint_slice_token(ctx, todo_id, model=model, brief=brief)
        elif read_scope:
            # The scoped grant path: mint + preflight, and for devin the
            # per-tree MCP config. Any failure launches as today — never
            # the shared token — with the grant revoked, the brief
            # rewritten to the no-MCP wording and mint_error naming the
            # reason in the launch event.
            try:
                read_url = _mcp_url(tree)
                if read_url is None:
                    raise RuntimeError("checkout names no precis MCP (.mcp.json)")
                grant = _mint_slice_token(
                    ctx,
                    todo_id,
                    model=model,
                    brief=brief,
                    scope=read_scope,
                    refs=_fixes_refs(store, todo_id),
                )
                if grant is None:
                    raise RuntimeError("slice-token mint failed")
                _preflight_mcp(tree, grant["token"])
                if adapter.name == "devin_tree":
                    grant["devin_config"] = str(
                        _devin_mcp_config(tree, read_url, grant["token"])
                    )
            except Exception as exc:
                _revoke_slice_grant(ctx, todo_id, grant, "aborted")
                grant = None
                read_url = None
                mint_error = str(exc)[:200]
                brief = render_brief(**brief_kwargs)
                brief_path.write_text(brief, encoding="utf-8")
        if adapter.name == "codex_exec":
            # A retry reuses the tree: never accept a previous run's handoff.
            (scratch / "last-message.md").unlink(missing_ok=True)
        atif_path = None
        if adapter.name == "devin_tree":
            # The run's ATIF export (its usage feed); a retry reuses the
            # ops dir, so a previous run's export never reads back.
            atif_path = ops / "devin.atif.json"
            atif_path.unlink(missing_ok=True)
        argv = adapter.argv(
            model=model,
            effort=effort,
            tree=tree,
            brief_path=brief_path,
            deny_paths=lane.deny_write_paths,
            max_turns=_max_turns(),
            models=lane.models,
            writable_roots=(
                _codex_writable_roots(tree) if adapter.name == "codex_exec" else ()
            ),
            read_mcp_url=read_url if grant else None,
            export_path=atif_path,
            budget_usd=budget_usd,
        )
        env = slice_env(
            todo_id,
            effort,
            mcp_token=grant["token"] if grant else None,
            mcp=adapter.mcp,
        )
        if adapter.name == "devin_tree":
            # The checkout's worktrees dir is the slice's parent; seed it
            # before launch or the session stalls on the trust prompt.
            _devin_preflight(tree.parent)
        if adapter.name == "kimi_tree":
            # Binary + the membership key it authenticates with — both
            # host problems that burn no attempt.
            _kimi_preflight()
            env.update(_kimi_launch_env(model, store=store))
            # The value the child actually got — process memory only, so
            # the poll can still scrub it after a mid-run vault rotation.
            job = getattr(ctx, "ref_id", None)
            if isinstance(job, int) and not isinstance(job, bool):
                _KIMI_SUBMIT_KEYS[job] = env["KIMI_MODEL_API_KEY"]
        if adapter.mcp:
            _preflight_mcp(tree, env.get("PRECIS_MCP_TOKEN"))
        pid = _launch(
            argv,
            cwd=tree,
            stdin=adapter.stdin(brief_path),
            log=log_path,
            exit_file=exit_path,
            env=env,
        )
        started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        identity = _process_identity(pid)
        ps_identity = (
            _darwin_process_identity(pid) if sys.platform == "darwin" else None
        )
    except _HostRefusal as exc:
        # The host cannot run the lane right now; the task is uncharged —
        # give the lease back clean, like the lane-table error path. The
        # refusal stamps the lane's backoff on this job (gr571395):
        # lane_backoffs skips the lane until then, and plan_dispatch never
        # counts the job as an attempt.
        _drop_submit_key(ctx)
        _revoke_slice_grant(ctx, todo_id, grant, "aborted")
        unclaim_todo(ctx.store, todo_id, lane.name)
        until = datetime.now(UTC) + _lanes.HOST_REFUSAL_BACKOFF
        ctx.set_meta(
            host_refusal={
                "lane": lane.name,
                "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "why": str(exc)[:300],
            }
        )
        _raise_lane_refusal(ctx.store, lane.name, str(exc), until)
        ctx.record_failure(f"run_slice: {exc}")
        return None
    except Exception as exc:
        log.warning("run_slice: launch of td%d failed", todo_id, exc_info=True)
        _revoke_slice_grant(ctx, todo_id, grant, "aborted")
        _fail_submit(ctx, todo_id, lane.name, f"launch failed: {exc}")
        return None

    token_note = (
        f"slice {grant['name']}"
        if grant
        else f"none ({mint_error})"
        if mint_error
        else "shared"
        if adapter.mcp
        else "none"
    )
    ctx.append_chunk(
        "job_event",
        f"run_slice: launched td{todo_id} on lane {lane.name} (model {model}, "
        f"effort {effort}) pid {pid} in {tree} "
        f"(mcp token: {token_note})",
    )
    # A launch on a lane that once refused is the probe that clears both
    # the backoff (this job is newer than the marked one) and the alert.
    _resolve_lane_refusal(ctx.store, lane.name)
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "worktree": str(tree),
        "log": str(log_path),
        "exit_file": str(exit_path),
        "started_at": started_at,
    }
    if atif_path is not None:
        handle["atif"] = str(atif_path)
    if grant:
        handle["slice_token_dir"] = grant["dir"]
        handle["slice_token_name"] = grant["name"]
        handle["agentlog_id"] = grant["agentlog_id"]
        if grant.get("devin_config"):
            handle["devin_config"] = grant["devin_config"]
    if identity is not None:
        # ssh_node's orphan reaper kills only a group whose leader still
        # has this boot_id and start-tick count; without them it skips.
        handle["boot_id"], handle["start_time"] = identity
    if ps_identity is not None:
        handle["ps_identity"] = ps_identity
    return handle


def _read_exit(handle: dict[str, Any]) -> int | None:
    path = Path(str(handle.get("exit_file") or ""))
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_log(handle: dict[str, Any]) -> str:
    try:
        return Path(str(handle.get("log") or "")).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""


def _branch_state(tree: Path) -> tuple[int | None, list[str]]:
    """``(uncommitted path count, commits ahead of origin/main)``."""
    status = _git(["status", "--porcelain"], tree)
    dirty = (
        len([ln for ln in status.stdout.splitlines() if ln.strip()])
        if status.returncode == 0
        else None
    )
    ahead = _git(["log", "--oneline", "origin/main..HEAD"], tree)
    commits = (
        [ln for ln in ahead.stdout.splitlines() if ln.strip()]
        if ahead.returncode == 0
        else []
    )
    return dirty, commits


#: Where a session leaves the commit message the brief asks for; a
#: sandboxed session that could not run ``git commit`` leaves it behind and
#: :func:`_commit_for_session` commits with it.
COMMIT_MSG_REL = Path("scratch") / "commit-msg.txt"
_SUBJECT_RE = re.compile(r"^[a-z]+(\([^)\s]+\))?!?: \S")
#: ``scripts/ship``'s subject hard limit, in CHARACTERS
#: (``ship_subject_lint`` counts UTF-8 code points and refuses past 120;
#: 72 is only its "aim for <=72" note). Used to be a 72-BYTE cut, which
#: mangled a subject the ship limit would have taken whole — mid-word
#: (gr574383).
_SUBJECT_CAP = 120


def cap_subject_chars(text: str, cap: int = _SUBJECT_CAP) -> str:
    """``text`` shortened to ``cap`` characters at the last word boundary
    inside the cut — never inside a word (gr574383: a mid-word cut turned
    "not" into "n" and the subject stopped saying what changed). A cut
    already landing on a boundary — the next character a space — keeps
    its complete final word. Only a text with no boundary inside the cap
    at all takes the hard character cut: ship's lint counts characters,
    and the limit still has to hold (td599723: exempting the boundary
    after the ``type(scope):`` prefix still cut a long first word)."""
    cut = text[: max(cap, 0)]
    if cap < len(text) and text[cap] != " ":
        boundary = cut.rfind(" ")
        if boundary > 0:
            cut = cut[:boundary]
    return cut.rstrip()


def _commit_message(tree: Path, todo_id: int) -> str | None:
    """The session's ``scratch/commit-msg.txt`` with a landable subject, or
    ``None`` when the file is missing or empty. A first line that is not a
    conventional subject is demoted to the body under a synthesised one, and
    an overlong subject is cut at :data:`_SUBJECT_CAP` characters at a
    word boundary: the lander squashes under that subject and refuses
    unprefixed ones."""
    try:
        text = (tree / COMMIT_MSG_REL).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    if not lines or not lines[0].strip():
        return None
    subject = lines[0].strip()
    body = lines[1:]
    if not _SUBJECT_RE.match(subject):
        body = [subject, *body]
        subject = f"fix(slice): td{todo_id} session commit"
    if len(subject) > _SUBJECT_CAP:
        subject = cap_subject_chars(subject)
    return "\n".join([subject, *body]).strip() + "\n"


def _commit_for_session(tree: Path, todo_id: int) -> str | None:
    """Commit the working tree a clean session left uncommitted, with its
    ``scratch/commit-msg.txt``. Returns a note for the summary, or ``None``
    when there was nothing to do (no message file, nothing staged).

    gr564261's first codex-direct run (jo565335): ``codex exec`` under
    ``--sandbox workspace-write`` keeps ``.git`` read-only, so the session
    changed eight files, wrote its message and reported that it could not
    create ``index.lock``; the poll then read 0 commits ahead as "nothing to
    land" and burned an attempt. The worker runs outside that sandbox, so
    it commits in the session's stead. The deny check ran on the working
    tree before this, so a denied path never reaches the commit."""
    message = _commit_message(tree, todo_id)
    if message is None:
        return None
    # scratch/ is gitignored on main, so the message file never rides along.
    add = _git(["add", "-A"], tree)
    if add.returncode != 0:
        return f"worker commit skipped: git add failed ({add.stderr.strip()[:200]})"
    staged = _git(["diff", "--cached", "--quiet"], tree)
    if staged.returncode == 0:
        return None
    # The normalised text, from a file outside the tree: the session's own
    # file keeps its bare or overlong subject, which the lander refuses.
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".txt", prefix="slice-commit-", delete=False
    ) as fh:
        fh.write(message)
        msg_path = Path(fh.name)
    try:
        commit = _git(["commit", "--quiet", "-F", str(msg_path)], tree)
    finally:
        msg_path.unlink(missing_ok=True)
    if commit.returncode != 0:
        return f"worker commit failed ({commit.stderr.strip()[:200]})"
    return f"{WORKER_COMMITTED} the session's working tree with {COMMIT_MSG_REL.as_posix()}"


def tests_ran(text: str | None) -> bool:
    """``True`` when the LAST ``tests:`` marker in ``text`` says ``ran``
    — the only receipt the quick lane takes (gr573563; Reto on gr573555:
    no receipt or a red one means the checked lane). No marker is
    ``False``: a hand-off without a ``tests:`` line lands checked, not
    quick — a pinned codex build that omits it included. A devin
    ``result`` is the whole transcript, so the last marker anywhere
    decides."""
    if not text:
        return False
    last = None
    for match in _TESTS_MARKER_RE.finditer(text):
        last = match
    return last is not None and last.group(1).lower() == "ran"


#: A ``moot:`` line anywhere in the session's result (the ``handoff:``
#: comment's source text — the clean source; a JSON-log adapter already
#: parsed it out, so a ``moot:`` inside the raw log's JSON strings never
#: reaches here, td584381 S1d). Position-free: unlike
#: :func:`generator_verdict` the line need not be first.
_MOOT_LINE_RE = re.compile(r"^\s*moot\s*:\s*(.*?)\s*$", re.IGNORECASE)
#: A sha candidate in the moot why (7–40 lowercase hex, whole token).
_MOOT_SHA_RE = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{7,40})(?![0-9A-Za-z])")
#: The gripe a moot line names (``gr<N>``); its text may name test files.
_MOOT_GRIPE_RE = re.compile(r"\bgr(\d+)\b", re.IGNORECASE)
#: A test-file path a gripe's text can name (``tests/….py``).
_MOOT_TEST_RE = re.compile(r"\btests/[\w./-]+\.py\b")
#: The release-fix loop's own marker (gr597599): on its fix node the
#: builder reports the digest's ids already green on ``origin/main``
#: (``verified: <ids> green on origin/main``) and the node closes on
#: the claim alone — the loop's next cut re-runs the legs, so a false
#: one cannot pass. Position-free like :data:`_MOOT_LINE_RE`.
_FIX_VERIFIED_RE = re.compile(r"^\s*verified\s*:\s*(.*?)\s*$", re.IGNORECASE)
_FIX_VERIFIED_GREEN_RE = re.compile(r"green on origin/main", re.IGNORECASE)


def _moot_line(result: str | None) -> str | None:
    """The first ``moot:`` line in the session's result, stripped."""
    for ln in (result or "").splitlines():
        if _MOOT_LINE_RE.match(ln):
            return ln.strip()
    return None


def _plan_root(store: Any, todo_id: int) -> int | None:
    """The ``plan:<id>`` open tag on the todo — its plan root — or ``None``."""
    for t in store.tags_for(todo_id):
        value = str(t.value)
        if value.startswith("plan:") and value[5:].isdigit():
            return int(value[5:])
    return None


def _blocked_by_ci_digest(store: Any, todo_id: int) -> list[int]:
    """Live todos ``todo_id`` is ``blocked-by`` whose ``meta.op`` is
    ``ci_red_digest`` — the release-fix loop's digest node
    (``ci_red_digest._fix_targets`` read the other way)."""
    pool = getattr(store, "pool", None)
    if pool is None:
        return []  # a store without the SQL surface expresses no links
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM links l JOIN refs r ON r.ref_id = l.dst_ref_id
             WHERE l.relation = 'blocked-by' AND l.src_ref_id = %s
               AND r.kind = 'todo' AND r.retired_at IS NULL
               AND (r.meta ->> 'op' = 'ci_red_digest'
                    OR r.meta -> 'op' ->> 'type' = 'ci_red_digest')
             ORDER BY r.ref_id
            """,
            (todo_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _fix_node(store: Any, todo_id: int) -> bool:
    """``todo_id`` is the release-fix loop's fix node: a ``run_slice``
    todo ``blocked-by`` a live ``ci_red_digest`` node under a
    ``release-pass`` root (the scope :func:`_fix_norepro` and
    :func:`_fix_moot` rule on)."""
    if not _blocked_by_ci_digest(store, todo_id):
        return False
    root = _plan_root(store, todo_id)
    return root is not None and "release-pass" in _open_tag_values(store, root)


def _fix_norepro(store: Any, todo_id: int) -> str | None:
    """The ``validated:`` non-repro ruling for ``todo_id`` (gr588028):

    ``"rerun"`` when it is the release-fix loop's fix node — a
    ``run_slice`` todo ``blocked-by`` a live ``ci_red_digest`` node under
    a ``release-pass`` root — and its run id's rerun has not fired;
    ``"spent"`` when it has (a second ``validated:`` on the same run id
    is a real red: the plain ``no_commit`` path owns it). ``None``
    otherwise — a ``validated:`` on any other node keeps its generic
    verdict close. An unresolvable run id reads ``"rerun"``: the tick's
    mint phase stalls on it rather than burning the attempt."""
    if not _fix_node(store, todo_id):
        return None
    rel = _release.release_of(store, todo_id)
    rid = _release.newest_red_run(store, str((rel or {}).get("sha") or ""))
    if rid and _release.ci_rerun_fired(store, rid):
        return "spent"
    return "rerun"


def _plan_chain_ids(store: Any, root: int, todo_id: int) -> set[int]:
    """The ``td`` ids a verified-moot commit message may name (td584381
    S1d review): the moot node itself, its plan root and every todo under
    the root — the factory's commit subjects carry ``(td<build>)``."""
    ids = {int(todo_id), int(root)}
    frontier = [int(root)]
    while frontier:
        parent = frontier.pop()
        for r in store.recent_refs(["todo"], parent_id=parent, limit=1000):
            rid = int(r.id)
            if rid not in ids:
                ids.add(rid)
                frontier.append(rid)
    return ids


def _fetch_main(tree: Path) -> None:
    """Best-effort ``git fetch origin main`` — a failure, a timeout or
    an ``OSError`` only means a later ancestor check runs on whatever
    ``origin/main`` ref exists."""
    try:
        _git(["fetch", "origin", "main"], tree)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _verified_moot(
    store: Any, tree: Path, result: str | None, todo_id: int
) -> dict[str, str] | None:
    """``{"sha", "line"}`` when the result's ``moot:`` claim checks out on
    the worker host (td584381 S1d), else ``None``.

    Verified means: after a best-effort ``git fetch origin main`` (a
    failure, a timeout or an ``OSError`` only means the check runs on
    whatever ref exists) the line names a sha that ``git merge-base
    --is-ancestor`` confirms on ``origin/main`` — AND that sha's own
    commit message (``git show -s --format=%B``) names the plan's gripe
    as a whole token: the ``gr<N>`` ``land_slice._plan_gripe`` resolves
    for the root, or a ``td<M>`` of its chain (the moot node, the root,
    any node under it — :func:`_plan_chain_ids`). Every sha-shaped token
    in the line is tried; the first satisfying both wins. A line naming
    a ``gr<N>`` different from the plan's gripe is rejected outright;
    one naming none verifies only through the message. When the plan
    gripe's text names test files every ``tests/….py`` must still exist
    on main (``git cat-file -e origin/main:<path>``) — an extra gate,
    never a verification by itself. No line, no plan tag, no plan
    gripe, a ``_plan_gripe`` raise, an unreadable gripe, no ancestor, a
    ``cat-file`` timeout/OSError or a message naming neither handle
    fails closed: the no-commit exit stays a failed attempt
    (S1a's straight-to-rescue path, the moot line in the evidence).
    """
    line = _moot_line(result)
    if line is None:
        return None
    root = _plan_root(store, todo_id)
    if root is None:
        return None
    from precis.workers.job_types import land_slice as _land_slice

    try:
        gid = _land_slice._plan_gripe(store, f"plan:{root}")
    except Exception:  # a DB fault on the plan gripe fails closed
        log.warning("run_slice: plan gripe lookup for td%s raised", todo_id)
        return None
    if gid is None:
        return None
    named = _MOOT_GRIPE_RE.search(line)
    if named is not None and int(named.group(1)) != gid:
        # A moot pinned to a different gripe can never be this plan's.
        return None
    try:
        paths = {
            p
            for c in store.chunks.list_chunks_for_ref(gid)
            for p in _MOOT_TEST_RE.findall(str(c.text or ""))
        }
    except Exception:  # an unreadable gripe fails closed
        log.warning("run_slice: moot gripe gr%s unreadable", gid)
        return None
    chain = sorted(_plan_chain_ids(store, root, todo_id))
    handles = [
        re.compile(rf"\bgr{gid}\b", re.IGNORECASE),
        re.compile(rf"\btd(?:{'|'.join(str(i) for i in chain)})\b", re.IGNORECASE),
    ]
    _fetch_main(tree)
    sha = None
    for cand in _MOOT_SHA_RE.findall(line):
        try:
            check = _git(["merge-base", "--is-ancestor", cand, "origin/main"], tree)
            if check.returncode != 0:
                continue
            msg = _git(["show", "-s", "--format=%B", cand], tree)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if msg.returncode != 0:
            continue
        if any(h.search(msg.stdout) for h in handles):
            sha = cand
            break
    if sha is None:
        return None
    for p in sorted(paths):
        try:
            if _git(["cat-file", "-e", f"origin/main:{p}"], tree).returncode != 0:
                return None
        except (OSError, subprocess.TimeoutExpired):
            # A cat-file that cannot answer fails closed like a miss.
            return None
    return {"sha": sha, "line": line[:300]}


def _posted_verdict(store: Any, todo_id: int) -> tuple[str, str] | None:
    """``(kind, why)`` of the verdict in the todo's newest comments
    (gr602896): the ``handoff:`` comment the poll just posted (the
    session's own final text) or a verdict line posted by hand,
    whichever is newest. Comments are read newest-first; the scan stops
    at the SECOND ``handoff:``/``brief:`` marker (the previous attempt's
    boundary) — a bare verdict comment between the two markers is a
    ruling on this attempt, anything older is not."""
    chunks = sorted(
        (
            c
            for c in _todo_chunks(store, todo_id)
            if c.chunk_kind == _COMMENT_KIND and c.text
        ),
        key=lambda c: (int(c.ord), int(getattr(c, "id", 0))),
        reverse=True,
    )
    boundary_seen = False
    for c in chunks:
        body = str(c.text).strip()
        lowered = body.lower()
        handoff = lowered.startswith("handoff:")
        if handoff or lowered.startswith("brief:") or body == _HANDOFF_WITHHELD:
            if boundary_seen:
                break
            boundary_seen = True
        hit = any_verdict(body[len("handoff:") :] if handoff else body)
        if hit is not None:
            return hit
    return None


def _foreign_moot(store: Any, todo_id: int, line: str) -> bool:
    """The ``moot:`` line pins the verdict to a ``gr<N>`` that is NOT the
    plan's gripe — it is another plan's statement, never this one's
    (gr602896: the S1d rejection, kept for the trusted close)."""
    named = _MOOT_GRIPE_RE.search(line)
    if named is None:
        return False
    root = _plan_root(store, todo_id)
    if root is None:
        return False
    from precis.workers.job_types import land_slice as _land_slice

    try:
        gid = _land_slice._plan_gripe(store, f"plan:{root}")
    except Exception:  # an unresolvable gripe proves no conflict
        log.warning("run_slice: plan gripe lookup for td%s raised", todo_id)
        return False
    return gid is not None and int(named.group(1)) != gid


def _fix_moot(tree: Path, result: str | None) -> dict[str, str] | None:
    """``{"kind", "line"}`` (plus ``"sha"`` on a moot) when the
    release-fix loop's marker lets the fix node close with no commit
    (gr597599), else ``None``.

    The marker is a ``verified: <ids> green on origin/main`` line — the
    builder asserts the digest's ids already pass on main and the
    loop's next cut is the check a false claim fails — or a ``moot:``
    line naming a sha :func:`_fetch_main` +
    ``git merge-base --is-ancestor`` confirms on ``origin/main``. The
    plan-gripe gates of :func:`_verified_moot` do NOT apply: the fix
    already on main landed under another plan's subject, so the
    message gate could never pass for the claim that is in fact true
    (r256's td597541). No marker, or a ``moot:`` whose shas are all
    non-ancestors or unreadable, is ``None`` — the no-commit failure
    :data:`NO_COMMIT_KEY` stamps stands."""
    for ln in (result or "").splitlines():
        m = _FIX_VERIFIED_RE.match(ln)
        if m is not None and _FIX_VERIFIED_GREEN_RE.search(m.group(1)):
            return {"kind": "verified", "line": ln.strip()[:300]}
    line = _moot_line(result)
    if line is None:
        return None
    _fetch_main(tree)
    for cand in _MOOT_SHA_RE.findall(line):
        try:
            check = _git(["merge-base", "--is-ancestor", cand, "origin/main"], tree)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if check.returncode == 0:
            return {"kind": "moot", "sha": cand, "line": line[:300]}
    return None


def changed_paths(tree: Path) -> list[str] | None:
    """Repo-relative paths the slice touched: the branch diff from its
    merge base with ``origin/main`` (renames split into delete + add) plus
    every working-tree change, untracked files included. ``None`` when git
    cannot answer (the deny check then fails closed)."""
    diff = _git(["diff", "--name-only", "--no-renames", "origin/main...HEAD"], tree)
    status = _git(["status", "--porcelain", "-z", "--untracked-files=all"], tree)
    if diff.returncode != 0 or status.returncode != 0:
        return None
    paths = {ln.strip() for ln in diff.stdout.splitlines() if ln.strip()}
    entries = status.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC" and i < len(entries):
            # -z puts a rename/copy's source path in the next entry.
            paths.add(entries[i])
            i += 1
    return sorted(p for p in paths if p)


def _deny_reason(
    tree: Path, deny_paths: tuple[str, ...]
) -> tuple[str | None, list[str]]:
    """``(why the branch breaks the deny paths, human-only paths hit)``.
    The check still runs on an empty ``deny_paths`` — human-only paths
    hold regardless (:func:`deny_violations`). The second element stamps
    :data:`HUMAN_ONLY_KEY` on the failed job (td584381 S1a)."""
    paths = changed_paths(tree)
    if paths is None:
        return (
            f"deny-path check could not read the branch in {tree} (git failed); "
            "failing closed",
            [],
        )
    hits = deny_violations(paths, deny_paths)
    if not hits:
        return None, []
    return (
        "touched deny_write_paths: " + ", ".join(f"`{p}`" for p in hits[:50]),
        _lanes.human_only_hits(paths),
    )


#: What the job summary shows where session text (a verdict's ``why``)
#: would go when a key value could not be re-read — the text is withheld,
#: never quoted.
_WITHHELD_TEXT = "withheld: credential unreadable at poll"


#: Failure classes that are the lane's window, not the task's verdict
#: (gr565359): a Claude quota notice, a 429. The router's classifier
#: (:mod:`precis.utils.llm.failure`) names them.
_OUTAGE_CLASSES = frozenset({"quota", "rate"})
#: Park horizon when the outage names no reset instant.
OUTAGE_BACKOFF = timedelta(hours=2)
#: Quota/rate parks one job may take before the next window closing counts
#: as a failed attempt (BUILDER on 5ce9e5d69: an unbounded park never
#: reaches the retry cap or the ask).
OUTAGE_PARK_CAP = 3
_NOT_BEFORE_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _todo_pins_lane(store: Any, todo_id: int) -> bool:
    """True when the todo's own ``meta.op.params.lane`` pins the lane (a
    human's or the triage's pin). A lane only in the job's params is the
    ladder's (``plan_dispatch._escalate``) and may be left on an outage."""
    todo = store.get_ref(kind="todo", id=todo_id)
    op = (getattr(todo, "meta", None) or {}).get("op")
    if not isinstance(op, dict):
        return False
    p = op.get("params")
    return isinstance(p, dict) and bool(p.get("lane"))


#: A ``cli-missing`` verdict needs the process's own report: exit 127,
#: or — when no exit code was recorded — a log short enough that the
#: miss IS the story. A real non-127 exit never qualifies: a 3-line
#: log under exit 1 is a failed session, not a missing binary (review
#: R2-4). A long session log ending in ``bash: pytest: command not
#: found`` is the task's tool either way (review B2).
_CLI_MISSING_LOG_LINES = 3

#: Bound on the result text the auth scan reads — the same whole-message
#: cap style as ``_MAX_QUOTA_TEXT_CHARS``: a short final message is the
#: CLI's own notice; a long answer quoting auth wording is session text.
_AUTH_TEXT_CAP = 240


def _cli_missing_on_tail(
    adapter: str, tail: str, exit_code: int | None, lines: int
) -> bool:
    """The lane's own binary missing, reported by the process itself:
    the tail names the adapter's binary in a not-found line AND the
    report is the process's own — exit 127, or no exit record under a
    near-empty log (the probe's own call); a real non-127 exit is the
    session's, never the lane's (review R2-4)."""
    if not re.search(r"command not found|no such file or directory", tail):
        return False
    if exit_code != 127 and (exit_code is not None or lines > _CLI_MISSING_LOG_LINES):
        return False
    try:
        binary = Path(adapter_bin(adapter)).name
    except KeyError:
        return False
    return bool(binary) and re.search(rf"\b{re.escape(binary)}\b", tail) is not None


def _lane_outage(
    result: str | None,
    log_text: str,
    *,
    adapter: str = "claude_p",
    exit_code: int | None = None,
) -> tuple[str, datetime | None, str] | None:
    """``(class, retry_at, key)`` when the session died of its lane's
    window or its credential — else ``None``.

    The lane's window first: the result wording classifies
    ``quota``/``rate``, or the CLI's final JSON carries
    ``api_error_status`` 429 (key ``""`` — no phrase). Its credential
    (gr588221, review B2): the auth allowlist
    (:func:`~precis.utils.llm.failure.lane_auth_key`) on STRUCTURED
    fields only — the final JSON's ``api_error`` marker always, and the
    result text only for ``claude_p`` — the CLI's own bounded final
    ``result`` under :data:`_AUTH_TEXT_CAP` (review R2-3: every other
    adapter's final message is session text — ``devin_tree``'s whole
    log, kimi's last assistant message, codex's ``last-message.md`` —
    and quoting auth-shaped prose must never park a lane) — then the
    401/403 status, then the lane's own binary reported missing by the
    process itself (exit 127, or no exit record under a near-empty log,
    never a line inside a session transcript). The ``key`` is the
    phrase mark (``not-logged-in``, ``http-401``, ``cli-missing``),
    never session text.

    jo565344: ``claude -p`` answered in 644 ms with a 429 and "You've hit
    your weekly limit · resets 11am (UTC)"; the poll read that as a failed
    attempt, which with the earlier sandbox miss hit the retry cap."""
    from precis.utils.llm.failure import classify_text, lane_auth_key

    text = result or ""
    status: Any = None
    api_error = ""
    lines = log_text.strip().splitlines() if log_text.strip() else []
    tail = lines[-1].strip() if lines else ""
    if tail.startswith("{"):
        try:
            obj = json.loads(tail)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            status = obj.get("api_error_status")
            extra = obj.get("api_error")
            if isinstance(extra, str) and extra:
                api_error = extra
                text = f"{text} {extra}".strip()
    found = classify_text(text)
    if found.reason_class in _OUTAGE_CLASSES:
        return str(found.reason_class), found.retry_at, ""
    if status == 429:
        return "rate", None, ""
    scan = api_error
    bounded = " ".join((result or "").split())
    if adapter == "claude_p" and bounded and len(bounded) <= _AUTH_TEXT_CAP:
        scan = f"{scan} {bounded}".strip()
    auth_key = lane_auth_key(scan) if scan else None
    if auth_key is not None:
        return "auth", None, auth_key
    if status in (401, 403):
        return "auth", None, f"http-{status}"
    if _cli_missing_on_tail(adapter, tail, exit_code, len(lines)):
        return "auth", None, "cli-missing"
    return None


def _outage_alternative(
    store: Any,
    todo_id: int,
    params: dict[str, Any],
    vendor: str,
    lanes: tuple[_lanes.Lane, ...],
) -> bool:
    """Whether another vendor's enabled lane fits this slice right now, so
    the relaunch need not wait for the window: never for a lane the todo
    itself pins or a generator (its MCP lane is the Claude one). A lane
    pinned only in the job's params is the retry ladder's pick
    (jo565689: the climb to claude-sub met the weekly 429 and waited for
    the reset although codex was free); the park leaves it."""
    del params
    if not vendor or _todo_pins_lane(store, todo_id):
        return False
    todo = store.get_ref(kind="todo", id=todo_id)
    if node_kind(todo.meta) == "generator":
        return False
    task_class = str((todo.meta or {}).get("task_class") or _DEFAULT_TASK_CLASS)
    others = tuple(ln for ln in lanes if ln.vendor != vendor)
    return _lanes.pick_lane(store, task_class, lanes=others) is not None


def _park_outage(
    ctx: Any,
    handle: dict[str, Any],
    todo_id: int,
    lane: str | None,
    cls: str,
    retry_at: datetime | None,
    phrase: str | None = None,
) -> bool:
    """The lane's window closed on the session: hand the lease back with
    no attempt burned, drop the dead process from the handle and park it
    with ``not_before`` — now, when another vendor's lane can take the
    slice (``meta.exclude_vendor`` steers the relaunch there), else the
    parsed reset (or :data:`OUTAGE_BACKOFF`). The job stays running, as
    land_slice's deferral does; :func:`_relaunch_when_due` resubmits.
    Always ``False``.

    An ``auth`` class (gr588221) is the LANE's credential, not a window:
    it also parks the lane itself (``parked_reason = auth:<phrase key>`` —
    the mark, never session text) so the whole fleet skips it until the
    ``lane_health`` probe's green result unparks it; ``enabled`` stays
    the human switch. The park stamp rides ``meta.attempt_free`` like
    ``lane-wait``."""
    now = datetime.now(UTC)
    if cls == "auth":
        _lanes.park_lane(ctx.store, str(lane), f"auth:{phrase or 'unclassified'}", now)
        ctx.set_meta(**{ATTEMPT_FREE_KEY: "lane-auth"})
    lane_table = _lanes.load_lanes(ctx.store)
    lane_obj = next((ln for ln in lane_table if ln.name == str(lane)), None)
    vendor = lane_obj.vendor if lane_obj is not None else ""
    params = (ctx.meta or {}).get("params") or {}
    unclaim_todo(ctx.store, todo_id, str(lane) if lane else None)
    _revoke_slice_grant(ctx, todo_id, _handle_grant(handle), "aborted")
    for key in ("pid", "pgid", "boot_id", "start_time", "ps_identity"):
        handle.pop(key, None)
    Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
    switch = _outage_alternative(ctx.store, todo_id, params, vendor, lane_table)
    if switch:
        due = now
        where = f"another vendor's lane (not {vendor})"
    else:
        due = (
            retry_at
            if retry_at is not None and retry_at > now
            else now + OUTAGE_BACKOFF
        )
        where = f"lane {lane} once its window resets"
    parks = int(handle.get("outage_parks") or 0) + 1
    handle.update(
        not_before=due.strftime(_NOT_BEFORE_FMT), outage=cls, outage_parks=parks
    )
    what = (
        f"auth failure (auth:{phrase or 'unclassified'}); lane parked"
        if cls == "auth"
        else f"{cls} window closed"
    )
    ctx.append_chunk(
        "job_event",
        f"run_slice: lane {lane} {what} on td{todo_id}; lease back "
        f"with no attempt burned; relaunch after {due:%Y-%m-%dT%H:%M}Z on {where} "
        f"(park {parks}/{OUTAGE_PARK_CAP})",
    )
    deadline = now.timestamp() + max((due - now).total_seconds(), 0.0) + wall_seconds()
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=deadline,
        exclude_vendor=vendor if switch else None,
    )
    return False


#: ``plan_dispatch.DEFAULT_ACU_USD``, restated — the dependency stays
#: one-way (the same rule as ``ATTEMPT_FREE_KEY``). ACU→USD for
#: ``meta.usage.acus_consumed`` when the lane's ``entitlement.acu_usd``
#: names no rate.
DEFAULT_ACU_USD = 2.0


def _lane_acu_usd(store: Any, lane: str | None) -> float:
    """The lane's ``entitlement.acu_usd`` rate, else
    :data:`DEFAULT_ACU_USD`; any read failure defaults too — the budget
    watch never takes a launch down over a config lookup."""
    try:
        from precis.utils.llm import accounts

        for a in accounts.list_accounts(store):
            if str(a.name) == str(lane):
                ent = a.entitlement if isinstance(a.entitlement, dict) else {}
                v = ent.get("acu_usd")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    f = float(v)
                    if math.isfinite(f) and f > 0:
                        return f
    except Exception:
        pass
    return DEFAULT_ACU_USD


def _usage_usd(usage: dict[str, Any] | None, store: Any, lane: str | None) -> float:
    """USD a ``meta.usage`` dict reports — the same shape
    ``plan_dispatch._job_spend_usd`` sums: ``on_demand_usd``/
    ``cost_usd`` direct, ``acus_consumed`` at the lane's rate."""
    if not isinstance(usage, dict):
        return 0.0
    total = 0.0
    for key in ("on_demand_usd", "cost_usd"):
        v = usage.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            f = float(v)
            if math.isfinite(f) and f > 0:
                total += f
    acus = usage.get("acus_consumed")
    if isinstance(acus, (int, float)) and not isinstance(acus, bool):
        f = float(acus)
        if math.isfinite(f) and f > 0:
            total += f * _lane_acu_usd(store, lane)
    return total


def _adapter_usage(
    ctx: Any, handle: dict[str, Any], log_text: str = ""
) -> dict[str, Any] | None:
    """The usage the session's adapter reports, stamped onto
    ``meta.usage`` — a live job's reported usage is real spend.
    ``log_text`` is the session log; the live check passes ``""`` (only
    devin's ATIF exists mid-run — every other vendor's usage lands at
    the end)."""
    adapter_name = str((ctx.meta or {}).get("adapter") or "")
    try:
        adapter = get_adapter(adapter_name)
    except KeyError:
        return None
    atif = Path(handle["atif"]) if handle.get("atif") else None
    usage = adapter.usage(
        log_text, Path(str(handle.get("worktree") or "")), export_path=atif
    )
    if usage:
        stamped = {
            **usage,
            "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        ctx.set_meta(usage=stamped)
        return stamped
    return None


#: Grace a SIGTERM'ed capped session gets before the watch escalates to
#: SIGKILL (td601426): a TERM the group ignored — or a signal that never
#: landed — ends the wait, never the enforcement.
_BUDGET_TERM_GRACE_S = 30.0

#: The ``started_at``/``budget_kill.at`` timestamp shape.
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _budget_bound(ctx: Any) -> float:
    """``params.budget_usd`` as an enforceable bound — the node's
    remaining allowance — or 0 when the job carries none."""
    params = (ctx.meta or {}).get("params") or {}
    raw = params.get("budget_usd")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return 0.0
    budget = float(raw)
    return budget if math.isfinite(budget) and budget > 0 else 0.0


def _watch_budget(ctx: Any, handle: dict[str, Any]) -> None:
    """The remaining-allowance bound (td601015): ``params.budget_usd``
    is the node's allowance ``plan_dispatch._escalate`` left after
    recorded spend — SIGTERM the session once its live usage meets it,
    so a job minted under the cap cannot run past it. The kill rides
    the handle as ``budget_kill``; the dead-process path then fails the
    attempt naming the bound — and even a clean exit after it is a
    failure, never a land (td601426). A group still answering
    liveness past :data:`_BUDGET_TERM_GRACE_S` — or a TERM that raised
    — escalates to SIGKILL, retried on every live pass: one signal is
    not the enforcement, termination is. Liveness is the group's, not
    the wrapper's — a TERM can kill the ``sh`` while a vendor process
    ignores it (td602769), and an exit file while the group still
    answers does not end the watch — the poll keeps it live while a
    bound applies (td603296). No live usage channel (a
    vendor reporting no spend, or none yet) means the bound simply
    cannot trip."""
    budget = _budget_bound(ctx)
    if budget <= 0:
        return
    pgid = _handle_pgid(handle)
    kill = handle.get("budget_kill")
    if isinstance(kill, dict):
        try:
            sent = datetime.strptime(str(kill.get("at") or ""), _TS_FMT).replace(
                tzinfo=UTC
            )
        except ValueError:
            sent = None
        if (
            kill.get("sig") == "term"
            and sent is not None
            and (datetime.now(UTC) - sent).total_seconds() < _BUDGET_TERM_GRACE_S
        ):
            return
        # The grace expired (or the TERM never landed): SIGKILL —
        # retried every live pass while the group keeps answering.
        if pgid:
            try:
                _signal_group(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass  # gone, or undelivered — retried on the next live pass
            else:
                if kill.get("sig") != "kill":
                    ctx.append_chunk(
                        "job_event",
                        "run_slice: capped session outlived SIGTERM "
                        f"(grace {_BUDGET_TERM_GRACE_S:.0f}s); SIGKILL "
                        "to the group",
                    )
                kill["sig"] = "kill"
                ctx.set_meta(compute_handle=handle)
        return
    usage = _adapter_usage(ctx, handle)
    usd = _usage_usd(usage, ctx.store, (ctx.meta or {}).get("lane"))
    if usd < budget:
        return
    kill = {
        "budget_usd": budget,
        "usd": round(usd, 4),
        "at": datetime.now(UTC).strftime(_TS_FMT),
        "sig": "none",
    }
    if pgid:
        try:
            _signal_group(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass  # gone is fine; undelivered escalates on the next pass
        else:
            kill["sig"] = "term"
    handle["budget_kill"] = kill
    ctx.set_meta(compute_handle=handle)
    ctx.append_chunk(
        "job_event",
        f"run_slice: session crossed its remaining budget "
        f"(${usd:.2f} ≥ ${budget:.2f}); SIGTERM to the group",
    )


def _relaunch_when_due(ctx: Any, handle: dict[str, Any]) -> bool:
    """Poll half of a parked outage: ``False`` before ``not_before``; at or
    after it, run :func:`submit` again (a fresh lane pick, the excluded
    vendor skipped) and persist the new handle in place. ``True`` only
    when submit drove the job terminal itself."""
    try:
        due = datetime.strptime(str(handle["not_before"]), _NOT_BEFORE_FMT).replace(
            tzinfo=UTC
        )
    except (KeyError, ValueError):
        due = datetime.now(UTC)  # unreadable marker: do not park forever
    if datetime.now(UTC) < due:
        return False
    fresh = submit(ctx, None)
    if fresh is None:
        return True
    parks = handle.get("outage_parks")
    handle.clear()
    handle.update(fresh)
    if parks:
        handle["outage_parks"] = parks  # the cap counts across relaunches
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=datetime.now(UTC).timestamp() + wall_seconds(),
        exclude_vendor=None,
        # The park stamp rides the park, not the job: a relaunched
        # session's real failure must count as an attempt (review S1).
        attempt_free=None,
    )
    return False


def _release_mint_holds(ctx: Any, handle: dict[str, Any], todo_id: int) -> list[int]:
    """td603429 follow-up (gr603945): the mint barrier stamps
    ``waiting-for:mint`` on every ``put`` landing inside a live plan and
    ``_generator_close`` never tells the session to remove it, so each
    minted child idled ``plan_dispatch.MINT_STALE`` (10 min) plus a
    hygiene tick on every generator run. A session that exits clean is
    the minter done writing: finish the settled mints under its plan
    root created in this job's window — plan_dispatch owns the settled
    rule and the sweep stays the fallback for anything less."""
    try:
        job = ctx.store.get_ref(kind="job", id=int(ctx.ref_id or 0))
    except Exception:
        job = None
    since = getattr(job, "created_at", None)
    if since is None:
        try:
            since = datetime.strptime(
                str(handle.get("started_at") or ""), _TS_FMT
            ).replace(tzinfo=UTC)
        except ValueError:
            return []
    try:
        from precis.workers.job_types import plan_dispatch as _pd

        return _pd.release_session_mints(ctx.store, todo_id, since)
    except Exception:  # pragma: no cover — the job outcome still lands
        log.warning(
            "run_slice: mint-hold release for td%d failed", todo_id, exc_info=True
        )
        return []


def poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half: ``False`` while the session runs; once it has
    exited, the handoff, the branch state and the terminal status."""
    if not isinstance(handle, dict):
        _drop_submit_key(ctx)
        ctx.record_failure(f"run_slice: malformed handle {handle!r}")
        return True
    if handle.get("not_before"):
        return _relaunch_when_due(ctx, handle)
    exit_code = _read_exit(handle)
    tree = Path(str(handle.get("worktree") or ""))
    vanished = not tree.is_dir()
    if not vanished and _session_alive(handle):
        _watch_budget(ctx, handle)
        # An exit file is the wrapper's end, not the group's (td603296):
        # while the group still answers and a bound applies — a stop
        # mid-escalation, or an allowance that can still trip — the
        # poll holds so the watch runs until the group is gone. An
        # uncapped session's survivor is the orphan reaper's.
        if (
            exit_code is None
            or isinstance(handle.get("budget_kill"), dict)
            or _budget_bound(ctx) > 0
        ):
            return False
    ctx.set_meta(compute_handle_released=True)

    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    lane = (ctx.meta or {}).get("lane")
    log_text = _read_log(handle)
    adapter_name = (ctx.meta or {}).get("adapter")
    if not adapter_name:
        # Jobs submitted before adapters existed carry only the lane name.
        lane_obj = _lanes.get_lane(str(lane), ctx.store) if lane else None
        adapter_name = lane_obj.adapter if lane_obj else "claude_p"
    # Scrub the child env's secrets before anything below stores log text
    # (handoff comment, job summary, failure tail): a session that runs
    # `env`/`printenv` puts its key in the log (td573681 review S1), and
    # a key rotated mid-run leaves the submit-time value to catch (round
    # 2). ``unscrubbed`` means a value the child may have carried could
    # not be re-read — the stored tail then drops the lines that could
    # carry it and the raw handoff is withheld, since it may quote the
    # unnameable value.
    pairs, unscrubbed = _child_env_secrets(
        str(adapter_name), ctx.store, job=getattr(ctx, "ref_id", None)
    )
    if pairs:
        log_text = _redact(log_text, pairs)
    try:
        adapter = get_adapter(str(adapter_name))
    except KeyError:
        _revoke_slice_grant(ctx, todo_id, _handle_grant(handle), "aborted")
        _fail_submit(
            ctx,
            todo_id,
            str(lane) if lane else None,
            f"lane {lane!r} has unknown adapter {adapter_name!r}",
        )
        return True
    result, is_error = adapter.parse_result(log_text, tree, exit_code=exit_code)
    _adapter_usage(ctx, handle, log_text)
    # A session the budget watch stopped (td601426): whatever its exit
    # code reads, the bound's stop is a failed attempt — no outage park
    # on its error text, no worker commit, no land mint.
    budget_kill = handle.get("budget_kill")
    budget_note = (
        (
            "crossed its remaining budget "
            f"(usage ${float(budget_kill.get('usd') or 0):.2f} ≥ "
            f"${float(budget_kill.get('budget_usd') or 0):.2f})"
        )
        if isinstance(budget_kill, dict)
        else None
    )
    if is_error and not vanished and budget_note is None:
        outage = _lane_outage(
            result, log_text, adapter=adapter.name, exit_code=exit_code
        )
        if outage is not None:
            parks = int(handle.get("outage_parks") or 0)
            if parks < OUTAGE_PARK_CAP:
                # The lane's window, not the task: no attempt, no handoff.
                return _park_outage(ctx, handle, todo_id, lane, *outage)
            # Parked and relaunched OUTAGE_PARK_CAP times already: this
            # window counts as a failed attempt, so the retry cap and the
            # ask can still fire.
            ctx.append_chunk(
                "job_event",
                f"run_slice: lane {lane} {outage[0]} window closed again after "
                f"{parks} parks on td{todo_id}; counting it as a failed attempt",
            )
    # Server-side deny check, before the handoff lands: independent of
    # whether the session honoured its deny rules (Bash writes included).
    denied, human_hits = _deny_reason(tree, _lanes.deny_paths_for(ctx.store))
    # Deny paths and commit lines are session text too (the session names
    # its files and writes its messages): the same scrub applies — redact
    # the known values; when a value could not be re-read, the entries
    # that could carry it drop.
    if pairs:
        denied = _redact(denied, pairs) if denied else denied
        human_hits = [_redact(hit, pairs) for hit in human_hits]
    if unscrubbed:
        human_hits = [
            hit
            for hit in human_hits
            if not any(name in hit for name in _KIMI_ENV_NAMES)
        ]
        if denied and any(name in denied for name in _KIMI_ENV_NAMES):
            denied = "touched deny_write_paths (path withheld)"
    if human_hits:
        # The failed attempt's durable mark (td584381 S1a): the tick
        # reads it on the ended job and asks instead of retrying.
        ctx.set_meta(**{HUMAN_ONLY_KEY: human_hits})

    handoff = (result or "").strip()
    if denied:
        handoff = f"attempt FAILED, {denied}.\n\n{handoff}".strip()
    elif budget_note is not None:
        handoff = f"attempt FAILED, {budget_note}.\n\n{handoff}".strip()
    if unscrubbed:
        # The raw result may quote a key the poll cannot name — the todo
        # gets the withheld comment instead of any session text (review
        # round 2); the failure record keeps the line-dropped tail.
        try:
            _append_comment(ctx.store, todo_id, _HANDOFF_WITHHELD)
        except Exception:  # pragma: no cover — the job outcome still lands
            log.warning("run_slice: withheld comment on td%d failed", todo_id)
    elif handoff:
        if len(handoff) > _HANDOFF_CAP:
            handoff = (
                handoff[:_HANDOFF_HEAD].rstrip()
                + " …(clipped)… "
                + handoff[-(_HANDOFF_CAP - _HANDOFF_HEAD) :].lstrip()
            )
        try:
            _append_comment(ctx.store, todo_id, f"handoff: {handoff}")
        except Exception:  # pragma: no cover — the job outcome still lands
            log.warning("run_slice: handoff comment on td%d failed", todo_id)

    committed = None
    if (
        exit_code == 0
        and not is_error
        and not denied
        and not vanished
        and budget_note is None
    ):
        if not _is_generator(ctx.store, todo_id):
            committed = _commit_for_session(tree, todo_id)
    if committed:
        if pairs:
            committed = _redact(committed, pairs)
        if unscrubbed and any(n in committed for n in _KIMI_ENV_NAMES):
            committed = "worker commit note withheld (credential unreadable)"
    dirty, commits = _branch_state(tree)
    if pairs:
        commits = [_redact(ln, pairs) for ln in commits]
    if unscrubbed:
        commits = [ln for ln in commits if not any(n in ln for n in _KIMI_ENV_NAMES)]
    ctx.set_meta(exit_code=exit_code, branch_dirty=dirty, branch_commits=commits[:50])
    vendor = "claude" if adapter.name == "claude_p" else adapter.name
    error_note = (
        f", {vendor} reported an error" if is_error and result is not None else ""
    )
    summary = [
        f"run_slice td{todo_id} on lane {lane}: exit "
        f"{'unknown (process gone, no exit status)' if exit_code is None else exit_code}"
        f"{error_note}.",
        f"Branch {slice_branch(todo_id)} in {tree}: "
        f"{'?' if dirty is None else dirty} uncommitted path(s), "
        f"{len(commits)} commit(s) ahead of origin/main.",
    ]
    if denied:
        summary.append(f"Deny check: {denied}.")
    if budget_note is not None:
        summary.append(f"Budget: {budget_note}.")
    if committed:
        summary.append(f"Commit: {committed}.")
    clean = exit_code == 0 and not is_error and not denied and budget_note is None
    if clean:
        # gr603945 (td603429 follow-up): a clean exit is the minter done
        # writing — finish the ``waiting-for:mint`` holds on the settled
        # children this job's window produced rather than idling them
        # for the MINT_STALE sweep.
        released = _release_mint_holds(ctx, handle, todo_id)
        if released:
            summary.append(
                "Mints: waiting-for:mint finished on "
                + ", ".join(f"td{i}" for i in released)
                + " (clean session end)."
            )
    # A clean task run hands its branch to the lander; a generator's work is
    # its child todos, so it has nothing to land.
    land = clean and not _is_generator(ctx.store, todo_id)
    nothing = None
    if land and not commits:
        verdict = generator_verdict(result)
        no_commit = _no_commit_kind(ctx.store, todo_id)
        # A ``moot:`` claim on a CODE task must verify before it may
        # close the plan (td584381 S1d): first line or anywhere in the
        # result — the ``handoff:`` comment's source text, already
        # parsed out of a JSON log. A document node keeps today's trust.
        # gr597599: the release-fix loop's fix node takes its own ruling
        # INSTEAD (:func:`_fix_moot`) — a verified claim there closes the
        # node untagged: the release plan is not moot, so
        # ``_verified_moot``'s ``verdict:moot`` + gripe stamp would kill
        # the pass the loop is fixing.
        fix_node = no_commit is None and _fix_node(ctx.store, todo_id)
        fix_moot = _fix_moot(tree, result) if fix_node else None
        verified = (
            _verified_moot(ctx.store, tree, result, todo_id)
            if no_commit is None and not fix_node
            else None
        )
        # gr588028: a ``validated:`` first line on the release-fix loop's
        # fix node reads "the red legs do not reproduce" — a CI flake,
        # not a diff. The tick reruns the failed legs once per run id
        # (:func:`_fix_norepro`); a ``"spent"`` ruling — the second
        # ``validated:`` on that run — falls to the ``no_commit`` arm so
        # the real red follows the existing path.
        norepro = (
            _fix_norepro(ctx.store, todo_id)
            if verdict is not None and verdict[0] == "validated" and no_commit is None
            else None
        )
        # gr602896: the fallback verdict read — anywhere in the result
        # (a lane may put prose over the line) or the newest comments
        # (a verdict posted by hand). The release-fix node is out: its
        # ``_fix_moot``/``norepro`` rulings own the verdict there — a
        # ``verdict:moot`` tag would kill the release plan it is fixing.
        found = (
            None
            if fix_node
            else any_verdict(result) or _posted_verdict(ctx.store, todo_id)
        )
        if (
            found is not None
            and found[0] == "moot"
            and no_commit is None
            and _foreign_moot(ctx.store, todo_id, f"{found[0]}: {found[1]}")
        ):
            # A ``moot:`` pinned to another gripe is not this plan's.
            found = None
        if verified is not None:
            # td584381 S1d (td584601): the session found the fix already
            # on main and named a sha the worker could verify. That is a
            # success with nothing to land: close done, tag for the tick
            # to close the plan, and stamp the sha so the tick resolves
            # the gripe exactly as a land does — no attempt, no ask, no
            # rescue.
            land = False
            line_ = _WITHHELD_TEXT if unscrubbed else verified["line"]
            close_todo(
                ctx.store,
                todo_id,
                str(lane) if lane else None,
                tags=(MOOT_TAG,),
            )
            ctx.set_meta(**{MOOT_VERIFIED_KEY: {"sha": verified["sha"], "line": line_}})
            summary.append(
                f"Land: none — verified moot (sha {verified['sha']} on "
                f"origin/main; `{line_[:160]}`); td{todo_id} "
                "closed and tagged for the tick to close the plan."
            )
        elif fix_moot is not None:
            # gr597599 (r256, td597541): the fixer found the digest's
            # ids already green on main and said so — ``moot: <sha>``
            # whose sha is an ancestor, or ``verified: <ids> green on
            # origin/main``. Success with nothing to land: close done —
            # no ``verdict:moot`` tag (the release plan is not moot;
            # the loop's recut re-checks the claim).
            land = False
            line_ = _WITHHELD_TEXT if unscrubbed else fix_moot["line"]
            close_todo(ctx.store, todo_id, str(lane) if lane else None)
            ctx.set_meta(**{FIX_VERIFIED_KEY: {**fix_moot, "line": line_}})
            summary.append(
                f"Land: none — the fix node's `{line_[:160]}` holds "
                f"({fix_moot['kind']}"
                + (
                    f", sha {fix_moot['sha']} on origin/main"
                    if fix_moot.get("sha")
                    else ""
                )
                + f"); td{todo_id} closed done."
            )
        elif norepro == "rerun":
            # Closing the fix node would unblock the recut while the run
            # still reads red — the r248 incident's hole; the generic
            # ``validated:`` close must not fire here. Park the node on
            # the tick's rerun step instead: the lease goes back with no
            # attempt tag and the job still succeeds (the stamp the tick
            # reads is on the job meta).
            kind_, why = verdict if verdict is not None else ("validated", "")
            land = False
            unclaim_todo(ctx.store, todo_id, str(lane) if lane else None)
            ctx.store.add_tag(todo_id, Tag.open(NOREPRO_WAIT_TAG), set_by="system")
            ctx.set_meta(
                **{
                    VALIDATED_NOREPRO_KEY: {
                        "line": f"{kind_}: {_WITHHELD_TEXT if unscrubbed else why}"[
                            :300
                        ]
                    },
                    ATTEMPT_FREE_KEY: "validated-norepro",
                }
            )
            summary.append(
                f"Land: none, by verdict `{kind_}: "
                f"{_WITHHELD_TEXT if unscrubbed else why[:200]}` on the "
                f"release-fix node; td{todo_id} parked on "
                f"{NOREPRO_WAIT_TAG} — the tick reruns the failed legs "
                "once per run id."
            )
        elif (
            verdict is not None
            and norepro is None
            and (verdict[0] != "moot" or no_commit is not None)
        ):
            # gr561132: a task that checked and found nothing to change (a
            # validation that passed, a fix already on main) says so on its
            # first line; that is a success with nothing to land, not a
            # failed attempt to retry and escalate. On a CODE node a
            # ``moot:`` is no verdict any more — it needs the verified sha
            # above (S1d); a document node keeps its trust.
            kind_, why = verdict
            moot = kind_ == "moot"
            land = False
            close_todo(
                ctx.store,
                todo_id,
                str(lane) if lane else None,
                tags=(MOOT_TAG,) if moot else (),
            )
            summary.append(
                f"Land: none, by verdict "
                f"`{kind_}: {_WITHHELD_TEXT if unscrubbed else why[:200]}`; "
                f"td{todo_id} "
                f"closed{' and tagged for the tick to close the plan' if moot else ''}."
            )
        elif found is not None:
            # gr602896: a verdict anywhere — a ``moot:`` the sha gates
            # could not verify (a cross-plan fix names its own plan's td,
            # never this one's gripe — jo602411), a verdict line under
            # prose, or one posted into the comments — still closes the
            # node. The S1d gates only decide whether the gripe may be
            # stamped ``resolved:`` on a sha; the verdict itself counts.
            kind_, why = found
            moot = kind_ == "moot"
            land = False
            close_todo(
                ctx.store,
                todo_id,
                str(lane) if lane else None,
                tags=(MOOT_TAG,) if moot else (),
            )
            summary.append(
                f"Land: none, by verdict "
                f"`{kind_}: {_WITHHELD_TEXT if unscrubbed else why[:200]}`; "
                f"td{todo_id} "
                f"closed{' and tagged for the tick to close the plan' if moot else ''}."
            )
        elif no_commit is not None:
            # gr570534 / jo570327: a brief-writer (or any node whose
            # class or role delivers a document, not a branch) leaves
            # no commit on purpose — that is its success, not a failed
            # attempt to retry.
            land = False
            close_todo(ctx.store, todo_id, str(lane) if lane else None)
            summary.append(
                f"Land: none — td{todo_id} is a {no_commit} node; a "
                "clean exit with no commit is its success. Closed done."
            )
        else:
            nothing = (
                f"left no commit on {slice_branch(todo_id)} ahead of "
                "origin/main (nothing to land)"
            )
            summary.append(f"Land: {nothing}.")
            # The durable mark the tick reads (td584381 S1a F4): one
            # such attempt exhausts the cap — rescue, no ladder.
            ctx.set_meta(**{NO_COMMIT_KEY: True})
    elif (
        clean
        and not land
        and _mints_children(ctx.store, todo_id)
        and _child_todo_count(ctx.store, todo_id) == 0
    ):
        verdict = generator_verdict(result)
        if verdict is None:
            # A child-minting generator that exits 0 with no children did
            # not do its job (jo546987: the session never reached the MCP
            # and said so in its handoff, yet exit 0 read as success and the
            # todo sat `doing` under its lease until the stale-claim sweep).
            # Count it as a failed attempt now, lease released.
            nothing = "created no child todos (a generator's work is its children)"
            summary.append(f"Children: {nothing}.")
        else:
            # jo548670 (gr548813): the session found the source already
            # resolved and correctly minted nothing. A `no-op:` / `moot:`
            # first line is that verdict: the generator closes done; on
            # `moot:` the tick closes the plan (MOOT_TAG).
            kind_, why = verdict
            moot = kind_ == "moot"
            close_todo(
                ctx.store,
                todo_id,
                str(lane) if lane else None,
                tags=(MOOT_TAG,) if moot else (),
            )
            summary.append(
                f"Children: none, by verdict "
                f"`{kind_}: {_WITHHELD_TEXT if unscrubbed else why[:200]}`; "
                f"td{todo_id} "
                f"closed{' and tagged for the tick to close the plan' if moot else ''}."
            )
    elif land:
        # gr573563 (Reto on gr573555): the quick lane wants the builder's
        # receipt — the last ``tests:`` marker saying ``ran``. Land
        # checked when the worker committed the tree the builder left
        # (no git, so no tests either) or the hand-off carries no green
        # marker. ``--checked`` re-runs the impacted tests before land.
        checked = (
            committed is not None and committed.startswith(WORKER_COMMITTED)
        ) or not tests_ran(result)
        try:
            land_job = _mint_land(ctx, todo_id, tree, lane, checked=checked)
        except Exception as exc:
            log.warning("run_slice: land_slice mint for td%d failed", todo_id)
            nothing = f"exited clean but the land_slice mint failed ({exc})"
        else:
            ctx.set_meta(land_job=land_job)
            summary.append(f"Land: land_slice job:{land_job} minted.")
    if commits:
        summary += ["", *commits[:20]]
    ctx.append_chunk("job_summary", "\n".join(summary))

    grant = _handle_grant(handle)
    if clean and nothing is None:
        _revoke_slice_grant(ctx, todo_id, grant, "ok")
        ctx.set_status("succeeded")
        return True

    _revoke_slice_grant(ctx, todo_id, grant, "failed")
    attempt = None
    try:
        attempt = release_todo(ctx.store, todo_id, str(lane) if lane else None)
    except Exception:  # pragma: no cover — defensive; the job still fails
        log.warning("run_slice: lease release on td%d failed", todo_id, exc_info=True)
    tail = log_text[-_TAIL_CAP:].strip()
    if unscrubbed:
        tail = _drop_secret_lines(tail)
    if vanished:
        gone = f"worktree {tree} vanished while the session ran (reaped?)"
    else:
        gone = None
    what = budget_note or denied or nothing or gone or f"failed (exit {exit_code})"
    reason = (
        f"run_slice: session for td{todo_id} {what}; todo back to open, "
        f"attempt:{attempt}"
    )
    if tail:
        reason = f"{reason}\n--- log tail ---\n{tail}"
    ctx.record_failure(reason)
    return True


def release(ctx: Any, handle: Any) -> None:
    """Release the failed run's lease/grant without signaling any process."""
    _drop_submit_key(ctx)
    meta = getattr(ctx, "meta", None) or {}
    todo_id = int((meta.get("params") or {}).get("todo_id") or 0)
    grant = _handle_grant(handle)
    if grant is not None:
        try:
            _revoke_slice_grant(ctx, todo_id, grant, "aborted")
        except Exception:  # pragma: no cover — the lease still releases
            log.warning("run_slice: slice-token revoke on release failed")
    lane = meta.get("lane")
    if lane and f"claimed-by:lane-{lane}" not in _open_tag_values(ctx.store, todo_id):
        return
    if todo_id and _todo_status(ctx.store, todo_id) == "doing":
        release_todo(ctx.store, todo_id, str(lane) if lane else None)


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: revoke the grant and SIGTERM."""
    _drop_submit_key(ctx)
    if not isinstance(handle, dict):
        return
    grant = _handle_grant(handle)
    if grant is not None:
        params = (getattr(ctx, "meta", None) or {}).get("params") or {}
        try:
            _revoke_slice_grant(ctx, int(params.get("todo_id") or 0), grant, "aborted")
        except Exception:  # pragma: no cover — the signal still goes
            log.warning("run_slice: slice-token revoke on kill failed")
    pgid = _handle_pgid(handle)
    if not pgid:
        return
    try:
        _signal_group(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "ENV_ALLOWLIST",
    "NAME",
    "PARAMS_SCHEMA",
    "PERMISSION_MODE",
    "REQUIRES",
    "cap_subject_chars",
    "changed_paths",
    "deny_settings",
    "deny_violations",
    "factory_off",
    "kill",
    "parse_result",
    "poll",
    "release",
    "release_todo",
    "render_brief",
    "run_dir",
    "slice_branch",
    "slice_env",
    "slice_root",
    "submit",
    "validate_submit",
    "wall_seconds",
]
