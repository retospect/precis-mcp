"""gr741489: the factory carries a corrective commit onto release/r<N>.

Real git — a bare origin, the factory checkout ``root`` and a separate
author clone — for ``release_carry.carry``; the DB store for the pass,
the carry node and its recorded outcome. The tick's half (when a carry
is minted, when the ask files) is in tests/test_release_poll.py.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from precis import settings as psettings
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.advisory import try_xact_advisory_lock
from precis.store.types import ChunkInsert, Tag
from precis.workers import factory_git as _fg
from precis.workers.job_types import release_carry as rc
from precis.workers.job_types import release_poll as rp
from precis.workers.job_types import run_slice as rs

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX git rig")

_LINES = [f"l{i}" for i in range(1, 21)]


def _g(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


class Rig:
    """``origin`` (bare), ``root`` (the factory checkout the carry runs
    from) and ``work`` (where the tests author commits, so ``root``'s
    own working tree is only ever touched by the code under test)."""

    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        self.root = tmp_path / "root"
        self.work = tmp_path / "work"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.work)], check=True
        )
        for key, value in (
            ("user.name", "t"),
            ("user.email", "t@t"),
            ("commit.gpgsign", "false"),
        ):
            _g(self.work, "config", key, value)
        (self.work / "docs").mkdir()
        (self.work / "docs" / "x.md").write_text("prev\n", encoding="utf-8")
        _g(self.work, "add", "-A")
        _g(self.work, "commit", "-q", "-m", "prev")
        self.prev = _g(self.work, "rev-parse", "HEAD")
        (self.work / "scripts").mkdir()
        (self.work / "scripts" / "ship").write_text(
            "\n".join(_LINES) + "\n", encoding="utf-8"
        )
        (self.work / "app.py").write_text("\n".join(_LINES) + "\n", encoding="utf-8")
        _g(self.work, "add", "-A")
        _g(self.work, "commit", "-q", "-m", "fix(gate): the land (td42)")
        self.cut = _g(self.work, "rev-parse", "HEAD")
        _g(self.work, "push", "-q", "origin", "HEAD:main")
        _g(self.work, "push", "-q", "origin", f"{self.prev}:refs/heads/prod")
        _g(self.work, "push", "-q", "origin", "HEAD:refs/heads/release/r7")
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.root)], check=True
        )
        for key, value in (
            ("user.name", "factory"),
            ("user.email", "factory@t"),
            ("commit.gpgsign", "false"),
        ):
            _g(self.root, "config", key, value)

    def head(self, ref: str) -> str:
        return _g(self.origin, "rev-parse", f"refs/heads/{ref}")

    def edit(self, ref: str, path: str, line: int, text: str, msg: str) -> str:
        """Commit ``text`` as line ``line`` of ``path`` onto origin's
        ``ref``; the new sha."""
        self._on(ref)
        rows = (self.work / path).read_text(encoding="utf-8").splitlines()
        rows[line - 1] = text
        (self.work / path).write_text("\n".join(rows) + "\n", encoding="utf-8")
        return self._push(ref, msg)

    def add(self, ref: str, path: str, text: str, msg: str) -> str:
        self._on(ref)
        (self.work / path).write_text(text, encoding="utf-8")
        return self._push(ref, msg)

    def run(self, ref: str, *args: str) -> str:
        """Run one git command on ``ref``'s tip and push the result."""
        self._on(ref)
        _g(self.work, *args)
        _g(self.work, "push", "-q", "origin", f"HEAD:refs/heads/{ref}")
        return _g(self.work, "rev-parse", "HEAD")

    def _on(self, ref: str) -> None:
        _g(self.work, "fetch", "-q", "origin")
        _g(self.work, "checkout", "-q", "-B", "w", f"origin/{ref}")

    def _push(self, ref: str, msg: str) -> str:
        _g(self.work, "add", "-A")
        _g(self.work, "commit", "-q", "-m", msg)
        _g(self.work, "push", "-q", "origin", f"HEAD:refs/heads/{ref}")
        return _g(self.work, "rev-parse", "HEAD")

    def subjects(self, rng: str) -> list[str]:
        out = _g(self.origin, "log", "--reverse", "--format=%s", rng)
        return out.splitlines()


#: The pass roots ``release_poll.follow_moved_head`` was called for — the
#: job calls it after a carry; the rig records the call instead of
#: letting it read GitHub.
_follows: list[int] = []


def _followed(store: Any, root_id: int, *, dry_run: bool = False) -> str | None:
    _follows.append(root_id)
    return f"release head: followed td{root_id}"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig(tmp_path)
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("PRECIS_FACTORY_ORIGIN_URL", raising=False)
    monkeypatch.setattr(_fg, "factory_git_token", lambda: None)
    monkeypatch.setattr(rc, "_origin_url", lambda root, env: str(rig.origin))
    monkeypatch.setattr(rs, "slice_root", lambda: rig.root)
    _follows.clear()
    monkeypatch.setattr(rp, "follow_moved_head", _followed)
    psettings.invalidate()
    return rig


def _cut(store: Store, rig: Rig) -> T.PlanMint:
    mint = T.mint_release_pass(store)
    R.record_cut(
        store, mint.ids["cut"], "r7", rig.cut, f"{rig.prev[:12]}..{rig.cut[:12]}"
    )
    return mint


def _node(store: Store, mint: T.PlanMint, rig: Rig, *shas: str) -> int:
    return T.mint_release_carry(
        store, mint.root_id, "r7", [s[:12] for s in shas], rig.head("release/r7")
    )


def _want(monkeypatch: pytest.MonkeyPatch, *shas: str) -> None:
    """What the gate still wants on the branch, for the job's re-check."""
    monkeypatch.setattr(
        rc, "carry_wanted", lambda store, rel: ([s[:12] for s in shas], None)
    )


def _outcome(store: Store, node: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=node)
    assert ref is not None
    return dict(ref.meta[rc.CARRY_KEY].get("outcome") or {})


def _trees(rig: Rig) -> list[str]:
    """The carry worktree directories under the factory checkout."""
    home = rig.root / ".claude" / "worktrees"
    return sorted(p.name for p in home.glob("release-carry-*")) if home.is_dir() else []


def _untouched(rig: Rig, before: str) -> None:
    """The factory checkout's own tree is as it was, and no carry
    worktree is left behind."""
    assert _g(rig.root, "rev-parse", "HEAD") == before
    assert _g(rig.root, "status", "--porcelain") == ""
    assert _trees(rig) == []
    assert len(_g(rig.root, "worktree", "list").splitlines()) == 1


def test_a_clean_carry_pushes_an_exact_copy_the_gate_recognises(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    fix = rig.add("release/r7", "fix.txt", "a\n", "release fix")
    f = rig.edit("main", "scripts/ship", 3, "l3 fixed", "fix(gate): the fix (td43)")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "carried", out
    pushed = rig.head("release/r7")
    assert out["pushed"] == pushed and out["base"] == fix
    assert out["picked"] == {f[:12]: pushed}
    assert _g(rig.origin, "rev-list", "--parents", "-n1", pushed).split() == [
        pushed,
        fix,
    ]
    body = _g(rig.origin, "log", "-1", "--format=%B", pushed)
    assert body.startswith("fix(gate): the fix (td43)")
    assert f"(cherry picked from commit {f})" in body
    assert rig.head("main") == f
    _untouched(rig, before)
    assert _outcome(store, node)["state"] == "carried"
    # The gate's own proof, and its own reading of the branch: the pick
    # counts as main's commit, so the carry hold clears.
    assert rp._replays_as(rig.root, f"{f}^", f"{pushed}^", f, pushed)
    rel = R.release_of(store, mint.root_id)
    assert rel is not None
    picked, _undone, _notes = rp._branch_carries(rel, rp._release_lands(rel))
    assert {k: v.sha for k, v in picked.items()} == {pushed: f}


def test_a_conflicting_pick_pushes_nothing_and_records_why(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    head = rig.edit("release/r7", "app.py", 3, "release", "release fix")
    f = rig.edit("main", "app.py", 3, "main", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "conflict" and out["files"] == ["app.py"]
    assert f"{f[:12]} does not apply to release/r7 at {head[:12]}" in out["why"]
    assert out["base"] == head
    assert rig.head("release/r7") == head
    _untouched(rig, before)
    assert _outcome(store, node)["state"] == "conflict"


def test_two_correctives_are_picked_in_mains_order(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    rig.add("release/r7", "fix.txt", "a\n", "release fix")
    f1 = rig.edit("main", "app.py", 5, "first", "fix one")
    rig.add("main", "later.txt", "x\n", "unrelated main work")
    f2 = rig.edit("main", "app.py", 5, "second", "fix two")  # needs fix one
    node = _node(store, mint, rig, f2, f1)  # minted newest first
    _want(monkeypatch, f2, f1)
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "carried", out
    assert list(out["picked"]) == [f1[:12], f2[:12]]
    assert rig.subjects(f"{rig.cut}..refs/heads/release/r7") == [
        "release fix",
        "fix one",
        "fix two",
    ]
    for src, new in out["picked"].items():
        full = f1 if f1.startswith(src) else f2
        assert rp._replays_as(rig.root, f"{full}^", f"{new}^", full, new)


def test_a_corrective_the_branch_already_holds_is_a_noop(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand pick is on the branch but the gate still asks for the
    commit: the pick comes out empty, nothing is pushed."""
    mint = _cut(store, rig)
    rig.add("release/r7", "fix.txt", "a\n", "release fix")
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    head = rig.run("release/r7", "cherry-pick", f)
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "noop", out
    assert out["skipped"] == {f[:12]: "release/r7 already holds its change"}
    assert rig.head("release/r7") == head
    _untouched(rig, before)


def test_a_corrective_the_release_head_contains_is_a_noop(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    _g(rig.work, "push", "-q", "origin", f"{f}:refs/heads/release/r7")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "noop", out
    assert out["skipped"] == {f[:12]: "release/r7 already contains it"}
    assert rig.head("release/r7") == f


def test_a_hold_the_gate_no_longer_reports_is_not_picked(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the mint and the job the carry ask was answered (the
    release ships without the commit) or the commit reached the branch:
    the job re-reads the gate and picks nothing."""
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch)
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "noop" and "gate no longer wants it" in str(out["skipped"])
    assert rig.head("release/r7") == rig.cut


def test_nothing_is_carried_once_the_deploy_started(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    R.mark_deploy_started(store, mint.ids["deploy"])
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "refused" and "deploy started" in out["why"]
    assert rig.head("release/r7") == rig.cut


def test_a_carry_does_not_push_after_its_pass_closed(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td741722: the deploy-start stamp is not the only end of a carry —
    a pass closed without a deploy (a drop, a cancel) leaves a queued
    carry that must not push. The look under the pass root's row lock
    reads its STATUS and pushes nothing."""
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    store.add_tag(
        mint.root_id,
        Tag.closed("STATUS", "won't-do"),
        set_by="system",
        replace_prefix=True,
    )
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "refused", out
    assert f"the pass td{mint.root_id} is won't-do" in out["why"]
    assert rig.head("release/r7") == rig.cut
    assert _outcome(store, node)["state"] == "refused"
    assert _follows == []
    _untouched(rig, before)


def test_a_deploy_that_starts_during_the_picks_stops_the_push(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deploy stamps its start after the picks were made and proven,
    before the locked look: the look sees it and nothing is pushed."""
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    real = rp._replays_as

    def proof(*args: Any) -> bool:
        ok = real(*args)
        R.mark_deploy_started(store, mint.ids["deploy"])
        return ok

    monkeypatch.setattr(rp, "_replays_as", proof)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "refused" and "takes no more commits" in out["why"]
    assert rig.head("release/r7") == rig.cut
    _untouched(rig, before)


def test_an_answer_that_arrives_during_the_job_stops_the_push(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate wanted the commit when the job started; the carry ask was
    answered while it picked (the release ships without the commit). The
    look under the lock reads the gate again and pushes nothing."""
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    asked: list[int] = []

    def wanted(store: Any, rel: dict[str, Any]) -> tuple[list[str], None]:
        asked.append(1)
        return ([f[:12]] if len(asked) == 1 else []), None

    monkeypatch.setattr(rc, "carry_wanted", wanted)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert len(asked) == 2
    assert out["state"] == "refused", out
    assert f"the gate no longer waits for {f[:12]}" in out["why"]
    assert rig.head("release/r7") == rig.cut
    assert _outcome(store, node)["state"] == "refused"
    _untouched(rig, before)


def test_the_look_and_the_push_hold_the_pass_roots_row_lock(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mark_deploy_started`` takes the pass root's row lock, so while
    the push is in flight no deploy can stamp its start — and the lock
    is gone once the carry returned."""
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    assert store.dsn
    dsn = store.dsn
    seen: list[str] = []

    def root_lock() -> str:
        with psycopg.connect(dsn) as other:
            try:
                row = other.execute(
                    "SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE NOWAIT",
                    (mint.root_id,),
                ).fetchone()
            except psycopg.errors.LockNotAvailable:
                return "held"
            return "free" if row is not None else "no row"

    real = rc._mirror.push_cas

    def push(*args: Any, **kw: Any) -> tuple[str, str]:
        seen.append(root_lock())
        return real(*args, **kw)

    monkeypatch.setattr(rc._mirror, "push_cas", push)
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "carried", out
    assert seen == ["held"]
    assert root_lock() == "free"


def test_nothing_is_carried_with_the_switch_off(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    psettings.set_setting("release.auto_carry", "false", store=store)
    out = rc.carry(store, node, rig.root)
    assert out == {"state": "refused", "why": "release.auto_carry is off"}
    assert rig.head("release/r7") == rig.cut


def test_a_release_head_that_moves_under_the_push_is_picked_again(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fix lands on the branch between the fetch and the push: the
    push is refused as no fast-forward, the pick is redone on the new
    head, and the other commit stays where it was."""
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    moved: list[str] = []

    def wanted(store: Any, rel: dict[str, Any]) -> tuple[list[str], None]:
        if not moved:
            moved.append(
                rig.add("release/r7", "other.txt", "o\n", "someone else's fix")
            )
        return [f[:12]], None

    monkeypatch.setattr(rc, "carry_wanted", wanted)
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "carried", out
    assert out["base"] == moved[0]
    assert rig.subjects(f"{rig.cut}..refs/heads/release/r7") == [
        "someone else's fix",
        "the fix",
    ]


def test_a_release_head_that_keeps_moving_is_left_for_the_next_tick(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    moved: list[str] = []
    calls: list[int] = []

    def wanted(store: Any, rel: dict[str, Any]) -> tuple[list[str], None]:
        # Two gate reads a try (before the picks, under the lock): the
        # head moves on the first of each pair.
        calls.append(1)
        if len(calls) % 2 == 1:
            n = len(moved)
            moved.append(rig.add("release/r7", f"o{n}.txt", "o\n", f"other fix {n}"))
        return [f[:12]], None

    monkeypatch.setattr(rc, "carry_wanted", wanted)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "moved" and len(moved) == 3
    assert rig.head("release/r7") == moved[-1]
    assert "the fix" not in rig.subjects(f"{rig.cut}..refs/heads/release/r7")
    _untouched(rig, before)


def test_origin_refuses_a_push_that_is_no_fast_forward(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real push path against the bare origin: the branch gains a
    commit after the look under the lock, the plain push is rejected,
    and the other commit is all the branch holds."""
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    monkeypatch.setattr(rc, "_PUSH_TRIES", 1)
    calls: list[int] = []
    other: list[str] = []

    def wanted(store: Any, rel: dict[str, Any]) -> tuple[list[str], None]:
        calls.append(1)
        if len(calls) == 2:  # the look under the lock, right before the push
            other.append(rig.add("release/r7", "o.txt", "o\n", "someone else's fix"))
        return [f[:12]], None

    monkeypatch.setattr(rc, "carry_wanted", wanted)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "moved" and out["base"] == rig.cut
    assert rig.head("release/r7") == other[0]
    assert rig.subjects(f"{rig.cut}..refs/heads/release/r7") == ["someone else's fix"]
    _untouched(rig, before)


def test_a_second_entry_while_a_carry_runs_touches_nothing(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two entries into one carry shared a worktree path: the second
    removed the first's tree mid-pick. Now the second does not get the
    release's run lock and returns — no tree touched, no outcome."""
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    first = rig.root / ".claude" / "worktrees" / "release-carry-r7-1-1"
    first.mkdir(parents=True)
    (first / "mid-pick").write_text("x\n", encoding="utf-8")
    assert store.dsn
    with try_xact_advisory_lock(store.dsn, rc._RUN_LOCK_NS, text_key="r7") as held:
        assert held
        out = rc.carry(store, node, rig.root)
        assert out["state"] == "busy", out
        assert _trees(rig) == ["release-carry-r7-1-1"]
        assert (first / "mid-pick").exists()
        assert _outcome(store, node) == {}
        assert rig.head("release/r7") == rig.cut
        ctx = _Ctx(store)
        rc.run(ctx, {"carry": True, "todo_id": node})
        assert ctx.status == "succeeded" and "nothing done" in ctx.chunks[0][1]
        assert _follows == []
    # Another release's lock is not this one's.
    with try_xact_advisory_lock(store.dsn, rc._RUN_LOCK_NS, text_key="r8") as held:
        assert held
        out = rc.carry(store, node, rig.root)
    assert out["state"] == "carried", out


def test_trees_a_dead_run_left_behind_are_removed_by_name(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    home = rig.root / ".claude" / "worktrees"
    dead = home / "release-carry-r7-5-99999"
    _g(rig.root, "worktree", "add", "-q", "--detach", str(dead), rig.cut)
    (dead / "app.py").write_text("half a pick\n", encoding="utf-8")
    (home / "release-carry-r7-6-1").mkdir()  # its gitdir entry already pruned
    (home / "release-carry-r8-1-1").mkdir()  # another release: not ours to reap
    (home / "some-slice").mkdir()
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "carried", out
    assert _trees(rig) == ["release-carry-r8-1-1"]
    assert (home / "some-slice").is_dir()
    assert len(_g(rig.root, "worktree", "list").splitlines()) == 1
    assert _g(rig.root, "rev-parse", "HEAD") == before


def test_a_node_that_already_recorded_its_carry_is_not_carried_twice(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    ref = store.get_ref(kind="todo", id=node)
    assert ref is not None
    earlier = {"state": "carried", "pushed": "c" * 40, "base": rig.cut}
    store.stamp_ref_meta(
        node, {rc.CARRY_KEY: {**ref.meta[rc.CARRY_KEY], "outcome": earlier}}
    )

    def no_network(root: Path, env: dict[str, str]) -> str:
        raise AssertionError("a settled node reads no origin")

    monkeypatch.setattr(rc, "_origin_url", no_network)
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "settled" and "already recorded" in out["why"]
    assert _outcome(store, node) == earlier
    assert rig.head("release/r7") == rig.cut and _trees(rig) == []
    ctx = _Ctx(store)
    rc.run(ctx, {"carry": True, "todo_id": node})
    assert ctx.status == "succeeded" and "nothing done" in ctx.chunks[0][1]
    assert _follows == []


def test_a_revert_is_carried_and_the_gate_drops_both_commits(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REVERT verdict's corrective is the revert commit on main. Picked
    onto the branch it is the exact revert of a commit of this release:
    the gate then ships neither and reviews neither."""
    mint = _cut(store, rig)
    rig.add("release/r7", "fix.txt", "a\n", "release fix")
    revert = rig.run("main", "revert", "--no-edit", rig.cut)
    node = _node(store, mint, rig, revert)
    _want(monkeypatch, revert)
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "carried", out
    pushed = rig.head("release/r7")
    assert out["picked"] == {revert[:12]: pushed}
    assert rp._replays_as(rig.root, f"{revert}^", f"{pushed}^", revert, pushed)
    assert _g(rig.origin, "ls-tree", "--name-only", pushed).splitlines() == [
        "docs",
        "fix.txt",
    ]
    rel = R.release_of(store, mint.root_id)
    assert rel is not None
    _picked, undone, _notes = rp._branch_carries(rel, rp._release_lands(rel))
    assert undone == {pushed, rig.cut}


def test_a_skip_that_fails_after_an_empty_pick_is_refused(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    # A branch commit first: picked straight onto the cut, the fix would
    # come out as the very same commit, not a second one.
    rig.add("release/r7", "fix.txt", "a\n", "release fix")
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    head = rig.run("release/r7", "cherry-pick", f)
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    real = rc._git

    def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["cherry-pick", "--skip"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no skip")
        return real(args, cwd)

    monkeypatch.setattr(rc, "_git", git)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "refused", out
    assert "cherry-pick --skip" in out["why"] and "no skip" in out["why"]
    assert rig.head("release/r7") == head
    _untouched(rig, before)


def test_a_tree_that_is_not_the_head_plus_the_picks_is_not_pushed(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each pick is proven against its own parent, so a tree reset under
    the run could pass with the later pick alone. The history check sees
    that the tree is not the release head plus both picks."""
    mint = _cut(store, rig)
    f1 = rig.add("main", "one.txt", "1\n", "fix one")
    f2 = rig.add("main", "two.txt", "2\n", "fix two")
    node = _node(store, mint, rig, f1, f2)
    _want(monkeypatch, f1, f2)
    real = rp._replays_as
    proven: list[str] = []

    def proof(root: Any, base: str, onto: str, change: str, result: str) -> bool:
        ok = real(root, base, onto, change, result)
        if not proven:  # after the first pick: the tree goes back to the head
            [tree] = (rig.root / ".claude" / "worktrees").glob("release-carry-r7-*")
            _g(tree, "reset", "-q", "--hard", rig.cut)
        proven.append(change)
        return ok

    monkeypatch.setattr(rp, "_replays_as", proof)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert proven == [f1, f2]
    assert out["state"] == "refused", out
    assert "plus exactly the picks" in out["why"]
    assert rig.head("release/r7") == rig.cut
    _untouched(rig, before)


def test_a_merge_commit_is_not_carried(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    _g(rig.work, "fetch", "-q", "origin")
    _g(rig.work, "checkout", "-q", "-B", "side", "origin/main")
    (rig.work / "s.txt").write_text("s\n", encoding="utf-8")
    _g(rig.work, "add", "-A")
    _g(rig.work, "commit", "-q", "-m", "side work")
    _g(rig.work, "checkout", "-q", "-B", "w", "origin/main")
    _g(rig.work, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    merge = _g(rig.work, "rev-parse", "HEAD")
    _g(rig.work, "push", "-q", "origin", "HEAD:refs/heads/main")
    node = _node(store, mint, rig, merge)
    _want(monkeypatch, merge)
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "refused" and "merge" in out["why"]
    assert rig.head("release/r7") == rig.cut


def test_a_corrective_that_is_not_on_main_is_not_carried(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store, rig)
    _g(rig.work, "fetch", "-q", "origin")
    _g(rig.work, "checkout", "-q", "-B", "side", "origin/main")
    (rig.work / "s.txt").write_text("s\n", encoding="utf-8")
    _g(rig.work, "add", "-A")
    _g(rig.work, "commit", "-q", "-m", "never landed")
    stray = _g(rig.work, "rev-parse", "HEAD")
    _g(rig.work, "push", "-q", "origin", "HEAD:refs/heads/side")
    _g(rig.root, "fetch", "-q", "origin", "side")
    node = _node(store, mint, rig, stray)
    _want(monkeypatch, stray)
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "refused" and "not on origin/main" in out["why"]
    assert rig.head("release/r7") == rig.cut


def test_a_pick_that_is_not_an_exact_copy_is_refused(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch renamed the file: git follows the rename and the pick
    applies clean, but to another path than the reviewed commit touched.
    The replay proof says so and nothing is pushed."""
    mint = _cut(store, rig)
    rig._on("release/r7")
    _g(rig.work, "mv", "app.py", "app2.py")
    head = rig._push("release/r7", "rename on the branch")
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    before = _g(rig.root, "rev-parse", "HEAD")
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "refused", out
    assert "not an exact copy" in out["why"]
    assert rig.head("release/r7") == head
    _untouched(rig, before)


def test_a_failed_fetch_fails_and_pushes_nothing(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    monkeypatch.setattr(rc, "_origin_url", lambda root, env: str(tmp_path / "gone.git"))
    out = rc.carry(store, node, rig.root)
    assert out["state"] == "failed" and "did not fetch" in out["why"]
    assert rig.head("release/r7") == rig.cut


class _Ctx:
    """The slice of the job context ``release_carry.run`` uses."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.meta: dict[str, Any] = {}
        self.status = ""
        self.failure = ""
        self.chunks: list[tuple[str, str]] = []

    def set_meta(self, **kw: Any) -> None:
        self.meta.update(kw)

    def set_status(self, status: str) -> None:
        self.status = status

    def record_failure(self, reason: str) -> None:
        self.status, self.failure = "failed", reason

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))


def test_the_job_records_every_outcome_and_never_fails_on_one(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    ctx = _Ctx(store)
    rc.run(ctx, {"carry": True, "todo_id": node})
    assert ctx.status == "succeeded"
    pushed = rig.head("release/r7")
    [(kind, text)] = ctx.chunks
    assert kind == "job_summary"
    assert f"picked {f[:12]} as {pushed[:12]}" in text
    assert ctx.meta["release_carry"]["state"] == "carried"
    # A second node for the same commit: the branch has it, nothing moves.
    again = _node(store, mint, rig, f)
    ctx = _Ctx(store)
    rc.run(ctx, {"carry": True, "todo_id": again})
    assert ctx.status == "succeeded" and "nothing pushed" in ctx.chunks[0][1]
    assert rig.head("release/r7") == pushed
    # A fetch that does not answer is recorded, not a failed job: a failed
    # job would climb the retry cap into an ask that holds the pass open.
    monkeypatch.setattr(rc, "_origin_url", lambda root, env: str(tmp_path / "gone.git"))
    third = _node(store, mint, rig, f)
    ctx = _Ctx(store)
    rc.run(ctx, {"carry": True, "todo_id": third})
    assert ctx.status == "succeeded" and ctx.failure == ""
    assert "did not fetch; the next release_poll tick tries again" in ctx.chunks[0][1]
    assert _outcome(store, third)["state"] == "failed"
    # Only the carry that pushed put the pass on the new head.
    assert _follows == [mint.root_id]


def test_a_carry_puts_the_pass_on_the_pushed_head_at_once(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job calls ``release_poll.follow_moved_head`` after its push, so
    the new head's poll mints now and not at the next ten-minute tick.
    A fault there is a note; the carry stands."""
    mint = _cut(store, rig)
    f = rig.edit("main", "app.py", 3, "fixed", "the fix")
    node = _node(store, mint, rig, f)
    _want(monkeypatch, f)
    ctx = _Ctx(store)
    rc.run(ctx, {"carry": True, "todo_id": node})
    assert _follows == [mint.root_id]
    assert ctx.chunks[0][1].endswith(f"\nrelease head: followed td{mint.root_id}")

    def boom(store: Any, root_id: int, *, dry_run: bool = False) -> str | None:
        raise RuntimeError("GitHub is down")

    monkeypatch.setattr(rp, "follow_moved_head", boom)
    f2 = rig.edit("main", "app.py", 9, "fixed too", "another fix")
    again = _node(store, mint, rig, f2)
    _want(monkeypatch, f2)
    ctx = _Ctx(store)
    rc.run(ctx, {"carry": True, "todo_id": again})
    assert ctx.status == "succeeded" and ctx.failure == ""
    assert ctx.meta["release_carry"]["state"] == "carried"
    assert _outcome(store, again)["state"] == "carried"
    assert "release head follow raised" in ctx.chunks[0][1]


# ── end to end: the tick mints, the job carries, the hold clears ────


def _reviewer(store: Store, from_id: int, review: dict[str, Any]) -> int:
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=f"review td{from_id}",
        meta={
            "generate": {"role": "reviewer", "from": f"td{from_id}"},
            "review": review,
        },
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )
    return int(ref.id)


def test_a_carry_hold_clears_without_anyone_being_asked(
    store: Store, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cut holds engine land td42, reviewed FIX; its fix landed on
    main after the cut. One tick mints the carry, the job picks the fix
    onto release/r7, and the next gate read holds nothing."""
    mint = _cut(store, rig)
    deploy = mint.ids["deploy"]
    fix_todo = int(
        store.insert_ref(kind="todo", slug=None, title="the fix", meta=None).id
    )
    f = rig.edit(
        "main", "scripts/ship", 3, "l3 fixed", f"fix(gate): the fix (td{fix_todo})"
    )
    store.chunks.insert_chunks(
        fix_todo,
        [
            ChunkInsert(
                ord=1, text=f"landed: {f} — fix", meta={"chunk_kind": "todo_comment"}
            )
        ],
    )
    _reviewer(store, 42, {"verdict": "FIX", "task": fix_todo})
    rel = R.release_of(store, deploy)
    assert rel is not None
    [held] = rp.deploy_review_blockers(store, deploy)
    assert rp._CARRY_MARK in held and f[:12] in held

    _v, notes, fatal, _pin, _scanned = rp._engine_verdicts(
        store, rel=rel, now=datetime.now(UTC), file_asks=True
    )
    assert fatal is None, fatal
    [node] = rc.carry_nodes(store, mint.root_id, "r7")
    assert node.shas == (f[:12],) and node.head == rig.cut
    assert any(f"minted td{node.id}" in n for n in notes)

    out = rc.carry(store, node.id, rig.root)
    assert out["state"] == "carried", out
    store.add_tag(
        node.id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )

    assert rp.deploy_review_blockers(store, deploy) == []
    _v, _notes, fatal, _pin, _scanned = rp._engine_verdicts(
        store, rel=rel, now=datetime.now(UTC), file_asks=True
    )
    assert fatal is None, fatal
    assert [n.id for n in rc.carry_nodes(store, mint.root_id, "r7")] == [node.id]
    with store.pool.connection() as conn:
        asks = conn.execute(
            "SELECT count(*) FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE t.namespace = 'OPEN' AND t.value LIKE 'release-review-wait:%%'"
        ).fetchone()
    assert asks is not None and asks[0] == 0


def test_the_carry_node_shows_as_its_own_row() -> None:
    from precis.todo import release_view as rv
    from precis_web import release_passes as web

    meta = {"op": {"type": "release_confirm", "params": {"carry": True, "todo_id": 7}}}
    assert rv._role(meta) == "carry" and "carry" in rv._STATE_WORD
    assert rv._role({"op": {"type": "release_confirm", "params": {}}}) == "confirm"
    # The web strip has no step for it, and it is never the confirm step.
    assert web._role(meta) is None
    assert web._role({"op": "release_confirm"}) == "confirm"
