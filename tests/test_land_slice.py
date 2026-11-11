"""land_slice job type — the dark-factory lander (td543731).

Pure tests against a fake store and a fake git seam; ship is either a fake
launcher or (one test) a stub script reached through ``PRECIS_SHIP_BIN`` and
the real detached launcher. No DB, no real git, no real scripts/ship.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: the launcher execs /bin/sh"
)

from precis.plans.templates import PENDING_SHA
from precis.store.types import ChunkInsert, Tag
from precis.workers.executors import ssh_node
from precis.workers.job_types import (
    get_job_type,
    known_job_types,
    land_slice,
    run_slice,
)

FULL_SHA = "abc1234" + "0" * 33
SUBJECT = "feat(x): widget (td42)"
# The real preflight, captured before the `seams` fixture replaces it.
_REAL_DRIFT_PREFLIGHT = land_slice._drift_preflight

NONE_GREEN_DIAG = (
    "last-gated-main-sha: status=none local=40 fetched=40 with_suites=40 "
    "with_runs=40 matched=40 runs: lint=SUCCESS, test-linux (3.13, 1)=FAILURE (exit 2)"
)
BLIND_DIAG = (
    "last-gated-main-sha: status=unknown (blind: no check runs visible) local=40 "
    "fetched=40 with_suites=40 with_runs=0 matched=40 runs: - (exit 0)"
)


def _preflight(rc: int | None, age: str, diag: str) -> dict[str, Any]:
    return {"ran": True, "rc": rc, "age": age, "diag": diag}


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
        "diff_names": "",
        "worktree_list": "",
        # The paths ``run_slice.changed_paths`` answers for the submit's
        # engine-diff probe; ``None`` = git could not read the diff.
        "changed_paths": ["src/precis/x.py"],
        # The verdict ``land_slice.spot_selection`` answers for the
        # submit's spot/wide probe (gr599427): True = a small spot
        # selection (``--checked`` earned), False = wide/empty/FULL/
        # unreadable (``--quick``).
        "spot": True,
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
        if args[:2] == ["diff", "--name-only"]:
            return _proc(calls["diff_names"])
        if args[:2] == ["worktree", "list"]:
            return _proc(calls["worktree_list"])
        return _proc()

    def fake_launch(argv: list[str], **kw: Any) -> int:
        calls["launch"].append((argv, kw))
        # A distinct pid per launch: ship is 5151, a verify probe 5152/… —
        # the handle's promoted pgid must name the LIVE phase's process.
        return 5150 + len(calls["launch"])

    monkeypatch.setattr(land_slice, "_git", fake_git)
    monkeypatch.setattr(run_slice, "changed_paths", lambda tree: calls["changed_paths"])
    monkeypatch.setattr(land_slice, "spot_selection", lambda tree: calls["spot"])
    monkeypatch.setattr(land_slice, "_launch", fake_launch)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: calls["alive"])
    monkeypatch.setattr(
        land_slice, "_pending_gates", lambda store, plan, todo: list(calls["gates"])
    )
    calls["gripe"] = None
    monkeypatch.setattr(land_slice, "_plan_gripe", lambda store, plan: calls["gripe"])
    # The lander's own drift preflight: by default "no helper in this tree",
    # so ship launches and its own guard decides (the pre-gr550941 shape).
    calls["preflight"] = {"ran": False, "rc": None, "age": "", "diag": ""}
    calls["probed"] = []

    def fake_preflight(tree: Path) -> dict[str, Any]:
        calls["probed"].append(tree)
        return dict(calls["preflight"])

    monkeypatch.setattr(land_slice, "_drift_preflight", fake_preflight)
    # Docker seams: no test reaches a daemon. `_sweep_stacks` records the
    # keep tree and returns `evicted_stacks`; `_evict_stack` records
    # (project, volumes) and returns `evict_ok`.
    calls["swept"] = []
    calls["evicted_stacks"] = []
    calls["evict"] = []
    calls["evict_ok"] = True
    monkeypatch.setattr(
        land_slice,
        "_sweep_stacks",
        lambda *, keep=None: (
            calls["swept"].append(keep) or list(calls["evicted_stacks"])
        ),
    )
    monkeypatch.setattr(
        land_slice,
        "_evict_stack",
        lambda project, *, volumes: (
            calls["evict"].append((project, volumes)) or calls["evict_ok"]
        ),
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

#: gr561142: ship's nothing-new guard — nothing squashed, then the landed
#: line names origin/main's existing head (another session's commit).
NO_DIFF_LOG = (
    "quick ship (--quick)\n"
    "nothing new to ship — tree identical to origin/main (already landed); "
    "skipping the push\n"
    "shipped — landed on main:\n"
    "e249413a7 docs(backlog): memory gardener spec and its fifteen slice gripes\n"
    "SHIP_STATUS: shipped\n"
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
    # the LAST non-test commit labels the squash — an early commit naming
    # the handle no longer beats the fix-ups that came after it (td599723)
    subjects = ["wip: scratch", "feat(a): the thing (td42)", "fix(a): lint (td42)"]
    assert land_slice.squash_subject(subjects, 42) == "fix(a): lint (td42)"
    # no commit names the slice: the LAST non-test commit, never the first
    # commit blindly (gr574383)
    assert land_slice.squash_subject(["feat(a): b", "fix: c"], 42) == ("fix: c (td42)")
    # a red-test-first commit never labels the squash (gr574383)
    assert (
        land_slice.squash_subject(
            [
                "test(f): red tests for the thing (td42)",
                "feat(f): the thing (td42)",
            ],
            42,
        )
        == "feat(f): the thing (td42)"
    )
    # nor does ship's wip snapshot commit
    assert (
        land_slice.squash_subject(
            ["wip(slice/42): end-of-session snapshot", "fix(f): the thing"], 42
        )
        == "fix(f): the thing (td42)"
    )
    # every commit a test/wip: the last stands in — a test subject still
    # lands rather than nothing
    assert (
        land_slice.squash_subject(
            ["test(f): red (td42)", "test(f): still red (td42)"], 42
        )
        == "test(f): still red (td42)"
    )
    assert land_slice.squash_subject([], 42) is None
    assert land_slice.squash_subject(["  ", ""], 42) is None


def test_merge_commit_never_labels_the_squash(tmp_path: Path) -> None:
    """gr627889: a builder resolves a migration collision with `git merge
    origin/main`; the merge's `Merge origin/main into slice/N` subject used to
    be the newest subject, so the land passed it to ship's type(scope) lint
    and failed. Real git: the branch lands under its own commit's subject."""
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, **env},
        )

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "base")
    git("switch", "-q", "-c", "slice/42")
    git("commit", "-q", "--allow-empty", "-m", "feat(x): widget")
    git("switch", "-q", "main")
    git("commit", "-q", "--allow-empty", "-m", "feat(y): sibling lands first")
    git("update-ref", "refs/remotes/origin/main", "main")
    git("switch", "-q", "slice/42")
    git("merge", "-q", "--no-ff", "-m", "Merge origin/main into slice/42", "main")

    subjects = land_slice._branch_subjects(tmp_path)
    assert subjects == ["feat(x): widget"]
    assert land_slice.squash_subject(subjects or [], 42) == "feat(x): widget (td42)"


def test_squash_subject_prefers_the_stated_subject() -> None:
    """gr574383: the brief's `subject:` line beats every commit subject."""
    assert (
        land_slice.squash_subject(
            ["test(f): red tests (td42)", "feat(f): the commits (td42)"],
            42,
            node="fix(f): the stated subject (gr9)",
        )
        == "fix(f): the stated subject (gr9, td42)"
    )
    assert land_slice.squash_subject([], 42, node="fix(f): x") is None


def test_node_subject_reads_the_brief() -> None:
    """A conventional `subject:` line on the todo body wins; then the
    newest brief:/handoff: comment (the builder's final hand-off); a
    non-conventional line is prose, not a subject (gr574383)."""
    store = _Store()
    store.add_todo(42)
    assert land_slice._node_subject(store, 42) is None
    store.chunks.rows[42] = [
        _Chunk(1, "the brief\n\nsubject: fix(b): the stated one (gr9)", "todo_body"),
        _Chunk(
            2,
            "handoff: finished\nsubject: fix(c): the handoff one",
            "todo_comment",
        ),
    ]
    assert land_slice._node_subject(store, 42) == "fix(b): the stated one (gr9)"
    # no body line: the newest handoff's line stands in
    store.chunks.rows[42] = [
        _Chunk(1, "the brief", "todo_body"),
        _Chunk(2, "note: not a brief comment", "todo_comment"),
        _Chunk(
            3,
            "handoff: finished\nsubject: fix(c): the handoff one",
            "todo_comment",
        ),
    ]
    assert land_slice._node_subject(store, 42) == "fix(c): the handoff one"
    # prose "subject:" that is not a conventional subject is ignored
    store.chunks.rows[42] = [
        _Chunk(1, "subject: make it a one-line conventional subject", "todo_body")
    ]
    assert land_slice._node_subject(store, 42) is None
    assert land_slice._node_subject(None, 42) is None


def test_with_handle_merges_and_caps() -> None:
    assert land_slice.with_handle("fix(x): y (gr7)", 42) == "fix(x): y (gr7, td42)"
    assert land_slice.with_handle("fix(x): y (gr7, td42)", 42) == (
        "fix(x): y (gr7, td42)"
    )
    # The cap is ship's hard limit of 120 CHARACTERS — a subject that fits
    # lands whole, handles appended (gr574383: this one is 95 characters
    # and used to be shortened to 72 bytes, cut inside "not").
    brief = (
        "fix(extract): an index row such as m = 0 is data, not a "
        "column-label row (gr573944)"
    )
    assert land_slice.with_handle(brief, 574224) == (
        "fix(extract): an index row such as m = 0 is data, not a "
        "column-label row (gr573944, td574224)"
    )
    # over the cap: cut at a word boundary, never inside a word
    long = "feat(scope): " + "alpha " * 40 + "beta (td42)"
    capped = land_slice.with_handle(long, 42)
    assert len(capped) <= land_slice.SUBJECT_CAP
    assert capped.endswith(" (td42)")
    head = capped[: -len(" (td42)")]
    assert head.split()[-1] == "alpha"
    # one giant word: the only boundary inside the cap sits after the
    # prefix, so the cut lands there — never inside a word (td599723)
    assert land_slice.with_handle("fix: " + "x" * 200, 42) == "fix: (td42)"
    long = "feat(scope): " + "é " * 80 + "(td42)"
    capped = land_slice.with_handle(long, 42)
    assert len(capped) <= land_slice.SUBJECT_CAP
    assert capped.endswith("(td42)") and capped.startswith("feat(scope): é")


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
    assert land_slice.nothing_new(GREEN_LOG) is False
    assert land_slice.nothing_new(NO_DIFF_LOG) is True
    # the guard's log still names main's head on the landed line
    assert land_slice.landed_short(NO_DIFF_LOG) == "e249413a7"


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
    # the checked run's xdist workers are capped (gr567471: a static-wide
    # selection at -n6 OOM-killed a worker on melchior twice). PRECIS_GATE_N
    # is what ship's checked gate expands into `pytest -n`; PRECIS_TEST_NPROC
    # is scripts/test's name for the same count.
    assert kw["env"]["PRECIS_GATE_N"] == str(land_slice.DEFAULT_TEST_NPROC)
    assert kw["env"]["PRECIS_TEST_NPROC"] == str(land_slice.DEFAULT_TEST_NPROC)
    # ship's uv steps run on the worker's Python line, never the newest
    # interpreter on the host (jo551780: Homebrew 3.15, no rdkit wheel)
    assert kw["env"]["UV_PYTHON"] == land_slice.ship_python()
    assert (
        kw["env"]["UV_PYTHON"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    assert handle["subject"] == SUBJECT and handle["pid"] == 5151
    assert handle["log"] == str(tree / "scratch" / "land_slice.log")
    assert ctx.meta_set["land_subject"] == SUBJECT
    # the launch event names the cap, because ship never echoes the resolved
    # `-n` (jo570059's log could not confirm whether the cap applied)
    launch_events = [t for k, t in ctx.chunks if k == "job_event" and " pid 5151 " in t]
    assert launch_events and launch_events[-1].endswith(
        f"(gate_n={land_slice.DEFAULT_TEST_NPROC}, "
        f"slots={land_slice.DEFAULT_LAND_GATE_SLOTS})"
    )
    assert (
        ["log", "--reverse", "--no-merges", "--format=%s", "origin/main..HEAD"],
        tree,
    ) in seams["git"]


def test_submit_uses_the_stated_subject(seams: dict[str, Any]) -> None:
    """gr574383: a `subject:` line on the todo beats the branch's commit
    subjects — here a red-test-first commit that used to win."""
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub",),
        comments=("handoff: done\nsubject: fix(stated): the brief said so",),
    )
    seams["subjects"] = [
        "test(x): red tests for the thing (td42)",
        "feat(x): the thing (td42)",
    ]
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None
    [(argv, _kw)] = seams["launch"]
    assert argv[2] == "fix(stated): the brief said so (td42)"
    assert handle["subject"] == argv[2]


def test_submit_skips_a_red_test_first_subject(seams: dict[str, Any]) -> None:
    """gr574383: no stated subject — the last non-test commit labels the
    squash, never the red-tests-first one."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    seams["subjects"] = [
        "test(x): red tests for the thing (td42)",
        "feat(x): the thing (td42)",
    ]
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None
    [(argv, _kw)] = seams["launch"]
    assert argv[2] == "feat(x): the thing (td42)"
    assert handle["subject"] == argv[2]


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


def test_poll_green_logs_the_land_on_the_plan_quest(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr548800: a landed slice appends a milestone logbook entry naming its
    plan root — here via the seam (the fake store has no quest lookup)."""
    seen: list[dict[str, Any]] = []

    def fake_log(store: Any, node_id: int, event: str, **kw: Any) -> int:
        seen.append({"node": node_id, "event": event, "kw": kw})
        return 1

    monkeypatch.setattr(land_slice, "log_plan_event", fake_log)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub", "plan:40"))
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seen == [
        {
            "node": 42,
            "event": f"slice td42 landed {FULL_SHA[:9]} — {SUBJECT}",
            "kw": {"entry_type": "milestone", "root": 40},
        }
    ]


def test_poll_green_moves_the_plans_gripe_to_verify(seams: dict[str, Any]) -> None:
    """gr561932: the land that fills the plan's sha-in-prod gate is the fix,
    so the gripe the plan is about gets `resolved: <sha>` and STATUS:verify
    (gripe_validate closes it once the sha is on prod). Idempotent; a
    settled gripe is left alone."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub", "plan:40"))
    store.add_todo(
        44,
        status="open",
        tags=("plan:40",),
        meta={"gate": {"kind": "sha-in-prod", "args": {"sha": PENDING_SHA}}},
    )
    store.refs[7] = _Ref(7, "gripe 7", {})
    store.tags[7] = [Tag.closed("STATUS", "triaged")]
    store.chunks.rows[7] = [_Chunk(1, "triage note", "gripe_comment")]
    seams["gates"] = [(44, True)]
    seams["gripe"] = 7
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert "STATUS:verify" in store.tag_strings(7)
    newest = store.chunks.rows[7][-1]
    assert newest.chunk_kind == "gripe_comment"
    assert newest.text.startswith(
        f"resolved: {FULL_SHA[:9]} (main, release pending) — {SUBJECT}"
    )
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "gripe gr7: resolved comment, STATUS:verify written" in summary

    # A second land on the same plan (a fix slice) does not repeat the comment.
    store.tags[42] = [Tag.closed("STATUS", "doing"), Tag.open("plan:40")]
    store.chunks.rows[42] = []
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert [c.text for c in store.chunks.rows[7]].count(newest.text) == 1
    assert (
        "gripe gr7: left STATUS:verify"
        in [t for k, t in pctx.chunks if k == "job_summary"][0]
    )


def test_poll_green_without_a_gate_leaves_the_gripe_alone(
    seams: dict[str, Any],
) -> None:
    """A brief node's land (a spec test, no gate blocked-by it) is not the fix."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub", "plan:40"))
    store.refs[7] = _Ref(7, "gripe 7", {})
    store.tags[7] = [Tag.closed("STATUS", "triaged")]
    store.chunks.rows[7] = []
    seams["gripe"] = 7
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert "STATUS:triaged" in store.tag_strings(7)
    assert store.chunks.rows[7] == []


def test_poll_nothing_new_lands_no_sha(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr561142: a green ship that squashed nothing closes the todo with the
    no-diff note; main's head never reaches the todo, the gate or the
    logbook."""
    seen: list[dict[str, Any]] = []

    def fake_log(store: Any, node_id: int, event: str, **kw: Any) -> int:
        seen.append({"node": node_id, "event": event, "kw": kw})
        return 1

    monkeypatch.setattr(land_slice, "log_plan_event", fake_log)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub", "plan:40"))
    store.add_todo(
        44,
        status="open",
        tags=("plan:40",),
        meta={"gate": {"kind": "sha-in-prod", "args": {"sha": PENDING_SHA}}},
    )
    seams["gates"] = [(44, True)]
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, NO_DIFF_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert pctx.meta_set["landed_sha"] is None
    assert store.comments(42)[-1] == land_slice.NO_DIFF_NOTE
    tags = store.tag_strings(42)
    assert "STATUS:done" in tags and "claimed-by:lane-claude-sub" not in tags
    # the gate keeps its pending sha
    assert store.refs[44].meta["gate"]["args"]["sha"] == PENDING_SHA
    assert store.comments(44) == []
    assert seen == [
        {
            "node": 42,
            "event": f"slice td42 {land_slice.NO_DIFF_NOTE}",
            "kw": {"entry_type": "observation", "root": 40},
        }
    ]
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "e249413a7" not in summary
    assert "no net diff" in summary and "gate td44: left pending" in summary
    # the branch and worktree are still cleaned up
    assert ["branch", "-D", "slice/42"] in _cleanup_cmds(seams)


def test_poll_no_diff_reland_fills_the_gate_from_the_landed_comment(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr576154: a reland after a land that pushed but died before its gate
    write squashes nothing — the pending gate still gets the sha the
    todo's own ``landed:`` comment names."""
    monkeypatch.setattr(land_slice, "log_plan_event", lambda *a, **k: 1)
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub", "plan:40"),
        comments=(f"landed: {FULL_SHA[:9]} — {SUBJECT}",),
    )
    store.add_todo(
        44,
        status="open",
        tags=("plan:40",),
        meta={"gate": {"kind": "sha-in-prod", "args": {"sha": PENDING_SHA}}},
    )
    seams["gates"] = [(44, True)]
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, NO_DIFF_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert pctx.meta_set["landed_sha"] is None
    assert pctx.meta_set["gate_id"] == 44
    # the gate gets the prior land's sha — the one the todo's comment names
    assert store.refs[44].meta["gate"]["args"]["sha"] == FULL_SHA[:9]
    assert store.comments(44)[-1] == f"sha: {FULL_SHA[:9]} — landed by td42"
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "from the todo's landed comment" in summary


def test_poll_no_diff_reland_after_a_revert_leaves_the_gate_pending(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td626034: a REVERT verdict resets the plan's gate and requeues the
    todo with a ``handoff: review: REVERT`` comment; the rebuild's no-diff
    reland must NOT recover the old ``landed:`` sha — that comment names
    the commit the revert took off main."""
    monkeypatch.setattr(land_slice, "log_plan_event", lambda *a, **k: 1)
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub", "plan:40"),
        comments=(
            f"landed: {FULL_SHA[:9]} — {SUBJECT}",
            "handoff: review: REVERT — breaks prod\n\n"
            "(review td7; reverted by td9 — rebuild after it lands, from "
            "the brief, with this finding in mind)",
        ),
    )
    store.add_todo(
        44,
        status="open",
        tags=("plan:40",),
        meta={"gate": {"kind": "sha-in-prod", "args": {"sha": PENDING_SHA}}},
    )
    seams["gates"] = [(44, True)]
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, NO_DIFF_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert pctx.meta_set["landed_sha"] is None
    assert pctx.meta_set["gate_id"] == 44
    # the reverted sha is not recovered into the reset gate
    assert store.refs[44].meta["gate"]["args"]["sha"] == PENDING_SHA
    assert store.comments(44) == []
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "left pending" in summary


def test_poll_no_diff_reland_recovers_a_landed_comment_newer_than_the_revert(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary is the REVERT handoff itself: a ``landed:`` comment
    NEWER than it — the rebuild's own land, its gate write lost — is
    still the sha the gate waits on (gr576154)."""
    monkeypatch.setattr(land_slice, "log_plan_event", lambda *a, **k: 1)
    rebuild_sha = "d4e5f6a" + "0" * 33
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub", "plan:40"),
        comments=(
            f"landed: {FULL_SHA[:9]} — {SUBJECT}",
            "handoff: review: REVERT — breaks prod\n\n"
            "(review td7; reverted by td9 — rebuild after it lands, from "
            "the brief, with this finding in mind)",
            f"landed: {rebuild_sha[:9]} — {SUBJECT}",
        ),
    )
    store.add_todo(
        44,
        status="open",
        tags=("plan:40",),
        meta={"gate": {"kind": "sha-in-prod", "args": {"sha": PENDING_SHA}}},
    )
    seams["gates"] = [(44, True)]
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, NO_DIFF_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert pctx.meta_set["gate_id"] == 44
    # the post-REVERT land's sha is the recoverable one
    assert store.refs[44].meta["gate"]["args"]["sha"] == rebuild_sha[:9]
    assert store.comments(44)[-1] == f"sha: {rebuild_sha[:9]} — landed by td42"
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "from the todo's landed comment" in summary


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


# ── stack eviction (gr570133) ──────────────────────────────────────


def test_poll_green_evicts_the_slice_stack(seams: dict[str, Any]) -> None:
    """gr570133: a landed slice's warm compose stack comes down (-v —
    the tree goes too) BEFORE `git worktree remove`; a left-up project
    kept a postgres and a sleep-infinity gate on the docker VM."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["evict"] == [("precis-test-slice-td42", True)]
    cleanup = pctx.meta_set["cleanup"]
    assert "compose down: ok" in cleanup
    # the stack is downed before the worktree goes, in the notes' order
    assert cleanup.index("compose down: ok") < cleanup.index("worktree remove: ok")
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "compose down: ok" in summary


def test_cleanup_down_failure_is_a_note(seams: dict[str, Any]) -> None:
    """A failed `compose down` is a cleanup note, never a job failure —
    the land is real either way."""
    seams["evict_ok"] = False
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    cleanup = pctx.meta_set["cleanup"]
    assert any(n.startswith("compose down: FAILED") for n in cleanup)
    # the rest of cleanup still ran
    assert ["worktree", "remove", str(seams["tree"])] in _cleanup_cmds(seams)
    assert "compose down: ok" not in cleanup


def test_cleanup_skips_a_non_slice_tree_name(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """A dep_check-pinned worktree can name a project outside
    precis-test-slice-*: evict_project's refusal is by design, so the
    cleanup note says 'skipped', never 'FAILED'."""
    tree = tmp_path / ".claude" / "worktrees" / "pr-7-relock"
    (tree / "scratch").mkdir(parents=True)
    seams["tree"] = tree
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seams["evict"] == []
    cleanup = pctx.meta_set["cleanup"]
    assert any(
        n == "compose down: skipped (not a slice project: precis-test-pr-7-relock)"
        for n in cleanup
    )
    assert not any("compose down: FAILED" in n for n in cleanup)


def test_launch_sweeps_other_stacks_and_keeps_own(seams: dict[str, Any]) -> None:
    """Every ship launch first sweeps the host's finished slice stacks,
    keeping this tree's project; the evictions are one job event."""
    seams["evicted_stacks"] = ["precis-test-slice-td99"]
    store = _Store()
    store.add_todo(42)
    ctx, _handle = _submit(store, seams)
    assert seams["swept"] == [seams["tree"]]
    events = [t for k, t in ctx.chunks if k == "job_event"]
    assert "land_slice: evicted idle slice stacks: precis-test-slice-td99" in events


def test_poll_red_lint_fails_with_the_cross_line(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        1,
        "ruff format --check\n"
        "All checks passed!\n"
        'tests/test_se_flow.py:450: error: Need type annotation for "memo"  '
        "[var-annotated]\n"
        "tests/precis_web/test_se_flow_tab.py:193: error: Argument 1 to "
        '"player_payload" has incompatible type "None"  [arg-type]\n'
        "Found 2 errors in 2 files (checked 4195 source files)\n"
        "\x1b[31m✖ ruff format: 1 file would be reformatted\x1b[0m\n",
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
    handoff = store.comments(42)[-1]
    assert handoff.startswith("handoff: land FAILED on slice/42: ✖")
    # td565331: the gate's own error lines ride the handoff, so the next
    # session's brief names what to fix, not only the verdict line.
    assert "The gate's error lines:" in handoff
    assert (
        'tests/test_se_flow.py:450: error: Need type annotation for "memo"' in handoff
    )
    assert "test_se_flow_tab.py:193: error:" in handoff
    assert "Found 2 errors" not in handoff and "All checks passed" not in handoff
    # the branch and worktree stay for the fix round
    assert _cleanup_cmds(seams) == []
    assert "landed_sha" not in pctx.meta_set


# ── the failing lines survive (gr573347 a) ─────────────────────────

#: The pinned ruff's default FULL format (0.17): ``CODE [*] message``,
#: then `` --> path:L:C`` — the concise ``path:N:M: CODE`` shape the error
#: regex was written for never appears, so a ruff red reached the todo as
#: the bare ✖ verdict and nothing else.
RUFF_FULL_LOG = (
    "quick ship (--quick)\n"
    "ruff check .\n"
    "F401 [*] `os` imported but unused\n"
    " --> src/foo.py:1:8\n"
    "  |\n"
    "1 | import os\n"
    "  |        ^^\n"
    "  = help: Remove unused import `os`\n"
    "Found 1 error.\n"
    "[*] 1 fixable with the `--fix` option.\n"
    "\x1b[31m✖ ruff: 1 error\x1b[0m\n"
)


def test_poll_red_keeps_failing_lines_in_summary(seams: dict[str, Any]) -> None:
    """gr573347 a: the land log dies with the worktree, so the failing
    lines ride the job summary and the handoff — ruff's full format
    included, condensed to one ``path:L:C CODE message`` line each."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, RUFF_FULL_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "src/foo.py:1:8: F401 `os` imported but unused" in summary
    handoff = store.comments(42)[-1]
    assert "src/foo.py:1:8: F401 `os` imported but unused" in handoff


def test_poll_red_without_error_lines_keeps_the_excerpt(
    seams: dict[str, Any],
) -> None:
    """A red whose log holds no parseable error line still hands the next
    session the tool output before the ✖ — the step banner and the last
    lines — on both the summary and the handoff."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    _finish(
        handle,
        1,
        "quick ship (--quick)\n"
        "▶ light gate — ruff · mypy · contracts\n"
        "gate slot 2 of 4; db sync 41.2s\n"
        "docker: unexpected EOF reading layer\n"
        "\x1b[31m✖ light gate is RED — not shipping.\x1b[0m\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "docker: unexpected EOF reading layer" in summary
    handoff = store.comments(42)[-1]
    assert "The gate's output before the verdict:" in handoff
    assert "gate slot 2 of 4; db sync 41.2s" in handoff
    assert "docker: unexpected EOF reading layer" in handoff


def test_gate_red_excerpt_shape() -> None:
    """The excerpt is the non-blank lines before the LAST ✖ (not the ✖
    itself — both surfaces already carry it), capped at 40 lines and
    3000 chars; no ✖, no excerpt."""
    assert land_slice.gate_red_excerpt("no cross\nlines here\n") == ""
    log = "".join(f"line {i}\n" for i in range(50)) + "✖ red\n"
    excerpt = land_slice.gate_red_excerpt(log)
    lines = excerpt.splitlines()
    assert len(lines) == 40
    assert lines[0] == "line 10" and lines[-1] == "line 49"
    assert "✖" not in excerpt
    wide = ("x" * 4000 + "\n") * 3 + "✖ red\n"
    assert len(land_slice.gate_red_excerpt(wide)) <= 3010


# ── the failing ids and whether they are the branch's (gr573347 b) ──


def test_failed_test_ids_summary_block_only() -> None:
    """Ids come only from the ``=== short test summary info ===`` block:
    an unindented captured-stdout line outside it (or a quoted/indented
    one inside a test body) cannot inject a failure. The id is the whole
    token up to the first `` - `` OUTSIDE square brackets, so a
    parametrized id with spaces — or with `` - `` inside them — survives,
    and collection ``ERROR`` lines count too."""
    log = (
        "FAILED tests/fake.py::test_injected - stdout leaked unindented\n"
        "=== short test summary info ===\n"
        "FAILED tests/a.py::test_one - AssertionError\n"
        "FAILED tests/b.py::test_two[param with space] - RuntimeError\n"
        "FAILED tests/c.py::t[a - b] - AssertionError\n"
        "FAILED tests/a.py::test_one - AssertionError\n"
        "ERROR tests/e.py::test_e - ImportError: boom\n"
        'E   assert "FAILED tests/q.py::test_quoted - x" in captured\n'
        "   FAILED tests/indented.py::test_no - quoted in a body\n"
        "=== 3 failed, 1 error in 30.00s ===\n"
        "FAILED tests/late.py::test_after - outside the block\n"
    )
    assert land_slice.failed_test_ids(log) == [
        "tests/a.py::test_one",
        "tests/b.py::test_two[param with space]",
        "tests/c.py::t[a - b]",
        "tests/e.py::test_e",
    ]
    assert land_slice.failed_test_ids("no failures\n") == []
    # no banner, no ids — even for a real FAILED line
    assert land_slice.failed_test_ids("FAILED tests/a.py::t - x\n") == []


def test_failed_test_ids_malformed_line_spoils_the_set() -> None:
    """Unbalanced brackets or an id without ``::`` means the parse can't
    be trusted: ``[]`` → no outside set → the ordinary red."""
    banner = "=== short test summary info ===\n"
    assert (
        land_slice.failed_test_ids(
            banner + "FAILED tests/a.py::t[a - b - AssertionError\n"
        )
        == []
    )
    assert (
        land_slice.failed_test_ids(
            banner + "FAILED tests/a.py::t]x[ - AssertionError\n"
        )
        == []
    )
    assert land_slice.failed_test_ids(banner + "FAILED tests/a.py - no colons\n") == []
    # a bare-path ERROR (collection failure) is no usable id either
    assert land_slice.failed_test_ids(banner + "ERROR tests/e.py - ImportError\n") == []
    # one bad line spoils the good ones too
    assert (
        land_slice.failed_test_ids(
            banner + "FAILED tests/a.py::t - x\n" + "FAILED tests/nop.py - no colons\n"
        )
        == []
    )


def test_outside_diff_ids_filters_by_the_diff(seams: dict[str, Any]) -> None:
    seams["diff_names"] = "tests/a.py\nsrc/precis/x.py\n"
    ids = ["tests/a.py::t1", "tests/b.py::t2", "tests/a.py::t3[param]"]
    assert land_slice.outside_diff_ids(seams["tree"], ids) == ["tests/b.py::t2"]
    [args, _cwd] = next(c for c in seams["git"] if c[0][:2] == ["diff", "--name-only"])
    assert args == ["diff", "--name-only", "origin/main...HEAD"]


def test_outside_diff_ids_unanswerable_diff_is_empty(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diff git cannot answer verifies nothing — the red stays
    ordinary."""
    monkeypatch.setattr(land_slice, "_git", lambda a, c: _proc(rc=2))
    assert land_slice.outside_diff_ids(seams["tree"], ["tests/b.py::t2"]) == []


FULL_REFUSAL_LOG = (
    "checked ship (--checked)\n"
    "2144 passed, 75 skipped, 2 warnings in 205.62s (0:03:25)\n"
    "\x1b[31m✖ the diff touches the harness/schema — the selector answers FULL, "
    "which the checked lane cannot run. Land with --quick (lint only; the "
    "release gate covers it) or a local full gate (--full / --impacted).\x1b[0m\n"
)


def test_poll_full_selector_refusal_relaunches_quick(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reto 2026-11-05 (me570388, gr570221): a migration or harness diff makes
    # the selector answer FULL, which --checked cannot run by design. The lane
    # is refused, not the branch, so the land goes through the quick lane
    # instead of bouncing the todo to a builder (jo570215 → jo570217 "fixed"
    # the migration it was told nothing about).
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    # A --quick default land never runs the selector: only a checked run
    # (params.checked, e.g. a PR land) can take the FULL refusal.
    ctx = _Ctx(store, {**_params(seams), "checked": True})
    handle = land_slice.submit(ctx, None)
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.status is None and pctx.failure is None
    [(first, _kw1), (second, _kw2)] = seams["launch"]
    assert first[1] == "--checked"
    assert second[1] == "--quick" and second[2] == SUBJECT
    assert handle["flag"] == "--quick"
    assert pctx.meta_set["compute_handle"]["flag"] == "--quick"
    assert pctx.meta_set["compute_handle_released"] is False
    tags = store.tag_strings(42)
    assert not any(t.startswith("attempt:") for t in tags)
    assert "claimed-by:lane-claude-sub" in tags
    assert not any(c.startswith("handoff: land FAILED") for c in store.comments(42))
    events = [t for k, t in pctx.chunks if k == "job_event"]
    assert any("selector answers FULL" in e and "--quick" in e for e in events)


def test_poll_full_selector_fallback_notes_the_quick_land(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td573580 review: when the FULL selector sends a checked land through
    --quick, the todo's receipt says so — a bare ``landed:`` comment would
    read as a checked land whose suite ran."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, {**_params(seams), "checked": True})
    handle = land_slice.submit(ctx, None)
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert handle["fell_back_full"] is True  # carried onto the relaunch
    _finish(handle, 0, GREEN_LOG)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert any("selector FULL" in c and "--quick" in c for c in store.comments(42))


def test_poll_full_selector_refusal_is_red_when_fallback_off(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_LAND_FULL_FALLBACK", "off")
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, {**_params(seams), "checked": True})
    handle = land_slice.submit(ctx, None)
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "selector answers FULL" in (pctx.failure or "")
    assert len(seams["launch"]) == 1


def test_poll_full_selector_refusal_on_quick_does_not_loop(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    handle["flag"] = "--quick"
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert len(seams["launch"]) == 1


# ── engine lands (td574937 E1 + gr599427) ──────────────────────────
#
# The submit stamps ``meta.engine_paths`` on its job — always, so a job
# with no stamp is a pre-E1 one. An engine diff lands ``--checked`` only
# when the selector's answer is a small spot selection (gr599427, Reto
# 2026-11-08 — lands run basic checks; GitHub CI is the gate). A
# FULL-selector refusal on such a land relaunches ``--quick`` like every
# other land — never ``--full``.


def test_engine_diff_lands_checked(seams: dict[str, Any]) -> None:
    seams["changed_paths"] = ["scripts/ship", "src/precis/x.py"]
    store = _Store()
    store.add_todo(42)
    # ``checked`` says quick; the engine diff asks for checked, and the
    # stubbed spot verdict earns it (gr599427).
    ctx = _Ctx(store, {**_params(seams), "checked": False})
    handle = land_slice.submit(ctx, None)
    assert handle is not None and ctx.failure is None
    [(argv, _kw)] = seams["launch"]
    assert argv[1] == "--checked"
    assert ctx.meta_set["engine_paths"] == ["scripts/ship"]


def test_non_engine_land_is_unchanged(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42)
    ctx, _handle = _submit(store, seams)
    [(argv, _kw)] = seams["launch"]
    assert argv[1] == "--quick"
    # The stamp is always written: an empty list is a checked non-engine
    # land, distinct from a pre-E1 job carrying no stamp at all.
    assert ctx.meta_set["engine_paths"] == []


def test_unreadable_diff_lands_checked(seams: dict[str, Any]) -> None:
    """A diff git cannot read is treated as engine — and an engine diff
    with a spot verdict earns ``--checked`` (gr599427)."""
    seams["changed_paths"] = None
    store = _Store()
    store.add_todo(42)
    ctx, _handle = _submit(store, seams)
    [(argv, _kw)] = seams["launch"]
    assert argv[1] == "--checked"
    assert ctx.meta_set["engine_paths"] == ["<unreadable>"]


def test_full_refusal_on_an_engine_land_relaunches_quick(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr599427 (Reto 2026-11-08): an engine land's FULL-selector refusal
    relaunches ``--quick`` like every other land — no land runs the full
    local gate; the release gate's GitHub CI runs the suite."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    seams["changed_paths"] = ["scripts/ship"]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = ["scripts/ship"]
    assert land_slice.poll(pctx, handle) is False
    assert pctx.status is None and pctx.failure is None
    [(first, _kw1), (second, _kw2)] = seams["launch"]
    assert first[1] == "--checked"
    assert second[1] == "--quick" and second[2] == SUBJECT
    assert handle["flag"] == "--quick"
    assert handle["fell_back_full"] is True
    assert all("--full" not in c[0] for c in seams["launch"])
    events = [t for k, t in pctx.chunks if k == "job_event"]
    assert any("selector answers FULL" in e and "--quick" in e for e in events)


def test_engine_land_full_fallback_follows_the_env_flag(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr599427: ``PRECIS_LAND_FULL_FALLBACK=off`` keeps a FULL refusal a
    red for an engine land too — the same fallback rule as every land."""
    monkeypatch.setenv("PRECIS_LAND_FULL_FALLBACK", "off")
    seams["changed_paths"] = ["scripts/ship"]
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = ["scripts/ship"]
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "selector answers FULL" in (pctx.failure or "")
    assert [c[0][1] for c in seams["launch"]] == ["--checked"]


def test_engine_full_refusal_does_not_loop(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relaunched run that still draws the FULL refusal is the ordinary
    red — the relaunch happens once."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    seams["changed_paths"] = ["scripts/ship"]
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = ["scripts/ship"]
    assert land_slice.poll(pctx, handle) is False
    _finish(handle, 1, FULL_REFUSAL_LOG)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert len(seams["launch"]) == 2
    assert all("--full" not in c[0] for c in seams["launch"])


def test_poll_red_without_a_cross_line_names_the_exit(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, "SHIP_STATUS: conflict\n")
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.failure is not None and "SHIP_STATUS conflict" in pctx.failure


# ── infra reds (gr570187) ──────────────────────────────────────────


def test_infra_red_marks() -> None:
    """The gate's own machinery dying — the gate-kill lines
    :func:`gate_kill_line` anchors (the exit-137 ✖ and the xdist worker
    crash) or a bare exit 137 (128+SIGKILL) — classes the red as gate
    infrastructure; a branch red does not. xdist's ``[gwN] node down:``
    line is NOT a mark: it is the ordinary per-worker death report and
    rides the worker-crash rule, not the infra path."""
    oom = (
        "pytest -n3 --impacted\n"
        "\x1b[31m✖ the gate container exited 137 (SIGKILL) — almost always "
        "the OOM killer on its mem_limit (24 GiB, docker/dev/compose.yaml), "
        "not a verdict on the diff.\x1b[0m\n"
    )
    assert land_slice.infra_red(oom, 1) == "gate infrastructure"
    worker = "worker 'gw2' crashed while running 'tests/test_x.py::test_y'\n"
    assert land_slice.infra_red(worker, 1) == "gate infrastructure"
    # xdist emits "[gwN] node down:" for every dead worker — ordinary,
    # not infra — even when failure output merely quotes it.
    node_down = "[gw2] node down: Not properly terminated\n"
    assert land_slice.infra_red(node_down, 1) is None
    assert land_slice.infra_red("pytest -n3\nKilled\n", 137) == "gate infrastructure"
    assert (
        land_slice.infra_red("\x1b[31m✖ ruff: 2 errors in src/x.py\x1b[0m\n", 1) is None
    )
    assert land_slice.infra_red("SHIP_STATUS: conflict\n", 0) is None


def test_poll_bare_exit_137_defers_then_the_ask(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """gr570187: a bare exit 137 — the OOM signature with no kill line —
    follows the marked kill's path exactly: it defers under
    ``gate_kills`` at the short backoff (no attempt, no handoff), and
    past :data:`MAX_GATE_KILL_RETRIES` fails attempt-free with
    ``meta.land_again_cap`` — the ask, never an automatic re-land."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 137, "pytest -n3 --impacted\nKilled\n")
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.status is None and pctx.failure is None
    assert handle["gate_kills"] == 1
    assert "main_refusals" not in handle
    assert handle["not_before"] == "2026-11-01T10:02:00Z"  # 120 s, not 30 min
    assert handle["last_refusal"].startswith(land_slice.GATE_KILL_CROSS)
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))
    assert store.comments(42) == []
    # At the backoff the land relaunches; the same bare 137 again, past
    # the cap: still outside the diff (no attempt) but spent — the ask.
    clock["t"] = T0 + timedelta(seconds=120)
    assert land_slice.poll(pctx, handle) is False
    assert len(seams["launch"]) == 2 and handle["gate_kills"] == 1
    handle["gate_kills"] = land_slice.MAX_GATE_KILL_RETRIES
    _finish(handle, 137, "pytest -n3 --impacted\nKilled\n")
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    n = land_slice.MAX_GATE_KILL_RETRIES + 1
    assert (
        f"land blocked by gate kills for {n} retries: "
        "gate killed (exit 137, VM memory)" in (pctx.failure or "")
    )
    assert pctx.meta_set["attempt_free"] == "infra-land"
    assert pctx.meta_set["land_again_cap"] is True
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags
    assert not any(t.startswith("attempt:") for t in tags)
    [handoff] = store.comments(42)
    assert handoff.startswith("handoff: land BLOCKED on slice/42:")
    assert "the docker VM's memory or the branch's own memory use" in handoff
    assert "not at fault" not in handoff


# ── main-side refusals (gr549328) ──────────────────────────────────

DRIFT_LOG = (
    "quick ship (--quick)\n"
    "\x1b[31m✖ main has no all-green shard matrix in the last 48h (refuse at "
    "48h). The burst has outrun its verdicts. Deliberate override: "
    "PRECIS_QLAND_DRIFT_OVERRIDE=1.\x1b[0m\n"
    "SHIP_STATUS: error\n"
)


def test_is_main_refusal_marks() -> None:
    none_green = land_slice.cross_line(DRIFT_LOG)
    stale = land_slice.cross_line(
        "\x1b[31m✖ main's last all-green shard matrix is 60.1h old (refuse at 48h)."
        "\x1b[0m\n"
    )
    assert none_green is not None and none_green.startswith("✖ main has no")
    assert stale is not None and stale.startswith("✖ main's last all-green")
    assert land_slice.is_main_refusal(none_green) is True
    assert land_slice.is_main_refusal(stale) is True
    assert (
        land_slice.is_main_refusal("✖ ruff format: 1 file would be reformatted")
        is False
    )
    assert land_slice.is_main_refusal("✖ release in flight") is False
    assert land_slice.is_main_refusal(None) is False
    assert land_slice.is_main_refusal("") is False


def _defer_setup(
    seams: dict[str, Any], refusals: int | None = None
) -> tuple[_Store, dict[str, Any]]:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    assert handle["flag"] == "--quick"
    if refusals is not None:
        handle["main_refusals"] = refusals
    _finish(handle, 1, DRIFT_LOG)
    return store, handle


T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    now = {"t": T0}
    monkeypatch.setattr(land_slice, "_now", lambda: now["t"])
    monkeypatch.delenv("PRECIS_LAND_REFUSAL_BACKOFF_S", raising=False)
    monkeypatch.delenv("PRECIS_LAND_GATE_KILL_BACKOFF_S", raising=False)
    return now


def test_refusal_backoff_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_LAND_REFUSAL_BACKOFF_S", raising=False)
    assert land_slice.MAIN_REFUSAL_BACKOFF_S == 1800
    assert land_slice.refusal_backoff_s() == 1800
    monkeypatch.setenv("PRECIS_LAND_REFUSAL_BACKOFF_S", "60")
    assert land_slice.refusal_backoff_s() == 60
    monkeypatch.setenv("PRECIS_LAND_REFUSAL_BACKOFF_S", "soon")
    assert land_slice.refusal_backoff_s() == 1800


def test_poll_main_refusal_defers_without_launching(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    store, handle = _defer_setup(seams)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert len(seams["launch"]) == 1  # the original submit only
    assert pctx.status is None and pctx.failure is None
    assert handle["main_refusals"] == 1
    assert handle["not_before"] == "2026-11-01T10:30:00Z"
    assert handle["last_refusal"].startswith("✖ main has no all-green shard matrix")
    # the dead ship's pid, identity and exit file are gone
    assert not {"pid", "pgid", "boot_id", "start_time"} & handle.keys()
    assert not Path(handle["exit_file"]).exists()
    assert pctx.meta_set["compute_handle"] == handle
    assert pctx.meta_set["compute_handle_released"] is False
    # parked time is not charged to the ssh_node wall deadline
    assert (
        pctx.meta_set["deadline"] == T0.timestamp() + 1800 + land_slice.wall_seconds()
    )
    [(kind, text)] = pctx.chunks
    assert kind == "job_event"
    assert text.startswith("land_slice: deferred (main-side refusal #1): ✖ main has no")
    assert text.endswith("scripts/ship again after 10:30Z")
    # the todo is untouched: still claimed, no attempt bump, no handoff
    tags = store.tag_strings(42)
    assert "claimed-by:lane-claude-sub" in tags
    assert not any(t.startswith("attempt:") for t in tags)
    assert store.comments(42) == []
    assert "exit_code" not in pctx.meta_set


def test_poll_main_refusal_records_the_drift_probe(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """gr550941: the deferral event carries what the lander's own identity saw
    of main's gate, so a blind token and a red window read differently. Exit 2
    (looked, none green) is ship's refusal to make: the launch goes ahead."""
    seams["preflight"] = _preflight(2, "", NONE_GREEN_DIAG)
    store, handle = _defer_setup(seams)
    assert len(seams["launch"]) == 1  # the preflight did not stop the launch
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert seams["probed"] == [seams["tree"], seams["tree"]]  # launch + deferral
    [(kind, text)] = pctx.chunks
    assert kind == "job_event"
    first, probe = text.split("\n", 1)
    assert first.endswith("scripts/ship again after 10:30Z")
    assert probe == f"drift probe (lander identity): {NONE_GREEN_DIAG}"


def test_submit_records_the_preflight_when_the_land_goes_on(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """A verdict (or the helper's own none-green) launches ship, and the job
    says what the lander's identity saw: jo550940 launched nine times without
    ever recording which answer let it through."""
    found = "last-gated-main-sha: status=found local=40 fetched=40 with_suites=40 "
    found += "with_runs=40 matched=40 runs: lint=SUCCESS (exit 0)"
    seams["preflight"] = _preflight(0, "3.2", found)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert handle["pid"] == 5151 and len(seams["launch"]) == 1
    kinds = [k for k, _ in ctx.chunks]
    assert kinds == ["job_event", "job_event"]
    assert ctx.chunks[0][1] == (
        f"land_slice: drift preflight (lander identity): age=3.2; {found}"
    )
    assert ctx.chunks[1][1].startswith("land_slice: scripts/ship --quick")
    # the helper's own none-green: ship makes the refusal, the job still says so
    seams["preflight"] = _preflight(2, "", NONE_GREEN_DIAG)
    seams["launch"].clear()
    store2 = _Store()
    store2.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx2, handle2 = _submit(store2, seams)
    assert handle2["pid"] == 5151
    assert ctx2.chunks[0][1].endswith(f"age=-; {NONE_GREEN_DIAG}")
    # no helper in the tree: nothing to say
    seams["preflight"] = {"ran": False, "rc": None, "age": "", "diag": ""}
    store3 = _Store()
    store3.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx3, _handle3 = _submit(store3, seams)
    assert [k for k, _ in ctx3.chunks] == ["job_event"]
    assert ctx3.chunks[0][1].startswith("land_slice: scripts/ship")


def test_gate_unknown_rules() -> None:
    gu = land_slice._gate_unknown
    assert not gu({"ran": False, "rc": None, "age": "", "diag": ""})  # no helper
    assert not gu(_preflight(0, "3.2", "found"))  # a verdict: ship judges its age
    assert not gu(_preflight(2, "", NONE_GREEN_DIAG))  # looked, none: ship refuses
    assert gu(_preflight(0, "", BLIND_DIAG))  # blind identity
    assert gu(_preflight(None, "", "helper did not answer (TimeoutExpired)"))
    assert gu(_preflight(1, "", "(no diag line; stderr: -) (exit 1)"))  # crashed
    # exit 2 without the helper's diag is uv's or the interpreter's, not a verdict
    assert gu(_preflight(2, "", "(no diag line; stderr: error: cache) (exit 2)"))


def test_submit_parks_when_the_lander_identity_cannot_read_the_gate(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """Fail closed (gr550941): ship treats an unknowable drift as "do not
    refuse"; an unattended land must not ride that. The lander parks before
    launching, under the main-side refusal backoff, no attempt burned."""
    seams["preflight"] = _preflight(0, "", BLIND_DIAG)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert seams["launch"] == []  # ship never ran
    assert seams["probed"] == [seams["tree"]]
    assert handle["not_before"] == "2026-11-01T10:30:00Z"
    assert handle["main_refusals"] == 1
    assert handle["last_refusal"] == land_slice.UNKNOWN_GATE_CROSS
    assert handle["flag"] == "--quick" and handle["subject"] == SUBJECT
    assert handle["worktree"] == str(seams["tree"])
    assert "pid" not in handle
    assert ctx.meta_set["compute_handle"] == handle
    assert ctx.meta_set["compute_handle_released"] is False
    assert ctx.meta_set["land_subject"] == SUBJECT
    assert ctx.meta_set["deadline"] == T0.timestamp() + 1800 + land_slice.wall_seconds()
    [(kind, text)] = ctx.chunks
    assert kind == "job_event"
    assert text.startswith(
        "land_slice: deferred (main-side refusal #1): " + land_slice.UNKNOWN_GATE_CROSS
    )
    assert text.endswith(f"\ndrift probe (lander identity): {BLIND_DIAG}")
    assert land_slice.is_main_refusal(land_slice.UNKNOWN_GATE_CROSS)
    tags = store.tag_strings(42)
    assert "claimed-by:lane-claude-sub" in tags
    assert not any(t.startswith("attempt:") for t in tags)
    assert store.comments(42) == [] and ctx.status is None
    # polls before the park ends do nothing
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.chunks == [] and seams["launch"] == []


def test_relaunch_parks_again_while_the_gate_stays_unknown(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    store, handle = _defer_setup(seams)
    assert land_slice.poll(_Ctx(store, _params(seams)), handle) is False
    assert handle["main_refusals"] == 1
    seams["preflight"] = _preflight(0, "", BLIND_DIAG)
    clock["t"] = T0 + timedelta(seconds=1800)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert len(seams["launch"]) == 1  # no second ship
    assert handle["main_refusals"] == 2
    assert handle["not_before"] == "2026-11-01T11:00:00Z"
    assert handle["last_refusal"] == land_slice.UNKNOWN_GATE_CROSS
    assert handle["flag"] == "--quick" and handle["subject"] == SUBJECT
    assert pctx.meta_set["compute_handle"] == handle
    due = T0 + timedelta(seconds=1800)
    assert (
        pctx.meta_set["deadline"] == due.timestamp() + 1800 + land_slice.wall_seconds()
    )
    [(kind, text)] = pctx.chunks
    assert kind == "job_event" and "refusal #2" in text and BLIND_DIAG in text
    # the identity can see the gate again: the relaunch goes ahead
    seams["preflight"] = _preflight(0, "3.2", "found")
    clock["t"] = T0 + timedelta(seconds=3600)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert len(seams["launch"]) == 2
    assert handle["pid"] == 5152 and "not_before" not in handle
    assert handle["main_refusals"] == 2  # carried, not reset


def test_preflight_past_the_cap_fails_blocked_without_a_launch(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    store, handle = _defer_setup(seams, refusals=land_slice.MAX_MAIN_REFUSALS - 1)
    assert land_slice.poll(_Ctx(store, _params(seams)), handle) is False
    assert handle["main_refusals"] == land_slice.MAX_MAIN_REFUSALS
    seams["preflight"] = _preflight(0, "", BLIND_DIAG)
    clock["t"] = T0 + timedelta(seconds=1800)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert len(seams["launch"]) == 1
    assert pctx.status == "failed"
    assert pctx.failure is not None
    assert (
        f"land blocked by main for {land_slice.MAX_MAIN_REFUSALS + 1} refusals: "
        + land_slice.UNKNOWN_GATE_CROSS
    ) in pctx.failure
    assert any("land BLOCKED on slice/42" in c for c in store.comments(42))
    [(kind, text)] = pctx.chunks
    assert kind == "job_summary" and BLIND_DIAG in text


def test_drift_preflight_prefers_mains_helper_over_the_branchs(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """jo550940 event 6: the slice branch's helper predated --diag. The
    preflight runs origin/main's copy, materialised under scratch/ so the
    helper's ROOT is still the tree."""
    tree = seams["tree"]
    own = tree / "scripts" / "last-gated-main-sha"
    own.parent.mkdir(parents=True)
    own.write_text("#!/bin/sh\n# old branch copy\n", encoding="utf-8")
    main_copy = "#!/bin/sh\n# main copy with --diag\n"
    real_git = land_slice._git

    def git_with_main(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[:1] == ["show"]:
            return _proc(main_copy)
        return real_git(args, cwd)

    monkeypatch.setattr(land_slice, "_git", git_with_main)
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="3.2\n", stderr="")

    monkeypatch.setattr(land_slice.subprocess, "run", fake_run)
    monkeypatch.setattr(land_slice._factory_git, "git_auth_env", lambda base=None: {})
    pre = _REAL_DRIFT_PREFLIGHT(tree)
    assert pre["ran"] and pre["age"] == "3.2"
    # The lib module is tried first (gr550044's shim would resolve lib/
    # beside itself and find nothing materialised alone).
    target = tree / land_slice.PREFLIGHT_LIB_REL
    assert seen[0][:2] == [sys.executable, str(target)]
    assert target.read_text(encoding="utf-8") == main_copy
    assert target.stat().st_mode & 0o111
    # neither copy: nothing ran
    own.unlink()
    monkeypatch.setattr(land_slice, "_git", real_git)
    target.unlink()
    assert _REAL_DRIFT_PREFLIGHT(tree)["ran"] is False


def test_drift_preflight_runs_the_helper_under_the_factory_identity(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = seams["tree"]
    helper = tree / "scripts" / "last-gated-main-sha"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    seen: list[dict[str, Any]] = []
    answer: dict[str, Any] = {
        "rc": 0,
        "out": "",
        "err": f"noise\n{BLIND_DIAG[: -len(' (exit 0)')]}\n",
    }

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append({"argv": argv, **kw})
        return subprocess.CompletedProcess(
            argv,
            int(answer["rc"]),
            stdout=str(answer["out"]),
            stderr=str(answer["err"]),
        )

    monkeypatch.setattr(land_slice.subprocess, "run", fake_run)
    monkeypatch.setattr(
        land_slice._factory_git, "git_auth_env", lambda base=None: {"GH_TOKEN": "x"}
    )
    pre = _REAL_DRIFT_PREFLIGHT(tree)
    assert pre == {"ran": True, "rc": 0, "age": "", "diag": BLIND_DIAG}
    [call] = seen
    assert call["argv"] == [
        sys.executable,  # never the helper's `uv run` shebang (gr551238)
        str(helper),
        "--age-hours",
        "--start",
        "origin/main",
        "--hours",
        "48",
        "--diag",
    ]
    assert call["cwd"] == tree
    assert call["env"] == {"GH_TOKEN": "x"}
    assert call["timeout"] == land_slice.DRIFT_PROBE_TIMEOUT_S
    # a verdict: the age comes back, exit 0
    answer.update(out="3.2\n", err="last-gated-main-sha: status=found local=1 x\n")
    pre = _REAL_DRIFT_PREFLIGHT(tree)
    assert pre["age"] == "3.2" and pre["diag"].endswith("(exit 0)")
    # the process died before the helper's diag (uv, interpreter): the event
    # keeps its stderr tail, and the exit 2 is not taken as a verdict
    answer.update(rc=2, out="", err="warning: x\nerror: failed to open cache\n")
    pre = _REAL_DRIFT_PREFLIGHT(tree)
    assert pre["diag"] == (
        "(no diag line; stderr: warning: x | error: failed to open cache) (exit 2)"
    )
    assert land_slice._gate_unknown(pre) is True  # not the helper's exit 2
    # no helper in the tree: nothing ran
    helper.unlink()
    assert _REAL_DRIFT_PREFLIGHT(tree) == {
        "ran": False,
        "rc": None,
        "age": "",
        "diag": "",
    }
    assert len(seen) == 3

    def boom(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 1)

    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(land_slice.subprocess, "run", boom)
    pre = _REAL_DRIFT_PREFLIGHT(tree)
    assert pre["ran"] is True and pre["rc"] is None and pre["age"] == ""
    assert pre["diag"] == "helper did not answer (TimeoutExpired)"


def test_poll_before_not_before_does_nothing(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    store, handle = _defer_setup(seams)
    assert land_slice.poll(_Ctx(store, _params(seams)), handle) is False
    before = dict(handle)
    pctx = _Ctx(store, _params(seams))
    clock["t"] = T0 + timedelta(seconds=1799)
    assert land_slice.poll(pctx, handle) is False
    assert len(seams["launch"]) == 1
    assert handle == before
    assert pctx.chunks == [] and pctx.meta_set == {} and pctx.status is None


def test_poll_at_not_before_relaunches_once(
    seams: dict[str, Any],
    clock: dict[str, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, handle = _defer_setup(seams, refusals=2)
    assert land_slice.poll(_Ctx(store, _params(seams)), handle) is False
    assert handle["main_refusals"] == 3
    launched: list[tuple[Any, ...]] = []
    real = land_slice._launch_ship

    def spy(
        ctx: Any, tree: Path, flag: str, subject: str, **kw: Any
    ) -> dict[str, Any] | None:
        launched.append((tree, flag, subject))
        return real(ctx, tree, flag, subject, **kw)

    monkeypatch.setattr(land_slice, "_launch_ship", spy)
    clock["t"] = T0 + timedelta(seconds=1800)  # exactly due
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert launched == [(seams["tree"], "--quick", SUBJECT)]
    assert len(seams["launch"]) == 2
    argv, kw = seams["launch"][1]
    assert argv == [str(seams["tree"] / "scripts" / "ship"), "--quick", SUBJECT]
    assert kw["env"]["PRECIS_SHIP_VERBOSE"] == "1"
    assert "not_before" not in handle
    assert handle["pid"] == 5152 and handle["flag"] == "--quick"
    assert handle["main_refusals"] == 3  # carried over
    assert handle["last_refusal"].startswith("✖ main has no")
    assert pctx.meta_set["compute_handle"] == handle
    assert pctx.meta_set["compute_handle_released"] is False
    # a running ship is bounded again: wall from the relaunch, nothing more
    due = T0 + timedelta(seconds=1800)
    assert pctx.meta_set["deadline"] == due.timestamp() + land_slice.wall_seconds()
    assert pctx.status is None and store.comments(42) == []
    # the relaunch is now running: polls wait on it, no further launch
    assert land_slice.poll(pctx, handle) is False
    assert len(seams["launch"]) == 2


def test_ship_flag_is_checked_only_on_a_spot_selection() -> None:
    """gr599427 (Reto 2026-11-08): a checked ask — ``params.checked`` (a
    PR land) or an engine diff — earns ``--checked`` only when the
    selector's answer is a small spot selection; a wide/unreadable one
    goes ``--quick`` like every land. E1's "an engine land never drops
    to lint" is superseded — GitHub CI is the gate."""
    assert land_slice.ship_flag({}) == "--quick"
    assert land_slice.ship_flag({"checked": False}) == "--quick"
    # asked but no spot verdict: --quick
    assert land_slice.ship_flag({"checked": True}) == "--quick"
    assert land_slice.ship_flag({}, engine=True) == "--quick"
    # asked + spot: --checked
    assert land_slice.ship_flag({"checked": True}, spot=True) == "--checked"
    assert land_slice.ship_flag({}, engine=True, spot=True) == "--checked"
    assert land_slice.ship_flag({"checked": False}, engine=True, spot=True) == (
        "--checked"
    )
    # asked + wide: --quick, engine included
    assert land_slice.ship_flag({"checked": True}, spot=False) == "--quick"
    assert land_slice.ship_flag({}, engine=True, spot=False) == "--quick"


def test_spot_selection_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spot rule's inputs (td602151): FULL, empty, ``testmon=1``
    (the checked lane then runs the map's whole impacted set on top —
    unbounded), an unreadable selector, an uncountable collect, and a
    >200-item selection all count as wide — only a non-empty selection
    pytest collects ≤200 items from is a spot check. (No ``seams`` here
    — it stubs ``spot_selection`` itself; the seams under test are
    ``_selector_stdout`` and ``_collected_count``.)"""
    collected: list[list[str]] = []
    count: dict[str, int | None] = {"n": 25}

    def fake_collect(tree: Path, targets: list[str], timeout_s: float) -> int | None:
        collected.append(list(targets))
        return count["n"]

    monkeypatch.setattr(land_slice, "_collected_count", fake_collect)
    cases: list[tuple[str | None, int | None, bool]] = [
        (None, 25, False),
        ("", 25, False),
        ("FULL\n", 25, False),
        ("testmon=0\n", 25, False),
        # testmon=1 adds the map's impacted set on top: unbounded → wide
        ("testmon=1\ntests/test_a.py\n", 3, False),
        ("testmon=0\ntests/test_a.py\ntests/test_b.py::t\n", 25, True),
        ("testmon=0\ntests/test_a.py\ntests/test_b.py::t\n", 200, True),
        ("testmon=0\ntests/test_a.py\ntests/test_b.py::t\n", 201, False),
        # td602151 regression: two target LINES can still collect
        # thousands of items — the bound is items, not lines
        ("testmon=0\ntests/test_a.py\ntests/test_b.py\n", 4000, False),
        # collect cannot answer (error, missing extra, timeout): wide
        ("testmon=0\ntests/test_a.py\ntests/test_b.py\n", None, False),
    ]
    for out, n, want in cases:
        count["n"] = n
        collected.clear()
        monkeypatch.setattr(
            land_slice, "_selector_stdout", lambda tree, timeout_s, out=out: out
        )
        assert land_slice.spot_selection(tmp_path) is want, out
    # the collect probe receives the selector's targets verbatim
    assert collected
    assert collected[-1] == ["tests/test_a.py", "tests/test_b.py"]


def test_collected_count_parses_the_selected_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-m 'not slow'`` deselects inside the collect: the FIRST number
    of ``N/M tests collected`` is what the gate runs; a collect that
    fails or selects nothing is unbounded (``None``), not zero."""
    argv_seen: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        argv_seen.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 0, stdout="7/40 tests collected (33 deselected) in 0.42s\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert land_slice._collected_count(tmp_path, ["tests/test_a.py"], 30) == 7
    [argv] = argv_seen
    assert argv[:5] == ["uv", "run", "pytest", "--collect-only", "-q"]
    assert argv[-3:] == ["-m", "not slow", "tests/test_a.py"]

    def fail_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 5, stdout="no tests collected\n")

    monkeypatch.setattr(subprocess, "run", fail_run)
    assert land_slice._collected_count(tmp_path, ["tests/test_a.py"], 30) is None


def test_checked_land_goes_quick_on_a_wide_selection(seams: dict[str, Any]) -> None:
    """gr599427: a checked ask on a wide/unreadable selection lands
    lint-only — the release gate's CI runs the suite."""
    seams["spot"] = False
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, {**_params(seams), "checked": True})
    handle = land_slice.submit(ctx, None)
    assert handle is not None and ctx.failure is None
    [(argv, _kw)] = seams["launch"]
    assert argv[1] == "--quick"
    assert ctx.meta_set["spot_selection"] is False


def test_engine_land_goes_quick_on_a_wide_selection(seams: dict[str, Any]) -> None:
    """gr599427: an engine diff follows the same spot rule — no more
    unconditional ``--checked``."""
    seams["spot"] = False
    seams["changed_paths"] = ["scripts/ship"]
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, {**_params(seams), "checked": False})
    handle = land_slice.submit(ctx, None)
    assert handle is not None and ctx.failure is None
    [(argv, _kw)] = seams["launch"]
    assert argv[1] == "--quick"
    assert ctx.meta_set["engine_paths"] == ["scripts/ship"]


def test_poll_main_refusal_keeps_checked_flag(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    store = _Store()
    store.add_todo(42)
    params = {**_params(seams), "checked": True}
    handle = land_slice.submit(_Ctx(store, params), None)
    assert handle["flag"] == "--checked"
    _finish(handle, 1, DRIFT_LOG)
    assert land_slice.poll(_Ctx(store, params), handle) is False
    clock["t"] = T0 + timedelta(hours=1)
    assert land_slice.poll(_Ctx(store, params), handle) is False
    assert seams["launch"][1][0][1] == "--checked"


def test_poll_relaunch_failure_fails_the_job(
    seams: dict[str, Any],
    clock: dict[str, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, handle = _defer_setup(seams)
    assert land_slice.poll(_Ctx(store, _params(seams)), handle) is False

    def boom(argv: list[str], **kw: Any) -> int:
        raise OSError("no fork")

    monkeypatch.setattr(land_slice, "_launch", boom)
    clock["t"] = T0 + timedelta(hours=1)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.failure is not None and "ship launch failed" in pctx.failure


def test_poll_main_refusal_cap_fails_with_the_blocked_handoff(
    seams: dict[str, Any],
) -> None:
    store, handle = _defer_setup(seams, refusals=land_slice.MAX_MAIN_REFUSALS)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert len(seams["launch"]) == 1  # no relaunch past the cap
    n = land_slice.MAX_MAIN_REFUSALS + 1
    assert pctx.status == "failed" and pctx.failure is not None
    assert pctx.failure.startswith(
        f"land_slice: land blocked by main for {n} refusals:"
    )
    assert "✖ main has no all-green shard matrix" in pctx.failure
    [handoff] = store.comments(42)
    assert handoff.startswith(
        "handoff: land BLOCKED on slice/42: main refused the land "
    )
    assert f"the land {n} times (✖ main has no" in handoff
    assert handoff.endswith("or a fixed red on main. Do not rebuild.")
    assert "Nothing is wrong on the branch" in handoff
    assert _cleanup_cmds(seams) == []


HOST_LOG = (
    "quick ship (--quick)\n"
    "Installed 169 packages in 516ms\n"
    ".devin/hooks.v1.json is up to date\n"
    "scripts/ship: docker VM disk has 4 GB free; run scripts/reap-test-dbs, "
    "or ask the orchestrator to prune; do not re-run\n"
)
HOST_CROSS = (
    "scripts/ship: docker VM disk has 4 GB free; run scripts/reap-test-dbs, "
    "or ask the orchestrator to prune; do not re-run"
)


def test_host_refusal_marks_and_line() -> None:
    """jo552075: the disk preflight refuses on stderr without a ✖, exit 3."""
    assert land_slice.cross_line(HOST_LOG) is None
    assert land_slice.host_refusal_line(HOST_LOG) == HOST_CROSS
    assert land_slice.is_host_refusal(HOST_CROSS) is True
    assert land_slice.is_main_refusal(HOST_CROSS) is False
    assert land_slice.refusal_side(HOST_CROSS) == "host"
    probe_failed = (
        "scripts/ship: disk probe failed: docker not on PATH; this is not a "
        "disk-full verdict. Re-run, or PRECIS_DOCKER_DISK_CHECK=0 to skip the probe"
    )
    assert land_slice.refusal_side(probe_failed) == "host"
    assert land_slice.refusal_side(land_slice.cross_line(DRIFT_LOG)) == "main"
    assert land_slice.refusal_side("✖ ruff format: 1 file would be reformatted") is None
    assert land_slice.refusal_side("scripts/ship exit 3") is None
    assert land_slice.host_refusal_line(DRIFT_LOG) is None
    assert land_slice.host_refusal_line("") is None
    # jo553766: no gate image on the node; ship's ✖ blamed the lockfile
    no_image = (
        " Image precis-dev pull access denied for precis-dev, repository does "
        "not exist or may require 'docker login' \n"
        "#5 ERROR: pull access denied, repository does not exist or may require "
        "authorization: server message: insufficient_scope: authorization failed\n"
        " > [context premodels] load metadata for precis-mcp:premodels:\n"
        "\x1b[31m✖ gate env sync failed and NOT on a package fetch — uv could "
        "not resolve/install the locked environment (output above).\x1b[0m\n"
    )
    line = land_slice.host_refusal_line(no_image)
    assert line is not None and "load metadata for precis-mcp:premodels" in line
    assert land_slice.refusal_side(line) == "host"


BROKEN_DOCKER_LOG = (
    "quick ship (--quick)\n"
    "WARNING: precis-gate didn't start — gate falls back to the per-run "
    "bind-mount container\n"
    "offline env sync missed the uv cache — trying online (a genuinely new dep "
    "lands here; the cache re-warms for next time)\n"
    "unknown shorthand flag: 'f' in -f\n"
    "\n"
    "Usage:  docker [OPTIONS] COMMAND [ARG...]\n"
    "\n"
    "Run 'docker --help' for more information\n"
    "\x1b[31m✖ gate env sync failed and NOT on a package fetch — uv could not "
    "resolve/install the locked environment (output above). A lockfile/dep "
    "problem, not lint: fix it and re-run scripts/ship.\x1b[0m\n"
)


def test_poll_broken_docker_outranks_the_lockfile_cross(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """jo553514: the deploy user had no compose plugin; every `docker compose`
    died on its own flags and ship's ✖ blamed the branch's lockfile."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 1, BROKEN_DOCKER_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert handle["main_refusals"] == 1
    assert handle["last_refusal"] == "unknown shorthand flag: 'f' in -f"
    [(kind, text)] = pctx.chunks
    assert kind == "job_event" and "host-side refusal #1" in text
    assert store.comments(42) == []
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))


def _host_defer_setup(
    seams: dict[str, Any], refusals: int | None = None
) -> tuple[_Store, dict[str, Any]]:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    if refusals is not None:
        handle["main_refusals"] = refusals
    _finish(handle, 3, HOST_LOG)
    return store, handle


def test_poll_host_refusal_defers_without_a_probe(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    store, handle = _host_defer_setup(seams)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert len(seams["launch"]) == 1  # the original submit only
    assert pctx.status is None and pctx.failure is None
    assert handle["main_refusals"] == 1
    assert handle["not_before"] == "2026-11-01T10:30:00Z"
    assert handle["last_refusal"] == HOST_CROSS
    assert not {"pid", "pgid", "boot_id", "start_time"} & handle.keys()
    assert not Path(handle["exit_file"]).exists()
    assert pctx.meta_set["compute_handle"] == handle
    assert pctx.meta_set["compute_handle_released"] is False
    [(kind, text)] = pctx.chunks
    assert kind == "job_event"
    assert text.startswith(
        "land_slice: deferred (host-side refusal #1): scripts/ship: docker VM disk"
    )
    assert text.endswith("scripts/ship again after 10:30Z")
    assert "drift probe" not in text
    assert seams["probed"] == [seams["tree"]]  # the launch only; no deferral probe
    # the todo is untouched: still claimed, no attempt bump, no handoff
    tags = store.tag_strings(42)
    assert "claimed-by:lane-claude-sub" in tags
    assert not any(t.startswith("attempt:") for t in tags)
    assert store.comments(42) == []
    assert "exit_code" not in pctx.meta_set


def test_poll_host_refusal_cap_names_the_node(seams: dict[str, Any]) -> None:
    store, handle = _host_defer_setup(seams, refusals=land_slice.MAX_MAIN_REFUSALS)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert len(seams["launch"]) == 1
    n = land_slice.MAX_MAIN_REFUSALS + 1
    assert pctx.status == "failed" and pctx.failure is not None
    assert (
        f"land blocked by the lander's host for {n} refusals: {HOST_CROSS}"
        in pctx.failure
    )
    [handoff] = store.comments(42)
    assert handoff.startswith(
        "handoff: land BLOCKED on slice/42: the lander's host refused the land "
    )
    assert "Nothing is wrong on the branch" in handoff
    assert "an operator, not a builder" in handoff
    assert handoff.endswith("Do not rebuild.")
    assert _cleanup_cmds(seams) == []


def test_poll_lint_red_is_unchanged_by_the_main_refusal_path(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    handle["main_refusals"] = 3
    _finish(handle, 1, "\x1b[31m✖ ruff: 2 errors in src/x.py\x1b[0m\n")
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert len(seams["launch"]) == 1
    assert (
        pctx.failure is not None
        and "ship of td42 red: ✖ ruff: 2 errors" in pctx.failure
    )
    assert "attempt:1" in pctx.failure
    assert store.comments(42)[-1].startswith("handoff: land FAILED on slice/42: ✖ ruff")
    assert "attempt:1" in store.tag_strings(42)


# ── gate kills are host-side (gr570133) ────────────────────────────

#: The real ``gate_oom_note`` line (scripts/lib/gate-slot.sh) the ship log
#: carries when the docker VM's OOM killer takes the gate container.
KILL_LOG = (
    "checked ship (--checked)\n"
    "pytest -n3 --testmon --select-failures-first\n"
    "\x1b[31m✖ the gate container (checked mode) exited 137 (SIGKILL) — almost "
    "always the OOM killer on its mem_limit (24 GiB, docker/dev/compose.yaml), "
    "not a verdict on the diff.\n"
    "  The kill is per-container now: siblings keep running. Re-run to rule "
    "out a flake; if it repeats, bisect the memory hog.\x1b[0m\n"
    "SHIP_STATUS: error\n"
)

#: Real pytest-xdist dead-worker output: the per-worker death report
#: ``[gwN] node down:`` rides next to the ``worker 'gwN' crashed``
#: kill line; the summary ✖ names the gate red.
XDIST_LOG = (
    "checked ship (--checked)\n"
    "gw0 gw1 gw2 [3 workers]\n"
    "worker 'gw2' crashed while running 'tests/test_x.py::test_y'\n"
    "[gw2] node down: Not properly terminated\n"
    "\x1b[31m✖ the checked gate is RED (1 crashed worker, 0 failed)\x1b[0m\n"
    "SHIP_STATUS: error\n"
)


def test_gate_kill_line_reads_whole_log() -> None:
    """The kill line is found anywhere in the log — a later generic
    "gate is RED" ✖ must not hide it. A clean exit is never a kill:
    scripts/test retries a lone 137 itself, so a mark can sit in a
    successful log."""
    kill = land_slice.gate_kill_line(KILL_LOG, 1)
    assert kill is not None and "exited 137 (SIGKILL)" in kill
    line = land_slice.gate_kill_line(XDIST_LOG, 1)
    assert line is not None and "crashed while running" in line
    assert land_slice.gate_kill_line("✖ ruff format: 1 file\n", 1) is None
    assert land_slice.gate_kill_line("", 1) is None
    assert land_slice.gate_kill_line(KILL_LOG, 0) is None


#: A genuinely red gate can still QUOTE the kill marks: pytest failure
#: output prints the asserted-on string constants mid-line — this file's
#: own KILL_LOG and XDIST_LOG are exactly such constants. Anchored marks
#: must not fire on the quotes.
QUOTED_KILL_LOG = (
    "checked ship (--checked)\n"
    "FAILED tests/test_land_slice.py::test_t - AssertionError\n"
    ">       assert \"worker 'gw2' crashed while running 'x'\" in LOG\n"
    "E       AssertionError: LOG held '✖ the gate container exited 137 "
    "(SIGKILL) — almost always the OOM killer'\n"
    "\x1b[31m✖ the checked gate is RED (0 crashed, 1 failed)\x1b[0m\n"
    "SHIP_STATUS: error\n"
)


def _kill_setup(
    seams: dict[str, Any], log: str, gate_kills: int | None = None
) -> tuple[_Store, dict[str, Any]]:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    if gate_kills is not None:
        handle["gate_kills"] = gate_kills
    _finish(handle, 1, log)
    return store, handle


def test_poll_exit_137_defers_without_attempt(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """gr570133: a gate the VM's OOM killer took is infrastructure, not
    the branch — defer under `gate_kills` at the short backoff, no
    attempt, no handoff, then relaunch (which sweeps the stacks first)."""
    store, handle = _kill_setup(seams, KILL_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.status is None and pctx.failure is None
    assert handle["gate_kills"] == 1
    assert "main_refusals" not in handle
    assert handle["not_before"] == "2026-11-01T10:02:00Z"  # 120 s, not 30 min
    assert handle["last_refusal"].startswith(land_slice.GATE_KILL_CROSS)
    assert not {"pid", "pgid", "boot_id", "start_time"} & handle.keys()
    assert pctx.meta_set["compute_handle"] == handle
    assert pctx.meta_set["compute_handle_released"] is False
    [(kind, text)] = pctx.chunks
    assert kind == "job_event"
    assert "gate killed (exit 137, VM memory) - infrastructure, not the branch" in text
    assert "exited 137 (SIGKILL)" in text
    assert text.endswith("scripts/ship again after 10:02Z")
    # the todo is untouched: still claimed, no attempt bump, no handoff
    tags = store.tag_strings(42)
    assert "claimed-by:lane-claude-sub" in tags
    assert not any(t.startswith("attempt:") for t in tags)
    assert store.comments(42) == []
    # at the backoff the land relaunches — the launch swept the stacks
    # again — and the counter rides the carry.
    clock["t"] = T0 + timedelta(seconds=120)
    assert land_slice.poll(pctx, handle) is False
    assert len(seams["launch"]) == 2
    assert seams["swept"] == [seams["tree"], seams["tree"]]
    assert handle["pid"] == 5152 and "not_before" not in handle
    assert handle["gate_kills"] == 1


def test_poll_exit_137_cap_names_vm_or_branch_memory(
    seams: dict[str, Any],
) -> None:
    """Past MAX_GATE_KILL_RETRIES the job fails with a handoff naming the
    docker VM's memory or the branch's own memory use — exit 137 is also
    the container's own mem_limit (24 GiB), which a branch memory hog can
    hit, so the handoff does NOT clear the branch. Still outside the
    diff: no attempt is burned — but the run is spent, so
    ``meta.land_again_cap`` sends the tick to the ask, not to an auto
    re-land (gr570187)."""
    store, handle = _kill_setup(
        seams, KILL_LOG, gate_kills=land_slice.MAX_GATE_KILL_RETRIES
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert len(seams["launch"]) == 1
    n = land_slice.MAX_GATE_KILL_RETRIES + 1
    assert pctx.status == "failed" and pctx.failure is not None
    assert (
        f"land blocked by gate kills for {n} retries: "
        "gate killed (exit 137, VM memory)" in pctx.failure
    )
    assert pctx.meta_set["attempt_free"] == "infra-land"
    assert pctx.meta_set["land_again_cap"] is True
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags
    assert "claimed-by:lane-claude-sub" not in tags
    assert not any(t.startswith("attempt:") for t in tags)
    [handoff] = store.comments(42)
    assert handoff.startswith("handoff: land BLOCKED on slice/42:")
    assert "gate killed (exit 137, VM memory)" in handoff
    assert "the docker VM's memory or the branch's own memory use" in handoff
    assert "Do not rebuild" not in handoff


def test_poll_node_down_without_a_kill_line_is_an_ordinary_red(
    seams: dict[str, Any],
) -> None:
    """xdist's ``[gwN] node down: Not properly terminated`` is the
    ordinary per-worker death report, not an infra mark: a red whose log
    carries it but no kill line is a branch red with an attempt."""
    store, handle = _kill_setup(
        seams,
        "gw0 gw1 gw2 [3 workers]\n"
        "[gw2] node down: Not properly terminated\n"
        "\x1b[31m✖ the checked gate is RED (0 crashed, 1 failed)\x1b[0m\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "gate_kills" not in handle
    assert "attempt_free" not in pctx.meta_set
    assert "attempt:1" in store.tag_strings(42)
    assert store.comments(42)[-1].startswith("handoff: land FAILED on slice/42")


def test_poll_red_quoting_kill_marks_is_an_ordinary_red(
    seams: dict[str, Any],
) -> None:
    """A genuinely red gate whose pytest failure output quotes both kill
    literals (this file's own KILL_LOG/XDIST_LOG constants are exactly
    such constants) is an ordinary red: anchored marks do not fire, no
    deferral, the attempt burns."""
    assert land_slice.gate_kill_line(QUOTED_KILL_LOG, 1) is None
    store, handle = _kill_setup(seams, QUOTED_KILL_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "gate_kills" not in handle
    assert len(seams["launch"]) == 1
    assert "attempt:1" in store.tag_strings(42)
    assert store.comments(42)[-1].startswith("handoff: land FAILED on slice/42")


def test_poll_xdist_crash_retries_once_then_red(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """A crashed xdist worker may have been the VM's OOM killer — one
    retry. The second identical crash is a branch problem: the ordinary
    red, attempt consumed."""
    store, handle = _kill_setup(seams, XDIST_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert handle["gate_kills"] == 1
    assert handle["not_before"] == "2026-11-01T10:02:00Z"
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))
    [(kind, text)] = pctx.chunks
    assert kind == "job_event" and "crashed" in text
    clock["t"] = T0 + timedelta(seconds=120)
    assert land_slice.poll(pctx, handle) is False
    assert len(seams["launch"]) == 2 and handle["gate_kills"] == 1
    # second identical crash: deterministic — a branch problem
    _finish(handle, 1, XDIST_LOG)
    pctx2 = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx2, handle) is True
    assert pctx2.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    assert store.comments(42)[-1].startswith("handoff: land FAILED on slice/42")
    assert "the checked gate is RED" in (pctx2.failure or "")


def test_poll_kill_does_not_bump_main_refusals(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    store, handle = _kill_setup(seams, KILL_LOG)
    handle["main_refusals"] = 3
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert handle["gate_kills"] == 1
    assert handle["main_refusals"] == 3


def test_poll_real_red_is_unchanged(seams: dict[str, Any]) -> None:
    """An ordinary red — no kill line — never touches gate_kills."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 1, "\x1b[31m✖ ruff: 2 errors in src/x.py\x1b[0m\n")
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "gate_kills" not in handle
    assert len(seams["launch"]) == 1


def test_poll_ship_waiting_on_a_gate_slot_eats_no_wall(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """gr598110 item 4: a ship queued inside ``gate_slot_acquire`` is
    waiting on the host, not gating — the deadline re-arms to a fresh
    wall each poll and the wait line heartbeats once per change."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    Path(handle["log"]).write_text(
        "quick ship (--quick)\n"
        "waiting for a gate slot (2 concurrent gate containers max — "
        "shared-VM OOM guard, gr202193)\n"
        "gate slot: waiting at position 3 of 6 (2 slots ahead are busy)\n",
        encoding="utf-8",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.meta_set["deadline"] == T0.timestamp() + land_slice.wall_seconds()
    assert handle["gate_wait"].startswith("gate slot: waiting at position")
    assert any("waiting at position" in t for _k, t in pctx.chunks)
    # The same line again heartbeats nothing new but re-arms the wall.
    Path(handle["log"]).write_text(
        "gate slot: waiting at position 2 of 6 (2 slots ahead are busy)\n",
        encoding="utf-8",
    )
    clock["t"] = T0 + timedelta(seconds=30)
    assert land_slice.poll(pctx, handle) is False
    assert (
        pctx.meta_set["deadline"]
        == (T0 + timedelta(seconds=30)).timestamp() + land_slice.wall_seconds()
    )
    # Gate chatter over: the wait marker drops and the wall stops growing.
    Path(handle["log"]).write_text(
        "gate slot: waiting at position 1 of 6\nstole gate slot 0\n"
        "gate env → synced offline\n",
        encoding="utf-8",
    )
    assert land_slice.poll(pctx, handle) is False
    assert "gate_wait" not in handle


def test_gate_slot_waiting_reads_the_indented_holder_dump() -> None:
    """td599058: a real queue log's newest lines are the per-slot holder
    dump — INDENTED ``  gate slot N: …``. Stripping the line before
    matching left them unmatched, so a genuinely queued ship read as
    gating and the wait ate the deadline."""
    log = (
        "waiting for a gate slot (2 concurrent gate containers max — "
        "shared-VM OOM guard, gr202193)\n"
        "(steals a slot immediately if its holder dies; a live holder on "
        "this host is waited out however long its gate takes)\n"
        "gate slot: waiting at position 3 of 6 (2 slots ahead are busy)\n"
        "  gate slot 0: held 45m by slice-td97 (pid 111 on spark, alive)\n"
        "  gate slot 1: held 12m by slice-td98 (pid 222 on spark, alive)\n"
    )
    got = land_slice.gate_slot_waiting(log)
    assert got is not None and got.startswith("gate slot 1: held")
    # a steal, a give-up, an access refusal or the gate's own output ends it
    assert land_slice.gate_slot_waiting(log + "stole gate slot 0\n") is None
    assert (
        land_slice.gate_slot_waiting(
            log + "gate slot: gave up after 1800s at position 3 of 6\n"
        )
        is None
    )
    assert land_slice.gate_slot_waiting(log + "pytest -q -n3\n") is None
    assert land_slice.gate_slot_waiting("") is None


def test_gate_slot_waiting_sees_through_the_scripts_test_ticker() -> None:
    """td600305: a queued ``scripts/test`` appends
    ``scripts/test: gate-slot wait … Ns`` every 10 s — each tick
    displaces the holder dump as the newest line. The ticker is
    transparent queue chatter, not the wait's end."""
    log = (
        "gate slot: waiting at position 3 of 6 (2 slots ahead are busy)\n"
        "  gate slot 0: held 45m by slice-td97 (pid 111 on spark, alive)\n"
        "  gate slot 1: held 12m by slice-td98 (pid 222 on spark, alive)\n"
        "scripts/test: gate-slot wait … 10s\n"
        "scripts/test: gate-slot wait … 20s\n"
    )
    got = land_slice.gate_slot_waiting(log)
    assert got is not None and got.startswith("gate slot 1: held")
    # a done line still ends it, even under a straggler tick
    assert (
        land_slice.gate_slot_waiting(
            log + "stole gate slot 0\nscripts/test: gate-slot wait … 30s\n"
        )
        is None
    )
    # so does the next phase's own ticker — only the queue's is
    # transparent
    assert (
        land_slice.gate_slot_waiting(log + "scripts/test: test db up … 10s\n") is None
    )
    assert land_slice.gate_slot_waiting(log + "pytest -q -n3\n") is None
    # and the ticker alone is still positive evidence of the queue
    assert (
        land_slice.gate_slot_waiting("scripts/test: gate-slot wait … 5s\n")
        == "scripts/test: gate-slot wait … 5s"
    )


def test_poll_ship_queued_behind_the_holder_dump_eats_no_wall(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """The holder-dump tail is what a REAL queue log ends with — the wait
    must still be detected and the deadline re-armed (td599058)."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    Path(handle["log"]).write_text(
        "gate slot: waiting at position 2 of 4 (2 slots ahead are busy)\n"
        "  gate slot 0: held 3m by w1 (pid 11 on spark, alive)\n"
        "  gate slot 1: held 1m by w2 (pid 22 on spark, alive)\n",
        encoding="utf-8",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.meta_set["deadline"] == T0.timestamp() + land_slice.wall_seconds()
    assert handle["gate_wait"].startswith("gate slot 1: held")


def test_poll_verify_queued_on_a_gate_slot_eats_no_wall(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """td599058: a live verify probe queues inside ``gate_slot_acquire``
    like ship does — every phase runs ``scripts/test``. The deadline
    re-arms while it waits and the position heartbeats once per change."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False  # phase alone launched
    verify = handle["verify"]
    Path(verify["log"]).write_text(
        "gate slot: waiting at position 2 of 5 (2 slots ahead are busy)\n"
        "  gate slot 0: held 4m by w1 (pid 11 on spark, alive)\n",
        encoding="utf-8",
    )
    clock["t"] = T0 + timedelta(seconds=60)
    assert land_slice.poll(pctx, handle) is False
    assert verify["gate_wait"].startswith("gate slot 0: held")
    assert (
        pctx.meta_set["deadline"]
        == (T0 + timedelta(seconds=60)).timestamp() + land_slice.wall_seconds()
    )
    # the queue drains: the marker drops and the wall stops growing
    Path(verify["log"]).write_text("gate env → synced offline\n", encoding="utf-8")
    assert land_slice.poll(pctx, handle) is False
    assert "gate_wait" not in verify


def test_poll_verify_queued_behind_scripts_test_ticks_eats_no_wall(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """td600305: the verify probe IS ``scripts/test`` — its
    ``gate-slot wait`` ticker lands in the probe log every 10 s and
    displaces the holder dump as the newest line. The wait must keep
    re-arming the deadline, and a fresh tick must not heartbeat a new
    job_event: the reported wait line stays the holder dump."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False  # phase alone launched
    verify = handle["verify"]
    Path(verify["log"]).write_text(
        "gate slot: waiting at position 2 of 5 (2 slots ahead are busy)\n"
        "  gate slot 0: held 4m by w1 (pid 11 on spark, alive)\n"
        "scripts/test: gate-slot wait … 10s\n"
        "scripts/test: gate-slot wait … 20s\n",
        encoding="utf-8",
    )
    clock["t"] = T0 + timedelta(seconds=60)
    assert land_slice.poll(pctx, handle) is False
    assert verify["gate_wait"].startswith("gate slot 0: held")
    assert (
        pctx.meta_set["deadline"]
        == (T0 + timedelta(seconds=60)).timestamp() + land_slice.wall_seconds()
    )
    # a newer tick under unchanged chatter re-arms without re-heartbeating
    Path(verify["log"]).write_text(
        "gate slot: waiting at position 2 of 5 (2 slots ahead are busy)\n"
        "  gate slot 0: held 4m by w1 (pid 11 on spark, alive)\n"
        "scripts/test: gate-slot wait … 30s\n",
        encoding="utf-8",
    )
    clock["t"] = T0 + timedelta(seconds=90)
    assert land_slice.poll(pctx, handle) is False
    assert sum("gate slot 0: held" in t for _k, t in pctx.chunks) == 1
    assert (
        pctx.meta_set["deadline"]
        == (T0 + timedelta(seconds=90)).timestamp() + land_slice.wall_seconds()
    )
    # the wait ends: the marker drops even under a straggler queue tick
    Path(verify["log"]).write_text(
        "stole gate slot 0\nscripts/test: gate-slot wait … 40s\n"
        "scripts/test: test db up … 10s\n",
        encoding="utf-8",
    )
    assert land_slice.poll(pctx, handle) is False
    assert "gate_wait" not in verify


def test_poll_deselect_wait_re_arms_the_wall(
    seams: dict[str, Any],
    clock: dict[str, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-FULL deselect run is a normal gate: its queue wait re-arms
    the ordinary 1 h wall (td602151 — the 3 h full-gate budget is gone;
    a FULL selection never reaches this phase)."""
    monkeypatch.setattr(land_slice, "_checked_selection_full", lambda tree: False)
    seams["diff_names"] = "src/precis/x.py\n"
    seams["changed_paths"] = ["scripts/ship"]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = ["scripts/ship"]
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    verify = handle["verify"]
    assert verify["phase"] == "deselect" and "full_selection" not in verify
    Path(verify["log"]).write_text(
        "gate slot: waiting at position 1 of 3 (2 slots ahead are busy)\n"
        "  gate slot 0: held 9m by w1 (pid 11 on spark, alive)\n",
        encoding="utf-8",
    )
    clock["t"] = T0 + timedelta(seconds=90)
    assert land_slice.poll(pctx, handle) is False
    assert verify["gate_wait"].startswith("gate slot 0: held")
    assert (
        pctx.meta_set["deadline"]
        == (T0 + timedelta(seconds=90)).timestamp() + land_slice.wall_seconds()
    )


def test_kill_pushes_the_slice_branch_to_origin(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr598110: a killed land pushes ``slice/<id>`` to origin first —
    the commit survives a worktree reap."""
    pushed: list[tuple[str, str]] = []

    def fake_push(tree: Path, branch: str) -> str:
        pushed.append((str(tree), branch))
        return "ok"

    monkeypatch.setattr(land_slice, "_push_branch", fake_push)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    ctx = _Ctx(store, _params(seams))
    land_slice.kill(ctx, handle)
    assert pushed == [(str(seams["tree"]), "slice/42")]


# ── outside-diff reds verified before charging (gr573347 b) ────────

#: Ship's real checked-selection verdict (``die`` in ``scripts/ship``):
#: pytest red, but the only failing id is outside the diff — the shape
#: td565331/td569573/td570062 all went red on.
OUTSIDE_ID = (
    "tests/test_pathway_environment.py"
    "::test_far_vs_legacy_changes_selectivity_and_the_basis"
)
CHECKED_RED_LOG = (
    "checked ship (--checked)\n"
    "pytest -n3 --testmon --select-failures-first\n"
    "=== short test summary info ===\n"
    f"FAILED {OUTSIDE_ID} - assert 1 == 2\n"
    "=== 1 failed in 30.00s ===\n"
    "\x1b[31m✖ checked selection is RED — not shipping. Fix the failure "
    "above and re-run scripts/ship --checked.\x1b[0m\n"
    "SHIP_STATUS: error\n"
)


def _checked_submit(store: _Store, seams: dict[str, Any]) -> Any:
    """A ``--checked`` land (the only lane that runs the selection)."""
    ctx = _Ctx(store, {**_params(seams), "checked": True})
    return land_slice.submit(ctx, None)


#: A probe log whose summary shows the named id failing — the only
#: non-zero exit that counts as evidence of "red alone"/"red on main".
VERIFY_RED_LOG = (
    "=== short test summary info ===\n"
    f"FAILED {OUTSIDE_ID} - assert 1 == 2\n"
    "=== 1 failed in 30.00s ===\n"
)


def _verify_finish(
    handle: dict[str, Any], exit_code: int, log: str = VERIFY_RED_LOG
) -> None:
    Path(handle["verify"]["exit_file"]).write_text(f"{exit_code}\n", encoding="utf-8")
    Path(handle["verify"]["log"]).write_text(log, encoding="utf-8")


def _main_tree(seams: dict[str, Any]) -> Path:
    # Beside the slice tree: <root>/.claude/worktrees/slice-main-td42.
    return seams["tree"].parent / "slice-main-td42"


def test_poll_checked_red_all_ids_in_diff_is_ordinary(
    seams: dict[str, Any],
) -> None:
    """Every failing id is the branch's own — no verify, the ordinary
    red burns an attempt."""
    seams["diff_names"] = "tests/test_pathway_environment.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "verify" not in handle
    assert len(seams["launch"]) == 1
    assert "attempt:1" in store.tag_strings(42)


def test_poll_checked_red_mixed_ids_is_ordinary(seams: dict[str, Any]) -> None:
    """One outside, one inside: the in-diff failure is the branch's."""
    seams["diff_names"] = "tests/touched.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(
        handle,
        1,
        "=== short test summary info ===\n"
        "FAILED tests/touched.py::test_a - boom\n"
        f"FAILED {OUTSIDE_ID} - assert 1 == 2\n"
        "=== 2 failed in 30.00s ===\n"
        "\x1b[31m✖ checked selection is RED — not shipping.\x1b[0m\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert "verify" not in handle
    assert "attempt:1" in store.tag_strings(42)


def test_poll_checked_red_only_quoted_ids_is_ordinary(
    seams: dict[str, Any],
) -> None:
    """A ``FAILED`` literal quoted inside a test body is no failing id —
    nothing to verify, the red is ordinary."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(
        handle,
        1,
        'E   assert "FAILED tests/x.py::test_t - boom" in captured\n'
        "\x1b[31m✖ checked selection is RED — not shipping.\x1b[0m\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert "verify" not in handle
    assert "attempt:1" in store.tag_strings(42)


def test_poll_checked_red_outside_alone_green_is_attempt_free(
    seams: dict[str, Any],
) -> None:
    """All ids outside the diff and green standalone: flaky or
    order-dependent — attempt-free, and land-again's re-land tick takes
    it from there."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.status is None and pctx.failure is None
    verify = handle["verify"]
    assert verify["phase"] == "alone" and verify["ids"] == [OUTSIDE_ID]
    argv, kw = seams["launch"][-1]
    assert argv == [str(seams["tree"] / "scripts" / "test"), OUTSIDE_ID]
    assert kw["cwd"] == seams["tree"]
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))
    _verify_finish(handle, 0)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set["attempt_free"] == "infra-land"
    assert pctx.meta_set["red_class"] == "flaky outside the diff"
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags
    assert "claimed-by:lane-claude-sub" not in tags
    assert not any(t.startswith("attempt:") for t in tags)
    [handoff] = store.comments(42)
    assert handoff.startswith("handoff: land FAILED on slice/42:")
    assert "flaky or order-dependent" in handoff
    assert "the lander re-lands" in handoff
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "green standalone" in summary and OUTSIDE_ID in summary


def test_poll_checked_red_outside_alone_red_main_red_deselects_then_quick(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Still red standalone, red on origin/main too: not the branch's —
    a NON-engine checked land earns ``--quick`` the same way an engine
    one does (td599058): the deselect gate proves the checked selection
    minus the main-red ids green, then the land relaunches ``--quick``.
    The probe worktree and its stack are removed, and the todo carries
    the note naming the ids."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    verify = handle["verify"]
    main_tree = _main_tree(seams)
    assert verify["phase"] == "main" and verify["run_tree"] == str(main_tree)
    adds = [a for a, _ in seams["git"] if a[:2] == ["worktree", "add"]]
    assert adds == [["worktree", "add", "--detach", str(main_tree), "origin/main"]]
    argv, kw = seams["launch"][-1]
    assert argv == [str(main_tree / "scripts" / "test"), OUTSIDE_ID]
    assert kw["cwd"] == main_tree
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    # the deselect phase runs first — the rest of the selection is
    # proven green before --quick lands
    verify = handle["verify"]
    assert verify["phase"] == "deselect" and verify["ids"] == [OUTSIDE_ID]
    argv, _kw = seams["launch"][-1]
    assert argv == [
        str(seams["tree"] / "scripts" / "test"),
        "--selected",
        "--deselect",
        OUTSIDE_ID,
    ]
    _verify_finish(handle, 0)
    assert land_slice.poll(pctx, handle) is False
    argv, _kw = seams["launch"][-1]
    assert argv[1] == "--quick" and argv[2] == SUBJECT
    assert handle["flag"] == "--quick" and "verify" not in handle
    removes = [a for a, _ in seams["git"] if a[:2] == ["worktree", "remove"]]
    assert removes == [["worktree", "remove", "--force", str(main_tree)]]
    assert ("precis-test-slice-main-td42", True) in seams["evict"]
    tags = store.tag_strings(42)
    assert "claimed-by:lane-claude-sub" in tags
    assert not any(t.startswith("attempt:") for t in tags)
    [note] = store.comments(42)
    assert note.startswith("handoff:") and "red on origin/main too" in note
    assert OUTSIDE_ID in note
    events = [t for k, t in pctx.chunks if k == "job_event"]
    assert any("red on origin/main too" in e and "--deselect" in e for e in events)


def test_poll_checked_red_main_red_fallback_off_stays_red(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PRECIS_LAND_FULL_FALLBACK=off`` opts a land out of the
    deselect gate: the main-side red stays the ordinary red (gr599427
    dropped E1's "never lint" — engine lands follow the same opt-out)."""
    monkeypatch.setenv("PRECIS_LAND_FULL_FALLBACK", "off")
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False  # phase main launched
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    assert all("--deselect" not in call[0] for call in seams["launch"])


def test_poll_checked_red_main_red_on_an_engine_land_deselects_checked(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr598110: an engine land whose outside-diff red is main's too
    gates on the checked selection MINUS the red-on-main ids — a
    detached ``scripts/test --selected --deselect`` — and lands
    ``--quick`` on its green. ``--full`` never launches."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    seams["diff_names"] = "src/precis/x.py\n"
    seams["changed_paths"] = ["scripts/ship"]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = ["scripts/ship"]
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    verify = handle["verify"]
    assert verify["phase"] == "deselect" and verify["ids"] == [OUTSIDE_ID]
    argv, _kw = seams["launch"][-1]
    assert argv == [
        str(seams["tree"] / "scripts" / "test"),
        "--selected",
        "--deselect",
        OUTSIDE_ID,
    ]
    _verify_finish(handle, 0)
    assert land_slice.poll(pctx, handle) is False
    argv, _kw = seams["launch"][-1]
    assert argv[1] == "--quick" and argv[2] == SUBJECT
    assert handle["flag"] == "--quick" and "verify" not in handle
    assert all("--full" not in call[0] for call in seams["launch"])
    events = [t for k, t in pctx.chunks if k == "job_event"]
    assert any("red on origin/main too" in e for e in events)


def test_poll_checked_red_main_red_full_selection_lands_quick(
    seams: dict[str, Any], clock: dict[str, datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td602151: when the checked selection IS the suite (FULL) no
    deselect gate launches — the minus-ids run would BE the suite and
    the 3 h full-gate budget is gone. The main-red ids are recorded as
    excluded on the todo BEFORE the land relaunches ``--quick`` inside
    the 1 h wall, engine land or not."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    monkeypatch.setattr(land_slice, "_checked_selection_full", lambda tree: True)
    seams["diff_names"] = "src/precis/x.py\n"
    seams["changed_paths"] = ["scripts/ship"]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = ["scripts/ship"]
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    clock["t"] = T0 + timedelta(seconds=90)
    assert land_slice.poll(pctx, handle) is False
    # no deselect launch, no --full relaunch — straight to --quick
    assert all("--deselect" not in call[0] for call in seams["launch"])
    assert all("--full" not in call[0] for call in seams["launch"])
    argv, _kw = seams["launch"][-1]
    assert argv[1] == "--quick" and argv[2] == SUBJECT
    assert handle["flag"] == "--quick" and "verify" not in handle
    assert (
        pctx.meta_set["deadline"]
        == (T0 + timedelta(seconds=90)).timestamp() + land_slice.wall_seconds()
    )
    # the excluded main-red ids ride the todo before --quick launched
    [note] = store.comments(42)
    assert note.startswith("handoff:") and "red on origin/main too" in note
    assert OUTSIDE_ID in note and "excluded" in note
    tags = store.tag_strings(42)
    assert "claimed-by:lane-claude-sub" in tags
    assert not any(t.startswith("attempt:") for t in tags)
    events = [t for k, t in pctx.chunks if k == "job_event"]
    assert any("red on origin/main too" in e and "excluded" in e for e in events)


def _refuse_exclusion_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_rs._append_comment`` raises on the main-red exclusion note —
    the write ``--quick`` must not relaunch without (td602837) — and
    passes every other comment through."""
    real_append = land_slice._rs._append_comment

    def fail_exclusion(store: Any, todo_id: int, text: str) -> None:
        if "red on origin/main too" in text:
            raise OSError("store write refused")
        real_append(store, todo_id, text)

    monkeypatch.setattr(land_slice._rs, "_append_comment", fail_exclusion)


def test_poll_checked_red_main_red_full_selection_note_failure_is_ordinary(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td602837: the exclusion note IS the record ``--quick`` stands on
    (the land log dies with the worktree) — a failed write does not
    relaunch; the ordinary red takes it, attempt burned."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    monkeypatch.setattr(land_slice, "_checked_selection_full", lambda tree: True)
    _refuse_exclusion_note(monkeypatch)
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False  # alone probe
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False  # main probe
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    # ship ran once (--checked) and the two probes ran; --quick never
    # relaunched and no deselect gate was cut for a FULL selection
    assert all("--quick" not in call[0] for call in seams["launch"])
    assert all("--deselect" not in call[0] for call in seams["launch"])
    assert "verify" not in handle and handle.get("verify_done") is True
    assert "attempt:1" in store.tag_strings(42)
    notes = store.comments(42)
    assert all("red on origin/main too" not in n for n in notes)
    assert any(n.startswith("handoff: land FAILED on slice/42:") for n in notes)


def test_poll_checked_red_main_red_deselect_note_failure_is_ordinary(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td602837: same guard on the deselect path — a green minus-ids
    gate does not earn ``--quick`` when the exclusion note could not be
    written; the ordinary red burns the attempt."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    _refuse_exclusion_note(monkeypatch)
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False  # alone probe
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False  # main probe
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False  # deselect gate launched
    assert handle["verify"]["phase"] == "deselect"
    _verify_finish(handle, 0)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert all("--quick" not in call[0] for call in seams["launch"])
    assert "verify" not in handle and handle.get("verify_done") is True
    assert "attempt:1" in store.tag_strings(42)
    assert all("red on origin/main too" not in n for n in store.comments(42))


def test_poll_checked_red_outside_alone_red_main_green_is_ordinary(
    seams: dict[str, Any],
) -> None:
    """Red standalone but green on origin/main: the red IS the branch's —
    ordinary red, attempt burned, the probe worktree still removed."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 0)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    main_tree = _main_tree(seams)
    assert ["worktree", "remove", "--force", str(main_tree)] in [
        a for a, _ in seams["git"]
    ]
    assert ("precis-test-slice-main-td42", True) in seams["evict"]
    handoff = store.comments(42)[-1]
    assert "GREEN on origin/main" in handoff and OUTSIDE_ID in handoff


def test_poll_checked_red_verify_launch_failure_is_ordinary(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verify launch itself failing falls back to the ordinary red —
    never a park."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)

    def boom(*a: Any, **k: Any) -> int:
        raise OSError("no exec")

    monkeypatch.setattr(land_slice, "_launch", boom)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "verify" not in handle
    assert "attempt:1" in store.tag_strings(42)


def test_poll_checked_red_alone_probe_no_tests_is_ordinary(
    seams: dict[str, Any],
) -> None:
    """A probe that never ran the named tests is a probe failure, never
    evidence: 'no tests collected' → the ordinary red, the branch
    charged."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1, "no tests collected in 0.01s\n")
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "verify" not in handle and handle.get("verify_done") is True
    assert "attempt:1" in store.tag_strings(42)
    # phase main never started: no probe worktree was cut
    assert not any(a[:2] == ["worktree", "add"] for a, _ in seams["git"])
    assert "main_probe" not in pctx.meta_set


def test_poll_checked_red_alone_probe_other_ids_is_ordinary(
    seams: dict[str, Any],
) -> None:
    """A summary failing ids that are NOT the probed ones is likewise no
    evidence — the run did something else."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(
        handle,
        1,
        "=== short test summary info ===\n"
        "FAILED tests/other.py::test_x - boom\n"
        "=== 1 failed in 30.00s ===\n",
    )
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    assert not any(a[:2] == ["worktree", "add"] for a, _ in seams["git"])


def test_poll_checked_red_alone_probe_subset_ids_is_ordinary(
    seams: dict[str, Any],
) -> None:
    """A subset is not the same: the probe summary names only one of the
    two ids it was asked to run — partial evidence is no evidence, the
    ordinary red, no relaunch."""
    other = "tests/test_other_outside.py::test_b"
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(
        handle,
        1,
        "checked ship (--checked)\n"
        "=== short test summary info ===\n"
        f"FAILED {OUTSIDE_ID} - assert 1 == 2\n"
        f"FAILED {other} - boom\n"
        "=== 2 failed in 30.00s ===\n"
        "\x1b[31m✖ checked selection is RED — not shipping.\x1b[0m\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert handle["verify"]["ids"] == [OUTSIDE_ID, other]
    _verify_finish(handle, 1)  # VERIFY_RED_LOG names only OUTSIDE_ID
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    # no phase-main worktree, no --quick relaunch
    assert not any(a[:2] == ["worktree", "add"] for a, _ in seams["git"])
    argv, _kw = seams["launch"][-1]
    assert argv[0].endswith("scripts/test")


def test_poll_checked_red_main_probe_without_evidence_is_ordinary(
    seams: dict[str, Any],
) -> None:
    """The main probe exits non-zero but never ran the named tests (a
    usage error): no --quick relaunch — the ordinary red."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False  # phase main launched
    _verify_finish(handle, 4, "scripts/test: unrecognized arguments\n")
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    # no --quick relaunch: the last launch was the probe itself
    argv, _kw = seams["launch"][-1]
    assert argv[0].endswith("scripts/test")


def test_poll_checked_red_outside_spaced_id_is_one_argv(
    seams: dict[str, Any],
) -> None:
    """A parametrized id with spaces rides to ``scripts/test`` as ONE
    argv element — no shell, no splitting."""
    seams["diff_names"] = "src/precis/x.py\n"
    spaced = "tests/test_x.py::test_p[param with space]"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(
        handle,
        1,
        "=== short test summary info ===\n"
        f"FAILED {spaced} - assert 1 == 2\n"
        "=== 1 failed in 30.00s ===\n"
        "\x1b[31m✖ checked selection is RED — not shipping.\x1b[0m\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert handle["verify"]["ids"] == [spaced]
    argv, _kw = seams["launch"][-1]
    assert argv == [str(seams["tree"] / "scripts" / "test"), spaced]


def test_poll_checked_red_in_diff_error_is_ordinary(seams: dict[str, Any]) -> None:
    """A collection ``ERROR`` whose file the diff touches is the
    branch's — the ordinary red, no verify."""
    seams["diff_names"] = "tests/broken.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(
        handle,
        1,
        "=== short test summary info ===\n"
        "ERROR tests/broken.py::test_broken - ImportError: boom\n"
        "=== 1 error in 30.00s ===\n"
        "\x1b[31m✖ checked selection is RED — not shipping.\x1b[0m\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert "verify" not in handle
    assert "attempt:1" in store.tag_strings(42)


def test_poll_checked_red_outside_error_verifies(seams: dict[str, Any]) -> None:
    """A collection ``ERROR`` outside the diff counts like a FAILED id:
    verified standalone before the branch is charged."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(
        handle,
        1,
        "=== short test summary info ===\n"
        "ERROR tests/broken.py::test_broken - ImportError: boom\n"
        "=== 1 error in 30.00s ===\n"
        "\x1b[31m✖ checked selection is RED — not shipping.\x1b[0m\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    verify = handle["verify"]
    assert verify["phase"] == "alone" and verify["ids"] == [
        "tests/broken.py::test_broken"
    ]
    argv, _kw = seams["launch"][-1]
    assert argv == [
        str(seams["tree"] / "scripts" / "test"),
        "tests/broken.py::test_broken",
    ]


def test_main_verify_removes_a_registered_stale_probe_tree(
    seams: dict[str, Any],
) -> None:
    """A crashed run's ``slice-main-td<id>`` still registered as a
    worktree comes down through git — stack evicted, forced remove —
    before the add runs. The probe is on the job meta BEFORE the add, so
    a kill can drop it."""
    seams["diff_names"] = "src/precis/x.py\n"
    main_tree = _main_tree(seams)
    main_tree.mkdir(parents=True)
    seams["worktree_list"] = (
        f"worktree {seams['tree'].parent.parent}\n"
        "HEAD abc1234\n"
        "branch refs/heads/main\n"
        "\n"
        f"worktree {main_tree}\n"
        "HEAD abc1234\n"
        "detached\n"
    )
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    git = [a for a, _ in seams["git"]]
    assert ["worktree", "prune"] in git
    rm = git.index(["worktree", "remove", "--force", str(main_tree)])
    add = git.index(["worktree", "add", "--detach", str(main_tree), "origin/main"])
    assert rm < add
    assert ("precis-test-slice-main-td42", True) in seams["evict"]
    assert pctx.meta_set["main_probe"] == {
        "tree": str(main_tree),
        "stack": "precis-test-slice-main-td42",
    }
    assert handle["verify"]["phase"] == "main"


def test_main_verify_deletes_an_unregistered_stale_probe_dir(
    seams: dict[str, Any],
) -> None:
    """A leftover ``slice-main-td<id>`` directory git does NOT register
    is deleted directly — only that exact validated path — before the
    add runs."""
    seams["diff_names"] = "src/precis/x.py\n"
    main_tree = _main_tree(seams)
    (main_tree / "debris").mkdir(parents=True)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    git = [a for a, _ in seams["git"]]
    assert ["worktree", "prune"] in git
    assert ["worktree", "list", "--porcelain"] in git
    # not a registered worktree: no git remove — the directory is deleted
    assert ["worktree", "remove", "--force", str(main_tree)] not in git
    assert not main_tree.exists()
    assert ("precis-test-slice-main-td42", True) in seams["evict"]
    assert ["worktree", "add", "--detach", str(main_tree), "origin/main"] in git
    assert handle["verify"]["phase"] == "main"


def test_main_verify_dead_probe_is_ordinary_and_dropped(
    seams: dict[str, Any],
) -> None:
    """The probe dying with no exit file is a probe failure: the
    ordinary red — and the throwaway tree and stack still come down."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False  # phase main running
    seams["alive"] = False  # probe died leaving no exit file
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "attempt:1" in store.tag_strings(42)
    main_tree = _main_tree(seams)
    assert ["worktree", "remove", "--force", str(main_tree)] in [
        a for a, _ in seams["git"]
    ]
    assert ("precis-test-slice-main-td42", True) in seams["evict"]
    # the meta forgets the probe, so a later kill removes nothing
    assert pctx.meta_set["main_probe"] is None


# ── kill signals the live phase and drops the probe ─────────────────


def _signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record ``(pgid, sig)`` the kill sends, through run_slice's seam."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        land_slice._rs, "_signal_group", lambda pgid, sig: sent.append((pgid, sig))
    )
    return sent


def test_kill_signals_ship_only_outside_verify(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ship running: ship's pgid is signaled and nothing else — no main
    probe exists to drop."""
    sent = _signals(monkeypatch)
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    land_slice.kill(ctx, handle)
    assert sent == [(5151, signal.SIGTERM)]
    assert seams["evict"] == []
    assert not any(a[:2] == ["worktree", "remove"] for a, _ in seams["git"])


def test_kill_during_alone_verify_signals_verify_only(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase ``alone`` runs the verify in the slice tree: only its pgid
    is signaled — a finished ship's pgid may have been reused — and
    there is no probe worktree to drop."""
    sent = _signals(monkeypatch)
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42)
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    handle["pgid"] = handle["pid"] = 9999  # the dead ship
    handle["verify"]["pgid"] = 7777
    land_slice.kill(pctx, handle)
    assert sent == [(7777, signal.SIGTERM)]
    assert seams["evict"] == []
    assert not any(a[:2] == ["worktree", "remove"] for a, _ in seams["git"])


def test_kill_during_main_verify_signals_probe_and_drops_it(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase ``main``: the verify's pgid is signaled and the probe's
    worktree and stack — read from the job meta — are removed."""
    sent = _signals(monkeypatch)
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42)
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False  # phase main running
    main_tree = _main_tree(seams)
    # _Ctx.set_meta records meta_set without mutating the starting meta;
    # the persisted job meta a later kill reads carries the same record —
    # seed it directly.
    pctx.meta["main_probe"] = {
        "tree": str(main_tree),
        "stack": "precis-test-slice-main-td42",
    }
    handle["pgid"] = handle["pid"] = 9999  # the dead ship
    handle["verify"]["pgid"] = 7777
    land_slice.kill(pctx, handle)
    assert sent == [(7777, signal.SIGTERM)]
    assert ("precis-test-slice-main-td42", True) in seams["evict"]
    assert ["worktree", "remove", "--force", str(main_tree)] in [
        a for a, _ in seams["git"]
    ]


def _fake_proc_fs(
    monkeypatch: pytest.MonkeyPatch,
    pgid: int,
    *,
    boot_id: str = "boot-for-test",
    start_time: int = 123456,
) -> None:
    """``/proc`` answers for the executor's identity gate: the boot-id
    file and the group leader's stat (start ticks at field 22). The
    group is reported alive so the gate reaches the kill hook."""
    original = Path.read_text

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return boot_id + "\n"
        if str(path) == f"/proc/{pgid}/stat":
            fields = ["S", *(["0"] * 18), str(start_time), "0"]
            return f"{pgid} (runner with ) spaces) " + " ".join(fields)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: True)
    monkeypatch.setattr(
        land_slice._rs,
        "_process_identity",
        lambda pid: (boot_id, start_time),
    )


def _gate_ctx(
    store: Any, handle: dict[str, Any], *, main_probe: dict[str, str] | None = None
) -> Any:
    """A DispatchContext over the real store, carrying the job meta the
    executor would read — the handle, and the recorded main probe."""
    meta: dict[str, Any] = {
        "executor": "ssh_node",
        "job_type": "land_slice",
        "params": {"todo_id": 42, "target_node": "spark"},
        "compute_handle": handle,
    }
    if main_probe is not None:
        meta["main_probe"] = main_probe
    ref = store.insert_ref(kind="job", slug=None, title="land job", meta=meta)
    return ssh_node._build_dispatch_context(store, int(ref.id), "land job", meta)


def test_executor_cancel_signals_the_alone_verify(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    store: Any,
) -> None:
    """Through the executor's gate: during phase ``alone`` the promoted
    probe identity is what ``_detached_identity_reason`` validates, so
    the land_slice kill hook runs and the PROBE's group is signalled —
    while the handle named the dead ship the gate failed closed and the
    kill never ran."""
    sent = _signals(monkeypatch)
    # identity must resolve at LAUNCH time for the promotion to carry it
    monkeypatch.setattr(
        land_slice._rs,
        "_process_identity",
        lambda pid: ("boot-for-test", 123456),
    )
    seams["diff_names"] = "src/precis/x.py\n"
    store_fake = _Store()
    store_fake.add_todo(42)
    handle = _checked_submit(store_fake, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store_fake, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert handle["verify"]["phase"] == "alone"
    # the probe IS the handle's live process now
    assert handle["pgid"] == handle["verify"]["pgid"] != 5151
    _fake_proc_fs(monkeypatch, int(handle["pgid"]))
    ctx = _gate_ctx(store, handle)
    assert ssh_node._cancel_one(ctx) is False  # first sight: TERM sent
    assert sent == [(int(handle["pgid"]), signal.SIGTERM)]
    # phase alone has no probe tree — nothing else was touched
    assert seams["evict"] == []
    assert not any(a[:2] == ["worktree", "remove"] for a, _ in seams["git"])


def test_executor_cancel_signals_the_main_probe_and_drops_it(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    store: Any,
) -> None:
    """Phase ``main``: the cancel reaches the kill hook through the
    promoted identity — the probe's group is signalled and the recorded
    main tree and stack are removed."""
    sent = _signals(monkeypatch)
    monkeypatch.setattr(
        land_slice._rs,
        "_process_identity",
        lambda pid: ("boot-for-test", 123456),
    )
    seams["diff_names"] = "src/precis/x.py\n"
    store_fake = _Store()
    store_fake.add_todo(42)
    handle = _checked_submit(store_fake, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store_fake, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    assert handle["verify"]["phase"] == "main"
    assert handle["pgid"] == handle["verify"]["pgid"] != 5151
    _fake_proc_fs(monkeypatch, int(handle["pgid"]))
    main_tree = _main_tree(seams)
    ctx = _gate_ctx(
        store,
        handle,
        main_probe={
            "tree": str(main_tree),
            "stack": "precis-test-slice-main-td42",
        },
    )
    assert ssh_node._cancel_one(ctx) is False
    assert sent == [(int(handle["pgid"]), signal.SIGTERM)]
    assert ("precis-test-slice-main-td42", True) in seams["evict"]
    assert ["worktree", "remove", "--force", str(main_tree)] in [
        a for a, _ in seams["git"]
    ]


# ── credential shapes never reach the todo (mask_secrets) ──────────


def test_poll_red_masks_credential_shapes_in_the_excerpt(
    seams: dict[str, Any],
) -> None:
    """Gate output that echoes a credential rides the handoff comment,
    the job summary and the failure's log tail masked — never the
    literal."""
    token = "ghp_" + "a1B2c3" * 6
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(
        handle,
        1,
        f"deploy: token={token} refused by the API\n✖ the deploy step failed\n",
    )
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    handoff = store.comments(42)[-1]
    assert token not in handoff and "<redacted:github token>" in handoff
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert token not in summary and "<redacted:github token>" in summary
    assert token not in (pctx.failure or "")


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
    # the classified head is fetched by sha and the branch set to it
    assert ["fetch", "origin", PR_HEAD] in git
    assert ["branch", "-f", "slice/pr-73", PR_HEAD] in git
    assert not any(a[:2] == ["fetch", "origin"] and "pull" in a[2] for a in git)
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


# --- the checked run's xdist worker cap (gr567471) ------------------------


def test_test_nproc_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_LAND_TEST_NPROC", raising=False)
    assert land_slice.test_nproc() == str(land_slice.DEFAULT_TEST_NPROC)
    monkeypatch.setenv("PRECIS_LAND_TEST_NPROC", "2")
    assert land_slice.test_nproc() == "2"
    monkeypatch.setenv("PRECIS_LAND_TEST_NPROC", "0")
    assert land_slice.test_nproc() == "0"


def test_test_nproc_rejects_garbage_and_negatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_LAND_TEST_NPROC", "many")
    assert land_slice.test_nproc() == str(land_slice.DEFAULT_TEST_NPROC)
    monkeypatch.setenv("PRECIS_LAND_TEST_NPROC", "-4")
    assert land_slice.test_nproc() == str(land_slice.DEFAULT_TEST_NPROC)


# --- the factory's share of the gate semaphore (td599058) ----------------


def test_land_gate_slots_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PRECIS_GATE_SLOTS`` on a land launch bounds the factory's share of
    the shared semaphore to 1–2 slots (the lib derives up to 6 on the
    factory host's VM) — the env narrows to 1, never widens past 2."""
    monkeypatch.delenv("PRECIS_LAND_GATE_SLOTS", raising=False)
    assert land_slice.land_gate_slots() == str(land_slice.DEFAULT_LAND_GATE_SLOTS)
    monkeypatch.setenv("PRECIS_LAND_GATE_SLOTS", "1")
    assert land_slice.land_gate_slots() == "1"
    for bad in ("6", "0", "-1", "many"):
        monkeypatch.setenv("PRECIS_LAND_GATE_SLOTS", bad)
        assert land_slice.land_gate_slots() == str(land_slice.DEFAULT_LAND_GATE_SLOTS)


def test_launches_carry_the_slot_cap(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both launch envs — ship's and a verify probe's — carry
    ``PRECIS_GATE_SLOTS`` so every gate the lander spawns stays inside
    the 1–2 slot share."""
    monkeypatch.delenv("PRECIS_LAND_GATE_SLOTS", raising=False)
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42)
    handle = _checked_submit(store, seams)
    _argv, kw = seams["launch"][-1]
    assert kw["env"]["PRECIS_GATE_SLOTS"] == str(land_slice.DEFAULT_LAND_GATE_SLOTS)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False  # phase alone launched
    _argv, kw = seams["launch"][-1]
    assert kw["env"]["PRECIS_GATE_SLOTS"] == str(land_slice.DEFAULT_LAND_GATE_SLOTS)
