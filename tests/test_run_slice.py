"""run_slice job type + precis.plans.lanes (dark factory step 6, td542094).

Pure tests against a fake store and fake git/launch/process seams: no DB,
no git, no claude is ever spawned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs shell script scripts/ship"
)

from precis.plans import lanes
from precis.plans.lanes import ANY_CLASS, Lane, ladder, pick_lane
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.workers.job_types import get_job_type, known_job_types, run_slice
from precis.workers.job_types import plan_dispatch as pd
from precis.workers.job_types._slice_adapters import get_adapter
from tests._lane_rows import SEED_LANES, seed_lane, use_lanes

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
        if args == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            return _proc(calls.get("git_common", ""))
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
        if args[:3] == ["diff", "--cached", "--quiet"]:
            # rc 1 = something staged (git's own convention).
            return _proc(rc=1 if calls.get("staged") else 0)
        if args[:1] == ["commit"]:
            calls["ahead"] = calls.get("committed_ahead", "")
            if "-F" in args:
                # Capture what git would commit: the file is unlinked after.
                calls["commit_text"] = Path(args[args.index("-F") + 1]).read_text(
                    encoding="utf-8"
                )
            return _proc(rc=calls.get("commit_rc", 0))
        if args[:2] == ["log", "--oneline"]:
            return _proc(calls.get("ahead", ""))
        if calls.get("raise_prefix") is not None and args[
            : len(calls["raise_prefix"])
        ] == list(calls["raise_prefix"]):
            raise calls.get("raise_exc") or subprocess.TimeoutExpired("git", 300)
        if args == ["fetch", "origin", "main"]:
            return _proc(rc=calls.get("fetch_rc", 0))
        if args[:2] == ["merge-base", "--is-ancestor"]:
            # rc 0 = the candidate sha is an ancestor of origin/main.
            return _proc(rc=calls.get("ancestor_rc", 0))
        if args[:3] == ["show", "-s", "--format=%B"]:
            # The candidate sha's own commit message.
            return _proc(calls.get("show_msg", ""), rc=calls.get("show_rc", 0))
        if args[:2] == ["cat-file", "-e"]:
            # rc 0 = the named path exists on origin/main.
            return _proc(rc=calls.get("cat_rc", 0))
        return _proc()

    def fake_launch(argv: list[str], **kw: Any) -> int:
        calls["launch"].append((argv, kw))
        return 4242

    calls["lands"] = []
    calls["land_checked"] = []

    def fake_mint_land(
        ctx: Any, todo_id: int, tree: Path, lane: Any, checked: bool = False
    ) -> int:
        calls["lands"].append((todo_id, Path(tree), lane))
        calls["land_checked"].append(checked)
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
    # Host-refusal backoffs (gr571395): a test sets seams["backoff"] =
    # {lane: until}; default none.
    monkeypatch.setattr(
        lanes, "lane_backoffs", lambda store, now=None: calls.get("backoff", {})
    )
    # The devin lane's submit preflight (gr570688): binary resolves, the
    # CLI reports a login, and trust seeding records the dir it seeded.
    # raising=False keeps the rest of the suite green on a run_slice that
    # predates the seams.
    calls["devin"] = {
        "bin_ok": True,
        "auth": _proc("Logged in (via Devin)\n"),
        "trust": None,
        "trusted": [],
    }

    def fake_seed_trust(under: Path) -> str | None:
        calls["devin"]["trusted"].append(Path(under))
        return calls["devin"]["trust"]

    monkeypatch.setattr(
        run_slice,
        "_devin_bin_ok",
        lambda _bin: calls["devin"]["bin_ok"],
        raising=False,
    )
    monkeypatch.setattr(
        run_slice,
        "_devin_auth_status",
        lambda _bin: calls["devin"]["auth"],
        raising=False,
    )
    monkeypatch.setattr(run_slice, "_devin_seed_trust", fake_seed_trust, raising=False)
    # The seams exercise the claude_p launch path: pin the table to the
    # Claude lane so an unpinned pick lands there (the seed table's codex
    # lane is table-first since td562568). Vendor-lane tests stand their
    # own table.
    use_lanes(monkeypatch, seed_lane("claude-sub"))
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path / "ops"))
    monkeypatch.setenv("PRECIS_RUN_SLICE_MODEL", "claude-test-model")
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    # No slice-token registry on this rig (the dir never exists) → the
    # shared-token path stays the default under test (gr543694).
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(tmp_path / "no-slice-tokens"))
    # Determinism for the codex writable roots: tests that want a Docker
    # socket set DOCKER_HOST themselves.
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    return calls


# ── lanes ──────────────────────────────────────────────────────────


def _lane(name: str, cap: int, classes: set[str], enabled: bool = True) -> Lane:
    return Lane(name, "claude_p", "pool", cap, frozenset(classes), enabled)


def test_seed_table_enabled_lanes() -> None:
    """td562568: devin and codex are on (free vendor lanes, table-first);
    kimi derives a lane since td573681 but stays disabled until an
    operator lights the row, and metered stays a seeded account — the
    ``api`` adapter derives no lane."""
    enabled = [ln.name for ln in SEED_LANES if ln.enabled]
    assert enabled == ["devin", "codex", "claude-sub"]
    devin = seed_lane("devin")
    assert (devin.adapter, devin.cap, devin.default_model) == (
        "devin_tree",
        3,
        "swe-2-high",
    )
    # Only the free SWE-2 tiers: no paid "-lightning" id is ever a rung.
    assert all(m.startswith("swe-2-") and "lightning" not in m for m in devin.models)
    assert [(r.lane, r.model) for r in ladder("code", lanes=SEED_LANES)] == [
        ("devin", "swe-2-high"),
        ("devin", "swe-2-max"),
        ("claude-sub", "claude-sonnet-5"),
        ("claude-sub", "claude-opus-4-8"),
    ]
    sub = seed_lane("claude-sub")
    assert (sub.adapter, sub.credential_pool, sub.cap) == (
        "claude_p",
        "subscription",
        2,
    )
    assert {ln.name for ln in SEED_LANES} == {"claude-sub", "codex", "devin", "kimi"}


def test_claude_sub_accepts_models_and_defaults_to_sonnet() -> None:
    """gr549046: the lane carries the model ids its adapter accepts
    (cheapest first) and the id an unpinned slice gets — the cheapest that
    can still write code, not the frontier pin."""
    sub = seed_lane("claude-sub")
    assert sub.models == (
        "claude-haiku-4-5-20251001",
        "claude-sonnet-5",
        "claude-opus-4-8",
    )
    assert sub.default_model == "claude-sonnet-5"
    for lane in SEED_LANES:
        assert lane.default_model is None or lane.default_model in lane.models


def test_resolve_model_order(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """params.model > PRECIS_RUN_SLICE_MODEL > the lane's default_model >
    the router's frontier pin (gr549046)."""
    lane = seed_lane("claude-sub")
    assert run_slice.resolve_model({"model": " claude-pinned "}, lane) == (
        "claude-pinned"
    )
    # The seams env pins PRECIS_RUN_SLICE_MODEL: a claude id, claude_p only.
    assert run_slice.resolve_model({}, lane) == "claude-test-model"
    assert run_slice.resolve_model({}, _vendor_lane("codex", "codex_exec")) is None
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    assert run_slice.resolve_model({}, lane) == "claude-sonnet-5"
    monkeypatch.delenv("PRECIS_MODEL_OPUS", raising=False)
    monkeypatch.delenv("PRECIS_LLM_MODEL_FRONTIER", raising=False)
    assert run_slice.resolve_model({}) == "claude-opus-4-8"
    assert run_slice.resolve_model({}, _lane("bare", 1, {ANY_CLASS})) == (
        "claude-opus-4-8"
    )


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


def test_pick_lane_honours_a_pinned_model() -> None:
    """A ``params.model`` pin narrows the fit to lanes that run it: the
    table-first codex lane declares no models, so a triage-pinned claude id
    skips it; an id no lane accepts picks nothing (the caller fails closed)."""
    codex = Lane("codex", "codex_exec", "codex", 1, frozenset({"code"}))
    sub = Lane(
        "claude-sub",
        "claude_p",
        "sub",
        2,
        frozenset({ANY_CLASS}),
        models=("claude-sonnet-5",),
        model_prefix="claude-",
    )
    table = (codex, sub)
    picked = pick_lane(None, "code", lanes=table, counts={})
    assert picked is codex
    picked = pick_lane(None, "code", lanes=table, counts={}, model="claude-sonnet-5")
    assert picked is sub
    # Outside ``models`` but under the prefix: the CLI takes any claude id.
    picked = pick_lane(None, "code", lanes=table, counts={}, model="claude-sonnet-5-5")
    assert picked is sub
    assert pick_lane(None, "code", lanes=table, counts={}, model="gpt-5-codex") is None
    assert not codex.accepts_model("gpt-5-codex")
    # The seed table: codex accepts no pin, devin only its free list.
    seed_codex = seed_lane("codex")
    seed_devin = seed_lane("devin")
    seed_sub = seed_lane("claude-sub")
    assert not seed_codex.accepts_model("claude-sonnet-5-5")
    assert seed_devin.accepts_model("swe-2-high")
    assert not seed_devin.accepts_model("swe-2-high-lightning")
    assert seed_sub.accepts_model("claude-sonnet-5-5")
    assert seed_sub.accepts_model("claude-opus-4-8")


def test_pick_lane_skips_a_backed_off_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr571395: a lane whose newest ``run_slice`` job refused the launch
    for a host reason leaves the fit set until the stamped window ends —
    the pick lands on the next lane rather than probing a dead host for
    every task."""
    use_lanes(monkeypatch)
    future = datetime.now(UTC) + timedelta(minutes=30)
    picked = pick_lane(None, "code", counts={}, backoff={"devin": future})
    assert picked is not None and picked.name == "claude-sub"
    # An expired window no longer skips: the next submit is the probe.
    past = datetime.now(UTC) - timedelta(minutes=1)
    picked = pick_lane(None, "code", counts={}, backoff={"devin": past})
    assert picked is not None and picked.name == "devin"


def test_pick_lane_reads_live_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seed table: devin (cap 3, free) and claude-sub (cap 2,
    subscription) fit ``code``; codex carries no ``code`` class (gr573563)."""
    use_lanes(monkeypatch)
    monkeypatch.setattr(lanes, "lane_backoffs", lambda store: {})
    monkeypatch.setattr(
        lanes,
        "live_lane_counts",
        lambda store: {"claude-sub": 2, "codex": 1, "devin": 3},
    )
    assert pick_lane(object(), "code") is None
    monkeypatch.setattr(
        lanes,
        "live_lane_counts",
        lambda store: {"claude-sub": 1, "codex": 1, "devin": 3},
    )
    picked = pick_lane(object(), "code")
    assert picked is not None and picked.name == "claude-sub"
    # Devin full: the subscription lane is next — codex no longer fits code.
    monkeypatch.setattr(
        lanes, "live_lane_counts", lambda store: {"claude-sub": 1, "devin": 3}
    )
    picked = pick_lane(object(), "code")
    assert picked is not None and picked.name == "claude-sub"
    # All free: the free lane is first in the table (td559457, gr573563).
    monkeypatch.setattr(lanes, "live_lane_counts", lambda store: {})
    picked = pick_lane(object(), "code")
    assert picked is not None and picked.name == "devin"


def test_pick_lane_devin_wins_with_one_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr573563: one or two live jobs on devin (cap 3) must not send the
    pick to an idle paid lane — the cost class is minimised before the
    live count."""
    use_lanes(monkeypatch)
    for live in (1, 2):
        picked = pick_lane(None, "code", counts={"devin": live})
        assert picked is not None and picked.name == "devin"


def test_pick_lane_cost_class_before_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr573563: cost class beats load — a free lane at two of three
    slots is picked ahead of an idle subscription lane."""
    use_lanes(monkeypatch, seed_lane("devin"), seed_lane("claude-sub"))
    picked = pick_lane(None, "code", counts={"devin": 2})
    assert picked is not None and picked.name == "devin"


def test_pick_lane_load_only_within_a_class() -> None:
    """Inside one cost class the live count orders the pick and a tie
    keeps table order — the class sort never reorders equal-class lanes."""
    table = (_lane("a", 2, {ANY_CLASS}), _lane("b", 2, {ANY_CLASS}))
    picked = pick_lane(None, "code", lanes=table, counts={"a": 1, "b": 0})
    assert picked is not None and picked.name == "b"
    picked = pick_lane(None, "code", lanes=table, counts={"a": 1})
    assert picked is not None and picked.name == "b"
    picked = pick_lane(None, "code", lanes=table, counts={})
    assert picked is not None and picked.name == "a"


def test_pick_lane_paid_not_ok_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr573563: ``paid_ok=False`` holds paid lanes back while a fit free
    lane exists — the free lane at its cap is a wait (``None``), never a
    paid pick. With every free lane filtered (a model pin it cannot run,
    a host-refusal backoff) the paid lanes stay in the set."""
    free = Lane(
        "free",
        "devin_tree",
        "free-promo",
        1,
        frozenset({"code"}),
        vendor="cognition",
    )
    paid = Lane(
        "paid",
        "claude_p",
        "subscription",
        1,
        frozenset({ANY_CLASS}),
        vendor="anthropic",
        models=("claude-sonnet-5",),
    )
    use_lanes(monkeypatch, free, paid)
    # The free lane at its cap is a wait, not a paid pick.
    assert pick_lane(None, "code", counts={"free": 1}, paid_ok=False) is None
    picked = pick_lane(None, "code", counts={"free": 1})
    assert picked is not None and picked.name == "paid"
    # Every free lane filtered → the paid lanes stay in the set.
    picked = pick_lane(None, "code", counts={}, model="claude-sonnet-5", paid_ok=False)
    assert picked is not None and picked.name == "paid"
    future = datetime.now(UTC) + timedelta(minutes=30)
    picked = pick_lane(None, "code", counts={}, paid_ok=False, backoff={"free": future})
    assert picked is not None and picked.name == "paid"


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
    off = Lane("off", "claude_p", "sub", 1, frozenset({ANY_CLASS}), enabled=False)
    metered = Lane("metered", "api", "metered", 1, frozenset({"code"}))
    use_lanes(monkeypatch, *SEED_LANES, off, metered)
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
    # An enabled account on an unlaunchable adapter (metered: api)
    # derives to no lane, so a pin on it reads unknown, not disabled.
    api = run_slice.validate_submit(store, params={"todo_id": 7, "lane": "metered"})
    assert api is not None and "unknown lane" in api
    disabled = run_slice.validate_submit(store, params={"todo_id": 7, "lane": "off"})
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
    # gr549046: the close pins each run_slice child's model in meta.op,
    # naming the lane's accepted ids.
    assert "'op': {'type': 'run_slice'" in brief
    assert "'model': '<id>'" in brief
    assert "'effort': '<low|medium|high>'" in brief
    assert "claude-sonnet-5" in brief
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
    # read-only review lane (gr573555): the verdict ends with the marker,
    # and the lane runs nothing — it reads the squash diff and the files.
    assert "(read-only, tests not run)" in tail
    assert "run no tests" in tail
    assert "no typecheck" in tail
    assert "no lint" in tail
    assert "`uv`" in tail
    assert "scripts/test" in tail
    assert "git show" in tail
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


def test_generator_close_links_not_prose_and_house_title() -> None:
    """gr548798 / gr548799: a generator's closing instructions put order
    in blocked-by links (never sequencing prose) and title children in
    the summary-title house style."""
    text = "\n".join(run_slice._generator_close(5))
    assert "blocked-by" in text
    assert "never write sequencing prose" in text
    assert "· " in text
    assert "telegraphic" in text


def test_generator_close_pins_child_model_in_meta() -> None:
    """gr549046: with the lane's accepted ids given, the close tells the
    generator to pin each run_slice child's model and effort in
    ``meta.op.params`` — prose on the child never reaches dispatch."""
    lane = seed_lane("claude-sub")
    text = "\n".join(run_slice._generator_close(5, lane.models))
    assert "'op': {'type': 'run_slice'" in text
    assert "'model': '<id>'" in text
    assert "'effort': '<low|medium|high>'" in text
    for model in lane.models:
        assert model in text
    assert "never in prose" in text
    # No accepted ids on the lane → no pin recipe, the mint instruction stays.
    bare = "\n".join(run_slice._generator_close(5))
    assert "'op': {'type': 'run_slice'" not in bare
    assert "create the child todos" in bare


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
    # td565331: the land's lint gate reopened a codex build for six missing
    # test annotations; the brief runs the land's lint steps before the
    # commit (each named in test_brief_names_the_lands_lint_steps).
    assert "uv run mypy src tests" in tail
    assert tail.index("uv run mypy") < tail.index(". Commit on")


def test_brief_names_the_lands_lint_steps() -> None:
    """gr573347 / td573419: a build session's prompt names the same lint
    the land runs — the whole tree, not just touched files — and forbids
    a commit while any step is red."""
    brief = run_slice.render_brief(
        todo_id=42,
        title="Build the widget",
        body="Do X.",
        latest=None,
        lane="claude-sub",
        model="m",
        effort="high",
    )
    tail = brief[brief.index("## When done: commit on the branch and hand off") :]
    for step in (
        "uv run ruff check --fix .",
        "uv run ruff format .",
        "uv run mypy src tests",
        "uv run lint-imports",
        "uv run pytest -q -n0 -p no:cacheprovider tests/test_deploy_tree_no_secrets.py",
    ):
        assert step in tail
    for step in run_slice._LAND_LINT_STEPS:
        assert f"`{step}`" in tail
    # one continuous numbered list: lint, then commit/clean/stop —
    # the commit step never restarts at 1
    assert "1. Commit on" not in tail
    assert f"{len(run_slice._LAND_LINT_STEPS) + 2}. Commit on" in tail
    assert tail.index("uv run ruff check --fix .") < tail.index(". Commit on")
    assert "do not commit while any" in tail.lower()


def test_brief_lint_steps_match_ship() -> None:
    """gr573347 / td573419: every lint step the prompt names appears in
    scripts/ship's pre-qland lint (``QLAND_CMD`` / ``QLAND_TAIL`` /
    ``QLAND_MYPY`` / ``SECRET_SCAN``), so the prompt cannot drift from
    the land."""
    ship = (Path(__file__).resolve().parents[1] / "scripts" / "ship").read_text(
        encoding="utf-8"
    )
    qland = "\n".join(
        line
        for line in ship.splitlines()
        if re.match(r"\s*(QLAND_(CMD|TAIL|MYPY)|SECRET_SCAN)=", line)
    )
    assert qland
    for step in run_slice._LAND_LINT_STEPS:
        assert step in qland, step


def test_render_brief_asks_for_the_tests_marker() -> None:
    """gr573563: the non-generator brief asks the builder to END its final
    message with one ``tests:`` line — ``tests: ran`` or
    ``tests: NOT RUN — <which step, why>`` — so the land mints checked
    when the gate could not run. The first-line verdicts stay first."""
    brief = run_slice.render_brief(
        todo_id=42,
        title="Build the widget",
        body="Do X.",
        latest=None,
        lane="claude-sub",
        model="m",
        effort="high",
    )
    assert "tests: ran" in brief
    assert "tests: NOT RUN" in brief
    assert "Your final message is posted on the todo as the `handoff:` comment" in brief


def test_code_brief_pins_the_verified_moot_wording() -> None:
    """td584381 S1d review round 2: a CODE task's brief asks for
    ``moot: gr<N> already fixed by <sha>`` and says a bare ``moot:`` is
    not accepted for a code build (it goes to rescue) — the verified
    claim the tick checks; a generator's brief keeps the plain
    ``moot: <why>`` sentence (trust, no sha check)."""
    code = run_slice.render_brief(
        todo_id=42,
        title="Fix the gate",
        body="Do it.",
        latest=None,
        lane="claude-sub",
        model="m",
        effort="high",
    )
    assert "moot: gr<N> already fixed by <sha>" in code
    assert "a bare `moot: <why>` is not accepted for a code build" in code
    gen = run_slice.render_brief(
        todo_id=43,
        title="Slice the work",
        body="Split it.",
        latest=None,
        lane="claude-sub",
        model="m",
        effort="high",
        generator=True,
    )
    assert "`moot: <why>` when the source is already resolved" in gen
    assert "already fixed by <sha>" not in gen


# ── submit / poll ──────────────────────────────────────────────────


def _submit(store: _Store, seams: dict[str, Any], **params: Any) -> tuple[_Ctx, Any]:
    ctx = _Ctx(store, {"todo_id": 42, **params})
    handle = run_slice.submit(ctx, None)
    return ctx, handle


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_submit_launches_and_claims(
    seams: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    monkeypatch.setattr(run_slice.sys, "platform", platform)
    store = _Store()
    store.add_todo(42, "Build the widget", body="Do X.")
    ctx, handle = _submit(store, seams, effort="medium")

    tree = tmp_path / ".claude" / "worktrees" / "slice-td42"
    started_at = handle.pop("started_at")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", started_at)
    if platform == "darwin":
        assert handle.pop("ps_identity") == seams["ps_identity"]
    else:
        assert "ps_identity" not in handle
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
        "adapter": "claude_p",
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
    assert len(deny) == 2 * 4 * len(lanes.STRICT_DENY)
    # The service account's clone is never trusted interactively; without
    # this the checkout's .mcp.json precis server is skipped in -p mode.
    assert json.loads(argv[-1])["enableAllProjectMcpServers"] is True
    assert kw["cwd"] == tree
    assert kw["stdin"] == tree / "scratch" / "brief.md"
    assert kw["env"]["PRECIS_SLICE_ID"] == "td42"


def test_submit_unpinned_child_uses_lane_default_model(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr549046: a child the generator left unpinned runs on the lane's
    default_model (Sonnet-class), not the frontier pin."""
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    store = _Store()
    store.add_todo(42, "Verify the fix", meta={"op": "run_slice"})
    ctx, _handle = _submit(store, seams)
    assert ctx.meta_set["model"] == "claude-sonnet-5"
    [(argv, _kw)] = seams["launch"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"


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
    deferred: list[tuple[Any, str]] = []

    def _defer(wake_at: Any, note: str) -> bool:
        deferred.append((wake_at, note))
        return True

    ctx.defer = _defer
    before = datetime.now(UTC)
    assert run_slice.submit(ctx, None) is None
    assert deferred and "no lane free" in deferred[0][1]
    # gr561491: a lane-full park names a short wake, not the executor's
    # two-hour quota fallback (wake_at=None).
    wake_at = deferred[0][0]
    assert isinstance(wake_at, datetime)
    horizon = wake_at - before
    assert (
        timedelta(minutes=5)
        < horizon
        < run_slice.LANE_FULL_BACKOFF + timedelta(minutes=1)
    )
    assert ctx.failure is None
    assert seams["launch"] == []
    assert "STATUS:open" in store.tag_strings(42)


def test_submit_parks_when_pinned_lane_full(seams: dict[str, Any]) -> None:
    """td562568: ``params.lane`` is held to the lane's cap like a pick —
    an escalated retry pinned on a free lane no longer runs it over."""
    seams["live"] = {"claude-sub": 2}
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42, "lane": "claude-sub"})
    deferred: list[tuple[Any, str]] = []

    def _defer(wake_at: Any, note: str) -> bool:
        deferred.append((wake_at, note))
        return True

    ctx.defer = _defer
    assert run_slice.submit(ctx, None) is None
    assert deferred and "pinned by params.lane is at its cap (2/2)" in deferred[0][1]
    assert isinstance(deferred[0][0], datetime)
    assert ctx.failure is None
    assert seams["launch"] == []
    assert "STATUS:open" in store.tag_strings(42)
    # One slot free → the pin launches.
    seams["live"] = {"claude-sub": 1}
    ctx2, handle = _submit(store, seams, lane="claude-sub")
    assert handle is not None and ctx2.meta_set["lane"] == "claude-sub"


def test_lane_wait_parks_on_their_own_budget(seams: dict[str, Any]) -> None:
    """gr572118: a lane-full wait counts under ``meta.wait_parks``, not the
    shared ``pause_parks`` budget (``MAX_PAUSE_PARKS`` = 6 → the seventh
    10-min park read as a failed attempt on a job that never ran)."""
    seams["live"] = {"claude-sub": 2}
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42}, pause_parks=6)
    deferred: list[tuple[Any, str]] = []

    def _defer(wake_at: Any, note: str) -> bool:
        deferred.append((wake_at, note))
        return True

    ctx.defer = _defer
    assert run_slice.submit(ctx, None) is None
    assert deferred and "no lane free" in deferred[0][1]
    assert ctx.failure is None
    assert ctx.meta_set["pause_parks"] == 0
    assert ctx.meta_set["wait_parks"] == 1


def test_lane_wait_past_the_cap_fails_attempt_free(
    seams: dict[str, Any],
) -> None:
    """gr572118: past ``LANE_WAIT_PARK_CAP`` the wait ends ``failed`` but
    marked ``meta.attempt_free`` — the job never ran, so it is no attempt
    toward the todo's retry cap."""
    seams["live"] = {"claude-sub": 2}
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42}, wait_parks=run_slice.LANE_WAIT_PARK_CAP)

    def _defer(wake_at: Any, note: str) -> bool:
        raise AssertionError("a capped wait must not park again")

    ctx.defer = _defer
    assert run_slice.submit(ctx, None) is None
    assert ctx.failure is not None
    assert "no lane free" in ctx.failure
    assert "no lane slot for" in ctx.failure
    assert ctx.meta_set["attempt_free"] == "lane-wait"
    assert "attempt:1" not in store.tag_strings(42)


def test_submit_parks_for_the_free_lane_then_falls_through(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr573563: a free lane at its cap parks the build rather than paying
    — the pick falls through to a subscription lane only once the
    free-lane wait (``factory.free_lane_wait_min``, default 20 min of
    ``wait_parks``) has passed, and never lands on codex."""
    use_lanes(monkeypatch)  # the seams table is claude-sub alone
    seams["live"] = {"devin": 3}
    store = _Store()
    store.add_todo(42, "Build")
    deferred: list[tuple[Any, str]] = []

    def _defer(wake_at: Any, note: str) -> bool:
        deferred.append((wake_at, note))
        return True

    ctx = _Ctx(store, {"todo_id": 42}, wait_parks=0)
    ctx.defer = _defer
    assert run_slice.submit(ctx, None) is None
    assert deferred and "devin" in deferred[0][1] and "20 min" in deferred[0][1]
    assert "paid lane" in deferred[0][1]
    assert ctx.failure is None
    assert seams["launch"] == []

    # Past the wait the pick falls through to the subscription lane.
    ctx2 = _Ctx(store, {"todo_id": 42}, wait_parks=2)
    handle = run_slice.submit(ctx2, None)
    assert handle is not None and ctx2.meta_set["lane"] == "claude-sub"

    # A longer configured wait keeps parking past 20 minutes of parks.
    monkeypatch.setattr(
        run_slice, "_free_lane_wait_min", lambda store: 50, raising=False
    )
    ctx3 = _Ctx(store, {"todo_id": 42}, wait_parks=2)
    ctx3.defer = _defer
    assert run_slice.submit(ctx3, None) is None
    assert "devin" in deferred[-1][1] and "50 min" in deferred[-1][1]
    assert ctx3.meta_set["wait_parks"] == 3


def test_submit_refuses_a_generator_on_a_lane_without_mcp(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generator mints its children through the precis MCP; the codex lane
    has none. A hand-minted generator left in the default ``code`` class
    (the templates class theirs off it) fails the submit with the fix
    named, before the claim: no launch, no attempt burned."""
    # The seams fixture pinned the table to claude-sub; stand the seed
    # table's codex lane (codex_exec adapter, no MCP) in its place.
    codex = Lane("codex", "codex_exec", "codex", 1, frozenset({"code"}))
    use_lanes(
        monkeypatch,
        codex,
    )
    store = _Store()
    store.add_todo(
        42, "Triage gr1", meta={"generate": {"role": "triage", "from": "gr1"}}
    )
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None
    assert "has no MCP" in ctx.failure and "meta.task_class" in ctx.failure
    assert seams["launch"] == []
    assert "STATUS:open" in store.tag_strings(42)
    assert "claimed-by:lane-codex" not in store.tag_strings(42)


def test_submit_runs_a_reviewer_on_a_lane_without_mcp(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """td564019 cut 1: a reviewer mints nothing, so it needs no MCP — its
    verdict rides the final message (``handoff: review: …``, which
    plan_dispatch reads) and the reviewed land is inlined in its brief."""
    codex = Lane("codex", "codex_exec", "codex", 1, frozenset({"code", "review"}))
    use_lanes(
        monkeypatch,
        codex,
    )
    store = _Store()
    store.add_todo(
        7,
        "Build the fix for gr1",
        body="Fix the widget leak in a.py.",
        status="done",
        comments=("handoff: committed the fix with its test", "landed: abc1234def"),
    )
    store.add_todo(
        42,
        "Review the gr1 fix after land",
        body="Review td7.",
        meta={"generate": {"role": "reviewer", "from": "td7"}, "task_class": "review"},
    )
    ctx, handle = _submit(store, seams)
    assert handle is not None and ctx.meta_set["lane"] == "codex"
    tree = tmp_path / ".claude" / "worktrees" / "slice-td42"
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert "This lane has no precis MCP" in brief
    target = brief[brief.index("## Review target") : brief.index("## When done")]
    assert "td7: Build the fix for gr1" in target
    assert "git show abc1234def" in target
    assert "Fix the widget leak in a.py." in target
    assert "handoff: committed the fix with its test" in target
    tail = brief[brief.index("## When done") :]
    assert tail.startswith("## When done: post your verdict")
    assert "FIRST line of your final message" in tail
    assert "Post exactly one comment" not in tail
    assert "`review: FIX — <list of findings>`" in tail


def test_review_target_without_a_land_says_so(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(7, "Build it", body="Build.", comments=("handoff: half done",))
    meta = {"generate": {"role": "reviewer", "from": "td7"}}
    target = run_slice._review_target(store, meta)
    assert target is not None
    assert (
        "no `landed:` comment yet" in target and "no-op: nothing landed yet" in target
    )
    assert run_slice._review_target(store, {"generate": {"role": "triage"}}) is None
    assert run_slice._review_target(store, {"op": "run_slice"}) is None
    assert (
        run_slice._review_target(
            store, {"generate": {"role": "reviewer", "from": "td9"}}
        )
        is None
    )


def test_submit_routes_a_pinned_claude_model_off_the_codex_lane(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td543634: a build whose triage pinned a claude id, unpinned on lane,
    lands on the Claude lane although codex is table-first and free; with
    no lane that runs the id the submit fails closed before the claim."""
    codex = Lane("codex", "codex_exec", "codex", 1, frozenset({"code"}))
    sub = seed_lane("claude-sub")
    use_lanes(monkeypatch, codex, sub)
    store = _Store()
    store.add_todo(42, "Build the fix for gr1")
    ctx, handle = _submit(store, seams, model="claude-sonnet-5-5")
    assert handle is not None and ctx.failure is None
    assert ctx.meta_set["lane"] == "claude-sub"
    assert ctx.meta_set["model"] == "claude-sonnet-5-5"

    use_lanes(
        monkeypatch,
        codex,
    )
    store2 = _Store()
    store2.add_todo(42, "Build the fix for gr1")
    ctx2, handle2 = _submit(store2, seams, model="claude-sonnet-5-5")
    assert handle2 is None and ctx2.status == "failed"
    assert ctx2.failure is not None
    assert "accepts model 'claude-sonnet-5-5'" in ctx2.failure
    assert "params.lane" in ctx2.failure
    assert "STATUS:open" in store2.tag_strings(42)
    assert "claimed-by:lane-codex" not in store2.tag_strings(42)


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


# ── per-slice read MCP grant (gr574082, td574161) ──────────────────


def test_read_mcp_scope_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PRECIS_SLICE_READ_MCP`` takes the scope name as its value; unset
    or anything else is today's no-MCP behaviour (the switch ships dark)."""
    monkeypatch.delenv("PRECIS_SLICE_READ_MCP", raising=False)
    assert run_slice._read_mcp_scope() is None
    monkeypatch.setenv("PRECIS_SLICE_READ_MCP", "bogus")
    assert run_slice._read_mcp_scope() is None
    monkeypatch.setenv("PRECIS_SLICE_READ_MCP", "reader")
    assert run_slice._read_mcp_scope() == "reader"
    monkeypatch.setenv("PRECIS_SLICE_READ_MCP", " triage ")
    assert run_slice._read_mcp_scope() == "triage"


def _read_mcp_lane(
    monkeypatch: pytest.MonkeyPatch, adapter: str = "codex_exec"
) -> None:
    """Pin a vendor read lane and arm the scope switch."""
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL", raising=False)
    monkeypatch.setenv("PRECIS_SLICE_READ_MCP", "triage")
    if adapter == "codex_exec":
        monkeypatch.setenv("PRECIS_CODEX_BIN", "/vendor/codex")
        use_lanes(monkeypatch, _vendor_lane("codex", "codex_exec"))
    else:
        monkeypatch.setenv("PRECIS_DEVIN_BIN", "/vendor/devin")
        use_lanes(
            monkeypatch,
            _vendor_lane("devin", "devin_tree", models=("swe-2-high",)),
        )


def test_read_mcp_mints_a_scoped_grant_and_codex_flags(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PRECIS_SLICE_READ_MCP=triage`` on a read lane: the session's
    ``PRECIS_MCP_TOKEN`` is the minted scoped grant (never the inherited
    shared token, never the OAuth login), and the codex argv carries the
    two ``-c`` dotted overrides pointing at the checkout's precis MCP."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "shared-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-leak")
    _read_mcp_lane(monkeypatch)
    _write_mcp_json(tmp_path)
    monkeypatch.setattr(run_slice, "_mcp_preflight_open", lambda req, timeout: _Resp())
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="codex")
    assert handle is not None and ctx.failure is None
    [(argv, kw)] = seams["launch"]
    token = kw["env"]["PRECIS_MCP_TOKEN"]
    assert token != "shared-token"
    assert re.fullmatch(r"td42j900\.[0-9a-f]{64}", token)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in kw["env"]
    content = (registry / "td42j900").read_text(encoding="utf-8")
    assert token not in content
    fields = content.split()
    assert "scope=triage" in fields and "refs=td42" in fields
    flags = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"]
    assert "mcp_servers.precis.url=http://127.0.0.1:8765/mcp" in flags
    assert "mcp_servers.precis.bearer_token_env_var=PRECIS_MCP_TOKEN" in flags
    assert "mcp token: slice td42j900" in ctx.chunks[-1][1]
    # The token stays out of every persisted surface.
    brief = (Path(handle["worktree"]) / "scratch" / "brief.md").read_text(
        encoding="utf-8"
    )
    assert token not in brief and token not in ctx.chunks[-1][1]
    assert "no precis MCP" not in brief


def test_read_mcp_off_without_the_switch(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset switch = today's behaviour exactly: no mint, no env token,
    no ``-c`` flags — even with the registry and a codex lane ready."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    monkeypatch.delenv("PRECIS_SLICE_READ_MCP", raising=False)
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL", raising=False)
    monkeypatch.setenv("PRECIS_CODEX_BIN", "/vendor/codex")
    use_lanes(monkeypatch, _vendor_lane("codex", "codex_exec"))
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "shared-token")
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="codex")
    assert handle is not None
    [(argv, kw)] = seams["launch"]
    assert "PRECIS_MCP_TOKEN" not in kw["env"]
    assert "-c" not in argv
    assert "mcp token: none" in ctx.chunks[-1][1]
    assert not list(registry.iterdir())


def test_read_mcp_non_code_task_class_mints_nothing(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read mint is a code-class affordance: a docs-class todo on a
    read lane launches as today even with the switch armed."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL", raising=False)
    monkeypatch.setenv("PRECIS_SLICE_READ_MCP", "triage")
    monkeypatch.setenv("PRECIS_CODEX_BIN", "/vendor/codex")
    use_lanes(
        monkeypatch,
        Lane(
            name="codex",
            adapter="codex_exec",
            credential_pool="codex",
            cap=1,
            task_classes=frozenset({ANY_CLASS}),
            enabled=True,
        ),
    )
    store = _Store()
    store.add_todo(42, "Write docs", meta={"task_class": "docs"})
    ctx, handle = _submit(store, seams, lane="codex")
    assert handle is not None and ctx.failure is None
    [(_argv, kw)] = seams["launch"]
    assert "PRECIS_MCP_TOKEN" not in kw["env"]
    assert not list(registry.iterdir())


def test_read_mcp_preflight_failure_launches_without_the_mcp(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected scoped bearer must not kill the attempt: the grant is
    revoked, the brief is rewritten to the no-MCP wording and the launch
    proceeds — ``mcp token: none (<reason>)`` names the failure."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    _read_mcp_lane(monkeypatch)
    _write_mcp_json(tmp_path)

    def reject(req: Any, timeout: float) -> _Resp:
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", Message(), None)

    monkeypatch.setattr(run_slice, "_mcp_preflight_open", reject)
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="codex")
    assert handle is not None and ctx.failure is None
    [(argv, kw)] = seams["launch"]
    assert "PRECIS_MCP_TOKEN" not in kw["env"]
    assert "-c" not in argv
    event = ctx.chunks[-1][1]
    assert "mcp token: none" in event and "401" in event
    assert not (registry / "td42j900").exists()
    brief = (Path(handle["worktree"]) / "scratch" / "brief.md").read_text(
        encoding="utf-8"
    )
    assert "no precis MCP. Do not try" in brief


def test_read_mcp_devin_config_written_and_revoked(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Devin's per-tree config carries the minted bearer at 0600 (never
    in argv or the env beyond PRECIS_MCP_TOKEN), rides the handle into
    ``_handle_grant`` and is unlinked wherever the grant is revoked."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    _read_mcp_lane(monkeypatch, adapter="devin_tree")
    _write_mcp_json(tmp_path)
    monkeypatch.setattr(run_slice, "_mcp_preflight_open", lambda req, timeout: _Resp())
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="devin")
    assert handle is not None and ctx.failure is None
    tree = Path(handle["worktree"])
    config = tree / ".devin" / "mcp_config.local.json"
    assert config.is_file()
    assert config.stat().st_mode & 0o777 == 0o600
    [(_argv, kw)] = seams["launch"]
    token = kw["env"]["PRECIS_MCP_TOKEN"]
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "precis": {
                "type": "http",
                "url": "http://127.0.0.1:8765/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    assert handle["devin_config"] == str(config)

    _finish(handle, 0, "handoff: done\n")
    assert run_slice.poll(_poll_ctx(store, ctx), handle) is True
    assert not config.exists()
    assert not (registry / "td42j900").exists()


def test_read_mcp_devin_config_unlinked_on_kill(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    _read_mcp_lane(monkeypatch, adapter="devin_tree")
    _write_mcp_json(tmp_path)
    monkeypatch.setattr(run_slice, "_mcp_preflight_open", lambda req, timeout: _Resp())
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="devin")
    config = Path(handle["devin_config"])
    assert config.is_file()
    run_slice.kill(ctx, handle)
    assert not config.exists()


def test_read_mcp_devin_config_unlinked_on_release(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release revoke path unlinks the Devin config the same as
    kill/poll — the bearer never outlives the grant on any exit."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    _read_mcp_lane(monkeypatch, adapter="devin_tree")
    _write_mcp_json(tmp_path)
    monkeypatch.setattr(run_slice, "_mcp_preflight_open", lambda req, timeout: _Resp())
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="devin")
    config = Path(handle["devin_config"])
    assert config.is_file()
    run_slice.release(_poll_ctx(store, ctx), handle)
    assert not config.exists()
    assert not (registry / "td42j900").exists()


def test_read_mcp_devin_config_unlinked_on_host_refusal(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host refusal after the config write revokes the grant — the
    minted bearer and the per-tree Devin config both die before the
    (never-launched) session could see them."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    _read_mcp_lane(monkeypatch, adapter="devin_tree")
    _write_mcp_json(tmp_path)
    monkeypatch.setattr(run_slice, "_mcp_preflight_open", lambda req, timeout: _Resp())
    seams["devin"]["bin_ok"] = False
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="devin")
    assert handle is None and ctx.status == "failed"
    assert "host_refusal" in ctx.meta_set
    config = (
        tmp_path
        / ".claude"
        / "worktrees"
        / "slice-td42"
        / ".devin"
        / "mcp_config.local.json"
    )
    assert not config.exists()
    assert not (registry / "td42j900").exists()


def test_read_mcp_devin_config_refuses_a_symlink(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``O_NOFOLLOW`` on the config open: a pre-planted symlink at the
    path fails the write instead of writing the bearer through it — the
    grant revokes and the slice launches without the MCP."""
    registry = tmp_path / "slice-tokens"
    registry.mkdir()
    monkeypatch.setenv("PRECIS_MCP_SLICE_TOKEN_DIR", str(registry))
    _read_mcp_lane(monkeypatch, adapter="devin_tree")
    tree = _write_mcp_json(tmp_path)
    monkeypatch.setattr(run_slice, "_mcp_preflight_open", lambda req, timeout: _Resp())
    decoy = tmp_path / "decoy.json"
    decoy.write_text("{}", encoding="utf-8")
    devin_dir = tree / ".devin"
    devin_dir.mkdir(parents=True)
    (devin_dir / "mcp_config.local.json").symlink_to(decoy)
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="devin")
    assert handle is not None and ctx.failure is None
    [(_argv, kw)] = seams["launch"]
    assert "PRECIS_MCP_TOKEN" not in kw["env"]
    assert "mcp token: none" in ctx.chunks[-1][1]
    assert not (registry / "td42j900").exists()
    assert decoy.read_text(encoding="utf-8") == "{}"


def test_fixes_refs_collects_the_todo_and_fixed_gripes() -> None:
    """``_fixes_refs`` names the grant's read set: the own todo plus the
    gripes its ``fixes`` links point at — dead links and a store without
    link reads both narrow to the todo itself."""

    class _Link:
        def __init__(self, dst: int) -> None:
            self.dst_ref_id = dst

    class _LinkStore(_Store):
        def links_for(
            self, ref_id: int, *, direction: str = "both", relation: Any = None
        ) -> list[Any]:
            assert ref_id == 42 and direction == "out" and relation == "fixes"
            return [_Link(800), _Link(801)]

        def get_ref(self, *, kind: str, id: int) -> Any:
            if kind == "gripe":
                return _Ref(id, f"gripe {id}") if id == 800 else None
            return super().get_ref(kind=kind, id=id)

    store = _LinkStore()
    store.add_todo(42, "t")
    assert run_slice._fixes_refs(store, 42) == ("td42", "gr800")
    assert run_slice._fixes_refs(_Store(), 42) == ("td42",)


def test_slice_env_minted_token_without_the_oauth_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read lanes' third env state: a minted bearer goes to
    ``PRECIS_MCP_TOKEN`` (beating an inherited one) while the Claude
    OAuth login is still popped — the vendor reads precis with its own
    account login."""
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "shared-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    env = run_slice.slice_env(42, "low", mcp_token="td42.abc", mcp=False)
    assert env["PRECIS_MCP_TOKEN"] == "td42.abc"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    env = run_slice.slice_env(42, "low", mcp=False)
    assert "PRECIS_MCP_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_render_brief_read_mcp_paragraphs() -> None:
    """The scope's brief paragraph replaces the no-MCP one: reader's is
    the spec text verbatim; triage's names its write set and never says
    'read-only' or that nothing is writable."""
    kw: dict[str, Any] = {
        "todo_id": 42,
        "title": "t",
        "body": "b",
        "latest": None,
        "lane": "codex",
        "model": None,
        "effort": "low",
        "mcp": False,
    }
    reader = run_slice.render_brief(**kw, read_mcp="reader")
    assert "read-only precis MCP (get and search)" in reader
    assert "search(kind='python', mode='pattern', q='<name>')" in reader
    assert "get(kind='python', id='main::<qualname>', view='source')" in reader
    assert "wt-slice-td<id>::<qualname>" in reader
    assert "get(kind='todo', id='td<id>', view='comments')" in reader
    assert "Nothing else is served and nothing is writable" in reader
    assert "no precis MCP. Do not try" not in reader
    triage = run_slice.render_brief(**kw, read_mcp="triage")
    assert "no precis MCP. Do not try" not in triage
    para = next(ln for ln in triage.splitlines() if "precis MCP" in ln)
    # The write-set wording never says the lane is read-only or that
    # nothing is writable (the shared close text's "read-only .git" is
    # a different claim).
    assert "read-only" not in para and "nothing is writable" not in para
    assert "put(" in para and "tag" in para
    assert "STATUS:" in para and "claimed-by:" in para
    assert "rel='fixes'" in para or 'rel="fixes"' in para


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


def test_poll_brief_node_clean_exit_without_a_commit_succeeds(
    seams: dict[str, Any],
) -> None:
    """jo570327 / td570325 (gr570534): a brief-writer's work is the
    ``brief:`` comment it posts through the MCP — the branch is meant to
    stay empty. ``task_class`` off ``code`` makes a clean exit with no
    commit the node's success: job succeeded, todo done, no attempt."""
    store = _Store()
    store.add_todo(
        42,
        "Write the builder brief",
        meta={"op": "run_slice", "task_class": "brief"},
    )
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        0,
        json.dumps({"is_error": False, "result": "Brief posted on td43."}),
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    tags = store.tag_strings(42)
    assert "STATUS:done" in tags
    assert not any(
        t.startswith("attempt:") or t.startswith("claimed-by:") for t in tags
    )
    assert "verdict:moot" not in tags
    assert seams["lands"] == []
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "non-code" in summary or "task_class" in summary


def test_poll_brief_node_with_a_commit_still_lands(seams: dict[str, Any]) -> None:
    """A spec test may ride the branch: commits still mint the land."""
    store = _Store()
    store.add_todo(
        42,
        "Write the builder brief",
        meta={"op": "run_slice", "task_class": "brief"},
    )
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 test: spec for the fix (td42)\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "done"}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["lands"] == [(42, Path(handle["worktree"]), "claude-sub")]


def test_poll_brief_role_generator_needs_no_children(
    seams: dict[str, Any],
) -> None:
    """gr570534: a ``generate.role`` of ``brief`` is a document role like
    reviewer/analyst — a clean childless exit is its success."""
    store = _Store()
    store.add_todo(42, "Draft it", meta={"generate": {"role": "brief", "from": "gr1"}})
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Drafted."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"


def test_poll_commits_the_tree_a_sandboxed_session_left(seams: dict[str, Any]) -> None:
    """gr564261 / jo565335: codex under workspace-write cannot write .git, so
    it leaves the changes and scratch/commit-msg.txt; the worker commits."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    (tree / "scratch" / "commit-msg.txt").write_text(
        "fix(se): one drc solve per block (td42)\n\nMemo the solve per pass.\n",
        encoding="utf-8",
    )
    seams["porcelain"] = " M a.py\n M b.py\n"
    seams["staged"] = True
    seams["committed_ahead"] = "def5678 fix(se): one drc solve per block (td42)\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Could not commit."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    commits = [a for a, _cwd in seams["git"] if a[:1] == ["commit"]]
    assert len(commits) == 1 and commits[0][:3] == ["commit", "--quiet", "-F"]
    # Committed from a normalised copy outside the tree, not the raw file.
    assert commits[0][3] != str(tree / "scratch" / "commit-msg.txt")
    assert not Path(commits[0][3]).exists()
    assert seams["commit_text"] == (
        "fix(se): one drc solve per block (td42)\n\nMemo the solve per pass.\n"
    )
    assert ["add", "-A"] in [a for a, _cwd in seams["git"]]
    assert seams["lands"] == [(42, tree, "claude-sub")]
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "Commit: worker committed the session's working tree" in summary
    assert "1 commit(s) ahead of origin/main" in summary


def test_poll_commits_a_bare_subject_normalised(seams: dict[str, Any]) -> None:
    """BUILDER on f039f543b: the rewritten subject must be what git gets,
    not the session's raw file."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    (tree / "scratch" / "commit-msg.txt").write_text(
        "Implemented the four reported fixes\n\nDetails.\n", encoding="utf-8"
    )
    seams["porcelain"] = " M a.py\n"
    seams["staged"] = True
    seams["committed_ahead"] = "def5678 fix(slice): td42 session commit\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Could not commit."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["commit_text"] == (
        "fix(slice): td42 session commit\nImplemented the four reported fixes\n\nDetails.\n"
    )
    assert seams["lands"] == [(42, tree, "claude-sub")]


def test_poll_worker_commit_mints_a_checked_land(seams: dict[str, Any]) -> None:
    """gr573563: the worker committing the tree a sandboxed session left
    means the builder could not have run the gate — the land mints
    ``checked``."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    tree = Path(handle["worktree"])
    (tree / "scratch" / "commit-msg.txt").write_text(
        "fix(se): one drc solve per block (td42)\n", encoding="utf-8"
    )
    seams["porcelain"] = " M a.py\n"
    seams["staged"] = True
    seams["committed_ahead"] = "def5678 fix(se): one drc solve per block (td42)\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Could not commit."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["lands"] == [(42, tree, "claude-sub")]
    assert seams["land_checked"] == [True]


def test_poll_tests_not_run_marker_mints_a_checked_land(
    seams: dict[str, Any],
) -> None:
    """gr573563: a final message ending ``tests: NOT RUN — <why>`` (the
    marker the brief asks for) mints the land checked even though the
    builder committed its own branch."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): the fix (td42)\n"
    result = "Done.\ntests: NOT RUN — scripts/test cannot reach docker"
    _finish(handle, 0, json.dumps({"is_error": False, "result": result}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["lands"] == [(42, Path(handle["worktree"]), "claude-sub")]
    assert seams["land_checked"] == [True]


def test_poll_tests_ran_marker_lands_quick(seams: dict[str, Any]) -> None:
    """``tests: ran`` (and no worker commit) lands ``--quick`` — the only
    marker that is a receipt (Reto on gr573555)."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): the fix (td42)\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Done.\ntests: ran"}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["land_checked"] == [False]


def test_poll_no_tests_marker_mints_a_checked_land(seams: dict[str, Any]) -> None:
    """td573580 review (Reto on gr573555): a hand-off with no ``tests:``
    marker is no receipt — the land mints checked, not quick. Covers the
    pinned-codex build that omits the marker."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): the fix (td42)\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Done."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["lands"] == [(42, Path(handle["worktree"]), "claude-sub")]
    assert seams["land_checked"] == [True]


def test_poll_tests_red_marker_mints_a_checked_land(seams: dict[str, Any]) -> None:
    """A ``tests: red`` marker — the gate ran and failed — is no green
    receipt either: the land mints checked."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["ahead"] = "abc1234 fix(x): the fix (td42)\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Done.\ntests: red"}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["lands"] == [(42, Path(handle["worktree"]), "claude-sub")]
    assert seams["land_checked"] == [True]


def test_poll_without_a_message_file_commits_nothing(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["porcelain"] = " M a.py\n"
    seams["staged"] = True
    _finish(handle, 0, json.dumps({"is_error": False, "result": "Forgot the message."}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert not [a for a, _cwd in seams["git"] if a[:1] in (["add"], ["commit"])]


_QUOTA_RESULT = {
    "type": "result",
    "is_error": True,
    "result": "You've hit your weekly limit · resets 11am (UTC)",
    "api_error_status": 429,
    "api_error": "usage_limit_reached",
}


def _codex_and_sub() -> tuple[Lane, Lane]:
    codex = Lane(
        "codex", "codex_exec", "codex", 1, frozenset({"code"}), vendor="openai"
    )
    sub = Lane(
        "claude-sub",
        "claude_p",
        "sub",
        2,
        frozenset({ANY_CLASS}),
        vendor="anthropic",
        models=("claude-test-model",),
        model_prefix="claude-",
    )
    return codex, sub


def test_poll_quota_window_parks_without_an_attempt_and_switches_vendor(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr565359 / jo565344: a claude-sub 429 is the lane's window, not the
    task's verdict: no attempt, lease back, relaunch now on codex."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    assert ctx.meta_set["lane"] == "claude-sub"
    codex, sub = _codex_and_sub()
    use_lanes(monkeypatch, codex, sub)
    _finish(handle, 1, json.dumps(_QUOTA_RESULT) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    assert pctx.status is None and pctx.failure is None
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags
    assert not any(
        t.startswith("attempt:") or t.startswith("claimed-by:") for t in tags
    )
    assert "pid" not in handle and handle["outage"] == "quota"
    assert handle["not_before"] <= datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert pctx.meta_set["exclude_vendor"] == "anthropic"
    assert pctx.meta_set["compute_handle"] is handle
    assert pctx.meta_set["compute_handle_released"] is False
    [event] = [t for k, t in pctx.chunks if k == "job_event"]
    assert "no attempt burned" in event and "not anthropic" in event
    assert store.comments(42) == []

    # Due now: the next poll resubmits on the other vendor's lane.
    launches = len(seams["launch"])
    rctx = _poll_ctx(store, ctx)
    rctx.meta["exclude_vendor"] = "anthropic"
    assert run_slice.poll(rctx, handle) is False
    assert len(seams["launch"]) == launches + 1
    argv = seams["launch"][-1][0]
    assert argv[0].endswith("codex") and argv[1] == "exec"
    assert handle["pid"] == 4242 and "not_before" not in handle
    assert rctx.meta_set["lane"] == "codex"
    assert rctx.meta_set["exclude_vendor"] is None
    assert "claimed-by:lane-codex" in store.tag_strings(42)


def test_poll_quota_window_with_no_other_lane_parks_until_the_reset(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, json.dumps(_QUOTA_RESULT) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    due = datetime.strptime(handle["not_before"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    assert due > datetime.now(UTC)  # the parsed 11am (UTC) reset, ahead of now
    assert pctx.meta_set["exclude_vendor"] is None
    assert "STATUS:open" in store.tag_strings(42)
    launches = len(seams["launch"])
    assert run_slice.poll(_poll_ctx(store, ctx), handle) is False
    assert len(seams["launch"]) == launches  # still parked


def test_poll_quota_window_leaves_a_ladder_pin_for_another_vendor(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """jo565689: the retry ladder pinned claude-sub (lane + model in the
    job's params only); the weekly 429 then parked until the reset although
    codex was free. A ladder pin is the dispatcher's: the park leaves it and
    the relaunch picks another vendor's lane, the rung's model dropped."""
    codex, sub = _codex_and_sub()
    use_lanes(monkeypatch, codex, sub)
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams, lane="claude-sub", model="claude-test-model")
    assert handle is not None and ctx.meta_set["lane"] == "claude-sub"
    _finish(handle, 1, json.dumps(_QUOTA_RESULT) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    assert pctx.meta_set["exclude_vendor"] == "anthropic"
    assert handle["not_before"] <= datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert handle["outage_parks"] == 1

    launches = len(seams["launch"])
    rctx = _poll_ctx(store, ctx)
    rctx.meta["exclude_vendor"] = "anthropic"
    assert run_slice.poll(rctx, handle) is False
    assert len(seams["launch"]) == launches + 1
    argv = seams["launch"][-1][0]
    assert argv[0].endswith("codex") and argv[1] == "exec"
    assert "-m" not in argv  # the rung's claude model went with its lane
    assert rctx.meta_set["lane"] == "codex"
    assert handle["outage_parks"] == 1  # the counter survives the relaunch
    assert any("picking another vendor's lane" in t for k, t in rctx.chunks)


def test_poll_quota_window_honours_the_todo_s_own_lane_pin(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, sub = _codex_and_sub()
    use_lanes(monkeypatch, codex, sub)
    store = _Store()
    store.add_todo(
        42, "t", meta={"op": {"type": "run_slice", "params": {"lane": "claude-sub"}}}
    )
    ctx, handle = _submit(store, seams, lane="claude-sub")
    _finish(handle, 1, json.dumps(_QUOTA_RESULT) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is False
    assert pctx.meta_set["exclude_vendor"] is None
    due = datetime.strptime(handle["not_before"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    assert due > datetime.now(UTC)  # waits for the pinned lane's reset


def test_poll_quota_window_past_the_park_cap_is_a_failed_attempt(
    seams: dict[str, Any],
) -> None:
    """BUILDER on 5ce9e5d69: a park burns no attempt, so a lane that keeps
    hitting its window could re-park without limit. Past the cap the window
    counts as a failed attempt, so the retry cap and the ask still fire."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    handle["outage_parks"] = run_slice.OUTAGE_PARK_CAP
    _finish(handle, 1, json.dumps(_QUOTA_RESULT) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    assert any("counting it as a failed attempt" in t for k, t in pctx.chunks)


def test_poll_plain_error_is_still_a_failed_attempt(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, json.dumps({"is_error": True, "result": "Tool crashed."}) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)


def test_submit_skips_the_excluded_vendor(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, sub = _codex_and_sub()
    use_lanes(monkeypatch, codex, sub)
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42}, exclude_vendor="openai")
    handle = run_slice.submit(ctx, None)
    assert handle is not None and ctx.meta_set["lane"] == "claude-sub"
    use_lanes(
        monkeypatch,
        sub,
    )
    store.add_todo(43, "t")
    ctx = _Ctx(store, {"todo_id": 43}, exclude_vendor="anthropic")
    assert run_slice.submit(ctx, None) is None
    assert ctx.failure is not None and "no lane free" in ctx.failure
    assert not any(t.startswith("attempt:") for t in store.tag_strings(43))


def test_commit_message_demotes_a_bare_subject(tmp_path: Path) -> None:
    (tmp_path / "scratch").mkdir()
    msg = tmp_path / "scratch" / "commit-msg.txt"
    msg.write_text("Implemented the four fixes\nmore\n", encoding="utf-8")
    out = run_slice._commit_message(tmp_path, 42)
    assert out == "fix(slice): td42 session commit\nImplemented the four fixes\nmore\n"
    msg.write_text("feat(x): " + "y" * 90 + "\n", encoding="utf-8")
    out = run_slice._commit_message(tmp_path, 42)
    assert out is not None
    assert len(out.splitlines()[0].encode("utf-8")) <= 72
    assert out.startswith("feat(x): yyy")
    msg.write_text("\n\n", encoding="utf-8")
    assert run_slice._commit_message(tmp_path, 42) is None
    assert run_slice._commit_message(tmp_path / "missing", 42) is None


def test_poll_task_validated_verdict_closes_it_as_done(seams: dict[str, Any]) -> None:
    """gr561132: a task that checked and found nothing to change (a validation
    that passed) says `validated:` first; that is a success with no land, not
    a failed attempt (td561139 and td561151 each hit the cap this way)."""
    store = _Store()
    store.add_todo(42, "Validate the fix")
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        0,
        json.dumps(
            {"is_error": False, "result": "validated: 715c352ca holds, 135 tests pass."}
        ),
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    tags = store.tag_strings(42)
    assert "STATUS:done" in tags
    assert not any(
        t.startswith("claimed-by:") or t.startswith("attempt:") for t in tags
    )
    assert "verdict:moot" not in tags
    assert seams["lands"] == []
    assert any(c.startswith("handoff: validated:") for c in store.comments(42))


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


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("moot: gr1 is already done", ("moot", "gr1 is already done")),
        (
            "\n  No-Op: section is thick enough.\n",
            ("no-op", "section is thick enough."),
        ),
        ("Minted td1.\nmoot: late", None),
        ("moot:", None),
        ("Validated: the fix holds.", ("validated", "the fix holds.")),
        ("BLOCKED: MCP 401.", None),
        ("", None),
    ],
)
def test_generator_verdict_parse(text: str, want: tuple[str, str] | None) -> None:
    from precis.plans.nodes import generator_verdict

    assert generator_verdict(text) == want


def test_poll_generator_noop_verdict_closes_it_as_done(seams: dict[str, Any]) -> None:
    """gr548813: a generator that says `no-op:` first minted nothing on
    purpose. Success, claim dropped, todo done, plan goes on (no moot tag)."""
    store = _Store()
    store.add_todo(
        42, "Spec it", meta={"generate": {"role": "spec-writer", "from": "x"}}
    )
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        0,
        json.dumps(
            {"is_error": False, "result": "no-op: the section is already thick."}
        ),
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    tags = store.tag_strings(42)
    assert "STATUS:done" in tags
    assert not any(
        t.startswith("claimed-by:") or t.startswith("attempt:") for t in tags
    )
    assert "verdict:moot" not in tags
    assert seams["lands"] == []


def test_poll_generator_moot_verdict_tags_the_plan_close(seams: dict[str, Any]) -> None:
    """jo548670: triage found the gripe already resolved. `moot:` closes the
    generator and leaves the tick's tag so it closes the plan."""
    store = _Store()
    store.add_todo(
        42, "Triage gr1", meta={"generate": {"role": "triage", "from": "gr1"}}
    )
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        0,
        json.dumps({"is_error": False, "result": "moot: gr1 is STATUS:done."}),
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    tags = store.tag_strings(42)
    assert {"STATUS:done", "verdict:moot"} <= tags
    assert any(c.startswith("handoff: moot:") for c in store.comments(42))


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


@pytest.mark.parametrize(
    ("role", "from_", "result"),
    [
        ("reviewer", "td7", "review: LAND"),
        ("analyst", "pr:9", "analysis: one file, low risk"),
    ],
)
def test_poll_document_role_clean_exit_without_a_commit_succeeds(
    seams: dict[str, Any], role: str, from_: str, result: str
) -> None:
    """gr570534 / review 1: a reviewer/analyst generator's deliverable is
    the one comment it posts — never a commit, never children. A clean
    exit on an empty branch is the node's success: job succeeded, no
    land minted, no attempt burned, the verdict handed off for the
    dispatcher to close the todo on."""
    store = _Store()
    store.add_todo(42, "Judge it", meta={"generate": {"role": role, "from": from_}})
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, json.dumps({"is_error": False, "result": result}))
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert seams["lands"] == []
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))
    assert any(f"handoff: {result}" in c for c in store.comments(42))


def test_poll_failed_land_mint_releases_the_lease(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_kw: Any) -> int:
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


def test_mint_land_checked_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr573563: ``checked=True`` puts ``params["checked"] = True`` on the
    minted land — what ``land_slice.ship_flag`` reads as ``--checked``;
    the default adds no key."""
    import precis.workers._mint as mint_mod
    from precis.workers.job_types import land_slice

    minted: list[dict[str, Any]] = []

    def fake_once(store: Any, **kw: Any) -> tuple[int, bool]:
        minted.append(kw)
        return 555, False

    monkeypatch.setattr(mint_mod, "mint_job_once", fake_once)
    store = _Store()
    monkeypatch.setattr(store, "tx", lambda: _Tx(), raising=False)
    ctx = _Ctx(store, {"todo_id": 42})
    run_slice._mint_land(ctx, 42, Path("/t/slice-td42"), "claude-sub", checked=True)
    params = minted[0]["params"]
    assert params["checked"] is True
    assert land_slice.ship_flag(params) == "--checked"
    minted.clear()
    run_slice._mint_land(ctx, 42, Path("/t/slice-td42"), "claude-sub")
    assert "checked" not in minted[0]["params"]


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


def test_poll_human_only_hit_stamps_the_failed_job(seams: dict[str, Any]) -> None:
    """td584381 S1a: a deny hit on a HUMAN_ONLY path stamps
    ``meta.human_only_hit`` on the failed job — the durable mark
    plan_dispatch reads to ask at once instead of climbing the ladder."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["diff"] = "deploy/inventory/prod.yml\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "edited it."}) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set[run_slice.HUMAN_ONLY_KEY] == ["deploy/inventory/prod.yml"]


def test_poll_factory_deny_hit_leaves_no_human_stamp(seams: dict[str, Any]) -> None:
    """A deny hit on a factory-deny (engine) path only is an ordinary
    failure — no human stamp — so it climbs the ladder (td584381 S1a)."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    seams["diff"] = "scripts/lib/gate.sh\n"
    _finish(handle, 0, json.dumps({"is_error": False, "result": "edited it."}) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert run_slice.HUMAN_ONLY_KEY not in pctx.meta_set


def test_poll_no_commit_exit_stamps_the_job(seams: dict[str, Any]) -> None:
    """td584381 S1a F4: a clean exit on a code node that left no commit
    stamps ``meta.no_commit`` on the failed job — the durable mark the
    tick reads to send the node straight to rescue; a ``no-op:``/
    ``validated:`` verdict exit and an ordinary red exit stamp nothing
    (since S1d a ``moot:`` claim on a code node must verify first — an
    unverified one stamps ``no_commit`` like any uncommitted exit)."""
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        0,
        json.dumps({"is_error": False, "result": "Read the code; nothing to change."})
        + "\n",
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set[run_slice.NO_COMMIT_KEY] is True

    # A verdict first line closes the node done — a success, no stamp.
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        0,
        json.dumps({"is_error": False, "result": "no-op: docs already match"}) + "\n",
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert run_slice.NO_COMMIT_KEY not in pctx.meta_set

    # An ordinary red exit (a real failure, commits or not) stamps nothing.
    store = _Store()
    store.add_todo(42, "t")
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, "boom\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert run_slice.NO_COMMIT_KEY not in pctx.meta_set


def _mooted_store() -> _Store:
    """A code todo inside ``plan:50`` whose plan gripe is gr7 — the shape
    ``_verified_moot`` resolves the plan root, the gripe and the td-chain
    from (review round 1)."""
    store = _Store()
    store.add_todo(50, "Fix gr7 plan")
    store.add_todo(42, "Fix gr7", parent_id=50, tags=("plan:50",))
    store.add_todo(43, "Review the fix", parent_id=50, tags=("plan:50",))
    # The plan gripe's report names its test file; it must exist on main.
    store.chunks.rows[7] = [_Chunk(0, "repro in tests/test_widget.py", "gripe")]
    return store


def _pin_plan_gripe(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.workers.job_types import land_slice

    monkeypatch.setattr(land_slice, "_plan_gripe", lambda _s, _t: 7)


def _moot_result(sha: str = "f628e681f", gripe: str = "gr7") -> str:
    return (
        json.dumps(
            {
                "is_error": False,
                "result": f"Checked main first.\nmoot: {gripe} already fixed by {sha}\n"
                "Nothing to land.",
            }
        )
        + "\n"
    )


def test_poll_verified_moot_closes_and_stamps_the_sha(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d + review round 1: a code task's ``moot:`` line
    verifies when a best-effort ``git fetch origin main`` ran, the named
    sha is an ancestor of ``origin/main`` AND the sha's own commit
    message names the plan's gripe — the todo closes done tagged
    ``verdict:moot``, the job carries ``meta.moot_verified``. No land
    minted, no attempt burned."""
    _pin_plan_gripe(monkeypatch)
    store = _mooted_store()
    seams["show_msg"] = "fix(plan): close the moot chain (gr7)\n"
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, _moot_result())
    seams["git"].clear()
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    tags = store.tag_strings(42)
    assert {"STATUS:done", "verdict:moot"} <= tags
    assert not any(
        t.startswith("attempt:") or t.startswith("claimed-by:") for t in tags
    )
    mv = pctx.meta_set[run_slice.MOOT_VERIFIED_KEY]
    assert mv["sha"] == "f628e681f"
    assert mv["line"].startswith("moot: gr7")
    assert seams["lands"] == []
    git_cmds = [args for args, _cwd in seams["git"]]
    assert ["fetch", "origin", "main"] in git_cmds
    assert ["merge-base", "--is-ancestor", "f628e681f", "origin/main"] in git_cmds
    assert ["show", "-s", "--format=%B", "f628e681f"] in git_cmds
    assert ["cat-file", "-e", "origin/main:tests/test_widget.py"] in git_cmds


@pytest.mark.parametrize(
    ("result", "show_msg", "ancestor_rc", "cat_rc", "want"),
    [
        # The sha's message names a ``td`` of the plan's chain instead of
        # the gripe handle — the factory's subjects carry ``(td<build>)``.
        ("moot: already fixed by f628e681f", "fix(x): y (td42)", 0, 0, True),
        ("moot: already fixed by f628e681f", "fix(x): y (td50)", 0, 0, True),
        ("moot: already fixed by f628e681f", "fix(x): y (td43)", 0, 0, True),
        # A 7-char sha verifies.
        ("moot: gr7 already fixed by abc1234", "fix(x): y (gr7)", 0, 0, True),
        # An ancestor sha whose message names neither handle — verified
        # before review round 1, unverified now.
        ("moot: gr7 already fixed by f628e681f", "fix(x): y (td999)", 0, 0, False),
        # A moot line pinned to a DIFFERENT gripe is rejected even when
        # the sha's message names the plan gripe.
        ("moot: gr99 already fixed by f628e681f", "fix(x): y (gr7)", 0, 0, False),
        # The moot line names no sha at all.
        ("moot: gr7 looks already fixed", "fix(x): y (gr7)", 0, 0, False),
        # An uppercase sha is no candidate — fails closed.
        ("moot: gr7 already fixed by F628E681F", "fix(x): y (gr7)", 0, 0, False),
        # The named sha is no ancestor of origin/main.
        ("moot: gr7 already fixed by f628e681f", "fix(x): y (gr7)", 1, 0, False),
        # The named test file is missing on main (extra gate).
        ("moot: gr7 already fixed by f628e681f", "fix(x): y (gr7)", 0, 1, False),
        # ``gr`` inside another word is no gripe token — the line names
        # no ``gr<N>``, so the message check alone verifies.
        ("moot: group7 already fixed by f628e681f", "fix(x): y (gr7)", 0, 0, True),
    ],
    ids=[
        "td-build",
        "td-root",
        "td-review",
        "short-sha",
        "message-names-neither",
        "different-gripe",
        "no-sha",
        "uppercase-sha",
        "not-an-ancestor",
        "named-test-missing",
        "gr-in-word",
    ],
)
def test_poll_moot_verifies_on_ancestor_and_own_message(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    result: str,
    show_msg: str,
    ancestor_rc: int,
    cat_rc: int,
    want: bool,
) -> None:
    """td584381 S1d review round 1: verification is ancestor AND the
    sha's own message naming the plan's gripe (its ``gr`` or a chain
    ``td``); everything else is the ordinary no-commit failure
    (``meta.no_commit``, ``attempt:1``, straight to rescue)."""
    _pin_plan_gripe(monkeypatch)
    store = _mooted_store()
    seams["show_msg"] = show_msg + "\n"
    seams["ancestor_rc"] = ancestor_rc
    seams["cat_rc"] = cat_rc
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, json.dumps({"is_error": False, "result": result}) + "\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    if want:
        assert pctx.status == "succeeded"
        assert run_slice.MOOT_VERIFIED_KEY in pctx.meta_set
        assert "verdict:moot" in store.tag_strings(42)
    else:
        assert pctx.status == "failed"
        assert pctx.meta_set[run_slice.NO_COMMIT_KEY] is True
        assert run_slice.MOOT_VERIFIED_KEY not in pctx.meta_set
        assert "verdict:moot" not in store.tag_strings(42)
        assert "attempt:1" in store.tag_strings(42)


@pytest.mark.parametrize("how", ["rc", "raise"], ids=["fetch-rc1", "fetch-timeout"])
def test_poll_verified_moot_survives_a_failed_fetch(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """td584381 S1d review round 1: the fetch is best-effort — a nonzero
    rc or a ``TimeoutExpired`` only means the ancestor+message checks
    run on whatever ``origin/main`` ref exists."""
    _pin_plan_gripe(monkeypatch)
    store = _mooted_store()
    seams["show_msg"] = "fix(x): y (gr7)\n"
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, _moot_result())
    # Submit's own fetch already ran; only the poll's is broken here.
    if how == "rc":
        seams["fetch_rc"] = 1
    else:
        seams["raise_prefix"] = ["fetch", "origin", "main"]
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert pctx.meta_set[run_slice.MOOT_VERIFIED_KEY]["sha"] == "f628e681f"


def test_poll_moot_fails_closed_on_git_error(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 1: a git error on the ancestor or the
    message check is unverified — the no-commit exit, never a close."""
    _pin_plan_gripe(monkeypatch)
    store = _mooted_store()
    seams["show_rc"] = 1
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, _moot_result())
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set[run_slice.NO_COMMIT_KEY] is True
    assert "verdict:moot" not in store.tag_strings(42)

    # A ``git`` call that raises is likewise no verification.
    _pin_plan_gripe(monkeypatch)
    store = _mooted_store()
    seams["show_rc"] = 0
    seams["show_msg"] = "fix(x): y (gr7)\n"
    seams["raise_prefix"] = ["merge-base", "--is-ancestor"]
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, _moot_result())
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "verdict:moot" not in store.tag_strings(42)


def test_poll_moot_cat_file_raise_fails_closed(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 2: a ``cat-file -e`` that raises
    (``TimeoutExpired``/``OSError``) on the named-test gate is
    UNVERIFIED — the S1a no-commit path — never a crash out of poll."""
    _pin_plan_gripe(monkeypatch)
    store = _mooted_store()
    seams["show_msg"] = "fix(x): y (gr7)\n"
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, _moot_result())
    seams["raise_prefix"] = ["cat-file", "-e"]
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set[run_slice.NO_COMMIT_KEY] is True
    assert run_slice.MOOT_VERIFIED_KEY not in pctx.meta_set
    assert "verdict:moot" not in store.tag_strings(42)


def test_poll_moot_plan_gripe_raise_fails_closed(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 2: a raise out of
    ``land_slice._plan_gripe`` (the DB read) is UNVERIFIED — the
    no-commit path — never a crash out of poll."""
    from precis.workers.job_types import land_slice

    def boom(_s: Any, _t: str) -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr(land_slice, "_plan_gripe", boom)
    store = _mooted_store()
    seams["show_msg"] = "fix(x): y (gr7)\n"
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, _moot_result())
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set[run_slice.NO_COMMIT_KEY] is True
    assert "verdict:moot" not in store.tag_strings(42)


def test_poll_moot_without_a_plan_fails_closed(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td584381 S1d review round 1: no ``plan:`` tag (a standalone code
    todo) or no plan gripe means no handle to verify against — the
    no-commit exit, never a close."""
    _pin_plan_gripe(monkeypatch)
    store = _Store()
    store.add_todo(42, "Fix gr7")
    seams["show_msg"] = "fix(x): y (gr7)\n"
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, _moot_result())
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "verdict:moot" not in store.tag_strings(42)

    from precis.workers.job_types import land_slice

    monkeypatch.setattr(land_slice, "_plan_gripe", lambda _s, _t: None)
    store = _mooted_store()
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, _moot_result())
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "verdict:moot" not in store.tag_strings(42)


@pytest.mark.parametrize("task_class", ["review", "diagnose"])
def test_poll_document_task_class_no_commit_succeeds(
    seams: dict[str, Any], task_class: str
) -> None:
    """td584381 S1d: a ``task_class`` off ``code`` (a reviewer's read, a
    diagnose) delivers a document, not a branch — a clean exit with no
    commit is the node's success: succeeded, no ``no_commit`` stamp."""
    store = _Store()
    store.add_todo(42, "t", meta={"task_class": task_class})
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        0,
        json.dumps({"is_error": False, "result": "Read it; notes in the handoff."})
        + "\n",
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert run_slice.NO_COMMIT_KEY not in pctx.meta_set
    assert "STATUS:done" in store.tag_strings(42)


def test_poll_document_task_class_moot_keeps_trust(seams: dict[str, Any]) -> None:
    """td584381 S1d: verification is the CODE node's gate — a
    ``task_class`` node's first-line ``moot:`` keeps today's trust and
    closes tagged, no sha check."""
    store = _Store()
    store.add_todo(42, "t", meta={"task_class": "validate"})
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        0,
        json.dumps({"is_error": False, "result": "moot: gr1 already fixed"}) + "\n",
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    tags = store.tag_strings(42)
    assert {"STATUS:done", "verdict:moot"} <= tags
    assert run_slice.MOOT_VERIFIED_KEY not in pctx.meta_set


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


def test_human_only_paths_are_denied_case_insensitively() -> None:
    """``HUMAN_ONLY_PATHS`` is never relaxable (td574937 E1): it holds
    under the full deny list too, matched case-insensitively — the slice
    hosts' filesystems fold case."""
    hits = run_slice.deny_violations(
        ["Deploy/Inventory/Hosts.yml", "CLAUDE.MD", "src/precis/foo.py"],
        lanes.FACTORY_DENY_WRITE_PATHS,
    )
    assert hits == ["CLAUDE.MD", "Deploy/Inventory/Hosts.yml"]


def test_the_briefs_deny_list_names_human_only_in_off_mode() -> None:
    """Off mode resolves FACTORY ∪ HUMAN_ONLY (td574960): the brief's
    deny section names the same set the post-check enforces —
    ``AGENTS.md`` included."""
    brief = run_slice.render_brief(
        todo_id=42,
        title="t",
        body="",
        latest=None,
        lane="devin",
        model=None,
        effort="medium",
    )
    assert "- `AGENTS.md`" in brief
    assert "- `deploy/inventory*/**`" in brief


def test_every_seed_lane_carries_the_factory_deny_paths() -> None:
    for lane in SEED_LANES:
        assert lane.deny_write_paths == lanes.STRICT_DENY
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
        "src/precis/workers/job_types/ci_red_digest.py",
        "src/precis/workers/job_types/release_poll.py",
        "src/precis/workers/job_types/release_deploy.py",
        "src/precis/workers/job_types/release_confirm.py",
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
            "src/precis/workers/job_types/ci_red_digest.py",
            "src/precis/workers/job_types/release_poll.py",
            "src/precis/workers/job_types/release_deploy.py",
            "src/precis/workers/job_types/release_confirm.py",
            "src/precis/workers/job_types/land_slice.py",
            "src/precis/workers/job_types/fix_gripe.py",
        ],
        lanes.FACTORY_DENY_WRITE_PATHS,
    )
    assert hits == [
        "scripts/release",
        "src/precis/workers/job_types/ci_red_digest.py",
        "src/precis/workers/job_types/ci_verdict_poll.py",
        "src/precis/workers/job_types/land_slice.py",
        "src/precis/workers/job_types/release_confirm.py",
        "src/precis/workers/job_types/release_cut.py",
        "src/precis/workers/job_types/release_deploy.py",
        "src/precis/workers/job_types/release_poll.py",
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


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Done.\ntests: ran", True),
        ("tests: ran — all green", True),
        ("Done.\ntests: NOT RUN — scripts/test cannot reach docker", False),
        ("tests: not run — no docker socket", False),
        ("tests: red", False),
        ("tests: FAILED — 2 errors", False),
        # the LAST marker decides — a devin result is the whole transcript
        ("tests: NOT RUN — first try\ntests: ran", True),
        ("tests: ran\ntests: NOT RUN — final gate", False),
        ("tests: ran\ntests: red", False),
        ("no marker at all", False),
        (None, False),
        ("", False),
        ("tests:", False),
        ("the tests: a paragraph mention", False),
        ("unrelated: ran", False),
    ],
)
def test_tests_ran_reads_the_last_marker(text: str | None, want: bool) -> None:
    """gr573563 / Reto on gr573555: ``True`` only when the last ``tests:``
    marker says ``ran`` — the receipt the quick lane takes. No marker,
    ``not run`` or a red run all land checked."""
    assert run_slice.tests_ran(text) is want


def _vendor_lane(
    name: str,
    adapter: str,
    *,
    models: tuple[str, ...] = (),
    default_model: str | None = None,
) -> Lane:
    return Lane(
        name=name,
        adapter=adapter,
        credential_pool=name,
        cap=1,
        task_classes=frozenset({"code"}),
        enabled=True,
        models=models,
        default_model=default_model,
    )


@pytest.mark.parametrize("model_source", ["default", "params", "lane", "env"])
def test_submit_codex_argv_and_model(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model_source: str,
) -> None:
    """gr570533: ``--add-dir`` grants the sandbox the shared git dir (the
    worktree's index/refs, the object store, and the gate queue + slot
    locks ``scripts/test`` takes) and the Docker socket's parent — a
    codex slice can ``git commit`` and run the gate."""
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    monkeypatch.setenv("PRECIS_CODEX_BIN", "/vendor/codex")
    common = tmp_path / "repo.git"
    sock_dir = tmp_path / ".colima"
    sock_dir.mkdir()
    seams["git_common"] = str(common)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{sock_dir}/docker.sock")
    lane = _vendor_lane(
        "codex",
        "codex_exec",
        models=("codex-test",) if model_source == "lane" else (),
        default_model="codex-test" if model_source == "lane" else None,
    )
    use_lanes(
        monkeypatch,
        lane,
    )
    params: dict[str, Any] = {"lane": "codex"}
    if model_source == "params":
        params["model"] = "codex-test"
    elif model_source == "env":
        monkeypatch.setenv("PRECIS_RUN_SLICE_MODEL", "codex-test")
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, **params)
    assert handle is not None and ctx.failure is None
    [(argv, kw)] = seams["launch"]
    tree = Path(handle["worktree"])
    expected: list[str] = [
        "/vendor/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(tree),
        "-o",
        str(tree / "scratch" / "last-message.md"),
        "--add-dir",
        str(common),
        "--add-dir",
        str(sock_dir),
    ]
    # PRECIS_RUN_SLICE_MODEL names a claude id: it never reaches a vendor lane.
    pinned = model_source in ("params", "lane")
    if pinned:
        expected += ["-m", "codex-test"]
    assert argv == expected + ["--sandbox", "workspace-write", "-"]
    assert kw["cwd"] == tree
    assert kw["stdin"] == tree / "scratch" / "brief.md"
    # The socket path must also reach the session's own env.
    assert kw["env"]["DOCKER_HOST"] == f"unix://{sock_dir}/docker.sock"
    assert ctx.meta_set["adapter"] == "codex_exec"
    assert ctx.meta_set["model"] == ("codex-test" if pinned else None)


def test_codex_add_dir_docker_fallback_and_skip(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No DOCKER_HOST: ``~/.colima`` is the socket root when it exists; a
    missing dir grants no phantom root. The git common dir always is."""
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    monkeypatch.setenv("PRECIS_CODEX_BIN", "/vendor/codex")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    common = tmp_path / "repo.git"
    seams["git_common"] = str(common)
    home = tmp_path / "home"
    (home / ".colima").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    use_lanes(
        monkeypatch,
        _vendor_lane("codex", "codex_exec"),
    )
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="codex")
    assert handle is not None and ctx.failure is None
    [(argv, _kw)] = seams["launch"]
    assert argv.count("--add-dir") == 2
    assert str(common) in argv and str(home / ".colima") in argv

    # A missing ~/.colima grants nothing extra.
    seams["launch"].clear()
    (home / ".colima").rename(home / ".colima-away")
    store2 = _Store()
    store2.add_todo(7, "Build")
    ctx2, handle2 = _submit(store2, seams, lane="codex", todo_id=7)
    assert handle2 is not None and ctx2.failure is None
    [(argv2, _kw2)] = seams["launch"]
    assert argv2.count("--add-dir") == 1
    assert str(common) in argv2


@pytest.mark.parametrize("unsafe", ["root", "home", "home-parent"])
def test_codex_add_dir_never_widens_to_an_unsafe_docker_root(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    unsafe: str,
) -> None:
    """Review 1: the Docker socket's parent is never granted when it is
    ``/``, ``$HOME`` or an ancestor of ``$HOME`` — that would widen the
    sandbox toward the worker's whole world. The unsafe root is logged
    and skipped; the git common dir is still granted."""
    home = tmp_path / "home" / "worker"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    socket = {
        "root": Path("/docker.sock"),
        "home": home / "docker.sock",
        "home-parent": home.parent / "docker.sock",
    }[unsafe]
    monkeypatch.setenv("DOCKER_HOST", f"unix://{socket}")
    common = tmp_path / "repo.git"
    seams["git_common"] = str(common)
    with caplog.at_level(logging.WARNING, "precis.workers.job_types.run_slice"):
        roots = run_slice._codex_writable_roots(tmp_path)
    assert roots == (common,)
    assert "docker" in caplog.text.lower()


@pytest.mark.parametrize("model", [None, "swe-2-high", "paid-model"])
def test_submit_devin_model_guard(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
) -> None:
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    monkeypatch.setenv("PRECIS_DEVIN_BIN", "/vendor/devin")
    lane = _vendor_lane("devin", "devin_tree", models=("swe-2-high",))
    use_lanes(
        monkeypatch,
        lane,
    )
    store = _Store()
    store.add_todo(42, "Build")
    params: dict[str, Any] = {"lane": "devin"}
    if model is not None:
        params["model"] = model
    ctx, handle = _submit(store, seams, **params)
    if model == "paid-model":
        assert handle is None and ctx.status == "failed"
        assert ctx.failure is not None and "paid-model" in ctx.failure
        assert seams["launch"] == []
        assert store.tag_strings(42) == {"STATUS:open", "attempt:1"}
        return
    assert handle is not None and ctx.failure is None
    [(argv, kw)] = seams["launch"]
    tree = Path(handle["worktree"])
    expected: list[str] = [
        "/vendor/devin",
        "-p",
        "--prompt-file",
        str(tree / "scratch" / "brief.md"),
        "--permission-mode",
        "dangerous",
        "--respect-workspace-trust",
        "false",
        "--verbosity",
        "1",
    ]
    if model is not None:
        expected += ["--model", model]
    assert argv == expected
    assert kw["stdin"] is None and kw["cwd"] == tree
    assert ctx.meta_set["adapter"] == "devin_tree"


def test_devin_adapter_rejects_explicit_model_without_allowlist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="paid-model"):
        get_adapter("devin_tree").argv(
            model="paid-model",
            effort="high",
            tree=tmp_path,
            brief_path=tmp_path / "brief.md",
            deny_paths=(),
            max_turns=1,
        )


def _devin_lane_submit(
    store: _Store,
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    **params: Any,
) -> tuple[_Ctx, Any]:
    """Submit td42 on a pinned devin lane (the model guard's lane)."""
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL", raising=False)
    monkeypatch.setenv("PRECIS_DEVIN_BIN", "/vendor/devin")
    lane = _vendor_lane("devin", "devin_tree", models=("swe-2-high",))
    use_lanes(
        monkeypatch,
        lane,
    )
    return _submit(store, seams, lane="devin", **params)


def test_devin_submit_seeds_trust_then_launches(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr570688: a devin submit registers the checkout's worktrees dir in
    Devin's trusted workspaces before launch — a headless session cannot
    answer the directory-trust prompt."""
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert handle is not None and ctx.failure is None
    tree = Path(handle["worktree"])
    assert seams["devin"]["trusted"] == [tree.parent]
    [(argv, kw)] = seams["launch"]
    assert argv[0] == "/vendor/devin" and kw["cwd"] == tree


def test_devin_preflight_refuses_a_missing_binary(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr570688: the worker's PATH never carried the devin binary, so the
    lane burned whole sessions just to die at spawn; the refusal names
    the fix (install the CLI or point PRECIS_DEVIN_BIN at it). A host
    problem burns no attempt — the todo reopens clean (review ruling)."""
    seams["devin"]["bin_ok"] = False
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None
    assert "PRECIS_DEVIN_BIN" in ctx.failure and "next:" in ctx.failure
    assert seams["launch"] == [] and seams["devin"]["trusted"] == []
    assert store.tag_strings(42) == {"STATUS:open"}


def test_devin_preflight_refuses_when_not_logged_in(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr570688: `devin auth status` without a login is a dead session at
    first prompt; the refusal names `devin auth login` (the deploy-user
    login is the recorded stopgap, not the architecture)."""
    seams["devin"]["auth"] = _proc("Not logged in\n", rc=1)
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None
    assert "devin auth login" in ctx.failure and "stopgap" in ctx.failure
    assert seams["launch"] == [] and seams["devin"]["trusted"] == []
    assert store.tag_strings(42) == {"STATUS:open"}


def test_devin_preflight_refuses_an_unreadable_auth_status(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auth status that exits 0 but reports no login is not a login:
    'Not logged in' still contains 'logged in'."""
    seams["devin"]["auth"] = _proc("Not logged in\n", rc=0)
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None and "devin auth login" in ctx.failure
    assert seams["launch"] == []


def test_devin_auth_status_maps_a_timeout_to_rc_124(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hanging `devin auth status` becomes rc 124 — the preflight then
    refuses with the login next step instead of blocking the submit."""

    def hang(*args: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kw.get("timeout", 15))

    import types

    stub = types.SimpleNamespace(
        run=hang,
        TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
    )
    monkeypatch.setattr(run_slice, "subprocess", stub)
    proc = run_slice._devin_auth_status("/vendor/devin")
    assert proc.returncode == 124


def test_devin_preflight_refuses_an_auth_status_timeout(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc 124 is not a login: the refusal still names `devin auth login`
    as the next step."""
    seams["devin"]["auth"] = _proc("timed out\n", rc=124)
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None and "devin auth login" in ctx.failure
    assert seams["launch"] == []


def test_host_refusal_stamps_a_lane_backoff(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr571395: a host refusal marks the failed job — ``meta.host_refusal``
    carries the lane and the window's end, so :func:`lanes.lane_backoffs`
    can skip it and ``plan_dispatch`` never counts the job as an attempt."""
    seams["devin"]["bin_ok"] = False
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert handle is None and ctx.status == "failed"
    refusal = ctx.meta_set["host_refusal"]
    assert refusal["lane"] == "devin"
    until = datetime.fromisoformat(str(refusal["until"]))
    assert until > datetime.now(UTC)
    assert str(refusal["why"]).startswith("devin preflight:")


def test_devin_preflight_refuses_when_trust_seeding_fails(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr570688: a trust write that cannot land (a malformed
    trusted_workspaces.json, a missing worktrees dir) must refuse the
    launch with the next step — a host problem, no attempt burned — not
    launch a session that stalls on the trust prompt."""
    seams["devin"]["trust"] = "not updating trusted_workspaces.json: bad json"
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None
    assert "trusted" in ctx.failure and "next:" in ctx.failure
    assert "devin_trust.py" in ctx.failure
    assert seams["launch"] == []
    assert store.tag_strings(42) == {"STATUS:open"}


def test_pinned_lane_in_backoff_parks_until_the_window_ends(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr571395: a lane pinned by ``params.lane`` while backed off parks to
    the stamped window end (not the 10-min lane-full horizon) — the next
    launch on it is the probe, and no attempt is burned. The wake carries
    a per-todo jitter so a fleet of jobs parked on the same refusal does
    not probe the lane in one burst."""
    until = datetime.now(UTC) + timedelta(minutes=25)
    seams["backoff"] = {"devin": until}
    store = _Store()
    store.add_todo(42, "Build")
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    monkeypatch.setenv("PRECIS_DEVIN_BIN", "/vendor/devin")
    use_lanes(
        monkeypatch,
        _vendor_lane("devin", "devin_tree"),
    )
    ctx = _Ctx(store, {"todo_id": 42, "lane": "devin"})
    deferred: list[tuple[Any, str]] = []

    def _defer(wake: Any, note: str) -> bool:
        deferred.append((wake, note))
        return True

    ctx.defer = _defer
    assert run_slice.submit(ctx, None) is None
    assert deferred and until <= deferred[0][0] < until + timedelta(minutes=6)
    assert "devin" in deferred[0][1] and "backed off" in deferred[0][1]
    assert ctx.failure is None and seams["launch"] == []
    assert store.tag_strings(42) == {"STATUS:open"}


def test_empty_pick_names_the_backed_off_lanes(
    seams: dict[str, Any],
) -> None:
    """An unpinned pick with every fit lane backed off says so in the park
    note — the plain lane-full wording would mislead."""
    seams["backoff"] = {"claude-sub": datetime.now(UTC) + timedelta(minutes=30)}
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42})
    deferred: list[tuple[Any, str]] = []

    def _defer(wake: Any, note: str) -> bool:
        deferred.append((wake, note))
        return True

    ctx.defer = _defer
    assert run_slice.submit(ctx, None) is None
    assert deferred and "claude-sub" in deferred[0][1]
    assert "backed off" in deferred[0][1]
    assert ctx.failure is None and seams["launch"] == []


def test_empty_lane_table_parks_as_no_enabled_lane(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account table that derives no lanes parks the pick 'no enabled
    lane' — 'every fit lane at its cap' would mislead."""
    from precis.utils.llm import accounts

    monkeypatch.setattr(accounts, "list_accounts", lambda store: [])
    store = _Store()
    store.add_todo(42, "t")
    ctx = _Ctx(store, {"todo_id": 42})
    deferred: list[tuple[Any, str]] = []

    def _defer(wake: Any, note: str) -> bool:
        deferred.append((wake, note))
        return True

    ctx.defer = _defer
    assert run_slice.submit(ctx, None) is None
    assert deferred and "no enabled lane" in deferred[0][1]
    assert ctx.failure is None and seams["launch"] == []


def test_refusal_raises_one_deduped_alert(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr571395: each refusal raises the lane's fingerprint, so the alert
    dedups to one open row per lane no matter how many todos probe it;
    an alert failure never masks the refusal itself."""
    seams["devin"]["bin_ok"] = False
    raised: list[dict[str, Any]] = []

    def fake_raise(store: Any, **kw: Any) -> tuple[int, bool]:
        raised.append(kw)
        return (1, True)

    monkeypatch.setattr(run_slice, "raise_alert", fake_raise)
    for tid in (42, 43):
        store = _Store()
        store.add_todo(tid, "Build")
        ctx, handle = _devin_lane_submit(store, seams, monkeypatch, todo_id=tid)
        assert handle is None and ctx.status == "failed"
    assert len(raised) == 2
    for kw in raised:
        assert kw["source"] == "run_slice"
        assert kw["fingerprint"] == "lane-host-refusal:devin"
        assert kw["severity"] == "warn"
        assert kw["title"] == "lane devin refused a launch (host)"
        assert "next:" in kw["detail"] and "skipped until" in kw["detail"]

    def boom(store: Any, **kw: Any) -> None:
        raise RuntimeError("alert store down")

    monkeypatch.setattr(run_slice, "raise_alert", boom)
    store3 = _Store()
    store3.add_todo(44, "Build")
    ctx3, handle3 = _devin_lane_submit(store3, seams, monkeypatch, todo_id=44)
    assert handle3 is None and ctx3.status == "failed"
    assert ctx3.failure is not None and "devin preflight" in ctx3.failure


def test_launch_after_backoff_resolves_the_alert(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful launch on a lane that once refused is the probe that
    clears the host: it resolves the open alert — and a resolve failure
    never masks the launch."""
    resolved: list[dict[str, Any]] = []

    def fake_resolve(store: Any, **kw: Any) -> bool:
        resolved.append(kw)
        return True

    monkeypatch.setattr(run_slice, "resolve_alert_by_fingerprint", fake_resolve)
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _devin_lane_submit(store, seams, monkeypatch)
    assert handle is not None and ctx.failure is None
    assert resolved == [
        {
            "source": "run_slice",
            "fingerprint": "lane-host-refusal:devin",
            "resolved_by": "run_slice launch",
        }
    ]

    def boom(store: Any, **kw: Any) -> None:
        raise RuntimeError("alert store down")

    monkeypatch.setattr(run_slice, "resolve_alert_by_fingerprint", boom)
    store2 = _Store()
    store2.add_todo(43, "Build")
    ctx2, handle2 = _devin_lane_submit(store2, seams, monkeypatch, todo_id=43)
    assert handle2 is not None and ctx2.failure is None


@pytest.mark.parametrize(
    "name,adapter",
    [("codex", "codex_exec"), ("devin", "devin_tree")],
)
@pytest.mark.parametrize("inherited_tokens", [False, True])
def test_non_mcp_submit_never_uses_credentials_or_preflight(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    adapter: str,
    inherited_tokens: bool,
) -> None:
    from precis import secrets as _secrets
    from precis.utils import claude_oauth

    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    use_lanes(
        monkeypatch,
        _vendor_lane(name, adapter),
    )
    for key in ("PRECIS_MCP_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        if inherited_tokens:
            monkeypatch.setenv(key, "must-not-reach-vendor")
        else:
            monkeypatch.delenv(key, raising=False)
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> None:
        calls.append("credential or preflight")
        raise AssertionError("non-MCP lane touched credentials or preflight")

    monkeypatch.setattr(run_slice, "_mint_slice_token", forbidden)
    monkeypatch.setattr(run_slice, "_preflight_mcp", forbidden)
    monkeypatch.setattr(claude_oauth, "ensure_oauth_token", forbidden)
    monkeypatch.setattr(_secrets, "mounted_secret", forbidden)
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane=name)
    assert handle is not None and ctx.failure is None
    assert calls == []
    [(_argv, kw)] = seams["launch"]
    assert "PRECIS_MCP_TOKEN" not in kw["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in kw["env"]
    brief = (Path(handle["worktree"]) / "scratch" / "brief.md").read_text(
        encoding="utf-8"
    )
    paragraph = (
        "This lane has no precis MCP. Do not try to reach it. "
        "Everything you want recorded goes in your final message."
    )
    assert paragraph in brief
    assert (
        brief.index(f"lane `{name}`") < brief.index(paragraph) < brief.index("## Paths")
    )
    assert "Your final message is posted on the todo as the `handoff:` comment" in brief


def test_submit_unknown_adapter_fails_without_claiming(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account on an adapter the builder cannot launch never enters
    the lane table: a pin on it fails as unknown, without claiming the
    todo or burning an attempt."""
    use_lanes(
        monkeypatch,
        _vendor_lane("mystery", "unknown-adapter"),
    )
    store = _Store()
    store.add_todo(42, "Build")
    before = store.tag_strings(42)
    ctx, handle = _submit(store, seams, lane="mystery")
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None
    assert "mystery" in ctx.failure and "unknown or disabled" in ctx.failure
    assert seams["launch"] == []
    assert store.tag_strings(42) == before
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))


def test_submit_codex_clears_previous_last_message(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    use_lanes(
        monkeypatch,
        _vendor_lane("codex", "codex_exec"),
    )
    message = (
        tmp_path
        / ".claude"
        / "worktrees"
        / "slice-td42"
        / "scratch"
        / "last-message.md"
    )
    message.parent.mkdir(parents=True)
    message.write_text("validated: stale previous result", encoding="utf-8")
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="codex")
    assert handle is not None and not message.exists()
    _finish(handle, 0, "{broken json\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert store.comments(42) == [] and seams["lands"] == []


@pytest.mark.parametrize("message_file", [None, "", "  \n", "file handoff\n"])
def test_codex_parse_prefers_file_else_last_agent_event(
    tmp_path: Path,
    message_file: str | None,
) -> None:
    if message_file is not None:
        (tmp_path / "scratch").mkdir()
        (tmp_path / "scratch" / "last-message.md").write_text(
            message_file, encoding="utf-8"
        )
    log = (
        '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"last"}}\n'
        '{"type":"item.completed","item":{"type":"command_execution","text":"tool"}}\n'
        '{"type":"turn.completed"}\n{broken\n[]\n'
    )
    expected = "file handoff" if message_file == "file handoff\n" else "last"
    assert get_adapter("codex_exec").parse_result(log, tmp_path) == (expected, False)


@pytest.mark.parametrize(
    "log",
    [
        "",
        "not json",
        '{"type":"item.completed","item":null}',
        '{"type":"item.completed","item":{"type":"agent_message","text":7}}',
    ],
)
def test_codex_parse_missing_message_is_error(tmp_path: Path, log: str) -> None:
    assert get_adapter("codex_exec").parse_result(log, tmp_path) == (None, True)


def test_claude_adapter_parse_preserves_result_error(tmp_path: Path) -> None:
    log = '{"result":"first"}\n{"result":"turn limit","is_error":true}\n'
    assert get_adapter("claude_p").parse_result(log, tmp_path) == ("turn limit", True)


@pytest.mark.parametrize("exit_code", [0, 1, None])
def test_devin_parse_uses_exit_code(tmp_path: Path, exit_code: int | None) -> None:
    log = "  tool progress\n\nFinal handoff.\n"
    assert get_adapter("devin_tree").parse_result(
        log, tmp_path, exit_code=exit_code
    ) == (
        "tool progress\n\nFinal handoff.",
        exit_code != 0,
    )


@pytest.mark.parametrize("lane_change", ["unchanged", "removed", "changed"])
def test_poll_uses_persisted_adapter(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    lane_change: str,
) -> None:
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    use_lanes(
        monkeypatch,
        _vendor_lane("codex", "codex_exec"),
    )
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="codex")
    assert handle is not None
    if lane_change == "removed":
        from precis.utils.llm import accounts

        monkeypatch.setattr(accounts, "list_accounts", lambda store: [])
    elif lane_change == "changed":
        use_lanes(
            monkeypatch,
            _vendor_lane("codex", "claude_p"),
        )
    seams["ahead"] = "abc1234 feat(x): build\n"
    _finish(
        handle,
        0,
        '{"type":"item.completed","item":{"type":"agent_message","text":"Built."}}',
    )
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert store.comments(42)[-1] == "handoff: Built."
    assert seams["lands"] == [(42, Path(handle["worktree"]), "codex")]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_poll_devin_legacy_lane_fallback_and_exit_failure(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    use_lanes(
        monkeypatch,
        _vendor_lane("devin", "devin_tree"),
    )
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="devin")
    assert handle is not None
    ctx.meta_set.pop("adapter")
    seams["ahead"] = "abc1234 feat(x): build\n"
    _finish(handle, exit_code, "Final response.\n")
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    assert store.comments(42)[-1] == "handoff: Final response."
    if exit_code:
        assert pctx.status == "failed" and seams["lands"] == []
        assert store.tag_strings(42) == {"STATUS:open", "attempt:1"}
    else:
        assert pctx.status == "succeeded"
        assert seams["lands"] == [(42, Path(handle["worktree"]), "devin")]


def test_poll_handoff_clip_keeps_the_tail(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td573578: a clipped handoff keeps head AND tail — a trailing
    ``review:`` verdict glued to a devin transcript survives the cap."""
    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL")
    use_lanes(
        monkeypatch,
        _vendor_lane("devin", "devin_tree"),
    )
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _submit(store, seams, lane="devin")
    assert handle is not None
    seams["ahead"] = "abc1234 feat(x): build\n"
    result = (
        "Reviewing the landed squash `17897effe`, narration first. "
        + "narration " * 1000
        + "review: FIX — x"
    )
    assert len(result) > run_slice._HANDOFF_CAP
    _finish(handle, 0, result)
    pctx = _poll_ctx(store, ctx)
    assert run_slice.poll(pctx, handle) is True
    posted = store.comments(42)[-1]
    assert posted.startswith("handoff: " + result[:40])
    assert posted.endswith("review: FIX — x")
    assert len(posted) <= len("handoff: ") + run_slice._HANDOFF_CAP + len(
        " …(clipped)… "
    )


def test_launch_none_stdin_uses_devnull(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProcess:
        pid = 4242

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(run_slice.subprocess, "Popen", fake_popen)
    env: dict[str, str] = {"PATH": "/usr/bin"}
    assert (
        run_slice._launch(
            ["devin", "-p"],
            cwd=tmp_path,
            stdin=None,
            log=tmp_path / "log",
            exit_file=tmp_path / "exit",
            env=env,
        )
        == 4242
    )
    [(argv, kwargs)] = calls
    assert argv[-2:] == ["devin", "-p"]
    assert ' < "$RS_IN" ' in argv[2]
    assert kwargs["env"]["RS_IN"] == os.devnull
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


# ── kimi_tree (td573681) ────────────────────────────────────────────


def _kimi_lane_submit(
    store: _Store,
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str | None = "sk-test-not-a-key",
    bin_ok: bool = True,
    **params: Any,
) -> tuple[_Ctx, Any]:
    """Submit td42 on a pinned kimi lane with the bin + key seams stubbed."""
    from precis import secrets as _secrets

    monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL", raising=False)
    monkeypatch.setenv("PRECIS_KIMI_BIN", "/vendor/kimi")
    monkeypatch.setattr(run_slice, "_kimi_bin_ok", lambda _bin: bin_ok)
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: key)
    use_lanes(monkeypatch, _vendor_lane("kimi", "kimi_tree"))
    return _submit(store, seams, lane="kimi", **params)


def test_kimi_submit_launches_headless_with_key_env(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td573681: a kimi slice is ``kimi -p <brief> --output-format
    stream-json`` — the prompt rides argv (no stdin), and the session
    authenticates through the synthesized ``KIMI_MODEL_*`` provider env
    the worker fills from the vault key, never the user's Kimi config."""
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)
    assert handle is not None and ctx.failure is None
    [(argv, kw)] = seams["launch"]
    tree = Path(handle["worktree"])
    brief = (tree / "scratch" / "brief.md").read_text(encoding="utf-8")
    assert argv == [
        "/vendor/kimi",
        "-p",
        brief,
        "--output-format",
        "stream-json",
    ]
    assert kw["stdin"] is None and kw["cwd"] == tree
    env = kw["env"]
    assert {name for name in env if name.startswith("KIMI_")} == {
        "KIMI_MODEL_API_KEY",
        "KIMI_MODEL_BASE_URL",
        "KIMI_MODEL_NAME",
        "KIMI_MODEL_PROVIDER_TYPE",
        "KIMI_CODE_TRUST_WORKSPACE",
    }
    assert env["KIMI_MODEL_API_KEY"] == "sk-test-not-a-key"
    assert env["KIMI_MODEL_BASE_URL"] == "https://api.kimi.ai/coding/v1"
    assert env["KIMI_MODEL_NAME"] == "kimi-for-coding"
    assert env["KIMI_MODEL_PROVIDER_TYPE"] == "kimi"
    assert env["KIMI_CODE_TRUST_WORKSPACE"] == "1"
    # No -m (it would rebind to the config's managed provider) and no
    # User-Agent/identity/telemetry override — the CLI's defaults ship as
    # shipped; the subscription terms forbid tampering.
    assert "-m" not in argv
    assert not any(
        mark in name
        for name in env
        for mark in ("USER_AGENT", "CUSTOM_HEADERS", "IDENTITY", "TELEMETRY")
    )
    # The key rides the env only — never argv or a persisted job surface.
    assert all("sk-test-not-a-key" not in s for s in _kimi_surfaces(ctx, handle, argv))
    assert any("(mcp token: none)" in text for _kind, text in ctx.chunks)
    assert ctx.meta_set["adapter"] == "kimi_tree"


def test_kimi_submit_refuses_a_missing_key(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No KIMI_CODE_API_KEY anywhere is a host problem, not the task's:
    the refusal names the fix and burns no attempt (mirrors the devin
    preflight's shape, gr570688)."""
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, key=None)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None
    assert "KIMI_CODE_API_KEY" in ctx.failure and "next:" in ctx.failure
    assert seams["launch"] == []
    assert store.tag_strings(42) == {"STATUS:open"}
    assert ctx.meta_set["host_refusal"]["lane"] == "kimi"


def test_kimi_submit_refuses_a_missing_binary(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same host-refusal shape when the kimi binary does not resolve."""
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, bin_ok=False)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None
    assert "PRECIS_KIMI_BIN" in ctx.failure and "next:" in ctx.failure
    assert seams["launch"] == []
    assert store.tag_strings(42) == {"STATUS:open"}
    assert ctx.meta_set["host_refusal"]["lane"] == "kimi"


@pytest.mark.parametrize("model,want", [("k3", "k3"), (None, "kimi-for-coding")])
def test_kimi_model_rides_kimi_model_name(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
    want: str,
) -> None:
    """The model id is exported as ``KIMI_MODEL_NAME`` — never ``-m``,
    which would rebind the CLI to the config's managed/OAuth provider."""
    store = _Store()
    store.add_todo(42, "Build")
    params: dict[str, Any] = {"model": model} if model else {}
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, **params)
    assert handle is not None
    [(argv, kw)] = seams["launch"]
    assert kw["env"]["KIMI_MODEL_NAME"] == want
    assert "-m" not in argv


def test_kimi_parse_last_assistant_line(tmp_path: Path) -> None:
    log = (
        '{"role":"meta","type":"system.version"}\n'
        '{"role":"assistant","content":"first"}\n'
        '{"role":"meta","type":"session.resume_hint"}\n'
        '{"role":"assistant","content":"last"}\n'
    )
    assert get_adapter("kimi_tree").parse_result(log, tmp_path) == ("last", False)


def test_kimi_parse_tolerates_content_parts(tmp_path: Path) -> None:
    log = (
        '{"role":"assistant","content":'
        '[{"type":"text","text":"a"},{"type":"text","text":"b"}]}\n'
    )
    assert get_adapter("kimi_tree").parse_result(log, tmp_path) == ("ab", False)


@pytest.mark.parametrize("exit_code", [1, 3, None])
def test_kimi_parse_bad_exit_is_error(tmp_path: Path, exit_code: int | None) -> None:
    log = '{"role":"assistant","content":"done"}\n'
    assert get_adapter("kimi_tree").parse_result(
        log, tmp_path, exit_code=exit_code
    ) == ("done", exit_code != 0)


@pytest.mark.parametrize(
    "log", ["", "noise\n", '{"role":"meta","type":"x"}', "{broken"]
)
def test_kimi_parse_missing_assistant_is_error(tmp_path: Path, log: str) -> None:
    assert get_adapter("kimi_tree").parse_result(log, tmp_path) == (None, True)


def _kimi_surfaces(
    ctx: _Ctx, handle: Any, argv: list[str], store: _Store | None = None
) -> list[str]:
    """Every persisted/visible surface a key leak could land on: argv,
    job chunks, the failure record, meta, the handle and the todo's
    comments."""
    surfaces = list(argv) + [text for _kind, text in ctx.chunks]
    surfaces += [ctx.failure or "", str(ctx.meta_set), str(handle)]
    if store is not None:
        surfaces += store.comments(42)
    return surfaces


@pytest.mark.parametrize("source", ["env", "vault"])
def test_kimi_key_reaches_child_env_from_env_or_vault(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """The membership key resolves through ``get_secret``'s env-var
    fallback or the vault — either way it lands on
    ``KIMI_MODEL_API_KEY`` in the child env and on no stored surface."""
    key = f"sk-src-{source}-key"
    store = _Store()
    store.add_todo(42, "Build")
    if source == "env":
        # No get_secret patch: the real resolver's env-first order is
        # what the worker runs.
        monkeypatch.setenv("KIMI_CODE_API_KEY", key)
        monkeypatch.delenv("PRECIS_RUN_SLICE_MODEL", raising=False)
        monkeypatch.setenv("PRECIS_KIMI_BIN", "/vendor/kimi")
        monkeypatch.setattr(run_slice, "_kimi_bin_ok", lambda _bin: True)
        use_lanes(monkeypatch, _vendor_lane("kimi", "kimi_tree"))
        ctx, handle = _submit(store, seams, lane="kimi")
    else:
        ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, key=key)
    assert handle is not None and ctx.failure is None
    [(argv, kw)] = seams["launch"]
    assert kw["env"]["KIMI_MODEL_API_KEY"] == key
    assert all(key not in s for s in _kimi_surfaces(ctx, handle, argv))


def test_kimi_launch_base_url_comes_from_the_profile(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account's region decides the endpoint: a ``base_url`` on the
    ``kimi`` profile reaches the child's ``KIMI_MODEL_BASE_URL``."""
    monkeypatch.setenv(
        "PRECIS_LLM_PROFILES",
        json.dumps({"kimi": {"base_url": "https://api.kimi.com/coding/v1"}}),
    )
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)
    assert handle is not None and ctx.failure is None
    [(_argv, kw)] = seams["launch"]
    assert kw["env"]["KIMI_MODEL_BASE_URL"] == "https://api.kimi.com/coding/v1"


#: A key spelled so its plain, URL-encoded and JSON-escaped forms all
#: differ — the scrub must catch all three.
_LEAKY_KEY = 'sk-x+a/b="c'


def test_kimi_poll_scrubs_key_from_failure_and_handoff(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that echoes its key gets it scrubbed before the log tail
    reaches the failure record and before the last assistant line posts
    as the handoff — plain, URL-encoded and JSON-escaped forms alike."""
    import urllib.parse

    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, key=_LEAKY_KEY)
    assert handle is not None
    escaped = json.dumps(_LEAKY_KEY)[1:-1]
    encoded = urllib.parse.quote(_LEAKY_KEY, safe="")
    log = (
        '{"role":"meta","type":"system.version"}\n'
        f'{{"role":"tool_result","content":"env says {escaped}"}}\n'
        f"tool echoed {_LEAKY_KEY} and urlencoded {encoded}\n"
        f'{{"role":"assistant","content":"handoff text with {escaped}"}}\n'
    )
    _finish(handle, 1, log)
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert pctx.status == "failed"
    assert pctx.failure is not None
    assert "failed (exit 1)" in pctx.failure
    assert "[redacted:KIMI_MODEL_API_KEY]" in pctx.failure
    handoff = store.comments(42)[-1]
    assert handoff.startswith("handoff: ")
    assert "[redacted:KIMI_MODEL_API_KEY]" in handoff
    surfaces = _kimi_surfaces(pctx, handle, [], store)
    surfaces += [text for _kind, text in pctx.chunks]
    for surface in surfaces:
        for form in (_LEAKY_KEY, escaped, encoded):
            assert form not in surface


def test_kimi_poll_clean_exit_posts_a_scrubbed_handoff(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful stream-json run posts the last assistant line as the
    handoff — key scrubbed the same way."""
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, key=_LEAKY_KEY)
    assert handle is not None
    escaped = json.dumps(_LEAKY_KEY)[1:-1]
    _finish(
        handle,
        0,
        f'{{"role":"assistant","content":"done, key was {escaped}"}}\n',
    )
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    handoff = store.comments(42)[-1]
    assert "[redacted:KIMI_MODEL_API_KEY]" in handoff
    assert _LEAKY_KEY not in handoff


def test_kimi_poll_unreadable_secret_stores_no_key_lines(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key gone at poll: nothing is redacted (the value is unknown),
    so the stored tail is truncated before the first line that names a
    kimi env var — the one place a key value could sit."""
    from precis import secrets as _secrets

    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)
    assert handle is not None
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: None)
    log = (
        "plain earlier line\nKIMI_MODEL_API_KEY=sk-test-not-a-key spilled\nlater line\n"
    )
    _finish(handle, 1, log)
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert pctx.status == "failed"
    assert pctx.failure is not None
    assert "sk-test-not-a-key" not in pctx.failure
    assert "KIMI_MODEL_API_KEY" not in pctx.failure
    assert "plain earlier line" in pctx.failure


#: The comment a poll posts instead of the raw handoff when a key value
#: the child may have carried could not be re-read (review round 2).
_WITHHELD = (
    "handoff withheld: credential unreadable at poll; log tail retained redacted"
)


def _assert_no_keys(pctx: _Ctx, handle: Any, store: _Store, *keys: str) -> None:
    """Every persisted surface a key could land on is clean."""
    surfaces = _kimi_surfaces(pctx, handle, [], store)
    surfaces += [text for _kind, text in pctx.chunks]
    for surface in surfaces:
        for key in keys:
            assert key not in surface


def test_kimi_poll_unreadable_secret_withholds_the_handoff(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 2 item 1: the credential unreadable at poll — the
    session's last message could carry a key the poll cannot name, so
    the todo gets the withheld comment and the key lands on no stored
    surface (failure record, comments, job summary, events)."""
    from precis import secrets as _secrets

    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)
    assert handle is not None
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: None)
    key = "sk-leaked-in-handoff"
    _finish(
        handle,
        1,
        "plain earlier line\n"
        f'{{"role":"assistant","content":"env says KIMI_MODEL_API_KEY={key}"}}\n',
    )
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert pctx.status == "failed"
    assert store.comments(42) == [_WITHHELD]
    assert pctx.failure is not None and "plain earlier line" in pctx.failure
    _assert_no_keys(pctx, handle, store, key)


def test_kimi_poll_unreadable_secret_withholds_a_clean_exit(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 0 changes nothing: the last assistant message still carries
    session text, so the withheld comment stands on a succeeded run."""
    from precis import secrets as _secrets

    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)
    assert handle is not None
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: None)
    seams["ahead"] = "abc1234 feat(slice): work"
    key = "sk-leaked-clean-exit"
    _finish(
        handle,
        0,
        f'{{"role":"assistant","content":"KIMI_MODEL_API_KEY={key}"}}\n',
    )
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert pctx.status == "succeeded"
    assert store.comments(42) == [_WITHHELD]
    _assert_no_keys(pctx, handle, store, key)


def test_kimi_poll_rotated_key_scrubs_both_values(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 2 item 2: a key rotated mid-run — the child env got
    A, the vault answers B at poll. The submit-time value rides process
    memory (never a stored surface), so the log naming either key is
    scrubbed everywhere, and the memory is cleared when the poll ends."""
    from precis import secrets as _secrets

    key_a, key_b = "sk-first-secret", "sk-rotated-secret"
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, key=key_a)
    assert handle is not None
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: key_b)
    _finish(
        handle,
        1,
        f'{{"role":"assistant","content":"saw {key_a} then {key_b}"}}\n'
        "later log noise\n",
    )
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    handoff = store.comments(42)[-1]
    assert handoff.startswith("handoff: ")
    assert handoff.count("[redacted:KIMI_MODEL_API_KEY]") == 2
    assert pctx.failure is not None and "later log noise" in pctx.failure
    _assert_no_keys(pctx, handle, store, key_a, key_b)
    assert run_slice._KIMI_SUBMIT_KEYS == {}


def test_kimi_poll_lost_submit_value_withholds_and_drops(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart case of item 2: the in-memory submit value is gone,
    so the unknown key A cannot be scrubbed — the stored tail drops the
    env-naming lines and the handoff is withheld, as when the credential
    is unreadable."""
    from precis import secrets as _secrets

    key_a, key_b = "sk-first-secret", "sk-rotated-secret"
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, key=key_a)
    assert handle is not None
    run_slice._KIMI_SUBMIT_KEYS.clear()  # a worker restart loses the dict
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: key_b)
    _finish(
        handle,
        1,
        "plain earlier line\n"
        f'{{"role":"assistant","content":"KIMI_MODEL_API_KEY={key_a}"}}\n',
    )
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert store.comments(42) == [_WITHHELD]
    assert pctx.failure is not None
    assert "plain earlier line" in pctx.failure
    assert "KIMI_MODEL_API_KEY" not in pctx.failure
    _assert_no_keys(pctx, handle, store, key_a, key_b)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://evil.example.invalid/coding/v1",
        "https://user:pass@api.kimi.ai/coding/v1",
        "https://api.kimi.ai:8443/coding/v1",
        "http://api.kimi.ai/coding/v1",
        "https://1.2.3.4/coding/v1",
        "https://api.kimi.ai.evil.example/coding/v1",
    ],
    ids=["other-host", "userinfo", "port", "http", "ip-literal", "suffix-host"],
)
def test_kimi_submit_refuses_a_base_url_off_the_allowlist(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """Review round 2 item 3: a profile ``base_url`` off the documented
    Kimi Code hosts is a launch refusal — the key never leaves the
    process for an unlisted host — and the refusal names the
    ``llm.profiles`` ``kimi`` ``base_url`` setting and the allowlist."""
    monkeypatch.setenv(
        "PRECIS_LLM_PROFILES", json.dumps({"kimi": {"base_url": base_url}})
    )
    store = _Store()
    store.add_todo(42, "Build")

    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)

    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == []
    assert "llm.profiles" in (ctx.failure or "")
    assert "api.kimi.ai" in (ctx.failure or "")
    assert ctx.meta_set["host_refusal"]["lane"] == "kimi"
    assert "sk-test-not-a-key" not in str(ctx.meta_set)


# ── td573681 round 3: the submit key dies on EVERY exit path ─────────────


def test_kimi_launch_error_drops_the_submit_key(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``_launch`` raise after the env was built is an exit path that
    skips the poll — ``_fail_submit`` must drop the stored submit key,
    not leave a credential in process memory for a run that never was."""
    monkeypatch.setattr(
        run_slice,
        "_launch",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("spawn failed")),
    )
    store = _Store()
    store.add_todo(42, "Build")

    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)

    assert handle is None and ctx.failure is not None
    assert run_slice._KIMI_SUBMIT_KEYS == {}


def test_kimi_launch_host_refusal_drops_the_submit_key(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``_HostRefusal`` out of ``_launch`` takes the backoff branch, not
    ``_fail_submit`` — it pops the stored key too."""
    monkeypatch.setattr(
        run_slice,
        "_launch",
        lambda *a, **kw: (_ for _ in ()).throw(run_slice._HostRefusal("host blocked")),
    )
    store = _Store()
    store.add_todo(42, "Build")

    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)

    assert handle is None and ctx.failure is not None
    assert ctx.meta_set["host_refusal"]["lane"] == "kimi"
    assert run_slice._KIMI_SUBMIT_KEYS == {}


def test_kimi_kill_and_release_drop_the_submit_key(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill and release end the job without a terminal poll — both drop
    the key a live submit recorded."""
    for end in ("kill", "release"):
        store = _Store()
        store.add_todo(42, "Build")
        ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)
        assert handle is not None
        assert run_slice._KIMI_SUBMIT_KEYS == {900: "sk-test-not-a-key"}

        getattr(run_slice, end)(_poll_ctx(store, ctx), handle)

        assert run_slice._KIMI_SUBMIT_KEYS == {}
        run_slice._KIMI_SUBMIT_KEYS.clear()


def test_kimi_moot_verdict_withholds_when_unscrubbed(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document node's ``moot:`` first line is session text: when the
    credential cannot be re-read the verdict's ``why`` is withheld from
    the job summary like the handoff is from the comments. (A code
    node's bare ``moot:`` is no verdict since td584381 S1d — it must
    verify a sha; the trusted path this pins is the ``task_class``
    one.)"""
    from precis import secrets as _secrets

    store = _Store()
    store.add_todo(42, "Check gr1", meta={"task_class": "validate"})
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)
    assert handle is not None
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: None)
    key = "sk-leaked-verdict"
    _finish(
        handle,
        0,
        f'{{"role":"assistant","content":"moot: KIMI_MODEL_API_KEY={key}"}}\n',
    )
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert pctx.status == "succeeded"
    assert store.comments(42) == [_WITHHELD]
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "Land: none, by verdict `moot: withheld: credential unreadable" in summary
    _assert_no_keys(pctx, handle, store, key)
    assert "KIMI_MODEL_API_KEY" not in summary


def test_kimi_generator_noop_verdict_withholds_when_unscrubbed(
    tmp_path: Path, seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same withheld verdict on a child-minting generator's
    ``Children: none`` line. kimi_tree has no MCP so submit refuses a
    generator — the ctx and handle are built by hand, keyed on ref 900."""
    from precis import secrets as _secrets

    store = _Store()
    store.add_todo(42, "Slice it", meta={"generate": {"role": "slicer", "from": "x"}})
    tree = tmp_path / "wt"
    tree.mkdir()
    handle = {
        "pid": 4242,
        "pgid": 4242,
        "worktree": str(tree),
        "log": str(tree / "run_slice.log"),
        "exit_file": str(tree / "run_slice.exit"),
    }
    ctx = _Ctx(store, {"todo_id": 42}, lane="kimi", adapter="kimi_tree", model="k3")
    key = "sk-leaked-gen-verdict"
    run_slice._KIMI_SUBMIT_KEYS.clear()
    run_slice._KIMI_SUBMIT_KEYS[900] = "sk-submitted-env-key"
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: None)
    _finish(
        handle,
        0,
        f'{{"role":"assistant","content":"no-op: KIMI_MODEL_API_KEY={key}"}}\n',
    )

    assert run_slice.poll(ctx, handle) is True

    assert ctx.status == "succeeded"
    assert store.comments(42) == [_WITHHELD]
    [summary] = [t for k, t in ctx.chunks if k == "job_summary"]
    assert (
        "Children: none, by verdict `no-op: withheld: credential unreadable" in summary
    )
    _assert_no_keys(ctx, handle, store, key, "sk-submitted-env-key")
    assert "KIMI_MODEL_API_KEY" not in summary
    assert run_slice._KIMI_SUBMIT_KEYS == {}


def test_kimi_deny_and_commit_surfaces_redact_the_key(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deny-path text, human-only hits and commit subjects are session
    surfaces too — with the key readable they carry
    ``[redacted:KIMI_MODEL_API_KEY]``, never the value."""
    key = "sk-leaky-path-name"
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, key=key)
    assert handle is not None
    seams["diff"] = f"scripts/lib/{key}.sh\ndeploy/inventory/{key}.yml\n"
    seams["ahead"] = f"abc1234 fix: reads KIMI_MODEL_API_KEY={key}\n"
    _finish(handle, 1, '{"role":"assistant","content":"oops"}\n')
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert pctx.status == "failed"
    surfaces = _kimi_surfaces(pctx, handle, [], store)
    surfaces += [text for _kind, text in pctx.chunks]
    joined = "\n".join(surfaces)
    assert key not in joined
    assert "[redacted:KIMI_MODEL_API_KEY]" in joined


def test_kimi_unscrubbed_withholds_deny_paths_and_commit_lines(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unreadable-credential fallback on the branch surfaces: a deny
    path or commit line NAMING a kimi env var could carry the value, so
    the deny reason is withheld whole, env-naming human hits drop (no
    meta stamp) and env-naming commit lines never reach branch_commits."""
    from precis import secrets as _secrets

    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)
    assert handle is not None
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: None)
    seams["diff"] = "deploy/inventory/KIMI_MODEL_API_KEY.yml\n"
    seams["ahead"] = (
        "abc1234 fix: reads KIMI_MODEL_API_KEY\ndef5678 fix: ordinary commit\n"
    )
    _finish(handle, 1, '{"role":"assistant","content":"oops"}\n')
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert pctx.status == "failed"
    assert pctx.failure is not None
    assert "path withheld" in pctx.failure
    assert "KIMI_MODEL_API_KEY" not in pctx.failure
    assert store.comments(42) == [_WITHHELD]
    assert run_slice.HUMAN_ONLY_KEY not in pctx.meta_set
    assert pctx.meta_set["branch_commits"] == ["def5678 fix: ordinary commit"]
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "KIMI_MODEL_API_KEY" not in summary
    assert run_slice._KIMI_SUBMIT_KEYS == {}


# ── gr587234: the committed-note guards + the withheld-verdict matrix ──


@pytest.mark.parametrize("readable", [True, False], ids=["redacted", "withheld"])
def test_kimi_worker_commit_note_scrubs_or_withholds(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, readable: bool
) -> None:
    """``_commit_for_session``'s note is session-side text too — the
    "git add failed (<stderr>)" / "worker commit failed (<stderr>)"
    shapes can carry a key stderr echoed — so the same scrub covers it:
    the credential readable at poll → ``[redacted:KIMI_MODEL_API_KEY]``
    on the summary's Commit line; unreadable → the note withheld whole
    when it names a kimi env var."""
    from precis import secrets as _secrets

    key = "sk-commit-note-key"
    note = f"worker commit failed (stderr: KIMI_MODEL_API_KEY={key})"
    monkeypatch.setattr(run_slice, "_commit_for_session", lambda *_a: note)
    store = _Store()
    store.add_todo(42, "Build")
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch, key=key)
    assert handle is not None
    if not readable:
        monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: None)
    seams["ahead"] = "abc1234 fix: work"
    _finish(handle, 0, '{"role":"assistant","content":"tests: ran"}\n')
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert pctx.status == "succeeded"
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    if readable:
        assert (
            "Commit: worker commit failed "
            "(stderr: KIMI_MODEL_API_KEY=[redacted:KIMI_MODEL_API_KEY])" in summary
        )
    else:
        assert "Commit: worker commit note withheld (credential unreadable)" in summary
        assert note not in summary
        assert store.comments(42) == [_WITHHELD]
    _assert_no_keys(pctx, handle, store, key)


def test_kimi_noop_verdict_withholds_when_unscrubbed(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task-side ``no-op:`` cell of the withheld-verdict matrix —
    same withheld ``Land: none, by verdict`` line as ``moot:``, without
    the moot tag."""
    from precis import secrets as _secrets

    store = _Store()
    store.add_todo(42, "Check gr1", meta={"op": "run_slice"})
    ctx, handle = _kimi_lane_submit(store, seams, monkeypatch)
    assert handle is not None
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: None)
    key = "sk-leaked-noop-verdict"
    _finish(
        handle,
        0,
        f'{{"role":"assistant","content":"no-op: KIMI_MODEL_API_KEY={key}"}}\n',
    )
    pctx = _poll_ctx(store, ctx)

    assert run_slice.poll(pctx, handle) is True

    assert pctx.status == "succeeded"
    assert store.comments(42) == [_WITHHELD]
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "Land: none, by verdict `no-op: withheld: credential unreadable" in summary
    assert "verdict:moot" not in store.tag_strings(42)
    _assert_no_keys(pctx, handle, store, key)
    assert "KIMI_MODEL_API_KEY" not in summary


def test_kimi_generator_moot_verdict_withholds_when_unscrubbed(
    tmp_path: Path, seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generator-side ``moot:`` cell — the withheld ``Children: none``
    line plus the ``verdict:moot`` tag the tick closes the plan on.
    kimi_tree has no MCP so submit refuses a generator — the ctx and
    handle are built by hand, keyed on ref 900."""
    from precis import secrets as _secrets

    store = _Store()
    store.add_todo(42, "Slice it", meta={"generate": {"role": "slicer", "from": "x"}})
    tree = tmp_path / "wt"
    tree.mkdir()
    handle = {
        "pid": 4242,
        "pgid": 4242,
        "worktree": str(tree),
        "log": str(tree / "run_slice.log"),
        "exit_file": str(tree / "run_slice.exit"),
    }
    ctx = _Ctx(store, {"todo_id": 42}, lane="kimi", adapter="kimi_tree", model="k3")
    key = "sk-leaked-gen-moot-verdict"
    run_slice._KIMI_SUBMIT_KEYS.clear()
    run_slice._KIMI_SUBMIT_KEYS[900] = "sk-submitted-env-key"
    monkeypatch.setattr(_secrets, "get_secret", lambda _name, **kw: None)
    _finish(
        handle,
        0,
        f'{{"role":"assistant","content":"moot: KIMI_MODEL_API_KEY={key}"}}\n',
    )

    assert run_slice.poll(ctx, handle) is True

    assert ctx.status == "succeeded"
    assert store.comments(42) == [_WITHHELD]
    [summary] = [t for k, t in ctx.chunks if k == "job_summary"]
    assert (
        "Children: none, by verdict `moot: withheld: credential unreadable" in summary
    )
    assert "verdict:moot" in store.tag_strings(42)
    _assert_no_keys(ctx, handle, store, key, "sk-submitted-env-key")
    assert "KIMI_MODEL_API_KEY" not in summary
    assert run_slice._KIMI_SUBMIT_KEYS == {}


def test_kimi_malformed_handle_drops_the_submit_key() -> None:
    """Poll's malformed-handle branch is an exit path that never reaches
    the terminal poll — it pops the job's stored submit key before the
    failure record, like a kill or a release."""
    store = _Store()
    store.add_todo(42, "Build")
    ctx = _Ctx(store, {"todo_id": 42}, lane="kimi", adapter="kimi_tree", model="k3")
    run_slice._KIMI_SUBMIT_KEYS.clear()
    run_slice._KIMI_SUBMIT_KEYS[900] = "sk-submitted-env-key"

    assert run_slice.poll(ctx, "not-a-handle") is True

    assert ctx.status == "failed"
    assert ctx.failure is not None and "malformed handle" in ctx.failure
    assert "sk-submitted-env-key" not in ctx.failure
    assert run_slice._KIMI_SUBMIT_KEYS == {}
