"""land_slice job type — the dark-factory lander (td543731).

Pure tests against a fake store and a fake git seam; ship is either a fake
launcher or (one test) a stub script reached through ``PRECIS_SHIP_BIN`` and
the real detached launcher. No DB, no real git, no real scripts/ship.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: the launcher execs /bin/sh"
)

from precis.plans.templates import PENDING_SHA
from precis.store.types import ChunkInsert, Tag
from precis.workers.job_types import get_job_type, known_job_types, land_slice

FULL_SHA = "abc1234" + "0" * 33
SUBJECT = "feat(x): widget (td42)"

# ── fakes ──────────────────────────────────────────────────────────


@dataclass
class _Chunk:
    ord: int
    text: str
    chunk_kind: str


@dataclass
class _Ref:
    id: int
    title: str
    meta: dict[str, Any] = field(default_factory=dict)


class _Chunks:
    def __init__(self) -> None:
        self.rows: dict[int, list[_Chunk]] = {}

    def list_chunks_for_ref(self, ref_id: int) -> list[_Chunk]:
        return sorted(self.rows.get(ref_id, []), key=lambda c: c.ord)

    def insert_chunks(self, ref_id: int, blocks: list[ChunkInsert]) -> None:
        for b in blocks:
            self.rows.setdefault(ref_id, []).append(
                _Chunk(b.ord, b.text, b.meta["chunk_kind"])
            )


class _Store:
    def __init__(self) -> None:
        self.refs: dict[int, _Ref] = {}
        self.tags: dict[int, list[Tag]] = {}
        self.chunks = _Chunks()

    def add_todo(
        self,
        ref_id: int,
        *,
        status: str = "doing",
        tags: tuple[str, ...] = (),
        comments: tuple[str, ...] = (),
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.refs[ref_id] = _Ref(ref_id, f"todo {ref_id}", dict(meta or {}))
        self.tags[ref_id] = [Tag.closed("STATUS", status)] + [Tag.open(t) for t in tags]
        self.chunks.rows[ref_id] = [
            _Chunk(i + 1, c, "todo_comment") for i, c in enumerate(comments)
        ]

    def get_ref(self, *, kind: str, id: int) -> _Ref | None:
        assert kind == "todo"
        return self.refs.get(id)

    def tags_for(self, ref_id: int) -> list[Tag]:
        return list(self.tags.get(ref_id, []))

    def add_tag(
        self,
        ref_id: int,
        tag: Tag,
        *,
        set_by: str = "agent",
        replace_prefix: bool = False,
    ) -> None:
        cur = self.tags.setdefault(ref_id, [])
        if replace_prefix and tag.prefix:
            cur[:] = [t for t in cur if t.prefix != tag.prefix]
        if tag not in cur:
            cur.append(tag)

    def remove_tag(self, ref_id: int, tag: Tag) -> bool:
        cur = self.tags.setdefault(ref_id, [])
        if tag in cur:
            cur.remove(tag)
            return True
        return False

    def stamp_ref_meta(self, ref_id: int, updates: dict[str, Any]) -> None:
        self.refs[ref_id].meta.update(updates)

    def tag_strings(self, ref_id: int) -> set[str]:
        return {
            f"{t.prefix}:{t.value}" if t.prefix else t.value
            for t in self.tags.get(ref_id, [])
        }

    def comments(self, ref_id: int) -> list[str]:
        return [
            c.text
            for c in self.chunks.list_chunks_for_ref(ref_id)
            if c.chunk_kind == "todo_comment"
        ]


class _Ctx:
    def __init__(self, store: _Store, params: dict[str, Any]) -> None:
        self.store = store
        self.ref_id = 901
        self.meta: dict[str, Any] = {"params": params}
        self.status: str | None = None
        self.failure: str | None = None
        self.chunks: list[tuple[str, str]] = []
        self.meta_set: dict[str, Any] = {}

    def set_status(self, value: str) -> None:
        self.status = value

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.meta_set.update(kw)

    def record_failure(self, reason: str, **_kw: Any) -> None:
        self.failure = reason
        self.status = "failed"


def _proc(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    calls: dict[str, Any] = {
        "git": [],
        "launch": [],
        "alive": True,
        "subjects": [SUBJECT],
        "remote": False,
        "gates": [],
    }

    def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls["git"].append((args, Path(cwd)))
        if args[:2] == ["log", "--reverse"]:
            return _proc("".join(f"{s}\n" for s in calls["subjects"]))
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            return _proc(FULL_SHA + "\n")
        if args[:2] == ["ls-remote", "--exit-code"]:
            if calls["remote"]:
                return _proc(f"{FULL_SHA}\trefs/heads/slice/42\n")
            return _proc(rc=2)
        return _proc()

    def fake_launch(argv: list[str], **kw: Any) -> int:
        calls["launch"].append((argv, kw))
        return 5151

    monkeypatch.setattr(land_slice, "_git", fake_git)
    monkeypatch.setattr(land_slice, "_launch", fake_launch)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: calls["alive"])
    monkeypatch.setattr(
        land_slice, "_pending_gates", lambda store, plan, todo: list(calls["gates"])
    )
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("PRECIS_SHIP_BIN", raising=False)
    tree = tmp_path / ".claude" / "worktrees" / "slice-td42"
    (tree / "scratch").mkdir(parents=True)
    calls["tree"] = tree
    return calls


def _params(seams: dict[str, Any]) -> dict[str, Any]:
    return {
        "todo_id": 42,
        "worktree": str(seams["tree"]),
        "branch": "slice/42",
        "lane": "claude-sub",
    }


def _submit(store: _Store, seams: dict[str, Any]) -> tuple[_Ctx, Any]:
    ctx = _Ctx(store, _params(seams))
    return ctx, land_slice.submit(ctx, None)


def _finish(handle: dict[str, Any], exit_code: int, log: str) -> None:
    Path(handle["log"]).write_text(log, encoding="utf-8")
    Path(handle["exit_file"]).write_text(f"{exit_code}\n", encoding="utf-8")


GREEN_LOG = (
    "quick ship (--quick)\n"
    "shipped — landed on main:\n"
    f"abc1234 {SUBJECT}\n"
    "land-notify abc1234: td42 done\n"
)

# ── registry ───────────────────────────────────────────────────────


def test_registered_as_detached_ssh_node_type() -> None:
    spec = get_job_type("land_slice")
    assert spec is not None and spec is land_slice.SPEC
    assert "land_slice" in known_job_types()
    assert spec.compatible_executors == frozenset({"ssh_node"})
    assert spec.submit is land_slice.submit and spec.poll is land_slice.poll
    assert spec.kill is land_slice.kill
    # worktree + branch (a slice) or pr (a pull request, td543749)
    assert spec.params_schema["required"] == ["todo_id"]
    assert {"worktree", "branch", "pr", "checked"} <= set(
        spec.params_schema["properties"]
    )


def test_validate_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    store.add_todo(42)
    store.add_todo(43, status="done")
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    assert land_slice.validate_submit(store, params={"todo_id": 42}) is None
    closed = land_slice.validate_submit(store, params={"todo_id": 43})
    assert closed is not None and closed.startswith("land_slice:")
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    off = land_slice.validate_submit(store, params={"todo_id": 42})
    assert off is not None and "PRECIS_FACTORY=off" in off


# ── subject ────────────────────────────────────────────────────────


def test_squash_subject_picks_the_slice_commit() -> None:
    subjects = ["wip: scratch", "feat(a): the thing (td42)", "fix(a): lint (td42)"]
    assert land_slice.squash_subject(subjects, 42) == "feat(a): the thing (td42)"
    # no commit names the slice: the oldest, with the handle appended
    assert land_slice.squash_subject(["feat(a): b", "fix: c"], 42) == (
        "feat(a): b (td42)"
    )
    assert land_slice.squash_subject([], 42) is None
    assert land_slice.squash_subject(["  ", ""], 42) is None


def test_with_handle_merges_and_caps() -> None:
    assert land_slice.with_handle("fix(x): y (gr7)", 42) == "fix(x): y (gr7, td42)"
    assert land_slice.with_handle("fix(x): y (gr7, td42)", 42) == (
        "fix(x): y (gr7, td42)"
    )
    long = "feat(scope): " + "é" * 80 + " (td42)"
    capped = land_slice.with_handle(long, 42)
    assert len(capped.encode("utf-8")) <= land_slice.SUBJECT_CAP
    assert capped.endswith(" (td42)") and capped.startswith("feat(scope): é")


# ── ship output ────────────────────────────────────────────────────


def test_ship_output_parsers() -> None:
    red = (
        "ruff check\n\x1b[31m✖ ruff: 2 errors in src/x.py\x1b[0m\n"
        "log: /tmp/ship.log\nSHIP_STATUS: error\n"
    )
    assert land_slice.ship_status(red) == "error"
    assert land_slice.cross_line(red) == "✖ ruff: 2 errors in src/x.py"
    assert land_slice.landed_short(red) is None
    assert land_slice.landed_short(GREEN_LOG) == "abc1234"
    assert land_slice.ship_status(GREEN_LOG) is None
    noted = GREEN_LOG + "SHIP_STATUS: shipped — local main ref not moved\n"
    assert land_slice.ship_status(noted) == "shipped"


# ── submit ─────────────────────────────────────────────────────────


def test_submit_launches_ship_with_the_slice_subject(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    tree = seams["tree"]
    assert ctx.failure is None and ctx.status is None
    [(argv, kw)] = seams["launch"]
    assert argv == [str(tree / "scripts" / "ship"), "--quick", SUBJECT]
    assert kw["cwd"] == tree
    assert kw["env"]["PRECIS_SHIP_VERBOSE"] == "1"
    assert handle["subject"] == SUBJECT and handle["pid"] == 5151
    assert handle["log"] == str(tree / "scratch" / "land_slice.log")
    assert ctx.meta_set["land_subject"] == SUBJECT
    assert (["log", "--reverse", "--format=%s", "origin/main..HEAD"], tree) in seams[
        "git"
    ]


def test_submit_honours_the_ship_bin_seam(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SHIP_BIN", "/stub/ship")
    store = _Store()
    store.add_todo(42)
    _submit(store, seams)
    [(argv, _kw)] = seams["launch"]
    assert argv[0] == "/stub/ship"


def test_submit_without_commits_fails_and_reopens(seams: dict[str, Any]) -> None:
    seams["subjects"] = []
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None and "nothing to land" in ctx.failure
    assert seams["launch"] == []
    tags = store.tag_strings(42)
    assert {"STATUS:open", "attempt:1"} <= tags
    assert "claimed-by:lane-claude-sub" not in tags
    assert store.comments(42)[-1].startswith("handoff: land FAILED")


def test_submit_missing_worktree_fails(seams: dict[str, Any], tmp_path: Path) -> None:
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, {**_params(seams), "worktree": str(tmp_path / "gone")})
    assert land_slice.submit(ctx, None) is None
    assert ctx.failure is not None and "missing" in ctx.failure
    assert "attempt:1" in store.tag_strings(42)


def test_submit_refuses_when_factory_off(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == [] and seams["git"] == []


# ── poll ───────────────────────────────────────────────────────────


def _cleanup_cmds(seams: dict[str, Any]) -> list[list[str]]:
    return [
        a
        for a, _cwd in seams["git"]
        if a[:2] in (["worktree", "remove"], ["branch", "-D"], ["push", "origin"])
    ]


def test_poll_green_after_land_notify(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub", "plan:40"))
    store.add_todo(
        44,
        status="open",
        tags=("plan:40",),
        meta={"gate": {"kind": "sha-in-prod", "args": {"sha": PENDING_SHA}}},
    )
    seams["gates"] = [(44, True)]
    seams["remote"] = True
    ctx, handle = _submit(store, seams)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False  # ship still running
    # ship's land_notify already wrote the comment and closed the todo
    store.chunks.rows[42].append(
        _Chunk(9, f"landed: abc123400 — {SUBJECT}", "todo_comment")
    )
    store.add_tag(42, Tag.closed("STATUS", "done"), replace_prefix=True)
    _finish(handle, 0, GREEN_LOG)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert pctx.meta_set["landed_sha"] == FULL_SHA
    assert pctx.meta_set["gate_id"] == 44
    # no second landed comment
    assert [c for c in store.comments(42) if c.startswith("landed:")] == [
        f"landed: abc123400 — {SUBJECT}"
    ]
    # the sha reached the pending gate
    assert store.refs[44].meta["gate"] == {
        "kind": "sha-in-prod",
        "args": {"sha": FULL_SHA},
    }
    assert store.comments(44)[-1] == f"sha: {FULL_SHA} — landed by td42"
    tree = str(seams["tree"])
    assert _cleanup_cmds(seams) == [
        ["worktree", "remove", tree],
        ["branch", "-D", "slice/42"],
        ["push", "origin", "--delete", "slice/42"],
    ]
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "landed abc123400" in summary
    assert "land_notify did it" in summary
    assert "gate td44" in summary


def test_poll_green_writes_the_land_when_land_notify_did_not(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(
        42, tags=("claimed-by:lane-claude-sub",), comments=("handoff: done",)
    )
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert store.comments(42)[-1] == f"landed: abc123400 — {SUBJECT}"
    tags = store.tag_strings(42)
    assert "STATUS:done" in tags and "claimed-by:lane-claude-sub" not in tags
    # no plan tag → no gate; never pushed → no remote delete
    assert pctx.meta_set["gate_id"] is None
    assert ["push", "origin", "--delete", "slice/42"] not in _cleanup_cmds(seams)
    assert "origin branch: not pushed" in pctx.meta_set["cleanup"]


def test_poll_red_lint_fails_with_the_cross_line(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        1,
        "ruff format --check\n\x1b[31m✖ ruff format: 1 file would be reformatted\x1b[0m\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.failure is not None
    assert "✖ ruff format: 1 file would be reformatted" in pctx.failure
    assert "attempt:1" in pctx.failure
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "RED — ✖ ruff format: 1 file would be reformatted" in summary
    tags = store.tag_strings(42)
    assert {"STATUS:open", "attempt:1"} <= tags
    assert "claimed-by:lane-claude-sub" not in tags
    assert store.comments(42)[-1].startswith("handoff: land FAILED on slice/42: ✖")
    # the branch and worktree stay for the fix round
    assert _cleanup_cmds(seams) == []
    assert "landed_sha" not in pctx.meta_set


def test_poll_red_without_a_cross_line_names_the_exit(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, "SHIP_STATUS: conflict\n")
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.failure is not None and "SHIP_STATUS conflict" in pctx.failure


# ── gate pick ──────────────────────────────────────────────────────


def test_pending_gate_prefers_the_blocked_by_gate(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, tags=("plan:40",))
    seams["gates"] = [(50, False), (51, True)]
    assert land_slice.pending_gate(store, 42) == 51
    # No gate blocked-by this todo (a revert task): fill none, even when there
    # is only one pending gate.
    seams["gates"] = [(50, False)]
    assert land_slice.pending_gate(store, 42) is None
    seams["gates"] = [(50, False), (52, False)]
    assert land_slice.pending_gate(store, 42) is None
    store.add_todo(43)
    assert land_slice.pending_gate(store, 43) is None  # in no plan


# ── real launcher + stub ship ──────────────────────────────────────


def test_stub_ship_through_the_real_launcher(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from precis.workers.job_types import run_slice

    stub = tmp_path / "ship-stub"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "args: $*"\n'
        'echo "verbose: $PRECIS_SHIP_VERBOSE"\n'
        "echo 'shipped — landed on main:'\n"
        'echo "abc1234 $2"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PRECIS_SHIP_BIN", str(stub))

    def real_launch(argv: list[str], **kw: Any) -> int:
        return run_slice._launch(argv, stdin=Path("/dev/null"), **kw)

    monkeypatch.setattr(land_slice, "_launch", real_launch)
    monkeypatch.setattr(land_slice, "_process_alive", run_slice._process_alive)
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    pctx = _Ctx(store, _params(seams))
    deadline = time.monotonic() + 20
    while not land_slice.poll(pctx, handle):
        assert time.monotonic() < deadline, "stub ship never exited"
        time.sleep(0.05)
    assert pctx.status == "succeeded", pctx.failure
    log = Path(handle["log"]).read_text(encoding="utf-8")
    assert f"args: --quick {SUBJECT}" in log
    assert "verbose: 1" in log
    assert pctx.meta_set["landed_sha"] == FULL_SHA


# ── pull requests (td543749) ───────────────────────────────────────

PR_HEAD = "c4b3f7068776baa190e7b05bd8758bf19d786c3b"
PR_TITLE = "deps: Update lxml requirement from >=5.0 to >=6.1.3"


@pytest.fixture
def pr_seams(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Any]:
    """``seams`` plus a PR fetch: ``worktree add`` makes the tree,
    ``rev-parse HEAD`` answers ``seams['head']``, ``gh`` is recorded."""
    inner = land_slice._git
    seams["head"] = PR_HEAD
    seams["gh"] = []

    def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["worktree", "add"]:
            seams["git"].append((args, Path(cwd)))
            Path(args[2]).mkdir(parents=True, exist_ok=True)
            return _proc()
        if args == ["rev-parse", "HEAD"]:
            return _proc(seams["head"] + "\n")
        return inner(args, cwd)

    def gh(args: list[str]) -> subprocess.CompletedProcess[str]:
        seams["gh"].append(args)
        return _proc()

    monkeypatch.setattr(land_slice, "_git", git)
    monkeypatch.setattr(land_slice, "_gh", gh)
    seams["pr_tree"] = tmp_path / ".claude" / "worktrees" / "slice-pr-73"
    return seams


def _pr_store() -> _Store:
    store = _Store()
    pr = {
        "number": 73,
        "repo": "acme/widgets",
        "title": PR_TITLE,
        "class": "dependabot",
        "head_sha": PR_HEAD,
    }
    store.add_todo(40, status="open", meta={"auto_close": True, "pr": pr})
    store.add_todo(42, status="open", tags=("plan:40",))
    return store


def test_pr_land_fetches_the_head_and_ships_checked(pr_seams: dict[str, Any]) -> None:
    store = _pr_store()
    ctx = _Ctx(store, {"todo_id": 42, "pr": 73, "checked": True})
    handle = land_slice.submit(ctx, None)
    assert ctx.failure is None, ctx.failure
    tree = pr_seams["pr_tree"]
    git = [a for a, _cwd in pr_seams["git"]]
    assert ["fetch", "origin", "+pull/73/head:refs/heads/slice/pr-73"] in git
    assert ["worktree", "add", str(tree), "slice/pr-73"] in git
    [(argv, kw)] = pr_seams["launch"]
    subject = "build(deps): update lxml requirement from >=5.0 to >=6.1.3 (td42)"
    assert argv == [str(tree / "scripts" / "ship"), "--checked", subject]
    assert len(subject.encode("utf-8")) <= land_slice.SUBJECT_CAP
    assert kw["cwd"] == tree and handle["worktree"] == str(tree)

    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, {"todo_id": 42, "pr": 73, "checked": True})
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded", pctx.failure
    [close] = pr_seams["gh"]
    assert close[:5] == ["pr", "close", "73", "--repo", "acme/widgets"]
    assert close[5] == "--comment" and FULL_SHA[:12] in close[6]
    assert ["branch", "-D", "slice/pr-73"] in _cleanup_cmds(pr_seams)
    assert pctx.meta_set["closed_pr"] == 73


def test_pr_land_refuses_a_moved_head(pr_seams: dict[str, Any]) -> None:
    pr_seams["head"] = "f" * 40
    store = _pr_store()
    ctx = _Ctx(store, {"todo_id": 42, "pr": 73})
    assert land_slice.submit(ctx, None) is None
    assert ctx.failure is not None and "not c4b3f7068" in ctx.failure
    assert pr_seams["launch"] == []


def test_pr_land_needs_the_pinned_worktree(
    pr_seams: dict[str, Any], tmp_path: Path
) -> None:
    store = _pr_store()
    gone = str(tmp_path / "gone")
    ctx = _Ctx(store, {"todo_id": 42, "pr": 73, "worktree": gone})
    assert land_slice.submit(ctx, None) is None
    assert ctx.failure is not None and "dep_check" in ctx.failure
    assert not any(a[:1] == ["fetch"] for a, _cwd in pr_seams["git"])


def test_fix_slice_land_closes_its_pr(pr_seams: dict[str, Any]) -> None:
    store = _pr_store()
    store.refs[42].meta["pr_close"] = 73
    ctx, handle = _submit(store, pr_seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(pr_seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    [close] = pr_seams["gh"]
    assert close[:3] == ["pr", "close", "73"]


def test_plain_slice_land_touches_no_pr(pr_seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, pr_seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(pr_seams))
    assert land_slice.poll(pctx, handle) is True
    assert pr_seams["gh"] == [] and pctx.meta_set["closed_pr"] is None
