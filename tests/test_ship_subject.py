"""``scripts/lib/ship-subject.sh``: squash-subject resolution + lint.

The pure functions are driven in a throwaway git repo (no gate, no network);
the last test runs the REAL ``scripts/ship`` far enough to prove a WIP-only
branch dies before any lint/gate work.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib" / "ship-subject.sh"
SHIP = REPO / "scripts" / "ship"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    ).stdout


def _repo(tmp_path: Path, subjects: list[str]) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    for s in subjects:
        _git(repo, "commit", "-q", "--allow-empty", "-m", s)
    return repo


def _resolve(repo: Path, msg: str = "", **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"; ship_resolve_msg "$1" origin/main', "_", msg],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={k: v for k, v in os.environ.items() if k != "PRECIS_SHIP_SUBJECT_LINT"}
        | env,
    )


def test_single_commit_message_keeps_its_trailers(tmp_path: Path) -> None:
    """gr517803: the branch commit's trailer block rides along verbatim —
    a non-Claude builder's footer must reach main, not be replaced by a
    substituted Claude one."""
    repo = _repo(
        tmp_path,
        [
            "fix(ship): derive subject\n\nWhy: placeholders are useless.\n\n"
            "Co-Authored-By: X <x@example.invalid>\nClaude-Session: https://x"
        ],
    )
    r = _resolve(repo)
    assert r.returncode == 0, r.stderr
    assert r.stdout == (
        "fix(ship): derive subject\n\nWhy: placeholders are useless.\n\n"
        "Co-Authored-By: X <x@example.invalid>\nClaude-Session: https://x\n"
    )


def test_several_commits_carry_their_trailers_deduped(tmp_path: Path) -> None:
    """The composed multi-commit message is subjects only — carry the
    commits' trailer blocks over too or a multi-commit branch loses its
    footer the same way (gr517803)."""
    repo = _repo(
        tmp_path,
        [
            "fix(a): first\n\nCo-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
            "Claude-Session: none (Devin builder, fleet)",
            "fix(a): second\n\nCo-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
            "Signed-off-by: T <t@example.invalid>",
        ],
    )
    r = _resolve(repo)
    assert r.returncode == 0, r.stderr
    # shas arrive newest-first from rev-list; each trailer keeps its first
    # occurrence.
    assert r.stdout == (
        "fix(a): first\n\nAlso:\n- fix(a): second\n\n"
        "Co-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
        "Signed-off-by: T <t@example.invalid>\n"
        "Claude-Session: none (Devin builder, fleet)\n"
    )


def test_several_same_prefix_commits_oldest_subject_plus_also(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        [
            "fix(a): first",
            "wip(branch): end-of-session snapshot",
            "fix(a): second",
            "fix(a): third",
        ],
    )
    r = _resolve(repo)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "fix(a): first\n\nAlso:\n- fix(a): second\n- fix(a): third\n"


def test_two_commits_with_different_prefixes_refuse_naming_m(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        ["fix(web): pcb page renders a string DRC status", "test(web): pin escaping"],
    )
    r = _resolve(repo)
    assert r.returncode == 1
    assert "pass -m '<type(scope): what changed>'" in r.stderr
    assert "fix(web): pcb page renders a string DRC status" in r.stderr
    assert r.stdout == ""
    # -m resolves it.
    assert (
        _resolve(repo, "fix(web): pcb page renders a string DRC status").returncode == 0
    )


def test_only_wip_dies(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["wip(branch): end-of-session snapshot"])
    r = _resolve(repo)
    assert r.returncode == 1
    assert (
        "no commit message: pass -m 'type(scope): what changed' or commit your "
        "work with a real subject before shipping" in r.stderr
    )


def test_no_commits_dies(tmp_path: Path) -> None:
    assert _resolve(_repo(tmp_path, [])).returncode == 1


def test_bad_subject_via_m_dies(tmp_path: Path) -> None:
    repo = _repo(tmp_path, [])
    for bad in (
        "ship(branch): squash-merge to main",
        "wip(x): snapshot",
        "fixed stuff",
        "feat(Bad Scope): x",
        "fix: " + "x" * 80,
    ):
        r = _resolve(repo, bad)
        assert r.returncode == 1, bad
        assert "PRECIS_SHIP_SUBJECT_LINT=0" in r.stderr


def test_good_subject_via_m_passes(tmp_path: Path) -> None:
    r = _resolve(_repo(tmp_path, []), "fix(ship)!: refuse placeholders")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "fix(ship)!: refuse placeholders\n"


def test_lint_off_via_env(tmp_path: Path) -> None:
    r = _resolve(_repo(tmp_path, []), "whatever", PRECIS_SHIP_SUBJECT_LINT="0")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "whatever\n"


def _fn(func: str, msg: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Pipe ``msg`` through a ship-subject.sh helper."""
    return subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; _m="$2"; shift 2; printf "%s" "$_m" | ' + func + ' "$@"',
            "_",
            str(LIB),
            msg,
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_msg_body_strips_the_trailer_block() -> None:
    r = _fn(
        "ship_msg_body",
        "fix(ship): x\n\nbody line\n\nCo-Authored-By: D <d@x>\nSigned-off-by: S <s@x>\n",
    )
    assert r.returncode == 0
    assert r.stdout == "fix(ship): x\n\nbody line\n"


def test_msg_body_never_eats_a_bare_subject() -> None:
    """A lone 'fix: x' line matches the trailer regex — the block needs a
    blank line above it, so it must survive as the body."""
    r = _fn("ship_msg_body", "fix: x")
    assert r.returncode == 0
    assert r.stdout == "fix: x\n"


def test_mint_trailers_keeps_author_footer_no_substitute() -> None:
    """gr517803 verbatim: a Devin footer stays; no Claude footer is minted."""
    msg = (
        "fix(ship): x\n\nCo-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
        "Claude-Session: none (Devin builder, F)\n"
    )
    r = _fn(
        "ship_mint_trailers", msg, "", "Co-Authored-By: Claude <noreply@anthropic.com>"
    )
    assert r.returncode == 0
    assert r.stdout == (
        "Co-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
        "Claude-Session: none (Devin builder, F)\n"
    )


def test_mint_trailers_adds_fallback_only_when_no_coauthor() -> None:
    r = _fn(
        "ship_mint_trailers",
        "fix(ship): x\n",
        "",
        "Co-Authored-By: Claude <noreply@anthropic.com>",
    )
    assert r.returncode == 0
    assert r.stdout == "Co-Authored-By: Claude <noreply@anthropic.com>\n"


def test_mint_trailers_prepends_gate_and_drops_stale_gate() -> None:
    msg = "fix(ship): x\n\nGate: stale from before\nCo-Authored-By: D <d@x>\n"
    r = _fn(
        "ship_mint_trailers",
        msg,
        "Gate: lint only; forward-merged over 1",
        "Co-Authored-By: Claude <noreply@anthropic.com>",
    )
    assert r.returncode == 0
    assert r.stdout == (
        "Gate: lint only; forward-merged over 1\nCo-Authored-By: D <d@x>\n"
    )


def test_branch_attr_trailers_dedup_order_skip_wip(tmp_path: Path) -> None:
    """gr538832: the branch's Co-Authored-By/Claude-Session trailers,
    deduped oldest-commit-first; a wip(*) snapshot's ship-minted footer is
    not attribution and stays out."""
    repo = _repo(
        tmp_path,
        [
            "fix(a): one\n\nCo-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
            "Claude-Session: none (Devin builder, x)",
            "wip(b): snapshot\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
            "fix(a): two\n\nCo-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
            "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
            "Signed-off-by: T <t@example.invalid>",
        ],
    )
    r = subprocess.run(
        ["bash", "-c", '. "$1"; ship_branch_attr_trailers origin/main', "_", str(LIB)],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == (
        "Co-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
        "Claude-Session: none (Devin builder, x)\n"
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
    )


def test_lowercase_coauthored_trailer_carried_no_fallback(tmp_path: Path) -> None:
    """Trailer keys are case-insensitive: GitHub-style "Co-authored-by:" is
    carried over from the branch, and the generic fallback is not added."""
    repo = _repo(
        tmp_path,
        ["fix(a): one\n\nCo-authored-by: Dev <dev@example.invalid>"],
    )
    r = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; attrs="$(ship_branch_attr_trailers origin/main)"; '
            'printf "fix(a): one\\n" | ship_mint_trailers "" "$2" "$attrs"',
            "_",
            str(LIB),
            "Co-Authored-By: Claude <noreply@anthropic.com>",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "Co-authored-by: Dev <dev@example.invalid>\n"


def test_mint_trailers_merges_branch_attrs_and_gates_fallback() -> None:
    """gr538832: $3 merges the branch's attribution trailers after the
    message's own (deduped); the fallback is minted only when neither the
    message nor $3 carries a Co-Authored-By."""
    r = _fn(
        "ship_mint_trailers",
        "fix(ship): x\n",
        "Gate: lint only",
        "Co-Authored-By: Claude <noreply@anthropic.com>",
        "Co-Authored-By: Devin SWE-2 <noreply@cognition.ai>\nClaude-Session: none (d)",
    )
    assert r.returncode == 0
    assert r.stdout == (
        "Gate: lint only\nCo-Authored-By: Devin SWE-2 <noreply@cognition.ai>\n"
        "Claude-Session: none (d)\n"
    )


def test_ship_dies_on_wip_only_branch_before_the_gate(tmp_path: Path) -> None:
    """The real script refuses at the WIP step: nothing after it ran."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "w"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "checkout", "-q", "-b", "feat-x")
    scripts = repo / "scripts" / "lib"
    scripts.mkdir(parents=True)
    (repo / "scripts" / "ship").write_bytes(SHIP.read_bytes())
    (scripts / "ship-subject.sh").write_bytes(LIB.read_bytes())
    for lib in (REPO / "scripts" / "lib").glob("*.sh"):
        (scripts / lib.name).write_bytes(lib.read_bytes())
    (repo / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    # Ship refuses a dirty tree (gr489288), so the placeholder is committed
    # by hand, as an older ship's WIP step would have.
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip(feat-x): end-of-session snapshot")
    r = subprocess.run(
        ["bash", "scripts/ship", "--quick"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no commit message: pass -m" in r.stderr
    assert "syncing origin/main" not in r.stdout
