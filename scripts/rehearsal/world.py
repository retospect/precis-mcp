"""The scratch world on disk: remote, host checkout, scratch home, wrappers.

Layout of one run directory (``scratch/rehearsal/run-<id>/``)::

    remote.git/    bare repository standing in for GitHub; ``main`` and
                   ``prod`` are the seed ref's commit
    host/          the host checkout cloned from remote.git: the engine's
                   PRECIS_SLICE_ROOT and the code the worker runs
                   (its .venv is seeded from the harness checkout's)
    home/          the worker's HOME (a git identity, nothing else)
    bin/           first on the worker's PATH: ``git`` (url wrapper),
                   ``claude`` ``devin`` ``kimi`` ``codex`` (scripted
                   sessions), ``gh`` (the drift guard's one CI read),
                   ``docker`` ``ssh`` ``scp`` (refusals)
    ops/ tmp/      PRECIS_SLICE_OPS_DIR and TMPDIR of the worker
    scripts/       per-task session scripts the scenario wrote (JSON)
    sessions/      one record per launched session (JSON), briefs/ copies
    logs/          worker log and the driver's event log

How the engine reaches a local bare repository although it only accepts
``https://github.com/...`` as the repository's url: the worker is pinned to
a stand-in url (``PRECIS_FACTORY_ORIGIN_URL``) and its PATH carries a
``git`` wrapper that adds ``-c url.<remote.git>.insteadOf=<stand-in url>``
to every git call (command-scope config survives the engine's
``GIT_CONFIG_GLOBAL=/dev/null``). The wrapper steps aside for a call that
already carries url rewrites (ship's own mirror rewrite) and refuses any
other ``github.com`` url, so nothing here can reach the real GitHub.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import objscan
from . import proc as launch

#: The stand-in for the repository's GitHub url. Matches the engine's
#: ``https://github.com/\S+`` shape and names no real repository.
FAKE_ORIGIN = "https://github.com/rehearsal-scratch/scratch.git"

LANES = ("claude", "devin", "kimi", "codex")
REFUSED_TOOLS = ("scp", "ssh", "docker")

HERE = Path(__file__).resolve().parent


#: The world's ``~/.gitconfig``: an identity, no credential helper, and a
#: second fence beside the ``git`` wrapper: any github.com url that no other
#: rule maps goes nowhere (the stand-in url is a longer prefix, so its rewrite
#: still wins).
GITCONFIG_TEXT = (
    "[user]\n\tname = Rehearsal Factory\n\temail = factory@rehearsal.invalid\n"
    "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n"
    "[credential]\n\thelper =\n"
    '[url "file:///nonexistent-rehearsal/"]\n\tinsteadOf = https://github.com/\n'
)


class WorldError(RuntimeError):
    """The scratch world could not be built."""


def run_git(
    args: list[str], cwd: Path | None = None, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Plain git for harness-side work (scratch remote, host checkout)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "GIT_TERMINAL_PROMPT": "0",
        },
        timeout=300,
    )
    if check and proc.returncode != 0:
        raise WorldError(
            f"git {' '.join(args[:4])} failed in {cwd}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return proc


@dataclass
class World:
    """Paths of one scratch world."""

    run_dir: Path
    repo_root: Path  # the harness's own checkout (seed source, venv source)
    #: The seed commit (``main`` and ``prod`` of the remote at the start).
    seed_sha: str = ""
    #: The host checkout's HEAD at the start.
    host_head0: str = ""
    #: Commits the driver put on the remote's ``main`` itself (a competing
    #: change): the only main commits that are not a land.
    competitors: list[str] = field(default_factory=list)
    #: Every object name the remote held right after seeding; the object
    #: scan reads only what came after.
    seed_objects: frozenset[str] = frozenset()

    @property
    def remote(self) -> Path:
        return self.run_dir / "remote.git"

    @property
    def host(self) -> Path:
        return self.run_dir / "host"

    @property
    def home(self) -> Path:
        return self.run_dir / "home"

    @property
    def bin(self) -> Path:
        return self.run_dir / "bin"

    @property
    def ops(self) -> Path:
        return self.run_dir / "ops"

    def link_uv_cache(self) -> None:
        """Give the slice worker's usual ops cache a persistent backing directory."""
        result = launch.run(["uv", "cache", "dir"], cwd=self.repo_root, timeout=60)
        cache_dir = result.stdout.strip()
        if result.returncode == 0 and cache_dir:
            target = (self.repo_root / cache_dir).resolve()
        else:
            target = self.repo_root / "scratch" / "rehearsal" / "uv-cache"
        target.mkdir(parents=True, exist_ok=True)
        (self.ops / "uv-cache").symlink_to(target, target_is_directory=True)

    @property
    def tmp(self) -> Path:
        return self.run_dir / "tmp"

    @property
    def scripts(self) -> Path:
        return self.run_dir / "scripts"

    @property
    def sessions(self) -> Path:
        return self.run_dir / "sessions"

    @property
    def logs(self) -> Path:
        return self.run_dir / "logs"

    @property
    def python(self) -> Path:
        return self.host / ".venv" / "bin" / "python"

    @property
    def hub(self) -> Path:
        return self.host / ".git" / "precis-hub.git"

    @property
    def folders(self) -> Path:
        """Where attempt folders live (``run_slice.worktrees_dir``)."""
        return self.host / ".claude" / "worktrees"

    def git_remote(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Plain git against the scratch remote (bare)."""
        return run_git(["--git-dir", str(self.remote), *args], check=False)

    def remote_tip(self, branch: str = "main") -> str:
        proc = self.git_remote(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
        )
        return proc.stdout.strip()

    def hub_refs(self, prefix: str = "refs/factory/build/") -> dict[str, str]:
        """``{ref: sha}`` in the host's hub under ``prefix``."""
        if not (self.hub / "HEAD").is_file():
            return {}
        proc = run_git(
            [
                "--git-dir",
                str(self.hub),
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                prefix,
            ],
            check=False,
        )
        out: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            sha, _, ref = line.partition(" ")
            if ref:
                out[ref] = sha
        return out

    def attempt_folders(self) -> list[Path]:
        if not self.folders.is_dir():
            return []
        return sorted(
            p for p in self.folders.iterdir() if p.name.startswith("slice-td")
        )

    def advance_remote_main(self, path: str, text: str, message: str) -> str:
        """Commit one file on the scratch remote's ``main`` (a competing
        change, scenario 4) and return the new tip. Works through a
        throwaway clone, like any other pusher."""
        work = self.tmp / "advance-main"
        shutil.rmtree(work, ignore_errors=True)
        run_git(["clone", "--quiet", "--branch", "main", str(self.remote), str(work)])
        run_git(["config", "user.name", "Rehearsal Competitor"], work)
        run_git(["config", "user.email", "competitor@rehearsal.invalid"], work)
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        run_git(["add", "--", path], work)
        run_git(["commit", "--quiet", "-m", message], work)
        run_git(["push", "--quiet", "origin", "HEAD:refs/heads/main"], work)
        tip = run_git(["rev-parse", "HEAD"], work).stdout.strip()
        shutil.rmtree(work, ignore_errors=True)
        self.competitors.append(tip)
        return tip

    def host_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Plain git in the host checkout."""
        return run_git(list(args), self.host, check=False)

    def all_refs(self, git_dir: Path) -> dict[str, str]:
        """``{ref: sha}`` of every ref of a bare repository."""
        proc = run_git(
            [
                "--git-dir",
                str(git_dir),
                "for-each-ref",
                "--format=%(objectname) %(refname)",
            ],
            check=False,
        )
        out: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            sha, _, ref = line.partition(" ")
            if ref:
                out[ref] = sha
        return out

    def objects_containing(self, git_dir: Path, needles: Sequence[str]) -> list[str]:
        """Objects of ``git_dir`` created after the seed (reachable or not)
        whose content holds any of ``needles``."""
        return objscan.objects_containing(git_dir, needles, skip=self.seed_objects)

    def manifest(self) -> dict[str, str]:
        return {
            "run_dir": str(self.run_dir),
            "remote": str(self.remote),
            "host": str(self.host),
        }


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _real(tool: str) -> str:
    found = shutil.which(tool)
    if found is None:
        raise WorldError(f"{tool} is not on PATH")
    return found


def _git_wrapper(real_git: str, remote: Path) -> str:
    """The ``git`` wrapper (see the module docstring)."""
    fake = FAKE_ORIGIN
    return f"""#!/bin/sh
# rehearsal git wrapper: the stand-in GitHub url is the scratch remote.
real={shlex.quote(real_git)}
remote={shlex.quote(str(remote))}
fake={shlex.quote(fake)}
# No system gitconfig (Apple and Homebrew git ship a keychain credential
# helper there) and no credential helper at all.
export GIT_CONFIG_NOSYSTEM=1
# Refuse any other github.com contact: the scratch world never reaches it.
for a in "$@"; do
  case "$a" in
    *github.com*)
      case "$a" in
        *rehearsal-scratch/scratch*) ;;
        *) echo "rehearsal: refusing git contact with $a" >&2; exit 2 ;;
      esac ;;
  esac
done
# A call that already carries url rewrites (ship's mirror env) keeps them.
n=${{GIT_CONFIG_COUNT:-0}}
i=0
while [ "$i" -lt "$n" ]; do
  eval k=\\$GIT_CONFIG_KEY_$i
  case "$k" in
    url.*.insteadOf) exec "$real" -c credential.helper= "$@" ;;
  esac
  i=$((i + 1))
done
exec "$real" -c credential.helper= -c "url.$remote.insteadOf=$fake" -c "url.$remote.insteadOf=${{fake%.git}}" "$@"
"""


def _session_wrapper(python: str, world: Path, lane: str) -> str:
    shim = HERE / "shim.py"
    return (
        "#!/bin/sh\n"
        f"exec {shlex.quote(python)} {shlex.quote(str(shim))} "
        f'--world {shlex.quote(str(world))} --lane {lane} -- "$@"\n'
    )


def _refusal(tool: str) -> str:
    return (
        "#!/bin/sh\n"
        f"echo 'rehearsal: {tool} is not available in the scratch world' >&2\n"
        "exit 127\n"
    )


def seed_cache(repo_root: Path, sha: str, cache_dir: Path) -> Path:
    """A packed bare repository holding exactly ``sha``'s history, built
    once per commit and reused by every later run (the shared object store
    has tens of thousands of loose objects, so a straight clone of it takes
    half a minute; a pack hard-links in a second). Returns its path."""
    cache = cache_dir / f"seed-{sha[:12]}.git"
    if (cache / "HEAD").is_file() and (cache / "seeded").is_file():
        return cache
    shutil.rmtree(cache, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--quiet", "--bare", str(cache)])
    # --no-verify: a pre-push hook of this repository must not run for the seed.
    run_git(
        ["push", "--quiet", "--no-verify", str(cache), f"{sha}:refs/heads/main"],
        repo_root,
    )
    run_git(["--git-dir", str(cache), "repack", "-a", "-d", "--quiet"])
    (cache / "seeded").write_text(sha, encoding="utf-8")
    return cache


def seed_remote(world: World, seed_ref: str, cache_dir: Path) -> str:
    """Make ``remote.git`` for the run: a hard-linked clone of the seed
    cache holding ``main`` and ``prod`` at ``seed_ref``'s commit and no other
    ref. Returns the seed commit."""
    sha = run_git(
        ["rev-parse", "--verify", f"{seed_ref}^{{commit}}"], world.repo_root
    ).stdout.strip()
    cache = seed_cache(world.repo_root, sha, cache_dir)
    run_git(
        [
            "clone",
            "--quiet",
            "--bare",
            "--local",
            "--no-tags",
            str(cache),
            str(world.remote),
        ]
    )
    for branch in ("main", "prod"):
        run_git(
            ["--git-dir", str(world.remote), "update-ref", f"refs/heads/{branch}", sha]
        )
    run_git(["--git-dir", str(world.remote), "symbolic-ref", "HEAD", "refs/heads/main"])
    run_git(["--git-dir", str(world.remote), "config", "gc.auto", "0"])
    return sha


def seed_object_names(remote: Path, cache_dir: Path, sha: str) -> set[str]:
    """Every object name of the freshly seeded remote, read once per seed
    commit and kept beside the seed cache."""
    listing = cache_dir / f"seed-{sha[:12]}.objects"
    if listing.is_file():
        return set(listing.read_text(encoding="utf-8").split())
    names = objscan.object_names(remote)
    listing.write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
    return names


def seed_venv(world: World, venv_from: Path | None) -> str:
    """``host/.venv`` from a checkout that has one, with the repo's own
    ``scripts/seed-worktree-venv`` (editable install repointed at
    ``host/src``). Returns a note."""
    src = venv_from or world.repo_root
    if not (src / ".venv").is_dir():
        raise WorldError(
            f"{src} has no .venv to seed the host checkout from; run "
            f"`scripts/seed-worktree-venv {src} <a checkout with a .venv>` "
            "or `uv sync` there once, or pass --venv-from"
        )
    script = world.repo_root / "scripts" / "seed-worktree-venv"
    seeded = launch.run([str(script), str(world.host), str(src)], timeout=900)
    if seeded.returncode != 0:
        raise WorldError(
            f"seed-worktree-venv failed: {(seeded.stderr or seeded.stdout).strip()[-300:]}"
        )
    probe = launch.run(
        [str(world.python), "-c", "import precis; print(precis.__file__)"],
        timeout=120,
    )
    where = probe.stdout.strip()
    if probe.returncode != 0 or not where.startswith(str(world.host)):
        raise WorldError(
            f"host interpreter imports precis from {where or probe.stderr.strip()[-200:]!r}, "
            f"not from {world.host}/src; next: re-seed or `uv sync` in {world.host}"
        )
    extras = ensure_gate_deps(world)
    return f"host .venv seeded from {src}; precis imports from {where}" + (
        f"; {extras}" if extras else ""
    )


#: What the quick lint's guards import beyond the base environment. The
#: production gate runs them in a container with every extra; the host
#: ``.venv`` has none, and ``tests/precis_web/conftest.py`` skips the whole
#: package at collection when ``fastapi`` is missing, which pytest reports as
#: an error and ship reads as a red lint.
GATE_DEPS = ("fastapi", "starlette", "jinja2", "python-multipart")


def locked_version(lock_text: str, name: str) -> str | None:
    """The version ``uv.lock`` pins for ``name``."""
    m = re.search(
        r'^\[\[package\]\]\nname = "' + re.escape(name) + r'"\nversion = "([^"]+)"',
        lock_text,
        re.MULTILINE,
    )
    return m.group(1) if m else None


def ensure_gate_deps(world: World) -> str:
    """Install :data:`GATE_DEPS` into the *host* checkout's ``.venv`` (never
    the source venv it was copied from) when ``fastapi`` is missing, at the
    versions the host checkout's ``uv.lock`` pins: a land folder's ``uv run``
    syncs back to the lock, so a newer fastapi would not survive it. Returns a
    note, or ``""`` when nothing was needed."""
    have = launch.run([str(world.python), "-c", "import fastapi"], timeout=120)
    if have.returncode == 0:
        return ""
    lock = (world.host / "uv.lock").read_text(encoding="utf-8")
    pins = []
    for name in GATE_DEPS:
        version = locked_version(lock, name)
        if version is None:
            raise WorldError(
                f"uv.lock pins no {name}; cannot prepare the lint's guards"
            )
        pins.append(f"{name}=={version}")
    done = launch.run(
        ["uv", "pip", "install", "--python", str(world.python), *pins], timeout=600
    )
    if done.returncode != 0:
        raise WorldError(
            f"could not install the lint's web dependencies: {(done.stderr or done.stdout).strip()[-300:]}"
        )
    return f"installed {', '.join(pins)} into the host .venv for the lint's guards"


def build_world(
    run_dir: Path, repo_root: Path, seed_ref: str, *, venv_from: Path | None = None
) -> tuple[World, list[str]]:
    """Create the scratch world. Returns it and the notes to print."""
    world = World(run_dir=run_dir.resolve(), repo_root=repo_root.resolve())
    notes: list[str] = []
    for d in (
        world.home,
        world.bin,
        world.ops,
        world.tmp,
        world.scripts,
        world.sessions,
        world.logs,
    ):
        d.mkdir(parents=True, exist_ok=True)
    world.link_uv_cache()
    sha = seed_remote(
        world, seed_ref, world.repo_root / "scratch" / "rehearsal" / "cache"
    )
    notes.append(f"scratch remote seeded: main = prod = {sha[:12]} ({seed_ref})")
    world.seed_sha = sha
    world.seed_objects = frozenset(
        seed_object_names(
            world.remote, world.repo_root / "scratch" / "rehearsal" / "cache", sha
        )
    )
    run_git(
        [
            "clone",
            "--quiet",
            "--local",
            "--branch",
            "main",
            str(world.remote),
            str(world.host),
        ]
    )
    run_git(["config", "user.name", "Rehearsal Host"], world.host)
    run_git(["config", "user.email", "host@rehearsal.invalid"], world.host)
    notes.append(f"host checkout cloned at {world.host}")
    world.host_head0 = run_git(["rev-parse", "HEAD"], world.host).stdout.strip()
    notes.append(seed_venv(world, venv_from))
    (world.home / ".gitconfig").write_text(GITCONFIG_TEXT, encoding="utf-8")
    py = sys.executable
    _write_exec(world.bin / "git", _git_wrapper(_real("git"), world.remote))
    for lane in LANES:
        _write_exec(world.bin / lane, _session_wrapper(py, world.run_dir, lane))
    for tool in REFUSED_TOOLS:
        _write_exec(world.bin / tool, _refusal(tool))
    # ``gh`` answers only the drift guard's CI read (gh_stub.py).
    _write_exec(
        world.bin / "gh",
        f'#!/bin/sh\nexec {shlex.quote(py)} {shlex.quote(str(HERE / "gh_stub.py"))} "$@"\n',
    )
    # The notify scripts' prod-precis seam: record, post nothing.
    _write_exec(
        world.bin / "notify-stub",
        f"#!/bin/sh\nexec {shlex.quote(py)} {shlex.quote(str(HERE / 'notify_stub.py'))} "
        f'{shlex.quote(str(world.logs / "notify-calls.jsonl"))} "$@"\n',
    )
    # uv must be reachable by name for ship and the engine; it is the one
    # tool the wrapper directory forwards.
    uv = _real("uv")
    link = world.bin / "uv"
    if not link.exists():
        link.symlink_to(uv)
    (world.run_dir / "world.json").write_text(
        json.dumps(world.manifest(), indent=2), encoding="utf-8"
    )
    return world, notes
