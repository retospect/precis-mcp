"""Gate-slot wait UX: the rough ETA (gr515328) and the bounded wait
(gr515704).

gr515328: the wait banner showed a queue position but nothing to plan
against. ``gate_slot_release`` now appends each hold's seconds to
``<git-common>/precis-gate-stats`` (kept to the freshest 50) and the
banner prints the recent median plus the implied free-slot interval.

gr515704: the queue is fair but was unbounded — a ``--bg --selected``
builder sat 40+ min at position 1 of 5 behind three sibling gates.
``PRECIS_GATE_WAIT_MAX_S`` (default 1800) now caps the wait; on expiry the
acquire refuses rc 4 with the holder dump instead of queueing forever.

Same staging as test_gate_slot_fairness.py: the real lib sourced under
``bash -c`` against a throwaway git repo with real slot/queue dirs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: bash mkdir-mutex, pids, hostname"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = _REPO_ROOT / "scripts" / "lib" / "gate-slot.sh"


def _this_host() -> str:
    return (
        subprocess.run(
            ["hostname", "-s"], capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()
        or "unknown"
    )


@pytest.fixture(autouse=True)
def _no_slot_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """A factory build exports PRECIS_GATE_SLOT_ROOT; these tests stage
    their own slot dirs in a throwaway repo's git dir."""
    monkeypatch.delenv("PRECIS_GATE_SLOT_ROOT", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _hold_slot(repo: Path, index: int, pid: int) -> Path:
    d = repo / ".git" / f"precis-gate-slot-{index}.lock.d"
    d.mkdir()
    (d / "holder").write_text(
        f"{repo} pid={pid} host={_this_host()}\n", encoding="utf-8"
    )
    return d


def _start_acquire(
    repo: Path, done: Path, err: Path, *, slots: int = 1, extra_env: str = ""
) -> subprocess.Popen:
    """``gate_slot_acquire`` in the background; won slot -> ``done``."""
    script = (
        f'cd "{repo}"; source "{_LIB}"; {extra_env}'
        f'gate_slot_acquire; rc=$?; printf "%s" "$GATE_SLOT_DIR" > "{done}"; '
        f'exit "$rc"'
    )
    return subprocess.Popen(
        ["bash", "-c", script],
        env={**os.environ, "PRECIS_GATE_SLOTS": str(slots)},
        stderr=err.open("w", encoding="utf-8"),
    )


def _wait_for(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return path.exists()


def test_bounded_wait_refuses_with_holder_dump(repo: Path, tmp_path: Path) -> None:
    """A wait past PRECIS_GATE_WAIT_MAX_S exits 4, names the holders and
    the knob — it does not queue forever."""
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _hold_slot(repo, 0, holder.pid)
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(
            repo, done, err, slots=1, extra_env="PRECIS_GATE_WAIT_MAX_S=2; "
        )
        rc = proc.wait(timeout=30)
        text = err.read_text(encoding="utf-8")
        assert rc == 4, f"expected the bounded refusal, got rc={rc}: {text}"
        assert "gave up after 2s" in text
        assert "PRECIS_GATE_WAIT_MAX_S" in text
        assert f"pid {holder.pid}" in text
        assert not done.exists() or not done.read_text(encoding="utf-8"), (
            "a refused acquire must not report a won slot"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_unbounded_wait_still_waits(repo: Path, tmp_path: Path) -> None:
    """PRECIS_GATE_WAIT_MAX_S=0 keeps the old behaviour: no refusal, the
    waiter out-sits the cap it would have hit."""
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _hold_slot(repo, 0, holder.pid)
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(
            repo, done, err, slots=1, extra_env="PRECIS_GATE_WAIT_MAX_S=0; "
        )
        try:
            # Well past a 2 s bound; must still be queued, not refused.
            assert not _wait_for(done, timeout=7)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        assert "gave up" not in err.read_text(encoding="utf-8")
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_release_records_hold_seconds(repo: Path, tmp_path: Path) -> None:
    """Each release appends one integer hold line to precis-gate-stats —
    the ETA's only data source."""
    stats = repo / ".git" / "precis-gate-stats"
    script = (
        f'cd "{repo}"; source "{_LIB}"; gate_slot_acquire; sleep 1; gate_slot_release'
    )
    cp = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PRECIS_GATE_SLOTS": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    assert stats.is_file(), f"no stats file written: {cp.stderr}"
    lines = stats.read_text(encoding="utf-8").split()
    assert len(lines) == 1
    assert lines[0].isdigit() and int(lines[0]) >= 1


def test_wait_banner_prints_rough_eta(repo: Path, tmp_path: Path) -> None:
    """With >=3 recorded holds the wait banner carries the median-based
    rough ETA next to the queue position."""
    stats = repo / ".git" / "precis-gate-stats"
    stats.write_text("600\n700\n800\n", encoding="utf-8")
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _hold_slot(repo, 0, holder.pid)
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(repo, done, err, slots=1)
        try:
            # First announce fires on the first poll cycle; give it two.
            time.sleep(5)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        text = err.read_text(encoding="utf-8")
        assert "median recent hold ~11m" in text, text
        assert "a slot frees ~every" in text, text
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_no_eta_without_history(repo: Path, tmp_path: Path) -> None:
    """Under 3 recorded holds no ETA line prints — a median of 1-2 points
    would be noise, not a hint."""
    stats = repo / ".git" / "precis-gate-stats"
    stats.write_text("600\n", encoding="utf-8")
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _hold_slot(repo, 0, holder.pid)
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(repo, done, err, slots=1)
        try:
            time.sleep(5)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        text = err.read_text(encoding="utf-8")
        assert "median recent hold" not in text
        assert "gate slot" in text  # the position line itself still prints
    finally:
        holder.terminate()
        holder.wait(timeout=10)


# --- gr567623: access failures refuse, they do not queue forever ----------

# chmod-based denial tests cannot fail under root — mode bits are ignored
# (gr569771). getattr keeps the decorator safe where geteuid is absent.
_IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0


@pytest.mark.skipif(_IS_ROOT, reason="root ignores mode bits — 0555 denies nothing")
def test_unwritable_common_dir_refuses_at_registration(
    repo: Path, tmp_path: Path
) -> None:
    """The incident verbatim: writes to the shared git dir denied → the
    queue dir can't be created → refuse in seconds naming the path, the
    operation and the permission — not 'position 1 of 0' forever."""
    gitdir = repo / ".git"
    gitdir.chmod(0o555)
    try:
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(repo, done, err, slots=1)
        rc = proc.wait(timeout=30)
    finally:
        gitdir.chmod(0o755)
    text = err.read_text(encoding="utf-8")
    assert rc == 2, f"expected the access refusal, got rc={rc}: {text}"
    assert "precis-gate-queue.d" in text
    assert "cannot create the gate-queue dir" in text
    assert "write access" in text
    assert not done.exists() or not done.read_text(encoding="utf-8")


@pytest.mark.skipif(_IS_ROOT, reason="root ignores mode bits — 0555 denies nothing")
def test_unwritable_queue_dir_refuses_at_ticket(repo: Path, tmp_path: Path) -> None:
    """Queue dir exists but refuses the ticket write — same refusal, at
    the registration step."""
    qdir = repo / ".git" / "precis-gate-queue.d"
    qdir.mkdir()
    qdir.chmod(0o555)
    try:
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(repo, done, err, slots=1)
        rc = proc.wait(timeout=30)
    finally:
        qdir.chmod(0o755)
    text = err.read_text(encoding="utf-8")
    assert rc == 2, f"expected the access refusal, got rc={rc}: {text}"
    assert "precis-gate-queue.d" in text
    assert "register a queue ticket" in text
    assert "write access" in text


def test_uncreatable_slot_lock_refuses(repo: Path, tmp_path: Path) -> None:
    """Every slot mkdir fails while no slot dir exists — the fs is
    refusing creation, not contention. Two all-free scans then refuse
    rc 2 instead of spinning 'all slots free'."""
    blocker = repo / ".git" / "precis-gate-slot-0.lock.d"
    blocker.write_text("not a lock dir\n", encoding="utf-8")
    done = tmp_path / "done"
    err = tmp_path / "acquire.err"
    proc = _start_acquire(repo, done, err, slots=1)
    rc = proc.wait(timeout=30)
    text = err.read_text(encoding="utf-8")
    assert rc == 2, f"expected the access refusal, got rc={rc}: {text}"
    assert "cannot create a gate-slot lock" in text
    assert "precis-gate-slot-" in text
    assert "write access" in text


# --- PRECIS_GATE_SLOT_ROOT: attempts in clones share the host's semaphore --


def _slot_root(tmp_path: Path) -> Path:
    root = tmp_path / "host-common"
    root.mkdir()
    return root


def test_slot_root_override_holds_slot_and_stats_outside_the_clone(
    repo: Path, tmp_path: Path
) -> None:
    """A clone's own git dir must stay untouched: the slot dir, the queue
    and the stats file all live in PRECIS_GATE_SLOT_ROOT, and a release
    cleans the slot there."""
    root = _slot_root(tmp_path)
    script = (
        f'cd "{repo}"; source "{_LIB}"; gate_slot_acquire || exit $?; '
        'printf "%s" "$GATE_SLOT_DIR" > "$OUT"; sleep 1; gate_slot_release'
    )
    out = tmp_path / "slot-dir"
    subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PRECIS_GATE_SLOTS": "1",
            "PRECIS_GATE_SLOT_ROOT": str(root),
            "OUT": str(out),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    assert out.read_text(encoding="utf-8") == f"{root}/precis-gate-slot-0.lock.d"
    assert (root / "precis-gate-queue.d").is_dir()
    assert not list((root / "precis-gate-queue.d").iterdir()), "ticket left behind"
    assert not (root / "precis-gate-slot-0.lock.d").exists(), "slot not released"
    assert int((root / "precis-gate-stats").read_text(encoding="utf-8").split()[0]) >= 1
    leftovers = [p.name for p in (repo / ".git").iterdir() if "precis-gate" in p.name]
    assert not leftovers, f"the clone's own git dir was used: {leftovers}"


def test_slot_root_is_one_semaphore_across_separate_repositories(
    repo: Path, tmp_path: Path
) -> None:
    """A slot held by a live holder in the root blocks a waiter whose own
    git dir has no slot at all (a clone), and the waiter takes it once the
    holder is gone."""
    root = _slot_root(tmp_path)
    holder = subprocess.Popen(["sleep", "60"])
    try:
        d = root / "precis-gate-slot-0.lock.d"
        d.mkdir()
        (d / "holder").write_text(
            f"/elsewhere/other-tree pid={holder.pid} host={_this_host()}\n",
            encoding="utf-8",
        )
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(
            repo,
            done,
            err,
            slots=1,
            extra_env=f'export PRECIS_GATE_SLOT_ROOT="{root}"; ',
        )
        try:
            assert not _wait_for(done, timeout=5), "took a slot held in the root"
            text = err.read_text(encoding="utf-8")
            assert "other-tree" in text, text  # the holder dump reads the root
            assert any((root / "precis-gate-queue.d").iterdir()), "ticket not in root"
            holder.terminate()
            holder.wait(timeout=10)
            assert _wait_for(done, timeout=20), "did not take the freed slot"
            assert done.read_text(encoding="utf-8") == str(d)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)


def test_slot_root_missing_directory_refuses(repo: Path, tmp_path: Path) -> None:
    """A root that does not exist is a refusal (rc 2) naming the variable,
    never a quiet fallback to the clone's own git dir: that would run the
    gate outside the shared cap."""
    done = tmp_path / "done"
    err = tmp_path / "acquire.err"
    missing = tmp_path / "nope"
    proc = _start_acquire(
        repo,
        done,
        err,
        slots=1,
        extra_env=f'export PRECIS_GATE_SLOT_ROOT="{missing}"; ',
    )
    rc = proc.wait(timeout=30)
    text = err.read_text(encoding="utf-8")
    assert rc == 2, f"expected the refusal, got rc={rc}: {text}"
    assert "PRECIS_GATE_SLOT_ROOT" in text
    assert "not an existing directory" in text
    assert not list((repo / ".git").glob("precis-gate-*")), "fell back to the git dir"


def test_empty_slot_root_is_unset(repo: Path, tmp_path: Path) -> None:
    """An empty value behaves as unset: the git common dir is used."""
    done = tmp_path / "done"
    err = tmp_path / "acquire.err"
    proc = _start_acquire(
        repo, done, err, slots=1, extra_env='export PRECIS_GATE_SLOT_ROOT=""; '
    )
    assert proc.wait(timeout=30) == 0, err.read_text(encoding="utf-8")
    assert done.read_text(encoding="utf-8") == ".git/precis-gate-slot-0.lock.d"


# --- attempt clones without the override fail closed ------------------------
#
# A factory attempt is a standalone clone at
# <host>/.claude/worktrees/slice-td<N>[-land]-j<M>; without the override its
# own .git would be a private semaphore, so the root is derived from the path.


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    return path


def _clone(host: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(host), str(dest)], check=True)
    return dest


def _slot_root_in(tree: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PRECIS_GATE_SLOT_ROOT"}
    return subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; _gate_slot_root'],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.mark.parametrize("name", ["slice-td7-j9", "slice-td7-land-j9"])
def test_attempt_clone_without_override_uses_the_hosts_git_dir(
    tmp_path: Path, name: str
) -> None:
    host = _git_init(tmp_path / "host")
    clone = _clone(host, host / ".claude" / "worktrees" / name)
    out = _slot_root_in(clone)
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()).resolve() == (host / ".git").resolve()

    # The real acquire/release creates the slot under the host, not the clone.
    script = (
        f'cd "{clone}"; source "{_LIB}"; unset PRECIS_GATE_SLOT_ROOT; '
        'gate_slot_acquire || exit $?; printf "%s" "$GATE_SLOT_DIR" > "$OUT"; '
        "sleep 0.5; gate_slot_release"
    )
    seen = tmp_path / "slot-dir"
    subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PRECIS_GATE_SLOTS": "1", "OUT": str(seen)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    slot = Path(seen.read_text(encoding="utf-8"))
    assert slot.parent.resolve() == (host / ".git").resolve()
    assert (host / ".git" / "precis-gate-queue.d").is_dir()
    leftovers = [p.name for p in (clone / ".git").iterdir() if "precis-gate" in p.name]
    assert not leftovers, f"the clone's own git dir was used: {leftovers}"


def test_clone_outside_the_attempt_layout_keeps_its_own_git_dir(
    tmp_path: Path,
) -> None:
    host = _git_init(tmp_path / "host")
    clone = _clone(host, tmp_path / "elsewhere" / "clone")
    out = _slot_root_in(clone)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ".git"


@pytest.mark.parametrize(
    "name", ["slice-td7-j9.cut-123", "slice-tdx-j9", "slice-td7-j9-foo"]
)
def test_near_miss_attempt_names_do_not_match(tmp_path: Path, name: str) -> None:
    host = _git_init(tmp_path / "host")
    clone = _clone(host, host / ".claude" / "worktrees" / name)
    out = _slot_root_in(clone)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ".git"


def test_attempt_clone_with_underivable_host_refuses(tmp_path: Path) -> None:
    # <tmp>/nohost is not a git checkout: the layout matches, the host does not.
    clone = _git_init(tmp_path / "nohost" / ".claude" / "worktrees" / "slice-td7-j9")
    out = _slot_root_in(clone)
    assert out.returncode == 1, (out.returncode, out.stdout, out.stderr)
    assert "PRECIS_GATE_SLOT_ROOT" in out.stderr
    assert out.stdout.strip() == ""

    # gate_slot_acquire turns that into its documented refusal, rc 2.
    done = tmp_path / "done"
    err = tmp_path / "acquire.err"
    proc = _start_acquire(
        clone, done, err, slots=1, extra_env="unset PRECIS_GATE_SLOT_ROOT; "
    )
    assert proc.wait(timeout=30) == 2
    assert "PRECIS_GATE_SLOT_ROOT" in err.read_text(encoding="utf-8")
    assert not list((clone / ".git").glob("precis-gate-*"))
