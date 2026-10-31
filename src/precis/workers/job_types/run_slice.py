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
  ``brief:``/``handoff:`` comment + the land instruction), picks the lane
  (:func:`precis.plans.lanes.pick_lane`) unless ``params.lane`` names one,
  stamps ``meta.lane``/``meta.model``/``meta.effort`` on the job, takes
  the lease on the todo (``claimed-by:lane-<lane>`` + ``STATUS:doing``),
  cuts worktree ``slice-td<id>`` on branch ``slice/<id>`` from
  ``origin/main`` under the checkout root (:func:`slice_root`), writes
  ``scratch/brief.md`` there and launches ``claude -p --output-format json
  --max-turns <n> --model <m> --permission-mode bypassPermissions
  --settings <deny rules>`` detached with the brief on stdin. The env is
  an allowlist (:func:`slice_env`): no DB DSN, no vault variables reach
  the session. The handle is ``{pid, pgid, worktree, log, exit_file,
  started_at}`` plus ``boot_id``/``start_time`` (the Linux process
  identity ssh_node's orphan reaper checks) when ``/proc`` has them.
* :func:`poll` waits for the exit file (or a dead pid), checks the branch
  against the lane's ``deny_write_paths`` (:func:`deny_violations`),
  posts the session's last message as a ``handoff:`` comment on the todo,
  records the branch state (dirty count, commits ahead of
  ``origin/main``) in the job summary, and ends the job ``succeeded`` on
  exit 0, ``failed`` with the log tail otherwise. A failure (a deny-path
  hit included) releases the lease: the claim tag goes, the todo returns
  to ``STATUS:open`` and gains ``attempt:<n>``.
* :func:`kill` SIGTERMs the session's process group.

Deny paths are enforced twice: the session gets them as ``Edit``/
``Write``/``MultiEdit``/``NotebookEdit`` deny rules through ``--settings``
(they hold under ``bypassPermissions``), and :func:`poll` re-checks the
branch diff and the working tree, which also catches a write made through
Bash. The poll check runs after the session, so it cannot stop a land the
session already made; it fails the attempt and reopens the todo.

The land is the slice's own: the brief ends with ``scripts/ship --quick``
and the ``landed:`` comment + ``STATUS:done``, so the todo closes through
the ship, not here. A generator node is the exception: its brief ends with
"create the child todos" and no land, and the plan tick closes it once its
children are closed (it reopens one a session marked done too early).

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.plans import lanes as _lanes
from precis.plans.nodes import node_kind
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag

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
#: Cap on the handoff comment and the failure tail.
_HANDOFF_CAP = 6000
_TAIL_CAP = 2000

_ATTEMPT_RE = re.compile(r"^attempt:(\d+)$")

#: The headless session's permission mode (Reto's ruling on td542236:
#: bypass, as plan_tick runs, bounded by the lane's deny_write_paths).
PERMISSION_MODE = "bypassPermissions"

#: File-writing tools each deny path is denied for.
_DENY_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit", "NotebookEdit")

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
        # claude model id/alias; unset → PRECIS_RUN_SLICE_MODEL, else the
        # router's FRONTIER pin.
        "model": {"type": "string"},
        # Recorded on meta.effort and exported as PRECIS_SLICE_EFFORT.
        "effort": {"type": "string"},
        # ssh_node's host pin (the host holding the lane's credentials).
        "target_node": {"type": "string"},
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

#: Empty: ssh_node's static PROVIDES is ``{has_gpaw}``; the claude binary
#: and the checkout are the pinned host's, checked at submit.
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Launch one slice (a todo with a brief) headless on a lane: claude -p in "
    "its own worktree slice-td<id> on branch slice/<id> cut from origin/main, "
    "meta.lane/model/effort on the job, the session's last message posted as "
    "a handoff: comment on the todo; a branch touching the lane's "
    "deny_write_paths fails the attempt. Refused while PRECIS_FACTORY=off."
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


def resolve_model(params: dict[str, Any]) -> str:
    """``params.model``, else ``PRECIS_RUN_SLICE_MODEL``, else the router's
    ``Tier.FRONTIER`` pin."""
    model = params.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    env = os.environ.get("PRECIS_RUN_SLICE_MODEL", "").strip()
    if env:
        return env
    from precis.utils.llm.router import Tier
    from precis.utils.llm.router import resolve_model as _router_model

    return _router_model(Tier.FRONTIER)


def claude_bin() -> str:
    """``PRECIS_CLAUDE_BIN`` or ``claude`` on PATH (the agent lanes' seam)."""
    return os.environ.get("PRECIS_CLAUDE_BIN") or "claude"


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


def slice_branch(todo_id: int) -> str:
    """Branch of a slice: ``slice/<id>`` (spec § "Branches")."""
    return f"slice/{todo_id}"


def slice_env(todo_id: int, effort: str) -> dict[str, str]:
    """The session's environment: :data:`ENV_ALLOWLIST` names present in
    the worker's env, the MCP token (env, else the mounted secret), and
    ``PRECIS_SLICE_ID``/``PRECIS_SLICE_EFFORT``. Nothing else."""
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if os.environ.get(k)}
    if "PRECIS_MCP_TOKEN" not in env:
        try:
            from precis.secrets import mounted_secret

            token = mounted_secret("PRECIS_MCP_TOKEN")
        except Exception:  # pragma: no cover — no secret store on this host
            token = None
        if token:
            env["PRECIS_MCP_TOKEN"] = token
    env["PRECIS_SLICE_ID"] = f"td{todo_id}"
    env["PRECIS_SLICE_EFFORT"] = effort
    return env


def deny_settings(deny_paths: tuple[str, ...], tree: Path) -> str:
    """The ``--settings`` JSON for the session: one deny rule per tool in
    :data:`_DENY_TOOLS` per path, spelled both cwd-relative (``./<glob>``)
    and absolute (``//<tree>/<glob>``) so the rule matches whichever base
    the CLI resolves a flag-supplied setting against."""
    rules: list[str] = []
    for path in deny_paths:
        for spec in (f"./{path}", f"/{tree.as_posix()}/{path}"):
            rules += [f"{tool}({spec})" for tool in _DENY_TOOLS]
    return json.dumps({"permissions": {"deny": rules}})


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
    stdin: Path,
    log: Path,
    exit_file: Path,
    env: dict[str, str],
) -> int:
    """Start ``argv`` detached in its own session: stdin from ``stdin``,
    stdout+stderr to ``log``, its exit code written to ``exit_file`` by the
    wrapping shell (so a poll from a restarted worker, which is no longer
    the parent, still reads it). Returns the pid (= the process group)."""
    script = '"$@" < "$RS_IN" > "$RS_LOG" 2>&1; echo $? > "$RS_EXIT"'
    full_env = {
        **env,
        "RS_IN": str(stdin),
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


def _process_alive(pid: int) -> bool:
    """``True`` while ``pid`` exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_group(pgid: int, sig: int) -> None:
    """Send ``sig`` to the process group ``pgid``."""
    os.killpg(pgid, sig)


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


# ── Brief ──────────────────────────────────────────────────────────


def _generator_close(todo_id: int) -> list[str]:
    """Closing steps of a generator brief: mint children, never close."""
    return [
        "",
        "## When done: create the child todos",
        "",
        "This todo is a generator: your work is the child todos, not a land.",
        "",
        "1. Create each child with `put(kind='todo', text=..., body=..., "
        "meta={'llm_tier': None, ...}, "
        f"args={{'parent_id': {todo_id}}})`.",
        "2. Add `blocked-by` links between the children for any sequence.",
        "3. Then stop. Do not tag this todo done: the dispatcher closes it "
        "when its children finish.",
        "",
        "Your final message is posted on the todo as the `handoff:` comment: "
        "the children you created (ids) and anything left open.",
    ]


def render_brief(
    *,
    todo_id: int,
    title: str,
    body: str,
    latest: str | None,
    lane: str,
    model: str,
    effort: str,
    generator: bool = False,
    deny_paths: tuple[str, ...] = _lanes.FACTORY_DENY_WRITE_PATHS,
) -> str:
    """The prompt a slice session starts from. Ends with the land
    instruction, so the slice closes its own todo; a ``generator`` brief
    ends with the child-todo instruction instead (the plan tick closes it).
    Names the ``deny_paths`` the session may not write."""
    name = slice_name(todo_id)
    lines = [
        f"# Slice td{todo_id}: {title.strip()}",
        "",
        f"You are a headless build session on lane `{lane}` (model `{model}`, "
        f"effort `{effort}`), in git worktree `{name}` on branch "
        f"`{slice_branch(todo_id)}`, cut from `origin/main`. Work only in this "
        "worktree. Read CLAUDE.md and AGENTS.md here before editing; their "
        "rules bind you.",
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
        lines += _generator_close(todo_id)
        return "\n".join(lines) + "\n"
    lines += [
        "",
        "## When done: land it yourself",
        "",
        "1. Commit with a conventional subject "
        f"(`type(scope): what (td{todo_id})`), message via "
        "`git commit -F scratch/commit-msg.txt`.",
        '2. Run `scripts/ship --quick "<the subject>" > scratch/ship.log 2>&1` '
        "and check `SHIP_STATUS: shipped` in the log; on red lint, fix, amend "
        "and re-run.",
        "3. Get the sha with `git log origin/main --oneline -1`, then "
        f"`put(kind='todo', id={todo_id}, text='landed: <sha> — <one line>')`.",
        f"4. `tag(kind='todo', id={todo_id}, add=['STATUS:done'])`.",
        "",
        "If you cannot land, do not mark the todo done: stop, and say why.",
        "Your final message is posted on the todo as the `handoff:` comment the "
        "next session starts from: what landed (sha), what is left, what you "
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
            if defer is not None and defer(None, note):
                return None
            ctx.record_failure(note)
            return None

    model = resolve_model(params)
    effort = str(params.get("effort") or _DEFAULT_EFFORT)
    ctx.set_meta(lane=lane.name, model=model, effort=effort)
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
        deny_paths=lane.deny_write_paths,
    )
    try:
        tree = _ensure_worktree(slice_root(), todo_id)
        scratch = tree / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        brief_path = scratch / "brief.md"
        brief_path.write_text(brief, encoding="utf-8")
        log_path = scratch / "run_slice.log"
        exit_path = scratch / "run_slice.exit"
        exit_path.unlink(missing_ok=True)
        argv = [
            claude_bin(),
            "-p",
            "--output-format",
            "json",
            "--max-turns",
            str(_max_turns()),
            "--model",
            model,
            "--permission-mode",
            PERMISSION_MODE,
            "--settings",
            deny_settings(lane.deny_write_paths, tree),
        ]
        pid = _launch(
            argv,
            cwd=tree,
            stdin=brief_path,
            log=log_path,
            exit_file=exit_path,
            env=slice_env(todo_id, effort),
        )
        started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        identity = _process_identity(pid)
    except Exception as exc:
        log.warning("run_slice: launch of td%d failed", todo_id, exc_info=True)
        _fail_submit(ctx, todo_id, lane.name, f"launch failed: {exc}")
        return None

    ctx.append_chunk(
        "job_event",
        f"run_slice: launched td{todo_id} on lane {lane.name} (model {model}, "
        f"effort {effort}) pid {pid} in {tree}",
    )
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "worktree": str(tree),
        "log": str(log_path),
        "exit_file": str(exit_path),
        "started_at": started_at,
    }
    if identity is not None:
        # ssh_node's orphan reaper kills only a group whose leader still
        # has this boot_id and start-tick count; without them it skips.
        handle["boot_id"], handle["start_time"] = identity
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


def parse_result(log_text: str) -> tuple[str | None, bool]:
    """``(last assistant message, is_error)`` from ``claude -p
    --output-format json`` output: the last JSON object line carrying a
    ``result``. ``(None, True)`` when there is none."""
    for line in reversed(log_text.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "result" in obj:
            return str(obj.get("result") or ""), bool(obj.get("is_error"))
    return None, True


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
    if exit_code is None and _process_alive(int(handle.get("pid") or 0)):
        return False
    ctx.set_meta(compute_handle_released=True)

    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    lane = (ctx.meta or {}).get("lane")
    log_text = _read_log(handle)
    result, is_error = parse_result(log_text)
    tree = Path(str(handle.get("worktree") or ""))
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
    summary = [
        f"run_slice td{todo_id} on lane {lane}: exit "
        f"{'unknown (process gone, no exit status)' if exit_code is None else exit_code}"
        f"{', claude reported an error' if is_error and result is not None else ''}.",
        f"Branch {slice_branch(todo_id)} in {tree}: "
        f"{'?' if dirty is None else dirty} uncommitted path(s), "
        f"{len(commits)} commit(s) ahead of origin/main.",
    ]
    if denied:
        summary.append(f"Deny check: {denied}.")
    if commits:
        summary += ["", *commits[:20]]
    ctx.append_chunk("job_summary", "\n".join(summary))

    if exit_code == 0 and not is_error and not denied:
        ctx.set_status("succeeded")
        return True

    attempt = None
    try:
        attempt = release_todo(ctx.store, todo_id, str(lane) if lane else None)
    except Exception:  # pragma: no cover — defensive; the job still fails
        log.warning("run_slice: lease release on td%d failed", todo_id, exc_info=True)
    tail = log_text[-_TAIL_CAP:].strip()
    what = denied if denied else f"failed (exit {exit_code})"
    reason = (
        f"run_slice: session for td{todo_id} {what}; todo back to open, "
        f"attempt:{attempt}"
    )
    if tail:
        reason = f"{reason}\n--- log tail ---\n{tail}"
    ctx.record_failure(reason)
    return True


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: SIGTERM the session's process group."""
    del ctx
    if not isinstance(handle, dict):
        return
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
    "release_todo",
    "render_brief",
    "slice_branch",
    "slice_env",
    "slice_root",
    "submit",
    "validate_submit",
    "wall_seconds",
]
