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


def test_a_corrective_for_a_main_commit_the_branch_repeats_targets_its_branch(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store)
    x, m = "a" * 40, "b" * 40
    monkeypatch.setattr(release_poll, "_open_pass_id", lambda store: mint.root_id)
    monkeypatch.setattr(
        release_poll,
        "_release_lands",
        lambda rel: [
            release_poll._GitLand(
                sha=x,
                subject="",
                committed_at=None,
                paths=("src/precis/plans/release.py",),
                branch_only=True,
            )
        ],
    )
    main_land = release_poll._GitLand(
        sha=m, subject="", committed_at=None, paths=("src/precis/plans/release.py",)
    )
    monkeypatch.setattr(
        release_poll, "_branch_carries", lambda rel, lands: ({x: main_land}, set(), [])
    )
    assert pd._release_scope_target(store, [m[:12]]) == "release/r7"
    # Control: with nothing picked, M is outside the release's scope.
    monkeypatch.setattr(
        release_poll, "_branch_carries", lambda rel, lands: ({}, set(), [])
    )
    assert pd._release_scope_target(store, [m[:12]]) is None


_ENGINE = ("src/precis/plans/release.py",)


def _named(
    monkeypatch: pytest.MonkeyPatch,
    root: int,
    *,
    paths: tuple[str, ...] = _ENGINE,
    branch_only: bool = True,
    undone: bool = False,
) -> None:
    """The release's scope is one commit, ``a…``, whose subject names
    td4242 and which repeats no main commit patch for patch."""
    land = release_poll._GitLand(
        sha="a" * 40,
        subject="fix(factory): salvage finds its stamp (td4242)",
        committed_at=None,
        paths=paths,
        branch_only=branch_only,
    )
    monkeypatch.setattr(release_poll, "_open_pass_id", lambda store: root)
    monkeypatch.setattr(release_poll, "_release_lands", lambda rel: [land])
    monkeypatch.setattr(
        release_poll,
        "_branch_carries",
        lambda rel, lands: ({}, {land.sha} if undone else set(), []),
    )


def test_a_fix_for_a_todo_the_branch_copy_names_targets_its_branch(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr741488: a hand carry with a fix-up folded in repeats no main
    patch, but the gate judges it as the todo its subject names — so that
    todo's FIX holds the deploy and its corrective belongs on the branch."""
    mint = _cut(store)
    _named(monkeypatch, mint.root_id)
    main_sha = "b" * 12
    assert pd._release_scope_target(store, [main_sha], [4242]) == "release/r7"
    assert pd._release_scope_target(store, [], [4242]) == "release/r7"
    # Control: the main sha alone is outside the release, and so is a
    # todo no commit of it names.
    assert pd._release_scope_target(store, [main_sha]) is None
    assert pd._release_scope_target(store, [main_sha], [4243]) is None


@pytest.mark.parametrize(
    "scope",
    [
        {"paths": ("docs/notes.md",)},  # no engine path: not a gate unit
        {"branch_only": False},  # in the cut range: the sha rule decides
        {"undone": True},  # reverted on the branch: the release ships neither
    ],
)
def test_the_todo_link_is_the_gates_unit_only(
    store: Store, monkeypatch: pytest.MonkeyPatch, scope: dict[str, Any]
) -> None:
    mint = _cut(store)
    _named(monkeypatch, mint.root_id, **scope)
    assert pd._release_scope_target(store, ["b" * 12], [4242]) is None


def test_a_started_deploy_keeps_a_named_todos_corrective_on_main(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store)
    _named(monkeypatch, mint.root_id)
    R.mark_deploy_started(store, mint.ids["deploy"])
    assert pd._release_scope_target(store, ["b" * 12], [4242]) is None


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
    calls: list[tuple[list[str], Path]] = []

    def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls.append((args, Path(cwd)))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    synced: list[tuple[Path, str]] = []
    monkeypatch.setattr(run_slice, "_git", fake_git)
    monkeypatch.setattr(run_slice, "slice_root", lambda: tmp_path)
    monkeypatch.setattr(
        run_slice,
        "_sync_base",
        lambda root, base, profile, notes=None: synced.append((root, base)),
    )
    clone = tmp_path / "clone"
    clone.mkdir()
    meta = {"generate": {"role": "reviewer", "from": f"td{target.id}"}}
    text = run_slice._review_target(store, meta, tree=clone, root=tmp_path)
    assert text is not None
    assert "on origin/release/r7, not main" in text
    assert "git fetch -q origin release/r7" in text
    assert "(it is on origin/main)" not in text
    # gr741695: the reviewer has its own clone of the host's hub. The hub
    # follows the release branch first, then the clone fetches it from there.
    assert synced == [(tmp_path, "release/r7")]
    assert (
        [
            "fetch",
            "-q",
            "origin",
            "+refs/heads/release/r7:refs/remotes/origin/release/r7",
        ],
        clone,
    ) in calls


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
    ("log", "exit_code", "cleared"),
    [
        ("backup-window guard refused this deploy — no host was touched.", 1, True),
        ("rollout failed on worker-b", 1, False),
        # gr730609: the exit code alone says no host was touched.
        (
            "install-from-tree: no overlay — this checkout's deploy/inventory is empty",
            75,
            True,
        ),
        (
            "install-from-tree: no overlay — this checkout's deploy/inventory is empty",
            1,
            False,
        ),
        # gr741486: the deploy script's CI wait on a moved head ended red.
        (
            "CI red on release/r7 at beef22222 — failing: gate-fast (3.13, 4)\n"
            "scripts/release: release/r7 has no green head to deploy — "
            "no host was touched",
            2,
            True,
        ),
    ],
    ids=[
        "window-text",
        "rollout-failed",
        "exit-75-no-text",
        "same-text-exit-1",
        "ci-wait-red",
    ],
)
def test_a_deploy_refused_before_any_host_clears_the_deploy_start(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    log: str,
    exit_code: int,
    cleared: bool,
) -> None:
    mint = _cut(store)
    deploy = mint.ids["deploy"]
    R.mark_deploy_started(store, deploy)
    assert R.deploy_started(store, deploy) is not None
    monkeypatch.setattr(
        rd._rc, "finish_run", lambda ctx, handle, name: (exit_code, log)
    )
    monkeypatch.setattr(
        rd._rc, "fail_with_tail", lambda ctx, msg, tail: ctx.failures.append(msg)
    )
    ctx = _Ctx(store, deploy)
    assert rd.poll(ctx, {"pid": 1}) is True
    assert ctx.failures
    assert (R.deploy_started(store, deploy) is None) is cleared


# ── gr697664: a backup-window refusal burns no retry attempt ──────────


@pytest.mark.parametrize(
    ("log", "exit_code", "attempts"),
    [
        (
            "backup-window guard refused this deploy (see above) — no host was touched.",
            1,
            0,
        ),
        (
            "overlay sync refused this deploy (see above) — no host was touched.",
            1,
            1,
        ),
        ("rollout failed on worker-b", 1, 1),
        # gr730609: exit 75 with other wording is not attempt-free.
        (
            "install-from-tree: no overlay — this checkout's deploy/inventory is empty",
            75,
            1,
        ),
    ],
    ids=["window", "overlay-sync", "rollout-failed", "exit-75-other-wording"],
)
def test_a_window_refusal_leaves_the_retry_budget_unchanged(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    log: str,
    exit_code: int,
    attempts: int,
) -> None:
    from precis.workers._mint import mint_background_job

    mint = _cut(store)
    deploy = mint.ids["deploy"]
    with store.tx() as conn:
        job = mint_background_job(
            store,
            conn=conn,
            job_type="release_deploy",
            executor="ssh_node",
            title="release_deploy",
            params={"todo_id": deploy},
            parent_id=deploy,
            status="failed",
        )
    monkeypatch.setattr(
        rd._rc, "finish_run", lambda ctx, handle, name: (exit_code, log)
    )
    monkeypatch.setattr(rd._rc, "fail_with_tail", lambda ctx, msg, tail: None)

    class _JobCtx(_Ctx):
        def set_meta(self, **kw: Any) -> None:
            store.stamp_ref_meta(job.id, kw)

    assert rd.poll(_JobCtx(store, deploy), {"pid": 1}) is True
    plan = pd._load(store)
    assert pd._attempts(plan, plan.nodes[deploy]) == attempts
