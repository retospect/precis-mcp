"""land_slice job type — the dark-factory lander (td543731).

Pure tests against a fake store and a fake git seam; ship is either a fake
launcher or (one test) a stub script reached through ``PRECIS_SHIP_BIN`` and
the real detached launcher. No DB, no real scripts/ship, and no real git
except the two ``test_real_git_*`` release-sync tests (a temp repo).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: the launcher execs /bin/sh"
)

from precis.plans import lanes, nodes
from precis.plans.templates import PENDING_SHA
from precis.store.types import ChunkInsert, Tag
from precis.workers.executors import ssh_node
from precis.workers.job_types import (
    Reconcile,
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
        # gr650484 seams: what ``rev-parse --absolute-git-dir`` answers.
        # The fixture points it at a real dir under tmp_path (set below)
        # so every submit exercises the land-lock/index-lock/
        # already-landed checks; "" would mark it unresolved and skip
        # them all.
        "gitdir": "",
        # ``--git-common-dir``: "" (or equal to gitdir) = a primary
        # checkout, no second index.lock pair.
        "common": "",
        # ``fetch``/``diff --quiet``/``log --max-count`` results for
        # ``_already_landed``: fetch_rc!=0 disables the check; diff_rc==0
        # = branch tree identical to main's; main_log carries
        # ``<sha>\x00<subject>`` lines for the subject scan. The content
        # proof rides ``merge_base``/``diff_names``/``path_diff_rc`` —
        # the paths since merge-base must be identical on main for a
        # same-subject commit to count as this land.
        "fetch_rc": 0,
        "diff_rc": 1,
        "main_log": "",
        "merge_base": "b" * 40,
        "path_diff_rc": 0,
    }

    def fake_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        calls["git"].append((args, Path(cwd)))
        if args[:2] == ["log", "--reverse"]:
            return _proc("".join(f"{s}\n" for s in calls["subjects"]))
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            if args[3].endswith("^{commit}") and "hub_commits" in calls:
                # the hub's answer for the commit to land (gr741695)
                sha = calls["hub_commits"].get(args[3][: -len("^{commit}")])
                return _proc(sha + "\n") if sha else _proc(rc=1)
            return _proc(FULL_SHA + "\n")
        if args == ["rev-parse", "HEAD"]:
            return _proc((calls.get("cut_head") or "") + "\n")
        if args[:2] == ["checkout", "-q"]:
            calls["checkout"] = args
            return _proc()
        if args[:1] == ["rev-parse"] and len(args) == 2 and args[1].startswith("refs/"):
            # A bare ref-tip read — ``_pushed_tip_ahead`` (gr681208)
            # reads the ``refs/remotes/origin/`` tracking tip after its
            # refspec fetch.
            return _proc(calls.get("tips", {}).get(args[1], ""))
        if args == ["rev-parse", "--absolute-git-dir"]:
            return _proc(calls["gitdir"] + "\n" if calls["gitdir"] else "")
        if args == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
            return _proc(calls["common"] + "\n" if calls["common"] else "")
        if args[:2] == ["fetch", "--quiet"]:
            return _proc(rc=calls["fetch_rc"])
        if args[:2] == ["diff", "--quiet"]:
            # A path-scoped proof ("diff --quiet a b -- <paths>") answers
            # path_diff_rc; the whole-tree check answers diff_rc.
            return _proc(rc=calls["path_diff_rc"] if "--" in args else calls["diff_rc"])
        if args[:2] == ["merge-base", "--is-ancestor"]:
            # rc 0 = the candidate sha is an ancestor of the land base;
            # rc 1 = ahead (``_pushed_tip_ahead`` wants the latter).
            return _proc(rc=calls.get("ancestor_rc", 0))
        if args[:1] == ["merge-base"]:
            return _proc(calls["merge_base"] + "\n" if calls["merge_base"] else "")
        if args[:1] == ["log"] and any(a.startswith("--max-count=") for a in args):
            return _proc(calls["main_log"])
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

    def fake_changed_paths(tree: Path, base: str = "origin/main") -> Any:
        calls.setdefault("changed_base", []).append(base)
        return calls["changed_paths"]

    monkeypatch.setattr(run_slice, "changed_paths", fake_changed_paths)
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

    def fake_preflight(tree: Path, **kw: Any) -> dict[str, Any]:
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
    # tests asserting "no credential wiring without a token" must hold on
    # the factory host too, where the worker env carries the token
    monkeypatch.delenv("PRECIS_FACTORY_GIT_TOKEN", raising=False)
    # gr741695: the land's own clone, named by the job that cut it. Most
    # tests stand the folder already there (a job resumed in its own
    # folder, which cuts nothing); ``seams["ws"]`` holds the workspace
    # layer's calls for the ones that cut (``_fake_workspace``).
    _fake_workspace(monkeypatch, calls, tmp_path)
    tree = calls["attempt_dir"](tmp_path, 42, 901, "land")
    (tree / "scratch").mkdir(parents=True)
    # a folder that stands for a job already launched in it: the ship's log
    # is the evidence a resubmit keeps the folder (a folder with none is an
    # unfinished cut and is cut again)
    (tree / "scratch" / "land_slice.log").write_text("", encoding="utf-8")
    calls["tree"] = tree
    calls["gitdir"] = str(tmp_path / "gitdir")
    Path(calls["gitdir"]).mkdir()
    return calls


#: The repository's GitHub url the fixture's checkout names.
GITHUB_URL = "https://github.com/example/precis.git"


def _fake_workspace(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, Any], root: Path
) -> None:
    """Stand ``_slice_ws`` (the workspace layer, tested by its owner) as a
    recorder: its calls go to ``calls["ws"]`` as ``(name, args)`` in order;
    ``cut`` makes the folder and ``drop`` removes it, as the real ones do.
    Its pure path functions (``attempt_dir``, ``parse_attempt_dir``,
    ``hub_path``, ``build_ref``) are the real ones; the hub sits in the
    checkout's git common dir, which the fixture makes."""
    from precis.workers.job_types import _slice_ws

    ws: list[tuple[str, Any]] = []
    calls["ws"] = ws
    calls["hub_tips"] = {}
    calls["legacy_tip"] = None
    calls["cut_error"] = None
    calls["hub_error"] = None
    calls["sync_tip"] = "b" * 40  # None: GitHub could not be read
    calls["wip"] = set()  # the shas whose commit subject is ``wip: ...``
    (root / ".git").mkdir(exist_ok=True)

    def ensure_hub(r: Path, *, notes: list[str] | None = None) -> Path:
        ws.append(("ensure_hub", r))
        if calls["hub_error"]:
            raise _slice_ws.WorkspaceError(calls["hub_error"])
        return _slice_ws.hub_path(r)

    def sync_hub(r: Path, *, url: str, base: str, **kw: Any) -> str | None:
        ws.append(("sync_hub", {"url": url, "base": base, **kw}))
        return calls["sync_tip"]

    def hub_tip(hub: Path, ref: str) -> str | None:
        ws.append(("hub_tip", ref))
        return calls["hub_tips"].get(ref)

    def import_legacy(hub: Path, url: str, todo: int, **kw: Any) -> str | None:
        ws.append(("import_legacy", {"url": url, "todo": todo}))
        tip = calls["legacy_tip"]
        if tip is not None:
            calls["hub_tips"][f"refs/factory/build/{todo}/0"] = tip
        return tip

    def cut(hub: Path, tree: Path, **kw: Any) -> str:
        ws.append(("cut", {"hub": hub, "tree": tree, **kw}))
        if calls["cut_error"]:
            raise _slice_ws.WorkspaceError(calls["cut_error"])
        (tree / "scratch").mkdir(parents=True)
        # the real cut checks out the start commit (the ref's tip)
        calls["cut_head"] = kw["start"].sha
        return str(calls["cut_head"])

    def drop(tree: Path, *, notes: list[str] | None = None) -> bool:
        ws.append(("drop", tree))
        import shutil

        shutil.rmtree(tree, ignore_errors=True)
        return not tree.exists()

    def delete_builds(hub: Path, todo: int) -> str:
        ws.append(("delete_builds", todo))
        return f"attempt refs of td{todo}: deleted"

    def _is_wip(hub: Path, sha: str) -> bool:
        return sha in calls["wip"]

    for fn in (
        _is_wip,
        ensure_hub,
        sync_hub,
        hub_tip,
        import_legacy,
        cut,
        drop,
        delete_builds,
    ):
        monkeypatch.setattr(_slice_ws, fn.__name__, fn)
    calls["attempt_dir"] = _slice_ws.attempt_dir
    monkeypatch.setattr(land_slice, "_github_url", lambda root: GITHUB_URL)


def _ws_names(seams: dict[str, Any]) -> list[str]:
    """The workspace layer's calls so far, by name and in order."""
    return [name for name, _arg in seams["ws"]]


def _ws_args(seams: dict[str, Any], name: str) -> list[Any]:
    return [arg for n, arg in seams["ws"] if n == name]


def _legacy_handle(handle: dict[str, Any], tmp_path: Path) -> Path:
    """Re-point ``handle`` at a linked worktree of the host checkout, as a
    land minted before gr741695 and still running holds: a folder that is
    not one of the lander's own clones (``slice-td<id>``)."""
    legacy = tmp_path / ".claude" / "worktrees" / "slice-td42"
    (legacy / "scratch").mkdir(parents=True, exist_ok=True)
    handle["worktree"] = str(legacy)
    return legacy


def _params(seams: dict[str, Any]) -> dict[str, Any]:
    return {
        "todo_id": 42,
        "branch": "slice/42",
        "lane": "claude-sub",
    }


def _submit(store: _Store, seams: dict[str, Any]) -> tuple[_Ctx, Any]:
    ctx = _Ctx(store, _params(seams))
    return ctx, land_slice.submit(ctx, None)


#: The engine path the engine-land tests stand once the land checks the
#: resolved deny set (gr645524): ``scripts/ship`` is denied under every
#: mode (``INTERIM_DENY``), so it can no longer be the fixture's engine
#: example — ``run_slice.py`` is FACTORY-deny under strict and
#: builder-writable when ``factory.deny_relaxed`` is on (td574937), and
#: it was gr645524's own incident diff.
_ENGINE_PATH = "src/precis/workers/job_types/run_slice.py"


def _relaxed_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand ``factory.deny_relaxed`` on (td574937): the resolved set is
    ``HUMAN_ONLY_PATHS + INTERIM_DENY`` — the engine python relaxes out,
    the ship/test/deploy harness and the human-only files stay denied."""
    monkeypatch.setattr(
        lanes,
        "deny_paths_for",
        lambda store: lanes.HUMAN_ONLY_PATHS + lanes.INTERIM_DENY,
    )


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


def test_deadline_kill_meta_marks_the_timed_out_verdict() -> None:
    """gr647914: a wall-deadline kill of a land is a distinct timed-out
    verdict — attempt-free (the deadline, not the diff, ended it) and
    capped at the re-land ask so a possibly half-landed ship never
    auto re-lands. ``ssh_node`` merges ``deadline_kill_meta`` into the
    ``swept:wall-timeout`` write."""
    spec = get_job_type("land_slice")
    assert spec is not None
    assert spec.deadline_kill_meta == {
        "attempt_free": "infra-land",
        "red_class": "timed-out",
        "land_again_cap": True,
    }


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


def test_validate_submit_admits_a_closed_todo_carrying_pending_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr570506: the pre-land defer closes the build done and stamps
    ``meta.pending_land``; the land the tick mints on `review: LAND`
    then finds a closed todo — admitted only while the stamp is there."""
    store = _Store()
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    store.add_todo(
        44,
        status="done",
        meta={"pending_land": {"todo_id": 44, "branch": "slice/44"}},
    )
    store.add_todo(45, status="done")
    assert land_slice.validate_submit(store, params={"todo_id": 44}) is None
    closed = land_slice.validate_submit(store, params={"todo_id": 45})
    assert closed is not None and "is closed" in closed


@pytest.fixture(params=["no-lander", "real-registry"])
def catpath_registry(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> str:
    """``catpath`` with no lander on its profile, and as the registry has it
    (``land_slice`` is its lander, but it is not in ``factory.open_repos``):
    this lander refuses it either way."""
    if request.param == "no-lander":
        profile = replace(lanes.REPO_PROFILES["catpath"], lander=None)
        monkeypatch.setitem(lanes.REPO_PROFILES, "catpath", profile)
    return str(request.param)


@pytest.fixture
def catpath_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """``catpath`` with a ``land_slice`` lander AND listed as open."""
    profile = replace(lanes.REPO_PROFILES["catpath"], lander="land_slice")
    monkeypatch.setitem(lanes.REPO_PROFILES, "catpath", profile)
    monkeypatch.setattr(
        lanes, "open_repos", lambda _s: frozenset({"precis", "catpath"})
    )


def test_validate_submit_refuses_another_repo(
    monkeypatch: pytest.MonkeyPatch, catpath_registry: str
) -> None:
    """td659522 step 2: another repo lands here only with a lander on its
    profile AND the repo opened; otherwise refused with a next: step."""
    store = _Store()
    store.add_todo(42, tags=("repo:catpath",))
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    refused = land_slice.validate_submit(
        store, params={"todo_id": 42, "repo": "catpath"}
    )
    assert refused is not None and refused.startswith("land_slice:")
    assert "'catpath'" in refused and "next:" in refused
    if catpath_registry == "no-lander":
        assert "no lander" in refused
    else:
        assert "open_repos" in refused
    store.add_todo(43)
    assert land_slice.validate_submit(store, params={"todo_id": 43}) is None
    assert (
        land_slice.validate_submit(store, params={"todo_id": 43, "repo": "precis"})
        is None
    )
    assert "repo" in land_slice.PARAMS_SCHEMA["properties"]


def test_submit_refuses_another_repo_before_any_work(
    seams: dict[str, Any], catpath_registry: str, tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(42, tags=("repo:catpath",))
    ctx = _Ctx(store, {**_params(seams), "repo": "catpath"})
    assert land_slice.submit(ctx, None) is None
    assert ctx.status == "failed" and ctx.failure is not None
    assert "'catpath'" in ctx.failure and "next:" in ctx.failure
    # Nothing ran: no git, no ship, the worktree is still there.
    assert seams["git"] == [] and seams["launch"] == []
    assert seams["tree"].is_dir() and ctx.meta_set == {}
    # An explicit default repo lands as before.
    store.add_todo(44)
    (seams["attempt_dir"](tmp_path, 44, 901, "land") / "scratch").mkdir(parents=True)
    (
        seams["attempt_dir"](tmp_path, 44, 901, "land") / "scratch" / "land_slice.log"
    ).touch()
    ctx = _Ctx(store, {**_params(seams), "todo_id": 44, "repo": "precis"})
    handle = land_slice.submit(ctx, None)
    assert ctx.failure is None and handle is not None
    assert seams["launch"]


def test_a_todo_tagged_for_another_repo_never_lands_here(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, catpath_registry: str
) -> None:
    """A land job without ``params.repo`` (a hand mint, a pre-profile job) is
    held to the todo's own tag: catpath-tagged work is refused before any git
    or ship call, even with a lander on the profile and the repo opened."""
    monkeypatch.setattr(
        lanes, "open_repos", lambda _s: frozenset({"precis", "catpath"})
    )
    store = _Store()
    store.add_todo(42, tags=("repo:catpath",))
    store.add_todo(43, tags=("repo:precis",))
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    refused = land_slice.validate_submit(store, params={"todo_id": 42})
    assert refused is not None and refused.startswith("land_slice:")
    assert "'catpath'" in refused and "next:" in refused
    ctx = _Ctx(store, _params(seams))
    assert land_slice.submit(ctx, None) is None
    assert ctx.status == "failed" and "'catpath'" in str(ctx.failure)
    assert seams["git"] == [] and seams["launch"] == []
    assert land_slice.validate_submit(store, params={"todo_id": 43}) is None


def test_no_repo_param_is_refused_even_with_the_repo_opened(
    catpath_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    store = _Store()
    store.add_todo(42, tags=("repo:catpath",))
    refused = land_slice.validate_submit(store, params={"todo_id": 42})
    assert refused is not None and "no params.repo" in refused and "next:" in refused


def test_tag_and_param_disagreement_is_refused(
    seams: dict[str, Any], catpath_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    store = _Store()
    store.add_todo(42, tags=("repo:catpath",))
    store.add_todo(43)
    # the param says precis, the tag says catpath
    refused = land_slice.validate_submit(
        store, params={"todo_id": 42, "repo": "precis"}
    )
    assert refused is not None and "next:" in refused
    # the param says catpath, the todo carries no tag
    refused = land_slice.validate_submit(
        store, params={"todo_id": 43, "repo": "catpath"}
    )
    assert refused is not None and "resolves to repo" in refused
    ctx = _Ctx(store, {**_params(seams), "todo_id": 43, "repo": "catpath"})
    assert land_slice.submit(ctx, None) is None and ctx.status == "failed"
    assert seams["git"] == [] and seams["launch"] == []


class _RepoLandSpy:
    """Stands in for ``repo_land``: records which half the dispatcher called."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []

    def submit(self, ctx: Any, spec: Any) -> Any:
        self.calls.append(("submit", ctx, spec))
        return {"lander": "repo_land", "repo": "catpath"}

    def poll(self, ctx: Any, handle: Any) -> bool:
        self.calls.append(("poll", ctx, handle))
        return True

    def kill(self, ctx: Any, handle: Any) -> None:
        self.calls.append(("kill", ctx, handle))


@pytest.fixture
def repo_land_spy(monkeypatch: pytest.MonkeyPatch) -> _RepoLandSpy:
    from precis.workers import repo_land

    spy = _RepoLandSpy()
    monkeypatch.setattr(repo_land, "submit", spy.submit)
    monkeypatch.setattr(repo_land, "poll", spy.poll)
    monkeypatch.setattr(repo_land, "kill", spy.kill)
    return spy


def test_an_opened_repo_with_a_lander_submits_through_repo_land(
    seams: dict[str, Any],
    catpath_open: None,
    repo_land_spy: _RepoLandSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    store = _Store()
    store.add_todo(42, tags=("repo:catpath",))
    params = {**_params(seams), "repo": "catpath"}
    assert land_slice.validate_submit(store, params=params) is None
    ctx = _Ctx(store, params)
    sentinel = object()
    handle = land_slice.submit(ctx, sentinel)
    assert handle == {"lander": "repo_land", "repo": "catpath"}
    assert repo_land_spy.calls == [("submit", ctx, sentinel)]
    # the precis path never ran: no git, no ship launch, no failure
    assert seams["git"] == [] and seams["launch"] == []
    assert ctx.failure is None and ctx.meta_set == {}


@pytest.mark.parametrize(
    "handle",
    [
        {"lander": "repo_land", "repo": "catpath", "pid": 7},
        {"no_land": True, "repo": "catpath"},
    ],
)
def test_poll_routes_a_repo_handle_to_repo_land(
    seams: dict[str, Any], repo_land_spy: _RepoLandSpy, handle: dict[str, Any]
) -> None:
    ctx = _Ctx(_Store(), _params(seams))
    assert land_slice.poll(ctx, handle) is True
    assert repo_land_spy.calls == [("poll", ctx, handle)]
    assert seams["git"] == [] and seams["launch"] == []


def test_poll_and_kill_route_on_the_job_params_repo(
    seams: dict[str, Any], repo_land_spy: _RepoLandSpy
) -> None:
    ctx = _Ctx(_Store(), {**_params(seams), "repo": "catpath"})
    assert land_slice.poll(ctx, {"pid": 7}) is True
    land_slice.kill(ctx, {"pid": 7})
    assert [c[0] for c in repo_land_spy.calls] == ["poll", "kill"]


def test_kill_routes_a_repo_handle_and_never_pushes_the_branch(
    seams: dict[str, Any],
    repo_land_spy: _RepoLandSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_push(tree: Path, branch: str) -> str:
        raise AssertionError("the precis branch push ran for a repo handle")

    monkeypatch.setattr(land_slice, "_push_branch", no_push)
    handle = {"lander": "repo_land", "repo": "catpath", "worktree": str(seams["tree"])}
    ctx = _Ctx(_Store(), _params(seams))
    land_slice.kill(ctx, handle)
    assert repo_land_spy.calls == [("kill", ctx, handle)]
    assert seams["git"] == []


def test_validate_submit_refuses_a_held_build_the_coordinator_ruled_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr663909: the pre-land exception is the machine's own ``done``
    hold only — a ``won't-do``/``auto-timeout`` set on the stamped build
    while its land queued is a ruling the queued land must not land
    over."""
    store = _Store()
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    for todo_id, status in ((48, "won't-do"), (49, "auto-timeout")):
        store.add_todo(
            todo_id,
            status=status,
            meta={"pending_land": {"todo_id": todo_id, "branch": f"slice/{todo_id}"}},
        )
        refused = land_slice.validate_submit(store, params={"todo_id": todo_id})
        assert refused is not None
        assert "is closed" in refused and f"STATUS:{status}" in refused


def test_submit_on_a_ruled_out_held_build_launches_no_ship(
    seams: dict[str, Any],
) -> None:
    """The same refusal end to end: ``submit`` fails the queued land on
    the closed todo and never launches ship; the ruling and the
    ``pending_land`` stamp stand."""
    store = _Store()
    store.add_todo(
        42,
        status="won't-do",
        meta={"pending_land": {"todo_id": 42, "branch": "slice/42"}},
    )
    ctx = _Ctx(store, _params(seams))
    assert land_slice.submit(ctx, None) is None
    assert ctx.status == "failed"
    failure = ctx.failure or ""
    assert "land_slice:" in failure and "STATUS:won't-do" in failure
    assert seams["launch"] == []
    assert "STATUS:won't-do" in store.tag_strings(42)


def test_record_land_clears_pending_land() -> None:
    """The ``landed:`` record discharges the hold — a lingering stamp
    would read a post-land review as pre-land."""
    store = _Store()
    store.add_todo(
        46,
        status="done",
        meta={
            "pending_land": {"todo_id": 46, "branch": "slice/46"},
            "preland_fix_rounds": {"count": 2, "finding": "0123456789abcdef"},
        },
    )
    wrote = land_slice._record_land(store, 46, "abc1234def", "feat(x): y")
    assert "pending_land cleared" in wrote
    assert "landed comment" in wrote
    assert not store.refs[46].meta.get("pending_land")
    # gr684815: the pre-land FIX-round count ends with the land.
    assert not store.refs[46].meta.get("preland_fix_rounds")
    assert store.comments(46)[-1].startswith("landed: abc1234de")


def test_record_land_without_pending_land_changes_nothing() -> None:
    """An ordinary land (builder minted it, todo still doing) writes the
    landed comment + STATUS:done and touches no hold key."""
    store = _Store()
    store.add_todo(47, status="doing")
    wrote = land_slice._record_land(store, 47, "abc1234def", "feat(x): y")
    assert "pending_land cleared" not in wrote
    assert "landed comment" in wrote and "STATUS:done" in wrote


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
    # a partial ``refs grN`` joins the list too (gr659980)
    assert land_slice.with_handle("feat(x): y (td1, refs gr2)", 42) == (
        "feat(x): y (td1, refs gr2, td42)"
    )
    assert land_slice.with_handle("feat(x): y (refs gr2)", 42) == (
        "feat(x): y (refs gr2, td42)"
    )
    assert land_slice.with_handle("feat(x): y (td42, refs gr2)", 42) == (
        "feat(x): y (td42, refs gr2)"
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


@pytest.mark.parametrize("breaks", ["set_meta", "append_chunk"])
def test_submit_returns_the_ships_handle_when_the_bookkeeping_after_it_raises(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, breaks: str
) -> None:
    """Once the ship is launched its handle must reach the executor: a raise
    in the bookkeeping after the launch would end the job while the ship
    goes on, and the host sweep would then stop that ship."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx = _Ctx(store, _params(seams))
    real_meta, real_chunk = ctx.set_meta, ctx.append_chunk

    def set_meta(**kw: Any) -> None:
        if breaks == "set_meta" and "land_subject" in kw:
            raise RuntimeError("the database went away")
        real_meta(**kw)

    def append_chunk(kind: str, text: str) -> None:
        if breaks == "append_chunk" and " pid 5151 " in text:
            raise RuntimeError("the database went away")
        real_chunk(kind, text)

    monkeypatch.setattr(ctx, "set_meta", set_meta)
    monkeypatch.setattr(ctx, "append_chunk", append_chunk)
    handle = land_slice.submit(ctx, None)
    assert isinstance(handle, dict) and handle["pid"] == 5151
    assert ctx.failure is None and ctx.status is None


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
    # selection at -n6 OOM-killed a worker on maple twice). PRECIS_GATE_N
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


# ── launch env (gr659929) ──────────────────────────────────────────


def test_land_env_scrubs_credentials_keeps_knobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr659929: every land child starts from the allowlist env — the
    worker's DSN, vault and token material stays in the worker, while
    the operator's ship/test/gate knobs still pass through."""
    # Values stay non-credential-shaped literals — the repo secret scan
    # flags even fake `scheme://user:pass@` forms (a runtime-joined fake
    # is the alternative; see test_factory_git.py).
    for name, value in {
        "PRECIS_FACTORY_GIT_TOKEN": "tok-a",
        "PRECIS_MCP_TOKEN": "tok-b",
        "PRECIS_DATABASE_URL": "prod-dsn",
        "PRECIS_TEST_PG_URL": "test-dsn",
        "PRECIS_EMBEDDER_URL": "embedder-endpoint",
        "UV_INDEX_URL": "private-index",
        "GH_TOKEN": "ghp_x",
        "ANTHROPIC_API_KEY": "sk-live",
        "PRECIS_OAUTH_SECRET": "s",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PRECIS_SHIP_LOCKED_NET_TIMEOUT", "90")
    # the checked selector's measured map must keep reaching the branch
    # tree's impacted-tests (tests/test_deploy_coverage_map.py)
    monkeypatch.setenv("PRECIS_COVERAGE_DB_DIR", "/cov")
    monkeypatch.setenv("PRECIS_TEST_PASSENV", "FOO,BAR")
    monkeypatch.setenv("LC_ALL", "C")
    env = land_slice.land_env()
    for dropped in (
        "PRECIS_FACTORY_GIT_TOKEN",
        "PRECIS_MCP_TOKEN",
        "PRECIS_DATABASE_URL",
        "PRECIS_TEST_PG_URL",
        "PRECIS_EMBEDDER_URL",
        "UV_INDEX_URL",
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "PRECIS_OAUTH_SECRET",
    ):
        assert dropped not in env, dropped
    assert env["PRECIS_SHIP_LOCKED_NET_TIMEOUT"] == "90"
    assert env["PRECIS_COVERAGE_DB_DIR"] == "/cov"
    assert env["PRECIS_TEST_PASSENV"] == "FOO,BAR"
    assert env["LC_ALL"] == "C"
    # set, not inherited: a planted refs/replace must not substitute the
    # object a child's trusted read resolves (pre-land review pass)
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    for leaked in ("tok-a", "tok-b", "sk-live", "ghp_x"):
        assert leaked not in env.values()


@pytest.mark.parametrize("existing_uv", [False, True])
def test_land_env_finds_user_uv_without_replacing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_uv: bool
) -> None:
    """gr702644: non-login land services must reach uv in nested helpers."""
    home = tmp_path / "coder"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    installed = local_bin / "uv"
    installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    installed.chmod(0o755)
    service_bin = tmp_path / "service-bin"
    service_bin.mkdir()
    if existing_uv:
        configured = service_bin / "uv"
        configured.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        configured.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(service_bin))
    env = land_slice.land_env()
    expected = str(service_bin)
    if not existing_uv:
        expected += os.pathsep + str(local_bin)
    assert env["PATH"] == expected
    result = subprocess.run(
        ["/usr/bin/env", "uv"], env=env, check=False, capture_output=True
    )
    assert result.returncode == 0


def test_submit_ship_env_is_allowlist_not_whole_worker_env(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr659929: ship starts from the allowlist env, never the worker's
    whole env — and with no token resolvable it also carries no
    credential wiring (the host's own git credentials apply, like the
    other launches)."""
    monkeypatch.setenv("PRECIS_DATABASE_URL", "prod-dsn")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live")
    monkeypatch.setenv("VAULT_VAR", "v")
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None and ctx.status is None
    [(argv, kw)] = seams["launch"]
    assert argv == [str(seams["tree"] / "scripts" / "ship"), "--quick", SUBJECT]
    for leaked in ("PRECIS_DATABASE_URL", "ANTHROPIC_API_KEY", "VAULT_VAR"):
        assert leaked not in kw["env"], leaked
    assert "sk-live" not in kw["env"].values()
    # with no token resolvable in the test env no wiring is added
    assert land_slice._factory_git.SECRET_NAME not in kw["env"]
    assert "GH_TOKEN" not in kw["env"]
    assert "GIT_CONFIG_COUNT" not in kw["env"]


def test_ship_env_is_token_free_with_mirrored_git_credentials(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gr659929 (review td660513): ship's environment carries NO token and
    no credential helper — ``scripts/impacted-tests`` and every other
    host-side step under ship run the branch's own code, and the fd-wired
    and socket-brokered wiring this replaced still let them read the token
    back. The lander now refreshes a bare mirror of the land refs under
    the factory checkout's ``scratch/`` with trusted ``git`` calls alone
    (never an interpreter from the slice tree) and hands ship an env
    override pointing ``origin`` at the mirror: ship fetches, squashes and
    CAS-pushes exactly as before, credential-free, and the land flock is
    the only fd riding ``pass_fds``."""
    monkeypatch.setenv("PRECIS_FACTORY_GIT_TOKEN", "tok-factory")
    monkeypatch.setenv("PRECIS_DATABASE_URL", "prod-dsn")
    monkeypatch.setenv("GH_TOKEN", "ghp_ambient")
    tree = seams["tree"]
    (tree / "scripts").mkdir(parents=True, exist_ok=True)
    mirror = tmp_path / "scratch" / "factory-mirror" / "td42.git"
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    real_run = land_slice._factory_git_mirror._run
    mirror_calls: list[tuple[list[str], Path, dict[str, str]]] = []
    tips = {"main": "a" * 40, "prod": "b" * 40}

    def _sub(args: list[str]) -> str:
        # every call goes `git <TRUSTED_ARGS> [-C dir] <sub>`
        rest = args[1 + len(land_slice._factory_git_mirror.TRUSTED_ARGS) :]
        if rest[:1] in (["-C"], ["--git-dir"]):
            rest = rest[2:]
        return rest[0]

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        mirror_calls.append((list(args), Path(cwd), dict(env)))
        sub = _sub(args)
        if sub in ("init", "config"):
            return real_run(args, cwd=cwd, env=env, timeout=timeout)
        if sub == "ls-remote":
            return _proc("".join(f"{s}\trefs/heads/{r}\n" for r, s in tips.items()))
        if sub == "fetch":
            return _proc(rc=0)
        if sub == "push":
            return _proc(rc=0)
        if sub == "rev-parse":
            return _proc(tips[args[-1].removeprefix("refs/heads/")] + "\n")
        return _proc(rc=1)

    monkeypatch.setattr(land_slice._factory_git_mirror, "_run", fake_run)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None and ctx.status is None
    # the mirror replaced the broker: exactly ONE launch — ship itself
    assert len(seams["launch"]) == 1
    # gr741695: the mirrored env keeps the host-state roots of the clone
    ship_env = seams["launch"][0][1]["env"]
    host_common = str((tmp_path / ".git").resolve())
    assert ship_env["PRECIS_GATE_SLOT_ROOT"] == host_common
    assert ship_env["PRECIS_ROUND_STATE_ROOT"] == host_common
    # review findings 1+2 (regression): every credential-bearing call is a
    # `git` argv whose cwd is OUTSIDE the slice tree — nothing
    # branch-importable runs while the token is live, so a branch-added
    # root precis/__init__.py has no interpreter to shadow — each on the
    # trusted boundary (hooks off, no system/global/local config).
    assert mirror_calls, "the mirror was refreshed under the token"
    for args, cwd, env in mirror_calls:
        assert args[:3] == ["git", "-c", "core.hooksPath=/dev/null"]
        assert cwd != tree and tree not in cwd.parents
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    # the populate's github fetch ran in an ops repo, never the slice
    # tree's or the mirror's own gitdir (a planted hooks/ or config there
    # is branch-writable)
    github = [
        c
        for a, c, _e in mirror_calls
        if "github.com" in " ".join(a) and _sub(a) == "fetch"
    ]
    assert github and all(
        str(c).startswith(str(mirror.parent)) and c != mirror for c in github
    )
    # the launch tips were recorded for poll's origin sync — on the
    # handle, and in the launch record BESIDE the mirror under the main
    # checkout's scratch (review td660513: never the tree's own
    # branch-writable scratch, where a deleted file read as "no mirror")
    assert handle["factory_mirror_tips"] == tips
    assert (
        json.loads(
            (tmp_path / "scratch" / "factory-mirror" / "td42.tips.json").read_text(
                encoding="utf-8"
            )
        )
        == tips
    )
    assert not (tree / "scratch" / "factory-mirror-tips.json").exists()
    # the ship launch: an origin override to the mirror — no token, no
    # credential helper, no token fd
    _argv, launch_kw = seams["launch"][0]
    ship_env = launch_kw["env"]
    for leaked in (
        "PRECIS_FACTORY_GIT_TOKEN",
        "GH_TOKEN",
        "PRECIS_DATABASE_URL",
    ):
        assert leaked not in ship_env, leaked
    for value in ship_env.values():
        assert "tok-factory" not in value
        assert "ghp_ambient" not in value
    # review finding 2 (regression): the only GIT_CONFIG entries are the
    # url.<mirror>.insteadOf rewrites pointing the real origin URL at the
    # mirror — no credential.* key of any kind, so a gate child's `git
    # credential fill` has nothing to answer it
    count = int(ship_env["GIT_CONFIG_COUNT"])
    keys = [ship_env[f"GIT_CONFIG_KEY_{i}"] for i in range(count)]
    values = [ship_env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)]
    assert keys == [f"url.{mirror}.insteadOf"] * count
    assert "https://github.com/example/precis.git" in values
    assert not any(k.startswith("credential.") for k in keys)
    assert not any("credential" in v for v in values)
    # its mirror reads and shared-dir merges ignore planted refs/replace
    assert ship_env["GIT_NO_REPLACE_OBJECTS"] == "1"
    # the land flock is the ONLY fd ship inherits — no credential fd
    fds = launch_kw["pass_fds"]
    assert len(fds) == 1 and fds[0] >= 100
    # finding 4, second pass (regression): ship's PATH leads with this
    # launch's git shim — the shared local-main advance is refused until
    # the lander's mirror sync verifies origin, then replayed there
    assert ship_env["PATH"].split(os.pathsep)[0] == str(mirror / "ship-bin")
    shim = mirror / "ship-bin" / "git"
    assert shim.exists() and shim.stat().st_mode & 0o111
    # the verify gate's env is the bare allowlist — no token, no wiring
    v = land_slice._launch_verify(
        tree, ["tests/test_x.py::test_t"], phase="tree", run_tree=tree
    )
    assert v is not None
    _, verify_kw = seams["launch"][-1]
    verify_env = verify_kw["env"]
    for leaked in (
        "PRECIS_FACTORY_GIT_TOKEN",
        "GH_TOKEN",
        "PRECIS_DATABASE_URL",
        "GIT_CONFIG_COUNT",
    ):
        assert leaked not in verify_env, leaked
    assert "tok-factory" not in verify_env.values()
    assert verify_env["UV_PYTHON"] == land_slice.ship_python()
    assert verify_env["PRECIS_GATE_SLOTS"] == land_slice.land_gate_slots()


def test_local_git_never_runs_the_worker_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gr659929 (review td660513): ``land_slice._git`` delegated to
    ``run_slice._git`` — the worker's whole env, factory token and
    ``GH_TOKEN`` included — on calls whose repo config is
    branch-writable, so a planted hook, filter or transport redirect
    met the token in a spawned child's env. The seam now runs the
    scrubbed local env with hooks off."""
    monkeypatch.setenv("PRECIS_FACTORY_GIT_TOKEN", "tok-factory")
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    monkeypatch.setenv("PRECIS_DATABASE_URL", "prod-dsn")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    seen: dict[str, Any] = {}

    def fake_run(*a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(a[0])
        seen["env"] = dict(kw["env"])
        return _proc()

    monkeypatch.setattr(land_slice.subprocess, "run", fake_run)
    land_slice._git(["rev-parse", "HEAD"], tmp_path)
    assert seen["argv"] == [
        "git",
        *land_slice._factory_git_mirror.TRUSTED_ARGS,
        "rev-parse",
        "HEAD",
    ]
    assert "--no-replace-objects" in seen["argv"]
    env = seen["env"]
    for leaked in ("PRECIS_FACTORY_GIT_TOKEN", "GH_TOKEN", "PRECIS_DATABASE_URL"):
        assert leaked not in env, leaked
    assert "GIT_CONFIG_COUNT" not in env
    assert not any(k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) for k in env)
    assert "tok-factory" not in env.values()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_cleanup_origin_delete_is_an_ops_repo_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gr659929 (review td660513): cleanup's remote delete is an
    authenticated transport — the on-origin check is the ``ls_remote``
    oracle and the ``--delete`` pushes to the trusted url from a fresh
    ops gitdir under the credential wiring. The branch-writable shared
    dir, its config and its hooks never meet the token."""
    monkeypatch.setenv("PRECIS_FACTORY_GIT_TOKEN", "tok-factory")
    url = "https://github.com/example/precis.git"
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "github_url", lambda root, *, env: url
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "ls_remote",
        lambda u, refs, *, env, **_kw: {"slice/42": FULL_SHA},
    )

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), Path(cwd), dict(env)))
        return _proc(rc=0)

    monkeypatch.setattr(land_slice._factory_git_mirror, "_run", fake_run)
    assert land_slice._delete_remote_branch(tmp_path, "slice/42") == [
        "push --delete: ok"
    ]
    [(dargs, dcwd, denv)] = [t for t in calls if "--delete" in t[0]]
    assert dargs[:3] == ["git", "-c", "core.hooksPath=/dev/null"]
    assert dargs[-5:] == [
        "push",
        url,
        "--delete",
        "refs/factory/slice/42",
        "refs/heads/slice/42",
    ]
    # a fresh, lander-created gitdir under the mirror root — never the
    # shared common dir whose config and hooks are branch-writable
    assert dcwd.parent == land_slice._factory_git_mirror.mirror_root(tmp_path)
    # the credential wiring rides ONLY this call's env (token + helper)
    assert denv["PRECIS_FACTORY_GIT_TOKEN"] == "tok-factory"
    assert denv["GH_TOKEN"] == "tok-factory"
    assert denv["GIT_CONFIG_NOSYSTEM"] == "1"
    # the ops gitdir's own init ran credential-free
    inits = [e for a, _c, e in calls if "init" in a]
    assert inits and all("PRECIS_FACTORY_GIT_TOKEN" not in e for e in inits)


def test_cleanup_origin_delete_skips_without_a_remote_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The oracle showing no branch on origin skips the delete; no
    trusted url skips it too — a note, never a ``push origin`` in the
    shared gitdir."""
    pushes: list[list[str]] = []
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "ls_remote", lambda u, refs, *, env, **_kw: {}
    )

    def spy_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        pushes.append(list(args))
        return _proc(rc=0)

    monkeypatch.setattr(land_slice._factory_git_mirror, "_run", spy_run)
    assert land_slice._delete_remote_branch(tmp_path, "slice/42") == [
        "origin branch: not pushed"
    ]
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "github_url", lambda root, *, env: None
    )
    assert land_slice._delete_remote_branch(tmp_path, "slice/42") == [
        "origin branch: skipped (no trusted origin url)"
    ]
    assert not any("push" in a for a in pushes)


def test_cleanup_origin_delete_only_names_the_refs_origin_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The slice lives under the hidden ``refs/factory/`` namespace now
    (no GitHub branch, no PR); a land deletes only that ref when no
    older build left a visible ``refs/heads/`` branch too."""
    url = "https://github.com/example/precis.git"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "github_url", lambda root, *, env: url
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "ls_remote",
        lambda u, refs, *, env, namespace="refs/heads/": (
            {"slice/42": FULL_SHA} if namespace == land_slice.SLICE_REF_NS else {}
        ),
    )

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return _proc(rc=0)

    monkeypatch.setattr(land_slice._factory_git_mirror, "_run", fake_run)
    assert land_slice._delete_remote_branch(tmp_path, "slice/42") == [
        "push --delete: ok"
    ]
    [dargs] = [a for a in calls if "--delete" in a]
    assert dargs[-4:] == ["push", url, "--delete", "refs/factory/slice/42"]


RELEASE_GREEN_LOG = (
    "release fix → release/r7 only (--onto; main is not touched)\n"
    "shipped — landed on release/r7:\n"
    f"abc1234 {SUBJECT}\n"
    "SHIP_STATUS: shipped\n"
)


def _main_reads(seams: dict[str, Any]) -> list[list[str]]:
    """Every git call this land made against main's ref."""
    return [
        args
        for args, _cwd in seams["git"]
        if "main" in args or any("origin/main" in a for a in args)
    ]


@pytest.mark.parametrize("where", ["meta", "brief"])
def test_a_release_fix_lands_on_its_release_branch_and_leaves_main_alone(
    seams: dict[str, Any], where: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """release-train T1: a slice whose todo names ``release/r7`` as its
    land target — ``meta.land_target`` or a ``land target:`` brief line —
    ships ``--quick --onto release/r7`` (lint-only even on an engine
    diff), reads its subject and the already-landed proof against the
    release branch, never probes main's drift gate, and takes its sha from
    ship's ``landed on release/r7:`` line. No git call touches main."""
    store = _Store()
    if where == "meta":
        store.add_todo(42, meta={"land_target": "release/r7", "op": "run_slice"})
    else:
        store.add_todo(42)
        store.chunks.rows[42].append(
            _Chunk(0, "Fix the cluster.\nland target: release/r7", "todo_body")
        )
    _relaxed_deny(monkeypatch)
    seams["changed_paths"] = [_ENGINE_PATH]  # would earn --checked on main
    ctx, handle = _submit(store, seams)
    tree = seams["tree"]
    assert ctx.failure is None, ctx.failure
    [(argv, _kw)] = seams["launch"]
    assert argv == [
        str(tree / "scripts" / "ship"),
        "--quick",
        "--onto",
        "release/r7",
        SUBJECT,
    ]
    assert ctx.meta_set["land_target"] == "release/r7"
    assert ctx.meta_set["spot_selection"] is False
    assert seams["probed"] == [], "main's drift preflight ran for a release fix"
    calls = [args for args, _cwd in seams["git"]]
    assert [
        "log",
        "--reverse",
        "--no-merges",
        "--format=%s",
        "origin/release/r7..HEAD",
    ] in calls
    refspec = "+refs/heads/release/r7:refs/remotes/origin/release/r7"
    assert ["fetch", "--quiet", "origin", refspec] in calls
    assert ["diff", "--quiet", "origin/release/r7", "HEAD"] in calls
    assert _main_reads(seams) == []

    pctx = _Ctx(store, _params(seams))
    _finish(handle, 0, RELEASE_GREEN_LOG)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded", pctx.failure
    assert pctx.meta_set["landed_sha"] == FULL_SHA
    assert ["rev-parse", "--verify", "--quiet", "abc1234^{commit}"] in [
        args for args, _cwd in seams["git"]
    ]
    assert _main_reads(seams) == []
    assert any(c.startswith("landed: abc123400") for c in store.comments(42))


# ── release sync (gr738762): the moved release head is merged in first ──

SHIP_LAUNCH = ["<ship launch>"]


def _release_sync_seams(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    ancestor_rc: int = 1,
    status: str = "",
    merge_rc: int = 0,
    merge_out: str = "",
    conflicted: str = "",
) -> list[list[str]]:
    """Answer the sync's git reads over the fixture's fake git and put a
    ``SHIP_LAUNCH`` marker into the same call list, so a test can assert
    the merge came before the launch. Returns that call list."""
    base = land_slice._git
    base_launch = land_slice._launch

    def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        # The merge carries ``-c user.*=…`` before its subcommand; the call
        # list records it without them, the identity goes to ``merge_cfg``.
        cfg = [args[i + 1] for i in range(0, len(args) - 1, 2) if args[i] == "-c"]
        if cfg:
            seams.setdefault("merge_cfg", []).extend(cfg)
            args = args[2 * len(cfg) :]
        if args[:1] == ["merge"]:
            seams["git"].append((args, Path(cwd)))
            return _proc(merge_out, rc=0 if args[1] == "--abort" else merge_rc)
        if args[:2] == ["status", "--porcelain"]:
            seams["git"].append((args, Path(cwd)))
            return _proc(status)
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            seams["git"].append((args, Path(cwd)))
            return _proc(conflicted)
        return base(args, cwd)

    def launch(argv: list[str], **kw: Any) -> int:
        seams["git"].append((SHIP_LAUNCH, Path(kw["cwd"])))
        return base_launch(argv, **kw)

    seams["ancestor_rc"] = ancestor_rc
    monkeypatch.setattr(land_slice, "_git", git)
    monkeypatch.setattr(land_slice, "_launch", launch)
    return [args for args, _cwd in seams["git"]]


def _release_store() -> _Store:
    store = _Store()
    store.add_todo(42, meta={"land_target": "release/r7", "op": "run_slice"})
    return store


def _git_calls(seams: dict[str, Any]) -> list[list[str]]:
    return [args for args, _cwd in seams["git"]]


def test_a_moved_release_head_is_merged_in_before_ship_launches(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release head moved since the branch was cut and merges
    cleanly: the merge runs BEFORE ship launches, ship still gets
    ``--onto release/r7`` and the squash subject, and the job says which
    head it synced onto."""
    _release_sync_seams(seams, monkeypatch)
    ctx, handle = _submit(_release_store(), seams)
    assert ctx.failure is None, ctx.failure
    calls = _git_calls(seams)
    merge = ["merge", "--no-edit", "origin/release/r7"]
    assert merge in calls
    assert calls.index(merge) < calls.index(SHIP_LAUNCH)
    [(argv, _kw)] = seams["launch"]
    assert argv[-3:] == ["--onto", "release/r7", SUBJECT]
    assert ctx.meta_set["release_sync"] == f"synced onto {FULL_SHA[:12]}"
    assert handle is not None
    # the merge commit's identity does not depend on host git config
    assert {c.split("=")[0] for c in seams["merge_cfg"]} == {"user.name", "user.email"}


def test_a_release_head_already_in_the_branch_is_not_merged_again(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``origin/release/r7`` is already an ancestor of HEAD: no merge, no
    status read, ship launches."""
    _release_sync_seams(seams, monkeypatch, ancestor_rc=0)
    ctx, handle = _submit(_release_store(), seams)
    assert ctx.failure is None, ctx.failure
    calls = _git_calls(seams)
    assert not any(args[:1] == ["merge"] for args in calls)
    assert ["merge-base", "--is-ancestor", "origin/release/r7", "HEAD"] in calls
    assert len(seams["launch"]) == 1 and handle is not None
    assert ctx.meta_set["release_sync"] == "already current"


def test_a_release_head_that_conflicts_fails_the_land_before_ship(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge conflicts: it is aborted (the tree is restored), ship is
    NOT launched, and the land fails as an ordinary red — an attempt, not
    attempt-free — whose reason carries the marker and whose handoff names
    the path and ``origin/release/r7`` (a merge, never main or a rebase)."""
    _release_sync_seams(seams, monkeypatch, merge_rc=1, conflicted="src/precis/b.py\n")
    store = _release_store()
    ctx, handle = _submit(store, seams)
    assert handle is None and seams["launch"] == []
    calls = _git_calls(seams)
    merge = ["merge", "--no-edit", "origin/release/r7"]
    assert calls.index(merge) < calls.index(["merge", "--abort"])
    assert SHIP_LAUNCH not in calls
    assert ctx.failure is not None
    assert ctx.failure.startswith(
        f"land_slice: {land_slice.RELEASE_SYNC_CONFLICT_MARK}:"
    )
    assert "src/precis/b.py" in ctx.failure
    assert land_slice._rs.ATTEMPT_FREE_KEY not in ctx.meta_set
    handoff = store.comments(42)[-1]
    assert handoff.startswith("handoff: lander: land FAILED")
    assert "release/r7 moved since this branch was cut" in handoff
    assert "src/precis/b.py" in handoff
    assert "Merge origin/release/r7 into the branch" in handoff
    assert "never origin/main" in handoff
    assert ctx.meta_set["release_sync"] == "conflict in src/precis/b.py"


def test_a_main_land_never_touches_a_release_ref(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A land onto main behaves as before: no merge, no status read, no
    ancestry read of any release ref, no ``release_sync`` on the job."""
    _release_sync_seams(seams, monkeypatch)
    store = _Store()
    store.add_todo(42, meta={"op": "run_slice"})
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None, ctx.failure
    calls = _git_calls(seams)
    assert not any(args[:1] == ["merge"] for args in calls)
    assert not any(args[:2] == ["status", "--porcelain"] for args in calls)
    assert not any(args[:2] == ["merge-base", "--is-ancestor"] for args in calls)
    assert not any("release/" in a for args in calls for a in args)
    [(argv, _kw)] = seams["launch"]
    assert "--onto" not in argv and handle is not None
    assert "release_sync" not in ctx.meta_set


def test_a_dirty_tree_skips_the_release_sync_and_ship_launches(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncommitted changes: the merge is not attempted, ship launches as
    before."""
    _release_sync_seams(seams, monkeypatch, status=" M src/precis/b.py\n")
    ctx, handle = _submit(_release_store(), seams)
    assert ctx.failure is None, ctx.failure
    assert not any(args[:1] == ["merge"] for args in _git_calls(seams))
    assert len(seams["launch"]) == 1 and handle is not None
    assert ctx.meta_set["release_sync"] == "skipped: dirty tree"


def test_a_failed_release_fetch_skips_the_sync_and_ship_launches(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release fetch fails (ship reports the network itself): no
    merge, ship launches."""
    _release_sync_seams(seams, monkeypatch)
    seams["fetch_rc"] = 1
    ctx, handle = _submit(_release_store(), seams)
    assert ctx.failure is None, ctx.failure
    assert not any(args[:1] == ["merge"] for args in _git_calls(seams))
    assert len(seams["launch"]) == 1 and handle is not None
    assert ctx.meta_set["release_sync"] == "skipped: fetch failed"


def test_a_merge_that_fails_without_a_conflict_is_aborted_and_ship_launches(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge refused for a non-conflict reason (identity, an untracked
    file in the way) names no path to resolve: aborted, ship launches."""
    _release_sync_seams(
        seams, monkeypatch, merge_rc=128, merge_out="fatal: unable to auto-detect"
    )
    ctx, handle = _submit(_release_store(), seams)
    assert ctx.failure is None, ctx.failure
    assert ["merge", "--abort"] in _git_calls(seams)
    assert len(seams["launch"]) == 1 and handle is not None
    assert ctx.meta_set["release_sync"].startswith("skipped: merge failed")


# The same sync against real git: a fake runner cannot show the tree comes
# back clean after an aborted merge.


def _real_release_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sibling_file: str,
    sibling_text: str = "sibling\n",
    sibling_also: dict[str, str] | None = None,
    host_identity: bool = True,
) -> Path:
    """A clone on ``slice/42`` cut from ``origin/release/r7`` with a fix to
    ``b.txt`` committed; then a sibling change to ``sibling_file`` lands on
    origin's release branch. The process env carries no git config (a
    private HOME, global and system config off); the commit identity is in
    the env only with ``host_identity`` — without it the tree refuses to
    invent one (``user.useConfigOnly``), so only an identity the merge
    supplies itself can succeed."""
    ident = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for name, value in (
        ("GIT_CONFIG_GLOBAL", os.devnull),
        ("GIT_CONFIG_NOSYSTEM", "1"),
        ("HOME", str(tmp_path / "home")),
    ):
        monkeypatch.setenv(name, value)
    for name, value in ident.items():
        if host_identity:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)

    def git(cwd: Path, *args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, **ident},
        )
        return done.stdout.strip()

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    for name in ("a.txt", "b.txt"):
        (seed / name).write_text("base\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "base")
    git(seed, "push", "-q", "origin", "HEAD:main", "HEAD:refs/heads/release/r7")
    tree = tmp_path / "tree"
    git(tmp_path, "clone", "-q", str(origin), str(tree))
    git(tree, "checkout", "-q", "-b", "slice/42", "origin/release/r7")
    (tree / "b.txt").write_text("fix\n", encoding="utf-8")
    git(tree, "commit", "-q", "-am", "fix b")
    (seed / sibling_file).write_text(sibling_text, encoding="utf-8")
    for name, text in (sibling_also or {}).items():
        (seed / name).write_text(text, encoding="utf-8")
    git(seed, "commit", "-q", "-am", "sibling fix")
    git(seed, "push", "-q", "origin", "HEAD:refs/heads/release/r7")
    if not host_identity:
        git(tree, "config", "user.useConfigOnly", "true")
    return tree


def _tree_git(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(tree),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_real_git_a_clean_release_merge_brings_the_sibling_fix_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _real_release_tree(tmp_path, monkeypatch, sibling_file="a.txt")
    ctx = _Ctx(_release_store(), {"todo_id": 42})
    synced = land_slice._sync_release(ctx, tree, "release/r7", 42, "claude-sub")
    assert synced == land_slice.SYNC_MERGED
    assert ctx.failure is None
    assert (tree / "a.txt").read_text(encoding="utf-8") == "sibling\n"
    assert (tree / "b.txt").read_text(encoding="utf-8") == "fix\n"
    assert (
        _tree_git(
            tree, "merge-base", "--is-ancestor", "origin/release/r7", "HEAD"
        ).returncode
        == 0
    )
    tip = _tree_git(tree, "rev-parse", "origin/release/r7").stdout.strip()
    assert ctx.meta_set["release_sync"] == f"synced onto {tip[:12]}"


@pytest.mark.parametrize("main_conflicts", [False, True])
def test_resolver_launch_merges_main_into_the_recorded_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_conflicts: bool
) -> None:
    tree = _real_release_tree(tmp_path, monkeypatch, sibling_file="b.txt")
    seed = tmp_path / "seed"

    def git(cwd: Path, *args: str) -> str:
        result = _tree_git(cwd, *args)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    git(tree, "push", "origin", "HEAD:main")
    git(tree, "fetch", "origin", "release/r7")
    release_head = git(tree, "rev-parse", "FETCH_HEAD")
    assert _tree_git(tree, "merge", "--no-edit", release_head).returncode == 1
    (tree / "b.txt").write_text("resolved\n", encoding="utf-8")
    git(tree, "add", "b.txt")
    git(tree, "commit", "-m", "Resolve release merge")
    resolved = git(tree, "rev-parse", "HEAD")

    git(seed, "fetch", "origin", "main")
    git(seed, "checkout", "--detach", "FETCH_HEAD")
    (seed / ("b.txt" if main_conflicts else "a.txt")).write_text(
        "new main\n", encoding="utf-8"
    )
    git(seed, "commit", "-am", "Advance main")
    git(seed, "push", "origin", "HEAD:main")
    main = git(seed, "rev-parse", "HEAD")

    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    monkeypatch.setattr(land_slice._factory_git, "factory_git_token", lambda: None)
    monkeypatch.setattr(land_slice, "_finish_if_landed", lambda *args: False)
    monkeypatch.setattr(land_slice, "_drop_land_folder", lambda *a, **kw: None)
    monkeypatch.setattr(land_slice, "_drift_preflight", lambda *a, **kw: {"ran": False})
    monkeypatch.setattr(land_slice, "_sweep_stacks", lambda **kw: [])
    launched: list[list[str]] = []

    def launch(argv: list[str], **kw: Any) -> int:
        # Ship's squash uses this merge base. Main already being the base
        # means its sync has no release commits or resolution to replay.
        assert git(tree, "merge-base", "origin/main", "HEAD") == main
        assert git(tree, "rev-parse", "HEAD^1") == resolved
        assert git(tree, "rev-parse", "HEAD^2") == main
        assert (tree / "a.txt").read_text(encoding="utf-8") == "new main\n"
        assert (tree / "b.txt").read_text(encoding="utf-8") == "resolved\n"
        launched.append(argv)
        return 4242

    monkeypatch.setattr(land_slice, "_launch", launch)
    store = _release_store()
    store.add_todo(42, meta={"merge_back_resolver": "r7"})
    ctx = _Ctx(store, {"todo_id": 42, "branch": "slice/42"})
    handle = land_slice._launch_ship(ctx, tree, "--quick", SUBJECT)
    if main_conflicts:
        assert handle is None and not launched
        assert ctx.failure is not None and "b.txt" in ctx.failure
        assert "Merge origin/main into the slice" in store.comments(42)[-1]
        # A conflict is the work's to redo: an ordinary red, an attempt
        # burned — the infra skips below are the attempt-free ones.
        assert "attempt_free" not in ctx.meta_set
        assert git(tree, "rev-parse", "HEAD") == resolved
        assert git(tree, "status", "--porcelain") == ""
    else:
        assert handle is not None and launched
        assert ctx.failure is None


@pytest.mark.parametrize("skip", ["fetch", "dirty", "merge-fail", "timeout"])
def test_resolver_sync_infra_skip_is_attempt_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip: str
) -> None:
    """gr754677 review: a resolver sync that cannot merge main for a
    non-conflict, infra cause fails the land attempt-free — the tick
    re-mints the land, no attempt is burned and no builder is briefed.
    Only a real conflict charges an attempt."""
    tree = _real_release_tree(tmp_path, monkeypatch, sibling_file="a.txt")
    seed = tmp_path / "seed"

    def git(cwd: Path, *args: str) -> str:
        result = _tree_git(cwd, *args)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    # main moves ahead of the slice, so the sync has something to merge.
    git(seed, "fetch", "origin", "main")
    git(seed, "checkout", "--detach", "FETCH_HEAD")
    (seed / "a.txt").write_text("new main\n", encoding="utf-8")
    git(seed, "commit", "-am", "Advance main")
    git(seed, "push", "origin", "HEAD:main")

    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    monkeypatch.setattr(land_slice._factory_git, "factory_git_token", lambda: None)
    monkeypatch.setattr(land_slice, "_finish_if_landed", lambda *args: False)
    monkeypatch.setattr(land_slice, "_drop_land_folder", lambda *a, **kw: None)
    monkeypatch.setattr(land_slice, "_drift_preflight", lambda *a, **kw: {"ran": False})
    monkeypatch.setattr(land_slice, "_sweep_stacks", lambda **kw: [])
    launched: list[list[str]] = []

    def launch(argv: list[str], **kw: Any) -> int:
        launched.append(argv)
        return 4242

    monkeypatch.setattr(land_slice, "_launch", launch)

    base_git = land_slice._git
    if skip == "fetch":
        git(tree, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    elif skip == "dirty":
        (tree / "b.txt").write_text("dirty\n", encoding="utf-8")
    else:

        def flaky(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            if "merge" in args and "--abort" not in args:
                if skip == "timeout":
                    raise subprocess.TimeoutExpired(args, 300)
                return _proc("merge exploded", rc=1)
            return base_git(args, cwd)

        monkeypatch.setattr(land_slice, "_git", flaky)

    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub",),
        meta={"merge_back_resolver": "r7", "op": "run_slice"},
    )
    ctx = _Ctx(store, {"todo_id": 42, "branch": "slice/42", "lane": "claude-sub"})
    handle = land_slice._launch_ship(ctx, tree, "--quick", SUBJECT)
    assert handle is None and launched == []
    assert ctx.failure is not None
    assert "could not sync merge-back resolver onto main" in ctx.failure
    assert ctx.meta_set["release_sync"].startswith("skipped:")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert ctx.meta_set["red_class"] == "resolver-sync"
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags
    assert not any(tag.startswith("attempt:") for tag in tags)


def test_real_git_a_conflicting_release_merge_is_aborted_and_fails_the_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _real_release_tree(tmp_path, monkeypatch, sibling_file="b.txt")
    head = _tree_git(tree, "rev-parse", "HEAD").stdout.strip()
    store = _release_store()
    ctx = _Ctx(store, {"todo_id": 42})
    synced = land_slice._sync_release(ctx, tree, "release/r7", 42, "claude-sub")
    assert synced == land_slice.SYNC_FAILED
    assert _tree_git(tree, "rev-parse", "HEAD").stdout.strip() == head
    assert _tree_git(tree, "status", "--porcelain").stdout == ""
    assert _tree_git(tree, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode != 0
    assert ctx.failure is not None
    assert land_slice.RELEASE_SYNC_CONFLICT_MARK in ctx.failure
    assert "b.txt" in ctx.failure
    assert "origin/release/r7" in store.comments(42)[-1]


def test_real_git_the_release_merge_needs_no_host_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lander's git env blinds global and system config, so a host whose
    identity lives only there used to fail the merge and launch unsynced.
    No GIT_AUTHOR_*/GIT_COMMITTER_* in the env, no config, and a tree that
    will not invent an identity: the merge still lands."""
    tree = _real_release_tree(
        tmp_path, monkeypatch, sibling_file="a.txt", host_identity=False
    )
    probe = subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "x"],
        cwd=str(tree),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert probe.returncode != 0, "the fixture must refuse a bare commit"
    ctx = _Ctx(_release_store(), {"todo_id": 42})
    synced = land_slice._sync_release(ctx, tree, "release/r7", 42, "claude-sub")
    assert synced == land_slice.SYNC_MERGED, ctx.meta_set
    assert (tree / "a.txt").read_text(encoding="utf-8") == "sibling\n"
    assert _tree_git(tree, "status", "--porcelain").stdout == ""


def test_real_git_a_fix_a_sibling_already_landed_finishes_without_ship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slice was cut from an older release head and a sibling landed the
    identical change: before the merge the trees differ (the first
    already-landed read says no), after it HEAD equals the release head and
    ship's onto-preflight would die "nothing to release". The land
    finishes green as already-landed, no ship launched, no attempt charged."""
    tree = _real_release_tree(
        tmp_path,
        monkeypatch,
        sibling_file="b.txt",
        sibling_text="fix\n",
        sibling_also={"a.txt": "another sibling\n"},
    )
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    monkeypatch.setattr(land_slice, "_sweep_stacks", lambda *, keep=None: [])
    monkeypatch.setattr(land_slice, "_evict_stack", lambda project, *, volumes: True)
    launched: list[list[str]] = []

    def _fake_launch(argv: list[str], **kw: Any) -> int:
        launched.append(argv)
        return 4242

    monkeypatch.setattr(land_slice, "_launch", _fake_launch)
    store = _release_store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub",),
        meta={"land_target": "release/r7", "op": "run_slice"},
    )
    params = {
        "todo_id": 42,
        "worktree": str(tree),
        "branch": "slice/42",
        "lane": "claude-sub",
    }
    # the first read, as ``_launch_ship`` makes it before the sync, says no
    assert land_slice._already_landed(tree, SUBJECT, "release/r7") == (False, None)
    ctx = _Ctx(store, params)
    handle = land_slice._launch_ship(ctx, tree, "--quick", SUBJECT)
    assert handle is None and launched == []
    assert ctx.failure is None, ctx.failure
    assert ctx.status == "succeeded"
    assert ctx.meta_set["release_sync"].startswith("synced onto")
    assert ctx.meta_set["ship_status"] == "already-landed"
    assert "attempt:1" not in store.tag_strings(42)


def test_a_merge_that_times_out_is_aborted_before_ship_launches(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge git never answered may have left a half-merged tree: it is
    aborted before the launch, and ship still launches as today."""
    _release_sync_seams(seams, monkeypatch)
    answer = land_slice._git

    def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if "merge" in args and "--abort" not in args:
            seams["git"].append((["merge", "<timeout>"], Path(cwd)))
            raise subprocess.TimeoutExpired(args, 300)
        return answer(args, cwd)

    monkeypatch.setattr(land_slice, "_git", git)
    ctx, handle = _submit(_release_store(), seams)
    assert ctx.failure is None, ctx.failure
    calls = _git_calls(seams)
    assert calls.index(["merge", "<timeout>"]) < calls.index(["merge", "--abort"])
    assert calls.index(["merge", "--abort"]) < calls.index(SHIP_LAUNCH)
    assert ctx.meta_set["release_sync"].startswith("skipped: git did not answer")
    assert handle is not None


def test_a_release_fix_is_refused_once_the_release_deploy_started(
    seams: dict[str, Any],
) -> None:
    """T1 review: ship's round lock is this host's checkout, not the deploy
    owner's — the land reads the plan's ``meta.release`` instead and,
    once the deploy started, refuses before ship launches: nothing
    pushed, no attempt burned, the re-land capped at the ask."""
    store = _Store()
    store.add_todo(
        1,
        meta={
            "release": {
                "rN": "r7",
                "sha": FULL_SHA,
                "range": "a..b",
                "deploy_started_at": "2026-11-12T10:00:00Z",
            }
        },
    )
    store.add_todo(
        42,
        tags=("plan:1", "claimed-by:lane-claude-sub"),
        meta={"land_target": "release/r7", "op": "run_slice"},
    )
    ctx, handle = _submit(store, seams)
    assert handle is None
    assert seams["launch"] == []
    assert ctx.failure is not None and "takes no more fixes" in ctx.failure
    assert "deploy started at 2026-11-12T10:00:00Z" in ctx.failure
    assert ctx.meta_set["land_again_cap"] is True
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert "attempt:1" not in store.tag_strings(42)
    assert store.comments(42)[-1].startswith("handoff: lander: land REFUSED")


def test_a_normal_slice_still_lands_on_main(seams: dict[str, Any]) -> None:
    """No land target — a brief that only CITES a release branch in prose
    is not one — lands exactly as before: ``--quick`` onto main, the
    subject read against origin/main, main's drift preflight run."""
    store = _Store()
    store.add_todo(42, meta={"op": "run_slice"})
    store.chunks.rows[42].append(
        _Chunk(0, "release/r7 went red on this test; fix it on main.", "todo_body")
    )
    assert land_slice.land_target(store, _params(seams), 42) is None
    ctx, handle = _submit(store, seams)
    tree = seams["tree"]
    assert ctx.failure is None, ctx.failure
    [(argv, _kw)] = seams["launch"]
    assert argv == [str(tree / "scripts" / "ship"), "--quick", SUBJECT]
    assert "land_target" not in ctx.meta_set
    assert seams["probed"] == [tree]
    calls = [args for args, _cwd in seams["git"]]
    assert ["fetch", "--quiet", "origin", "main"] in calls
    assert [
        "log",
        "--reverse",
        "--no-merges",
        "--format=%s",
        "origin/main..HEAD",
    ] in calls
    assert not any("release/" in a for args in calls for a in args)
    pctx = _Ctx(store, _params(seams))
    _finish(handle, 0, GREEN_LOG)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.meta_set["landed_sha"] == FULL_SHA


def test_a_release_target_needs_the_release_branch_shape() -> None:
    """A malformed target never redirects a land: only ``release/r<N>``."""
    from precis.plans import release as R

    assert R.land_target_of({"land_target": "release/r7"}) == "release/r7"
    assert R.land_target_of({"land_target": "main"}) is None
    assert R.land_target_of({"land_target": "release/r7; rm"}) is None
    assert R.land_target_of({}, "x\nland target: release/r12\n") == "release/r12"
    assert R.land_target_of({}, "land target: main") is None
    assert R.land_target_of({}, "the land target: release/r7 is named") is None


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
    assert store.comments(42)[-1].startswith("handoff: lander: land FAILED")


def test_submit_nothing_to_land_closes_done(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr648125: a land minted over a branch that carries NOTHING — no
    commit ahead of origin/main and no net diff (the fix is already on
    main by another route) — finishes done, not red: submit launches no
    ship and the poll writes the same nothing-new record (the dependent
    re-cut unblocks instead of burning a ladder). A dirty/diff branch
    keeps the failure above. The gr681208 pushed-tip oracle confirms
    nothing is on origin either — ``ls_remote`` names no branch."""
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "ls_remote", lambda u, refs, *, env, **_kw: {}
    )
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    seams["subjects"] = []
    seams["changed_paths"] = []
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None and ctx.status is None
    assert handle == {"no_land": True, "worktree": str(seams["tree"])}
    assert seams["launch"] == []
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert pctx.meta_set["landed_sha"] is None
    assert store.comments(42)[-1] == land_slice.NO_DIFF_NOTE
    tags = store.tag_strings(42)
    assert "STATUS:done" in tags and "claimed-by:lane-claude-sub" not in tags
    # the useless clone and the todo's attempt refs are still cleaned up
    assert _ws_names(seams) == ["drop", "delete_builds"]


def _oracle(monkeypatch: pytest.MonkeyPatch, remote: dict[str, str] | None) -> None:
    """Stand the ``ls_remote`` oracle up (the ``_pushed_tip_ahead``
    read): a resolvable origin url, then ``remote`` as GitHub's answer —
    ``None`` when it cannot be read."""
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "ls_remote",
        lambda u, refs, *, env, **_kw: remote,
    )


def test_submit_no_land_refuses_when_origin_branch_is_ahead(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """review td682306 (gr681208): a held land can name a recut or reset
    worktree — the local ``slice/42`` reads empty while
    ``origin/slice/42`` still carries the commits review judged. The
    ``no_land`` record would write a false "landed: nothing" and let
    ``_cleanup``'s ``push --delete`` remove the branch holding the
    unlanded work — refuse like the missing-worktree arm instead."""
    tip = "c" * 40
    _oracle(monkeypatch, {"slice/42": tip})
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    seams["subjects"] = []
    seams["changed_paths"] = []
    seams["tips"] = {"refs/remotes/origin/slice/42": tip}
    seams["ancestor_rc"] = 1  # the pushed tip is ahead of the land base
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None and "reads empty" in ctx.failure
    assert f"origin/slice/42 is at {tip[:12]}" in ctx.failure
    assert seams["launch"] == []
    tags = store.tag_strings(42)
    assert {"STATUS:open", "attempt:1"} <= tags
    assert not any(c.startswith("landed:") for c in store.comments(42))
    assert store.comments(42)[-1].startswith("handoff: lander: land FAILED")
    # The branch was fetched for the ancestry read — never deleted.
    git_cmds = [a for a, _cwd in seams["git"]]
    assert not any(a[:2] == ["push", "origin"] for a in git_cmds)


def test_submit_no_land_refuses_when_origin_tip_is_unverifiable(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oracle cannot answer (no trusted url, or ``ls_remote`` down):
    the pushed state is unknown, so the land refuses the record rather
    than risk abandoning pushed work — recoverable on a re-run."""
    _oracle(monkeypatch, None)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    seams["subjects"] = []
    seams["changed_paths"] = []
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None and "could not be verified" in ctx.failure
    assert seams["launch"] == []
    assert "STATUS:open" in store.tag_strings(42)
    assert not any(c.startswith("landed:") for c in store.comments(42))


def test_submit_no_land_refuses_when_ancestry_cannot_be_read(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge-base failure (rc >1 — no common base object, git broke)
    is not a "nothing pushed ahead" answer: the land refuses rather
    than record no-land on an unread ancestry."""
    tip = "e" * 40
    _oracle(monkeypatch, {"slice/42": tip})
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    seams["subjects"] = []
    seams["changed_paths"] = []
    seams["tips"] = {"refs/remotes/origin/slice/42": tip}
    seams["ancestor_rc"] = 128
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert ctx.failure is not None and "could not be verified" in ctx.failure
    assert "STATUS:open" in store.tag_strings(42)


def test_submit_no_land_passes_when_origin_tip_is_not_ahead(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pushed branch whose tip the base already contains (landed by
    another route, or pushed empty): verified nothing ahead — the
    ``no_land`` close proceeds as before."""
    tip = "d" * 40
    _oracle(monkeypatch, {"slice/42": tip})
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    seams["subjects"] = []
    seams["changed_paths"] = []
    seams["tips"] = {"refs/remotes/origin/slice/42": tip}
    # ancestor_rc default 0: the tip is an ancestor of the land base.
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None and ctx.status is None
    assert handle == {"no_land": True, "worktree": str(seams["tree"])}


def test_submit_nothing_to_land_clears_a_crashed_predecessors_mirror(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review td660513 (regression, third pass): a crashed predecessor's
    launch files — the mirror and its tips record beside it, stale
    mirror refs present — must not survive submit's ``no_land`` path.
    The poll's sync still reads the record, judges a mirror in play,
    and the nothing-new job defers on a dead CAS basis instead of
    completing."""
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "ls_remote", lambda u, refs, *, env, **_kw: {}
    )
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub",),
        meta={"factory_mirror": True},
    )
    seams["subjects"] = []
    seams["changed_paths"] = []
    mirror_root = tmp_path / "scratch" / "factory-mirror"
    stale_mirror = mirror_root / "td42.git"
    (stale_mirror / "refs" / "heads").mkdir(parents=True)
    record = mirror_root / "td42.tips.json"
    record.write_text(json.dumps({"main": "d" * 40}) + "\n", encoding="utf-8")
    ctx, handle = _submit(store, seams)
    assert handle == {"no_land": True, "worktree": str(seams["tree"])}
    assert seams["launch"] == []
    assert not store.refs[42].meta.get("factory_mirror")
    assert not stale_mirror.exists()
    assert not record.exists()
    # the poll's no_land path completes — no stale-record sync defers it
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert store.comments(42)[-1] == land_slice.NO_DIFF_NOTE


def test_submit_ignores_the_builders_worktree_param(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """gr741695: the builder's folder is gone by the time the lander runs;
    ``params.worktree`` is ignored — the land goes on in its own folder."""
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, {**_params(seams), "worktree": str(tmp_path / "gone")})
    handle = land_slice.submit(ctx, None)
    assert ctx.failure is None and handle is not None
    assert handle["worktree"] == str(seams["tree"])
    assert _ws_names(seams) == []  # the folder was there: nothing cut


# ── the land's own clone (gr741695) ────────────────────────────────

REF = "refs/factory/build/42/7"
HEAD = "1" * 40
SNAPSHOT = "2" * 40


def _no_folder(seams: dict[str, Any]) -> None:
    """The first submit of a land job: its folder is not there yet."""
    import shutil

    shutil.rmtree(seams["tree"])


def _cut_params(seams: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {**_params(seams), "ref": REF, "head": HEAD, **extra}


def _cut_seams(seams: dict[str, Any], *, tip: str = HEAD) -> None:
    """The hub holds ``REF`` at ``tip`` and knows ``HEAD``; the folder is
    not there yet."""
    _no_folder(seams)
    seams["hub_tips"][REF] = tip
    seams["hub_commits"] = {HEAD: HEAD, tip: tip}


def _cut_submit(store: _Store, seams: dict[str, Any], **extra: Any) -> tuple[_Ctx, Any]:
    ctx = _Ctx(store, _cut_params(seams, **extra))
    return ctx, land_slice.submit(ctx, None)


def test_submit_cuts_its_own_clone_at_the_commit_to_land(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """gr741695: the builder's folder is gone — the land cuts a clone of the
    host's hub at ``params.head``, held by ``params.ref``, in the folder its
    own job id names, then points the clone's origin at GitHub (what ship
    and the mirror's url rewrite expect) and runs ship there."""
    _cut_seams(seams)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert ctx.failure is None and handle is not None
    assert _ws_names(seams) == ["ensure_hub", "sync_hub", "hub_tip", "cut"]
    [sync] = _ws_args(seams, "sync_hub")
    assert sync["url"] == GITHUB_URL and sync["base"] == "main"
    [cut] = _ws_args(seams, "cut")
    assert cut["tree"] == seams["tree"] and handle["worktree"] == str(seams["tree"])
    assert cut["base"] == "main"
    assert str(cut["tree"]).endswith("/slice-td42-land-j901")
    assert cut["branch"] == "slice/42" and cut["seed_from"] == tmp_path
    assert cut["start"].sha == HEAD and cut["start"].ref == REF
    assert cut["hub"] == tmp_path / ".git" / "precis-hub.git"
    # origin is GitHub's url in the clone, set after the cut and before ship
    gits = [(a, c) for a, c in seams["git"]]
    assert (["remote", "set-url", "origin", GITHUB_URL], seams["tree"]) in gits
    [(argv, kw)] = seams["launch"]
    assert kw["cwd"] == seams["tree"] and argv[1] == "--quick"
    assert any("cut slice-td42-land-j901 at 111111111111" in t for _k, t in ctx.chunks)
    # the lander launched in the clone, so it holds the todo's lock there
    lock_dir, lock_name = _land_lock(seams)
    assert f"tree={seams['tree']}" in (lock_dir / lock_name).read_text(encoding="utf-8")


def test_ship_env_carries_the_host_checkouts_state_roots(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """A clone's git dir is its own, so ship (and the verify probes that run
    ``scripts/test``) get ``PRECIS_GATE_SLOT_ROOT`` and
    ``PRECIS_ROUND_STATE_ROOT`` set to the host checkout's git common dir:
    gate slots stay shared and ``ship --onto`` reads the host's round
    journal. A PR land's linked worktree already shares that dir and gets
    neither."""
    host_common = str((tmp_path / ".git").resolve())
    assert Path(host_common).is_absolute()
    store = _Store()
    store.add_todo(42)
    _ctx, handle = _submit(store, seams)
    env = seams["launch"][0][1]["env"]
    assert env["PRECIS_GATE_SLOT_ROOT"] == host_common
    assert env["PRECIS_ROUND_STATE_ROOT"] == host_common
    # a verify probe in the clone queues on the same slots
    verify = land_slice._launch_verify(
        seams["tree"], ["t.py::x"], phase="alone", run_tree=seams["tree"]
    )
    assert verify is not None
    assert seams["launch"][-1][1]["env"]["PRECIS_GATE_SLOT_ROOT"] == host_common
    # not a land clone: nothing set
    assert (
        land_slice._host_state_env(tmp_path / ".claude" / "worktrees" / "slice-pr-7")
        == {}
    )
    assert handle is not None


def test_a_release_land_cuts_from_the_release_branch(
    seams: dict[str, Any],
) -> None:
    """The hub's copy of the land target is the release branch; the clone's
    base — what the branch subjects and the diff read — is it."""
    _cut_seams(seams)
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub",),
        meta={"land_target": "release/r7", "op": "run_slice"},
    )

    ctx, handle = _cut_submit(store, seams)

    assert ctx.failure is None and handle is not None
    [sync] = _ws_args(seams, "sync_hub")
    [cut] = _ws_args(seams, "cut")
    assert sync["base"] == cut["base"] == "release/r7"


def test_the_land_takes_head_when_the_ref_holds_a_snapshot_above_it(
    seams: dict[str, Any],
) -> None:
    """The worker may have saved uncommitted leftovers as a ``wip:`` commit
    on top of the builder's last real commit: the ref's tip is that
    snapshot, ``head`` is its parent, and the land takes ``head``. The cut
    starts at the ref's tip (the workspace layer refuses a start commit its
    ref does not hold); the clone's branch is then put on ``head`` before
    ship runs."""
    _cut_seams(seams, tip=SNAPSHOT)  # ancestor_rc 0: HEAD is below the tip
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert ctx.failure is None and handle is not None
    [cut] = _ws_args(seams, "cut")
    assert cut["start"].sha == SNAPSHOT and cut["start"].ref == REF
    hub = Path(seams["tree"]).parents[2] / ".git" / "precis-hub.git"
    assert (["merge-base", "--is-ancestor", HEAD, SNAPSHOT], hub) in seams["git"]
    assert seams["checkout"] == ["checkout", "-q", "-B", "slice/42", HEAD]
    gits = [a for a, _c in seams["git"]]
    assert gits.index(seams["checkout"]) < gits.index(
        ["remote", "set-url", "origin", GITHUB_URL]
    )
    assert any(f"cut slice-td42-land-j901 at {HEAD[:12]}" in t for _k, t in ctx.chunks)


def test_a_cut_at_head_needs_no_checkout(seams: dict[str, Any]) -> None:
    _cut_seams(seams)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _cut_submit(store, seams)
    assert ctx.failure is None and handle is not None
    assert "checkout" not in seams


def test_a_ref_that_no_longer_holds_head_fails_attempt_free(
    seams: dict[str, Any],
) -> None:
    """The ref was replaced (a rebuild force-pushed it): ``head`` is no
    longer its tip or an ancestor. Nothing is cut and nothing lands; the
    job fails attempt-free under ``infra-land`` with the reason."""
    _cut_seams(seams, tip=SNAPSHOT)
    seams["ancestor_rc"] = 1
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert handle is None and ctx.status == "failed"
    assert f"{REF} no longer holds {HEAD[:12]}" in (ctx.failure or "")
    assert f"it is at {SNAPSHOT[:12]}" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert ctx.meta_set["red_class"] == "infra-land"
    assert "attempt:1" not in store.tag_strings(42)
    assert "cut" not in _ws_names(seams) and seams["launch"] == []
    assert any("land DEFERRED" in c for c in store.comments(42))
    assert not any(c.startswith("landed:") for c in store.comments(42))


def test_a_head_the_hub_does_not_hold_fails_attempt_free(
    seams: dict[str, Any],
) -> None:
    _cut_seams(seams)
    seams["hub_commits"] = {}  # the hub has no such commit
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert handle is None and "does not have it" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert "cut" not in _ws_names(seams)


def test_a_ref_missing_from_this_hosts_hub_fails_attempt_free(
    seams: dict[str, Any],
) -> None:
    """A land runs on the host whose hub holds the work; another host's
    hub has no such ref."""
    _cut_seams(seams)
    seams["hub_tips"].clear()
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert handle is None and f"holds no {REF}" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert "cut" not in _ws_names(seams) and seams["launch"] == []


def test_an_unreadable_ancestry_fails_attempt_free(seams: dict[str, Any]) -> None:
    _cut_seams(seams, tip=SNAPSHOT)
    seams["ancestor_rc"] = 128
    store = _Store()
    store.add_todo(42)

    ctx, handle = _cut_submit(store, seams)

    assert handle is None and "could not say whether" in (ctx.failure or "")
    assert "cut" not in _ws_names(seams)


def test_a_legacy_land_imports_the_work_github_holds(seams: dict[str, Any]) -> None:
    """A land minted before the switch has no ``ref``: the work GitHub
    holds under ``refs/factory/slice/<id>`` is imported into the hub and
    its ref used; ``head`` comes from the land's params."""
    _no_folder(seams)
    seams["legacy_tip"] = HEAD
    seams["hub_commits"] = {HEAD: HEAD}
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx = _Ctx(store, {**_params(seams), "head": HEAD})

    handle = land_slice.submit(ctx, None)

    assert ctx.failure is None and handle is not None
    [legacy] = _ws_args(seams, "import_legacy")
    assert legacy == {"url": GITHUB_URL, "todo": 42}
    names = _ws_names(seams)
    assert names[:2] == ["ensure_hub", "sync_hub"]
    assert names.index("import_legacy") < names.index("cut")
    [cut] = _ws_args(seams, "cut")
    assert cut["start"].ref == "refs/factory/build/42/0"
    assert cut["start"].sha == HEAD


def test_a_legacy_land_takes_head_from_the_pending_land_then_the_tip(
    seams: dict[str, Any],
) -> None:
    """``head``: ``params.head``, else ``meta.pending_land.head``, else the
    imported ref's tip."""
    _no_folder(seams)
    seams["legacy_tip"] = SNAPSHOT
    seams["hub_commits"] = {HEAD: HEAD, SNAPSHOT: SNAPSHOT}
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub",),
        meta={"pending_land": {"head": HEAD}},
    )
    ctx = _Ctx(store, _params(seams))
    assert land_slice.submit(ctx, None) is not None
    [cut] = _ws_args(seams, "cut")
    assert cut["start"].sha == SNAPSHOT  # the cut starts at the ref's tip
    assert seams["checkout"][-1] == HEAD  # the land takes the pending head

    import shutil

    shutil.rmtree(seams["tree"])
    seams["ws"].clear()
    store.refs[42].meta.pop("pending_land")
    ctx = _Ctx(store, _params(seams))
    assert land_slice.submit(ctx, None) is not None
    [cut] = _ws_args(seams, "cut")
    assert cut["start"].sha == SNAPSHOT


def test_a_legacy_land_whose_work_is_nowhere_fails_attempt_free(
    seams: dict[str, Any],
) -> None:
    _no_folder(seams)
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, {**_params(seams), "head": HEAD})

    assert land_slice.submit(ctx, None) is None

    assert "names no hub ref" in (ctx.failure or "")
    assert "refs/factory/slice/42" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"


@pytest.mark.parametrize(
    "what",
    ["hub", "cut", "url"],
)
def test_a_workspace_error_fails_the_land_attempt_free(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, what: str
) -> None:
    """Any refusal of the workspace layer (or an unnameable origin) ends the
    job attempt-free under ``infra-land`` with its own message; no ship,
    no landed record, no attempt charged."""
    _cut_seams(seams)
    if what == "hub":
        seams["hub_error"] = "the hub cannot be made; next: check the disk"
    elif what == "cut":
        seams["cut_error"] = "git clone failed; next: retry"
    else:
        monkeypatch.setattr(land_slice, "_github_url", lambda root: None)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert handle is None and ctx.status == "failed"
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert "attempt:1" not in store.tag_strings(42)
    assert seams["launch"] == []
    assert not any(c.startswith("landed:") for c in store.comments(42))
    if what != "url":
        assert (seams["hub_error"] or seams["cut_error"]) in (ctx.failure or "")
    # a failed cut leaves no folder (the real cut renames a finished one)
    assert not seams["tree"].exists()


def test_a_clone_without_the_base_tracking_ref_fails_the_land(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything the lander reads before ship's own fetch must be in the
    clone: a hub that holds no ``main`` yields a clone without
    ``origin/main``, and the land says so instead of reading blind."""
    _cut_seams(seams)
    inner = land_slice._git

    def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["rev-parse", "--verify", "--quiet"] and args[3].startswith(
            "refs/remotes/origin/"
        ):
            return _proc(rc=1)
        return inner(args, cwd)

    monkeypatch.setattr(land_slice, "_git", git)
    store = _Store()
    store.add_todo(42)
    ctx, handle = _cut_submit(store, seams)
    assert handle is None and "origin/main is missing" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"


def test_a_resumed_land_finds_its_own_folder_and_cuts_nothing(
    seams: dict[str, Any],
) -> None:
    """A job re-submitted after an outage names the same folder: it is
    there, a ship was launched in it (the fixture's folder holds the ship's
    log), it is the job's own — nothing is cut (and no hub is touched)."""
    seams["hub_tips"][REF] = HEAD
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert ctx.failure is None and handle["worktree"] == str(seams["tree"])
    assert _ws_names(seams) == []


def test_a_folder_with_no_launch_on_record_is_dropped_and_cut_again(
    seams: dict[str, Any],
) -> None:
    """The worker died between the cut's rename and its last step: the
    resubmitted job finds a folder nothing was ever launched in and no ship
    is behind. A land folder holds nothing of its own — it is dropped and
    cut again."""
    _cut_seams(seams)
    (seams["tree"]).mkdir(parents=True)  # the unfinished folder
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert ctx.failure is None and handle is not None
    names = _ws_names(seams)
    assert names.index("drop") < names.index("cut")
    assert _ws_args(seams, "drop") == [seams["tree"]]
    assert any("left unfinished" in t for _k, t in ctx.chunks)


@pytest.mark.parametrize("evidence", ["compute_handle", "exit_file"])
def test_a_folder_with_a_launch_on_record_is_kept(
    seams: dict[str, Any], evidence: str
) -> None:
    (seams["tree"] / "scratch" / "land_slice.log").unlink()
    seams["hub_tips"][REF] = HEAD
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx = _Ctx(store, _cut_params(seams))
    if evidence == "compute_handle":
        ctx.meta["compute_handle"] = {"pid": 5150}
    else:
        (seams["tree"] / "scratch" / "land_slice.exit").write_text(
            "0\n", encoding="utf-8"
        )

    assert land_slice.submit(ctx, None) is not None
    assert _ws_names(seams) == []


def test_an_unfinished_folder_that_cannot_be_dropped_fails_attempt_free(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    (seams["tree"] / "scratch" / "land_slice.log").unlink()
    monkeypatch.setattr(land_slice._slice_ws, "drop", lambda tree, *, notes=None: False)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert handle is None and "could not be removed" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert "cut" not in _ws_names(seams) and seams["launch"] == []


def test_a_ship_of_this_todo_in_another_jobs_folder_defers_without_a_cut(
    seams: dict[str, Any],
) -> None:
    """The lock is the todo's, in the host checkout: a ship an ended job
    left running in ITS folder still owns the todo's mirror. A later job
    never adopts it (its folder holds none of that ship's work) and never
    cuts: it defers attempt-free."""
    _cut_seams(seams)
    other = seams["tree"].parent / "slice-td42-land-j800"
    held, fd, _detail = land_slice._try_land_lock(*_land_lock(seams))
    assert not held and fd is not None
    land_slice._stamp_lock_holder(fd, 5150, 42, "slice/42", SUBJECT, tree=str(other))
    try:
        store = _Store()
        store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
        ctx, handle = _cut_submit(store, seams)
    finally:
        os.close(fd)
    assert handle is None and ctx.status == "failed"
    assert "another land's folder" in (ctx.failure or "")
    assert ctx.meta_set["red_class"] == "ship-busy"
    assert _ws_names(seams) == [] and seams["launch"] == []


def test_the_land_lock_of_a_pr_land_stays_in_its_worktrees_gitdir(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    pr_tree = tmp_path / ".claude" / "worktrees" / "slice-pr-73"
    gitdir = Path(seams["gitdir"])
    assert land_slice._land_lock_where(pr_tree, 42, gitdir) == (
        gitdir,
        land_slice.LAND_LOCK_FILE,
    )
    assert land_slice._land_lock_where(seams["tree"], 42, gitdir) == _land_lock(seams)
    assert land_slice._land_lock_where(pr_tree, 42, None) is None


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


def test_poll_green_after_land_notify(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub", "plan:40"),
        meta={
            "build_ref": {"state": "pushed", "ref": "refs/factory/build/42/7"},
            "conflict": {"head": "0" * 40, "files": ["src/precis/x.py"]},
        },
    )
    store.add_todo(
        44,
        status="open",
        tags=("plan:40",),
        meta={"gate": {"kind": "sha-in-prod", "args": {"sha": PENDING_SHA}}},
    )
    seams["gates"] = [(44, True)]
    # the slice branch is on origin: the cleanup's remote delete reads
    # the trusted-url/oracle/ops-repo seams, never `_git` in the shared
    # gitdir (review td660513)
    url = "https://github.com/example/precis.git"
    mirror_calls: list[tuple[list[str], Path, dict[str, str]]] = []
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "github_url", lambda root, *, env: url
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "ls_remote",
        lambda u, refs, *, env, **_kw: {"slice/42": FULL_SHA},
    )

    def fake_mirror_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        mirror_calls.append((list(args), Path(cwd), dict(env)))
        return _proc(rc=0)

    monkeypatch.setattr(land_slice._factory_git_mirror, "_run", fake_mirror_run)
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
    # gr741695: the land's clone goes through the workspace layer — the
    # folder, then the todo's attempt refs in the hub — never a
    # worktree remove / branch -D in a host checkout, and the task's
    # ``meta.build_ref`` is cleared
    assert _ws_args(seams, "drop") == [seams["tree"]]
    assert _ws_names(seams) == ["drop", "delete_builds"]
    assert _ws_args(seams, "delete_builds") == [42]
    assert _cleanup_cmds(seams) == []
    assert store.refs[42].meta["build_ref"] is None
    # and so is the conflict an earlier land stamped: it is resolved
    assert store.refs[42].meta["conflict"] is None
    # ... and the build floor is this land job's id (the clone's name)
    assert store.refs[42].meta["build_floor"] == 901
    # the land's per-todo lock file does not pile up in the host's git dir
    lock_dir, lock_name = _land_lock(seams)
    assert not (lock_dir / lock_name).exists()
    # the origin branch delete is an authenticated transport: it ran as
    # a `push --delete` to the trusted url from a fresh ops gitdir under
    # the mirror root — never `push origin` in the shared, branch-
    # writable gitdir where a planted pre-push or url.insteadOf would
    # meet the token (review td660513)
    deletes = [(a, c, e) for a, c, e in mirror_calls if "--delete" in a]
    [(dargs, dcwd, denv)] = deletes
    assert dargs[:3] == ["git", "-c", "core.hooksPath=/dev/null"]
    assert dargs[-5:] == [
        "push",
        url,
        "--delete",
        "refs/factory/slice/42",
        "refs/heads/slice/42",
    ]
    # ``root`` here is slice_root() == tmp_path (PRECIS_SLICE_ROOT)
    assert dcwd.parent == land_slice._factory_git_mirror.mirror_root(tmp_path)
    assert dcwd != seams["tree"] and seams["tree"] not in dcwd.parents
    assert denv["GIT_CONFIG_NOSYSTEM"] == "1"
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "landed abc123400" in summary
    assert "land_notify did it" in summary
    assert "gate td44" in summary


def _pin_todo(store: _Store, **record: Any) -> None:
    """A pin-bump node handed to the lander (td659522 step 4)."""
    meta = {
        "op": "pin_bump",
        "pin_bump": {
            "state": "handed",
            "commit": FULL_SHA,
            "land_job": 777,
            "package": "autocatpath",
            "version": "0.50.0",
            "repo": "catpath",
            **record,
        },
    }
    store.add_todo(42, meta=meta)


def test_a_pin_bump_land_finishes_the_pin_record(seams: dict[str, Any]) -> None:
    """td682512 review: the ``land_slice`` a ``pin_bump`` minted runs the
    pin's closing tail right before the land record —
    ``meta.pin_bump`` ``state: landed`` at the pushed sha (without it the
    record would sit ``handed`` forever)."""
    store = _Store()
    _pin_todo(store)
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None, ctx.failure
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded", pctx.failure
    record = store.refs[42].meta["pin_bump"]
    assert record["state"] == "landed" and record["landed_sha"] == FULL_SHA
    assert any(c.startswith("landed: abc123400") for c in store.comments(42))
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert f"pin: record landed at {FULL_SHA[:9]}" in summary


def test_a_no_diff_pin_land_finishes_at_the_records_commit(
    seams: dict[str, Any],
) -> None:
    """A reland that squashes nothing still lands the pin record: the
    commit is on main under the record's own sha."""
    store = _Store()
    _pin_todo(store)
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None, ctx.failure
    _finish(handle, 0, NO_DIFF_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded", pctx.failure
    record = store.refs[42].meta["pin_bump"]
    assert record["state"] == "landed" and record["landed_sha"] == FULL_SHA


def test_a_plain_slice_land_touches_no_pin_record(seams: dict[str, Any]) -> None:
    """A land whose todo carries no ``meta.pin_bump`` never calls into
    ``pin_bump``: no ``pin:`` note, no meta written."""
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None, ctx.failure
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded", pctx.failure
    assert "pin_bump" not in store.refs[42].meta
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "pin:" not in summary
    # and a plain land keeps ship's own land_notify (nothing held)
    [(_argv, kw)] = seams["launch"]
    assert "PRECIS_LAND_NOTIFY" not in kw["env"]
    assert "STATUS:done" in store.tag_strings(42)


def test_a_pin_land_records_landed_but_leaves_the_node_open(
    seams: dict[str, Any],
) -> None:
    """td659522 F7 (Reto 2026-11-15): the land writes ``landed:`` and the
    record, but a pin-bump node stays open until its gate image carries the
    pin. If this fails glue blocked by the node unblocks on the land alone
    and builds against a stale gate image."""
    store = _Store()
    _pin_todo(store)
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None, ctx.failure
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded", pctx.failure
    assert store.refs[42].meta["pin_bump"]["state"] == "landed"
    assert any(c.startswith("landed: abc123400") for c in store.comments(42))
    tags = store.tag_strings(42)
    assert "STATUS:done" not in tags and "STATUS:doing" in tags
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "STATUS held: gate image wait (F7)" in summary


def test_a_pin_land_holds_ships_land_notify_even_unmirrored(
    seams: dict[str, Any],
) -> None:
    """With no factory token (no mirror) ship's land_notify would still close
    the pin node from the subject's ``(td42)``: it runs with the notify marks
    held, and the lander writes the subject's other handles itself. If this
    fails an unmirrored pin land closes its node behind the image wait, or
    the other todos it names are never recorded."""
    store = _Store()
    _pin_todo(store)
    store.add_todo(43)
    seams["subjects"] = ["chore(deps): pin autocatpath v0.50.0 (td42, td43)"]
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None, ctx.failure
    [(_argv, kw)] = seams["launch"]
    assert kw["env"]["PRECIS_LAND_NOTIFY"] == "0"
    assert kw["env"]["PRECIS_GRIPE_NOTE"] == "0"
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded", pctx.failure
    assert "STATUS:done" not in store.tag_strings(42)
    assert "STATUS:done" in store.tag_strings(43)
    assert store.comments(43)[-1].startswith("landed: abc123400")


def test_the_pin_check_fails_closed_when_the_store_cannot_say() -> None:
    """The pin check guards the close: a store that cannot be read holds
    ship's land_notify (and defers the notify) rather than let it close a
    pin node behind its image wait."""

    class _Broken(_Store):
        def get_ref(self, *, kind: str, id: int) -> _Ref | None:
            raise RuntimeError("db down")

    assert land_slice._carries_pin(_Broken(), 42) is True
    store = _Store()
    store.add_todo(42)
    assert land_slice._carries_pin(store, 42) is False


def test_record_land_closes_a_pin_node_whose_image_is_ready() -> None:
    """Once the record says the gate image carries the pin, a land record
    (a hand reland, the deferred notify) closes the node as for a slice; if
    this fails a ready pin node can only ever close through its job."""
    store = _Store()
    _pin_todo(store, state="landed", image={"ready": True, "want": FULL_SHA})
    wrote = land_slice._record_land(store, 42, FULL_SHA, SUBJECT)
    assert "STATUS:done" in wrote and "STATUS:done" in store.tag_strings(42)
    store2 = _Store()
    _pin_todo(store2, state="landed", image={"reason": "stale", "want": FULL_SHA})
    wrote = land_slice._record_land(store2, 42, FULL_SHA, SUBJECT)
    assert "STATUS held: gate image wait (F7)" in wrote
    assert "STATUS:done" not in store2.tag_strings(42)


def _patch_sync_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stand in the mirror→origin sync: the GitHub url read, the sync
    itself, and the shared-dir repair — recording each's arguments."""
    seen: dict[str, Any] = {"sync": [], "repair": []}
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )

    def fake_sync(
        mirror: Any,
        cache: Any,
        url: str,
        tips: dict[str, str],
        *,
        expect: dict[str, str],
        env: dict[str, str],
    ) -> tuple[str, str]:
        seen["sync"].append((mirror, cache, url, dict(tips), dict(expect)))
        return ("ok", "main → origin")

    def fake_repair(*a: Any, **kw: Any) -> list[str]:
        seen["repair"].append((a, kw))
        return ["origin/main → ddddddddd"]

    monkeypatch.setattr(land_slice._factory_git_mirror, "sync", fake_sync)
    monkeypatch.setattr(land_slice, "_repair_origin_state", fake_repair)
    return seen


def test_poll_green_syncs_the_mirror_to_origin(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gr659929: ship pushed the launcher's private mirror, not GitHub —
    poll places the refs ship moved on the real origin, each leased on the
    launch tip, BEFORE any land record is written."""
    seen = _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    tips = {"main": "d" * 40, "prod": "e" * 40}
    handle["factory_mirror_tips"] = tips
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert pctx.meta_set["landed_sha"] == FULL_SHA
    [(mirror, _cache, url, got_tips, expect)] = seen["sync"]
    assert mirror == tmp_path / "scratch" / "factory-mirror" / "td42.git"
    assert url == "https://github.com/example/precis.git"
    assert got_tips == tips
    # the sync is told the sha ship reported — a mirror tip that is not
    # it (or a ref that never moved under a claimed land) is refused
    assert expect == {"main": FULL_SHA}
    assert "STATUS:done" in store.tag_strings(42)


def test_poll_green_replays_the_local_main_advance_only_after_the_sync(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review td660513 finding 4, second pass (regression): the launch
    shim refused ship's own ``_sync_local_main``, so a verified
    mirror→origin sync ends with the LANDER fast-forwarding the shared
    local main — to the sha the sync verified on origin, after the push,
    never before — under a credential-free env."""
    order: list[str] = []

    def fake_sync(
        mirror: Any,
        cache: Any,
        url: str,
        tips: dict[str, str],
        *,
        expect: dict[str, str],
        env: dict[str, str],
    ) -> tuple[str, str]:
        order.append("sync")
        return ("ok", "main → origin")

    advanced: list[tuple[Path, str, dict[str, str]]] = []

    def fake_advance(common: Path, sha: str, *, env: dict[str, str]) -> str:
        order.append("advance")
        advanced.append((common, sha, dict(env)))
        return f"local main → {sha[:9]}"

    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    monkeypatch.setattr(land_slice._factory_git_mirror, "sync", fake_sync)
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "advance_local_main", fake_advance
    )
    monkeypatch.setattr(land_slice, "_repair_origin_state", lambda *a, **k: [])
    common = tmp_path / "common-gitdir"
    seams["common"] = str(common)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    # a linked worktree shares the host checkout's main (a land minted
    # before gr741695, a PR land); a land's own clone shares none — the
    # test below
    _legacy_handle(handle, tmp_path)
    handle["factory_mirror_tips"] = {"main": "d" * 40}
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert order == ["sync", "advance"]
    [(got_common, sha, env)] = advanced
    assert got_common == common and sha == FULL_SHA
    for leaked in ("PRECIS_FACTORY_GIT_TOKEN", "GH_TOKEN"):
        assert leaked not in env
    assert any("local main →" in t for k, t in pctx.chunks if k == "job_event")


def test_poll_nothing_new_replays_the_advance_to_the_launch_tip(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A nothing-new land deferred ship's local-main advance too — the
    shim refuses it regardless — so the lander replays it to the LAUNCH
    tip (the verified real main), not to a phantom."""
    _patch_sync_seams(monkeypatch)
    advanced: list[tuple[Path, str]] = []

    def fake_advance(common: Path, sha: str, *, env: dict[str, str]) -> str:
        advanced.append((common, sha))
        return "ok"

    monkeypatch.setattr(
        land_slice._factory_git_mirror, "advance_local_main", fake_advance
    )
    common = tmp_path / "common-gitdir"
    seams["common"] = str(common)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    _legacy_handle(handle, tmp_path)
    tip = "f" * 40
    handle["factory_mirror_tips"] = {"main": tip}
    _finish(handle, 0, NO_DIFF_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert advanced == [(common, tip)]


def test_poll_green_skips_the_local_main_advance_in_a_land_clone(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gr741695: a land's clone has a git dir of its own and no main in
    common with the host checkout, and it is dropped right after the land:
    the advance has nothing to protect and is skipped, with a note."""
    _patch_sync_seams(monkeypatch)
    advanced: list[Any] = []

    def moved(*a: Any, **k: Any) -> str:
        advanced.append((a, k))
        return "moved"

    monkeypatch.setattr(land_slice._factory_git_mirror, "advance_local_main", moved)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    handle["factory_mirror_tips"] = {"main": "d" * 40}
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and advanced == []
    assert any(
        "local main advance: skipped" in t for k, t in pctx.chunks if k == "job_event"
    )


def test_poll_green_release_land_never_advances_main(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An --onto release land's local-main advance is a no-op: main was
    not the target and must not move even after the release ref's sync
    verifies."""
    _patch_sync_seams(monkeypatch)
    advanced: list = []

    def fake_advance(*a: Any, **k: Any) -> str:
        advanced.append((a, k))
        return "ok"

    monkeypatch.setattr(
        land_slice._factory_git_mirror, "advance_local_main", fake_advance
    )
    seams["common"] = str(tmp_path / "common-gitdir")
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub",),
        meta={"land_target": "release/r7", "op": "run_slice"},
    )
    ctx, handle = _submit(store, seams)
    handle["factory_mirror_tips"] = {"main": "d" * 40, "release/r7": "e" * 40}
    _finish(handle, 0, RELEASE_GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert advanced == []


def test_poll_green_mirror_sync_race_parks_the_land(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost launch-tip lease defers the land instead of recording a sha
    origin does not hold: the handle parks with ``not_before`` and a
    ``main_refusals`` count, the todo is untouched, ship re-runs
    against a fresh mirror when the backoff expires, and ship's
    already-written shared state (origin/* refs, the fast-forwarded
    local main) is repaired to GitHub's real tips first (review
    td660513 finding 4)."""
    seen = _patch_sync_seams(monkeypatch)
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "sync",
        lambda *a, **k: (
            "raced",
            "main moved on origin: expected ddddddddd, found ccccccccc",
        ),
    )
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    handle["factory_mirror_tips"] = {"main": "d" * 40}
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False  # parked, not done
    assert pctx.status is None and pctx.failure is None
    assert handle["main_refusals"] == 1
    assert "not_before" in handle
    # no land record: no landed comment, no done, no landed_sha meta
    assert not any(c.startswith("landed:") for c in store.comments(42))
    assert "STATUS:done" not in store.tag_strings(42)
    assert "landed_sha" not in pctx.meta_set
    # ship's shared-dir post-land state was rolled back before the park
    assert seen["repair"], "no origin repair ran"
    # the event tells the sweep why the green ship did not land
    assert any(
        "origin sync did not stick" in t for k, t in pctx.chunks if k == "job_event"
    )


def test_poll_green_mirror_sync_reads_adopted_tips_from_the_launch_record(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A handle an adopting land rebuilt (gr650484) carries no tips: poll
    falls back to the launch record BESIDE the mirror — under the main
    checkout's scratch, not the branch-writable tree (review td660513)."""
    seen = _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    # the adopted handle's shape: no factory_mirror_tips, but the launch
    # left the tips record beside the mirror
    tips = {"main": "f" * 40, "prod": "0" * 40}
    record = tmp_path / "scratch" / "factory-mirror" / "td42.tips.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(tips) + "\n", encoding="utf-8")
    assert "factory_mirror_tips" not in handle
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    [(_mirror, _cache, _url, got_tips, _expect)] = seen["sync"]
    assert got_tips == tips


def test_poll_green_adopted_ship_with_missing_tips_fails_closed(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review td660513 (regression): an adopted handle carries no tips —
    and when the launch record is gone while the mirror itself still
    stands, poll must NOT read "no mirror": ship ran mirrored, so its
    squash lives only in the lander's mirror and recording it as landed
    would name a sha origin never held. Fail closed: no sync, no land
    record."""
    seen = _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    # the adopted handle's shape — no factory_mirror_tips — and the
    # launch record deleted while the mirror dir survives
    (tmp_path / "scratch" / "factory-mirror" / "td42.git").mkdir(parents=True)
    assert "factory_mirror_tips" not in handle
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "mirror launch record" in (pctx.failure or "")
    assert seen["sync"] == []
    assert not any(c.startswith("landed:") for c in store.comments(42))
    assert "STATUS:done" not in store.tag_strings(42)
    assert "landed_sha" not in pctx.meta_set


@pytest.mark.parametrize("with_mirror_dir", [False, True])
def test_poll_green_adopted_ship_with_malformed_tips_record_fails_closed(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    with_mirror_dir: bool,
) -> None:
    """A launch record that cannot be parsed is set aside and reads as
    absent (gr762423), but the ``.broken-*`` sibling it leaves is evidence
    of a mirrored launch exactly like the mirror dir or the meta stamp —
    with no dir and no stamp too — so poll fails closed with no CAS basis,
    never a blind land."""
    seen = _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    record = tmp_path / "scratch" / "factory-mirror" / "td42.tips.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    if with_mirror_dir:
        (record.parent / "td42.git").mkdir()
    record.write_text("not json\n", encoding="utf-8")
    assert "factory_mirror_tips" not in handle
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert seen["sync"] == []
    assert "STATUS:done" not in store.tag_strings(42)


def test_drop_launch_retires_the_broken_tips_evidence(tmp_path: Path) -> None:
    """gr762423 review: a token-less launch or a no_land submit drops the
    launch's mirror state; the set-aside ``.broken-*`` record is part of
    it, or every later land of the todo would still read "mirrored" and
    fail with "mirror launch record is unreadable". Retired by rename,
    never deleted."""
    store = _Store()
    store.add_todo(42, tags=())
    mirror = land_slice._factory_git_mirror
    record = mirror.launch_tips_path(tmp_path, "td42")
    record.parent.mkdir(parents=True)
    record.write_text("not json\n", encoding="utf-8")
    assert mirror.read_launch_tips(tmp_path, "td42") is None
    assert land_slice._mirror_in_play(tmp_path, 42, {}, store) is True
    mirror.drop_launch(tmp_path, "td42")
    assert land_slice._mirror_in_play(tmp_path, 42, {}, store) is False
    (kept,) = record.parent.glob("td42.tips.json.dropped-*")
    assert kept.read_text(encoding="utf-8") == "not json\n"


def test_poll_green_unmirrored_ship_ignores_a_planted_tips_file(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The legacy tree-scratch copy is a tips SOURCE, never evidence: a
    planted ``factory-mirror-tips.json`` in the branch-writable tree
    does not turn an unmirrored launch into a mirror sync."""
    seen = _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    (seams["tree"] / "scratch" / "factory-mirror-tips.json").write_text(
        json.dumps({"main": "f" * 40}) + "\n", encoding="utf-8"
    )
    assert "factory_mirror_tips" not in handle
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    # no mirror evidence — the sync never ran, the land records plainly
    assert seen["sync"] == []
    assert "STATUS:done" in store.tag_strings(42)


def test_poll_green_adopted_pre_review_launch_syncs_from_the_tree_copy(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Upgrade path: a launch still running from before the record moved
    beside the mirror keeps its tips in the tree's scratch — with the
    mirror standing (mirror mode already proven), the legacy copy
    recovers the adopted sync."""
    seen = _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    (tmp_path / "scratch" / "factory-mirror" / "td42.git").mkdir(parents=True)
    tips = {"main": "f" * 40, "prod": "0" * 40}
    (seams["tree"] / "scratch" / "factory-mirror-tips.json").write_text(
        json.dumps(tips) + "\n", encoding="utf-8"
    )
    assert "factory_mirror_tips" not in handle
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    [(_mirror, _cache, _url, got_tips, _expect)] = seen["sync"]
    assert got_tips == tips


def test_poll_green_adopted_mirrored_ship_fails_closed_when_launch_files_are_gone(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review td660513, second pass (regression): the gate code ship runs
    is same-uid with the lander — it can delete BOTH
    ``<launch>.tips.json`` and ``<launch>.git`` under the main checkout's
    scratch, and before the durable stamp that pair's absence read as
    "no mirror": poll recorded a mirror-only squash as landed. Mirror
    mode now also rides the todo's meta — a stamped launch whose launch
    files are all gone still fails closed: no sync, no land record."""
    seen = _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    # the adopted handle's shape — no factory_mirror_tips — the earlier
    # mirrored launch's stamp on the todo, and every launch-side file
    # record deleted by the same-uid gate
    assert "factory_mirror_tips" not in handle
    store.stamp_ref_meta(42, {"factory_mirror": True})
    mirror_root = tmp_path / "scratch" / "factory-mirror"
    assert not (mirror_root / "td42.tips.json").exists()
    assert not (mirror_root / "td42.git").exists()
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "mirror launch record" in (pctx.failure or "")
    assert seen["sync"] == []
    assert not any(c.startswith("landed:") for c in store.comments(42))
    assert "STATUS:done" not in store.tag_strings(42)
    assert "landed_sha" not in pctx.meta_set


def test_poll_green_adopted_unmirrored_ship_records_the_land(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp's absence reads as before: an adopted handle with no
    mirror-mode evidence anywhere — no tips, no launch files, no todo
    stamp, so the token never resolved at launch and ship pushed origin
    itself — lands plainly on its green exit."""
    seen = _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert "factory_mirror_tips" not in handle
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert seen["sync"] == []
    assert "STATUS:done" in store.tag_strings(42)


def test_mirrored_launch_stamps_mirror_mode_on_the_todo(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launch's durable half: a mirrored submit stamps
    ``meta.factory_mirror`` on the todo — the record an adopted handle's
    sync consults when every launch file is gone — alongside the tips
    file beside the mirror."""
    monkeypatch.setattr(
        land_slice._factory_git, "factory_git_token", lambda: "tok-factory"
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    tips = {"main": "a" * 40, "prod": "b" * 40}
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "populate", lambda *a, **k: dict(tips)
    )
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None and handle is not None
    assert handle["factory_mirror_tips"] == tips
    assert store.refs[42].meta["factory_mirror"] is True
    assert (tmp_path / "scratch" / "factory-mirror" / "td42.tips.json").exists()


@pytest.mark.parametrize("raised", [False, True])
def test_mirror_refresh_failure_is_attempt_free_and_names_the_step(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, raised: bool
) -> None:
    """gr730461: a populate that cannot refresh the mirror is infra — the
    land is re-minted attempt-free and the failure names the step."""
    monkeypatch.setattr(
        land_slice._factory_git, "factory_git_token", lambda: "tok-factory"
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )

    def refuse(*a: Any, why: list[str], **k: Any) -> None:
        if raised:
            raise OSError("fetch rc=128: could not resolve host")
        why.append("fetch rc=128: could not resolve host (after 3 tries)")

    monkeypatch.setattr(land_slice._factory_git_mirror, "populate", refuse)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert "fetch rc=128" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert ctx.meta_set["red_class"] == "mirror-refresh"
    assert seams["launch"] == []
    tags = store.tag_strings(42)
    assert "STATUS:open" in tags
    assert not any(tag.startswith("attempt:") for tag in tags)


def test_mirrored_launch_fails_when_the_todo_stamp_cannot_be_written(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like the launch record beside the mirror, a mirror-mode stamp that
    cannot be persisted fails the launch — ship must not run a mirrored
    land no durable record will convict."""
    monkeypatch.setattr(
        land_slice._factory_git, "factory_git_token", lambda: "tok-factory"
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "populate",
        lambda *a, **k: {"main": "a" * 40},
    )
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    def boom(ref_id: int, updates: dict[str, Any]) -> None:
        raise OSError("db gone")

    monkeypatch.setattr(store, "stamp_ref_meta", boom)
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == []
    assert "ship launch failed" in (ctx.failure or "")


def test_tokenless_launch_clears_the_mirror_mode_stamp(seams: dict[str, Any]) -> None:
    """A launch with no resolvable token supersedes an earlier mirrored
    attempt: ``meta.factory_mirror`` on the todo is cleared alongside
    the launch files, so the unmirrored ship's green exit lands plainly."""
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub",),
        meta={"factory_mirror": True},
    )
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None and handle is not None
    assert "factory_mirror_tips" not in handle
    assert not store.refs[42].meta.get("factory_mirror")


def test_poll_green_mirror_sync_clears_the_todo_stamp(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A verified sync discharges the launch's records in both halves:
    ``drop_launch`` for the files, ``meta.factory_mirror`` cleared on
    the todo."""
    _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    store.stamp_ref_meta(42, {"factory_mirror": True})
    handle["factory_mirror_tips"] = {"main": "d" * 40}
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert not store.refs[42].meta.get("factory_mirror")
    assert not (tmp_path / "scratch" / "factory-mirror" / "td42.git").exists()
    assert not (tmp_path / "scratch" / "factory-mirror" / "td42.tips.json").exists()


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
    (seams["tree"] / "scratch").mkdir(parents=True)  # the first land dropped it
    (seams["tree"] / "scratch" / "land_slice.log").touch()
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
    # the clone and the todo's attempt refs are still cleaned up
    assert _ws_names(seams) == ["drop", "delete_builds"]


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
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.add_todo(
        42, tags=("claimed-by:lane-claude-sub",), comments=("handoff: done",)
    )
    # the oracle sees no slice branch on origin → no remote delete
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "ls_remote", lambda u, refs, *, env, **_kw: {}
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


def test_poll_green_evicts_the_slice_stack(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """gr570133: a landed slice's warm compose stack comes down (-v —
    the tree goes too) BEFORE `git worktree remove`; a left-up project
    kept a postgres and a sleep-infinity gate on the docker VM. A land's
    own clone gets the same from ``_slice_ws.drop``; this is the linked
    worktree (a PR land, a land minted before gr741695)."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _legacy_handle(handle, tmp_path)
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


def test_cleanup_down_failure_is_a_note(seams: dict[str, Any], tmp_path: Path) -> None:
    """A failed `compose down` is a cleanup note, never a job failure —
    the land is real either way."""
    seams["evict_ok"] = False
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    legacy = _legacy_handle(handle, tmp_path)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    cleanup = pctx.meta_set["cleanup"]
    assert any(n.startswith("compose down: FAILED") for n in cleanup)
    # the rest of cleanup still ran
    assert ["worktree", "remove", str(legacy)] in _cleanup_cmds(seams)
    assert "compose down: ok" not in cleanup


def test_a_failed_folder_drop_is_a_note_never_a_failure(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr741695: ``_slice_ws.drop`` answering ``False`` (or raising) is a
    cleanup note — the land is real — and the hub's refs and the task's
    ``build_ref`` are still cleared."""
    from precis.workers.job_types import _slice_ws

    monkeypatch.setattr(_slice_ws, "drop", lambda tree, *, notes=None: False)
    store = _Store()
    store.add_todo(
        42, tags=("claimed-by:lane-claude-sub",), meta={"build_ref": {"a": 1}}
    )
    _ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert any(n.startswith("folder drop: FAILED") for n in pctx.meta_set["cleanup"])
    assert _ws_args(seams, "delete_builds") == [42]
    assert store.refs[42].meta["build_ref"] is None


def test_the_build_floor_is_stamped_whatever_delete_builds_answered(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor is what makes a ref left behind harmless: a failing delete
    (or one that raises) changes nothing about it."""

    def boom(hub: Path, todo: int) -> str:
        raise RuntimeError("update-ref failed")

    monkeypatch.setattr(land_slice._slice_ws, "delete_builds", boom)
    store = _Store()
    store.add_todo(
        42, tags=("claimed-by:lane-claude-sub",), meta={"build_ref": {"a": 1}}
    )
    _ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert any("delete FAILED" in n for n in pctx.meta_set["cleanup"])
    assert store.refs[42].meta["build_floor"] == 901
    assert store.refs[42].meta["build_ref"] is None


def test_the_attempt_refs_stay_when_the_floor_could_not_be_stamped(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refs deleted under a ``build_ref`` that still names them, with no
    floor in, refuse a reopened task at launch. With the store down for the
    floor and the clearing stamp the refs are kept; the land stands."""
    store = _Store()
    store.add_todo(
        42, tags=("claimed-by:lane-claude-sub",), meta={"build_ref": {"a": 1}}
    )
    _ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    real = store.stamp_ref_meta

    def down(ref_id: int, stamps: dict[str, Any], *a: Any, **kw: Any) -> Any:
        if "build_floor" in stamps or "build_ref" in stamps:
            raise RuntimeError("store down")
        return real(ref_id, stamps, *a, **kw)

    monkeypatch.setattr(store, "stamp_ref_meta", down)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded" and pctx.failure is None
    assert _ws_args(seams, "delete_builds") == []
    assert any(n.startswith("attempt refs kept") for n in pctx.meta_set["cleanup"])
    assert store.refs[42].meta["build_ref"] == {"a": 1}
    assert "build_floor" not in store.refs[42].meta


def test_the_floor_is_stamped_before_the_land_record(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the land record and the cleanup (here: before the
    record) must find the floor already on the task."""

    def die(*a: Any, **k: Any) -> list[str]:
        raise SystemExit("worker died")

    monkeypatch.setattr(land_slice, "_record_land", die)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    with pytest.raises(SystemExit):
        land_slice.poll(_Ctx(store, _params(seams)), handle)
    assert store.refs[42].meta["build_floor"] == 901
    assert _ws_names(seams) == []  # the cleanup never ran


def test_a_conflict_stamp_reads_head_and_ref_from_the_cut(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy land has no ``params.ref``, and ship's squash moved the
    clone's branch: the stamp carries what the cut resolved and recorded on
    the job."""
    _conflict_git(seams, monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 1, REBASE_CONFLICT_LOG)
    pctx = _Ctx(store, _params(seams))  # no params.head, no params.ref
    pctx.meta[land_slice.LAND_START_KEY] = {
        "head": HEAD,
        "ref": "refs/factory/build/42/0",
    }
    assert land_slice.poll(pctx, handle) is True
    conflict = store.refs[42].meta["conflict"]
    assert conflict["head"] == HEAD and conflict["ref"] == "refs/factory/build/42/0"
    assert conflict["job"] == 901 and isinstance(conflict["job"], int)


def test_a_cut_records_what_it_resolved_on_the_job(seams: dict[str, Any]) -> None:
    _cut_seams(seams, tip=SNAPSHOT)
    store = _Store()
    store.add_todo(42)
    ctx, handle = _cut_submit(store, seams)
    assert handle is not None
    assert ctx.meta_set[land_slice.LAND_START_KEY] == {"head": HEAD, "ref": REF}


def test_the_land_lock_file_goes_with_a_failed_land_whose_ship_is_gone(
    seams: dict[str, Any],
) -> None:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    lock_dir, lock_name = _land_lock(seams)
    assert (lock_dir / lock_name).exists()  # the launch took it
    _finish(handle, 1, "x\n\x1b[31m✖ ruff: 1 error\x1b[0m\n")
    assert land_slice.poll(_Ctx(store, _params(seams)), handle) is True
    assert not (lock_dir / lock_name).exists()


def test_a_held_land_lock_file_is_left_alone(seams: dict[str, Any]) -> None:
    """Removing the file is only done while holding the lock: a ship (or
    its orphan) still behind it keeps the file — and nobody can then take a
    lock on a file that is no longer the one at the path."""
    lock_dir, lock_name = _land_lock(seams)
    held, fd, _detail = land_slice._try_land_lock(lock_dir, lock_name)
    assert not held and fd is not None
    try:
        land_slice._unlink_land_lock(seams["tree"], 42)
        assert (lock_dir / lock_name).exists()
        busy, none_fd, _ = land_slice._try_land_lock(lock_dir, lock_name)
        assert busy and none_fd is None
    finally:
        os.close(fd)
    land_slice._unlink_land_lock(seams["tree"], 42)
    assert not (lock_dir / lock_name).exists()


def test_a_lock_on_a_replaced_file_is_not_a_lock(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that opened the lock file before another unlinked it must
    not take a lock on the dead inode: ``_try_land_lock`` re-checks that the
    file it locked is still the one at the path."""
    lock_dir, lock_name = _land_lock(seams)
    path = lock_dir / lock_name
    real_open = os.open
    opened: list[int] = []

    def open_then_unlink(p: Any, flags: int, mode: int = 0o777) -> int:
        fd = real_open(p, flags, mode)
        if str(p) == str(path) and not opened:
            opened.append(fd)
            os.unlink(p)  # the land that held it removed it just now
        return fd

    monkeypatch.setattr(land_slice.os, "open", open_then_unlink)
    held, fd, _detail = land_slice._try_land_lock(lock_dir, lock_name)
    assert not held and fd is not None
    # the lock we got is on the file that is there now
    assert os.fstat(fd).st_ino == os.stat(path).st_ino
    os.close(fd)


def test_every_fail_reached_under_the_land_lock_names_its_folder_mode() -> None:
    """The reviewer's guard: ``_fail`` drops the job's clone unless told
    otherwise, and a failure reached while this worker holds the land lock
    would read that lock as a ship. Every ``_fail`` call in
    ``_launch_ship`` and in each function it can reach passes ``folder=``
    explicitly — a new call site that forgets fails here."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(land_slice))
    funcs = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.col_offset == 0
    }

    def called(fn: ast.FunctionDef) -> set[str]:
        return {
            c.func.id
            for c in ast.walk(fn)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id in funcs
        }

    reach: set[str] = set()
    todo = ["_launch_ship"]
    while todo:
        name = todo.pop()
        if name in reach:
            continue
        reach.add(name)
        todo.extend(called(funcs[name]))
    # the walk is not vacuous: it reaches the helpers that fail under the lock
    assert {"_land_busy", "_clear_index_locks", "_sync_release"} <= reach
    missing = [
        f"{name}:{c.lineno}"
        for name in sorted(reach - {"_fail", "_fail_workspace"})
        for c in ast.walk(funcs[name])
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id == "_fail"
        and "folder" not in {k.arg for k in c.keywords}
    ]
    assert missing == [], f"_fail without folder= under the land lock: {missing}"


def test_cleanup_skips_a_non_slice_tree_name(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """A dep_check-pinned worktree can name a project outside
    precis-test-slice-*: evict_project's refusal is by design, so the
    cleanup note says 'skipped', never 'FAILED'."""
    tree = tmp_path / ".claude" / "worktrees" / "pr-7-relock"
    (tree / "scratch").mkdir(parents=True)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    handle["worktree"] = str(tree)
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
    assert handoff.startswith("handoff: lander: land FAILED on slice/42: ✖")
    # td565331: the gate's own error lines ride the handoff, so the next
    # session's brief names what to fix, not only the verdict line.
    assert "The gate's error lines:" in handoff
    assert (
        'tests/test_se_flow.py:450: error: Need type annotation for "memo"' in handoff
    )
    assert "test_se_flow_tab.py:193: error:" in handoff
    assert "Found 2 errors" not in handoff and "All checks passed" not in handoff
    # gr741695: nothing is kept for the fix round — the hub holds the
    # commit — so the land's clone is dropped (and no branch is pushed);
    # the attempt refs stay: the rebuild continues from them
    assert _cleanup_cmds(seams) == []
    assert _ws_names(seams) == ["drop"]
    assert not seams["tree"].exists()
    assert "landed_sha" not in pctx.meta_set


def test_a_red_land_saves_nothing_and_leaves_the_hub_alone(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old park-save pushed the branch tip to GitHub on every red; the
    lander adds no work of its own now, so nothing is pushed."""
    pushed: list[Any] = []

    def push(*a: Any, **k: Any) -> str:
        pushed.append(a)
        return "ok"

    monkeypatch.setattr(land_slice, "_push_branch", push)
    store = _Store()
    store.add_todo(
        42, tags=("claimed-by:lane-claude-sub",), meta={"build_ref": {"a": 1}}
    )
    _ctx, handle = _submit(store, seams)
    _finish(handle, 1, "x\n\x1b[31m✖ ruff: 1 error\x1b[0m\n")
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed" and pushed == []
    assert _ws_names(seams) == ["drop"]  # no delete_builds: the work is unlanded
    assert store.refs[42].meta["build_ref"] == {"a": 1}
    assert any("dropped" in t for k, t in pctx.chunks if k == "job_event")


def test_a_red_with_a_ship_still_behind_the_folder_keeps_it(
    seams: dict[str, Any],
) -> None:
    """The folder is dropped only once the ship is gone: a descendant of the
    exited ship still holds the land lock, so the clone is left for the
    host sweep (which settles a terminal land's folder as holding nothing)."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 1, "x\n\x1b[31m✖ ruff: 1 error\x1b[0m\n")
    held, fd, _detail = land_slice._try_land_lock(*_land_lock(seams))
    assert not held and fd is not None
    land_slice._stamp_lock_holder(fd, 99999, 42, "slice/42", SUBJECT)
    seams["alive"] = False
    try:
        pctx = _Ctx(store, _params(seams))
        assert land_slice.poll(pctx, handle) is True
    finally:
        os.close(fd)
    assert pctx.status == "failed"
    assert _ws_names(seams) == [] and seams["tree"].is_dir()
    assert any(
        "kept for the host sweep" in t for k, t in pctx.chunks if k == "job_event"
    )


def test_a_failure_before_launch_drops_the_folder_it_cut(
    seams: dict[str, Any],
) -> None:
    """A deferral before any ship (here a blind drift probe past its cap)
    holds the land lock itself; that is not a ship, so the clone goes."""
    seams["preflight"] = _preflight(0, "", BLIND_DIAG)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = land_slice._launch_ship(
        _Ctx(store, _params(seams)),
        seams["tree"],
        "--quick",
        SUBJECT,
        carry={"main_refusals": land_slice.MAX_MAIN_REFUSALS},
    )
    assert handle is None
    assert _ws_args(seams, "drop") == [seams["tree"]]


# ── a land red on a conflict stamps the task (gr741695) ────────────

REBASE_CONFLICT_LOG = (
    "quick ship (--quick)\n"
    "CONFLICT (content): Merge conflict in src/precis/x.py\n"
    "\x1b[31m✖ rebase conflict onto origin/main — resolve in /w, then 'git add "
    "-A && GIT_EDITOR=true git rebase --continue' and re-run scripts/ship (or "
    "'git rebase --abort' to back out).\x1b[0m\n"
    "SHIP_STATUS: conflict\n"
)
MERGE_CONFLICT_LOG = REBASE_CONFLICT_LOG.replace(
    "rebase conflict onto origin/main", "merge conflict integrating origin/main"
)
BASE_TIP = "b" * 40


def _conflict_git(seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """The clone as ship left it mid-rebase: two conflicted paths and an
    ``origin/main`` tip; reads are recorded with the folder's existence."""
    inner = land_slice._git
    seams["conflict_reads"] = []

    def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args == ["diff", "--name-only", "--diff-filter=U"]:
            seams["conflict_reads"].append(Path(cwd).exists())
            return _proc("src/precis/x.py\ntests/test_x.py\n")
        if args == ["rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"]:
            return _proc(BASE_TIP + "\n")
        return inner(args, cwd)

    monkeypatch.setattr(land_slice, "_git", git)


@pytest.mark.parametrize("log", [REBASE_CONFLICT_LOG, MERGE_CONFLICT_LOG])
def test_a_rebase_or_merge_conflict_stamps_the_task_before_the_drop(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, log: str
) -> None:
    _conflict_git(seams, monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx = _Ctx(store, {**_params(seams), "head": HEAD, "ref": REF})
    handle = land_slice.submit(ctx, None)
    _finish(handle, 1, log)
    pctx = _Ctx(store, {**_params(seams), "head": HEAD, "ref": REF})

    assert land_slice.poll(pctx, handle) is True

    # the red is the ordinary one: an attempt is charged, the handoff as ever
    assert pctx.status == "failed" and "attempt:1" in store.tag_strings(42)
    assert store.comments(42)[-1].startswith("handoff: lander: land FAILED on slice/42")
    conflict = store.refs[42].meta["conflict"]
    assert conflict["head"] == HEAD and conflict["base_tip"] == BASE_TIP
    assert conflict["ref"] == REF
    assert conflict["files"] == ["src/precis/x.py", "tests/test_x.py"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", conflict["at"])
    # the dispatcher compares job ids, not this host's clock with the database's
    assert conflict["job"] == 901 and isinstance(conflict["job"], int)
    assert set(conflict) == {"head", "ref", "base_tip", "files", "at", "job"}
    # read from the clone while it was still there, then the folder went
    assert seams["conflict_reads"] == [True]
    assert _ws_names(seams) == ["drop"] and not seams["tree"].exists()


def test_a_conflict_stamp_falls_back_to_the_pending_land_then_the_branch(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _conflict_git(seams, monkeypatch)
    store = _Store()
    store.add_todo(
        42,
        tags=("claimed-by:lane-claude-sub",),
        meta={"pending_land": {"head": HEAD}},
    )
    _ctx, handle = _submit(store, seams)
    _finish(handle, 1, REBASE_CONFLICT_LOG)
    assert land_slice.poll(_Ctx(store, _params(seams)), handle) is True
    assert store.refs[42].meta["conflict"]["head"] == HEAD


@pytest.mark.parametrize(
    "log",
    [
        "x\n\x1b[31m✖ ruff: 1 error\x1b[0m\n",  # an ordinary red
        # outside the diff: attempt-free, nothing to resolve
        "x\n\x1b[31m✖ gate exited 137 (SIGKILL)\x1b[0m\n",
    ],
)
def test_no_conflict_stamp_without_a_conflict_line(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, log: str
) -> None:
    _conflict_git(seams, monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    _finish(handle, 1, log)
    land_slice.poll(_Ctx(store, _params(seams)), handle)
    assert "conflict" not in store.refs[42].meta
    assert seams["conflict_reads"] == []


def test_is_conflict_refusal() -> None:
    assert land_slice.is_conflict_refusal("✖ rebase conflict onto origin/main — x")
    assert land_slice.is_conflict_refusal("✖ merge conflict integrating origin/main")
    assert not land_slice.is_conflict_refusal("✖ release conflict — ask")
    assert not land_slice.is_conflict_refusal(None)


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
    assert not any(
        c.startswith("handoff: lander: land FAILED") for c in store.comments(42)
    )
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


def test_engine_diff_lands_checked(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _relaxed_deny(monkeypatch)
    seams["changed_paths"] = [_ENGINE_PATH, "src/precis/x.py"]
    store = _Store()
    store.add_todo(42)
    # ``checked`` says quick; the engine diff asks for checked, and the
    # stubbed spot verdict earns it (gr599427).
    ctx = _Ctx(store, {**_params(seams), "checked": False})
    handle = land_slice.submit(ctx, None)
    assert handle is not None and ctx.failure is None
    [(argv, _kw)] = seams["launch"]
    assert argv[1] == "--checked"
    assert ctx.meta_set["engine_paths"] == [_ENGINE_PATH]


def test_non_engine_land_is_unchanged(seams: dict[str, Any]) -> None:
    store = _Store()
    store.add_todo(42)
    ctx, _handle = _submit(store, seams)
    [(argv, _kw)] = seams["launch"]
    assert argv[1] == "--quick"
    # The stamp is always written: an empty list is a checked non-engine
    # land, distinct from a pre-E1 job carrying no stamp at all.
    assert ctx.meta_set["engine_paths"] == []


# ── deny gate (gr645524) ───────────────────────────────────────────
# The land re-checks the resolved deny set before launching ship — the
# run's poll check is the first gate, this is the last (td574960's
# contract: brief text, post-check and land check name the same set in
# every mode). A denied diff is refused, stamped ``human_only_hit`` so
# the node caps at once and the tick's ruling ask opens on the frozen
# branch. The fake store resolves ``deny_paths_for`` to ``STRICT_DENY``
# (no ``app_settings`` row is readable), so ``run_slice.py`` — the
# incident's own path — is the strict-deny case.


def test_unreadable_diff_is_refused(seams: dict[str, Any]) -> None:
    """td645833: the deny gate fails closed — a diff git cannot
    enumerate cannot be checked, so the land refuses and ship never
    launches (the same fail-closed ``run_slice._deny_reason`` takes at
    the first gate). The ``<unreadable>`` stamp still lands — a refused
    job is not a pre-E1 one — but no ``human_only_hit`` cap: an
    ordinary failed attempt, so a fresh worktree may retry."""
    seams["changed_paths"] = None
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert handle is None
    assert ctx.status == "failed" and seams["launch"] == []
    assert "failing closed" in (ctx.failure or "")
    assert ctx.meta_set["engine_paths"] == ["<unreadable>"]
    assert run_slice.HUMAN_ONLY_KEY not in ctx.meta_set
    assert any("land FAILED" in c for c in store.comments(42))
    assert "STATUS:open" in store.tag_strings(42)


def test_submit_refuses_a_deny_path_diff(seams: dict[str, Any]) -> None:
    """gr645524: a branch reaching the land with a resolved-deny path
    never launches ship — the gripe's 'refused by land_slice with a
    coordinator ask': the stamp caps the node and ``_mint_ask``'s
    ruling ask opens, its paths named."""
    seams["changed_paths"] = [_ENGINE_PATH, "src/precis/x.py"]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert handle is None
    assert ctx.status == "failed" and "deny path" in (ctx.failure or "")
    assert seams["launch"] == []
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == [_ENGINE_PATH]
    # The engine stamp still landed — a refused job is not a pre-E1 one.
    assert ctx.meta_set["engine_paths"] == [_ENGINE_PATH]
    assert any("land FAILED" in c and "run_slice.py" in c for c in store.comments(42))
    # The lease released for the ruling — the todo is open, not closed.
    assert "STATUS:open" in store.tag_strings(42)


def test_submit_refuses_a_human_only_diff(seams: dict[str, Any]) -> None:
    """The never-relaxable set bites under every mode — a diff on a
    human-only path is refused even when the lane resolves relaxed."""
    seams["changed_paths"] = ["deploy/inventory/prod.yml", "src/precis/x.py"]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert handle is None
    assert ctx.status == "failed" and seams["launch"] == []
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == ["deploy/inventory/prod.yml"]


def test_submit_refuses_an_interim_deny_diff_under_relaxed(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relaxed releases the engine python, not the harness: the slice's
    own ``scripts/ship`` stays denied — a diff reaching the land with
    it is refused under relaxed too."""
    _relaxed_deny(monkeypatch)
    seams["changed_paths"] = ["scripts/ship", "src/precis/x.py"]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert handle is None
    assert ctx.status == "failed" and seams["launch"] == []
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == ["scripts/ship"]


# td659522 step 4: the one named exception — a pin-bump node's own commit
# may carry the autocatpath_min_version line of deploy/group_vars/all.yml.
# ``pin_bump.land_refusal`` is the content check (its own tests run it on
# real git); these stand it and pin the lander's side.

_GROUP_VARS = "deploy/group_vars/all.yml"
_PIN_DIFF = [_GROUP_VARS, "pyproject.toml", "uv.lock"]


def _stand_pin_check(
    monkeypatch: pytest.MonkeyPatch, answer: str | None
) -> list[tuple[Any, ...]]:
    from precis.workers.job_types import pin_bump

    asked: list[tuple[Any, ...]] = []

    def land_refusal(*args: Any) -> str | None:
        asked.append(args)
        return answer

    monkeypatch.setattr(pin_bump, "land_refusal", land_refusal)
    return asked


def test_submit_admits_a_pin_nodes_group_vars_line(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails every pin bump is refused at the land (the group_vars
    file is human-only), or it lands without the admission being recorded."""
    asked = _stand_pin_check(monkeypatch, None)
    seams["changed_paths"] = list(_PIN_DIFF)
    store = _Store()
    _pin_todo(store)
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None and handle is not None, ctx.failure
    assert len(seams["launch"]) == 1
    assert run_slice.HUMAN_ONLY_KEY not in ctx.meta_set
    assert ctx.meta_set["pin_exempt"] == _GROUP_VARS
    [(_, todo_id, tree, base, paths)] = asked
    assert (todo_id, tree, paths) == (42, seams["tree"], _PIN_DIFF)
    assert base == seams["changed_base"][-1]
    [event] = [t for k, t in ctx.chunks if k == "job_event" and "admitted" in t]
    assert _GROUP_VARS in event and FULL_SHA[:9] in event


def test_submit_refuses_a_pin_node_whose_check_fails(seams: dict[str, Any]) -> None:
    """If this fails a pin node's group_vars diff lands although the real
    content check refused it (here: the fake git proves no commit)."""
    seams["changed_paths"] = list(_PIN_DIFF)
    store = _Store()
    _pin_todo(store, base="b" * 40)
    ctx, handle = _submit(store, seams)
    assert handle is None and seams["launch"] == []
    assert ctx.status == "failed"
    assert "pin-bump exemption refused: " in (ctx.failure or "")
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == [_GROUP_VARS]
    assert "pin_exempt" not in ctx.meta_set
    assert any("exemption refused" in c for c in store.comments(42))


def test_the_pin_exemption_admits_only_the_group_vars_path(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a passing pin check lets another denied path through
    with the group_vars file."""
    _stand_pin_check(monkeypatch, None)
    seams["changed_paths"] = [*_PIN_DIFF, "deploy/inventory/prod.yml"]
    store = _Store()
    _pin_todo(store)
    ctx, handle = _submit(store, seams)
    assert handle is None and seams["launch"] == []
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == ["deploy/inventory/prod.yml"]
    # nothing was admitted, so nothing says so
    assert "pin_exempt" not in ctx.meta_set
    assert not any("admitted" in t for _, t in ctx.chunks)


def test_a_plain_slice_touching_group_vars_is_never_asked(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a todo without a pin record reaches the exemption
    check, or its refusal starts naming one."""
    asked = _stand_pin_check(monkeypatch, None)
    seams["changed_paths"] = [_GROUP_VARS]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert handle is None and seams["launch"] == [] and asked == []
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == [_GROUP_VARS]
    assert "exemption" not in (ctx.failure or "")


@pytest.mark.parametrize("unreadable", [True, False])
def test_a_pin_check_that_cannot_run_fails_the_attempt_without_the_cap(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, unreadable: bool
) -> None:
    """If this fails a content check that could not run (git did not
    answer, or it raised) admits the file, or caps the node for a ruling
    although a rebuilt bump would pass."""
    from precis.workers.job_types import pin_bump

    def boom(*args: Any) -> str | None:
        if unreadable:
            raise pin_bump.LandUnreadable("git could not read the branch head")
        raise RuntimeError("git went away")

    monkeypatch.setattr(pin_bump, "land_refusal", boom)
    seams["changed_paths"] = list(_PIN_DIFF)
    store = _Store()
    _pin_todo(store)
    ctx, handle = _submit(store, seams)
    assert handle is None and seams["launch"] == []
    assert ctx.status == "failed"
    assert run_slice.HUMAN_ONLY_KEY not in ctx.meta_set
    assert run_slice.ATTEMPT_FREE_KEY not in ctx.meta_set
    why = "git could not read" if unreadable else "the check itself failed"
    assert f"could not be checked ({why}" in (ctx.failure or "")
    assert any("the pin bump is built again" in c for c in store.comments(42))


def test_an_unreadable_pin_check_beside_another_denied_path_still_caps(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a second denied path escapes the human-only cap because
    the pin check beside it could not run."""
    from precis.workers.job_types import pin_bump

    def boom(*args: Any) -> str | None:
        raise pin_bump.LandUnreadable("git could not read the branch head")

    monkeypatch.setattr(pin_bump, "land_refusal", boom)
    seams["changed_paths"] = [*_PIN_DIFF, "deploy/inventory/prod.yml"]
    store = _Store()
    _pin_todo(store)
    ctx, handle = _submit(store, seams)
    assert handle is None and seams["launch"] == []
    assert ctx.meta_set[run_slice.HUMAN_ONLY_KEY] == [
        _GROUP_VARS,
        "deploy/inventory/prod.yml",
    ]


def test_full_refusal_on_an_engine_land_relaunches_quick(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr599427 (Reto 2026-11-08): an engine land's FULL-selector refusal
    relaunches ``--quick`` like every other land — no land runs the full
    local gate; the release gate's GitHub CI runs the suite."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    _relaxed_deny(monkeypatch)
    seams["changed_paths"] = [_ENGINE_PATH]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = [_ENGINE_PATH]
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
    _relaxed_deny(monkeypatch)
    seams["changed_paths"] = [_ENGINE_PATH]
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = [_ENGINE_PATH]
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
    _relaxed_deny(monkeypatch)
    seams["changed_paths"] = [_ENGINE_PATH]
    store = _Store()
    store.add_todo(42)
    ctx, handle = _submit(store, seams)
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = [_ENGINE_PATH]
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
    assert handoff.startswith("handoff: lander: land BLOCKED on slice/42:")
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
    [(kind, text)] = [c for c in pctx.chunks if c[0] == "job_summary"]
    assert kind == "job_summary" and BLIND_DIAG in text
    # nothing was launched, so the clone is dropped with the failure
    assert _ws_args(seams, "drop") == [seams["tree"]]


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
    monkeypatch.setattr(land_slice._factory_git, "factory_git_token", lambda: None)
    pre = _REAL_DRIFT_PREFLIGHT(tree)
    assert pre["ran"] and pre["age"] == "3.2"
    # The lib module is tried first (gr550044's shim would resolve lib/
    # beside itself and find nothing materialised alone), under `python
    # -I` so nothing in the tree decides its imports.
    target = tree / land_slice.PREFLIGHT_LIB_REL
    assert seen[0][:3] == [sys.executable, "-I", str(target)]
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
    monkeypatch.setattr(land_slice._factory_git, "factory_git_token", lambda: None)
    pre = _REAL_DRIFT_PREFLIGHT(tree)
    assert pre == {"ran": True, "rc": 0, "age": "", "diag": BLIND_DIAG}
    [call] = seen
    assert call["argv"] == [
        sys.executable,  # never the helper's `uv run` shebang (gr551238)
        "-I",  # isolated: no cwd/script dir on sys.path, no user site
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


def test_drift_preflight_never_runs_branch_code_under_the_token(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review td660513 finding 1 (regression): the token resolves but no
    verified mirror source came with it — the branch's own helper (or a
    ``scratch/lib`` shadow it could import) must NOT run under the
    credential: the probe refuses instead."""
    tree = seams["tree"]
    helper = tree / "scripts" / "last-gated-main-sha"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        land_slice._factory_git, "factory_git_token", lambda: "tok-factory"
    )
    monkeypatch.setattr(
        land_slice._factory_git, "git_auth_env", lambda base=None: {"GH_TOKEN": "x"}
    )
    seen: list[list[str]] = []

    def spy_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(land_slice.subprocess, "run", spy_run)
    pre = _REAL_DRIFT_PREFLIGHT(tree)
    assert pre["ran"] is True
    assert "refusing" in pre["diag"]
    assert seen == [], "a helper the branch can shape ran under the token"


def test_drift_preflight_sources_the_helper_from_the_verified_mirror(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review td660513 finding 1 (regression): with the per-launch mirror
    in play the helper is ``git show``n from the oracle-verified main tip
    — never the branch's tree copy — and still runs under ``python -I``."""
    tree = seams["tree"]
    branch_copy = tree / "scripts" / "lib" / "last_gated_main_sha.py"
    branch_copy.parent.mkdir(parents=True)
    branch_copy.write_text("# branch copy — must NOT run\n", encoding="utf-8")
    mirror = tmp_path / "scratch" / "factory-mirror" / "td42.git"
    mirror.mkdir(parents=True)
    tip = "f" * 40
    main_copy = "# main copy with --diag (verified mirror tip)\n"
    git_calls: list[list[str]] = []
    monkeypatch.setattr(
        land_slice._factory_git, "factory_git_token", lambda: "tok-factory"
    )
    monkeypatch.setattr(
        land_slice._factory_git, "git_auth_env", lambda base=None: {"GH_TOKEN": "x"}
    )

    def fake_mirror_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        git_calls.append(list(args))
        if args[-1].endswith(":scripts/lib/last_gated_main_sha.py"):
            return _proc(main_copy)
        return _proc(rc=1)

    monkeypatch.setattr(land_slice._factory_git_mirror, "_run", fake_mirror_run)
    seen: list[list[str]] = []

    def spy_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="1.0\n", stderr="")

    monkeypatch.setattr(land_slice.subprocess, "run", spy_run)
    pre = _REAL_DRIFT_PREFLIGHT(tree, mirror=mirror, main_tip=tip)
    assert pre["ran"] and pre["age"] == "1.0"
    # the helper came from `git show <tip>:...` inside the mirror — the
    # branch's own lib copy was never read, and the read ignores a
    # planted refs/replace for the verified tip (pre-land review pass)
    [show] = git_calls
    assert "show" in show and f"{tip}:scripts/lib/last_gated_main_sha.py" in show
    assert "--no-replace-objects" in show
    target = tree / land_slice.PREFLIGHT_LIB_REL
    assert target.read_text(encoding="utf-8") == main_copy
    assert seen[0][:3] == [sys.executable, "-I", str(target)]
    # a bad tip: no helper at all, refusal — not the branch copy
    pre = _REAL_DRIFT_PREFLIGHT(tree, mirror=mirror, main_tip="notasha")
    assert pre["ran"] and "refusing" in pre["diag"]


def test_deferred_notify_writes_the_subjects_handles(seams: dict[str, Any]) -> None:
    """Review td660513 finding 4 (regression): ship's land_notify was held
    (``PRECIS_LAND_NOTIFY=0``); after the origin sync verified the sha the
    lander writes every handle the subject names — this todo's own record
    is :func:`_record_land`'s, the rest land_notify's shape."""
    store = _Store()
    store.add_todo(42)
    store.add_todo(55)
    store.add_todo(7)
    store.add_todo(9)
    sha = "c" * 40
    subject = "fix the credential boundary (td42, gr7, refs gr9, td55)"
    notes = land_slice._deferred_notify(store, subject, sha, 42, seams["tree"])
    joined = " ".join(notes)
    # own todo untouched by the deferred pass — _record_land's job
    assert not any(c.startswith("landed:") for c in store.comments(42))
    assert any("td55" in n for n in notes)
    assert any(c.startswith("landed: ccccccccc") for c in store.comments(55))
    assert "STATUS:done" in store.tag_strings(55)
    assert "resolved" in joined and "auto_verify" in joined
    assert "partial" in joined
    gripe9 = [
        c.text for c in store.chunks.rows.get(9, []) if c.chunk_kind == "gripe_comment"
    ]
    assert any("landed (partial):" in t for t in gripe9)
    assert "STATUS:auto_verify" not in store.tag_strings(9)
    gripe7 = [
        c.text for c in store.chunks.rows.get(7, []) if c.chunk_kind == "gripe_comment"
    ]
    assert any(t.startswith("resolved: ccccccccc") for t in gripe7)
    assert "STATUS:auto_verify" in store.tag_strings(7)
    # idempotent: a second pass writes nothing new
    second = land_slice._deferred_notify(store, subject, sha, 42, seams["tree"])
    assert all("already" in n or "left" in n for n in second)


def test_deferred_notify_leaves_a_second_repo_todo_alone(
    seams: dict[str, Any],
) -> None:
    """If this fails a precis land whose subject names a todo that landed
    in another repo (td659522) writes a precis ``landed:`` sha on it — the
    reviewer of that slice would then read a sha its repo does not hold."""
    store = _Store()
    store.add_todo(42)
    store.add_todo(55, status="done", comments=("landed: 123456789 — engine",))
    store.refs[55].meta[lanes.LANDED_REPO_KEY] = "catpath"
    store.add_todo(56)
    store.refs[56].meta[lanes.LANDED_REPO_KEY] = lanes.DEFAULT_REPO
    sha = "c" * 40
    subject = "build(deps): bump autocatpath to v0.50.0 (td42, td55, td56)"
    notes = land_slice._deferred_notify(store, subject, sha, 42, seams["tree"])
    assert store.comments(55) == ["landed: 123456789 — engine"]
    assert "todo td55: left alone (it landed in catpath)" in notes
    assert any(c.startswith("landed: ccccccccc") for c in store.comments(56))


def test_resolve_gripe_is_the_one_writer_for_every_caller(
    seams: dict[str, Any],
) -> None:
    """If this fails the shared ``resolved:`` writer repeats a comment that
    already names the sha, moves a settled gripe, or drops land_notify's
    comment on a settled one."""
    store = _Store()
    store.add_todo(7, status="open")
    store.add_todo(8, status="done")
    sha = "c" * 40

    def gripe_comments(gid: int) -> list[str]:
        return [
            c.text
            for c in store.chunks.rows.get(gid, [])
            if c.chunk_kind == "gripe_comment"
        ]

    first = land_slice._resolve_gripe(store, 7, sha, "why", status="auto_verify")
    assert first == ["resolved comment", "STATUS:auto_verify"]
    assert gripe_comments(7) == ["resolved: ccccccccc (main, release pending) — why"]
    again = land_slice._resolve_gripe(store, 7, sha, "why", status="auto_verify")
    assert again == ["left STATUS:auto_verify"] and len(gripe_comments(7)) == 1
    quiet = land_slice._resolve_gripe(store, 8, sha, "why", status="verify")
    assert quiet == ["left STATUS:done"] and gripe_comments(8) == []
    noted = land_slice._resolve_gripe(
        store, 8, sha, "why", status="auto_verify", settled_comment=True
    )
    assert noted == ["resolved comment", "left STATUS:done"]
    assert "STATUS:done" in store.tag_strings(8)


def test_deferred_notify_replays_gripe_note(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review td660513 finding 4 (regression): ship's gripe-note was held
    too (``PRECIS_GRIPE_NOTE=0``) — its ``landed <sha9> on main`` comment
    goes on every gripe the SQUASH MESSAGE names (subject tail and
    ``Fixes:`` trailers alike), before land_notify's writes so a
    ``resolved:`` stays the newest."""
    store = _Store()
    store.add_todo(42)
    store.add_todo(7)
    store.add_todo(11)
    sha = "c" * 40
    message = "fix(workers): the boundary (gr7)\n\nbody\nFixes: gr11, gr7\n"
    real_git = land_slice._git
    monkeypatch.setattr(
        land_slice,
        "_git",
        lambda args, cwd: (
            _proc(message) if args[:2] == ["show", "-s"] else real_git(args, cwd)
        ),
    )
    subject = "fix(workers): the boundary (gr7)"
    notes = land_slice._deferred_notify(store, subject, sha, 42, seams["tree"])
    gripe7 = [
        c.text for c in store.chunks.rows.get(7, []) if c.chunk_kind == "gripe_comment"
    ]
    gripe11 = [
        c.text for c in store.chunks.rows.get(11, []) if c.chunk_kind == "gripe_comment"
    ]
    # both gripes got gripe-note's comment — gr7 named by the subject,
    # gr11 by the Fixes: trailer — and gr7's resolved: is still newest
    assert "landed ccccccccc on main" in gripe7
    assert gripe7[-1].startswith("resolved:")
    assert gripe11 == ["landed ccccccccc on main"]
    assert any("gr11" in n for n in notes)
    # a second pass posts neither comment twice
    second = land_slice._deferred_notify(store, subject, sha, 42, seams["tree"])
    assert any("already posted" in n for n in second)
    assert [
        c.text for c in store.chunks.rows.get(11, []) if c.chunk_kind == "gripe_comment"
    ] == ["landed ccccccccc on main"]


def test_push_branch_runs_in_the_ops_repo_not_the_tree(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review td660513 finding 2 (regression): the killed-land branch push
    never runs credential-bearing git from the slice tree's gitdir — its
    config and hooks are branch-written. The push runs in a fresh ops
    repo under the trusted boundary."""
    tree = seams["tree"]
    monkeypatch.setenv("PRECIS_FACTORY_GIT_TOKEN", "tok-factory")
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    sha = "d" * 40
    seen: list[tuple[list[str], Path]] = []

    def fake_run(
        args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append((list(args), Path(cwd)))
        return _proc(rc=0)

    monkeypatch.setattr(land_slice._factory_git_mirror, "_run", fake_run)
    real_git = land_slice._git
    monkeypatch.setattr(
        land_slice,
        "_git",
        lambda args, cwd: (
            _proc(sha + "\n") if args[:1] == ["rev-parse"] else real_git(args, cwd)
        ),
    )
    note = land_slice._push_branch(tree, "slice/42")
    assert "on origin" in note
    pushes = [a for a, _c in seen if "push" in a]
    assert pushes and f"{sha}:refs/factory/slice/42" in pushes[0]
    for args, cwd in seen:
        assert args[:3] == ["git", "-c", "core.hooksPath=/dev/null"]
        assert cwd != tree and tree not in cwd.parents


class _FakeOrigin:
    """A scripted origin for the slice-branch push (gr665602): answers
    ``ls-remote`` from ``tips`` and applies a push's ``--force-with-lease``
    the way git does — refused as ``stale info`` when the expectation is
    not the current tip (empty = the ref must be absent)."""

    def __init__(self, tips: dict[str, str]) -> None:
        self.tips = dict(tips)
        self.pushes: list[list[str]] = []
        #: ``ref -> sha`` applied between the ls-remote read and the push.
        self.move_before_push: dict[str, str] = {}

    def run(
        self, args: list[str], *, cwd: Path, env: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in args:
            out = "".join(
                f"{sha}\trefs/factory/{ref}\n"
                for ref, sha in self.tips.items()
                if f"refs/factory/{ref}" in args
            )
            self.tips.update(self.move_before_push)
            return _proc(out)
        if "push" not in args:
            return _proc()  # the ops repo's init / alternates plumbing
        self.pushes.append(list(args))
        spec = next(a for a in args if ":refs/factory/" in a and "=" not in a)
        new, _, dest = spec.partition(":")
        ref = dest.removeprefix("refs/factory/")
        lease = next((a for a in args if a.startswith("--force-with-lease=")), None)
        if lease is None:
            held = self.tips.get(ref)
            # Plain push: only a fast-forward; the rebased sha is not one.
            if held is not None and held != new:
                return subprocess.CompletedProcess(
                    [],
                    1,
                    stdout="",
                    stderr=f"! [rejected] {ref} -> {ref} (non-fast-forward)",
                )
        else:
            name, _, expect = lease.removeprefix("--force-with-lease=").partition(":")
            assert name == dest
            if self.tips.get(ref, "") != expect:
                return subprocess.CompletedProcess(
                    [],
                    1,
                    stdout="",
                    stderr=f"! [rejected] {ref} -> {ref} (stale info)",
                )
        self.tips[ref] = new
        return _proc()


def _slice_push_rig(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    origin: _FakeOrigin,
    sha: str,
) -> Path:
    monkeypatch.setenv("PRECIS_FACTORY_GIT_TOKEN", "tok-factory")
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: "https://github.com/example/precis.git",
    )
    monkeypatch.setattr(land_slice._factory_git_mirror, "_run", origin.run)
    real_git = land_slice._git
    monkeypatch.setattr(
        land_slice,
        "_git",
        lambda args, cwd: (
            _proc(sha + "\n") if args[:1] == ["rev-parse"] else real_git(args, cwd)
        ),
    )
    return Path(seams["tree"])


def test_push_branch_rebased_slice_overwrites_its_own_earlier_push(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr665602: a slice already on origin and then rebased (a rebase
    conflict brief, a re-arm onto newer main) is pushed under a lease on
    the tip origin holds, so the rebased history lands instead of the
    non-fast-forward rejection that burned the retry ladder."""
    old, new = "a" * 40, "b" * 40
    origin = _FakeOrigin({"slice/42": old})
    tree = _slice_push_rig(seams, monkeypatch, origin, new)
    note = land_slice._push_branch(tree, "slice/42")
    assert note == "branch push of slice/42: on origin (replaced)"
    assert origin.tips["slice/42"] == new
    assert f"--force-with-lease=refs/factory/slice/42:{old}" in origin.pushes[0]


def _git_run(repo: Path, *args: str) -> str:
    """A real ``git -C <repo>`` for the real-remote push test — returns
    stripped stdout, raises on failure (test setup must not go on)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout.strip()


def test_push_branch_replaces_amended_tip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gr663738: real git against a local bare remote — the amended
    commit a FIX-verdict rebuild left replaces the stale tip origin
    still holds (the lease on the tip ``ls-remote`` just read), the
    remote tip ends at the new sha and the note marks the replace; a
    remote moved between that read and the push still fails.

    A real linked worktree is required: the ops repo borrows the common
    object store via alternates, which a primary checkout never gets."""

    def commit(repo: Path, *args: str) -> None:
        _git_run(
            repo,
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            *args,
        )

    main, bare, tree = tmp_path / "main", tmp_path / "origin.git", tmp_path / "tree"
    main.mkdir()
    _git_run(main, "init", "-b", "main")
    (main / "f.txt").write_text("base\n", encoding="utf-8")
    _git_run(main, "add", "-A")
    commit(main, "-m", "base")
    _git_run(tmp_path, "init", "--bare", str(bare))
    _git_run(main, "worktree", "add", str(tree), "-b", "slice/42")
    # The unfixed commit reaches origin; the FIX-verdict rebuild amends it.
    (tree / "f.txt").write_text("unfixed\n", encoding="utf-8")
    commit(tree, "-a", "-m", "unfixed (td42)")
    _git_run(tree, "push", str(bare), "refs/heads/slice/42:refs/factory/slice/42")
    (tree / "g.txt").write_text("fix\n", encoding="utf-8")
    _git_run(tree, "add", "-A")
    commit(tree, "--amend", "-m", "fixed (td42)")
    old_tip = _git_run(bare, "rev-parse", "refs/factory/slice/42")
    new_tip = _git_run(tree, "rev-parse", "refs/heads/slice/42")
    assert old_tip != new_tip

    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    # Local path transport needs no credential; keep the host's out of it.
    monkeypatch.delenv("PRECIS_FACTORY_GIT_TOKEN", raising=False)
    monkeypatch.setattr(
        land_slice._factory_git_mirror,
        "github_url",
        lambda root, *, env: str(bare),
    )
    assert land_slice._push_branch(tree, "slice/42") == (
        "branch push of slice/42: on origin (replaced)"
    )
    assert _git_run(bare, "rev-parse", "refs/factory/slice/42") == new_tip

    # A move between the ls-remote read and the push is refused.
    commit(main, "--allow-empty", "-m", "theirs")
    theirs = _git_run(main, "rev-parse", "HEAD")
    commit(tree, "--allow-empty", "-m", "v2 (td42)")
    real_ls = land_slice._factory_git_mirror.ls_remote

    def moved_ls(url: str, refs: Any, *, env: Any, **kw: Any) -> Any:
        out = real_ls(url, refs, env=env, **kw)
        _git_run(main, "push", "--force", url, f"{theirs}:refs/factory/slice/42")
        return out

    monkeypatch.setattr(land_slice._factory_git_mirror, "ls_remote", moved_ls)
    note = land_slice._push_branch(tree, "slice/42")
    assert note.startswith("branch push of slice/42: FAILED (lease refused")
    assert _git_run(bare, "rev-parse", "refs/factory/slice/42") == theirs


def test_push_branch_first_push_expects_the_ref_absent(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _FakeOrigin({})
    tree = _slice_push_rig(seams, monkeypatch, origin, "c" * 40)
    assert land_slice._push_branch(tree, "slice/42").endswith(": on origin")
    assert "--force-with-lease=refs/factory/slice/42:" in origin.pushes[0]
    assert origin.tips["slice/42"] == "c" * 40


def test_push_branch_lease_refused_when_someone_else_moved_the_branch(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr665602: the branch moves between the tip read and the push — the
    lease refuses, the note still carries FAILED (run_slice raises on it)
    and origin keeps the other writer's tip."""
    old, theirs, new = "a" * 40, "d" * 40, "b" * 40
    origin = _FakeOrigin({"slice/42": old})
    origin.move_before_push = {"slice/42": theirs}
    tree = _slice_push_rig(seams, monkeypatch, origin, new)
    note = land_slice._push_branch(tree, "slice/42")
    assert note.startswith("branch push of slice/42: FAILED (lease refused")
    assert old[:9] in note
    assert origin.tips["slice/42"] == theirs


def test_push_branch_unreadable_origin_falls_back_to_the_plain_push(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _FakeOrigin({})
    tree = _slice_push_rig(seams, monkeypatch, origin, "c" * 40)
    monkeypatch.setattr(
        land_slice._factory_git_mirror, "ls_remote", lambda u, refs, *, env, **_kw: None
    )
    assert land_slice._push_branch(tree, "slice/42").endswith(": on origin")
    assert not any(a.startswith("--force-with-lease") for a in origin.pushes[0])


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
    # land_env resolves the host root for the git ceiling: pin it, so the
    # patched subprocess.run below sees only the collect probe
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
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


def test_engine_land_goes_quick_on_a_wide_selection(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr599427: an engine diff follows the same spot rule — no more
    unconditional ``--checked``."""
    _relaxed_deny(monkeypatch)
    seams["spot"] = False
    seams["changed_paths"] = [_ENGINE_PATH]
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, {**_params(seams), "checked": False})
    handle = land_slice.submit(ctx, None)
    assert handle is not None and ctx.failure is None
    [(argv, _kw)] = seams["launch"]
    assert argv[1] == "--quick"
    assert ctx.meta_set["engine_paths"] == [_ENGINE_PATH]


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
        "handoff: lander: land BLOCKED on slice/42: main refused the land "
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


BUSY_LOG = (
    "quick ship (--quick)\n"
    "release lifecycle busy — a cut, deploy, abandon or release fix is "
    "running; retry after it completes\n"
    "\x1b[31m✖ release lifecycle busy — nothing pushed.\x1b[0m\n"
)


def test_poll_a_busy_release_lock_waits_a_minute_on_its_own_counter(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """Sibling release fixes build side by side and reach ``ship --onto``'s
    push together; the one that finds the round lock held runs again a
    minute later, not after the half-hour main-refusal backoff, and the
    wait does not spend the main-refusal budget."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    handle["main_refusals"] = land_slice.MAX_MAIN_REFUSALS
    _finish(handle, 1, BUSY_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.status is None and pctx.failure is None
    assert handle["release_busy"] == 1
    assert handle["main_refusals"] == land_slice.MAX_MAIN_REFUSALS
    assert handle["not_before"] == "2026-11-01T10:01:00Z"
    [(kind, text)] = pctx.chunks
    assert kind == "job_event" and "host-side refusal #1" in text
    assert "release lifecycle busy" in text
    assert store.comments(42) == []
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))


def test_poll_a_release_lock_busy_past_its_cap_blocks_the_land(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    handle["release_busy"] = land_slice.MAX_RELEASE_BUSY_WAITS
    _finish(handle, 1, BUSY_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.failure is not None


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
        "handoff: lander: land BLOCKED on slice/42: the lander's host refused the land "
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
    assert store.comments(42)[-1].startswith(
        "handoff: lander: land FAILED on slice/42: ✖ ruff"
    )
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
    assert handoff.startswith("handoff: lander: land BLOCKED on slice/42:")
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
    assert store.comments(42)[-1].startswith("handoff: lander: land FAILED on slice/42")


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
    assert store.comments(42)[-1].startswith("handoff: lander: land FAILED on slice/42")


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
    assert store.comments(42)[-1].startswith("handoff: lander: land FAILED on slice/42")
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


def test_readopted_ship_keeps_its_started_at_deadline(
    seams: dict[str, Any], clock: dict[str, datetime]
) -> None:
    """gr647913: the wall-clock deadline rides the job handle —
    ``handle.deadline`` = ``started_at`` + wall + the waits already
    observed — so a worker restart's re-adoption resumes the job's own
    count. ``now + wall`` at the re-adopt poll reset the run's elapsed
    time and moved the kill bound out by the whole run."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    assert handle["deadline"] == T0.timestamp() + land_slice.wall_seconds()
    # ship seen working at T0+10 — the wait that follows counts from
    # that last-seen instant, not from the launch all over.
    Path(handle["log"]).write_text("gate env → synced offline\n", encoding="utf-8")
    clock["t"] = T0 + timedelta(seconds=10)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    Path(handle["log"]).write_text(
        "gate slot: waiting at position 2 of 6 (2 slots ahead are busy)\n",
        encoding="utf-8",
    )
    clock["t"] = T0 + timedelta(seconds=30)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.meta_set["deadline"] == T0.timestamp() + land_slice.wall_seconds() + 20
    # A worker restart re-adopts the running job at T0+50: the deadline
    # resumes the handle's count — T0 + wall + the observed wait —
    # never T0+50 + wall.
    clock["t"] = T0 + timedelta(seconds=50)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    assert pctx.meta_set["deadline"] == T0.timestamp() + land_slice.wall_seconds() + 40
    assert handle["deadline"] == T0.timestamp() + land_slice.wall_seconds() + 40


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
        "  gate slot 0: held 45m by slice-td97 (pid 111 on spruce, alive)\n"
        "  gate slot 1: held 12m by slice-td98 (pid 222 on spruce, alive)\n"
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
        "  gate slot 0: held 45m by slice-td97 (pid 111 on spruce, alive)\n"
        "  gate slot 1: held 12m by slice-td98 (pid 222 on spruce, alive)\n"
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
        "  gate slot 0: held 3m by w1 (pid 11 on spruce, alive)\n"
        "  gate slot 1: held 1m by w2 (pid 22 on spruce, alive)\n",
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
        "  gate slot 0: held 4m by w1 (pid 11 on spruce, alive)\n",
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
        "  gate slot 0: held 4m by w1 (pid 11 on spruce, alive)\n"
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
        "  gate slot 0: held 4m by w1 (pid 11 on spruce, alive)\n"
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
    _relaxed_deny(monkeypatch)
    seams["diff_names"] = "src/precis/x.py\n"
    seams["changed_paths"] = [_ENGINE_PATH]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = [_ENGINE_PATH]
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    verify = handle["verify"]
    assert verify["phase"] == "deselect" and "full_selection" not in verify
    Path(verify["log"]).write_text(
        "gate slot: waiting at position 1 of 3 (2 slots ahead are busy)\n"
        "  gate slot 0: held 9m by w1 (pid 11 on spruce, alive)\n",
        encoding="utf-8",
    )
    clock["t"] = T0 + timedelta(seconds=90)
    assert land_slice.poll(pctx, handle) is False
    assert verify["gate_wait"].startswith("gate slot 0: held")
    assert (
        pctx.meta_set["deadline"]
        == (T0 + timedelta(seconds=90)).timestamp() + land_slice.wall_seconds()
    )


def test_kill_signals_ship_and_pushes_nothing(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr741695: a killed land SIGTERMs ship and pushes no branch (it used
    to push ``slice/<id>`` so the commit survived a worktree reap, gr598110:
    the hub holds the commit now). The clone is left for the janitor."""
    pushed: list[tuple[str, str]] = []

    def push(tree: Path, branch: str, sha: str | None = None) -> str:
        pushed.append((str(tree), branch))
        return "ok"

    monkeypatch.setattr(land_slice, "_push_branch", push)
    signalled: list[tuple[int, int]] = []
    # The fake ship pgid (5151) is a real id on the runner: never signal it.
    monkeypatch.setattr(
        land_slice._rs, "_signal_group", lambda pgid, sig: signalled.append((pgid, sig))
    )
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    ctx = _Ctx(store, _params(seams))
    land_slice.kill(ctx, handle)
    assert signalled == [(5151, signal.SIGTERM)]
    assert pushed == []
    assert seams["tree"].is_dir() and _ws_names(seams) == []


def test_kill_skips_a_pgid_owned_by_another_user(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reused pgid owned by another user (EPERM) is skipped, not raised
    (r331's red gate, a real 5151 on the runner)."""

    def eperm(pgid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(land_slice._rs, "_signal_group", eperm)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    _ctx, handle = _submit(store, seams)
    land_slice.kill(_Ctx(store, _params(seams)), handle)


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
    assert handoff.startswith("handoff: lander: land FAILED on slice/42:")
    assert "flaky or order-dependent" in handoff
    assert "the lander re-lands" in handoff
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "green standalone" in summary and OUTSIDE_ID in summary


def test_main_probe_skips_the_refresh_when_the_hub_cannot_be_synced(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hub's main is only as new as the sync: when GitHub cannot be read
    the forced fetch is skipped — it could move the checkout's origin/main
    backwards — the probe tests what the checkout has, and the job event
    says so."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    monkeypatch.setattr(land_slice, "_checked_selection_full", lambda tree: False)
    seams["diff_names"] = "src/precis/x.py\n"
    seams["sync_tip"] = None
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    assert handle["verify"]["phase"] == "main"
    calls = [a for a, _ in seams["git"]]
    assert not any(
        a[0] == "fetch" and a[-1] == "+refs/heads/main:refs/remotes/origin/main"
        for a in calls
    )
    assert any(a[:2] == ["worktree", "add"] for a in calls)  # still probes
    assert any("could not be synced" in t for k, t in pctx.chunks if k == "job_event")


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
    monkeypatch.setattr(land_slice, "_checked_selection_full", lambda tree: False)
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
    # the host checkout's origin/main comes from the hub first: no land
    # fetches in that checkout any more
    calls = [a for a, _ in seams["git"]]
    refresh = next(
        i
        for i, a in enumerate(calls)
        if a[0] == "fetch" and a[-1] == "+refs/heads/main:refs/remotes/origin/main"
    )
    assert calls[refresh][-2].endswith("precis-hub.git")
    assert refresh < calls.index(adds[0])
    # and the hub is synced from GitHub (main) first, so the forced fetch
    # cannot move the checkout's origin/main backwards
    syncs = [a for a in _ws_args(seams, "sync_hub") if a["base"] == "main"]
    assert len(syncs) == 1 and syncs[0]["url"] == GITHUB_URL  # the folder was there
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
    monkeypatch.setattr(land_slice, "_checked_selection_full", lambda tree: False)
    _relaxed_deny(monkeypatch)
    seams["diff_names"] = "src/precis/x.py\n"
    seams["changed_paths"] = [_ENGINE_PATH]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = [_ENGINE_PATH]
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
    _relaxed_deny(monkeypatch)
    seams["diff_names"] = "src/precis/x.py\n"
    seams["changed_paths"] = [_ENGINE_PATH]
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    pctx.meta["engine_paths"] = [_ENGINE_PATH]
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


def test_poll_checked_red_main_red_unreadable_selector_never_runs_selected(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr586292 (Reto 2026-11-12: no land runs FULL; the deploy gate
    does): a selector read that times out counts as FULL. ``scripts/test
    --selected`` re-reads the selector without a timeout and runs the
    whole suite on a FULL answer, so the land never launches it; the
    main-red ids are excluded and ``--quick`` lands, as for FULL."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    sel = seams["tree"] / "scripts" / "impacted-tests"
    sel.parent.mkdir(parents=True)
    sel.write_text("#!/bin/sh\n", encoding="utf-8")
    timed_out: list[str] = []
    real_run = subprocess.run

    def slow_selector(argv: Any, *args: Any, **kw: Any) -> Any:
        if argv and str(argv[0]) == str(sel):
            timed_out.append(str(argv[0]))
            raise subprocess.TimeoutExpired(argv, 120)
        return real_run(argv, *args, **kw)

    monkeypatch.setattr(land_slice.subprocess, "run", slow_selector)
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False
    assert timed_out
    assert all("--selected" not in call[0] for call in seams["launch"])
    assert all("--full" not in call[0] for call in seams["launch"])
    argv, _kw = seams["launch"][-1]
    assert argv[1] == "--quick" and argv[2] == SUBJECT
    assert handle["flag"] == "--quick" and "verify" not in handle
    [note] = store.comments(42)
    assert note.startswith("handoff:") and OUTSIDE_ID in note and "excluded" in note
    events = [t for k, t in pctx.chunks if k == "job_event"]
    assert any("unreadable" in e and "deploy gate" in e for e in events)


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
    assert any(n.startswith("handoff: lander: land FAILED on slice/42:") for n in notes)


def test_poll_checked_red_main_red_deselect_note_failure_is_ordinary(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td602837: same guard on the deselect path — a green minus-ids
    gate does not earn ``--quick`` when the exclusion note could not be
    written; the ordinary red burns the attempt."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    monkeypatch.setattr(land_slice, "_checked_selection_full", lambda tree: False)
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


def test_poll_checked_red_verify_launch_refused_is_attempt_free(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verify launch refused for capacity defers attempt-free
    (``red_class = "admission"``): the tick re-mints the land when the
    host frees — capacity is the host's moment, never the branch's red
    (review td702053)."""

    def boom(*a: Any, **_kw: Any) -> int:
        raise run_slice.admission.Refused("host cannot fit the verify footprint")

    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    monkeypatch.setattr(land_slice, "_launch", boom)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert "verify" not in handle
    assert pctx.meta_set["attempt_free"] == "infra-land"
    assert pctx.meta_set["red_class"] == "admission"
    assert "deferred" in (pctx.failure or "")
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))


def test_main_verify_launch_refused_is_attempt_free(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The main probe's launch refused for capacity drops the throwaway
    worktree and defers attempt-free — never the ordinary red."""
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False  # alone probe launched
    real_launch = land_slice._launch

    def refuse_main(argv: list[str], **kw: Any) -> int:
        if Path(str(kw.get("cwd") or "")).name == "slice-main-td42":
            raise run_slice.admission.Refused("host cannot fit the verify footprint")
        return real_launch(argv, **kw)

    monkeypatch.setattr(land_slice, "_launch", refuse_main)
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set["attempt_free"] == "infra-land"
    assert pctx.meta_set["red_class"] == "admission"
    assert "verify" not in handle
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))
    main_tree = _main_tree(seams)
    assert ["worktree", "remove", "--force", str(main_tree)] in [
        a for a, _ in seams["git"]
    ]
    assert pctx.meta_set.get("main_probe") is None


def test_deselect_verify_launch_refused_is_attempt_free(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deselect gate's launch refused for capacity still drops the
    finished main probe's tree and defers attempt-free — the --quick
    relaunch waits for the host, not a rebuild."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    monkeypatch.setattr(land_slice, "_checked_selection_full", lambda tree: False)
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = _checked_submit(store, seams)
    _finish(handle, 1, CHECKED_RED_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is False  # alone probe launched
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is False  # main probe launched
    real_launch = land_slice._launch

    def refuse_deselect(argv: list[str], **kw: Any) -> int:
        if "--deselect" in argv:
            raise run_slice.admission.Refused("host cannot fit the verify footprint")
        return real_launch(argv, **kw)

    monkeypatch.setattr(land_slice, "_launch", refuse_deselect)
    _verify_finish(handle, 1)
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set["attempt_free"] == "infra-land"
    assert pctx.meta_set["red_class"] == "admission"
    assert "verify" not in handle
    assert not any(t.startswith("attempt:") for t in store.tag_strings(42))
    main_tree = _main_tree(seams)
    assert ["worktree", "remove", "--force", str(main_tree)] in [
        a for a, _ in seams["git"]
    ]
    assert pctx.meta_set.get("main_probe") is None


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
        "params": {"todo_id": 42, "target_node": "spruce"},
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
    # the clone's drop would take the ship log with it: leave the folder
    monkeypatch.setattr(land_slice._slice_ws, "drop", lambda tree, *, notes=None: True)
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


# --- gr572654: Darwin launch identity (ps_identity) ---------------------


def test_launch_ship_mints_ps_identity_on_darwin(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Darwin launch records ``ps_identity`` (start time + user) on the
    ship handle — ssh_node's identity gate verifies it through ``ps``, so
    a cancel can actually signal the ship instead of "cancelled without
    signalling"."""
    monkeypatch.setattr(land_slice.sys, "platform", "darwin")
    monkeypatch.setattr(land_slice._rs, "_process_identity", lambda pid: None)
    monkeypatch.setattr(
        land_slice._rs,
        "_darwin_process_identity",
        lambda pid: "Mon Nov 2 09:07:00 2026 deploy",
    )
    store = _Store()
    store.add_todo(42)

    _ctx, handle = _submit(store, seams)

    assert handle["ps_identity"] == "Mon Nov 2 09:07:00 2026 deploy"


def test_launch_ship_has_no_ps_identity_off_darwin(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Darwin the handle keeps the /proc pair only — the ps read is
    the fallback, never a second identity."""
    monkeypatch.setattr(land_slice.sys, "platform", "linux")
    monkeypatch.setattr(land_slice._rs, "_process_identity", lambda pid: ("boot", 1))
    monkeypatch.setattr(
        land_slice._rs,
        "_darwin_process_identity",
        lambda pid: pytest.fail("ps read off Darwin"),
    )
    store = _Store()
    store.add_todo(42)

    _ctx, handle = _submit(store, seams)

    assert "ps_identity" not in handle
    assert handle["boot_id"] == "boot"


def test_promote_verify_carries_ps_identity() -> None:
    """The promoted probe's identity — ``ps_identity`` included — replaces
    the dead ship's at the handle's top level, so the executor's gates
    validate the LIVE phase; a probe without it clears the stale record."""
    handle = {"pid": 1, "pgid": 1, "ps_identity": "ship-identity"}
    verify = {"pid": 2, "pgid": 2, "ps_identity": "probe-identity"}
    land_slice._promote_verify(handle, verify)
    assert handle["pgid"] == 2
    assert handle["ps_identity"] == "probe-identity"
    handle2 = {"pid": 1, "pgid": 1, "ps_identity": "ship-identity"}
    land_slice._promote_verify(handle2, {"pid": 3, "pgid": 3})
    assert "ps_identity" not in handle2


def test_defer_to_main_drops_ps_identity(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked handle names no process — the dead ship's whole identity,
    ``ps_identity`` included, is dropped so no gate acts on it."""
    monkeypatch.setattr(land_slice.sys, "platform", "darwin")
    exit_file = seams["tree"] / "scratch" / "land_slice.exit"
    exit_file.write_text("1\n", encoding="utf-8")
    handle = {
        "pid": 1,
        "pgid": 1,
        "ps_identity": "ship-identity",
        "worktree": "/t",
        "exit_file": str(exit_file),
    }
    ctx = _Ctx(_Store(), _params(seams))

    assert land_slice._defer_to_main(ctx, handle, "cross", 1, probe="diag") is False

    assert "ps_identity" not in handle
    assert handle["not_before"]


# ── gr650484: one ship per worktree, index.lock, already-landed ──────


def _job_summary(ctx: _Ctx) -> str:
    return "\n".join(t for k, t in ctx.chunks if k == "job_summary")


def _job_events(ctx: _Ctx) -> str:
    return "\n".join(t for k, t in ctx.chunks if k == "job_event")


def _land_lock(seams: dict[str, Any], todo: int = 42) -> tuple[Path, str]:
    """Where a land's own clone takes its lock (gr741695): the host
    checkout's git common dir (the hub's parent), one file per todo."""
    root = Path(seams["tree"]).parents[2]
    return root / ".git", land_slice.LAND_LOCK_TODO_FILE.format(todo=todo)


def test_foreign_ship_lockdir_holder_defers_retryable(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """gr650484: a live holder in ship's own worktree lockdir — a ship
    launched without our flock (a hand run, or a pre-stamp land) names
    no slice, so it cannot be adopted: the job defers attempt-free,
    never succeeded-without-record, and exits before any git read that
    could collide with the running ship's index.lock (two live ships on
    slice-td649847 was the incident)."""
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    gitdir = Path(seams["gitdir"])
    lockdir = gitdir / land_slice.SHIP_LOCK_DIR
    lockdir.mkdir()
    (lockdir / "holder").write_text(
        f"{seams['tree']} pid=5150 host=test\n", encoding="utf-8"
    )

    ctx, handle = _submit(store, seams)

    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == [] and seams["probed"] == [] and seams["swept"] == []
    assert "ship busy" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert ctx.meta_set["red_class"] == "ship-busy"
    assert "attempt:1" not in store.tag_strings(42)
    # Only the gitdir rev-parse ran — no branch-subject or changed-path
    # read could bump the running ship's index.lock.
    assert seams["git"] == [(["rev-parse", "--absolute-git-dir"], seams["tree"])]


def test_foreign_slice_land_lock_defers_retryable(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """gr650484: a live pid holds ``<gitdir>/precis-land.lock`` behind a
    DIFFERENT slice's stamp — not adoptable, so the job defers
    attempt-free; a green exit could leave no land record anywhere."""
    held, fd, _detail = land_slice._try_land_lock(*_land_lock(seams))
    assert not held and fd is not None
    land_slice._stamp_lock_holder(fd, 5150, 99, "slice/99", "feat(x): w (td99)")
    try:
        store = _Store()
        store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
        ctx, handle = _submit(store, seams)
    finally:
        os.close(fd)
    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == []
    assert "held by a live pid 5150" in (ctx.failure or "")
    assert "td=99" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert ctx.meta_set["red_class"] == "ship-busy"


def test_ship_launch_refused_is_attempt_free(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that cannot fit the land gate's footprint defers attempt-free
    (``red_class = "admission"``): the tick re-mints the land when capacity
    returns — a capacity refusal never sends a clean branch for rebuild
    (review td702053)."""

    def boom(*_a: Any, **_kw: Any) -> int:
        raise run_slice.admission.Refused("host cannot fit the gate footprint")

    monkeypatch.setattr(land_slice, "_launch", boom)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx, handle = _submit(store, seams)
    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == []
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert ctx.meta_set["red_class"] == "admission"
    assert "deferred" in (ctx.failure or "")


def test_own_ship_stamp_is_adopted_not_relaunched(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """gr650484: the land flock is held by a live pid behind THIS
    slice's stamp — the earlier land's ship is still running, so this
    job adopts it (polls its exit file, lands the record) instead of
    exiting clean or launching a second ship."""
    held, fd, _detail = land_slice._try_land_lock(*_land_lock(seams))
    assert not held and fd is not None
    land_slice._stamp_lock_holder(
        fd, 5150, 42, "slice/42", SUBJECT, tree=str(seams["tree"])
    )
    try:
        store = _Store()
        store.add_todo(42)
        ctx, handle = _submit(store, seams)
    finally:
        os.close(fd)
    assert handle is not None and seams["launch"] == []
    assert handle["pid"] == 5150 and handle["adopted"] is True
    assert handle["worktree"] == str(seams["tree"])
    assert handle["subject"] == SUBJECT
    assert ctx.meta_set["land_subject"] == SUBJECT
    assert "adopted the already-running ship pid 5150" in _job_events(ctx)


def test_second_submit_in_the_prelaunch_window_never_adopts_the_worker(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td652194: between taking the land flock and launching ship (fetch,
    index.lock wait, stack sweep, drift preflight) no ship pid exists, so
    the stamp names none — a second submit for the same slice in that
    window defers attempt-free as an orphan instead of adopting the
    worker's own pid, and a kill on a handle naming the worker never
    signals the worker's process group."""
    sent = _signals(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    second: dict[str, Any] = {}

    def racing_preflight(tree: Path, **_kw: Any) -> dict[str, Any]:
        seams["probed"].append(tree)
        ctx2 = _Ctx(store, _params(seams))
        second["ctx"], second["handle"] = ctx2, land_slice.submit(ctx2, None)
        return dict(seams["preflight"])

    monkeypatch.setattr(land_slice, "_drift_preflight", racing_preflight)

    _ctx, handle = _submit(store, seams)

    assert handle is not None and handle["pid"] == 5151
    assert len(seams["launch"]) == 1
    ctx2 = second["ctx"]
    assert second["handle"] is None and ctx2.status == "failed"
    assert "orphan" in (ctx2.failure or "")
    assert ctx2.meta_set["attempt_free"] == "infra-land"
    assert ctx2.meta_set["red_class"] == "ship-busy"
    # Once ship is up the stamp names its pid, not the worker's.
    lock_dir, lock_name = _land_lock(seams)
    stamp = (lock_dir / lock_name).read_text(encoding="utf-8")
    assert f"pid={os.getpid()} " not in stamp and "pid=5151" in stamp
    for own in (
        {"pid": os.getpid(), "pgid": os.getpgrp()},
        {"pid": os.getpid(), "pgid": os.getpid()},
    ):
        land_slice.kill(ctx2, {**own, "worktree": str(seams["tree"])})
    assert sent == []


def test_adopted_checked_ship_full_refusal_relaunches_quick(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td652194: the adopted handle keeps the flag the original submit
    picked — it rides the lock stamp — so an adopted ``--checked`` ship
    the selector refuses with FULL relaunches ``--quick`` like an owned
    one, instead of a flagless handle's default reading ``--quick`` and
    turning the lane refusal into a red land."""
    monkeypatch.delenv("PRECIS_LAND_FULL_FALLBACK", raising=False)
    held, fd, _detail = land_slice._try_land_lock(*_land_lock(seams))
    assert not held and fd is not None
    os.write(
        fd,
        f"pid=5150 td=42 branch=slice/42 flag=--checked subject={SUBJECT}\n".encode(),
    )
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    params = {**_params(seams), "checked": True}
    try:
        handle = land_slice.submit(_Ctx(store, params), None)
    finally:
        os.close(fd)
    assert handle is not None and handle["adopted"] is True
    assert handle["flag"] == "--checked" and seams["launch"] == []
    _finish(handle, 1, FULL_REFUSAL_LOG)
    pctx = _Ctx(store, params)
    assert land_slice.poll(pctx, handle) is False
    assert pctx.status is None and pctx.failure is None
    [(argv, _kw)] = seams["launch"]
    assert argv[1] == "--quick" and argv[2] == SUBJECT
    assert handle["fell_back_full"] is True


def test_lock_stamp_keys_are_read_ahead_of_the_subject() -> None:
    """The subject runs to the line's end: a subject quoting ``flag=`` or
    ``pid=`` never poses as the stamp's own field."""
    stamp = land_slice._lock_holder_stamp(
        "td=42 branch=slice/42 subject=fix(x): pid=7 flag=--checked (td42)"
    )
    assert "flag" not in stamp and "pid" not in stamp
    assert stamp["subject"] == "fix(x): pid=7 flag=--checked (td42)"


def test_dead_stamp_pid_with_live_holders_is_retryable_orphan(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr650484: the stamped land-lock pid is dead yet the flock is
    still held — orphaned descendants pin the inherited fd — so the job
    fails retryable naming the holder pids lsof reports, never
    "already shipping"."""
    held, fd, _detail = land_slice._try_land_lock(*_land_lock(seams))
    assert not held and fd is not None
    land_slice._stamp_lock_holder(fd, 99999, 42, "slice/42", SUBJECT)
    seams["alive"] = False  # the stamped pid is dead
    try:
        store = _Store()
        store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
        ctx, handle = _submit(store, seams)
    finally:
        os.close(fd)
    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == []
    assert "orphan" in (ctx.failure or "")
    # The message names the holder pids lsof reports (or "no live
    # holder listed" where lsof cannot answer — either way retryable).
    assert "holder pids:" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert ctx.meta_set["red_class"] == "ship-busy"


def test_ship_busy_peek_releases_a_free_lock(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """Submit's peek must not keep the lock: a free flock is released at
    once (``_launch_ship`` makes the same check while acquiring)."""
    gitdir = Path(seams["gitdir"])
    assert land_slice._ship_busy(seams["tree"], 42, "slice/42") is None
    held, fd, _detail = land_slice._try_land_lock(gitdir)
    try:
        assert not held and fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def test_launch_passes_the_land_flock_fd_to_ship(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """The acquired flock rides ``pass_fds`` into the ship process on a
    high fd (>=100, out of shell shuffling range), so the lock is held
    for the run's whole life even if this worker dies — the window a
    retried land_slice used to launch a duplicate in."""
    gitdir = Path(seams["gitdir"])
    store = _Store()
    store.add_todo(42)

    _ctx, handle = _submit(store, seams)

    assert handle is not None and seams["launch"]
    _argv, kw = seams["launch"][0]
    fds = kw.get("pass_fds", ())
    # The flock rides a high fd (>=100, out of shell shuffling range); no
    # credential fd ever does — the mirror's token stayed in the lander's
    # own git calls (gr659929's mirror wiring), and with no token
    # resolvable in the ambient env only the flock ships.
    assert fds and all(isinstance(fd, int) and fd >= 100 for fd in fds)
    # The holder stamp names the launched ship's pid and slice for the
    # next land's adopt-or-defer decision.
    lock_dir, lock_name = _land_lock(seams)
    stamp = (lock_dir / lock_name).read_text(encoding="utf-8")
    assert "pid=5151" in stamp and "td=42" in stamp
    assert f"tree={seams['tree']}" in stamp
    # The parent closed its copies after launch; with no real child
    # keeping it the lock is acquirable again (never a wedging residue).
    held, fd, _detail = land_slice._try_land_lock(lock_dir, lock_name)
    try:
        assert not held
    finally:
        if fd is not None:
            os.close(fd)


def test_stale_index_lock_is_reaped_with_a_log_line(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provably-dead index.lock (old, no live git in the checkout, no
    open handle — the reaper's rule) is removed before the ship and the
    reap is a job_event line (jo650396's squash red)."""
    gitdir = Path(seams["gitdir"])
    lock = gitdir / "index.lock"
    lock.write_bytes(b"")
    old = time.time() - 1200
    os.utime(lock, (old, old))
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda checkout: False)
    monkeypatch.setattr(land_slice, "_held_open", lambda p: False)
    store = _Store()
    store.add_todo(42)

    _ctx2, handle = _submit(store, seams)
    ctx = _ctx2

    assert handle is not None and seams["launch"]
    assert not lock.exists()
    assert "reaped stale index.lock" in _job_events(ctx)
    # gr684320: the land-side reap counts toward the re-formed-lock
    # escalation too — job meta plus a todo comment, like run_slice's.
    reaped = ctx.meta_set["index_lock_reaped"]
    assert reaped["forced"] is False and reaped["why"] == "stale"
    assert any("index.lock reaped" in c for c in store.comments(42))


def test_linked_worktree_primary_index_lock_is_also_reaped(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linked worktree's land clears the PRIMARY checkout's index.lock
    too — ship's main-sync fast-forwards the primary's local main and a
    lock there wedges the land (jo650356's red was the shared index)."""
    gitdir = Path(seams["gitdir"])
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True, exist_ok=True)
    seams["common"] = str(common)
    lock = common / "index.lock"
    lock.write_bytes(b"")
    old = time.time() - 1200
    os.utime(lock, (old, old))
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda checkout: False)
    monkeypatch.setattr(land_slice, "_held_open", lambda p: False)
    store = _Store()
    store.add_todo(42)

    _ctx, handle = _submit(store, seams)

    assert handle is not None and seams["launch"]
    assert not lock.exists()


def test_live_index_lock_waits_then_fails_retryable(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live index.lock is waited on briefly, then the job fails
    attempt-free ``"index-lock"`` VERBATIM (gr684320: a plain wait is
    self-clearing — exempt from the re-land cap and the free-failure
    ask — and the tick re-mints the land like an infra red), never the
    branch-charging red path."""
    gitdir = Path(seams["gitdir"])
    (gitdir / "index.lock").write_bytes(b"")
    monkeypatch.setenv("PRECIS_LAND_INDEX_LOCK_WAIT_S", "0.05")
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda checkout: True)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _submit(store, seams)

    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == []
    assert "index.lock" in (ctx.failure or "")
    assert ctx.meta_set["attempt_free"] == "index-lock"
    assert "red_class" not in ctx.meta_set
    assert "attempt:1" not in store.tag_strings(42)
    assert (gitdir / "index.lock").exists()
    assert "attempt:1" not in store.tag_strings(42)


def test_live_index_lock_clearing_mid_wait_still_ships(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live lock whose writer finishes inside the wait ships normally."""
    gitdir = Path(seams["gitdir"])
    (gitdir / "index.lock").write_bytes(b"")
    states = iter(["live", "live", "absent"])
    monkeypatch.setattr(
        land_slice,
        "_index_lock_state",
        lambda lock, checkout: (next(states, "absent"), None),
    )
    monkeypatch.setattr(land_slice.time, "sleep", lambda s: None)
    store = _Store()
    store.add_todo(42)

    _ctx, handle = _submit(store, seams)

    assert handle is not None and seams["launch"]


def test_index_lock_state_keeps_every_doubt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keep-on-doubt ladder: absent → young → git-in-tree →
    unjudgeable → held-open all keep the lock; only the old, fully
    disproven lock reads stale — and returns the stat it was judged
    on, the identity the unlink must re-match."""
    lock = tmp_path / "index.lock"
    assert land_slice._index_lock_state(lock, tmp_path) == ("absent", None)
    lock.write_bytes(b"")
    state, _judged = land_slice._index_lock_state(lock, tmp_path)
    assert state == "live"  # young
    old = time.time() - (land_slice.INDEX_LOCK_STALE_S + 60)
    os.utime(lock, (old, old))
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda c: True)
    state, _judged = land_slice._index_lock_state(lock, tmp_path)
    assert state == "live"
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda c: None)
    state, _judged = land_slice._index_lock_state(lock, tmp_path)
    assert state == "live"
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda c: False)
    monkeypatch.setattr(land_slice, "_held_open", lambda p: True)
    state, _judged = land_slice._index_lock_state(lock, tmp_path)
    assert state == "live"
    monkeypatch.setattr(land_slice, "_held_open", lambda p: None)
    state, _judged = land_slice._index_lock_state(lock, tmp_path)
    assert state == "live"
    monkeypatch.setattr(land_slice, "_held_open", lambda p: False)
    state, judged = land_slice._index_lock_state(lock, tmp_path)
    assert state == "stale" and judged == lock.stat()


def test_reap_index_lock_removes_only_the_judged_inode(tmp_path: Path) -> None:
    """The re-stat before the unlink is checked against the JUDGED
    identity, not a fresh stat: a lock swapped out between judgement
    and reap is a live writer's and is kept — two fresh stats would
    have matched the replacement and unlinked it. (A recreated file
    can recycle the inode number on some filesystems; the fresh
    mtime_ns is what never matches — the judged file's is minutes
    old.)"""
    ctx = _Ctx(_Store(), {})
    lock = tmp_path / "index.lock"
    lock.write_bytes(b"")
    old = time.time() - (land_slice.INDEX_LOCK_STALE_S + 600)
    os.utime(lock, (old, old))
    judged = lock.stat()
    assert land_slice._reap_index_lock(ctx, lock, judged) is True
    assert not lock.exists()

    # A writer replaced the judged remnant with its own live lock:
    # the re-stat mismatches the judged identity and the unlink never
    # runs — whether the filesystem recycled the inode or not.
    lock.write_bytes(b"live writer's lock")
    assert land_slice._reap_index_lock(ctx, lock, judged) is False
    assert lock.exists() and lock.read_bytes() == b"live writer's lock"
    # Only the first, genuine reap wrote a job_event.
    reaped_events = [
        text
        for kind, text in ctx.chunks
        if kind == "job_event" and "reaped stale index.lock" in text
    ]
    assert len(reaped_events) == 1


def test_reap_index_lock_keeps_a_retouched_judged_lock(tmp_path: Path) -> None:
    """Same inode, but the file moved since judgement (a writer wrote
    to it): mtime_ns no longer matches — kept."""
    ctx = _Ctx(_Store(), {})
    lock = tmp_path / "index.lock"
    lock.write_bytes(b"")
    old = time.time() - (land_slice.INDEX_LOCK_STALE_S + 600)
    os.utime(lock, (old, old))
    judged = lock.stat()
    lock.write_bytes(b"written after judgement")
    assert land_slice._reap_index_lock(ctx, lock, judged) is False
    assert lock.read_bytes() == b"written after judgement"


def test_index_lock_replaced_between_judgement_and_unlink_is_kept(
    seams: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through ``_clear_index_locks``: a live writer that
    swaps in its own ``index.lock`` after the stale verdict keeps it —
    the judged inode/mtime rides to the unlink, the replacement
    mismatches, and the land waits attempt-free instead of removing a
    live lock."""
    gitdir = Path(seams["gitdir"])
    lock = gitdir / "index.lock"
    lock.write_bytes(b"")
    old = time.time() - (land_slice.INDEX_LOCK_STALE_S + 600)
    os.utime(lock, (old, old))
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda checkout: False)
    monkeypatch.setattr(land_slice, "_held_open", lambda p: False)
    monkeypatch.setenv("PRECIS_LAND_INDEX_LOCK_WAIT_S", "0.05")
    monkeypatch.setattr(land_slice.time, "sleep", lambda s: None)
    real_state = land_slice._index_lock_state
    swapped = False

    def state_then_replace(
        lock: Path, checkout: Path
    ) -> tuple[str, os.stat_result | None]:
        nonlocal swapped
        verdict = real_state(lock, checkout)
        if verdict[0] == "stale" and not swapped:
            swapped = True
            # The writer raced in after judgement: the old remnant is
            # gone and its own live lock sits at the path.
            lock.unlink()
            lock.write_bytes(b"live writer's lock")
        return verdict

    monkeypatch.setattr(land_slice, "_index_lock_state", state_then_replace)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _submit(store, seams)

    assert swapped  # the race actually fired
    assert handle is None and ctx.status == "failed"
    assert seams["launch"] == []
    assert lock.exists() and lock.read_bytes() == b"live writer's lock"
    assert ctx.meta_set["attempt_free"] == "index-lock"
    assert "red_class" not in ctx.meta_set
    assert "attempt:1" not in store.tag_strings(42)


def test_already_on_main_identical_tree_finishes_green_without_ship(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """gr650484: the branch's tree already equals origin/main's — the
    land writes the nothing-new record, cleans up, and never launches."""
    seams["diff_rc"] = 0
    store = _Store()
    store.add_todo(42)

    ctx, handle = _submit(store, seams)

    assert handle is None and ctx.status == "succeeded"
    assert seams["launch"] == [] and seams["swept"] == [] and seams["probed"] == []
    assert ctx.meta_set["ship_status"] == "already-landed"
    assert ctx.meta_set.get("landed_sha") is None
    assert any("landed: nothing" in c for c in store.comments(42))
    assert _ws_names(seams) == ["drop", "delete_builds"]


def test_already_on_main_subject_match_finishes_with_sha(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """gr650484: the squash subject is already a commit on origin/main
    AND the slice's changed paths are identical there — the land closes
    with that sha; td649946's second ship never runs."""
    sha = "f" * 40
    seams["main_log"] = f"{sha}\x00{SUBJECT}\n{'a' * 40}\x00unrelated (gr7)\n"
    seams["diff_names"] = "src/precis/x.py\n"
    store = _Store()
    store.add_todo(42)

    ctx, handle = _submit(store, seams)

    assert handle is None and ctx.status == "succeeded"
    assert seams["launch"] == []
    assert ctx.meta_set["landed_sha"] == sha
    assert any(f"landed: {sha[:9]}" in c for c in store.comments(42))
    assert "STATUS:done" in store.tag_strings(42)


def test_same_subject_same_handle_different_content_still_ships(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """A main commit carrying the same ``(td<id>)`` and the same subject
    but DIFFERENT content is an earlier land of the same todo (td572037,
    td572552 and td626856 each landed twice) — not this one: without the
    content proof the land must ship, not close and reap unshipped
    work."""
    sha = "e" * 40
    seams["main_log"] = f"{sha}\x00{SUBJECT}\n"
    seams["diff_names"] = "src/precis/x.py\n"
    seams["path_diff_rc"] = 1  # the slice's paths differ from main's
    store = _Store()
    store.add_todo(42)

    ctx, handle = _submit(store, seams)

    assert handle is not None and seams["launch"]
    assert ctx.status != "succeeded"
    assert ctx.meta_set.get("landed_sha") is None
    assert "STATUS:done" not in store.tag_strings(42)


def test_neighbouring_td_handles_do_not_cross_match(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """td4, td42 and td423 are distinct lands: a main commit's subject
    must equal this land's squash subject byte for byte — a handle
    substring never satisfies it."""
    seams["main_log"] = (
        f"{'c' * 40}\x00feat(x): widget (td4)\n{'d' * 40}\x00feat(x): widget (td423)\n"
    )
    store = _Store()
    store.add_todo(42)

    _ctx, handle = _submit(store, seams)

    assert handle is not None and seams["launch"]


def test_already_landed_check_is_a_fast_path_not_a_gate(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """A fetch that cannot answer only disables the check — ship still
    runs and its own nothing-new guard is the arbiter."""
    seams["fetch_rc"] = 128
    store = _Store()
    store.add_todo(42)

    _ctx, handle = _submit(store, seams)

    assert handle is not None and seams["launch"]


def test_already_landed_no_match_still_ships(
    seams: dict[str, Any], tmp_path: Path
) -> None:
    """A diff with no subject/handle match on main ships as usual."""
    seams["main_log"] = f"{'b' * 40}\x00feat(x): other work (gr9)\n"
    store = _Store()
    store.add_todo(42)

    _ctx, handle = _submit(store, seams)

    assert handle is not None and seams["launch"]


def test_held_open_lsof_no_holders_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lsof's exit 1 with empty output is the ordinary "nothing holds
    this file" — ``False``, not "can't tell" (keep-on-doubt would pin
    every provably-dead lock forever)."""
    lock = tmp_path / "index.lock"
    lock.write_bytes(b"")
    monkeypatch.setattr(land_slice.shutil, "which", lambda cmd: "/usr/sbin/lsof")
    monkeypatch.setattr(
        land_slice.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr=""),
    )
    assert land_slice._held_open(lock) is False


def test_held_open_lsof_stderr_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any lsof stderr means the open-handle question went unanswered —
    ``None`` keeps the lock on doubt."""
    lock = tmp_path / "index.lock"
    lock.write_bytes(b"")
    monkeypatch.setattr(land_slice.shutil, "which", lambda cmd: "/usr/sbin/lsof")
    monkeypatch.setattr(
        land_slice.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="lsof: can't stat"
        ),
    )
    assert land_slice._held_open(lock) is None


def test_git_in_tree_unparseable_ps_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ps listing that parses to zero ``pid comm`` rows never answered
    the question — ``None`` keeps the lock; only a parsed scan with no
    git in the checkout disproves the writer."""
    monkeypatch.setattr(
        land_slice.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            [], 0, stdout="garbage\n%%%\n", stderr=""
        ),
    )
    assert land_slice._git_in_tree(tmp_path) is None


def _old_lock(tmp_path: Path) -> Path:
    lock = tmp_path / "index.lock"
    lock.write_bytes(b"")
    old = time.time() - (land_slice.INDEX_LOCK_STALE_S + 600)
    os.utime(lock, (old, old))
    return lock


def _fake_lsof(tmp_path: Path, body: str) -> Path:
    bin_ = tmp_path / "fakebin" / "lsof"
    bin_.parent.mkdir()
    bin_.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    bin_.chmod(0o755)
    return bin_


def test_lsof_missing_from_path_uses_the_system_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr684320: a worker PATH without /usr/sbin still finds lsof, so an
    old 0-byte lock nobody holds is judged stale."""
    lock = _old_lock(tmp_path)
    fake = _fake_lsof(tmp_path, "exit 1")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(land_slice, "_LSOF_FALLBACKS", (str(fake),))
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda c: False)
    state, _j, why, _o = land_slice._index_lock_judge(lock, tmp_path)
    assert (state, why) == ("stale", "")


def test_no_lsof_anywhere_keeps_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _old_lock(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(land_slice, "_LSOF_FALLBACKS", (str(tmp_path / "none"),))
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda c: False)
    state, _j, why, _o = land_slice._index_lock_judge(lock, tmp_path)
    assert (state, why) == ("live", "lsof-missing")


def test_non_git_holder_does_not_keep_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Colima VM's file share holds the handle: recorded, not obeyed."""
    lock = _old_lock(tmp_path)
    fake = _fake_lsof(tmp_path, "printf 'p4242\\ncVirtualMachine\\n'")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(land_slice, "_LSOF_FALLBACKS", (str(fake),))
    monkeypatch.setattr(land_slice, "_git_in_tree", lambda c: False)
    state, _j, why, other = land_slice._index_lock_judge(lock, tmp_path)
    assert (state, why) == ("stale", "")
    assert other == [("4242", "VirtualMachine")]
    assert land_slice._index_lock_state(lock, tmp_path)[0] == "stale"


# ── skill-editor follow-up mint (gr657065) ────────────────────────────


class _MintStore:
    def __init__(self, marker: str) -> None:
        self.meta: dict[str, Any] = {}
        self.minted: list[dict[str, Any]] = []
        self.bodies: list[Any] = []
        self.chunks = SimpleNamespace(
            list_chunks_for_ref=lambda _: [
                SimpleNamespace(
                    text="handoff: Built.\n" + marker, chunk_kind="todo_comment"
                )
            ],
            insert_chunks=lambda ref_id, chunks, **kw: self.bodies.extend(chunks),
        )

    @contextmanager
    def tx(self) -> Iterator[_MintStore]:
        yield self

    def execute(self, sql: str, args: tuple[Any, ...]) -> _MintStore:
        assert "FOR UPDATE" in sql
        return self

    def fetchone(self) -> tuple[dict[str, Any]]:
        return (self.meta,)

    def insert_ref(self, **kw: Any) -> SimpleNamespace:
        self.minted.append(kw)
        return SimpleNamespace(id=99)

    def add_tag(self, *args: Any, **kw: Any) -> None:
        pass

    def tags_for(self, ref_id: int) -> list[Any]:
        return []

    def stamp_ref_meta(self, ref_id: int, updates: dict[str, Any], **kw: Any) -> None:
        self.meta.update(updates)


def test_land_mints_skill_edit_job(monkeypatch: pytest.MonkeyPatch) -> None:
    finished: list[int] = []
    monkeypatch.setattr(nodes, "finish_mint", lambda s, tid, **kw: finished.append(tid))
    store = _MintStore("skills: precis-get-help, precis-put-help")
    sha = "a" * 40
    assert "minted td99" in land_slice._mint_skill_edit(store, 42, sha)
    assert "already minted" in land_slice._mint_skill_edit(store, 42, sha)
    assert len(store.minted) == 1 and finished == [99]
    job = store.minted[0]
    assert job["parent_id"] == 42 and job["meta"]["task_class"] == "skill"
    assert job["meta"]["skill_ids"] == ["precis-get-help", "precis-put-help"]
    assert sha in store.bodies[0].text
    for marker, note in [("skills: none", "skills: none"), ("", "skills: not stated")]:
        empty = _MintStore(marker)
        assert land_slice._mint_skill_edit(empty, 42, sha) == note
        assert empty.minted == []


def test_reconcile_completed_ship_resumes_mirror_publication(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen = _patch_sync_seams(monkeypatch)
    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path / "ops"))
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx = _Ctx(store, _params(seams))
    ctx.meta.update(
        submit_attempt="a" * 32,
        submit_host=run_slice.admission.local_host(),
        submit_started_at=1.0,
    )
    tips = {"main": "f" * 40, "prod": "0" * 40}
    handle = land_slice.submit(ctx, None)
    assert handle is not None
    attempt = run_slice.run_dir(42) / ("a" * 32)
    assert Path(handle["exit_file"]) == attempt / "exit"

    # Simulate death before even the full handle record. The completed
    # wrapper left its PID and exit code, and the mirror is awaiting poll.
    (attempt / "handle").unlink()
    baseline = json.loads((attempt / "baseline").read_text(encoding="utf-8"))
    run_slice._write_launch_record(
        attempt / "baseline", {**baseline, "factory_mirror_tips": tips}
    )
    (attempt / "pid").write_text(str(handle["pid"]), encoding="utf-8")
    _finish(handle, 0, GREEN_LOG)
    assert land_slice._ship_busy(Path(seams["tree"]), 42, "slice/42") is None

    verdict = land_slice.reconcile(ctx)

    assert isinstance(verdict, Reconcile)
    assert land_slice.poll(ctx, verdict.handle) is True
    assert ctx.status == "succeeded"
    [(_mirror, _cache, _url, got_tips, _expect)] = seen["sync"]
    assert got_tips == tips
    assert len(seams["launch"]) == 1


def test_reconcile_free_lock_without_current_evidence_is_unknown(
    seams: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path / "ops"))
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, _params(seams))
    ctx.meta.update(
        submit_attempt="a" * 32,
        submit_host=run_slice.admission.local_host(),
        submit_started_at=1.0,
    )

    assert land_slice._ship_busy(Path(seams["tree"]), 42, "slice/42") is None
    assert land_slice.reconcile(ctx) is None
    assert seams["launch"] == []


# ── a tip that is a wip snapshot is never landed unnamed ───────────


def test_head_below_a_wip_tip_still_lands_head(seams: dict[str, Any]) -> None:
    """``head`` is named: the wip snapshot above it is not what lands."""
    _cut_seams(seams, tip=SNAPSHOT)
    seams["wip"].add(SNAPSHOT)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))

    ctx, handle = _cut_submit(store, seams)

    assert ctx.failure is None and handle is not None
    assert seams["checkout"][-1] == HEAD


def test_no_head_and_a_wip_tip_is_refused_attempt_free(seams: dict[str, Any]) -> None:
    """Nothing names the commit to land and the ref's newest commit is an
    unfinished snapshot (it can carry stashes and other branches as extra
    parents — content no reviewer saw): refused, nothing cut or landed."""
    _no_folder(seams)
    seams["hub_tips"][REF] = SNAPSHOT
    seams["wip"].add(SNAPSHOT)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx = _Ctx(store, {**_params(seams), "ref": REF})

    assert land_slice.submit(ctx, None) is None

    assert ctx.failure is not None
    assert "newest work of td42" in ctx.failure
    assert "unfinished snapshot; next: let a build finish it" in ctx.failure
    assert ctx.meta_set["attempt_free"] == "infra-land"
    assert "attempt:1" not in store.tag_strings(42)
    assert "cut" not in _ws_names(seams) and seams["launch"] == []
    assert not seams["tree"].exists()


def test_no_head_and_a_finished_tip_lands_the_tip(seams: dict[str, Any]) -> None:
    _no_folder(seams)
    seams["hub_tips"][REF] = SNAPSHOT  # not a wip commit here
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    ctx = _Ctx(store, {**_params(seams), "ref": REF})

    handle = land_slice.submit(ctx, None)

    assert ctx.failure is None and handle is not None
    [cut] = _ws_args(seams, "cut")
    assert cut["start"].sha == SNAPSHOT
    assert ctx.meta_set[land_slice.LAND_START_KEY]["head"] == SNAPSHOT


# -- todo-build plan: the source todo (td-dispatch U4) ----------------------


def _source_plan(store: _Store, *, source: int | None = 50) -> None:
    """Root td40 (``meta.source_todo`` = ``source``, none for a gripe plan),
    the build td42 and its pending ``sha-in-prod`` gate td44."""
    meta = {} if source is None else {"source_todo": source}
    store.add_todo(40, status="open", tags=("plan:40",), meta=meta)
    store.add_todo(42, tags=("claimed-by:lane-claude-sub", "plan:40"))
    store.add_todo(
        44,
        status="open",
        tags=("plan:40",),
        meta={"gate": {"kind": "sha-in-prod", "args": {"sha": PENDING_SHA}}},
    )
    if source is not None:
        store.add_todo(source, status="open")


def _poll_green(store: _Store, seams: dict[str, Any]) -> tuple[_Ctx, str]:
    seams["gates"] = [(44, True)]
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None, ctx.failure
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded", pctx.failure
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    return pctx, summary


def test_source_gets_landed_comment_stays_open(seams: dict[str, Any]) -> None:
    """The land that fills a todo-build plan's gate leaves ONE ``landed:``
    comment on the source todo, naming the commit, its subject and the build
    node, and leaves its STATUS alone. If this fails the source is closed at
    the land (before the deploy) or never told it landed."""
    store = _Store()
    _source_plan(store)
    _pctx, summary = _poll_green(store, seams)
    assert store.comments(50) == [f"landed: {FULL_SHA[:9]} — {SUBJECT} (built by td42)"]
    assert store.tag_strings(50) == {"STATUS:open"}
    assert "source td50: landed comment written, left open" in summary
    # The build node itself closes as ever, and the gate holds the sha.
    assert "STATUS:done" in store.tag_strings(42)
    assert store.refs[44].meta["gate"]["args"]["sha"] == FULL_SHA


def test_subject_naming_source_does_not_close_it(seams: dict[str, Any]) -> None:
    """A subject that names the source todo next to the build node must not
    close it at the land, with or without a factory token: ship's land_notify
    is held (it would close the handle) and the lander's own handle walk skips
    the source. Other handles behave as ever. If this fails the source is
    done before its commit is in prod."""
    store = _Store()
    _source_plan(store)
    store.add_todo(43)
    seams["subjects"] = ["feat(x): widget (td42, td50, td43)"]
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None, ctx.failure
    [(_argv, kw)] = seams["launch"]
    assert kw["env"]["PRECIS_LAND_NOTIFY"] == "0"
    assert kw["env"]["PRECIS_GRIPE_NOTE"] == "0"
    _finish(handle, 0, GREEN_LOG)
    seams["gates"] = [(44, True)]
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded", pctx.failure
    assert store.tag_strings(50) == {"STATUS:open"}
    [landed] = store.comments(50)
    assert landed.startswith(f"landed: {FULL_SHA[:9]} — ")
    assert "STATUS:done" in store.tag_strings(43)
    assert store.comments(43)[-1].startswith(f"landed: {FULL_SHA[:9]}")
    [summary] = [t for k, t in pctx.chunks if k == "job_summary"]
    assert "todo td50: left open (the plan's source todo)" in summary


def test_landed_comment_once_per_sha() -> None:
    """A retried land tail (or a second land on the plan under the same sha)
    writes no second ``landed:`` comment on the source; a different sha is a
    different fact."""
    store = _Store()
    _source_plan(store)
    sha = FULL_SHA
    first = land_slice._note_source_landed(store, 50, sha, SUBJECT, 42)
    again = land_slice._note_source_landed(store, 50, sha, SUBJECT, 42)
    assert "written" in first and "already posted" in again
    assert len(store.comments(50)) == 1
    other = "d" * 40
    land_slice._note_source_landed(store, 50, other, SUBJECT, 42)
    assert [c[:17] for c in store.comments(50)] == [
        f"landed: {sha[:9]}",
        f"landed: {other[:9]}",
    ]


def test_land_without_a_gate_leaves_the_source_alone(seams: dict[str, Any]) -> None:
    """A land that fills no gate (none blocked-by the node) writes nothing on
    the source."""
    store = _Store()
    _source_plan(store)
    seams["gates"] = []
    ctx, handle = _submit(store, seams)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert store.comments(50) == []


def test_gripe_notify_unchanged(seams: dict[str, Any]) -> None:
    """A plan whose root carries no ``meta.source_todo`` lands exactly as
    before (the reference is ``test_poll_green_moves_the_plans_gripe_to_verify``,
    unedited): ship's land_notify is not held, the summary has no source
    line, and a subject handle still closes its todo."""
    store = _Store()
    _source_plan(store, source=None)
    store.refs[7] = _Ref(7, "gripe 7", {})
    store.tags[7] = [Tag.closed("STATUS", "triaged")]
    store.chunks.rows[7] = []
    seams["gripe"] = 7
    store.add_todo(55)
    _pctx, summary = _poll_green(store, seams)
    [(_argv, kw)] = seams["launch"]
    assert "PRECIS_LAND_NOTIFY" not in kw["env"]
    assert "source" not in summary
    assert "gripe gr7: resolved comment, STATUS:verify written" in summary
    assert land_slice._holds_notify(store, 42) is False
    assert land_slice._plan_source_todo(store, "plan:40") is None
    notes = land_slice._deferred_notify(
        store, "fix (td42, td55)", "c" * 40, 42, seams["tree"]
    )
    assert "STATUS:done" in store.tag_strings(55)
    assert notes == ["todo td55: landed comment, STATUS:done written"]


def test_source_checks_never_fail_the_land(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that cannot name the plan's source fails closed: ship's
    notifier is held (launch and poll agree), no todo handle of the subject
    is closed (the source could be one), and the land still completes — the
    source note's failure is reported, not raised."""
    store = _Store()
    _source_plan(store)
    store.add_todo(43)

    def boom(_store: Any, _plan_tag: str) -> int | None:
        raise RuntimeError("db down")

    monkeypatch.setattr(land_slice, "_plan_source_todo", boom)
    assert land_slice._holds_notify(store, 42) is True
    seams["subjects"] = ["feat(x): widget (td42, td50, td43)"]
    _pctx, summary = _poll_green(store, seams)
    [(_argv, kw)] = seams["launch"]
    assert kw["env"]["PRECIS_LAND_NOTIFY"] == "0"
    assert "gate: write FAILED (db down)" in summary
    assert "todo td50: left open (plan source unreadable)" in summary
    assert "todo td43: left open (plan source unreadable)" in summary
    assert store.comments(50) == [] and store.comments(43) == []
    assert store.tag_strings(50) == {"STATUS:open"}
    assert "STATUS:done" not in store.tag_strings(43)
    assert "STATUS:done" in store.tag_strings(42)


def test_unreadable_source_leaves_todo_handles_but_not_gripes(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the source unreadable, only todo handles wait; a gripe handle
    still gets its resolved comment as today."""
    store = _Store()
    _source_plan(store)
    store.add_todo(55)
    store.add_todo(7, status="open")

    def boom(_store: Any, _plan_tag: str) -> int | None:
        raise RuntimeError("db down")

    monkeypatch.setattr(land_slice, "_plan_source_todo", boom)
    notes = land_slice._deferred_notify(
        store, "fix (td42, td55, gr7)", "c" * 40, 42, seams["tree"]
    )
    assert "todo td55: left open (plan source unreadable)" in notes
    assert store.comments(55) == [] and "STATUS:done" not in store.tag_strings(55)
    assert any("gripe gr7:" in n and "auto_verify" in n for n in notes)


def test_source_comment_failure_leaves_the_gate_pending(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source's ``landed:`` comment is written before the gate fill: if
    it fails the gate stays pending, so the retry redoes both instead of
    skipping a filled gate and never marking the source."""
    store = _Store()
    _source_plan(store)

    def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("comment down")

    monkeypatch.setattr(land_slice, "_note_source_landed", boom)
    _pctx, summary = _poll_green(store, seams)
    assert "gate: write FAILED (comment down)" in summary
    assert store.refs[44].meta["gate"]["args"]["sha"] == PENDING_SHA
    assert store.comments(50) == []


def test_launch_notify_hold_is_what_poll_uses(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store error at launch holds ship's notifier; poll must defer the
    notify marks on that launch decision even when its own lookup then
    succeeds, or nobody writes them."""
    store = _Store()
    _source_plan(store, source=None)
    store.add_todo(55)
    seams["subjects"] = ["fix (td42, td55)"]
    real = land_slice._holds_notify
    monkeypatch.setattr(land_slice, "_holds_notify", lambda *_a: True)
    ctx, handle = _submit(store, seams)
    assert ctx.failure is None, ctx.failure
    [(_argv, kw)] = seams["launch"]
    assert kw["env"]["PRECIS_LAND_NOTIFY"] == "0"
    monkeypatch.setattr(land_slice, "_holds_notify", real)
    _finish(handle, 0, GREEN_LOG)
    seams["gates"] = [(44, True)]
    pctx = _Ctx(store, _params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert "STATUS:done" in store.tag_strings(55)


def _land_signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Capture ``outcome.record_build_signal`` as ``(job, kind, value)``."""
    from precis.plans import outcome

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        outcome,
        "record_build_signal",
        lambda store, job, kind, value, *, by_job=None: calls.append(
            (job, kind, value)
        ),
    )
    return calls


def _ref_params(seams: dict[str, Any]) -> dict[str, Any]:
    return {**_params(seams), "head": HEAD, "ref": REF}


def test_a_green_land_stamps_landed_on_the_build_job(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr761723 end to end: ship green with a sha — the build job the
    attempt ref names (``refs/factory/build/42/7``) gets ``landed``."""
    calls = _land_signals(monkeypatch)
    _patch_sync_seams(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = land_slice.submit(_Ctx(store, _ref_params(seams)), None)
    _finish(handle, 0, GREEN_LOG)
    pctx = _Ctx(store, _ref_params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "succeeded"
    assert calls == [(7, "land", "landed")]


def test_a_red_on_the_diff_stamps_red_on_the_build_job(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _land_signals(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = land_slice.submit(_Ctx(store, _ref_params(seams)), None)
    _finish(handle, 1, "x\n\x1b[31m✖ ruff: 1 error\x1b[0m\n")
    pctx = _Ctx(store, _ref_params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed" and "attempt:1" in store.tag_strings(42)
    assert calls == [(7, "land", "red")]


def test_a_conflict_red_stamps_no_land_signal(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _land_signals(monkeypatch)
    _conflict_git(seams, monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = land_slice.submit(_Ctx(store, _ref_params(seams)), None)
    _finish(handle, 1, REBASE_CONFLICT_LOG)
    pctx = _Ctx(store, _ref_params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert calls == []


def test_an_infra_red_stamps_no_land_signal(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate kill past its retries fails the land attempt-free as gate
    infrastructure — not the build's red, so no signal."""
    calls = _land_signals(monkeypatch)
    store = _Store()
    store.add_todo(42, tags=("claimed-by:lane-claude-sub",))
    handle = land_slice.submit(_Ctx(store, _ref_params(seams)), None)
    handle["gate_kills"] = land_slice.MAX_GATE_KILL_RETRIES
    _finish(handle, 137, "pytest -n3 --impacted\nKilled\n")
    pctx = _Ctx(store, _ref_params(seams))
    assert land_slice.poll(pctx, handle) is True
    assert pctx.status == "failed"
    assert pctx.meta_set["attempt_free"] == "infra-land"
    assert calls == []


def test_land_signal_lands_on_the_build_job_its_ref_names(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """gr761723: the land's result is the validated signal of the build
    job whose attempt ref it shipped; no ref records nothing, and a
    failed write only logs."""
    from precis.plans import outcome

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        outcome,
        "record_build_signal",
        lambda store, job, kind, value, *, by_job=None: calls.append(
            (job, kind, value, by_job)
        ),
    )
    ctx = _Ctx(_Store(), {"todo_id": 42})
    land_slice._stamp_land_signal(
        ctx, {"todo_id": 42, "ref": "refs/factory/build/42/900"}, "landed"
    )
    land_slice._stamp_land_signal(ctx, {"todo_id": 42}, "red")
    assert calls == [(900, "land", "landed", ctx.ref_id)]

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(outcome, "record_build_signal", boom)
    land_slice._stamp_land_signal(
        ctx, {"todo_id": 42, "ref": "refs/factory/build/42/901"}, "red"
    )
    assert "land signal on job 901 failed" in caplog.text


def test_submit_refuses_a_host_lacking_the_gate_on_the_first_attempt(
    seams: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr701559: no premodels/precis-dev image or no uv → the land fails at
    once (a clean unclaim, no defer loop), before any git or ship call."""
    monkeypatch.setattr(
        land_slice._capability_probe, "gate_host_lack", lambda: "image precis-dev"
    )
    store = _Store()
    store.add_todo(42)
    ctx = _Ctx(store, _params(seams))
    assert land_slice.submit(ctx, None) is None
    assert ctx.status == "failed"
    assert ctx.failure is not None and "host lacks image precis-dev" in ctx.failure
    assert seams["git"] == [] and seams["launch"] == []
