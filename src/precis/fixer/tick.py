"""One tick of the laptop fixer loop (ADR 0048).

    pick (ready-gated) → build (Claude, host OAuth) → gate
      → [ship → deploy → look-at-prod → fix-forward]   (autonomy≥ship/full)
      → report (by exception)

Run once via ``scripts/fixer-tick`` (launchd, skip-on-battery). Serial
by a lockfile: a second tick that finds the lock held **exits** rather
than racing a concurrent ship/deploy.

**Autonomy is a dial, defaulting to the safe rung** so that merely
landing this module does nothing dangerous:

* ``report`` (default) — build + quick gate + report; **no ship, no
  deploy**. Leaves a pushed branch for the human to ``/go``.
* ``ship`` — additionally run ``scripts/ship`` (the authoritative
  container gate + squash-merge to main). No deploy.
* ``full`` — additionally ``scripts/deploy`` + the agentic prod-look +
  fix-forward. This is the keyless dark-factory rung.

Set ``PRECIS_FIXER_AUTONOMY`` to opt in. Auth is the host's ``claude``
OAuth (``~/.claude``) — *not* ``--bare``/API-key like the container
``fix_gripe`` — so the builder sees CLAUDE.md + skills.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from precis.fixer.intake import WorkItem, pick_next, ready_proposals
from precis.fixer.report import Report, ReportStatus, emit_report

log = logging.getLogger("precis.fixer")


class Autonomy(StrEnum):
    """How far down the pipeline a tick is allowed to walk."""

    REPORT = "report"
    SHIP = "ship"
    FULL = "full"

    @classmethod
    def from_env(cls, raw: str | None) -> Autonomy:
        value = (raw or "report").strip().lower()
        try:
            return cls(value)
        except ValueError:
            log.warning("unknown PRECIS_FIXER_AUTONOMY=%r; defaulting to report", raw)
            return cls.REPORT


@dataclass(frozen=True)
class FixerConfig:
    repo_root: Path
    proposals_dir: Path
    work_dir: Path
    claude_bin: str
    claude_model: str
    autonomy: Autonomy
    build_timeout_s: int
    gate_cmds: tuple[tuple[str, ...], ...]
    discord_webhook: str | None
    readyz_url: str | None

    @classmethod
    def from_env(cls, repo_root: Path) -> FixerConfig:
        work = os.environ.get("PRECIS_FIXER_WORK_DIR")
        work_dir = Path(work).resolve() if work else repo_root / ".fixer-work"
        model = os.environ.get("PRECIS_FIXER_CLAUDE_MODEL", "claude-opus-4-8")
        return cls(
            repo_root=repo_root,
            proposals_dir=repo_root / "docs" / "proposals",
            work_dir=work_dir,
            claude_bin=os.environ.get("PRECIS_FIXER_CLAUDE_BIN", "claude"),
            claude_model=model,
            autonomy=Autonomy.from_env(os.environ.get("PRECIS_FIXER_AUTONOMY")),
            build_timeout_s=int(os.environ.get("PRECIS_FIXER_BUILD_TIMEOUT_S", "1800")),
            gate_cmds=(
                ("uv", "run", "ruff", "check", "."),
                ("uv", "run", "ruff", "format", "--check", "."),
                ("uv", "run", "mypy", "src"),
            ),
            discord_webhook=os.environ.get("PRECIS_FIXER_DISCORD_WEBHOOK"),
            readyz_url=os.environ.get("PRECIS_FIXER_READYZ_URL"),
        )


# ── git helpers ────────────────────────────────────────────────────


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def branch_exists(repo: Path, branch: str) -> bool:
    """True if ``branch`` exists locally, in a worktree, or on origin.

    The idempotent-pick guard: any of these means the fixer already
    started (or finished) this item, so don't re-pick it.
    """
    local = _git(repo, "branch", "--list", branch, check=False).stdout.strip()
    if local:
        return True
    remote = _git(repo, "ls-remote", "--heads", "origin", branch, check=False).stdout
    return bool(remote.strip())


def _worktree_add(repo: Path, path: Path, branch: str) -> None:
    _git(repo, "fetch", "origin", "main", check=False)
    _git(repo, "worktree", "add", "-b", branch, str(path), "origin/main")


def _worktree_remove(repo: Path, path: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(path), check=False)


# ── build / gate / ship / deploy ───────────────────────────────────


def _compose_prompt(item: WorkItem) -> str:
    return (
        "You are an autonomous engineer in the precis-mcp repository, on a "
        f"fresh branch `{item.branch}`. Implement the spec below with the "
        "smallest correct change, run the relevant tests, and commit.\n\n"
        "Do NOT touch main. Do NOT switch branches. When done, commit your "
        "work (do not push — the fixer handles that).\n\n"
        "## Keep the agent-context maps fresh (same commit)\n"
        "If your change makes any agent-context *map* stale, update it in "
        "the SAME commit — while you still know what you changed. The maps "
        "are: `CLAUDE.md`, `AGENTS.md`, "
        "`src/precis/data/skills/precis-*-help.md`, `OPEN-ITEMS.md`, and the "
        "ADR index table in `docs/decisions/README.md`. This honours the "
        "CLAUDE.md norm: update the map in the same commit that changes what "
        "it describes.\n"
        "Do NOT force-update: archival prose under `docs/design/` (a "
        "drift-note is the honest treatment), the schema SVG, or existing "
        "ADR bodies — ADRs are append-only, so a change may *add* an ADR but "
        "never edit a sealed one.\n\n"
        f"# SPEC: {item.title}\n\n{item.spec_text}\n"
    )


def _spawn_claude(
    cfg: FixerConfig, cwd: Path, prompt: str
) -> subprocess.CompletedProcess[str]:
    """Run the host ``claude`` in ``cwd`` with OAuth (not ``--bare``).

    The laptop runs claude on the host, so ``~/.claude`` OAuth is
    reachable and the builder sees CLAUDE.md + skills. The gate — the
    only thing that needs the container — runs separately.
    """
    return subprocess.run(
        [
            cfg.claude_bin,
            "-p",
            "--dangerously-skip-permissions",
            "--model",
            cfg.claude_model,
            prompt,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=cfg.build_timeout_s,
        check=False,
    )


def _committed_anything(worktree: Path) -> bool:
    head = _git(worktree, "rev-parse", "HEAD", check=False).stdout.strip()
    base = _git(worktree, "rev-parse", "origin/main", check=False).stdout.strip()
    return bool(head) and head != base


def _quick_gate(cfg: FixerConfig, worktree: Path) -> tuple[bool, str]:
    """Fast host gate (ruff + mypy) for the report signal.

    Not the authoritative gate — ``scripts/ship`` runs ruff/mypy/pytest
    in the container at ship/full autonomy. This is the cheap
    "does it obviously pass" signal for the report-only rung.
    """
    for cmd in cfg.gate_cmds:
        res = subprocess.run(
            list(cmd), cwd=str(worktree), capture_output=True, text=True, check=False
        )
        if res.returncode != 0:
            tail = "\n".join((res.stdout + res.stderr).splitlines()[-15:])
            return False, f"gate failed: {' '.join(cmd)}\n{tail}"
    return True, "gate green (ruff + mypy)"


def _run_script(cfg: FixerConfig, script: str, *args: str) -> tuple[bool, str]:
    res = subprocess.run(
        [str(cfg.repo_root / "scripts" / script), *args],
        cwd=str(cfg.repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    tail = "\n".join((res.stdout + res.stderr).splitlines()[-20:])
    return res.returncode == 0, tail


def _look_at_prod(cfg: FixerConfig) -> tuple[bool, str]:
    """Post-deploy check. MVP: cheap ``/readyz`` liveness only.

    The full agentic look (load diff-scoped pages, make MCP calls,
    judge, fix-forward) is deferred — ADR 0048. Until then a liveness
    probe is the honest floor; a red probe blocks the "green" report.
    """
    if not cfg.readyz_url:
        return (
            True,
            "prod-look skipped (no PRECIS_FIXER_READYZ_URL); agentic look deferred",
        )
    try:
        import urllib.request

        with urllib.request.urlopen(cfg.readyz_url, timeout=20) as resp:
            ok = 200 <= resp.status < 300
        return ok, f"/readyz {resp.status}"
    except Exception as exc:
        return False, f"/readyz unreachable: {exc}"


# ── orchestration ──────────────────────────────────────────────────


@dataclass
class TickResult:
    picked: WorkItem | None = None
    report: Report | None = None
    notes: list[str] = field(default_factory=list)


def run_tick(cfg: FixerConfig) -> TickResult:
    """Execute one tick. Returns what happened (for tests + the CLI)."""
    result = TickResult()
    items = ready_proposals(cfg.proposals_dir)
    item = pick_next(items, lambda b: branch_exists(cfg.repo_root, b))
    if item is None:
        result.notes.append(f"nothing to do ({len(items)} ready, all branched)")
        return result
    result.picked = item

    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    worktree = cfg.work_dir / item.branch.replace("/", "_")
    if worktree.exists():
        _worktree_remove(cfg.repo_root, worktree)
    _worktree_add(cfg.repo_root, worktree, item.branch)

    try:
        build = _spawn_claude(cfg, worktree, _compose_prompt(item))
        if build.returncode != 0:
            tail = "\n".join((build.stderr or build.stdout or "").splitlines()[-10:])
            result.report = Report(
                ReportStatus.NEEDS_YOU,
                item.title,
                f"build failed: claude exited {build.returncode}\n{tail}",
            )
            return result
        if not _committed_anything(worktree):
            result.report = Report(
                ReportStatus.NEEDS_YOU,
                item.title,
                "claude exited cleanly but committed nothing — no change to review",
            )
            return result

        gate_ok, gate_msg = _quick_gate(cfg, worktree)
        if not gate_ok:
            result.report = Report(ReportStatus.NEEDS_YOU, item.title, gate_msg)
            return result

        if cfg.autonomy is Autonomy.REPORT:
            _git(worktree, "push", "origin", item.branch, check=False)
            result.report = Report(
                ReportStatus.OK,
                item.title,
                f"branch {item.branch} built + {gate_msg} → run /go to ship",
            )
            return result

        # ship (authoritative container gate lives here)
        ship_ok, ship_tail = _run_script_in_worktree(worktree, "ship", item.title)
        if not ship_ok:
            result.report = Report(
                ReportStatus.NEEDS_YOU, item.title, f"ship gate:\n{ship_tail}"
            )
            return result

        if cfg.autonomy is Autonomy.SHIP:
            result.report = Report(
                ReportStatus.OK, item.title, f"shipped {item.branch} to main"
            )
            return result

        # full: deploy + look at prod
        dep_ok, dep_tail = _run_script(cfg, "deploy")
        prod_ok, prod_msg = _look_at_prod(cfg)
        if dep_ok and prod_ok:
            result.report = Report(
                ReportStatus.OK, item.title, f"shipped + deployed; {prod_msg}"
            )
        else:
            result.report = Report(
                ReportStatus.NEEDS_YOU,
                item.title,
                f"deployed but prod check failed — fix-forward needed. {prod_msg}\n{dep_tail}",
            )
        return result
    finally:
        # Always clean up the build worktree — ship/full modes were
        # leaking them under .fixer-work (report mode already removed).
        _worktree_remove(cfg.repo_root, worktree)


def _run_script_in_worktree(worktree: Path, script: str, msg: str) -> tuple[bool, str]:
    """Run the worktree's OWN copy of a repo script (ship).

    ``scripts/ship`` does ``cd "$(dirname "$0")/.."``, so it operates on
    the repo rooted at the *script's path*, not on ``cwd``. Invoking the
    main checkout's ``scripts/ship`` cd's back to main ("on main —
    nothing to ship"); we must call ``<worktree>/scripts/ship`` so it
    lands in the worktree on the feature branch.
    """
    res = subprocess.run(
        [str(worktree / "scripts" / script), msg],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    tail = "\n".join((res.stdout + res.stderr).splitlines()[-20:])
    return res.returncode == 0, tail


# ── lock + CLI ─────────────────────────────────────────────────────


def _acquire_lock(lock_path: Path) -> object | None:
    """Non-blocking flock; returns the handle to keep open, or None."""
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # noqa: SIM115 — held for the process lifetime
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="precis.fixer.tick", description="one fixer tick"
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single tick (default)"
    )
    parser.add_argument(
        "--repo", type=Path, default=None, help="repo root (default: cwd)"
    )
    parser.add_argument(
        "--autonomy",
        choices=[a.value for a in Autonomy],
        default=None,
        help="override PRECIS_FIXER_AUTONOMY",
    )
    args = parser.parse_args(argv)

    repo_root = (args.repo or Path.cwd()).resolve()
    cfg = FixerConfig.from_env(repo_root)
    if args.autonomy:
        cfg = replace(cfg, autonomy=Autonomy(args.autonomy))

    lock = _acquire_lock(cfg.work_dir / "fixer.lock")
    if lock is None:
        log.info("another tick holds the lock — exiting")
        return 0

    result = run_tick(cfg)
    for note in result.notes:
        log.info("%s", note)
    if result.report is not None:
        emit_report(result.report, cfg.discord_webhook)
    return 0


if __name__ == "__main__":
    sys.exit(main())
