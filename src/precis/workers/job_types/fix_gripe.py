"""fix_gripe — clone the repo, run claude on a gripe_<id> branch, push.

The first job_type. Invoked by the `claude_inproc` executor's
runner. Reads the linked gripe's body + comment timeline as the
brief, clones ``$PRECIS_FIX_REPO_DIR`` into
``$PRECIS_FIX_WORK_DIR/clones/gripe_<id>``, runs
``claude -p --dangerously-skip-permissions`` with cwd = the clone
and a restricted env (no DB creds), then pushes the resulting
``gripe_<id>`` branch back to origin (the source repo) for human
review.

Trust model: claude shares the precis container's filesystem +
network. ``cwd`` + restricted env are the failure boundary; a
pre-push hook in every clone rejects pushes to anything not
matching ``gripe_*``. See the safety section in
``precis-fix-gripe-help`` for the full picture.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── Declared metadata (read by the dispatcher and the runner) ──────

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"claude_inproc"})

REQUIRES: frozenset[str] = frozenset(
    {"claude_bin", "git", "clones_dir", "claude_config_mount"}
)

DESCRIPTION: str = (
    "Clone the repo, run claude -p with --dangerously-skip-permissions "
    "as a subprocess, push the resulting branch gripe_<id> to origin "
    "for human review."
)


# ── Configuration helpers ──────────────────────────────────────────


@dataclass(frozen=True)
class FixGripeConfig:
    repo_dir: Path
    work_dir: Path
    claude_bin: str
    claude_model: str
    timeout_seconds: int


def load_config_from_env() -> FixGripeConfig:
    """Read the fix_gripe env vars. Raises if a required one is unset."""
    repo_dir_raw = os.environ.get("PRECIS_FIX_REPO_DIR")
    work_dir_raw = os.environ.get("PRECIS_FIX_WORK_DIR")
    if not repo_dir_raw:
        raise RuntimeError(
            "fix_gripe: PRECIS_FIX_REPO_DIR is not set (source repo path)"
        )
    if not work_dir_raw:
        raise RuntimeError(
            "fix_gripe: PRECIS_FIX_WORK_DIR is not set (clone scratch root)"
        )
    return FixGripeConfig(
        repo_dir=Path(repo_dir_raw).resolve(),
        work_dir=Path(work_dir_raw).resolve(),
        claude_bin=os.environ.get("PRECIS_FIX_CLAUDE_BIN", "claude"),
        claude_model=os.environ.get(
            "PRECIS_FIX_CLAUDE_MODEL", "claude-opus-4-7"
        ),
        timeout_seconds=int(
            os.environ.get("PRECIS_FIX_TIMEOUT_SECONDS", "1800")
        ),
    )


# ── Runner entry point ─────────────────────────────────────────────


@dataclass
class RunOutcome:
    """Result of one fix_gripe attempt — what the executor needs to
    transition status and write the summary."""

    status: str  # "succeeded" | "failed"
    summary_text: str
    gripe_comment_text: str
    branch: str | None
    sha: str | None
    wall_seconds: float


def run(
    *,
    store: Any,
    job_id: int,
    gripe_id: int,
    config: FixGripeConfig | None = None,
) -> RunOutcome:
    """Execute one fix_gripe attempt.

    Reads the gripe at run-time (no snapshot), clones the repo
    into a fresh ``gripe_<id>`` dir under ``work_dir/clones``,
    runs claude, pushes on success. Returns the structured
    outcome — the caller (executor runner) is responsible for
    writing chunks / tags / events back to the DB.
    """
    import time

    t0 = time.perf_counter()
    cfg = config or load_config_from_env()

    # Resolve the gripe so we can fail fast if it was deleted between
    # claim and run.
    ref = store.get_ref(kind="gripe", id=gripe_id)
    if ref is None:
        raise RuntimeError(f"fix_gripe: gripe id={gripe_id} not found")

    blocks = store.list_blocks_for_ref(gripe_id)
    if not blocks:
        raise RuntimeError(
            f"fix_gripe: gripe id={gripe_id} has no body chunk"
        )
    prompt = _compose_prompt(ref_title=ref.title, blocks=blocks)

    clone_dir = cfg.work_dir / "clones" / f"gripe_{gripe_id}"
    branch = f"gripe_{gripe_id}"
    if clone_dir.exists():
        shutil.rmtree(clone_dir)
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    _git_clone_and_branch(cfg.repo_dir, clone_dir, branch)
    _install_prepush_hook(clone_dir)

    base_sha = _git_rev_parse(clone_dir, "origin/main")

    result = _spawn_claude(cfg, clone_dir, prompt)
    wall = time.perf_counter() - t0
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").splitlines()[-20:]
        return RunOutcome(
            status="failed",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: "
                f"claude exited {result.returncode}. Took {wall:.1f}s. "
                "stderr tail:\n" + "\n".join(tail)
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] fix attempt failed: claude exited "
                f"{result.returncode}. stderr tail:\n" + "\n".join(tail[-5:])
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )

    # Verify the agent actually committed + pushed the branch.
    branch_sha = _git_rev_parse(clone_dir, branch)
    pushed_sha = _git_rev_parse(clone_dir, f"origin/{branch}")
    main_sha_after = _git_rev_parse(clone_dir, "origin/main")
    if branch_sha is None or pushed_sha is None or branch_sha != pushed_sha:
        return RunOutcome(
            status="failed",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: "
                "no commits pushed to origin under branch "
                f"{branch}. Took {wall:.1f}s."
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] claude exited cleanly but did "
                f"not push branch {branch} to origin. No fix to review."
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )
    if main_sha_after != base_sha:
        return RunOutcome(
            status="failed",
            summary_text=(
                f"fix_gripe job:{job_id} for gripe:{gripe_id} failed: "
                "origin/main moved during the run (the prepush hook "
                "should have prevented this — bug?)."
            ),
            gripe_comment_text=(
                f"[worker:job:{job_id}] aborted: origin/main was "
                "modified during the run."
            ),
            branch=branch,
            sha=None,
            wall_seconds=wall,
        )

    diffstat = _git_diff_stat(clone_dir, base_sha, branch_sha)
    return RunOutcome(
        status="succeeded",
        summary_text=(
            f"Fix attempt pushed to origin as branch {branch} @ "
            f"{branch_sha}. {diffstat}. Took {wall:.1f}s."
        ),
        gripe_comment_text=(
            f"[worker:job:{job_id}] branch {branch} @ {branch_sha} "
            "pushed to origin. Review with: "
            f"`git fetch && git checkout {branch} && git diff main..{branch}`."
        ),
        branch=branch,
        sha=branch_sha,
        wall_seconds=wall,
    )


# ── Prompt composition ────────────────────────────────────────────


def _compose_prompt(*, ref_title: str, blocks: list[Any]) -> str:
    """Build the prompt fed to ``claude -p`` from the gripe timeline."""
    lines: list[str] = []
    lines.append(
        "You are an autonomous engineer assigned a bug fix in the "
        "precis-mcp repository."
    )
    lines.append("")
    lines.append("BUG REPORT (gripe body + comments, in timeline order):")
    lines.append("")
    for i, block in enumerate(blocks):
        if i == 0:
            lines.append(f"BODY: {block.text}")
        else:
            lines.append(f"COMMENT {i}: {block.text}")
    lines.append("")
    lines.append("CONSTRAINTS:")
    lines.append("- You are on a fresh branch named gripe_<id>.")
    lines.append("- Make the smallest commits that fix the reported bug.")
    lines.append("- Run any relevant tests before committing.")
    lines.append("- When you are done, push your branch to origin "
                 "(`git push origin HEAD`).")
    lines.append("- Do NOT touch main. Do NOT switch branches.")
    lines.append("- The pre-push hook will reject pushes to anything not "
                 "matching gripe_*.")
    return "\n".join(lines)


# ── Subprocess + git plumbing ─────────────────────────────────────


def _spawn_claude(
    cfg: FixGripeConfig, cwd: Path, prompt: str
) -> subprocess.CompletedProcess[str]:
    """Spawn ``claude -p`` with a stripped env."""
    env = _restricted_env(cwd)
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
        env=env,
        capture_output=True,
        text=True,
        timeout=cfg.timeout_seconds,
        check=False,
    )


def _restricted_env(cwd: Path) -> dict[str, str]:
    """Build the subprocess env: minimal vars, no DB creds.

    Strips every ``PG*`` and ``PRECIS_DATABASE_URL`` so claude can't
    reach the postgres backing the precis runtime even if it tries.
    Keeps ``HOME`` (so claude can read ``~/.claude``), ``PATH``,
    ``TERM``, and a small allowlist of safe vars.
    """
    src = os.environ
    allowed_prefixes = ("ANTHROPIC_",)
    allowed_keys = {
        "HOME",
        "PATH",
        "TERM",
        "LANG",
        "LC_ALL",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
    }
    out: dict[str, str] = {}
    for k, v in src.items():
        if k.startswith("PG") or k.startswith("PRECIS_DATABASE"):
            continue
        if k.startswith("PRECIS_"):
            # Strip every other PRECIS_* var — claude doesn't need
            # to know about precis internals (and a stray DSN that
            # leaks via PRECIS_FOO_DATABASE would otherwise survive
            # the prefix filter above).
            continue
        if k in allowed_keys or any(k.startswith(p) for p in allowed_prefixes):
            out[k] = v
    out["PWD"] = str(cwd)
    return out


def _git_clone_and_branch(repo_dir: Path, dest: Path, branch: str) -> None:
    """Clone ``repo_dir`` into ``dest`` and check out ``branch``.

    Uses ``--local --no-hardlinks`` so a single file in the source
    repo working tree being modified mid-clone can't corrupt the
    clone's object store.
    """
    subprocess.run(
        ["git", "clone", "--local", "--no-hardlinks", str(repo_dir), str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=str(dest),
        check=True,
        capture_output=True,
        text=True,
    )


def _install_prepush_hook(clone_dir: Path) -> None:
    """Drop a pre-push hook that rejects pushes outside ``gripe_*``."""
    hook_dir = clone_dir / ".git" / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hook_dir / "pre-push"
    hook_path.write_text(
        "#!/usr/bin/env bash\n"
        "# precis fix_gripe pre-push guard: only branches matching\n"
        "# gripe_* may be pushed. Protects origin/main from an agent\n"
        "# pushing the wrong thing.\n"
        "while read local_ref local_sha remote_ref remote_sha; do\n"
        '  case "$remote_ref" in\n'
        "    refs/heads/gripe_*) ;;\n"
        "    *)\n"
        '      echo "[fix_gripe] refusing push to $remote_ref '
        '(only gripe_* branches may be pushed)" >&2\n'
        "      exit 1\n"
        "      ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    hook_path.chmod(0o755)


def _git_rev_parse(clone_dir: Path, refname: str) -> str | None:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", refname],
        cwd=str(clone_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    return (res.stdout or "").strip() or None


def _git_diff_stat(clone_dir: Path, base: str | None, head: str) -> str:
    if base is None:
        return "diff stats unavailable (no base)"
    res = subprocess.run(
        ["git", "diff", "--shortstat", f"{base}..{head}"],
        cwd=str(clone_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    text = (res.stdout or "").strip()
    return text or "no detectable diff"


__all__ = [
    "PARAMS_SCHEMA",
    "COMPATIBLE_EXECUTORS",
    "REQUIRES",
    "DESCRIPTION",
    "FixGripeConfig",
    "RunOutcome",
    "load_config_from_env",
    "run",
]
