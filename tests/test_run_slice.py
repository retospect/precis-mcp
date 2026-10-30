"""run_slice job type + precis.plans.lanes (dark factory step 6, td542094).

Pure tests against a fake store and fake git/launch/process seams: no DB,
no git, no claude is ever spawned.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from precis.plans import lanes
from precis.plans.lanes import ANY_CLASS, Lane, pick_lane
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.workers.job_types import get_job_type, known_job_types, run_slice

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
            assert all(c.ord != b.ord for c in self.rows.get(ref_id, []))
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
        title: str,
        *,
        body: str = "",
        status: str = "open",
        comments: tuple[str, ...] = (),
        tags: tuple[str, ...] = (),
    ) -> None:
        self.refs[ref_id] = _Ref(ref_id, title)
        self.tags[ref_id] = [Tag.closed("STATUS", status)] + [Tag.open(t) for t in tags]
        rows = [_Chunk(0, body, TODO_BODY_KIND)] if body else []
        rows += [_Chunk(i + 1, c, "todo_comment") for i, c in enumerate(comments)]
        self.chunks.rows[ref_id] = rows

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
    def __init__(self, store: _Store, params: dict[str, Any], **meta: Any) -> None:
        self.store = store
        self.ref_id = 900
        self.title = "job"
        self.meta: dict[str, Any] = {"params": params, **meta}
        self.status: str | None = None
        self.failure: str | None = None
        self.chunks: list[tuple[str, str]] = []
        self.meta_set: dict[str, Any] = {}
        self.defer: Any = None

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
    """Fake git + launcher + process probe; records every call."""
    calls: dict[str, Any] = {"git": [], "launch": [], "alive": True, "signals": []}

    def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls["git"].append((args, Path(cwd)))
        if args[:2] == ["worktree", "add"]:
            tree = args[4] if args[2] == "-b" else args[2]
            Path(tree).mkdir(parents=True, exist_ok=True)
        if args == ["status", "--porcelain"]:
            return _proc(calls.get("porcelain", ""))
        if args[:2] == ["log", "--oneline"]:
            return _proc(calls.get("ahead", ""))
        return _proc()

    def fake_launch(argv: list[str], **kw: Any) -> int:
        calls["launch"].append((argv, kw))
        return 4242

    monkeypatch.setattr(run_slice, "_git", fake_git)
    monkeypatch.setattr(run_slice, "_launch", fake_launch)
    monkeypatch.setattr(run_slice, "_process_alive", lambda pid: calls["alive"])
    monkeypatch.setattr(
        run_slice, "_signal_group", lambda pgid, sig: calls["signals"].append(pgid)
    )
    monkeypatch.setattr(lanes, "live_lane_counts", lambda store: calls.get("live", {}))
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    monkeypatch.setenv("PRECIS_RUN_SLICE_MODEL", "claude-test-model")
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    return calls


# ── lanes ──────────────────────────────────────────────────────────


def _lane(name: str, cap: int, classes: set[str], enabled: bool = True) -> Lane:
    return Lane(name, "claude_p", "pool", cap, frozenset(classes), enabled)


def test_seed_table_has_one_enabled_lane() -> None:
    enabled = [ln.name for ln in lanes.LANES if ln.enabled]
    assert enabled == ["claude-sub"]
    sub = lanes.get_lane("claude-sub")
    assert sub is not None
    assert (sub.adapter, sub.credential_pool, sub.cap) == (
        "claude_p",
        "subscription",
        2,
    )
    metered = lanes.get_lane("metered")
    assert metered is not None and metered.adapter == "api" and metered.cap == 1
    assert {ln.name for ln in lanes.LANES} == {
        "claude-sub",
        "codex",
        "kimi",
        "devin",
        "metered",
    }


def test_pick_lane_fit_first() -> None:
    table = (_lane("docs-only", 3, {"docs"}), _lane("coder", 1, {"code"}))
    picked = pick_lane(None, "code", lanes=table, counts={})
    assert picked is not None and picked.name == "coder"
    assert pick_lane(None, "review", lanes=table, counts={}) is None


def test_pick_lane_fewest_live_under_cap() -> None:
    table = (_lane("a", 2, {ANY_CLASS}), _lane("b", 2, {ANY_CLASS}))
    picked = pick_lane(None, "code", lanes=table, counts={"a": 1, "b": 0})
    assert picked is not None and picked.name == "b"
    # tie → table order
    picked = pick_lane(None, "code", lanes=table, counts={})
    assert picked is not None and picked.name == "a"
    # a at cap → b even with more live jobs than a would have
    picked = pick_lane(None, "code", lanes=table, counts={"a": 2, "b": 1})
    assert picked is not None and picked.name == "b"
    assert pick_lane(None, "code", lanes=table, counts={"a": 2, "b": 2}) is None


def test_pick_lane_never_picks_disabled() -> None:
    table = (_lane("off", 9, {ANY_CLASS}, enabled=False), _lane("on", 1, {"code"}))
    picked = pick_lane(None, "code", lanes=table, counts={})
    assert picked is not None and picked.name == "on"
    assert pick_lane(None, "docs", lanes=table, counts={}) is None


def test_pick_lane_reads_live_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lanes, "live_lane_counts", lambda store: {"claude-sub": 2})
    assert pick_lane(object(), "code") is None
    monkeypatch.setattr(lanes, "live_lane_counts", lambda store: {"claude-sub": 1})
    picked = pick_lane(object(), "code")
    assert picked is not None and picked.name == "claude-sub"


# ── registry ───────────────────────────────────────────────────────


def test_registered_as_detached_ssh_node_type() -> None:
    spec = get_job_type("run_slice")
    assert spec is not None
    assert "run_slice" in known_job_types()
    assert spec.compatible_executors == frozenset({"ssh_node"})
    assert spec.submit is run_slice.submit and spec.poll is run_slice.poll
    assert spec.kill is run_slice.kill
    assert spec.validate_submit is run_slice.validate_submit
    assert spec.params_schema["required"] == ["todo_id"]


# ── validate_submit ────────────────────────────────────────────────


def test_validate_submit_refuses_when_factory_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    store = _Store()
    store.add_todo(7, "t")
    err = run_slice.validate_submit(store, params={"todo_id": 7})
    assert err is not None and "PRECIS_FACTORY=off" in err


def test_validate_submit_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    store = _Store()
    store.add_todo(7, "open one")
    store.add_todo(8, "closed one", status="done")
    store.add_todo(9, "dropped", status="won't-do")
    ok = run_slice.validate_submit(store, params={"todo_id": 7})
    assert ok is None
    missing = run_slice.validate_submit(store, params={"todo_id": 99})
    assert missing is not None and "not found" in missing
    for closed in (8, 9):
        err = run_slice.validate_submit(store, params={"todo_id": closed})
        assert err is not None and "closed" in err
    bad = run_slice.validate_submit(store, params={"todo_id": "7"})
    assert bad is not None and "positive integer" in bad
    unknown = run_slice.validate_submit(store, params={"todo_id": 7, "lane": "nope"})
    assert unknown is not None and "unknown lane" in unknown
    disabled = run_slice.validate_submit(store, params={"todo_id": 7, "lane": "kimi"})
    assert disabled is not None and "disabled" in disabled


# ── brief ──────────────────────────────────────────────────────────


def test_brief_render_takes_newest_brief_or_handoff(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(
        42,
        "Build the widget",
        body="Do X, then Y.",
        comments=(
            "brief: first brief",
            "just a remark",
            "handoff: second session stopped at Y",
            "landed? not yet",
        ),
    )
    body, latest = run_slice._brief_parts(store, 42)
    assert body == "Do X, then Y."
    assert latest == "handoff: second session stopped at Y"
    brief = run_slice.render_brief(
        todo_id=42,
        title="Build the widget",
        body=body,
        latest=latest,
        lane="claude-sub",
        model="m",
        effort="high",
    )
    assert brief.startswith("# Slice td42: Build the widget\n")
    assert "Do X, then Y." in brief
    assert "second session stopped at Y" in brief
    assert "first brief" not in brief
    # ends with the land instruction, so the slice closes itself
    tail = brief[brief.index("## When done") :]
    assert "scripts/ship --quick" in tail
    assert "put(kind='todo', id=42, text='landed: <sha>" in tail
    assert "tag(kind='todo', id=42, add=['STATUS:done'])" in tail
    assert brief.index("scripts/ship --quick") > brief.index("Do X, then Y.")


# ── submit / poll ──────────────────────────────────────────────────


def _submit(store: _Store, seams: dict[str, Any], **params: Any) -> tuple[_Ctx, Any]:
    ctx = _Ctx(store, {"todo_id": 42, **params})
    handle = run_slice.submit(ctx, None)
    return ctx, handle


def test_submit_launches_and_claims(seams: dict[str, Any], tmp_path: Path) -> None:
    store = _Store()
    store.add_todo(42, "Build the widget", body="Do X.")
    ctx, handle = _submit(store, seams, effort="medium")

    tree = tmp_path / ".claude" / "worktrees" / "slice-td42"
    assert handle == {
        "pid": 4242,
        "pgid": 4242,
        "worktree": str(tree),
        "log": str(tree / "scratch" / "run_slice.log"),
        "exit_file": str(tree / "scratch" / "run_slice.exit"),
    }
    assert ctx.status is None and ctx.failure is None
    assert ctx.meta_set == {
        "lane": "claude-sub",
        "model": "claude-test-model",
        "effort": "medium",
    }
    assert {"claimed-by:lane-claude-sub", "STATUS:doing"} <= store.tag_strings(42)
    assert "STATUS:open" not in store.tag_strings(42)

    git_cmds = [args for args, _cwd in seams["git"]]
    assert ["fetch", "origin", "main"] in git_cmds
    assert ["worktree", "add", "-b", "slice-td42", str(tree), "origin/main"] in git_cmds
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert brief.startswith("# Slice td42: Build the widget")

    [(argv, kw)] = seams["launch"]
    assert argv[1:] == [
        "-p",
        "--output-format",
        "json",
        "--max-turns",
        str(run_slice._DEFAULT_MAX_TURNS),
        "--model",
        "claude-test-model",
    ]
    assert kw["cwd"] == tree
    assert kw["stdin"] == tree / "scratch" / "brief.md"
    assert kw["env"]["PRECIS_SLICE_ID"] == "td42"


def test_submit_parks_when_every_lane_full(seams: dict[str, Any]) -> None:
    seams["live"] = {"claude-sub": 2}
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42})
    deferred: list[str] = []

    def _defer(wake_at: Any, note: str) -> bool:
        deferred.append(note)
        return True

    ctx.defer = _defer
    assert run_slice.submit(ctx, None) is None
    assert deferred and "no lane free" in deferred[0]
    assert ctx.failure is None
    assert seams["launch"] == []
    assert "STATUS:open" in store.tag_strings(42)


def test_submit_refuses_when_factory_off(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == [] and seams["git"] == []


def _finish(handle: dict[str, Any], exit_code: int | None, log: str) -> None:
    Path(handle["log"]).write_text(log, encoding="utf-8")
    if exit_code is not None:
        Path(handle["exit_file"]).write_text(f"{exit_code}\n", encoding="utf-8")


def _poll_ctx(store: _Store, ctx: _Ctx) -> _Ctx:
    """A fresh per-pass ctx, as ssh_node builds one from the DB meta."""
    return _Ctx(store, ctx.meta["params"], **ctx.meta_set)


def test_poll_running_then_success(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "t", body="b")
    ctx, handle = _submit(store, seams)
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    assert pctx.status is None

    seams["porcelain"] = " M a.py\n?? b.py\n"
    seams["ahead"] = "abc1234 feat(x): y (td42)\n"
    result = {"type": "result", "is_error": False, "result": "Landed abc1234."}
    _finish(handle, 0, "noise\n" + json.dumps(result) + "\n")
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert store.comments(42)[-1] == "handoff: Landed abc1234."
    assert pctx.meta_set["branch_dirty"] == 2
    assert pctx.meta_set["branch_commits"] == ["abc1234 feat(x): y (td42)"]
    assert pctx.meta_set["compute_handle_released"] is True
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "2 uncommitted path(s), 1 commit(s) ahead of origin/main" in summary
    # success keeps the lease; the slice's own land closes the todo
    assert "claimed-by:lane-claude-sub" in store.tag_strings(42)
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))


def test_poll_failure_releases_lease_and_bumps_attempt(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "t", tags=("attempt:1",))
    ctx, handle = _submit(store, seams)
    result = {"type": "result", "is_error": True, "result": "Hit max turns."}
    _finish(handle, 1, "boom line\n" + json.dumps(result) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.failure is not None and "attempt:2" in pctx.failure
    assert "boom line" in pctx.failure  # the log tail
    tags = store.tag_strings(42)
    assert "claimed-by:lane-claude-sub" not in tags
    assert "STATUS:open" in tags and "STATUS:doing" not in tags
    assert "attempt:2" in tags and "attempt:1" not in tags
    assert store.comments(42)[-1] == "handoff: Hit max turns."


def test_poll_dead_process_without_exit_is_failure(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(handle, None, "partial output, no result\n")
    seams["alive"] = False
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    assert store.comments(42) == []  # no result → no handoff comment


def test_exit_zero_with_is_error_fails(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, json.dumps({"is_error": True, "result": "refused"}) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"


def test_kill_sigterms_the_group(seams: dict[str, Any]) -> None:
    run_slice.kill(None, {"pid": 4242, "pgid": 4242})
    assert seams["signals"] == [4242]
    run_slice.kill(None, {"pid": 1})
    assert seams["signals"] == [4242]


def test_parse_result_takes_last_result_object() -> None:
    log = (
        '{"type":"x"}\n{"result":"one"}\ntrailing\n{"result":"two","is_error":false}\n'
    )
    assert run_slice.parse_result(log) == ("two", False)
    assert run_slice.parse_result("not json") == (None, True)
