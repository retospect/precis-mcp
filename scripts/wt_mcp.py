"""Per-worktree precis MCP server (dev-loop-scratch D1) — the library behind
``scripts/wt-mcp``.

One git worktree gets its own ``precis serve`` behind its own Caddy proxy,
started through the SAME ensure script as the shared session server
(``~/work/infrastructure/precis-mcp/scripts/precis-mcp-http-ensure.sh``,
repo copy ``deploy/mcp-http/``) with these overrides::

    PRECIS_MCP_HTTP_NAME=precis-wt-<tree>   PRECIS_MCP_HTTP_PORT=880N
    PRECIS_MCP_HTTP_STATE=~/.cache/precis-mcp-wt/<tree>
    PRECIS_MCP_SRC=<worktree>  PRECIS_MCP_REPO=<main checkout>
    PRECIS_MCP_WORKTREE=<tree>

The server is read-write on prod with the shared server's role, serves the
worktree's code and skills, and in a ``scripts/wt-mcp claude`` session it
REPLACES the shared server under the same name ``precis``
(``claude --mcp-config <generated> --strict-mcp-config``), so the session
has exactly one precis and the tool names stay ``mcp__precis__*``.

Safety, enforced here and tested in ``tests/test_wt_mcp.py``:

* every container, network, volume or ensure-script target this module
  names must start with ``precis-wt-`` (:func:`assert_wt_target`); the
  shared ``precis-mcp-http`` family is never started, stopped, restarted or
  reconfigured from here — not even by accident of a typo in a tree name;
* the generated MCP config's bearer header is the env reference
  ``${PRECIS_MCP_TOKEN}`` (gr515184): written mode 600 to
  ``~/.cache/precis-mcp-wt/<tree>/mcp.json`` (outside the checkout), never
  printed or logged; this module never reads the token itself (the ensure
  script's ``--config`` output goes straight to the file, and the value
  reaches the ``claude`` child only through its environment);
* a cap of :data:`MAX_SERVERS` live worktree servers, one port each from
  :data:`PORT_RANGE`; one tree root per server name;
* the migration guard fails closed: ``up``, every watcher ``--restart`` and
  every recovery ensure refuse a tree carrying a migration file prod has
  not applied (committed, staged or untracked; ``origin/prod`` lacks it),
  and any git failure is a refusal — ``scripts/dev`` is the lane for that.
  The server's own schema gate refuses at boot as the second line, with
  auto-migrate pinned off in worktree mode;
* the ensure script must carry worktree mode; an older copy is refused
  rather than serving the main checkout.

Test seams (env): ``PRECIS_WT_ENSURE`` (ensure script path),
``PRECIS_WT_STATE_HOME`` (ensure state dirs, default
``~/.cache/precis-mcp-wt``), ``PRECIS_WT_LIVE_STATE`` (the shared server's
state dir, read only to seed the md vector cache and python-roots).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

WT_PREFIX = "precis-wt-"
LIVE_NAME = "precis-mcp-http"
PORT_RANGE = range(8801, 8810)
MAX_SERVERS = 9
_TARGET_RE = re.compile(r"^precis-wt-[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PORT_RE = re.compile(r"127\.0\.0\.1:(\d+)->")

#: The ensure script's repo path; the installed copy lives in the
#: infrastructure checkout and is updated by ``scripts/mcp-http-install``.
ENSURE_REL = Path("deploy") / "mcp-http" / "precis-mcp-http-ensure.sh"
_MIGRATION_RE = re.compile(r"(^|/)migrations/[^/]+\.sql$")
#: Bound on every docker call: a loaded daemon must not wedge a teardown.
DOCKER_TIMEOUT_S = 180.0


class WtMcpError(Exception):
    """A refusal with a next step; ``main`` prints it and exits 1."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_wt_target(name: str) -> str:
    """Return ``name`` if it is a worktree-server name; raise otherwise.

    The one guard every docker/ensure call goes through. ``precis-mcp-http``
    and anything else outside ``precis-wt-<tree>`` is refused, so this
    module cannot touch the shared session server however it is invoked.
    """
    if not _TARGET_RE.match(name):
        raise WtMcpError(
            f"refusing target {name!r}: wt-mcp only operates on "
            f"{WT_PREFIX}<tree> servers, never {LIVE_NAME}"
        )
    return name


def tree_name(root: Path) -> str:
    """Docker-safe tree name from the worktree's directory name."""
    raw = root.resolve().name
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-.")
    if not name:
        raise WtMcpError(f"cannot derive a tree name from {root}")
    return name[:48]


#: ensure script may build/pull images; generous but finite.
ENSURE_TIMEOUT_S = 1800


def is_worktree(root: Path) -> bool:
    """A linked worktree has a ``.git`` *file* (``gitdir: …``)."""
    return (root / ".git").is_file()


def main_checkout(root: Path) -> Path:
    """The primary checkout (parent of the common git dir)."""
    out = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    ).stdout.strip()
    common = Path(out)
    if not common.is_absolute():
        common = (root / common).resolve()
    return common.resolve().parent


@dataclass(frozen=True)
class Tree:
    """One worktree and the paths its server uses."""

    root: Path
    name: str
    main: Path

    @classmethod
    def at(cls, root: Path) -> Tree:
        root = root.resolve()
        if not is_worktree(root):
            raise WtMcpError(
                f"{root} is not a linked git worktree; the primary checkout "
                f"already has the shared {LIVE_NAME} server"
            )
        return cls(root=root, name=tree_name(root), main=main_checkout(root))

    @classmethod
    def for_root(cls, root: Path, main: Path) -> Tree:
        """A tree addressed by path only (``down --root``, the reaper): no
        git lookups, so a half-removed or non-worktree path still names the
        same ``precis-wt-<tree>`` family to tear down."""
        root = root.resolve()
        return cls(root=root, name=tree_name(root), main=main.resolve())

    @property
    def server(self) -> str:
        return assert_wt_target(f"{WT_PREFIX}{self.name}")

    @property
    def state_dir(self) -> Path:  # gitignored: .claude/* is
        return self.root / ".claude" / "wt-mcp"

    @property
    def port_file(self) -> Path:
        return self.state_dir / "port"

    @property
    def config_file(self) -> Path:
        # Outside the repo tree on purpose: it holds the bearer token, and the
        # public-repo secret gate (tests/test_deploy_tree_no_secrets.py) walks
        # the whole checkout, gitignored files included.
        return self.ensure_state / "mcp.json"

    @property
    def watch_pid_file(self) -> Path:
        return self.state_dir / "watch.pid"

    @property
    def watch_log(self) -> Path:
        return self.state_dir / "watch.log"

    @property
    def ensure_state(self) -> Path:
        home = os.environ.get("PRECIS_WT_STATE_HOME") or str(
            Path.home() / ".cache" / "precis-mcp-wt"
        )
        return Path(home) / self.name


# ── ensure script ──────────────────────────────────────────────────────────


def ensure_script(tree: Tree) -> Path:
    """The ensure script to run: ``PRECIS_WT_ENSURE``, else the tree's own
    repo copy, else the main checkout's.

    Whichever it is, it must carry worktree mode (the ``PRECIS_MCP_WORKTREE``
    handling): an older script would silently serve the MAIN checkout with
    the schema gate at ``warn`` and report success (security review of
    2d84ee9bf, item 1). Refused with the two ways out.
    """
    raw = os.environ.get("PRECIS_WT_ENSURE")
    candidates = (
        [Path(raw)] if raw else [tree.root / ENSURE_REL, tree.main / ENSURE_REL]
    )
    path = next((p for p in candidates if p.is_file()), candidates[0])
    if not path.is_file():
        raise WtMcpError(
            f"ensure script not found: {path} — set PRECIS_WT_ENSURE to a copy "
            f"of {ENSURE_REL} that has worktree mode"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise WtMcpError(f"ensure script unreadable: {path}: {e}") from e
    if "PRECIS_MCP_WORKTREE" not in text:
        raise WtMcpError(
            f"ensure script {path} has no worktree mode (no PRECIS_MCP_WORKTREE "
            "handling): it would serve the main checkout instead of this tree. "
            f"next: PRECIS_WT_ENSURE=<repo>/{ENSURE_REL}, or install the repo "
            "copy with `scripts/mcp-http-install --apply`"
        )
    return path


#: Rig and override knobs the ensure script honours; a worktree server must
#: never inherit one from the shell (a fake backend, another image, another
#: DB address, a different /main) — review item 6.
_ENV_DROP = (
    "PRECIS_MCP_LIVE",
    "PRECIS_MCP_RUN_CMD",
    "PRECIS_MCP_ENTRYPOINT",
    "PRECIS_MCP_IMAGE",
    "PRECIS_MCP_DB_IP",
    "PRECIS_MCP_CADDY_EXTRA",
    "PRECIS_MCP_CADDY_IMAGE",
    "PRECIS_MCP_MAIN",
)


def ensure_env(tree: Tree, port: int) -> dict[str, str]:
    """The worktree-mode overrides on top of the caller's environment."""
    env = {k: v for k, v in os.environ.items() if k not in _ENV_DROP}
    env.update(
        {
            "PRECIS_MCP_HTTP_NAME": tree.server,
            "PRECIS_MCP_HTTP_PORT": str(port),
            "PRECIS_MCP_HTTP_STATE": str(tree.ensure_state),
            "PRECIS_MCP_SRC": str(tree.root),
            "PRECIS_MCP_REPO": str(tree.main),
            "PRECIS_MCP_WORKTREE": tree.name,
        }
    )
    return env


def run_ensure(
    tree: Tree,
    port: int,
    mode: str | None = None,
    *,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the ensure script in worktree mode. The name is asserted first."""
    assert_wt_target(tree.server)
    if mode not in (None, "--status", "--restart", "--recreate", "--config"):
        raise WtMcpError(f"ensure mode {mode!r} is not one wt-mcp issues")
    cmd = ["bash", str(ensure_script(tree))]
    if mode:
        cmd.append(mode)
    return subprocess.run(
        cmd,
        env=ensure_env(tree, port),
        capture_output=capture,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=ENSURE_TIMEOUT_S,
    )


# ── docker (listing only; every mutating call names an asserted target) ────


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        raise WtMcpError("docker is not on PATH")
    try:
        return subprocess.run(
            [docker, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=DOCKER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise WtMcpError(
            f"docker {' '.join(args[:2])} did not finish within {DOCKER_TIMEOUT_S:.0f}s"
        ) from e


def wt_containers() -> list[tuple[str, str]]:
    """``(name, ports)`` of every ``precis-wt-*`` container, running or not."""
    out = _docker(
        "ps",
        "-a",
        "--filter",
        f"name=^/{WT_PREFIX}",
        "--format",
        "{{.Names}}\t{{.Ports}}",
    )
    if out.returncode != 0:
        # Fail closed: a daemon that cannot answer is not "no containers" —
        # `--restart unless-stopped` backends would come back after a reap.
        raise WtMcpError(
            f"docker ps exited {out.returncode}: {out.stderr.strip()[-200:] or 'no output'}"
            " — cannot tell which worktree servers exist"
        )
    rows: list[tuple[str, str]] = []
    for line in out.stdout.splitlines():
        name, _, ports = line.partition("\t")
        if name.startswith(WT_PREFIX):
            rows.append((name, ports))
    return rows


def family(server: str) -> frozenset[str]:
    """The three container names of one worktree server, asserted."""
    return frozenset(assert_wt_target(server + s) for s in ("-proxy", "-a", "-b"))


def live_wt_proxies() -> list[str]:
    out = _docker(
        "ps",
        "--filter",
        f"name=^/{WT_PREFIX}",
        "--filter",
        "status=running",
        "--format",
        "{{.Names}}",
    )
    return [
        n
        for n in out.stdout.split()
        if n.startswith(WT_PREFIX) and n.endswith("-proxy")
    ]


def _port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(tree: Tree) -> int:
    """This tree's recorded port, else the lowest free one in the range."""
    if tree.port_file.is_file():
        try:
            port = int(tree.port_file.read_text(encoding="utf-8").strip())
        except ValueError:
            port = 0
        if port in PORT_RANGE:
            return port
    taken: set[int] = set()
    own = family(tree.server)
    for name, ports in wt_containers():
        if name in own:  # exact names: `dl-x-extra` is not `dl-x`'s family
            continue
        taken.update(int(m) for m in _PORT_RE.findall(ports))
    for port in PORT_RANGE:
        if port not in taken and _port_free(port):
            return port
    raise WtMcpError(
        f"no free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}; "
        "run `scripts/wt-mcp down` in a tree you are done with"
    )


def enforce_cap(tree: Tree) -> None:
    others = [p for p in live_wt_proxies() if p != f"{tree.server}-proxy"]
    if len(others) >= MAX_SERVERS:
        raise WtMcpError(
            f"{len(others)} worktree servers are live (cap {MAX_SERVERS}); "
            "run `scripts/wt-mcp down` in a tree you are done with"
        )


# ── guards ─────────────────────────────────────────────────────────────────


def _git_lines(root: Path, *args: str) -> list[str]:
    """``git`` output lines in ``root``; any failure is a refusal (fail closed:
    this server is prod read-write, so "could not check" means "no")."""
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WtMcpError(f"migration guard: git {args[0]} failed in {root}: {e}") from e
    if res.returncode != 0:
        raise WtMcpError(
            f"migration guard: `git {' '.join(args)}` exited {res.returncode} in "
            f"{root}: {res.stderr.strip()[-200:] or 'no output'} — refusing to serve "
            "a tree whose migrations cannot be compared with origin/prod"
        )
    return res.stdout.splitlines()


def unapplied_migrations(root: Path) -> list[str]:
    """Migration files in the tree that ``origin/prod`` does not carry.

    ``origin/prod`` is the sha the cluster runs (scripts/deploy moves it), so
    a migration absent there is one prod has not applied. The tree side is
    ``git ls-files --cached --others --exclude-standard``: committed, staged
    and untracked files alike. Any git failure — ``origin/prod`` missing, a
    broken checkout, a timeout — raises rather than returning ``[]``.
    """
    here = {
        p
        for p in _git_lines(
            root, "ls-files", "--cached", "--others", "--exclude-standard"
        )
        if _MIGRATION_RE.search(p)
    }
    prod = {
        p
        for p in _git_lines(root, "ls-tree", "-r", "--name-only", "origin/prod")
        if _MIGRATION_RE.search(p)
    }
    return sorted(here - prod)


def migration_guard(tree: Tree) -> None:
    """Refuse (raise) when the tree carries a migration prod has not applied.

    Runs on ``up``, before every watcher ``--restart`` and before every
    recovery ensure: a migration that appears while the server is up must
    not reach a running prod read-write server either.
    """
    files = unapplied_migrations(tree.root)
    if files:
        raise WtMcpError(
            f"{tree.name} carries migration(s) prod has not applied: "
            f"{', '.join(files)} — a worktree server runs on prod's DB; "
            "use `scripts/dev` (the dev DB) for this tree until it lands"
        )


def claim_root(tree: Tree) -> None:
    """Record which tree root owns ``precis-wt-<name>``; refuse a second root.

    Two worktrees with the same directory name under different parents would
    otherwise share one server family and one state dir (review item 4).
    """
    tree.ensure_state.mkdir(parents=True, exist_ok=True)
    tree.ensure_state.chmod(0o700)
    marker = tree.ensure_state / "root"
    if marker.is_file():
        owner = marker.read_text(encoding="utf-8").strip()
        if owner and owner != str(tree.root):
            raise WtMcpError(
                f"{tree.server} already belongs to {owner}; this tree is "
                f"{tree.root}. Run `scripts/wt-mcp down --root {owner}` there "
                "first, or rename this worktree directory"
            )
    marker.write_text(f"{tree.root}\n", encoding="utf-8")


# ── config (token never enters this process's output) ──────────────────────


def write_config(tree: Tree, port: int) -> Path:
    """``<ensure state>/mcp.json`` naming the server ``precis``, mode 600.

    The ensure script's ``--config`` prints the client entry (URL + the
    ``${PRECIS_MCP_TOKEN}`` bearer reference, gr515184); it is parsed and
    wrapped into Claude Code's ``mcpServers`` shape and written with the
    file created 0600 before any byte lands. Nothing from it is logged or
    returned. It lives in the tree's ensure state dir under ``~/.cache``,
    never inside the checkout.
    """
    res = run_ensure(tree, port, "--config", capture=True)
    if res.returncode != 0:
        raise WtMcpError(
            f"ensure --config failed (exit {res.returncode}): {res.stderr.strip()}"
        )
    try:
        entry = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise WtMcpError("ensure --config did not print a JSON client entry") from e
    if entry.get("url") != f"http://127.0.0.1:{port}/mcp":
        raise WtMcpError("ensure --config named a URL that is not this tree's port")
    tree.ensure_state.mkdir(parents=True, exist_ok=True)
    tree.ensure_state.chmod(0o700)
    tmp = tree.config_file.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {"precis": entry}}, fh)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, tree.config_file)
    return tree.config_file


# ── probes ─────────────────────────────────────────────────────────────────


def probe(port: int, timeout: float = 5.0) -> str:
    """``up`` (proxy 200 and backend 401), ``down`` (refused / 5xx / wrong
    answer) or ``unknown`` (no answer within ``timeout``: a respawn in
    progress holds requests in the backlog rather than refusing them)."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/proxy-health")
        if conn.getresponse().status != 200:
            return "down"
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request(
            "POST",
            "/mcp",
            body="{}",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        status = conn.getresponse().status
        conn.close()
        return "up" if status == 401 else "down"
    except TimeoutError:
        return "unknown"
    except OSError:
        return "down"


def src_fingerprint(root: Path) -> tuple[int, int]:
    """``(file count, newest mtime_ns)`` under ``src/``; cheap enough per second."""
    count = 0
    newest = 0
    stack = [root / "src"]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith(".") or e.name == "__pycache__":
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False) and not e.name.endswith(
                        ".pyc"
                    ):
                        count += 1
                        m = e.stat(follow_symlinks=False).st_mtime_ns
                        if m > newest:
                            newest = m
        except OSError:
            continue
    return count, newest


# ── commands ───────────────────────────────────────────────────────────────


def _seed_state(tree: Tree) -> None:
    """First ``up``: clone the shared server's md vector cache and python
    roots into this tree's ensure state (APFS clone, so ~free), so the
    worktree server does not re-embed ~19k vectors from cold. Secrets,
    the Caddyfile and the live colour are never copied."""
    live = Path(
        os.environ.get("PRECIS_WT_LIVE_STATE")
        or str(Path.home() / ".cache" / LIVE_NAME)
    )
    tree.ensure_state.mkdir(parents=True, exist_ok=True)
    cache = tree.ensure_state / "cache"
    if not cache.exists() and (live / "cache").is_dir():
        cp = subprocess.run(
            ["cp", "-Rc", str(live / "cache"), str(cache)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=300,
        )
        if cp.returncode != 0:
            shutil.rmtree(cache, ignore_errors=True)
            shutil.copytree(live / "cache", cache, dirs_exist_ok=True)
    roots = live / "python-roots"
    if roots.is_file() and not (tree.ensure_state / "python-roots").exists():
        shutil.copy2(roots, tree.ensure_state / "python-roots")


def cmd_up(tree: Tree) -> int:
    ensure_script(tree)
    migration_guard(tree)
    claim_root(tree)
    enforce_cap(tree)
    port = pick_port(tree)
    tree.state_dir.mkdir(parents=True, exist_ok=True)
    tree.state_dir.chmod(0o700)
    tree.port_file.write_text(f"{port}\n", encoding="utf-8")
    _seed_state(tree)
    t0 = time.monotonic()
    res = run_ensure(tree, port)
    if res.returncode != 0:
        raise WtMcpError(
            f"ensure exited {res.returncode} for {tree.server}; see "
            f"{tree.ensure_state / 'ensure.log'} and `docker logs {tree.server}-a`"
        )
    write_config(tree, port)
    print(
        f"{tree.server} up on 127.0.0.1:{port} ({time.monotonic() - t0:.0f}s); "
        f"session config {tree.config_file} "
        "(server name 'precis'; run `scripts/wt-mcp claude`)"
    )
    return 0


def cmd_restart(tree: Tree) -> int:
    migration_guard(tree)  # the manual restart is a code path to prod too
    port = pick_port(tree)
    res = run_ensure(tree, port, "--restart")
    return res.returncode


def _watcher_pid(tree: Tree) -> int | None:
    try:
        pid = int(tree.watch_pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    cmd = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    ).stdout
    return pid if "wt-mcp" in cmd and "watch" in cmd else None


def stop_watcher(tree: Tree) -> None:
    pid = _watcher_pid(tree)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    tree.watch_pid_file.unlink(missing_ok=True)


def cmd_down(tree: Tree, *, if_present: bool = False) -> int:
    """Remove this tree's proxy, backends, network, uv-cache volume, ensure
    state (its secrets copy included) and the session config.

    Verified, not assumed: after the removals the family is listed again and
    a survivor is a non-zero exit — the reaper keeps the tree in that case
    rather than orphaning a prod read-write container. ``if_present`` makes
    a tree that never ran a server a quick no-op.
    """
    server = assert_wt_target(tree.server)
    names = family(server)
    marker = tree.ensure_state / "root"
    if marker.is_file():
        owner = marker.read_text(encoding="utf-8").strip()
        if owner and owner != str(tree.root):
            # A colliding tree (same directory name, other parent) being
            # reaped must not take the owner's server down with it.
            raise WtMcpError(
                f"{server} belongs to {owner}, not {tree.root}; not touching it "
                f"(run `scripts/wt-mcp down --root {owner}` for that tree)"
            )
    if if_present:
        present = [n for n, _ in wt_containers() if n in names]
        if (
            not present
            and not tree.ensure_state.exists()
            and not tree.state_dir.exists()
        ):
            print(f"{server}: nothing to tear down")
            return 0
    stop_watcher(tree)
    for name in sorted(names):
        _docker("rm", "-f", name)
    _docker("network", "rm", assert_wt_target(server + "-net"))
    _docker("volume", "rm", assert_wt_target(server + "-uv-cache"))
    left = [n for n, _ in wt_containers() if n in names]
    if left:
        print(
            f"wt-mcp: {server} NOT fully down: {', '.join(sorted(left))} still "
            "exist (docker refused or timed out); state kept",
            file=sys.stderr,
        )
        return 1
    shutil.rmtree(tree.ensure_state, ignore_errors=True)
    shutil.rmtree(tree.state_dir, ignore_errors=True)
    print(f"{server} down; state removed")
    return 0


def cmd_status(tree: Tree) -> int:
    port = pick_port(tree) if tree.port_file.is_file() else None
    print(f"tree:     {tree.name} ({tree.root})")
    print(f"server:   {tree.server}")
    if port is None:
        print("port:     (not up — run `scripts/wt-mcp up`)")
        return 1
    print(f"port:     127.0.0.1:{port} -> {probe(port)}")
    print(
        f"config:   {tree.config_file} ({'present' if tree.config_file.is_file() else 'missing'})"
    )
    pid = _watcher_pid(tree)
    print(f"watcher:  {f'pid {pid}' if pid else 'not running'}")
    res = run_ensure(tree, port, "--status", capture=True)
    for line in res.stdout.splitlines():
        print(f"ensure:   {line}")
    return 0


def _log(tree: Tree, msg: str) -> None:
    line = f"{_now()} {msg}"
    print(line, flush=True)
    try:
        with tree.watch_log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def cmd_watch(
    tree: Tree,
    *,
    poll_s: float = 1.0,
    debounce_s: float = 2.0,
    health_every_s: float = 10.0,
    iterations: int | None = None,
) -> int:
    """Restart the worktree server's child when ``src/`` changes; bring the
    server back when its proxy or backend is gone.

    Only two actions exist and both go through :func:`run_ensure` on this
    tree's asserted name: ``--restart`` (child respawn, port held) after a
    quiet ``debounce_s`` following an edit, and a plain ensure when the
    probe says ``down`` (at most once per 30 s). ``unknown`` (no answer in
    time) is a respawn in progress, not a failure.
    """
    port = pick_port(tree)
    tree.state_dir.mkdir(parents=True, exist_ok=True)
    tree.watch_pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    _log(
        tree,
        f"watch {tree.server} :{port} src={tree.root / 'src'} debounce={debounce_s}s",
    )
    last = src_fingerprint(tree.root)
    changed_at: float | None = None
    next_health = 0.0
    last_recover = -1e9
    n = 0
    try:
        while iterations is None or n < iterations:
            n += 1
            now = time.monotonic()
            cur = src_fingerprint(tree.root)
            if cur != last:
                last = cur
                changed_at = now
            if changed_at is not None and now - changed_at >= debounce_s:
                changed_at = None
                try:
                    migration_guard(tree)
                except WtMcpError as e:
                    _log(tree, f"src changed but NOT restarting: {e}")
                    next_health = time.monotonic() + health_every_s
                    time.sleep(poll_s)
                    continue
                _log(tree, "src changed: respawning the server child (--restart)")
                t0 = time.monotonic()
                res = run_ensure(tree, port, "--restart", capture=True)
                if res.returncode != 0:
                    _log(
                        tree,
                        f"--restart exited {res.returncode}: {res.stderr.strip()[-300:]}",
                    )
                else:
                    state = probe(port, timeout=180.0)
                    _log(
                        tree,
                        f"served again: {state} after {time.monotonic() - t0:.1f}s",
                    )
                next_health = time.monotonic() + health_every_s
            if now >= next_health:
                next_health = now + health_every_s
                state = probe(port)
                if state == "down" and now - last_recover >= 30.0:
                    last_recover = now
                    try:
                        migration_guard(tree)
                    except WtMcpError as e:
                        _log(tree, f"{tree.server} is down and stays down: {e}")
                    else:
                        _log(
                            tree,
                            f"{tree.server} is down: running ensure to bring it back",
                        )
                        res = run_ensure(tree, port, capture=True)
                        _log(tree, f"ensure exited {res.returncode}")
            time.sleep(poll_s)
    finally:
        tree.watch_pid_file.unlink(missing_ok=True)
    return 0


def _readable_token_file(path: Path) -> bool:
    """Owner/mode gate before any read (gr515184 review): ``True`` only
    when ``path`` is a non-empty file owned by the current uid with no
    group/other permission bits. A foreign-owned or too-open file is
    refused loudly — path and ``next: chmod 600 <path>`` on stderr, never
    the content. A missing file is silent (the caller tries the next
    candidate)."""
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size == 0:
        return False
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        print(
            f"wt-mcp: refusing {path} (owner/mode too open)\nnext: chmod 600 {path}",
            file=sys.stderr,
        )
        return False
    return True


def _session_mcp_env() -> dict[str, str]:
    """Env for the ``claude`` child: ``os.environ`` plus ``PRECIS_MCP_TOKEN``
    read from the client token file (gr515184). The generated
    ``--mcp-config`` entry's bearer header is the env reference
    ``${PRECIS_MCP_TOKEN}``, expanded at connect time — the value must be
    in the child's environment and is never printed, logged or passed on
    argv. ``PRECIS_MCP_TOKEN_FILE`` overrides the default
    ``~/.config/precis/token`` (ansible's mode-600 file); the shared
    server's ``~/.secrets/pw/PRECIS_MCP_TOKEN`` is the same value until
    that run lands. Each candidate passes :func:`_readable_token_file`'s
    owner/mode gate before any read."""
    env = dict(os.environ)
    if env.get("PRECIS_MCP_TOKEN"):
        return env
    token = ""
    for cand in (
        Path(env.get("PRECIS_MCP_TOKEN_FILE") or "~/.config/precis/token").expanduser(),
        Path.home() / ".secrets" / "pw" / "PRECIS_MCP_TOKEN",
    ):
        if not _readable_token_file(cand):
            continue
        try:
            token = cand.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            break
    if token:
        env["PRECIS_MCP_TOKEN"] = token
    else:
        print(
            "wt-mcp: no client token file — the session starts but precis calls"
            " will answer 401 (docs/runbooks/session-restart.md)",
            file=sys.stderr,
        )
    return env


def cmd_claude(tree: Tree, args: list[str]) -> int:
    """``up``, start the watcher, run Claude Code with this tree's server as
    its only ``precis``; stop the watcher when the session ends."""
    cmd_up(tree)
    stop_watcher(tree)
    script = tree.root / "scripts" / "wt-mcp"
    log_fh = tree.watch_log.open("a", encoding="utf-8")
    watcher = subprocess.Popen(
        [str(script), "watch"],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"watcher pid {watcher.pid} (log {tree.watch_log.relative_to(tree.root)})")
    claude = shutil.which("claude")
    if claude is None:
        watcher.terminate()
        raise WtMcpError("claude is not on PATH")
    port = pick_port(tree)
    print(f"session precis = {tree.server} on 127.0.0.1:{port} (strict mcp config)")
    proc = subprocess.Popen(
        [claude, "--mcp-config", str(tree.config_file), "--strict-mcp-config", *args],
        env=_session_mcp_env(),
    )
    prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        # Interactive session: unbounded by design, written as a poll loop.
        while True:
            try:
                rc = proc.wait(timeout=3600)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        signal.signal(signal.SIGINT, prev)
        watcher.terminate()
        try:
            watcher.wait(timeout=10)
        except subprocess.TimeoutExpired:
            watcher.kill()
        log_fh.close()
        tree.watch_pid_file.unlink(missing_ok=True)
    return rc


def main(argv: list[str], root: Path) -> int:
    p = argparse.ArgumentParser(prog="wt-mcp", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up", help="start this worktree's server (idempotent)")
    d = sub.add_parser("down", help="stop and remove it, state included")
    d.add_argument(
        "--root",
        type=Path,
        default=None,
        help="tear down the server of the worktree at this path (the reaper's "
        "form: runs from any checkout, needs no git)",
    )
    d.add_argument(
        "--if-present",
        action="store_true",
        help="exit 0 quickly when the tree never ran a server",
    )
    sub.add_parser("status", help="probe + ensure --status")
    sub.add_parser("restart", help="respawn the server child (port held)")
    w = sub.add_parser(
        "watch", help="auto-restart on src/ edits; recover a dead server"
    )
    w.add_argument("--poll", type=float, default=1.0)
    w.add_argument("--debounce", type=float, default=2.0)
    w.add_argument("--health-every", type=float, default=10.0)
    w.add_argument("--iterations", type=int, default=None)
    c = sub.add_parser(
        "claude", help="up + watcher + claude with this server as 'precis'"
    )
    c.add_argument("args", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    try:
        if a.cmd == "down" and a.root is not None:
            return cmd_down(Tree.for_root(a.root, root), if_present=a.if_present)
        tree = Tree.at(root)
        if a.cmd == "up":
            return cmd_up(tree)
        if a.cmd == "down":
            return cmd_down(tree, if_present=a.if_present)
        if a.cmd == "status":
            return cmd_status(tree)
        if a.cmd == "restart":
            return cmd_restart(tree)
        if a.cmd == "watch":
            return cmd_watch(
                tree,
                poll_s=a.poll,
                debounce_s=a.debounce,
                health_every_s=a.health_every,
                iterations=a.iterations,
            )
        if a.cmd == "claude":
            return cmd_claude(tree, a.args)
    except WtMcpError as e:
        print(f"wt-mcp: {e}", file=sys.stderr)
        return 1
    return 2


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)
