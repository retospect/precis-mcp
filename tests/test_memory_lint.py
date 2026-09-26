"""``scripts/memory-lint`` check 2 — the landed-thread scan.

Found via docs/backlog/memory-lint-threads-scan-dead.md: check 2 sliced
``MEMORY.md`` with ``awk '/^## Threads/{f=1;next} /^## /{f=0} f'``, but the
live index (``~/.claude/projects/<repo>/memory/MEMORY.md``) is a flat bullet
list with zero ``##`` headings. The slice came back empty, the scan loop
never ran, and the session-start hygiene line still printed "no landed
threads lingering" — a false negative that silenced the one auto-catch that
was supposed to drive thread retirement.

Exercised against the REAL script, never reimplemented — same technique as
tests/test_deploy_lag_honesty.py: ``scripts/memory-lint`` is copied
byte-for-byte into a throwaway ``git init`` repo at its real relative path
(it resolves its own repo root off ``dirname "$0"/..`` and, from there,
derives ``$HOME/.claude/projects/<escaped-root>/memory`` via
``git rev-parse --git-common-dir``). A throwaway repo, not this worktree, is
required: a worktree-isolated container test can't see this worktree's real
shared ``.git`` (it lives outside the mounted subtree), so any git op against
the real repo fails with "not a git repository" in-container.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only bash/awk/git script"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_LINT_SRC = REPO_ROOT / "scripts" / "memory-lint"


def _today() -> str:
    return datetime.datetime.now(datetime.UTC).date().isoformat()


def _test_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_test_env(), capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


@pytest.fixture
def lint_repo(tmp_path: Path) -> Path:
    """A throwaway repo carrying the REAL scripts/memory-lint at its real
    relative path, plus an in-window sibling-repo weekly-gate stamp (check 6
    always runs and self-stamps; giving it a fresh stamp keeps these tests
    from tripping that unrelated append)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("root\n", encoding="utf-8")

    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(MEMORY_LINT_SRC, scripts_dir / "memory-lint")
    (scripts_dir / "memory-lint").chmod(0o755)

    runbooks_dir = repo / "docs" / "runbooks"
    runbooks_dir.mkdir(parents=True)
    (runbooks_dir / "memory-sibling-repos.md").write_text(
        f"## Log\n\n**{_today()}** — placeholder\n", encoding="utf-8"
    )

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add memory-lint")
    # memory-lint only reads a hex token as a sha when it contains an a-f
    # letter, so a plain digit run (a byte count, a year) isn't mistaken for
    # a commit. That makes an all-digit short sha a ~1-in-150 fixture draw
    # that empties the scan's sha list: the landed-thread tests below would
    # miss their line, and the open-work-words test would pass for the wrong
    # reason. Re-roll the commit until the short form carries a letter.
    for attempt in range(50):
        if any(c in "abcdef" for c in _landed_sha(repo)):
            break
        _git(repo, "commit", "-q", "--amend", "-m", f"add memory-lint {attempt}")
    else:  # pragma: no cover - 50 consecutive all-digit shas
        pytest.fail("no fixture sha with a hex letter after 50 attempts")
    return repo


def _main_root(repo: Path) -> str:
    """Same derivation memory-lint uses: parent of the git-common-dir."""
    out = _git(
        repo, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).stdout.strip()
    return str(Path(out).parent)


def _landed_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short=12", "HEAD").stdout.strip()


@pytest.fixture
def home_and_mem(lint_repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A fake $HOME whose memory dir matches the path scripts/memory-lint
    derives from ``lint_repo``, so the script finds our fixture INDEX."""
    home = tmp_path / "home"
    escaped = _main_root(lint_repo).replace("/", "-")
    mem = home / ".claude" / "projects" / escaped / "memory"
    mem.mkdir(parents=True)
    return home, mem


def _run(repo: Path, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "scripts" / "memory-lint")],
        cwd=str(repo),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_flat_index_flags_missing_threads_heading(
    lint_repo: Path, home_and_mem: tuple[Path, Path]
) -> None:
    """A flat index (no `## ` headings at all) must surface a finding, not
    the false-clean "no landed threads lingering" line."""
    home, mem = home_and_mem
    (mem / "MEMORY.md").write_text(
        "- some durable fact about the system, no section headings here.\n"
        "- another note, still flat.\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home)

    assert res.returncode == 0
    assert "no `## Threads` heading" in res.stdout, res.stdout
    assert "no landed threads lingering" not in res.stdout, res.stdout


def test_flat_index_still_scans_every_bullet_for_landed_threads(
    lint_repo: Path, home_and_mem: tuple[Path, Path]
) -> None:
    """Even with no `## Threads` heading, a linked topic file whose every
    cited sha is an ancestor of main and carries no open-work words must
    still be flagged as landed — the scan can't depend on section structure
    that the live index doesn't have."""
    home, mem = home_and_mem
    sha = _landed_sha(lint_repo)
    (mem / "MEMORY.md").write_text(
        "- some campaign — [detail](some-topic.md)\n",
        encoding="utf-8",
    )
    (mem / "some-topic.md").write_text(
        f"state: SHIPPED. commit {sha} landed in main.\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home)

    assert res.returncode == 0
    assert "landed thread → some-topic.md" in res.stdout, res.stdout


def test_open_work_words_still_suppress_the_landed_flag(
    lint_repo: Path, home_and_mem: tuple[Path, Path]
) -> None:
    """Regression guard: the widened scan must keep honouring the
    open-work-words exclusion (a live thread with a landed sub-part isn't a
    landed thread)."""
    home, mem = home_and_mem
    sha = _landed_sha(lint_repo)
    (mem / "MEMORY.md").write_text(
        "- some campaign — [detail](some-topic.md)\n",
        encoding="utf-8",
    )
    (mem / "some-topic.md").write_text(
        f"state: SHIPPED sub-part at {sha}; NEXT step still pending.\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home)

    assert res.returncode == 0
    assert "landed thread → some-topic.md" not in res.stdout, res.stdout


def test_kebab_case_broken_link_is_reported(
    lint_repo: Path, home_and_mem: tuple[Path, Path]
) -> None:
    """gr450546: check 1a's link-extraction regex had the same missing-hyphen
    character class as check 2's — a kebab-case link (every real topic file
    name) to a MISSING target must be flagged as broken."""
    home, mem = home_and_mem
    (mem / "MEMORY.md").write_text(
        "- some campaign — [detail](some-missing-topic.md)\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home)

    assert res.returncode == 0
    assert "broken link → some-missing-topic.md" in res.stdout, res.stdout
