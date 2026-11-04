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
  (:func:`precis.plans.lanes.pick_lane`) unless ``params.lane`` names one,
  stamps ``meta.lane``/``meta.adapter``/``meta.model``/``meta.effort`` on
  the job, takes the lease on the todo (``claimed-by:lane-<lane>`` + ``STATUS:doing``),
  cuts worktree ``slice-td<id>`` on branch ``slice/<id>`` from
  ``origin/main`` under the checkout root (:func:`slice_root`), writes
  ``scratch/brief.md`` there and launches the lane's adapter (``claude_p``,
  ``codex_exec`` or ``devin_tree``) detached. Claude's argv and MCP grant
  are unchanged; the other vendors receive no precis MCP token. The env is
  an allowlist (:func:`slice_env`): no DB DSN, no vault variables reach
  the session. The handle is ``{pid, pgid, worktree, log, exit_file,
  started_at}`` plus ``boot_id``/``start_time`` (the Linux process
  identity ssh_node's orphan reaper checks) when ``/proc`` has them.
  On Darwin it carries ``ps_identity`` (start time + user); polling
  requires a recorded identity to match, since a reused pid can pass
  signal 0. Legacy Darwin handles cannot use EPERM as evidence of liveness.
* :func:`poll` waits for the exit file (or a dead pid), checks the branch
  against the lane's ``deny_write_paths`` (:func:`deny_violations`),
  posts the session's last message as a ``handoff:`` comment on the todo,
  records the branch state (dirty count, commits ahead of
  ``origin/main``) in the job summary. On a clean exit (0, no claude
  error, no deny hit) with commits on the branch it mints one
  ``land_slice`` job (:mod:`precis.workers.job_types.land_slice`; idem key
  ``land_slice:td<id>``, a child of the todo, params ``{todo_id, worktree,
  branch, lane, target_node}``) and ends ``succeeded``; the lease stays
  until the land. Otherwise (non-zero exit, a deny-path hit, no commit to
  land) it ends ``failed`` with the log tail and releases the lease: the
  claim tag goes, the todo returns to ``STATUS:open`` and gains
  ``attempt:<n>``.
* :func:`kill` SIGTERMs the session's process group. :func:`release`
  revokes the MCP grant and releases the todo even when the poison
  guard cannot safely signal a process (poll may never run).

Deny paths are enforced twice for Claude: the session gets them as ``Edit``/
``Write``/``MultiEdit``/``NotebookEdit`` deny rules through ``--settings``
(they hold under ``bypassPermissions``), and :func:`poll` re-checks the
branch diff and the working tree, which also catches a write made through
Bash. Every vendor receives the paths in its brief and gets the poll
check. The poll check is pre-land: the session only commits on its branch,
and nothing is landed until the check passed.

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
import os
import re
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.plans import lanes as _lanes
from precis.plans.nodes import MOOT_TAG, generator_verdict, node_kind
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.workers.job_types._slice_adapters import (
    PERMISSION_MODE,
    deny_settings,
    get_adapter,
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
#: Cap on the handoff comment and the failure tail.
_HANDOFF_CAP = 6000
_TAIL_CAP = 2000

_ATTEMPT_RE = re.compile(r"^attempt:(\d+)$")

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
    consult their secret stores."""
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
    glob (``fnmatch``: ``*`` spans ``/``, so ``dir/**`` is the subtree)."""
    return sorted(
        {p for p in paths if any(fnmatch.fnmatchcase(p, g) for g in deny_paths)}
    )


def _lane_deny_paths(lane_name: Any) -> tuple[str, ...]:
    lane = _lanes.get_lane(str(lane_name)) if lane_name else None
    return lane.deny_write_paths if lane else _lanes.FACTORY_DENY_WRITE_PATHS


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


def _session_alive(handle: dict[str, Any]) -> bool:
    pid = int(handle.get("pid") or 0)
    if pid <= 1:
        return False
    if "ps_identity" in handle:
        expected = handle.get("ps_identity")
        if not expected or _darwin_process_identity(pid) != expected:
            return False
        return _process_alive(pid, identity_matches=True)
    # If ps failed at submit there is no ps_identity, so Darwin falls
    # back to the legacy rule: EPERM without matching identity is not ours.
    return _process_alive(pid)


def _signal_group(pgid: int, sig: int) -> None:
    """Send ``sig`` to the process group ``pgid``."""
    os.killpg(pgid, sig)


def _mint_land(ctx: Any, todo_id: int, tree: Path, lane: Any) -> int:
    """Mint the ``land_slice`` job for the slice (idem key
    ``land_slice:td<id>``, a child of the todo, pinned to this run's host so
    the claimant holds the worktree). Returns its id."""
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
    deliverable is child todos. A reviewer or analyst (``_ROLE_CLOSE``)
    delivers one comment instead and never mints."""
    ref = store.get_ref(kind="todo", id=todo_id)
    if ref is None or node_kind(ref.meta) != "generator":
        return False
    gen = (ref.meta or {}).get("generate")
    role = gen.get("role") if isinstance(gen, dict) else None
    return role not in _ROLE_CLOSE


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
        [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": _COMMENT_KIND})],
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


def _reviewer_close(todo_id: int) -> list[str]:
    """Closing steps of a reviewer generator's brief: one verdict comment,
    nothing minted, nothing committed, nothing landed. The grammar is the
    one ``plan_dispatch._VERDICT_RE`` parses."""
    return [
        "",
        "## When done: post your verdict",
        "",
        "This todo is a reviewer: your work is one verdict, not a land and "
        "not child todos.",
        "",
        f"Post exactly one comment on td{todo_id} (the node you were given), one of:",
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
        "Your final message is posted as the `handoff:` comment: what you "
        "checked, in a line or two.",
    ]


def _analyst_close(todo_id: int) -> list[str]:
    """Closing steps of an analyst generator's brief (PR analysis): one
    ``analysis:`` comment, nothing minted, nothing committed, nothing run.
    The prefix is the one ``plan_dispatch._analysis_posted`` looks for."""
    from precis.plans.templates import ANALYSIS_PREFIX

    return [
        "",
        "## When done: post your analysis",
        "",
        "This todo is an analyst: your work is one analysis, not a land and "
        "not child todos.",
        "",
        f"Post exactly one comment on td{todo_id} (the node you were given), "
        f"starting with `{ANALYSIS_PREFIX}`, with three parts: (a) threats, "
        "(b) fit, (c) size.",
        "",
        "Mint nothing: no child todos, no fix task. Commit nothing. Never "
        "run `uv sync`, tests or any file from the PR: read it with "
        "`git fetch` and `git diff` only. Do not land, push or ship. Do not "
        "tag this todo done: the dispatcher closes it once your comment is "
        "posted.",
        "",
        "Your final message is posted as the `handoff:` comment: what you "
        "read, in a line or two.",
    ]


#: Generator role -> closing-section builder; a role not listed gets
#: :func:`_generator_close` (create the child todos). A third role is one line.
_ROLE_CLOSE: dict[str, Callable[[int], list[str]]] = {
    "reviewer": _reviewer_close,
    "analyst": _analyst_close,
}


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
    deny_paths: tuple[str, ...] = _lanes.FACTORY_DENY_WRITE_PATHS,
) -> str:
    """The prompt a slice session starts from. Ends with the commit-and-
    hand-off instruction (the ``land_slice`` job lands the branch); a
    ``generator`` brief ends with the child-todo instruction instead (the
    plan tick closes it), and a generator whose ``role`` is in ``_ROLE_CLOSE``
    (reviewer: one verdict comment; analyst: one analysis comment) ends with
    that role's instruction. Names the ``deny_paths`` the session may not
    write."""
    name = slice_name(todo_id)
    lines = [
        f"# Slice td{todo_id}: {title.strip()}",
        "",
        f"You are a headless build session on lane `{lane}` "
        f"(model `{model or 'account default'}`, "
        f"effort `{effort}`), in git worktree `{name}` on branch "
        f"`{slice_branch(todo_id)}`, cut from `origin/main`. Work only in this "
        "worktree. Read CLAUDE.md and AGENTS.md here before editing; their "
        "rules bind you.",
    ]
    if not mcp:
        lines += [
            "",
            "This lane has no precis MCP. Do not try to reach it. "
            "Everything you want recorded goes in your final message.",
        ]
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
    if generator:
        close = _ROLE_CLOSE.get(role or "")
        if close is not None:
            lines += close(todo_id)
        else:
            lane_obj = _lanes.get_lane(lane)
            lines += _generator_close(
                todo_id, lane_obj.models if lane_obj is not None else ()
            )
        return "\n".join(lines) + "\n"
    lines += [
        "",
        "## When done: commit on the branch and hand off",
        "",
        f"1. Commit on `{slice_branch(todo_id)}` with a conventional subject "
        f"(`type(scope): what (td{todo_id})`, at most 72 bytes), message via "
        "`git commit -F scratch/commit-msg.txt`. One commit is best: the "
        "lander squashes the branch under that subject.",
        "2. Leave the working tree clean (`git status --porcelain` prints nothing).",
        "3. Stop. Do not land, push, comment `landed:` or tag this todo done: "
        "once you exit cleanly, the lander lands every commit on the branch, "
        "so commit only work fit to land.",
        "",
        "Nothing to commit on purpose? Say so as the FIRST line of your final "
        "message: `validated: <why>` when the check you were asked for passed, "
        "`no-op: <why>` when there is nothing to change and the plan should go "
        "on, `moot: <why>` when the plan's reason is gone (already fixed, "
        "closed). The todo then closes done. Any other commit-less exit counts "
        "as a failed attempt and is retried.",
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
    lane_name = params.get("lane")
    if lane_name is not None:
        lane = _lanes.get_lane(str(lane_name))
        known = [ln.name for ln in _lanes.LANES if ln.enabled]
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
    tree = root / ".claude" / "worktrees" / slice_name(todo_id)
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


def _mint_slice_token(
    ctx: Any, todo_id: int, *, model: str, brief: str
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
        token = slice_tokens.mint(directory, name, agentlog_id=log_id)
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
    agentlog.close_run(ctx.store, grant.get("agentlog_id"), status=status)


def _fail_submit(ctx: Any, todo_id: int, lane: str | None, reason: str) -> None:
    attempt = None
    try:
        attempt = release_todo(ctx.store, todo_id, lane)
    except Exception:  # pragma: no cover — defensive; the job still fails
        log.warning("run_slice: lease release on td%d failed", todo_id, exc_info=True)
    suffix = f" (todo td{todo_id} back to open, attempt:{attempt})" if attempt else ""
    ctx.record_failure(f"run_slice: {reason}{suffix}")


def submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half (see the module docstring). Returns the handle,
    or ``None`` after driving the job terminal (or parking it when every
    fit lane is full)."""
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
    if params.get("lane"):
        lane = _lanes.get_lane(str(params["lane"]))
        if lane is None or not lane.enabled:
            ctx.record_failure(
                f"run_slice: lane {params['lane']!r} unknown or disabled"
            )
            return None
    else:
        lane = _lanes.pick_lane(store, task_class)
        if lane is None:
            note = (
                f"run_slice: no lane free for task class {task_class!r} "
                "(every fit lane at its cap)"
            )
            defer = getattr(ctx, "defer", None)
            if defer is not None and defer(datetime.now(UTC) + LANE_FULL_BACKOFF, note):
                return None
            ctx.record_failure(note)
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
    model = resolve_model(params, lane)
    effort = str(params.get("effort") or _DEFAULT_EFFORT)
    ctx.set_meta(lane=lane.name, adapter=adapter.name, model=model, effort=effort)
    _claim_todo(store, todo_id, lane.name)

    body, latest = _brief_parts(store, todo_id)
    brief = render_brief(
        todo_id=todo_id,
        title=str(todo.title or ""),
        body=body,
        latest=latest,
        lane=lane.name,
        model=model,
        effort=effort,
        generator=node_kind(todo.meta) == "generator",
        role=_generate_role(todo.meta),
        mcp=adapter.mcp,
        deny_paths=lane.deny_write_paths,
    )
    grant: dict[str, Any] | None = None
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
        argv = adapter.argv(
            model=model,
            effort=effort,
            tree=tree,
            brief_path=brief_path,
            deny_paths=lane.deny_write_paths,
            max_turns=_max_turns(),
            models=lane.models,
        )
        if adapter.name == "codex_exec":
            # A retry reuses the tree: never accept a previous run's handoff.
            (scratch / "last-message.md").unlink(missing_ok=True)
        if adapter.mcp:
            assert model is not None
            grant = _mint_slice_token(ctx, todo_id, model=model, brief=brief)
        env = slice_env(
            todo_id,
            effort,
            mcp_token=grant["token"] if grant else None,
            mcp=adapter.mcp,
        )
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
    except Exception as exc:
        log.warning("run_slice: launch of td%d failed", todo_id, exc_info=True)
        _revoke_slice_grant(ctx, todo_id, grant, "aborted")
        _fail_submit(ctx, todo_id, lane.name, f"launch failed: {exc}")
        return None

    token_note = (
        f"slice {grant['name']}" if grant else "shared" if adapter.mcp else "none"
    )
    ctx.append_chunk(
        "job_event",
        f"run_slice: launched td{todo_id} on lane {lane.name} (model {model}, "
        f"effort {effort}) pid {pid} in {tree} "
        f"(mcp token: {token_note})",
    )
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "worktree": str(tree),
        "log": str(log_path),
        "exit_file": str(exit_path),
        "started_at": started_at,
    }
    if grant:
        handle["slice_token_dir"] = grant["dir"]
        handle["slice_token_name"] = grant["name"]
        handle["agentlog_id"] = grant["agentlog_id"]
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


def _deny_reason(tree: Path, deny_paths: tuple[str, ...]) -> str | None:
    """Why the slice's branch breaks its lane's deny paths, or ``None``."""
    if not deny_paths:
        return None
    paths = changed_paths(tree)
    if paths is None:
        return (
            f"deny-path check could not read the branch in {tree} (git failed); "
            "failing closed"
        )
    hits = deny_violations(paths, deny_paths)
    if not hits:
        return None
    return "touched deny_write_paths: " + ", ".join(f"`{p}`" for p in hits[:50])


def poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half: ``False`` while the session runs; once it has
    exited, the handoff, the branch state and the terminal status."""
    if not isinstance(handle, dict):
        ctx.record_failure(f"run_slice: malformed handle {handle!r}")
        return True
    exit_code = _read_exit(handle)
    tree = Path(str(handle.get("worktree") or ""))
    vanished = not tree.is_dir()
    if exit_code is None and not vanished and _session_alive(handle):
        return False
    ctx.set_meta(compute_handle_released=True)

    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    lane = (ctx.meta or {}).get("lane")
    log_text = _read_log(handle)
    adapter_name = (ctx.meta or {}).get("adapter")
    if not adapter_name:
        # Jobs submitted before adapters existed carry only the lane name.
        lane_obj = _lanes.get_lane(str(lane)) if lane else None
        adapter_name = lane_obj.adapter if lane_obj else "claude_p"
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
    # Server-side deny check, before the handoff lands: independent of
    # whether the session honoured its deny rules (Bash writes included).
    denied = _deny_reason(tree, _lane_deny_paths(lane))

    handoff = (result or "").strip()
    if denied:
        handoff = f"attempt FAILED, {denied}.\n\n{handoff}".strip()
    if handoff:
        if len(handoff) > _HANDOFF_CAP:
            handoff = handoff[:_HANDOFF_CAP].rstrip() + " …(clipped)"
        try:
            _append_comment(ctx.store, todo_id, f"handoff: {handoff}")
        except Exception:  # pragma: no cover — the job outcome still lands
            log.warning("run_slice: handoff comment on td%d failed", todo_id)

    dirty, commits = _branch_state(tree)
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
    clean = exit_code == 0 and not is_error and not denied
    # A clean task run hands its branch to the lander; a generator's work is
    # its child todos, so it has nothing to land.
    land = clean and not _is_generator(ctx.store, todo_id)
    nothing = None
    if land and not commits:
        verdict = generator_verdict(result)
        if verdict is None:
            nothing = (
                f"left no commit on {slice_branch(todo_id)} ahead of origin/main "
                "(nothing to land)"
            )
            summary.append(f"Land: {nothing}.")
        else:
            # gr561132: a task that checked and found nothing to change (a
            # validation that passed, a fix already on main) says so on its
            # first line; that is a success with nothing to land, not a
            # failed attempt to retry and escalate.
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
                f"Land: none, by verdict `{kind_}: {why[:200]}`; td{todo_id} "
                f"closed{' and tagged for the tick to close the plan' if moot else ''}."
            )
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
                f"Children: none, by verdict `{kind_}: {why[:200]}`; td{todo_id} "
                f"closed{' and tagged for the tick to close the plan' if moot else ''}."
            )
    elif land:
        try:
            land_job = _mint_land(ctx, todo_id, tree, lane)
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
    if vanished:
        gone = f"worktree {tree} vanished while the session ran (reaped?)"
    else:
        gone = None
    what = denied or nothing or gone or f"failed (exit {exit_code})"
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
    if not isinstance(handle, dict):
        return
    grant = _handle_grant(handle)
    if grant is not None:
        params = (getattr(ctx, "meta", None) or {}).get("params") or {}
        try:
            _revoke_slice_grant(ctx, int(params.get("todo_id") or 0), grant, "aborted")
        except Exception:  # pragma: no cover — the signal still goes
            log.warning("run_slice: slice-token revoke on kill failed")
    pgid = int(handle.get("pgid") or handle.get("pid") or 0)
    if pgid <= 1:
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
