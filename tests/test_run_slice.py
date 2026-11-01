"""run_slice job type + precis.plans.lanes (dark factory step 6, td542094).

Pure tests against a fake store and fake git/launch/process seams: no DB,
no git, no claude is ever spawned.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs shell script scripts/ship"
)

from precis.plans import lanes
from precis.plans.lanes import ANY_CLASS, Lane, pick_lane
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.workers.job_types import get_job_type, known_job_types, run_slice
from precis.workers.job_types import plan_dispatch as pd

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
    parent_id: int | None = None


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
        meta: dict[str, Any] | None = None,
        parent_id: int | None = None,
    ) -> None:
        self.refs[ref_id] = _Ref(ref_id, title, dict(meta or {}), parent_id)
        self.tags[ref_id] = [Tag.closed("STATUS", status)] + [Tag.open(t) for t in tags]
        rows = [_Chunk(0, body, TODO_BODY_KIND)] if body else []
        rows += [_Chunk(i + 1, c, "todo_comment") for i, c in enumerate(comments)]
        self.chunks.rows[ref_id] = rows

    def get_ref(self, *, kind: str, id: int) -> _Ref | None:
        assert kind == "todo"
        return self.refs.get(id)

    def recent_refs(
        self, kinds: list[str], *, parent_id: int | None = None, **_: Any
    ) -> list[_Ref]:
        assert kinds == ["todo"]
        return [r for r in self.refs.values() if r.parent_id == parent_id]

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
    calls: dict[str, Any] = {
        "git": [],
        "launch": [],
        "alive": True,
        "signals": [],
        "identity": None,
        "ps_identity": "Sat Oct 31 19:57:00 2026 slice-user",
        "process_alive": run_slice._process_alive,
    }

    def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls["git"].append((args, Path(cwd)))
        if args[:2] == ["worktree", "add"]:
            tree = args[4] if args[2] == "-b" else args[2]
            Path(tree).mkdir(parents=True, exist_ok=True)
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            branches = calls.get("branches")
            if branches is not None:
                return _proc(rc=0 if args[3] in branches else 1)
        if args == ["status", "--porcelain"]:
            return _proc(calls.get("porcelain", ""))
        if args[:2] == ["status", "--porcelain"] and "-z" in args:
            return _proc(calls.get("porcelain_z", ""), rc=calls.get("status_rc", 0))
        if args[:2] == ["diff", "--name-only"]:
            return _proc(calls.get("diff", ""))
        if args[:2] == ["log", "--oneline"]:
            return _proc(calls.get("ahead", ""))
        return _proc()

    def fake_launch(argv: list[str], **kw: Any) -> int:
        calls["launch"].append((argv, kw))
        return 4242

    calls["lands"] = []

    def fake_mint_land(ctx: Any, todo_id: int, tree: Path, lane: Any) -> int:
        calls["lands"].append((todo_id, Path(tree), lane))
        return 777

    monkeypatch.setattr(run_slice, "_mint_land", fake_mint_land)
    monkeypatch.setattr(run_slice, "_git", fake_git)
    monkeypatch.setattr(run_slice, "_launch", fake_launch)
    monkeypatch.setattr(run_slice, "_process_alive", lambda pid, **kw: calls["alive"])
    monkeypatch.setattr(run_slice, "_process_identity", lambda pid: calls["identity"])
    monkeypatch.setattr(
        run_slice, "_darwin_process_identity", lambda pid: calls["ps_identity"]
    )
    monkeypatch.setattr(
        run_slice, "_signal_group", lambda pgid, sig: calls["signals"].append(pgid)
    )
    monkeypatch.setattr(lanes, "live_lane_counts", lambda store: calls.get("live", {}))
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path / "ops"))
    monkeypatch.setenv("PRECIS_RUN_SLICE_MODEL", "claude-test-model")
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    # No slice-token registry on this rig (the dir never exists) → the
    # shared-token path stays the default under test (gr543694).
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(tmp_path / "no-slice-tokens"))
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
    # ends with commit-and-hand-off: the lander lands, not the session
    tail = brief[brief.index("## When done") :]
    assert "Commit on `slice/42`" in tail
    assert "(td42)`, at most 72 bytes" in tail
    assert "git commit -F scratch/commit-msg.txt" in tail
    assert "Leave the working tree clean" in tail
    assert brief.index("## When done") > brief.index("Do X, then Y.")
    # (the deny list names scripts/ship as a path; the steps never do)
    assert "ship" not in tail
    assert "add=['STATUS:done']" not in brief
    assert "put(kind='todo', id=42" not in brief


def test_generator_brief_creates_children_and_never_lands(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(
        42,
        "Slice the work",
        body="Split it.",
        meta={"generate": {"role": "slicer", "from": "body"}},
    )
    _submit(store, seams)
    tree = tmp_path / ".claude" / "worktrees" / "slice-td42"
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "Split it." in brief
    assert "put(kind='todo', text=..., body=..., meta={'llm_tier': None" in brief
    assert "args={'parent_id': 42}" in brief
    assert "blocked-by" in brief
    assert "Do not tag this todo done: the dispatcher closes it" in brief
    # no land steps: no ship, no landed comment, no STATUS:done instruction
    assert "scripts/ship --quick" not in brief
    assert "landed:" not in brief
    assert "add=['STATUS:done']" not in brief


def test_reviewer_brief_posts_one_verdict_and_mints_nothing(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(
        42,
        "Review the fix after land",
        body="Review td7.",
        meta={"generate": {"role": "reviewer", "from": "td7"}},
    )
    _submit(store, seams)
    tree = tmp_path / ".claude" / "worktrees" / "slice-td42"
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    tail = brief[brief.index("## When done") :]
    assert tail.startswith("## When done: post your verdict")
    for line in ("`review: LAND`", "`review: FIX — <list", "`review: REVERT — <why>`"):
        assert line in tail
    assert "Post exactly one comment on td42" in tail
    assert "Mint nothing" in tail
    assert "Commit nothing" in tail
    assert "Do not land" in tail
    # not the slicer's closing
    assert "create the child todos" not in brief
    assert "args={'parent_id': 42}" not in brief
    assert "scripts/ship --quick" not in brief
    assert "add=['STATUS:done']" not in brief
    # the grammar it names is the one the tick parses
    assert pd.parse_verdict("review: FIX — a.py misses the empty case") is not None
    assert pd.parse_verdict("review: REVERT — breaks the API") is not None
    assert pd.parse_verdict("review: LAND") == ("LAND", "")


def test_analyst_brief_posts_one_analysis_and_mints_nothing(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(
        42,
        "Analyse PR #9 (read-only)",
        body="Read PR 9.",
        meta={"generate": {"role": "analyst", "from": "pr:9"}},
    )
    _submit(store, seams)
    tree = tmp_path / ".claude" / "worktrees" / "slice-td42"
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    tail = brief[brief.index("## When done") :]
    assert tail.startswith("## When done: post your analysis")
    assert "Post exactly one comment on td42" in tail
    assert "starting with `analysis:`" in tail
    for part in ("(a) threats", "(b) fit", "(c) size"):
        assert part in tail
    assert "Mint nothing" in tail
    assert "Commit nothing" in tail
    assert "Never run `uv sync`, tests or any file from the PR" in tail
    assert "Do not land" in tail
    # neither the slicer's nor the reviewer's closing
    assert "create the child todos" not in brief
    assert "args={'parent_id': 42}" not in brief
    assert "review: LAND" not in brief
    assert "scripts/ship --quick" not in brief
    assert "add=['STATUS:done']" not in brief


def test_role_close_table_is_the_only_dispatch() -> None:
    assert set(run_slice._ROLE_CLOSE) == {"reviewer", "analyst"}
    # a role outside the table keeps the generic child-todo closing
    generic = "\n".join(run_slice._generator_close(5))
    assert "create the child todos" in generic
    for role in run_slice._ROLE_CLOSE:
        brief = run_slice.render_brief(
            todo_id=5,
            title="t",
            body="b",
            latest=None,
            lane="l",
            model="m",
            effort="e",
            generator=True,
            role=role,
        )
        assert "create the child todos" not in brief, role
    other = run_slice.render_brief(
        todo_id=5,
        title="t",
        body="b",
        latest=None,
        lane="l",
        model="m",
        effort="e",
        generator=True,
        role="slicer",
    )
    assert "create the child todos" in other


def test_task_brief_commits_and_never_ships(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(42, "Do it", body="Do X.", meta={"op": "run_slice"})
    _submit(store, seams)
    tree = tmp_path / ".claude" / "worktrees" / "slice-td42"
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    tail = brief[brief.index("## When done: commit on the branch and hand off") :]
    assert "ship" not in tail
    assert "add=['STATUS:done']" not in brief
    assert "Do not land, push" in brief
    assert "create the child todos" not in brief


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
    started_at = handle.pop("started_at")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", started_at)
    assert handle == {
        "pid": 4242,
        "pgid": 4242,
        "worktree": str(tree),
        # Outside the tree: a reaped worktree must not take the exit record
        # and log with it (jo544355).
        "log": str(run_slice.run_dir(42) / "run_slice.log"),
        "exit_file": str(run_slice.run_dir(42) / "run_slice.exit"),
    }
    assert not Path(handle["log"]).is_relative_to(tree)
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
    assert ["worktree", "add", "-b", "slice/42", str(tree), "origin/main"] in git_cmds
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert brief.startswith("# Slice td42: Build the widget")
    assert "on branch `slice/42`" in brief
    assert "## Paths you may not write" in brief
    assert "- `scripts/lib/**`" in brief

    [(argv, kw)] = seams["launch"]
    assert argv[1:-1] == [
        "-p",
        "--output-format",
        "json",
        "--max-turns",
        str(run_slice._DEFAULT_MAX_TURNS),
        "--model",
        "claude-test-model",
        "--permission-mode",
        "bypassPermissions",
        "--settings",
    ]
    deny = json.loads(argv[-1])["permissions"]["deny"]
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert f"{tool}(./scripts/ship)" in deny
        assert f"{tool}(./src/precis/plans/**)" in deny
        assert f"{tool}(/{tree.as_posix()}/.github/**)" in deny
    assert len(deny) == 2 * 4 * len(lanes.FACTORY_DENY_WRITE_PATHS)
    # The service account's clone is never trusted interactively; without
    # this the checkout's .mcp.json precis server is skipped in -p mode.
    assert json.loads(argv[-1])["enableAllProjectMcpServers"] is True
    assert kw["cwd"] == tree
    assert kw["stdin"] == tree / "scratch" / "brief.md"
    assert kw["env"]["PRECIS_SLICE_ID"] == "td42"


def test_submit_env_is_an_allowlist(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_DB_DSN", "dbname=precis user=agent_rw")
    monkeypatch.setenv("DATABASE_URL", "postgresql:///precis")
    monkeypatch.setenv("PGPASSWORD", "pw")
    monkeypatch.setenv("VAULT_TOKEN", "v")
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "mcp-token")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    store = _Store()
    store.add_todo(42, "t")
    _submit(store, seams, effort="low")
    [(_argv, kw)] = seams["launch"]
    env = kw["env"]
    for dropped in ("PRECIS_DB_DSN", "DATABASE_URL", "PGPASSWORD", "VAULT_TOKEN"):
        assert dropped not in env
    assert env["PRECIS_MCP_TOKEN"] == "mcp-token"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["PRECIS_SLICE_ID"] == "td42"
    assert env["PRECIS_SLICE_EFFORT"] == "low"
    # The checkout's SessionEnd reaper must leave the slice tree alone.
    assert env["PRECIS_NO_AUTOREAP"] == "1"
    # uv's cache lives under the worker-owned ops dir, not the service
    # account's home (jo546987: root-owned ~/.cache/uv killed every hook).
    assert env["UV_CACHE_DIR"] == str(run_slice.ops_root() / "uv-cache")
    assert Path(env["UV_CACHE_DIR"]).is_dir()
    assert set(env) <= set(run_slice.ENV_ALLOWLIST) | {
        "PRECIS_SLICE_ID",
        "PRECIS_SLICE_EFFORT",
        "PRECIS_NO_AUTOREAP",
        "UV_CACHE_DIR",
    }


def test_submit_env_loads_oauth_token_from_the_vault(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """jo544333: the launchd ``deploy`` worker exports no subscription login,
    so the session must get ``CLAUDE_CODE_OAUTH_TOKEN`` from the vault (the
    only store, ``utils/claude_oauth``); the API key never passes."""
    from precis import secrets as _secrets

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-billed")
    monkeypatch.setattr(
        _secrets,
        "get_secret",
        lambda name, *a, **k: (
            "oauth-from-vault" if name == "CLAUDE_CODE_OAUTH_TOKEN" else None
        ),
    )
    store = _Store()
    store.add_todo(42, "t")
    _submit(store, seams)
    [(_argv, kw)] = seams["launch"]
    assert kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-from-vault"
    assert "ANTHROPIC_API_KEY" not in kw["env"]


def test_submit_env_oauth_token_from_env_wins(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis import secrets as _secrets

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-from-plist")
    monkeypatch.setattr(_secrets, "get_secret", lambda *a, **k: "oauth-from-vault")
    store = _Store()
    store.add_todo(42, "t")
    _submit(store, seams)
    [(_argv, kw)] = seams["launch"]
    assert kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-from-plist"


def test_submit_handle_carries_process_identity(seams: dict[str, Any]) -> None:
    seams["identity"] = ("b00t", 12345)
    store = _Store()
    store.add_todo(42, "t")
    _ctx, handle = _submit(store, seams)
    assert handle["boot_id"] == "b00t" and handle["start_time"] == 12345
    assert handle["pgid"] == handle["pid"] == 4242


def test_submit_handle_carries_darwin_identity(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_slice.sys, "platform", "darwin")
    store = _Store()
    store.add_todo(42, "t")
    _ctx, handle = _submit(store, seams)
    assert handle["ps_identity"] == seams["ps_identity"]
    assert "boot_id" not in handle


@pytest.mark.parametrize("output,rc", [("", 1), ("", 0), ("start user\n", 0)])
def test_darwin_process_identity_uses_ps(
    monkeypatch: pytest.MonkeyPatch, output: str, rc: int
) -> None:
    def ps(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["ps", "-o", "lstart=,user=", "-p", "4242"]
        assert kw["env"]["LC_ALL"] == "C"
        assert kw["timeout"] == 5
        return _proc(output, rc)

    monkeypatch.setattr(run_slice.subprocess, "run", ps)
    assert run_slice._darwin_process_identity(4242) == (
        "start user" if output else None
    )


@pytest.mark.parametrize(
    "error", [OSError("no ps"), subprocess.TimeoutExpired("ps", 5)]
)
def test_darwin_process_identity_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def ps(*args: Any, **kw: Any) -> Any:
        raise error

    monkeypatch.setattr(run_slice.subprocess, "run", ps)
    assert run_slice._darwin_process_identity(4242) is None


def test_submit_renames_legacy_branch(seams: dict[str, Any]) -> None:
    seams["branches"] = {"refs/heads/slice-td42"}
    store = _Store()
    store.add_todo(42, "t")
    _ctx, handle = _submit(store, seams)
    assert handle is not None
    git_cmds = [args for args, _cwd in seams["git"]]
    assert ["branch", "-m", "slice-td42", "slice/42"] in git_cmds
    # already on the new name → no rename
    seams["git"].clear()
    seams["branches"] = {"refs/heads/slice/42", "refs/heads/slice-td42"}
    _submit(store, seams)
    assert any(a[:2] == ["rev-parse", "--verify"] for a, _ in seams["git"])
    assert not any(a[:2] == ["branch", "-m"] for a, _ in seams["git"])


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


# ── per-slice MCP token (gr543694) ─────────────────────────────────


def test_submit_mints_a_per_slice_token_and_poll_revokes_it(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the registry dir present the session's ``PRECIS_MCP_TOKEN`` is
    a ``td<id>.<hex>`` grant the server attributes to this slice — never
    the shared token — and poll deletes the file (the revoke)."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "shared-token")
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)

    [(_argv, kw)] = seams["launch"]
    token = kw["env"]["PRECIS_MCP_TOKEN"]
    assert token != "shared-token"
    # The grant names the JOB (ctx.ref_id 900): two sessions for one todo
    # never overwrite each other's file.
    assert re.fullmatch(r"td42j900\.[0-9a-f]{64}", token)
    grant_file = registry / "td42j900"
    # the file carries the digest, never the token (a 0644 file, not a credential)
    content = grant_file.read_text(encoding="utf-8")
    assert content.split()[0] == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert token not in content
    assert handle["slice_token_dir"] == str(registry)
    assert handle["slice_token_name"] == "td42j900"
    assert "mcp token: slice td42j900" in ctx.chunks[-1][1]

    _finish(handle, 0, json.dumps({"is_error": False, "result": "done"}) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert not grant_file.exists()


def test_launch_failure_revokes_the_slice_token(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))

    def boom(*_a: Any, **_kw: Any) -> int:
        raise RuntimeError("no claude")

    monkeypatch.setattr(run_slice, "_launch", boom)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert not (registry / "td42j900").exists()


class _Resp:
    status = 200

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None


def _write_mcp_json(tmp_path: Path) -> Path:
    tree = tmp_path / ".claude" / "worktrees" / "slice-td42"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "precis": {
                        "url": "http://127.0.0.1:8765/mcp",
                        "bearerTokenEnvVar": "PRECIS_MCP_TOKEN",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return tree


def test_submit_preflights_the_mcp_with_the_session_bearer(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the launch one ``initialize`` goes to the checkout's precis
    URL carrying the bearer the session will use; a 2xx lets the launch
    proceed."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    _write_mcp_json(tmp_path)
    seen: list[Any] = []

    def fake_open(req: Any, timeout: float) -> _Resp:
        seen.append(req)
        return _Resp()

    monkeypatch.setattr(run_slice, "_mcp_preflight_open", fake_open)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    assert handle is not None
    [(_argv, kw)] = seams["launch"]
    [req] = seen
    assert req.full_url == "http://127.0.0.1:8765/mcp"
    assert req.get_header("Authorization") == f"Bearer {kw['env']['PRECIS_MCP_TOKEN']}"
    assert b'"initialize"' in req.data


def test_submit_fails_fast_when_the_mcp_rejects_the_bearer(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 at preflight is the jo547598 shape: the attempt fails in
    seconds (no session launched), the grant is revoked and the todo goes
    back to open — never a 40-minute session ending in a handoff."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    _write_mcp_json(tmp_path)

    def reject(req: Any, timeout: float) -> _Resp:
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(run_slice, "_mcp_preflight_open", reject)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert "preflight: HTTP 401" in (ctx.failure or "")
    assert seams["launch"] == []
    assert not (registry / "td42j900").exists()
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags and not any(t.startswith("claimed-by:") for t in tags)


def test_submit_skips_the_preflight_without_a_precis_server(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``.mcp.json`` precis entry: nothing to check, the launch proceeds."""

    def boom(req: Any, timeout: float) -> _Resp:
        raise AssertionError("preflight must not run")

    monkeypatch.setattr(run_slice, "_mcp_preflight_open", boom)
    store = _Store()
    store.add_todo(42, "t")
    _ctx, handle = _submit(store, seams)
    assert handle is not None


def test_resubmit_mints_a_job_qualified_grant_not_an_overwrite(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two jobs can run sessions for one todo at once: the grant file
    names the job, so the resubmit's mint does not clobber the live
    grant — and the older job's revoke does not delete the new one."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    store = _Store()
    store.add_todo(42, "t")
    ctx1, handle1 = _submit(store, seams)
    ctx2 = _Ctx(store, {"todo_id": 42})
    ctx2.ref_id = 901
    handle2 = run_slice.submit(ctx2, None)
    first, second = registry / "td42j900", registry / "td42j901"
    assert first.exists() and second.exists()
    assert handle2["slice_token_name"] == "td42j901"

    # the older job finishing revokes only its own grant
    _finish(handle1, 0, json.dumps({"is_error": False, "result": "done"}))
    assert run_slice.poll(_poll_ctx(store, ctx1), handle1) is True
    assert not first.exists() and second.exists()


def test_kill_revokes_the_slice_grant(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep kill ends the job without another poll — kill() itself
    deletes the grant file (and still signals the process group)."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    grant_file = registry / "td42j900"
    assert grant_file.exists()
    run_slice.kill(ctx, handle)
    assert seams["signals"] == [4242]
    assert not grant_file.exists()


def test_release_revokes_grant_and_releases_todo_without_a_signal(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    pctx = _poll_ctx(store, ctx)

    run_slice.release(pctx, handle)
    run_slice.release(pctx, handle)

    assert not (registry / "td42j900").exists()
    assert seams["signals"] == []
    assert store.tag_strings(42) == {"STATUS:open", "attempt:1"}


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
    # the clean exit hands the branch to the lander
    assert seams["lands"] == [(42, Path(handle["worktree"]), "claude-sub")]
    assert pctx.meta_set["land_job"] == 777
    assert "Land: land_slice job:777 minted." in summary
    # success keeps the lease; the land closes the todo
    assert "claimed-by:lane-claude-sub" in store.tag_strings(42)
    assert "STATUS:doing" in store.tag_strings(42)
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))


@pytest.mark.parametrize(
    "current,denied,alive",
    [
        ("Sat Oct 31 20:00:00 2026 slice-user", False, False),
        ("Sat Oct 31 19:57:00 2026 other-user", True, False),
        (None, True, False),
        ("Sat Oct 31 19:57:00 2026 slice-user", False, True),
        ("Sat Oct 31 19:57:00 2026 slice-user", True, True),
    ],
)
def test_poll_darwin_requires_matching_pid_identity(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    current: str | None,
    denied: bool,
    alive: bool,
) -> None:
    """gr546830: a reused PID must not keep a dead session's todo claimed."""
    monkeypatch.setattr(run_slice.sys, "platform", "darwin")
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ps_identity"] = current

    def probe(pid: int, sig: int) -> None:
        assert (pid, sig) == (4242, 0)
        if denied:
            raise PermissionError("not ours")

    monkeypatch.setattr(run_slice.os, "kill", probe)
    # Exercise the real liveness probe, not the fixture's stub.
    monkeypatch.setattr(run_slice, "_process_alive", seams["process_alive"])
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is not alive
    if alive:
        assert pctx.status is None
        assert "STATUS:doing" in store.tag_strings(42)
    else:
        assert pctx.status == "failed"
        assert "STATUS:open" in store.tag_strings(42)
        assert "claimed-by:lane-claude-sub" not in store.tag_strings(42)


@pytest.mark.parametrize(
    "platform,alive", [("darwin", False), ("linux", True), ("freebsd14", True)]
)
def test_process_alive_permission_error_is_platform_specific(
    monkeypatch: pytest.MonkeyPatch, platform: str, alive: bool
) -> None:
    def denied(pid: int, sig: int) -> None:
        raise PermissionError("not ours")

    monkeypatch.setattr(run_slice.os, "kill", denied)
    monkeypatch.setattr(run_slice.sys, "platform", platform)
    assert run_slice._process_alive(4242) is alive


def test_poll_linux_permission_error_keeps_session_running(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_slice.sys, "platform", "linux")
    seams["identity"] = ("boot", 12345)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    assert "ps_identity" not in handle

    def denied(pid: int, sig: int) -> None:
        raise PermissionError("pid probe denied")

    monkeypatch.setattr(run_slice.os, "kill", denied)
    monkeypatch.setattr(run_slice, "_process_alive", seams["process_alive"])
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    assert pctx.status is None
    assert store.tag_strings(42) == {"STATUS:doing", "claimed-by:lane-claude-sub"}


def test_poll_legacy_darwin_permission_error_releases_todo(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_slice.sys, "platform", "darwin")
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    del handle["ps_identity"]

    def denied(pid: int, sig: int) -> None:
        raise PermissionError("not ours")

    monkeypatch.setattr(run_slice.os, "kill", denied)
    monkeypatch.setattr(run_slice, "_process_alive", seams["process_alive"])
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert store.tag_strings(42) == {"STATUS:open", "attempt:1"}


def test_poll_vanished_worktree_fails_and_releases(seams: dict[str, Any]) -> None:
    """jo544355: the tree was reaped under the running session, no exit record
    was ever written, and a pid that still looked alive kept the poll waiting
    for three hours. A missing worktree ends the poll: failed, todo released,
    the log (outside the tree) still read."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    pctx = _poll_ctx(store, ctx)
    Path(handle["log"]).write_text("partial output\n", encoding="utf-8")
    shutil.rmtree(handle["worktree"])
    seams["alive"] = True  # pid reused or a stuck wrapper: liveness lies
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "vanished while the session ran" in (pctx.failure or "")
    assert "partial output" in (pctx.failure or "")
    assert "STATUS:open" in store.tag_strings(42)
    assert "claimed-by:lane-claude-sub" not in store.tag_strings(42)


def test_poll_clean_exit_without_a_commit_fails(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Nothing to do."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.failure is not None and "nothing to land" in pctx.failure
    assert seams["lands"] == []
    assert {"STATUS:open", "attempt:1"} <= store.tag_strings(42)


def test_poll_generator_clean_exit_mints_no_land(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "Slice it", meta={"generate": {"role": "slicer", "from": "x"}})
    ctx, handle = _submit(store, seams)
    # The session minted its children through the MCP while it ran.
    store.add_todo(1, "child one", parent_id=42)
    store.add_todo(2, "child two", parent_id=42)
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Minted td1, td2."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["lands"] == []
    assert "land_job" not in pctx.meta_set


def test_poll_generator_with_no_children_fails_and_releases(
    seams: dict[str, Any],
) -> None:
    """jo546987: the session never reached the MCP, minted nothing, said so
    in its handoff and exited 0 — which read as success and left the todo
    `doing` under its lease. A child-minting generator with no children is
    a failed attempt."""
    store = _Store()
    store.add_todo(42, "Slice it", meta={"generate": {"role": "slicer", "from": "x"}})
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, json.dumps({"is_error": False, "result": "BLOCKED: MCP 401."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.failure is not None and "created no child todos" in pctx.failure
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags
    assert "attempt:1" in tags
    assert not any(t.startswith("claimed-by:") for t in tags)
    assert seams["lands"] == []


def test_poll_reviewer_generator_needs_no_children(seams: dict[str, Any]) -> None:
    """A reviewer's deliverable is one verdict comment, never children."""
    store = _Store()
    store.add_todo(
        42, "Review it", meta={"generate": {"role": "reviewer", "from": "x"}}
    )
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, json.dumps({"is_error": False, "result": "review: LAND"}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"


def test_poll_failed_land_mint_releases_the_lease(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any) -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr(run_slice, "_mint_land", boom)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 feat(x): y (td42)\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "done"}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert (
        pctx.failure is not None and "land_slice mint failed (db down)" in pctx.failure
    )
    assert "attempt:1" in store.tag_strings(42)


class _Tx:
    def __enter__(self) -> str:
        return "conn"

    def __exit__(self, *_exc: Any) -> None:
        return None


def test_mint_land_params_and_idem_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import precis.workers._mint as mint_mod

    minted: list[dict[str, Any]] = []

    def fake_once(store: Any, **kw: Any) -> tuple[int, bool]:
        minted.append(kw)
        return 555, False

    monkeypatch.setattr(mint_mod, "mint_job_once", fake_once)
    store = _Store()
    store.tx = lambda: _Tx()  # type: ignore[attr-defined]
    ctx = _Ctx(store, {"todo_id": 42, "target_node": "node-a"}, plan_root=40)
    assert run_slice._mint_land(ctx, 42, Path("/t/slice-td42"), "claude-sub") == 555
    [kw] = minted
    assert kw["job_type"] == "land_slice" and kw["executor"] == "ssh_node"
    assert kw["idem_key"] == "land_slice:td42" and kw["parent_id"] == 42
    assert kw["params"] == {
        "todo_id": 42,
        "worktree": "/t/slice-td42",
        "branch": "slice/42",
        "lane": "claude-sub",
        "target_node": "node-a",
    }
    assert kw["meta_extra"] == {"plan_root": 40}
    assert kw["conn"] == "conn"


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


def test_poll_deny_path_in_branch_fails_the_attempt(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["diff"] = "src/precis/foo.py\nscripts/lib/gate.sh\n"
    seams["porcelain_z"] = "?? .github/workflows/x.yml\0 M docs/a.md\0"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Landed it."}) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.failure is not None and "deny_write_paths" in pctx.failure
    assert seams["lands"] == []  # the check is pre-land: nothing to the lander
    handoff = store.comments(42)[-1]
    assert handoff.startswith("handoff: attempt FAILED, touched deny_write_paths:")
    assert "`.github/workflows/x.yml`" in handoff
    assert "`scripts/lib/gate.sh`" in handoff
    assert "src/precis/foo.py" not in handoff and "docs/a.md" not in handoff
    assert "Landed it." in handoff
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags and "attempt:1" in tags
    assert "claimed-by:lane-claude-sub" not in tags


def test_poll_deny_check_fails_closed_when_git_fails(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["status_rc"] = 128
    _finish(handle, 0, json.dumps({"is_error": False, "result": "ok"}) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "failing closed" in store.comments(42)[-1]


def test_changed_paths_reads_renames_and_untracked(seams: dict[str, Any]) -> None:
    seams["diff"] = "a.py\n"
    seams["porcelain_z"] = "R  new/x.py\0scripts/test\0?? u.txt\0"
    paths = run_slice.changed_paths(Path("/tree"))
    assert paths == ["a.py", "new/x.py", "scripts/test", "u.txt"]
    assert run_slice.deny_violations(paths, lanes.FACTORY_DENY_WRITE_PATHS) == [
        "scripts/test"
    ]


def test_deny_violations_globs() -> None:
    deny = lanes.FACTORY_DENY_WRITE_PATHS
    hits = run_slice.deny_violations(
        [
            "scripts/ship",
            "scripts/shipit",
            "scripts/lib/a/b.sh",
            "src/precis/plans/nodes.py",
            "src/precis/workers/job_types/run_slice.py",
            "src/precis/workers/job_types/other.py",
            "src/precis/workers/nursery.py",
            "deploy/roles/monitoring/templates/alert_rules.yml.j2",
            "tests/test_run_slice.py",
        ],
        deny,
    )
    assert hits == [
        "deploy/roles/monitoring/templates/alert_rules.yml.j2",
        "scripts/lib/a/b.sh",
        "scripts/ship",
        "src/precis/plans/nodes.py",
        "src/precis/workers/job_types/run_slice.py",
        "src/precis/workers/nursery.py",
    ]


def test_every_seed_lane_carries_the_factory_deny_paths() -> None:
    for lane in lanes.LANES:
        assert lane.deny_write_paths == lanes.FACTORY_DENY_WRITE_PATHS
    for path in (
        "scripts/ship",
        "scripts/test",
        "scripts/lib/**",
        ".github/**",
        "src/precis/plans/**",
        "src/precis/workers/job_types/plan_dispatch.py",
        "src/precis/workers/job_types/gripe_validate.py",
        # the dark release pass (td543856)
        "src/precis/plans/release.py",
        "src/precis/plans/lanes.py",
        "src/precis/workers/job_types/release_cut.py",
        "src/precis/workers/job_types/ci_verdict_poll.py",
        "src/precis/workers/job_types/release_deploy.py",
        "src/precis/workers/job_types/land_slice.py",
        "scripts/release",
    ):
        assert path in lanes.FACTORY_DENY_WRITE_PATHS


def test_release_modules_are_denied_by_name() -> None:
    hits = run_slice.deny_violations(
        [
            "scripts/release",
            "scripts/release-notes",
            "src/precis/workers/job_types/release_cut.py",
            "src/precis/workers/job_types/ci_verdict_poll.py",
            "src/precis/workers/job_types/release_deploy.py",
            "src/precis/workers/job_types/land_slice.py",
            "src/precis/workers/job_types/fix_gripe.py",
        ],
        lanes.FACTORY_DENY_WRITE_PATHS,
    )
    assert hits == [
        "scripts/release",
        "src/precis/workers/job_types/ci_verdict_poll.py",
        "src/precis/workers/job_types/land_slice.py",
        "src/precis/workers/job_types/release_cut.py",
        "src/precis/workers/job_types/release_deploy.py",
    ]


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
