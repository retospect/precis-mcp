"""release-train T3 review fixes F2-F5: correctives inside the release land
on its branch, reviewers read the right ref, a busy round lock is a wait,
and a deploy that touched no host reopens the release to fixes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.workers.job_types import land_slice, release_poll, run_slice
from precis.workers.job_types import plan_dispatch as pd
from precis.workers.job_types import release_deploy as rd

SHA = "c0ffee" + "1" * 34


def _cut(store: Store) -> T.PlanMint:
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", SHA, f"{'9' * 12}..{SHA[:12]}")
    return mint


def _scope(monkeypatch: pytest.MonkeyPatch, root: int, shas: list[str]) -> None:
    monkeypatch.setattr(release_poll, "_open_pass_id", lambda store: root)
    monkeypatch.setattr(
        release_poll,
        "_release_lands",
        lambda rel: [
            release_poll._GitLand(sha=s, subject="", committed_at=None, paths=())
            for s in shas
        ],
    )


# ── F2: a FIX/REVERT on a land inside the open release ───────────────


def test_a_corrective_for_a_land_inside_the_release_targets_its_branch(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store)
    inside, outside = "a" * 40, "b" * 40
    _scope(monkeypatch, mint.root_id, [inside])
    assert pd._release_scope_target(store, [inside[:12]]) == "release/r7"
    assert pd._release_scope_target(store, [outside[:12]]) is None
    assert pd._release_scope_target(store, []) is None


def test_a_started_deploy_sends_the_corrective_to_main(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store)
    _scope(monkeypatch, mint.root_id, ["a" * 40])
    R.mark_deploy_started(store, mint.ids["deploy"])
    assert pd._release_scope_target(store, ["a" * 12]) is None


def test_an_unreadable_scope_keeps_main(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store)
    monkeypatch.setattr(release_poll, "_open_pass_id", lambda store: mint.root_id)

    def boom(rel: dict[str, Any]) -> list[Any]:
        raise RuntimeError("no such ref")

    monkeypatch.setattr(release_poll, "_release_lands", boom)
    assert pd._release_scope_target(store, ["a" * 12]) is None


# ── F3: the reviewer reads a release-branch squash on its branch ─────


def test_a_reviewer_of_a_release_fix_reads_and_fetches_its_branch(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = store.insert_ref(
        kind="todo",
        slug=None,
        title="the fix",
        meta={"op": "run_slice", "land_target": "release/r7"},
    )
    store.chunks.insert_chunks(
        target.id,
        [
            ChunkInsert(
                ord=1,
                text=f"landed: {'d' * 9} — fix(x): it",
                meta={"chunk_kind": "todo_comment"},
            )
        ],
    )
    calls: list[list[str]] = []

    def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(run_slice, "_git", fake_git)
    monkeypatch.setattr(run_slice, "slice_root", lambda: tmp_path)
    meta = {"generate": {"role": "reviewer", "from": f"td{target.id}"}}
    text = run_slice._review_target(store, meta)
    assert text is not None
    assert "on origin/release/r7, not main" in text
    assert "git fetch -q origin release/r7" in text
    assert "(it is on origin/main)" not in text
    assert [
        "fetch",
        "-q",
        "origin",
        "+refs/heads/release/r7:refs/remotes/origin/release/r7",
    ] in calls


# ── F4: a busy round lock is an attempt-free wait ────────────────────


def test_a_busy_release_round_lock_defers_the_land() -> None:
    line = "✖ release lifecycle busy — nothing pushed."
    assert land_slice.is_host_refusal(line)
    assert land_slice.refusal_side(line) == "host"


# ── F5: a deploy that touched no host reopens the release ────────────


class _Ctx:
    def __init__(self, store: Store, todo_id: int) -> None:
        self.store = store
        self.meta: dict[str, Any] = {"params": {"todo_id": todo_id}}
        self.chunks: list[tuple[str, str]] = []
        self.failures: list[str] = []

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **_kw: Any) -> None:
        pass


@pytest.mark.parametrize(
    ("log", "cleared"),
    [
        ("backup-window guard refused this deploy — no host was touched.", True),
        ("rollout failed on worker-b", False),
    ],
)
def test_a_deploy_refused_before_any_host_clears_the_deploy_start(
    store: Store, monkeypatch: pytest.MonkeyPatch, log: str, cleared: bool
) -> None:
    mint = _cut(store)
    deploy = mint.ids["deploy"]
    R.mark_deploy_started(store, deploy)
    assert R.deploy_started(store, deploy) is not None
    monkeypatch.setattr(rd._rc, "finish_run", lambda ctx, handle, name: (1, log))
    monkeypatch.setattr(
        rd._rc, "fail_with_tail", lambda ctx, msg, tail: ctx.failures.append(msg)
    )
    ctx = _Ctx(store, deploy)
    assert rd.poll(ctx, {"pid": 1}) is True
    assert ctx.failures
    assert (R.deploy_started(store, deploy) is None) is cleared
