"""release-train T3: the merge-back of release/r<N> into main after deploy.

Real git (a bare origin, a checkout) for ``release_merge_back.merge_back``;
the DB store for the plan's ``meta.release`` and the resolver todo.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from precis.plans import gates
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import Tag
from precis.workers.job_types import release_merge_back as mb
from tests._refs import insert_gripe

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX git rig")


def _g(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def rig(tmp_path: Path) -> dict[str, Path]:
    origin, root = tmp_path / "origin.git", tmp_path / "root"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    subprocess.run(["git", "clone", "-q", str(origin), str(root)], check=True)
    _g(root, "config", "user.name", "t")
    _g(root, "config", "user.email", "t@t")
    (root / "app.py").write_text(
        "".join(f"l{i}\n" for i in range(1, 9)), encoding="utf-8"
    )
    _g(root, "add", "-A")
    _g(root, "commit", "-q", "-m", "cut")
    _g(root, "push", "-q", "origin", "HEAD:main")
    _g(root, "push", "-q", "origin", "HEAD:refs/heads/release/r7")
    return {"origin": origin, "root": root}


def _commit(root: Path, ref: str, path: str, text: str, msg: str) -> str:
    _g(root, "fetch", "-q", "origin")
    _g(root, "checkout", "-q", "-B", "w", f"origin/{ref}")
    (root / path).write_text(text, encoding="utf-8")
    _g(root, "add", path)
    _g(root, "commit", "-q", "-m", msg)
    _g(root, "push", "-q", "origin", f"HEAD:refs/heads/{ref}")
    return _g(root, "rev-parse", "HEAD")


def _cut(store: Store, root: Path) -> T.PlanMint:
    mint = T.mint_release_pass(store)
    sha = _g(root, "rev-parse", "origin/main")
    R.record_cut(store, mint.ids["cut"], "r7", sha, f"{'0' * 12}..{sha[:12]}")
    return mint


def _origin(rig: dict[str, Path], ref: str) -> str:
    return _g(rig["origin"], "rev-parse", f"refs/heads/{ref}")


def _note(store: Store, todo_id: int, text: str) -> None:
    from precis.handlers import _comment_timeline as tl

    ref = store.get_ref(kind="todo", id=todo_id)
    assert ref is not None
    tl.append_comment(store, ref, kind="todo", comment_kind="todo_comment", text=text)


def _close(store: Store, todo_id: int) -> None:
    store.add_tag(
        todo_id, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
    )


def test_two_branch_fixes_merge_back_as_one_merge_commit(
    store: Store, rig: dict[str, Path]
) -> None:
    root = rig["root"]
    mint = _cut(store, root)
    _commit(root, "release/r7", "fix1.txt", "a\n", "fix 1")
    head = _commit(root, "release/r7", "fix2.txt", "b\n", "fix 2")
    main_before = _commit(root, "main", "later.txt", "main\n", "main after the cut")
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "merged", out
    main = _origin(rig, "main")
    parents = _g(rig["origin"], "rev-list", "--parents", "-n1", main).split()[1:]
    assert parents == [main_before, head]
    assert _g(rig["origin"], "log", "-1", "--format=%s", main) == mb.merge_message("r7")
    # prod (the release head) is now plain ancestry of main: compare
    # prod...main reads ahead, never diverged, so release_poll mints.
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", head, main],
        cwd=rig["origin"],
        check=True,
    )
    rel = R.release_of(store, mint.root_id)
    assert rel is not None and rel["merge_back"]["state"] == "merged"
    assert _g(rig["origin"], "rev-list", "--count", f"{main_before}..{main}") == "3"


def test_a_release_with_no_fixes_mints_no_merge_commit(
    store: Store, rig: dict[str, Path]
) -> None:
    root = rig["root"]
    mint = _cut(store, root)
    main_before = _commit(root, "main", "later.txt", "main\n", "main after the cut")
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "noop" and "no commit past the cut" in out["why"]
    assert _origin(rig, "main") == main_before


def test_a_conflicting_merge_mints_one_resolver_and_pushes_nothing(
    store: Store, rig: dict[str, Path]
) -> None:
    root = rig["root"]
    mint = _cut(store, root)
    _commit(root, "release/r7", "app.py", "release\n", "release fix")
    main_before = _commit(root, "main", "app.py", "main\n", "main edit")
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "conflict" and out["files"] == ["app.py"]
    assert _origin(rig, "main") == main_before
    rid = out["resolver"]
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.parent_id == mint.root_id
    assert ref.meta["op"] == "run_slice"
    assert ref.meta[mb.RESOLVER_KEY] == "r7"
    assert "land_target" not in ref.meta  # a normal slice onto main
    assert f"plan:{mint.root_id}" in {str(t.value) for t in store.tags_for(rid)}
    ours = _ours_node(store, mint.root_id)
    assert ours["op"]["params"]["resolver"] == rid
    # A retry reuses the one resolver.
    again = mb.merge_back(store, mint.ids["merge_back"], root)
    assert again["resolver"] == rid and _origin(rig, "main") == main_before


def test_a_deleted_release_branch_conflict_briefs_the_resolver_on_the_sha(
    store: Store, rig: dict[str, Path]
) -> None:
    """The gone-ref path can still hit a real conflict: the resolver
    brief must fetch and read the recovered head sha — it cannot fetch
    the retired ``release/r<N>`` ref."""
    root = rig["root"]
    mint = _cut(store, root)
    head = _commit(root, "release/r7", "app.py", "release\n", "release fix")
    R.record_head(store, mint.root_id, head)
    main_before = _commit(root, "main", "app.py", "main\n", "main edit")
    _g(root, "push", "-q", "origin", "--delete", "release/r7")
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "conflict" and out["files"] == ["app.py"]
    assert _origin(rig, "main") == main_before
    body = T._todo_body(store, out["resolver"])
    assert f"git fetch origin {head}" in body
    assert "git fetch origin release/r7" not in body
    assert f"..{head[:12]}" in body
    ours = _ours_node(store, mint.root_id)
    assert f"-s ours {head}" in T._todo_body(store, ours["id"])


def _resolvers(store: Store, root_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE parent_id = %s AND meta ? %s",
            (root_id, mb.RESOLVER_KEY),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _ours_node(store: Store, root_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE parent_id = %s "
            "AND meta->'op'->'params'->>'strategy' = 'ours'",
            (root_id,),
        ).fetchall()
    assert len(rows) == 1
    return {"id": int(rows[0][0]), **dict(rows[0][1])}


def test_the_resolver_dedupe_reads_what_the_mint_wrote(
    store: Store, rig: dict[str, Path]
) -> None:
    """A crash between the mint and the ``meta.release.merge_back`` record
    leaves no record: the retry must still find the minted resolver."""
    root = rig["root"]
    mint = _cut(store, root)
    _commit(root, "release/r7", "app.py", "release\n", "release fix")
    _commit(root, "main", "app.py", "main\n", "main edit")
    first = mb.merge_back(store, mint.ids["merge_back"], root)
    rel = R.release_of(store, mint.root_id)
    assert rel is not None
    rel.pop(mb.MERGE_BACK_KEY)
    store.stamp_ref_meta(mint.root_id, {"release": rel})
    again = mb.merge_back(store, mint.ids["merge_back"], root)
    assert again["resolver"] == first["resolver"]
    assert _resolvers(store, mint.root_id) == [first["resolver"]]


def _prod_vs_main(origin: Path, prod: str) -> tuple[int, int, list[str]]:
    """``(ahead_by, behind_by, files)`` of GitHub's ``compare
    prod...main``, read from git: the counts each side lacks, and the
    files main's tree differs in from the merge base."""
    ahead = int(_g(origin, "rev-list", "--count", f"{prod}..main"))
    behind = int(_g(origin, "rev-list", "--count", f"main..{prod}"))
    files = _g(origin, "diff", "--name-only", f"{prod}...main").split()
    return ahead, behind, files


def test_a_conflict_resolved_on_main_then_an_ours_merge_restores_ancestry(
    store: Store, rig: dict[str, Path]
) -> None:
    root = rig["root"]
    mint = _cut(store, root)
    head = _commit(root, "release/r7", "app.py", "release\n", "release fix")
    _commit(root, "main", "app.py", "main\n", "main edit")
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "conflict"
    # Before the resolver lands the ours-merge refuses: nothing pushed.
    ours = _ours_node(store, mint.root_id)
    before = _origin(rig, "main")
    early = mb.merge_back(
        store, ours["id"], root, strategy=mb.OURS, resolver=out["resolver"]
    )
    assert early["state"] == "failed" and "not done" in early["why"]
    assert _origin(rig, "main") == before
    # The resolver lands like any slice: one squashed, non-merge commit.
    resolved = _commit(root, "main", "app.py", "main+release\n", "resolve r7")
    _note(store, out["resolver"], f"landed: {resolved}")
    _close(store, out["resolver"])
    _, behind, _ = _prod_vs_main(rig["origin"], head)
    assert behind == 1  # the squash alone leaves prod's commit off main
    done = mb.merge_back(
        store, ours["id"], root, strategy=mb.OURS, resolver=out["resolver"]
    )
    assert done["state"] == "merged" and done["strategy"] == mb.OURS
    main = _origin(rig, "main")
    parents = _g(rig["origin"], "rev-list", "--parents", "-n1", main).split()[1:]
    assert parents == [resolved, head]
    assert _g(rig["origin"], "log", "-1", "--format=%s", main) == mb.merge_message(
        "r7", resolved[:12]
    )
    # main's tree is the resolver's: the ours-merge carries no content.
    assert _g(rig["origin"], "rev-parse", f"{main}^{{tree}}") == _g(
        rig["origin"], "rev-parse", f"{resolved}^{{tree}}"
    )
    # release_poll's diverged check: prod (the release head) is an
    # ancestor of main now, so behind_by reads 0.
    ahead, behind, files = _prod_vs_main(rig["origin"], head)
    assert behind == 0 and ahead == 3 and files == ["app.py"]


def test_a_hand_merge_makes_the_record_step_a_noop_whatever_the_resolver(
    store: Store, rig: dict[str, Path]
) -> None:
    """gr735669 (r336): the coordinator merged the release into main by
    hand and closed the resolver won't-do. The record step then refused
    "resolver is won't-do, not done" until its retry cap. With the head
    already on main there is nothing to record: ``noop``, nothing pushed."""
    root = rig["root"]
    mint = _cut(store, root)
    head = _commit(root, "release/r7", "app.py", "release\n", "release fix")
    _commit(root, "main", "app.py", "main\n", "main edit")
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "conflict"
    ours = _ours_node(store, mint.root_id)
    store.add_tag(
        out["resolver"],
        Tag.closed("STATUS", "won't-do"),
        set_by="agent",
        replace_prefix=True,
    )
    # Closed, but the head is not on main yet: still refused, and it says so.
    early = mb.merge_back(
        store, ours["id"], root, strategy=mb.OURS, resolver=out["resolver"]
    )
    assert early["state"] == "failed"
    assert "not done" in early["why"] and "does not contain" in early["why"]
    # The hand merge: a real merge commit that keeps main's side.
    _g(root, "fetch", "-q", "origin")
    _g(root, "checkout", "-q", "-B", "w", "origin/main")
    _g(root, "merge", "-q", "--no-ff", "-s", "ours", "-m", "hand merge", head)
    _g(root, "push", "-q", "origin", "HEAD:refs/heads/main")
    merged = _origin(rig, "main")
    done = mb.merge_back(
        store, ours["id"], root, strategy=mb.OURS, resolver=out["resolver"]
    )
    assert done["state"] == "noop" and "already contains" in done["why"]
    assert _origin(rig, "main") == merged
    rel = R.release_of(store, mint.root_id)
    assert rel is not None and rel["merge_back"]["state"] == "noop"


def test_after_a_merge_back_alone_main_s_tree_equals_prod_s(
    store: Store, rig: dict[str, Path]
) -> None:
    """The compare ``release_poll`` skips on (no file differs) until a
    real land follows the merge-back."""
    root = rig["root"]
    mint = _cut(store, root)
    head = _commit(root, "release/r7", "fix1.txt", "a\n", "fix 1")
    assert mb.merge_back(store, mint.ids["merge_back"], root)["state"] == "merged"
    ahead, behind, files = _prod_vs_main(rig["origin"], head)
    assert (ahead, behind, files) == (1, 0, [])
    _commit(root, "main", "later.txt", "x\n", "a real land")
    ahead, behind, files = _prod_vs_main(rig["origin"], head)
    assert (ahead, behind, files) == (2, 0, ["later.txt"])


def test_a_push_lost_to_a_moving_main_remerges_and_wins(
    store: Store, rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = rig["root"]
    mint = _cut(store, root)
    head = _commit(root, "release/r7", "fix1.txt", "a\n", "fix 1")
    real = mb._git
    raced: list[str] = []
    pushes: list[int] = []

    def git(args: list[str], cwd: Path) -> Any:
        if args[0] == "push":
            pushes.append(1)
            if not raced:
                # Another land wins main between the merge and the push.
                raced.append(_commit(root, "main", "other.txt", "o\n", "racer"))
        return real(args, cwd)

    monkeypatch.setattr(mb, "_git", git)
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "merged" and len(pushes) == 2
    main = _origin(rig, "main")
    parents = _g(rig["origin"], "rev-list", "--parents", "-n1", main).split()[1:]
    assert parents == [raced[0], head]


def test_a_retry_after_an_unrecorded_push_reads_noop(
    store: Store, rig: dict[str, Path]
) -> None:
    root = rig["root"]
    mint = _cut(store, root)
    _commit(root, "release/r7", "fix1.txt", "a\n", "fix 1")
    first = mb.merge_back(store, mint.ids["merge_back"], root)
    assert first["state"] == "merged"
    again = mb.merge_back(store, mint.ids["merge_back"], root)
    assert again["state"] == "noop" and "already contains" in again["why"]
    assert _origin(rig, "main") == first["sha"]
    rel = R.release_of(store, mint.root_id)
    assert rel is not None and rel["merge_back"]["state"] == "noop"


def _failing(real: Any, verb: str) -> Any:
    def git(args: list[str], cwd: Path) -> Any:
        if args[0] == verb:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="refused")
        return real(args, cwd)

    return git


def test_a_failed_fetch_fails_and_pushes_nothing(
    store: Store, rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = rig["root"]
    mint = _cut(store, root)
    _commit(root, "release/r7", "fix1.txt", "a\n", "fix 1")
    before = _origin(rig, "main")
    monkeypatch.setattr(mb, "_git", _failing(mb._git, "fetch"))
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out == {"state": "failed", "why": "git fetch: refused"}
    assert _origin(rig, "main") == before


def test_a_failed_fetch_on_a_gone_ref_still_fails_when_main_wont_fetch(
    store: Store, rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted release branch plus an unfetchable main is the
    tick-retry kind of failure, not a noop."""
    root = rig["root"]
    mint = _cut(store, root)
    _g(root, "push", "-q", "origin", "--delete", "release/r7")
    monkeypatch.setattr(mb, "_git", _failing(mb._git, "fetch"))
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "failed" and "git fetch" in out["why"]


def test_a_deleted_release_branch_whose_head_is_on_main_is_a_noop(
    store: Store, rig: dict[str, Path]
) -> None:
    """gr660285: ``scripts/round`` retires release/r<N> once its head is
    on main; a queued merge_back behind the delete reads noop, not a
    retried fetch failure that parks the pass on a human ask."""
    root = rig["root"]
    mint = _cut(store, root)
    main_before = _commit(root, "main", "later.txt", "main\n", "main after the cut")
    _g(root, "push", "-q", "origin", "--delete", "release/r7")
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "noop" and "gone" in out["why"]
    assert _origin(rig, "main") == main_before
    rel = R.release_of(store, mint.root_id)
    assert rel is not None and rel["merge_back"]["state"] == "noop"


def test_a_deleted_release_branch_with_unmerged_commits_still_merges(
    store: Store, rig: dict[str, Path]
) -> None:
    """The ref is gone but its recorded head is still in the checkout —
    a hand-deleted branch carrying work merges on the sha."""
    root = rig["root"]
    mint = _cut(store, root)
    head = _commit(root, "release/r7", "fix1.txt", "a\n", "fix 1")
    R.record_head(store, mint.root_id, head)
    _commit(root, "main", "later.txt", "main\n", "main after the cut")
    _g(root, "push", "-q", "origin", "--delete", "release/r7")
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "merged" and out["head"] == head
    main = _origin(rig, "main")
    parents = _g(rig["origin"], "rev-list", "--parents", "-n1", main).split()[1:]
    assert parents[1] == head
    assert _g(rig["origin"], "log", "-1", "--format=%s", main) == mb.merge_message("r7")


def test_a_deleted_release_branch_fetches_its_recorded_head_by_sha(
    store: Store, rig: dict[str, Path], tmp_path: Path
) -> None:
    """The head commit exists on the server but not in this checkout —
    fetch it by sha and merge."""
    root, origin = rig["root"], rig["origin"]
    _g(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    mint = _cut(store, root)
    # Order matters: root must fetch main BEFORE the fix commit exists,
    # else its objects land here and the sha fetch is never exercised.
    _commit(root, "main", "later.txt", "main\n", "main after the cut")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _g(other, "config", "user.name", "t")
    _g(other, "config", "user.email", "t@t")
    head = _commit(other, "release/r7", "fix1.txt", "a\n", "fix 1")
    R.record_head(store, mint.root_id, head)
    _g(root, "push", "-q", "origin", "--delete", "release/r7")
    absent = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"{head}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert absent.returncode != 0  # the sha fetch is really exercised
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "merged" and out["head"] == head
    main = _origin(rig, "main")
    parents = _g(origin, "rev-list", "--parents", "-n1", main).split()[1:]
    assert parents[1] == head


def test_a_deleted_release_branch_with_an_unfetchable_head_fails(
    store: Store, rig: dict[str, Path]
) -> None:
    """A gone ref whose recorded head can be neither read nor fetched
    fails for a human — nothing recoverable to merge."""
    root = rig["root"]
    mint = _cut(store, root)
    R.record_head(store, mint.root_id, "f" * 40)
    _commit(root, "main", "later.txt", "main\n", "main after the cut")
    _g(root, "push", "-q", "origin", "--delete", "release/r7")
    before = _origin(rig, "main")
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "failed" and "gone" in out["why"]
    assert _origin(rig, "main") == before


def test_a_push_rejected_three_times_fails(
    store: Store, rig: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = rig["root"]
    mint = _cut(store, root)
    _commit(root, "release/r7", "fix1.txt", "a\n", "fix 1")
    before = _origin(rig, "main")
    calls: list[list[str]] = []
    failing = _failing(mb._git, "push")

    def git(args: list[str], cwd: Path) -> Any:
        calls.append(args)
        return failing(args, cwd)

    monkeypatch.setattr(mb, "_git", git)
    out = mb.merge_back(store, mint.ids["merge_back"], root)
    assert out["state"] == "failed" and "rejected 3x" in out["why"]
    assert sum(1 for a in calls if a[0] == "push") == 3
    assert _origin(rig, "main") == before
    rel = R.release_of(store, mint.root_id)
    assert rel is not None and "merge_back" not in rel


def test_a_release_branch_fix_reads_in_prod_after_the_deploy(store: Store) -> None:
    fix = "f" * 40
    R.record_prod_head(store, "a" * 40, [fix[:12], "a" * 12])
    assert gates.sha_deployed(store, fix).open


def test_the_merge_back_node_follows_the_deploy(store: Store) -> None:
    mint = T.mint_release_pass(store)
    ref = store.get_ref(kind="todo", id=mint.ids["merge_back"])
    assert ref is not None
    meta = ref.meta or {}
    assert meta["op"] == {
        "type": "release_confirm",
        "params": {"merge_back": True, "todo_id": mint.ids["merge_back"]},
    }


def _nightly_gripe(store: Store, test: str) -> int:
    gid = insert_gripe(
        store,
        f"nightly red: {test}",
        meta={"ci": {"sig": f"ci:check.yml:{test}", "wf": "nightly"}},
    ).id
    store.stamp_ref_meta(gid, {"filed_by": {"principal": "worker:ci_watch"}})
    return gid


def test_release_green_merge_carries_only_the_matching_nightly_fix(
    store: Store, rig: dict[str, Path]
) -> None:
    from precis.workers import release_test_fixes as fixes
    from precis.workers.job_types import plan_dispatch as pd

    root = rig["root"]
    mint = _cut(store, root)
    test = "tests/test_app.py::test_app"
    gripe = _nightly_gripe(store, test)
    chain = T.mint_gripe_fix_direct(store, gripe_id=gripe)
    other = _nightly_gripe(store, "tests/test_app.py::test_other")
    other_chain = T.mint_gripe_fix_direct(store, gripe_id=other)
    assert fixes.plan_disposition(store, chain.root_id) == (True, None)

    fixes.note_failures(store, mint.root_id, "r7", [f"{test}[one]"], complete=False)
    assert fixes.plan_disposition(store, other_chain.root_id) == (True, None)
    fixes.note_failures(store, mint.root_id, "r7", [f"{test}[one]"], complete=True)
    assert fixes.plan_disposition(store, other_chain.root_id) == (True, None)
    head = _commit(root, "release/r7", "fix.txt", "fixed\n", "fix the red")
    R.record_head(store, mint.root_id, head)
    R.record_ci_verdict(
        store, head, "", "green", "17", now=datetime.now(UTC) + timedelta(seconds=1)
    )
    assert fixes.plan_disposition(store, chain.root_id) == (True, None)
    assert mb.merge_back(store, mint.ids["merge_back"], root)["state"] == "merged"
    # A retry still carries the release head, even though it has no push to make.
    assert mb.merge_back(store, mint.ids["merge_back"], root)["state"] == "noop"

    state = fixes.ownership(store)
    waiting, carried = fixes.plan_disposition(store, chain.root_id, state)
    assert not waiting and carried is not None and carried.head == head
    assert fixes.plan_disposition(store, other_chain.root_id, state) == (False, None)
    regression = _nightly_gripe(store, test)
    with store.atomic() as conn:
        conn.execute(
            "UPDATE refs SET created_at = %s WHERE ref_id = %s",
            (carried.green_at + timedelta(seconds=1), regression),
        )
    fixes.resolve_carried_gripes(store, state)
    assert any(
        t.prefix == "STATUS" and t.value == "open" for t in store.tags_for(regression)
    )
    assert any(
        t.prefix == "STATUS" and t.value == "auto_verify" for t in store.tags_for(gripe)
    )
    plan = pd._load(store, [chain.root_id, other_chain.root_id])
    remaining = pd._release_test_holds(
        plan, pd._Writer(store, False), pd.PassResult(dry_run=False)
    )
    assert remaining.roots == [other_chain.root_id]
    for tid in chain.ids.values():
        assert any(
            t.prefix == "STATUS" and t.value == "done" for t in store.tags_for(tid)
        )


def test_waived_red_is_released_back_to_main_intake(
    store: Store, rig: dict[str, Path]
) -> None:
    from precis.workers import release_test_fixes as fixes

    root = rig["root"]
    mint = _cut(store, root)
    test = "tests/test_app.py::test_app"
    chain = T.mint_gripe_fix_direct(store, gripe_id=_nightly_gripe(store, test))
    fixes.note_failures(store, mint.root_id, "r7", [test], complete=True)
    other = _nightly_gripe(store, "tests/test_app.py::test_other")
    other_chain = T.mint_gripe_fix_direct(store, gripe_id=other)
    assert fixes.plan_disposition(store, chain.root_id) == (True, None)
    assert fixes.plan_disposition(store, other_chain.root_id) == (False, None)
    head = _commit(root, "release/r7", "waive.txt", "waived\n", "waive the red")
    R.record_head(store, mint.root_id, head)
    R.record_ci_verdict(store, head, "", "green", "17", waived={"run_id": "17"})
    assert mb.merge_back(store, mint.ids["merge_back"], root)["state"] == "merged"
    assert fixes.plan_disposition(store, chain.root_id) == (False, None)
