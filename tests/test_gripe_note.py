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
from pathlib import Path
from types import ModuleType

import pytest

from precis.cli.gripe import note_gripes
from precis.store import Store
from tests._refs import insert_gripe
from tests._script import load_script
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
    return load_script(name, copy)


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


def _g(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(repo: Path, n: int) -> str:
    (repo / "f").write_text(str(n), encoding="utf-8")
    _g(repo, "add", "f")
    _g(
        repo,
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        f"c{n}",
    )
    return _g(repo, "rev-parse", "HEAD")


def _prod_rig(tmp_path: Path) -> tuple[Path, Path, Path, list[str]]:
    """A bare origin, a seed clone that pushes c1..c3, and the release owner's
    checkout ``owner`` (at c1). Origin's ``prod`` starts at c1."""
    origin = tmp_path / "origin.git"
    _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _g(tmp_path, "clone", "-q", str(origin), str(seed))
    shas = [_commit(seed, n) for n in (1, 2, 3)]
    _g(seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    _g(seed, "push", "-q", "origin", f"{shas[0]}:refs/heads/prod")
    owner = tmp_path / "owner"
    _g(tmp_path, "clone", "-q", str(origin), str(owner))
    _g(owner, "checkout", "-q", "--detach", shas[0])
    return origin, seed, owner, shas


def _prod_clone(tmp_path: Path, origin: Path, sha: str) -> Path:
    clone = tmp_path / "prod-clone"
    _g(tmp_path, "clone", "-q", str(origin), str(clone))
    _g(clone, "checkout", "-q", "--detach", sha)
    (clone / "scripts").mkdir()
    (clone / "scripts" / "prod-precis").write_text("#!/bin/sh\n", encoding="utf-8")
    return clone


def test_write_tree_keeps_a_tree_that_contains_origin_prod(tmp_path: Path) -> None:
    _, _, owner, _ = _prod_rig(tmp_path)
    mod = _load(SCRIPT, "gripe_note", tmp_path)
    assert mod.write_tree(owner) == owner


def test_write_tree_moves_a_stale_owner_to_the_prod_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, seed, owner, shas = _prod_rig(tmp_path)
    _g(seed, "push", "-q", "origin", f"{shas[2]}:refs/heads/prod")  # the deploy
    clone = _prod_clone(tmp_path, origin, shas[2])
    monkeypatch.setenv("PRECIS_PROD_CHECKOUT", str(clone))
    mod = _load(SCRIPT, "gripe_note", tmp_path)
    # The owner's checkout never fetched the move: the local origin/prod is
    # still c1, so only a fetch-then-compare sees it is behind.
    assert _g(owner, "rev-parse", "origin/prod") == shas[0]
    assert mod.write_tree(owner) == clone


def test_write_tree_refuses_to_pick_a_stale_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, seed, owner, shas = _prod_rig(tmp_path)
    _g(seed, "push", "-q", "origin", f"{shas[2]}:refs/heads/prod")
    clone = _prod_clone(tmp_path, origin, shas[1])  # behind origin/prod
    monkeypatch.setenv("PRECIS_PROD_CHECKOUT", str(clone))
    mod = _load(SCRIPT, "gripe_note", tmp_path)
    # Nothing contains origin/prod: fall back to the caller's tree, whose
    # prod-precis guard then refuses the write.
    assert mod.write_tree(owner) == owner


def test_write_tree_without_a_prod_clone_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, seed, owner, shas = _prod_rig(tmp_path)
    _g(seed, "push", "-q", "origin", f"{shas[2]}:refs/heads/prod")
    monkeypatch.setenv("PRECIS_PROD_CHECKOUT", str(tmp_path / "absent"))
    mod = _load(SCRIPT, "gripe_note", tmp_path)
    assert mod.write_tree(owner) == owner
