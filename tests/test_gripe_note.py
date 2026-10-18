"""scripts/gripe-note and ``precis gripes --note`` — landed/deployed comments.

scripts/ship and scripts/release call scripts/gripe-note after a landing or
rollout; it collects the gripes a commit FIXES (subject ``(grN)`` or a
``Fixes: grN`` trailer, never prose mentions) and posts one comment on each
through ``precis gripes --note`` (one prod call, idempotent). Pinned here:
the matcher, the dry-run/off-switch paths, that a failed or timed-out post
never fails or holds ship/release, and the in-process poster on the test DB.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

from precis.cli.gripe import note_gripes
from precis.store import Store
from tests._gripe import insert_gripe
from tests.test_ship_narrow_lock import Rig, _finish, _tree_files

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "gripe-note"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs scripts in a git repo"
)


def _load(path: Path, name: str, tmp_path: Path) -> ModuleType:
    # A .py copy: a dotless script path does not load as a module.
    copy = tmp_path / f"{name}.py"
    copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return SourceFileLoader(name, str(copy)).load_module()


@pytest.mark.parametrize(
    ("message", "want"),
    [
        ("fix(x): y (gr12)\n\nbody", [12]),
        ("fix(x): y (gr12, gr13)", [12, 13]),
        ("fix(x): y\n\nFixes: gr34, gr35\nfixes: gr36 gr34", [34, 35, 36]),
        ("fix(x): y (gr1)\n\nFixes: gr2", [1, 2]),
        # Context mentions never match: they are not fixes.
        ("fix(x): y\n\nblocks gr493201; related gr493180", []),
        ("fix(x): y\n\nper gr489909 c6, this fixes gr7 in passing", []),
        ("fix(x): gr12 is half done\n\nsee gr5", []),
        ("fix(x): y (gr12) and more", []),
        ("fix(x): y\n\nFixes: gr8 and also something", []),
    ],
)
def test_fixed_gripe_ids(tmp_path: Path, message: str, want: list[int]) -> None:
    mod = _load(SCRIPT, "gripe_note", tmp_path)
    assert mod.fixed_gripe_ids(message) == want


def _repo(tmp_path: Path, *messages: str) -> tuple[Path, list[str]]:
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    shas = []
    for i, msg in enumerate(messages):
        (root / "f").write_text(str(i), encoding="utf-8")
        git("add", "f")
        git("commit", "-q", "-m", msg)
        shas.append(git("rev-parse", "--short=9", "HEAD"))
    return root, shas


def _run(root: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **env},
        timeout=60,
    )


def _fake_cli(tmp_path: Path, body: str) -> str:
    fake = tmp_path / "fake-prod-precis"
    fake.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    fake.chmod(0o755)
    return str(fake)


def test_dry_run_names_the_fixed_gripes_and_the_sha(tmp_path: Path) -> None:
    root, (sha,) = _repo(tmp_path, "fix(a): b (gr7)\n\nFixes: gr8\nrelated gr9")
    cp = _run(root, "--rev", "HEAD", "--text", "landed {sha} on main", "--dry-run")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == (
        f"gripe-note: would post 'landed {sha} on main' on gr7 gr8"
    )


def test_range_reads_every_commit(tmp_path: Path) -> None:
    root, shas = _repo(
        tmp_path, "chore: base", "fix(a): b (gr7)", "fix(c): d\n\nblocks gr9"
    )
    cp = _run(
        root, "--rev", "HEAD~2..HEAD", "--text", "deployed r1 ({sha})", "--dry-run"
    )
    assert cp.stdout.strip() == (
        f"gripe-note: would post 'deployed r1 ({shas[-1]})' on gr7"
    )


def test_related_and_blocks_mentions_post_nothing(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path, "fix(a): b\n\nrelated gr1\nblocks gr2")
    cli = _fake_cli(tmp_path, f"touch {tmp_path}/called")
    cp = _run(root, "--rev", "HEAD", "--text", "t", PRECIS_GRIPE_NOTE_CLI=cli)
    assert cp.returncode == 0
    assert cp.stdout == ""
    assert not (tmp_path / "called").exists()


def test_off_switch_skips_and_exits_zero(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path, "fix(a): b (gr7)")
    cp = _run(root, "--rev", "HEAD", "--text", "t", PRECIS_GRIPE_NOTE="0")
    assert cp.returncode == 0
    assert cp.stdout.strip() == "gripe-note: PRECIS_GRIPE_NOTE=0 — skipped gr7"


def test_unreadable_rev_exits_zero(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path, "fix(a): b (gr7)")
    cp = _run(root, "--rev", "nope..alsonope", "--text", "t")
    assert cp.returncode == 0
    assert "cannot read" in cp.stdout


def test_posts_through_the_cli_and_reports(tmp_path: Path) -> None:
    root, (sha,) = _repo(tmp_path, "fix(a): b (gr7)")
    cli = _fake_cli(tmp_path, f'echo "$@" > {tmp_path}/args; echo "commented gr7"')
    cp = _run(
        root, "--rev", "HEAD", "--text", "landed {sha}", PRECIS_GRIPE_NOTE_CLI=cli
    )
    assert cp.returncode == 0
    assert cp.stdout.strip() == "gripe-note: commented gr7"
    args = (tmp_path / "args").read_text(encoding="utf-8").strip()
    assert args == f"gripes --note landed {sha} --ids 7"


def test_failed_post_is_logged_and_exits_zero(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path, "fix(a): b (gr7)")
    # A fake DSN for the redaction check, assembled so the source carries no
    # credential-shaped URL (tests/test_deploy_tree_no_secrets.py).
    dsn = "postgresql://" + ":".join(("u", "p")) + "@h/db"
    cli = _fake_cli(tmp_path, f"echo 'boom {dsn}' >&2; exit 1")
    cp = _run(root, "--rev", "HEAD", "--text", "t", PRECIS_GRIPE_NOTE_CLI=cli)
    assert cp.returncode == 0
    assert cp.stdout.strip() == "gripe-note: posting on gr7 failed (exit 1): boom <url>"


def test_timed_out_post_is_logged_and_exits_promptly(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path, "fix(a): b (gr7)")
    cli = _fake_cli(tmp_path, "sleep 30")
    t0 = time.monotonic()
    cp = _run(
        root,
        "--rev",
        "HEAD",
        "--text",
        "t",
        PRECIS_GRIPE_NOTE_CLI=cli,
        PRECIS_GRIPE_NOTE_TIMEOUT="1",
    )
    assert time.monotonic() - t0 < 15
    assert cp.returncode == 0
    assert "TimeoutExpired posting on gr7" in cp.stdout


def test_prod_call_cap_fits_inside_the_release_wait(tmp_path: Path) -> None:
    note = _load(SCRIPT, "gripe_note_cap", tmp_path)
    release = _load(REPO / "scripts" / "release", "release_cap", tmp_path)
    assert note.TIMEOUT_S < release.NOTE_TIMEOUT_S <= 30


@pytest.mark.parametrize("body", ["sleep 30", "echo boom >&2; exit 1"])
def test_release_note_never_fails_or_holds_the_deploy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    """A hung or failing gripe-note costs the deploy path at most
    NOTE_TIMEOUT_S and never raises."""
    release = _load(REPO / "scripts" / "release", "release_note", tmp_path)
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    fake = fake_dir / "gripe-note"
    fake.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(release, "HERE", fake_dir)
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "NOTE_TIMEOUT_S", 1.0)
    t0 = time.monotonic()
    release.note_gripes("aaaa", "bbbb", "deployed r1 ({sha})")
    assert time.monotonic() - t0 < 15
    out = capsys.readouterr().out
    if body.startswith("sleep"):
        assert "gripe-note: TimeoutExpired" in out


def test_ship_lands_when_the_gripe_note_fails(tmp_path: Path) -> None:
    """A failing gripe-note after the push never changes ship's exit status."""
    rig = Rig(tmp_path)
    wt = rig.worktree("gn", {"gn.txt": "g\n"})
    fake = wt / "scripts" / "gripe-note"
    fake.write_text(
        "#!/bin/sh\necho 'gripe-note: posting failed (fake)'; exit 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    rig.g(wt, "add", "-A")
    rig.g(wt, "commit", "-q", "--amend", "--no-edit")
    try:
        p, log = rig.ship(wt, "--quick", sleep=1)
        out = _finish(p, log)
    finally:
        rig.release()
    assert "gn.txt" in _tree_files(rig)
    assert "gripe-note: posting failed (fake)" in out


def test_note_gripes_posts_once_and_reports(store: Store) -> None:
    a = insert_gripe(store, "first landed probe").id
    b = insert_gripe(store, "second landed probe").id
    line = note_gripes(store, "landed abc123456 on main", [a, b, 99999999])
    assert line == f"commented gr{a} gr{b} · not found: gr99999999"
    for gid in (a, b):
        texts = [
            c.text
            for c in store.chunks.list_chunks_for_ref(gid)
            if c.chunk_kind == "gripe_comment"
        ]
        assert texts == ["landed abc123456 on main"]

    again = note_gripes(store, "landed abc123456 on main", [a, b])
    assert again == f"already had it: gr{a} gr{b}"
    comments = [
        c
        for c in store.chunks.list_chunks_for_ref(a)
        if c.chunk_kind == "gripe_comment"
    ]
    assert len(comments) == 1
