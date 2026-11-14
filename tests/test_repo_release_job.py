"""repo_release, the release job (td659522 step 3 R5 and R6).

Built on ``test_repo_land``'s rig: a real "origin" bare repo and clone in tmp,
the credentialed fetch replaced by a local fetch, the detached launch by a
recorder, and the lease by ``FakeLease``. The tag oracle (``ls_remote_tag``),
``push_cas`` and ``push_tag_new`` are faked at the ``factory_git_mirror``
seam; the workflow-runs read and the re-run POST at the module seam
(``repo_release._read_runs`` / ``_rerun``), the plan subtree the batch
backstop walks at ``_plan_rows``. Pyproject and lockfile contents are
synthetic.
"""

from __future__ import annotations

import copy
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from precis.plans import lanes
from precis.plans.pr_class import GitHubUnreadable
from precis.workers import factory_git_mirror, repo_land, repo_lease
from precis.workers.job_types import land_slice, repo_release, run_slice
from tests.test_land_slice import _Ctx, _Store
from tests.test_repo_land import FakeLease, Rig, _g, rig  # noqa: F401

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: the verifier script runs under sh"
)

PYPROJECT = '[project]\nname = "demo"\nversion = "1.2.0"\ndependencies = []\n'
LOCK = (
    "version = 1\n"
    'requires-python = ">=3.10"\n'
    "\n"
    "[[package]]\n"
    'name = "demo"\n'
    'version = "1.2.0"\n'
    'source = { editable = "." }\n'
    "dependencies = []\n"
    "\n"
    "[[package]]\n"
    'name = "other"\n'
    'version = "2.0.0"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
)
PARAMS = {"todo_id": 42, "repo": "catpath"}
TS = "%Y-%m-%dT%H:%M:%SZ"
CHECK_RED_LOG = "lock stale\n✖ gate check failed: uv lock --check\n"
# What the rig puts in the credentialed env: no job output may ever carry it.
TOKEN = "ghp_" + "".join(chr(65 + (i * 5) % 26) for i in range(36))
RUN_URL = "https://github.com/retospect/catpath/actions/runs/500"


class ReleaseLease(FakeLease):
    """``FakeLease`` that also records the order of ``want`` and ``acquire``."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.wants: list[tuple[str, int]] = []

    def want(self, store: Any, repo: str, *, job_id: int) -> None:
        self.events.append("want")
        self.wants.append((repo, job_id))

    def acquire(
        self, store: Any, repo: str, *, job_id: int, kind: str
    ) -> dict[str, Any] | None:
        self.events.append("acquire")
        return super().acquire(store, repo, job_id=job_id, kind=kind)


@dataclass
class Push:
    url: str
    ref: str
    sha: str
    tip: str
    record: dict[str, Any] | None  # the todo's repo_release record at call time


@dataclass
class Rel:
    rig: Rig
    store: _Store
    lease: ReleaseLease
    events: list[str]
    tags: dict[str, str] = field(default_factory=dict)
    unreadable: set[str] = field(default_factory=set)
    oracle_calls: list[str] = field(default_factory=list)
    pushes: list[Push] = field(default_factory=list)
    outcomes: list[tuple[str, str]] = field(default_factory=list)
    on_push: Any = None
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    signals: list[tuple[int, int]] = field(default_factory=list)
    ctx: _Ctx | None = None
    base: str = ""
    # the CI side: the runs a read of the sha returns, the runs of the whole
    # branch (defaults to the same list), a failure to raise instead
    runs: list[dict[str, Any]] = field(default_factory=list)
    branch_runs: list[dict[str, Any]] | None = None
    read_error: Exception | None = None
    reads: list[dict[str, Any]] = field(default_factory=list)
    rerun_results: list[tuple[bool, int]] = field(default_factory=list)
    rerun_calls: list[int] = field(default_factory=list)
    tag_outcomes: list[tuple[str, str]] = field(default_factory=list)
    tag_calls: list[dict[str, Any]] = field(default_factory=list)
    open_nodes: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[tuple[int, int]] = field(default_factory=list)

    def tree(self) -> Path:
        return self.rig.clone / ".claude" / "worktrees" / "release-td42"

    def commit(self, subject: str, files: dict[str, str] | None = None) -> str:
        """A commit on the clone's main, pushed to origin; its sha."""
        name = f"f{time.monotonic_ns()}.txt"
        for fname, text in (files or {name: "x\n"}).items():
            (self.rig.clone / fname).write_text(text, encoding="utf-8")
        _g(self.rig.clone, "add", "-A")
        _g(self.rig.clone, "commit", "-q", "-m", subject)
        _g(self.rig.clone, "push", "-q", "origin", "main")
        return _g(self.rig.clone, "rev-parse", "HEAD")

    def tip(self) -> str:
        return _g(self.rig.origin, "rev-parse", "refs/heads/main")

    def record(self) -> Any:
        return self.store.refs[42].meta.get("repo_release")


@pytest.fixture
def rel(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Rel:  # noqa: F811
    store = _Store()
    store.add_todo(42, tags=("repo:catpath",), meta={"retries": 1})
    events: list[str] = []
    lease = ReleaseLease(events)
    r = Rel(rig, store, lease, events)
    r.rig.lease = lease
    monkeypatch.setattr(repo_lease, "acquire", r.rig.lease.acquire)
    monkeypatch.setattr(repo_lease, "holder", r.rig.lease.holder)
    monkeypatch.setattr(repo_lease, "release", r.rig.lease.release)
    monkeypatch.setattr(repo_lease, "want", lease.want)
    stamp = store.stamp_ref_meta

    def stamped(ref_id: int, updates: dict[str, Any]) -> None:
        rec = updates.get("repo_release")
        r.events.append(f"stamp:{rec['state'] if rec else 'clear'}")
        stamp(ref_id, updates)

    monkeypatch.setattr(store, "stamp_ref_meta", stamped)
    # catpath is open here, with its releaser pinned.
    profile = replace(lanes.REPO_PROFILES["catpath"], releaser="repo_release")
    monkeypatch.setitem(lanes.REPO_PROFILES, "catpath", profile)
    monkeypatch.setattr(
        lanes, "open_repos", lambda store: frozenset({"precis", "catpath"})
    )

    def ls_remote_tag(url: str, tag: str, *, env: Any) -> str | None:
        r.oracle_calls.append(tag)
        return None if tag in r.unreadable else r.tags.get(tag, "")

    def push_cas(
        url: str,
        ref: str,
        sha: str,
        tip: str,
        *,
        ops_parent: Path,
        env: Any,
        object_dirs: tuple[Path, ...],
    ) -> tuple[str, str]:
        r.events.append("push")
        rec = store.refs[42].meta.get("repo_release")
        r.pushes.append(Push(url, ref, sha, tip, copy.deepcopy(rec)))
        if r.on_push is not None:
            r.on_push()
        return r.outcomes.pop(0) if r.outcomes else ("ok", ref)

    monkeypatch.setattr(factory_git_mirror, "ls_remote_tag", ls_remote_tag)
    monkeypatch.setattr(factory_git_mirror, "push_cas", push_cas)

    def push_tag_new(
        url: str,
        tag: str,
        sha: str,
        *,
        message: str,
        tagger: tuple[str, str],
        ops_parent: Path,
        env: Any,
        object_dirs: tuple[Path, ...] = (),
    ) -> tuple[str, str]:
        r.events.append("tag")
        r.tag_calls.append(
            {
                "url": url,
                "tag": tag,
                "sha": sha,
                "message": message,
                "tagger": tagger,
                "env": dict(env),
                "object_dirs": object_dirs,
            }
        )
        if r.tag_outcomes:
            return r.tag_outcomes.pop(0)
        r.tags[tag] = sha
        return ("ok", f"{tag} created")

    monkeypatch.setattr(factory_git_mirror, "push_tag_new", push_tag_new)
    monkeypatch.setattr(
        repo_release, "_auth_env", lambda: {"PATH": "/bin", "GH_FAKE": TOKEN}
    )

    def read_runs(url: str, params: dict[str, Any]) -> Any:
        r.reads.append({"url": url, **params})
        if r.read_error is not None:
            raise r.read_error
        if "head_sha" in params or r.branch_runs is None:
            return {"workflow_runs": r.runs}
        return {"workflow_runs": r.branch_runs}

    def rerun(api: str, run_id: int) -> tuple[bool, int]:
        r.rerun_calls.append(run_id)
        return r.rerun_results.pop(0) if r.rerun_results else (True, 201)

    monkeypatch.setattr(repo_release, "_read_runs", read_runs)
    monkeypatch.setattr(repo_release, "_rerun", rerun)
    # the plan subtree the backstop walks (the real one is a SQL query)
    monkeypatch.setattr(
        repo_release,
        "_plan_rows",
        lambda store, root: [
            {"id": 42, "parent_id": None, "kind": "todo"},
            *r.open_nodes,
        ],
    )
    monkeypatch.setattr(repo_release, "_blocked_edges", lambda store, ids: r.blocked)

    def launch(argv: list[str], **kw: Any) -> int:
        # What the handle held the moment the verifier started.
        assert r.ctx is not None
        r.snapshots.append(
            {
                "handle": copy.deepcopy(r.ctx.meta_set.get("compute_handle")),
                "head": _g(kw["cwd"], "rev-parse", "HEAD"),
            }
        )
        r.rig.launches.append((argv, kw))
        return 4242

    monkeypatch.setattr(run_slice, "_launch", launch)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: False)
    monkeypatch.setattr(
        run_slice, "_signal_group", lambda pgid, sig: r.signals.append((pgid, int(sig)))
    )
    # The base the release is cut on: synthetic version files at origin's tip.
    r.base = r.commit(
        "chore: release files", {"pyproject.toml": PYPROJECT, "uv.lock": LOCK}
    )
    return r


def _ctx(rel: Rel, **params: Any) -> _Ctx:
    ctx = _Ctx(rel.store, {**PARAMS, **params})
    rel.ctx = ctx
    return ctx


def _due(handle: dict[str, Any]) -> None:
    handle["not_before"] = "2000-01-01T00:00:00Z"


def _submitted(rel: Rel, **params: Any) -> tuple[_Ctx, dict[str, Any]]:
    ctx = _ctx(rel, **params)
    handle = repo_release.submit(ctx, None)
    assert isinstance(handle, dict), ctx.failure
    return ctx, handle


def _prepared(rel: Rel, **params: Any) -> tuple[_Ctx, dict[str, Any]]:
    """A job past submit and the lease: poll once and it is in ``check``."""
    ctx, handle = _submitted(rel, **params)
    assert repo_release.poll(ctx, handle) is False, ctx.failure
    assert handle["phase"] == "check", (handle, ctx.failure)
    return ctx, handle


def _finish(handle: dict[str, Any], code: int, log_text: str = "") -> None:
    Path(handle["log"]).write_text(log_text, encoding="utf-8")
    Path(handle["exit_file"]).write_text(f"{code}\n", encoding="utf-8")


def _attempt_free(ctx: _Ctx, store: _Store, red_class: str) -> None:
    assert ctx.status == "failed"
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "infra-release"
    assert ctx.meta_set["red_class"] == red_class
    # a release node is not a slice: its claim, status and tags are untouched
    assert store.tag_strings(42) == {"STATUS:doing", "repo:catpath"}


def _charged(ctx: _Ctx, store: _Store) -> None:
    assert ctx.status == "failed"
    assert run_slice.ATTEMPT_FREE_KEY not in ctx.meta_set
    assert store.tag_strings(42) == {"STATUS:doing", "repo:catpath"}
    assert store.comments(42)[-1].startswith("handoff: release FAILED:")


def _no_pushed_record(rel: Rel) -> None:
    rec = rel.record()
    assert rec is None or rec.get("state") != "pushed"


# ── submit and admission ───────────────────────────────────────────


def test_submit_writes_nothing_and_returns_a_parked_lease_handle(rel: Rel) -> None:
    """If this fails submit fetches, takes the lease or writes the todo before
    the worker polls, or its parked handle is not the shape poll and kill read."""
    ctx, handle = _submitted(rel)
    assert handle["op"] == "repo_release" and handle["repo"] == "catpath"
    assert handle["phase"] == "lease"
    due = datetime.strptime(handle["not_before"], TS).replace(tzinfo=UTC)
    assert abs((due - datetime.now(UTC)).total_seconds()) < 5
    assert "pid" not in handle and "sha" not in handle
    assert ctx.meta_set["compute_handle"] is handle
    assert ctx.meta_set["compute_handle_released"] is False
    assert ctx.meta_set["deadline"] == handle["deadline"] > time.time()
    assert ctx.failure is None and ctx.status is None and ctx.chunks == []
    assert rel.rig.fetches == [] and rel.rig.launches == []
    assert rel.rig.lease.acquires == [] and rel.events == []
    assert rel.oracle_calls == [] and rel.pushes == []
    assert rel.store.comments(42) == [] and rel.record() is None
    assert not rel.tree().exists()


def _no_releaser(m: pytest.MonkeyPatch) -> str:
    m.setitem(
        lanes.REPO_PROFILES,
        "catpath",
        replace(lanes.REPO_PROFILES["catpath"], releaser=None),
    )
    return "names no releaser"


def _closed(m: pytest.MonkeyPatch) -> str:
    m.setattr(lanes, "open_repos", lambda store: frozenset({"precis"}))
    return "open_repos"


def _factory_off(m: pytest.MonkeyPatch) -> str:
    m.setenv("PRECIS_FACTORY", "off")
    return "factory is off"


def _no_ci(m: pytest.MonkeyPatch) -> str:
    m.setitem(
        lanes.REPO_PROFILES,
        "catpath",
        replace(lanes.REPO_PROFILES["catpath"], ci_workflow=None),
    )
    return "ci_workflow"


def _no_check(m: pytest.MonkeyPatch) -> str:
    m.setitem(
        lanes.REPO_PROFILES,
        "catpath",
        replace(lanes.REPO_PROFILES["catpath"], release_check_cmds=()),
    )
    return "release check"


@pytest.mark.parametrize(
    "how", [_no_releaser, _closed, _factory_off, _no_ci, _no_check]
)
def test_a_repo_that_may_not_be_released_is_refused_at_submit(
    rel: Rel, monkeypatch: pytest.MonkeyPatch, how: Any
) -> None:
    """If this fails a job for a repo with no releaser, a closed repo, a
    profile without CI or a release check, or a factory switched off, gets a
    lease and a release tree (admission is the only gate before the push)."""
    needle = how(monkeypatch)
    ctx = _ctx(rel)
    assert repo_release.submit(ctx, None) is None
    assert ctx.status == "failed" and needle in (ctx.failure or "")
    assert repo_release.validate_submit(rel.store, params=PARAMS) == ctx.failure
    assert rel.rig.fetches == [] and rel.rig.lease.acquires == []


def test_real_catpath_profile_names_the_releaser(
    rel: Rel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails the real profile no longer names this job, and the
    release refusal for a repo with no releaser is the only answer."""
    monkeypatch.undo()  # the real REPO_PROFILES
    assert lanes.REPO_PROFILES["catpath"].releaser == repo_release.NAME
    assert "names no releaser" not in (
        repo_release._release_refusal(None, PARAMS) or ""
    )


def test_a_todo_tagged_for_another_repo_or_closed_is_refused(rel: Rel) -> None:
    """If this fails a release job can run against a node of another repo or
    one a ruling already closed."""
    other = _Store()
    other.add_todo(42, tags=("repo:precis",))
    assert "resolves to repo" in (
        repo_release.validate_submit(other, params=PARAMS) or ""
    )
    done = _Store()
    done.add_todo(42, status="done", tags=("repo:catpath",))
    assert "is closed" in (repo_release.validate_submit(done, params=PARAMS) or "")


# ── the lease ──────────────────────────────────────────────────────


def test_a_blocked_lease_re_parks_and_want_comes_before_acquire(rel: Rel) -> None:
    """If this fails a release waiting for the lease does not state its want
    first (lands then overtake it forever), or starts work while blocked."""
    ctx, handle = _submitted(rel)
    rel.rig.lease.blocker = {"job_id": 77, "kind": "land", "reason": "held"}
    assert repo_release.poll(ctx, handle) is False
    assert rel.events == ["want", "acquire"]
    assert rel.rig.lease.acquires == [("catpath", 901, "release")]
    assert rel.lease.wants == [("catpath", 901)]
    assert handle["phase"] == "lease" and rel.rig.fetches == []
    due = datetime.strptime(handle["not_before"], TS).replace(tzinfo=UTC)
    assert due > datetime.now(UTC)
    assert ctx.meta_set["deadline"] > time.time() + repo_land.LEASE_PARK_S
    events = [t for k, t in ctx.chunks if k == "job_event"]
    assert len(events) == 1
    assert "repo_release: catpath lease held by land job #77" in events[0]


def test_a_poll_before_not_before_asks_for_nothing(rel: Rel) -> None:
    """If this fails every poll tick hits the lease table."""
    ctx, handle = _submitted(rel)
    handle["not_before"] = "2999-01-01T00:00:00Z"
    assert repo_release.poll(ctx, handle) is False
    assert rel.events == [] and rel.rig.fetches == []


def test_the_lease_wait_cap_fails_attempt_free(rel: Rel) -> None:
    """If this fails a release behind a wedged holder waits forever, or its
    failure is charged to the node."""
    ctx, handle = _submitted(rel)
    rel.rig.lease.blocker = {"job_id": 77, "kind": "land", "reason": "held"}
    handle["lease_since"] = time.time() - repo_land.LEASE_WAIT_CAP_S - 60
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_land.HELD_CLASS)
    assert rel.rig.fetches == []


def test_a_parked_release_waking_to_a_closed_repo_takes_no_lease(
    rel: Rel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a release that waited takes the lease after the operator
    closed the repo."""
    ctx, handle = _submitted(rel)
    monkeypatch.setattr(lanes, "open_repos", lambda store: frozenset({"precis"}))
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_land.CLOSED_CLASS)
    assert rel.events == []


# ── prepare ────────────────────────────────────────────────────────


def test_prepare_commits_the_two_version_lines_on_the_tip_and_launches_the_verifier(
    rel: Rel,
) -> None:
    """If this fails the release commit is not a one-line-each child of the
    tip, carries the wrong identity or subject, is not in the handle before
    the verifier runs, or the verifier can see a credential."""
    rel.commit("chore: later")
    tip = rel.tip()
    ctx, handle = _prepared(rel)
    sha = handle["sha"]
    clone = rel.rig.clone
    assert handle["tip"] == tip and len(sha) == 40 and sha != tip
    assert _g(clone, "rev-parse", f"{sha}^") == tip
    assert (handle["version"], handle["tag"]) == ("1.3.0", "v1.3.0")
    assert handle["package"] == "demo" and handle["prev_sha"] is None
    changed = _g(clone, "diff", "--name-only", tip, sha).splitlines()
    assert changed == ["pyproject.toml", "uv.lock"]
    numstat = _g(clone, "diff", "--numstat", tip, sha).splitlines()
    assert sorted(numstat) == ["1\t1\tpyproject.toml", "1\t1\tuv.lock"]
    assert '+version = "1.3.0"' in _g(clone, "diff", tip, sha, "--", "pyproject.toml")
    lock = _g(clone, "show", f"{sha}:uv.lock")
    assert lock.count('version = "1.3.0"') == 1 and 'version = "2.0.0"' in lock
    assert _g(clone, "log", "-1", "--format=%s", sha) == "chore(release): v1.3.0 (td42)"
    assert _g(clone, "log", "-1", "--format=%an <%ae>", sha) == (
        "precis factory <factory-release@users.noreply.github.com>"
    )
    assert _g(clone, "log", "-1", "--format=%cn <%ce>", sha) == (
        "precis factory <factory-release@users.noreply.github.com>"
    )
    # the tree is the detached release tree on the commit
    assert handle["release_tree"] == str(rel.tree()) and rel.tree().is_dir()
    # the sha was on the persisted handle, and the tree at it, when the
    # verifier started
    assert len(rel.snapshots) == 1
    snap = rel.snapshots[0]
    assert snap["head"] == sha
    for key, want in (
        ("tip", tip),
        ("sha", sha),
        ("version", "1.3.0"),
        ("tag", "v1.3.0"),
    ):
        assert snap["handle"][key] == want
    assert "pid" not in snap["handle"]
    # detached launch: cwd the release tree, scrubbed env, the profile's check
    argv, kw = rel.rig.launches[0]
    assert kw["cwd"] == rel.tree()
    script = Path(argv[1]).read_text(encoding="utf-8")
    assert "uv lock --check" in script and "✖ gate check failed" in script
    env = kw["env"]
    assert not [k for k in env if land_slice._LAND_ENV_SECRET_RE.search(k)]
    for name in ("SSH_AUTH_SOCK", "GH_CONFIG_DIR", "DOCKER_HOST"):
        assert name not in env
    assert env["UV_PYTHON"] == "3.12" and not [
        k for k in env if k.startswith("PRECIS_")
    ]
    assert handle["pid"] == handle["pgid"] == 4242
    assert ctx.meta_set["compute_handle"] == handle
    assert rel.rig.lease.holder_job == 901 and rel.rig.fetches
    assert rel.pushes == [] and rel.record() is None


@pytest.mark.parametrize(
    ("subject", "bump", "version"),
    [
        ("feat(x): widget", "auto", "1.3.0"),
        ("feat!: break", "auto", "1.3.0"),
        ("fix(x): bug", "auto", "1.2.1"),
        ("chore: tidy", "auto", "1.2.1"),
        ("feat(x): widget", "patch", "1.2.1"),
        ("fix(x): bug", "minor", "1.3.0"),
    ],
)
def test_the_bump_is_picked_from_subjects_since_the_previous_tag(
    rel: Rel, subject: str, bump: str, version: str
) -> None:
    """If this fails an auto release bumps minor without a feat (or patch with
    one), or params.bump is ignored."""
    rel.tags["v1.2.0"] = rel.base
    rel.commit(subject)
    ctx, handle = _prepared(rel, bump=bump)
    assert handle["version"] == version and handle["tag"] == f"v{version}"
    assert (handle["prev_tag"], handle["prev_sha"]) == ("v1.2.0", rel.base)
    body = _g(rel.rig.clone, "log", "-1", "--format=%b", handle["sha"])
    assert "Since v1.2.0:" in body and f"- {subject}" in body


def test_an_old_feat_before_the_previous_tag_does_not_bump_minor(rel: Rel) -> None:
    """If this fails the subject scan reads past the previous tag."""
    rel.commit("feat: old")
    rel.tags["v1.2.0"] = rel.commit("fix: tagged")
    rel.commit("fix: after")
    _, handle = _prepared(rel)
    assert handle["version"] == "1.2.1"


def test_no_previous_tag_bumps_minor(rel: Rel) -> None:
    """If this fails the first release of a repo with no tag goes patch."""
    rel.commit("fix: only a fix")
    _, handle = _prepared(rel)
    assert handle["version"] == "1.3.0"
    assert handle["prev_tag"] is None and handle["prev_sha"] is None


def test_a_previous_tag_off_the_base_bumps_minor(rel: Rel) -> None:
    """If this fails a tag that is not an ancestor of the tip is read as
    history (its subjects are not the base's)."""
    clone = rel.rig.clone
    _g(clone, "checkout", "-q", "-b", "side", rel.base)
    (clone / "side.txt").write_text("s\n", encoding="utf-8")
    _g(clone, "add", "-A")
    _g(clone, "commit", "-q", "-m", "fix: on a side branch")
    rel.tags["v1.2.0"] = _g(clone, "rev-parse", "HEAD")
    _g(clone, "checkout", "-q", "main")
    rel.commit("fix: on main")
    _, handle = _prepared(rel)
    assert handle["version"] == "1.3.0"
    assert handle["prev_sha"] == rel.tags["v1.2.0"]


def test_the_previous_tag_at_the_tip_is_a_no_op_success(rel: Rel) -> None:
    """If this fails a release with nothing new commits and pushes a version
    anyway, or the no-op leaves no record the batch can read."""
    tip = rel.tip()
    rel.tags["v1.2.0"] = tip
    commits = _g(rel.rig.clone, "rev-list", "--all", "--count")
    ctx, handle = _submitted(rel)
    assert repo_release.poll(ctx, handle) is True
    assert ctx.status == "succeeded" and ctx.failure is None
    rec = rel.record()
    assert rec["state"] == "tagged" and rec["no_op"] is True
    assert (rec["version"], rec["tag"], rec["sha"]) == ("1.2.0", "v1.2.0", tip)
    assert rec["repo"] == "catpath" and rec["package"] == "demo" and rec["job"] == 901
    assert rel.store.comments(42) == [
        f"released: v1.2.0 {tip} (no-op: nothing landed since the last release)"
    ]
    assert rel.pushes == [] and rel.rig.launches == [] and not rel.tree().exists()
    assert _g(rel.rig.clone, "rev-list", "--all", "--count") == commits
    assert rel.rig.lease.releases == [("catpath", 901)]
    assert "STATUS:done" not in rel.store.tag_strings(42)  # the dispatcher closes it


def test_a_new_tag_already_on_the_remote_is_charged_and_pushes_nothing(
    rel: Rel,
) -> None:
    """If this fails a version that already has a tag is released again."""
    rel.tags["v1.3.0"] = "c" * 40
    ctx, handle = _submitted(rel)
    assert repo_release.poll(ctx, handle) is True
    _charged(ctx, rel.store)
    assert "v1.3.0 already exists" in (ctx.failure or "")
    assert rel.pushes == [] and rel.rig.launches == [] and not rel.tree().exists()
    _no_pushed_record(rel)


@pytest.mark.parametrize("unreadable", ["v1.3.0", "v1.2.0"])
def test_an_unreadable_tag_oracle_is_attempt_free(rel: Rel, unreadable: str) -> None:
    """If this fails a remote that cannot be read is charged to the node, or
    worse, read as "no tag" and released."""
    rel.unreadable.add(unreadable)
    ctx, handle = _submitted(rel)
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_release.TAG_ORACLE_CLASS)
    assert rel.pushes == [] and rel.rig.launches == []


def test_an_unreadable_base_fetch_is_attempt_free(
    rel: Rel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails an unreachable remote is charged to the node."""
    monkeypatch.setattr(factory_git_mirror, "fetch_ref_into", lambda *a, **k: None)
    ctx, handle = _submitted(rel)
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_release.FETCH_CLASS)


@pytest.mark.parametrize(
    "files",
    [
        {"uv.lock": 'version = 1\n\n[[package]]\nname = "other"\nversion = "1.0.0"\n'},
        {"pyproject.toml": '[project]\nname = "demo"\nversion = "1.2"\n'},
    ],
)
def test_a_base_the_text_helpers_refuse_is_charged(
    rel: Rel, files: dict[str, str]
) -> None:
    """If this fails a lockfile without the project's block, or a version that
    is not X.Y.Z, is released by some other route than the pure edits."""
    rel.commit("chore: break it", files)
    ctx, handle = _submitted(rel)
    assert repo_release.poll(ctx, handle) is True
    _charged(ctx, rel.store)
    assert rel.pushes == [] and rel.rig.launches == [] and not rel.tree().exists()


def test_a_stale_release_tree_is_replaced(rel: Rel) -> None:
    """If this fails a leftover tree from a killed job breaks the next one."""
    stale = rel.tree()
    stale.mkdir(parents=True)
    (stale / "junk").write_text("x", encoding="utf-8")
    _, handle = _prepared(rel)
    assert not (stale / "junk").exists() and handle["sha"]


# ── check ──────────────────────────────────────────────────────────


def test_a_running_verifier_keeps_waiting(
    rel: Rel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a verifier still running is judged, or its tree dropped."""
    ctx, handle = _prepared(rel)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: True)
    assert repo_release.poll(ctx, handle) is False
    assert ctx.status is None and rel.tree().is_dir() and rel.pushes == []
    assert ctx.meta_set["compute_handle_released"] is False


def test_past_its_deadline_the_verifier_is_killed_attempt_free(
    rel: Rel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a wedged verifier holds the repo lease until the row is
    reaped."""
    ctx, handle = _prepared(rel)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: True)
    handle["deadline"] = 1.0
    assert repo_release.poll(ctx, handle) is True
    assert rel.signals == [(4242, 15)]
    _attempt_free(ctx, rel.store, repo_land.TIMED_OUT_CLASS)
    assert not rel.tree().exists()


def test_a_red_verifier_pushes_nothing_and_is_charged_with_a_relock_handoff(
    rel: Rel,
) -> None:
    """If this fails a red lockfile check pushes, leaves a pushed record,
    spends the attempt-free path, or tells the next worker nothing."""
    secret = "ghp_" + "".join(chr(97 + (i * 7) % 26) for i in range(36))
    ctx, handle = _prepared(rel)
    _finish(handle, 1, f"token {secret}\n{CHECK_RED_LOG}")
    assert repo_release.poll(ctx, handle) is True
    _charged(ctx, rel.store)
    assert rel.pushes == [] and not rel.tree().exists()
    _no_pushed_record(rel)
    handoff = rel.store.comments(42)[-1]
    assert "uv lock --check" in handoff and "relock" in handoff
    assert "no version was spent" in handoff
    assert secret not in handoff and secret not in (ctx.failure or "")
    assert "stamp:pushed" not in rel.events


@pytest.mark.parametrize("how", ["137", "gone"])
def test_a_killed_verifier_is_attempt_free(
    rel: Rel, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """If this fails a verifier killed from outside (exit 137, or a process
    that vanished without an exit file) is charged to the node."""
    ctx, handle = _prepared(rel)
    if how == "137":
        _finish(handle, 137, "Killed\n")
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_land.GATE_INFRA_CLASS)
    assert rel.pushes == [] and not rel.tree().exists()
    _no_pushed_record(rel)


def test_a_verifier_that_rewrites_the_tree_does_not_change_the_pushed_sha(
    rel: Rel,
) -> None:
    """If this fails repo-controlled code run by the verifier can change what
    is pushed (the sha must come from the handle, never from the tree)."""
    ctx, handle = _prepared(rel)
    sha, tip = handle["sha"], handle["tip"]
    tree = rel.tree()
    # a hostile verifier: rewrite the files, add a commit and move HEAD
    (tree / "pyproject.toml").write_text("# pwned\n", encoding="utf-8")
    (tree / "evil.py").write_text("x = 1\n", encoding="utf-8")
    _g(tree, "add", "-A")
    _g(tree, "commit", "-q", "-m", "evil")
    assert _g(tree, "rev-parse", "HEAD") != sha
    _finish(handle, 0)
    assert repo_release.poll(ctx, handle) is False, ctx.failure
    assert [(p.sha, p.tip) for p in rel.pushes] == [(sha, tip)]
    assert handle["phase"] == "ci" and handle["sha"] == sha


def test_green_stamps_the_record_before_the_push_then_parks_in_ci(rel: Rel) -> None:
    """If this fails a push can happen with no record on the node (a later
    failure then spends the version with nothing to resume), or the job ends
    instead of waiting for CI with the lease held."""
    rel.tags["v1.2.0"] = rel.base
    rel.commit("feat: thing")
    ctx, handle = _prepared(rel)
    sha, tip = handle["sha"], handle["tip"]
    _finish(handle, 0)
    rel.events.clear()
    assert repo_release.poll(ctx, handle) is False, ctx.failure
    assert rel.events == ["stamp:pushed", "push"]
    (push,) = rel.pushes
    assert (push.url, push.ref, push.sha, push.tip) == (
        lanes.REPO_PROFILES["catpath"].remote,
        "main",
        sha,
        tip,
    )
    stamped = push.record
    assert stamped is not None
    assert stamped["state"] == "pushed" and stamped["sha"] == sha
    assert (stamped["version"], stamped["tag"]) == ("1.3.0", "v1.3.0")
    assert (stamped["prev_tag"], stamped["prev_sha"]) == ("v1.2.0", rel.base)
    assert (stamped["repo"], stamped["package"], stamped["base"]) == (
        "catpath",
        "demo",
        "main",
    )
    assert stamped["job"] == 901 and stamped["pushed_at"]
    assert rel.record() == stamped
    assert "landed_repo" not in rel.store.refs[42].meta
    # parked in ci, the lease still held, the tree gone, the comment written
    assert (
        handle["phase"] == "ci" and handle["sha"] == sha and handle["tag"] == "v1.3.0"
    )
    due = datetime.strptime(handle["not_before"], TS).replace(tzinfo=UTC)
    assert 60 < (due - datetime.now(UTC)).total_seconds() <= 125
    assert ctx.meta_set["compute_handle"] == handle
    assert ctx.status is None and rel.rig.lease.releases == []
    assert not rel.tree().exists()
    assert rel.store.comments(42) == [
        f"release commit pushed: v1.3.0 {sha}; waiting for CI"
    ]


def test_a_ci_handle_not_yet_due_reads_nothing(rel: Rel) -> None:
    """If this fails every poll tick of a parked CI wait hits GitHub, or lets
    go of the lease."""
    ctx, handle = _prepared(rel)
    _finish(handle, 0)
    assert repo_release.poll(ctx, handle) is False
    assert handle["phase"] == "ci"
    before = copy.deepcopy(handle)
    ctx.meta_set.clear()
    assert repo_release.poll(ctx, handle) is False  # not due: untouched
    assert handle == before and ctx.meta_set == {}
    assert rel.reads == [] and rel.rig.lease.releases == [] and ctx.status is None
    assert len(rel.pushes) == 1


# ── the push: raced, error, admission ──────────────────────────────


def test_a_raced_push_prepares_again_on_the_new_tip(rel: Rel) -> None:
    """If this fails a push that lost the race fails the job, or is retried
    on the old tip, or leaves the first round's record behind."""
    ctx, handle = _prepared(rel)
    first = handle["sha"]
    _finish(handle, 0)
    rel.outcomes[:] = [("raced", "main moved"), ("ok", "main")]
    new_tip: list[str] = []
    rel.on_push = lambda: new_tip.append(rel.commit("fix: raced ahead"))
    assert repo_release.poll(ctx, handle) is False, ctx.failure
    assert handle["phase"] == "check" and handle["rounds"] == 2
    assert handle["tip"] == new_tip[0] and handle["sha"] != first
    assert _g(rel.rig.clone, "rev-parse", f"{handle['sha']}^") == new_tip[0]
    assert rel.events.count("stamp:clear") == 1 and rel.record() is None
    assert len(rel.rig.launches) == 2 and rel.rig.lease.acquires == [
        ("catpath", 901, "release")
    ]
    assert ctx.meta_set["compute_handle"] == handle and ctx.status is None
    rel.on_push = None
    Path(handle["exit_file"]).unlink(missing_ok=True)
    _finish(handle, 0)
    assert repo_release.poll(ctx, handle) is False, ctx.failure
    assert handle["phase"] == "ci"
    assert [p.sha for p in rel.pushes][0] == first
    assert rel.pushes[1].tip == new_tip[0] and rel.pushes[1].sha != first
    assert rel.record()["state"] == "pushed"


def test_three_raced_rounds_end_attempt_free(rel: Rel) -> None:
    """If this fails a base that moves on every try loops forever, or the
    exhausted job is charged to the node."""
    rel.outcomes[:] = [("raced", "main moved")] * 3
    rel.on_push = lambda: rel.commit("fix: raced again")
    ctx, handle = _prepared(rel)
    for expect_rounds in (1, 2, 3):
        assert handle["rounds"] == expect_rounds
        Path(handle["exit_file"]).unlink(missing_ok=True)
        _finish(handle, 0)
        done = repo_release.poll(ctx, handle)
        assert done is (expect_rounds == 3)
    _attempt_free(ctx, rel.store, repo_land.RACED_CLASS)
    assert len(rel.pushes) == 3 and len(rel.rig.launches) == 3
    assert not rel.tree().exists()
    # The last round's record stays: the next job's resume check reads the
    # remote and clears it (the sha is not on the base).
    assert rel.record()["state"] == "pushed"
    ctx2, handle2 = _submitted(rel)
    rel.outcomes[:] = []
    rel.on_push = None
    assert repo_release.poll(ctx2, handle2) is False, ctx2.failure
    assert handle2["phase"] == "check" and rel.record() is None


def test_a_raced_push_that_landed_under_another_commit_waits_for_its_ci(
    rel: Rel,
) -> None:
    """If this fails a push that landed, with someone's commit on top before
    the read-back, is read as lost: the record is cleared, a second version is
    cut and the first release commit is never tagged."""
    ctx, handle = _prepared(rel)
    first = handle["sha"]
    _finish(handle, 0)
    rel.outcomes[:] = [("raced", "main moved")]

    def landed_then_moved() -> None:
        _g(rel.rig.clone, "push", "-q", "origin", f"{first}:refs/heads/main")
        _g(rel.rig.clone, "reset", "-q", "--hard", first)
        rel.commit("fix: a hand push on top")

    rel.on_push = landed_then_moved
    assert repo_release.poll(ctx, handle) is False, ctx.failure
    assert handle["phase"] == "ci" and handle["sha"] == first
    assert rel.record()["state"] == "pushed" and rel.record()["sha"] == first
    assert len(rel.pushes) == 1 and len(rel.rig.launches) == 1
    assert "stamp:clear" not in rel.events


def test_a_history_git_cannot_read_ends_attempt_free_and_keeps_the_record(
    rel: Rel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a git error on the resume's ancestry check is read as
    "the push never happened" and a pushed release commit is forgotten."""
    monkeypatch.setattr(repo_release, "_on_base", lambda root, sha, tip: None)
    ctx, _handle, done = _resumed(rel, _pushed_record(rel.tip()))
    assert done is True
    _attempt_free(ctx, rel.store, repo_release.READ_CLASS)
    assert rel.record()["state"] == "pushed"


def test_a_push_error_is_attempt_free_and_leaves_the_record_for_the_resume(
    rel: Rel,
) -> None:
    """If this fails a push that may have happened loses its record (the next
    job would cut a second release commit for the same version)."""
    rel.outcomes[:] = [("error", "unreadable after the push")]
    ctx, handle = _prepared(rel)
    _finish(handle, 0)
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_land.PUSH_ERROR_CLASS)
    assert rel.record()["state"] == "pushed" and rel.record()["sha"] == handle["sha"]
    assert not rel.tree().exists()


@pytest.mark.parametrize("how", ["closed", "off", "lease-lost", "no-lease"])
def test_a_repo_closed_or_lease_lost_during_the_check_is_never_pushed(
    rel: Rel, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """If this fails a release pushes after the operator closed the repo or
    another job took the lease while the verifier ran."""
    ctx, handle = _prepared(rel)
    _finish(handle, 0)
    red_class = repo_land.CLOSED_CLASS
    if how == "closed":
        monkeypatch.setattr(lanes, "open_repos", lambda store: frozenset({"precis"}))
    elif how == "off":
        monkeypatch.setenv("PRECIS_FACTORY", "off")
    else:
        rel.rig.lease.holder_job = 77 if how == "lease-lost" else None
        red_class = repo_land.HELD_CLASS
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, red_class)
    assert rel.pushes == [] and not rel.tree().exists()
    _no_pushed_record(rel)
    assert "stamp:pushed" not in rel.events


def test_a_failed_stamp_means_no_push(
    rel: Rel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a push goes out with the record unwritten."""
    ctx, handle = _prepared(rel)
    _finish(handle, 0)

    def broken(ref_id: int, updates: dict[str, Any]) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(rel.store, "stamp_ref_meta", broken)
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_release.RECORD_CLASS)
    assert rel.pushes == []


@pytest.mark.parametrize("field_name", ["sha", "tip"])
def test_a_handle_whose_commit_is_not_a_child_of_its_tip_is_never_pushed(
    rel: Rel, field_name: str
) -> None:
    """If this fails a tampered or stale handle pushes a commit that was not
    verified against the tip."""
    ctx, handle = _prepared(rel)
    _finish(handle, 0)
    handle[field_name] = rel.base if field_name == "sha" else handle["sha"]
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_land.HANDLE_CLASS)
    assert rel.pushes == []


# ── resume ─────────────────────────────────────────────────────────


def _pushed_record(sha: str, **over: Any) -> dict[str, Any]:
    return {
        "repo": "catpath",
        "package": "demo",
        "version": "1.3.0",
        "tag": "v1.3.0",
        "sha": sha,
        "base": "main",
        "prev_tag": None,
        "prev_sha": None,
        "state": "pushed",
        "job": 800,
        "pushed_at": "2026-11-14T10:00:00+00:00",
        **over,
    }


def _resumed(rel: Rel, record: dict[str, Any]) -> tuple[_Ctx, dict[str, Any], Any]:
    rel.store.refs[42].meta["repo_release"] = record
    ctx, handle = _submitted(rel)
    return ctx, handle, repo_release.poll(ctx, handle)


@pytest.mark.parametrize("readable", [True, False])
def test_a_pushed_record_with_its_tag_at_that_sha_records_without_waiting(
    rel: Rel, readable: bool
) -> None:
    """If this fails a job after an interrupted release cuts a second commit
    for a version that is already pushed and tagged, waits for CI it does not
    need, or loses the ci block when one cheap read has it (and fails when
    that read does not)."""
    sha = "a" * 40
    rel.tags["v1.3.0"] = sha
    if readable:
        rel.runs = [_run(sha)]
    else:
        rel.read_error = GitHubUnreadable("GET u: HTTP 502")
    ctx, handle, done = _resumed(rel, _pushed_record(sha))
    assert done is True and ctx.status == "succeeded" and ctx.failure is None
    rec = rel.record()
    assert rec["state"] == "tagged" and rec["sha"] == sha and rec["job"] == 901
    assert rec["pushed_at"] == "2026-11-14T10:00:00+00:00" and rec["tagged_at"]
    if readable:
        assert rec["ci"] == {
            "run_id": 500,
            "attempt": 1,
            "url": RUN_URL,
            "conclusion": "success",
        }
        assert rel.store.comments(42) == [f"released: v1.3.0 {sha} (CI run 500)"]
    else:
        assert "ci" not in rec
        assert rel.store.comments(42) == [f"released: v1.3.0 {sha}"]
    assert rel.rig.launches == [] and rel.pushes == [] and rel.tag_calls == []
    assert rel.rig.lease.releases == [("catpath", 901)]


def test_a_pushed_record_with_the_tag_elsewhere_is_a_charged_tag_conflict(
    rel: Rel,
) -> None:
    """If this fails a tag that points at another commit than the one this
    node pushed is built on, or the conflict is retried for free."""
    rel.tags["v1.3.0"] = "d" * 40
    ctx, handle, done = _resumed(rel, _pushed_record("a" * 40))
    assert done is True
    _charged(ctx, rel.store)
    assert rel.record()["state"] == "tag-conflict"
    assert "tag v1.3.0 exists at" in (ctx.failure or "")
    assert rel.pushes == [] and rel.rig.launches == []


def test_a_pushed_record_with_no_tag_and_its_sha_on_main_resumes_in_ci(
    rel: Rel,
) -> None:
    """If this fails a release commit that reached main but was not tagged yet
    is released a second time."""
    sha = rel.base  # an ancestor of the tip
    rel.commit("fix: after")
    ctx, handle, done = _resumed(rel, _pushed_record(sha))
    assert done is False and handle["phase"] == "ci" and handle["sha"] == sha
    assert rel.rig.launches == []


def test_a_pushed_record_whose_sha_never_reached_main_starts_fresh(rel: Rel) -> None:
    """If this fails a push that never happened leaves the version stuck on a
    commit nobody has."""
    ctx, handle, done = _resumed(rel, _pushed_record("b" * 40))
    assert done is False and handle["phase"] == "check"
    assert handle["sha"] != "b" * 40 and handle["version"] == "1.3.0"
    assert rel.record() is None
    assert len(rel.rig.launches) == 1


def test_a_pushed_record_with_an_unreadable_tag_is_attempt_free(rel: Rel) -> None:
    """If this fails an unreadable remote is read as "no tag" and the record
    is dropped."""
    rel.unreadable.add("v1.3.0")
    ctx, _, done = _resumed(rel, _pushed_record("a" * 40))
    assert done is True
    _attempt_free(ctx, rel.store, repo_release.TAG_ORACLE_CLASS)
    assert rel.record()["state"] == "pushed"


# ── kill ───────────────────────────────────────────────────────────


def test_kill_on_a_parked_handle_does_not_raise(rel: Rel) -> None:
    """If this fails the wall-clock or operator kill of a parked release
    raises (no pid, no tree) and the row is never reaped."""
    ctx, handle = _submitted(rel)
    repo_release.kill(ctx, handle)
    ci = {"op": "repo_release", "repo": "catpath", "phase": "ci", "sha": "a" * 40}
    repo_release.kill(ctx, ci)
    repo_release.kill(ctx, "garbage")
    repo_release.kill(ctx, None)
    assert rel.signals == []


def test_kill_on_a_live_verifier_signals_its_group_and_drops_the_tree(
    rel: Rel,
) -> None:
    """If this fails a killed release leaves its verifier running and its tree
    in the clone."""
    ctx, handle = _prepared(rel)
    assert rel.tree().is_dir()
    repo_release.kill(ctx, handle)
    assert rel.signals == [(4242, 15)]
    assert not rel.tree().exists()


def test_commands_script_labels_the_failing_step(tmp_path: Path) -> None:
    """If this fails the verifier's failure line is not the one poll parses."""
    script = tmp_path / "s.sh"
    script.write_text(
        repo_land.commands_script([("check", "echo A"), ("check", "exit 4")]),
        encoding="utf-8",
    )
    import subprocess

    proc = subprocess.run(
        ["sh", str(script)], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 4
    assert proc.stdout.splitlines() == ["A", "✖ gate check failed: exit 4"]


# ── ci: the runs read, the re-run, the tag ─────────────────────────


def _run(sha: str, **over: Any) -> dict[str, Any]:
    """One workflow run of the profile's ci.yml, green by default."""
    return {
        "id": 500,
        "head_sha": sha,
        "path": ".github/workflows/ci.yml",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "html_url": RUN_URL,
        **over,
    }


def _in_ci(rel: Rel) -> tuple[_Ctx, dict[str, Any], str]:
    """A job whose push just went out: parked in ``ci``, due now."""
    ctx, handle = _prepared(rel)
    _finish(handle, 0)
    assert repo_release.poll(ctx, handle) is False, ctx.failure
    assert handle["phase"] == "ci"
    _due(handle)
    rel.events.clear()
    return ctx, handle, handle["sha"]


def _coordinator(ctx: _Ctx, store: _Store, needle: str) -> None:
    """A charged failure that plan_dispatch's coordinator reader sees."""
    _charged(ctx, store)
    assert needle in ctx.meta_set[repo_release.COORDINATOR_ASK_KEY]
    assert needle in (ctx.failure or "")


def _clean(rel: Rel, ctx: _Ctx) -> None:
    """Nothing the job wrote carries the credential the rig put in the env."""
    written = [
        repr(ctx.meta_set),
        repr(ctx.chunks),
        repr(ctx.failure),
        repr(rel.store.comments(42)),
        repr(rel.store.refs[42].meta),
        repr(rel.events),
        repr(rel.reads),
    ]
    assert not [w for w in written if TOKEN in w]


def _parked(handle: dict[str, Any]) -> bool:
    due = datetime.strptime(handle["not_before"], TS).replace(tzinfo=UTC)
    return due > datetime.now(UTC)


def test_green_run_tags_the_pushed_sha_and_records_it(rel: Rel) -> None:
    """If this fails a green release is not tagged at the pushed sha, the
    record lacks its ci block, a `landed:` line is written instead of the one
    `released:` comment, the lease is kept, or the job does not succeed."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha), _run("e" * 40, id=499), _run(sha, id=498, path="other.yml")]
    assert repo_release.poll(ctx, handle) is True, ctx.failure
    assert ctx.status == "succeeded" and ctx.failure is None
    (call,) = rel.tag_calls
    assert (call["tag"], call["sha"], call["url"]) == (
        "v1.3.0",
        sha,
        lanes.REPO_PROFILES["catpath"].remote,
    )
    assert call["tagger"] == repo_release.RELEASE_IDENT and call["message"] == "v1.3.0"
    assert call["env"]["GH_FAKE"] == TOKEN  # the helper gets the credential env
    assert call["object_dirs"] and call["object_dirs"][0].name == "objects"
    (read,) = rel.reads
    assert read["url"] == "https://api.github.com/repos/retospect/catpath/actions/runs"
    assert (read["head_sha"], read["branch"], read["event"]) == (sha, "main", "push")
    rec = rel.record()
    assert rec["state"] == "tagged" and rec["sha"] == sha and rec["tag"] == "v1.3.0"
    assert rec["tagged_at"] and rec["pushed_at"] and rec["job"] == 901
    assert rec["ci"] == {
        "run_id": 500,
        "attempt": 1,
        "url": RUN_URL,
        "conclusion": "success",
    }
    comments = rel.store.comments(42)
    assert [c for c in comments if c.startswith("released:")] == [
        f"released: v1.3.0 {sha} (CI run 500)"
    ]
    assert not [c for c in comments if c.startswith("landed:")]
    assert rel.rig.lease.releases == [("catpath", 901)]
    assert "STATUS:done" not in rel.store.tag_strings(42)
    assert ctx.meta_set["release_tag"] == "v1.3.0"
    _clean(rel, ctx)


def test_red_run_is_recorded_charged_and_never_tagged(rel: Rel) -> None:
    """If this fails a red release gets a tag, is retried for free, leaves no
    `red` record (the next try would resume a bad commit), or the handoff does
    not name the run to look at."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha, conclusion="failure")]
    assert repo_release.poll(ctx, handle) is True
    _charged(ctx, rel.store)
    assert rel.tag_calls == [] and rel.rig.lease.releases == []
    rec = rel.record()
    assert rec["state"] == "red" and rec["sha"] == sha
    assert rec["ci"]["conclusion"] == "failure" and rec["ci"]["run_id"] == 500
    handoff = rel.store.comments(42)[-1]
    assert RUN_URL in handoff and "next try takes a new version" in handoff
    assert repo_release.COORDINATOR_ASK_KEY not in ctx.meta_set
    _clean(rel, ctx)


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [{"id": 7, "head_sha": "e" * 40}],
        [{"id": 8, "path": ".github/workflows/other.yml", "head_sha": "SHA"}],
        [
            {
                "id": 9,
                "path": ".github/workflows/ci.yml",
                "head_sha": "SHA",
                "status": "in_progress",
            }
        ],
        [
            {
                "id": 9,
                "path": ".github/workflows/ci.yml",
                "head_sha": "SHA",
                "status": "queued",
            }
        ],
    ],
)
def test_pending_or_foreign_runs_re_park_the_wait(rel: Rel, runs: list[Any]) -> None:
    """If this fails a run for another sha or another workflow is mistaken for
    ours (tagging on someone else's green), or a run still going is judged."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [
        {**r, "head_sha": sha if r.get("head_sha") == "SHA" else r.get("head_sha")}
        for r in runs
    ]
    handle["not_before"] = "2000-01-01T00:00:00Z"
    assert repo_release.poll(ctx, handle) is False
    assert ctx.status is None and rel.tag_calls == [] and rel.rerun_calls == []
    assert _parked(handle) and ctx.meta_set["compute_handle"] == handle
    assert rel.record()["state"] == "pushed"


def test_no_run_after_the_cutoff_is_the_coordinators_failure(rel: Rel) -> None:
    """If this fails a push whose CI event was dropped waits forever, is
    retried blindly, or does not reach the coordinator reader."""
    ctx, handle, sha = _in_ci(rel)
    assert repo_release.poll(ctx, handle) is False  # inside the cutoff: waits
    _due(handle)
    old = time.time() - repo_release.NO_RUN_CUTOFF_S - 60
    handle["pushed_at"] = datetime.fromtimestamp(old, UTC).isoformat()
    assert repo_release.poll(ctx, handle) is True
    _coordinator(ctx, rel.store, "no CI run appeared")
    assert rel.tag_calls == [] and rel.record()["state"] == "pushed"


def test_ci_wall_is_attempt_free_and_keeps_the_pushed_record(rel: Rel) -> None:
    """If this fails a CI that never settles holds the lease forever, or the
    give-up is charged / drops the record (the next job must resume the sha)."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha, status="in_progress", conclusion=None)]
    handle["ci_since"] = time.time() - repo_release.CI_WALL_S - 60
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_release.CI_WALL_CLASS)
    assert rel.record()["state"] == "pushed" and rel.tag_calls == []


def test_cancelled_run_waits_for_quiet_then_reruns_once_and_remembers(
    rel: Rel,
) -> None:
    """If this fails a cancelled run is re-run beside another run of the
    branch (cancelling it or being cancelled), is re-run twice for one cancel,
    the old completed attempt is judged again after the re-run, or the new
    attempt's green is not tagged."""
    ctx, handle, sha = _in_ci(rel)
    cancelled = _run(sha, conclusion="cancelled")
    rel.runs = [cancelled]
    other = _run("f" * 40, id=777, status="in_progress", conclusion=None)
    rel.branch_runs = [cancelled, other]
    assert repo_release.poll(ctx, handle) is False  # another run holds the branch
    assert rel.rerun_calls == [] and handle["reruns"] == 0 and _parked(handle)
    rel.branch_runs = [cancelled]
    _due(handle)
    assert repo_release.poll(ctx, handle) is False  # quiet: one re-run
    assert rel.rerun_calls == [500]
    assert handle["reruns"] == 1 and handle["rerun_attempt_before"] == 1
    assert _parked(handle) and ctx.status is None
    # the old attempt still reads completed/cancelled: pending, no second POST
    _due(handle)
    assert repo_release.poll(ctx, handle) is False
    assert rel.rerun_calls == [500] and handle["reruns"] == 1
    # the new attempt runs, then goes green: tagged
    rel.runs = [_run(sha, run_attempt=2, status="in_progress", conclusion=None)]
    _due(handle)
    assert repo_release.poll(ctx, handle) is False and rel.tag_calls == []
    rel.runs = [_run(sha, run_attempt=2)]
    _due(handle)
    assert repo_release.poll(ctx, handle) is True and ctx.status == "succeeded"
    assert rel.record()["ci"]["attempt"] == 2 and len(rel.tag_calls) == 1
    _clean(rel, ctx)


def test_the_third_cancel_is_the_coordinators_failure(rel: Rel) -> None:
    """If this fails a run that keeps being cancelled is re-run forever."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha, conclusion="cancelled", run_attempt=3)]
    handle["reruns"] = repo_release.MAX_RERUNS
    handle["rerun_attempt_before"] = 2
    assert repo_release.poll(ctx, handle) is True
    _coordinator(ctx, rel.store, "cancelled again after 2 re-runs")
    assert rel.rerun_calls == [] and rel.tag_calls == []


def test_a_refused_rerun_is_the_coordinators_failure(rel: Rel) -> None:
    """If this fails a token without Actions write retries a POST it can never
    make, or hides that a human must re-run the run."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha, conclusion="cancelled")]
    rel.rerun_results[:] = [(False, 403)]
    assert repo_release.poll(ctx, handle) is True
    _coordinator(ctx, rel.store, "Actions write")
    assert rel.rerun_calls == [500] and rel.record()["state"] == "pushed"
    _clean(rel, ctx)


def test_a_failed_rerun_post_is_retried_on_the_next_poll(rel: Rel) -> None:
    """If this fails a 5xx or a lost connection on the POST fails the job
    instead of trying again, or counts as a re-run."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha, conclusion="cancelled")]
    rel.rerun_results[:] = [(False, 502), (True, 201)]
    assert repo_release.poll(ctx, handle) is False
    assert handle["reruns"] == 0 and _parked(handle) and ctx.status is None
    _due(handle)
    assert repo_release.poll(ctx, handle) is False
    assert handle["reruns"] == 1 and rel.rerun_calls == [500, 500]


def test_a_403_after_an_unanswered_rerun_post_reads_as_requested(rel: Rel) -> None:
    """If this fails a re-run POST that was accepted but whose reply was lost
    ends in a false "token lacks Actions write" ask (GitHub answers 403 for a
    run that is already re-running); or a second 403 is excused too."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha, conclusion="cancelled")]
    rel.rerun_results[:] = [(False, 0), (False, 403), (False, 403)]
    assert repo_release.poll(ctx, handle) is False
    assert handle["reruns"] == 0 and handle["rerun_unsure"] is True
    _due(handle)
    assert repo_release.poll(ctx, handle) is False and ctx.status is None
    assert handle["reruns"] == 1 and handle["rerun_attempt_before"] == 1
    assert "rerun_unsure" not in handle
    # attempt 2 was cancelled as well: this 403 is a real refusal
    rel.runs = [_run(sha, conclusion="cancelled", run_attempt=2)]
    _due(handle)
    assert repo_release.poll(ctx, handle) is True
    _coordinator(ctx, rel.store, "Actions write")


def test_the_retry_after_a_red_release_counts_from_the_last_tagged_release(
    rel: Rel,
) -> None:
    """If this fails the try after a red release reads the untagged release
    commit as "no previous release" and cuts a minor for fixes only; or it
    trusts a carried tag the remote no longer has at that sha."""
    base = rel.tip()
    rel.tags["v1.2.0"] = base
    rel.commit("fix: first")
    ctx, handle, sha = _in_ci(rel)
    assert handle["version"] == "1.2.1" and rel.record()["prev_sha"] == base
    rel.runs = [_run(sha, conclusion="failure")]
    _g(rel.rig.clone, "push", "-q", "origin", f"{sha}:refs/heads/main")
    assert repo_release.poll(ctx, handle) is True
    red = rel.record()
    assert red["state"] == "red"
    _g(rel.rig.clone, "reset", "-q", "--hard", sha)
    rel.commit("fix: what CI caught")
    ctx2, handle2 = _prepared(rel)
    assert handle2["version"] == "1.2.2" and handle2["prev_tag"] == "v1.2.0"
    assert handle2["prev_sha"] == base
    body = _g(rel.rig.clone, "log", "-1", "--format=%b", handle2["sha"])
    assert "v1.2.0" in body and "what CI caught" in body
    # the carried tag moved on the remote: not trusted, back to a first release
    rel.store.refs[42].meta["repo_release"] = red
    rel.tags["v1.2.0"] = "f" * 40
    ctx3, handle3 = _prepared(rel)
    assert handle3["version"] == "1.3.0" and handle3["prev_tag"] is None


def test_a_404_on_the_runs_read_is_the_coordinators_failure(rel: Rel) -> None:
    """If this fails a token with no Actions read on a private repo is treated
    as a passing outage and the release waits for nothing."""
    ctx, handle, sha = _in_ci(rel)
    rel.read_error = GitHubUnreadable("GET u: HTTP 404")
    assert repo_release.poll(ctx, handle) is True
    _coordinator(ctx, rel.store, "lacks Actions read")
    assert rel.tag_calls == []


def test_an_unreadable_github_re_parks_then_gives_up_attempt_free(rel: Rel) -> None:
    """If this fails one bad read fails the release, a long outage waits for
    ever or is charged to the node, or a recovery does not reset the clock."""
    ctx, handle, sha = _in_ci(rel)
    rel.read_error = GitHubUnreadable("GET u: HTTP 502")
    assert repo_release.poll(ctx, handle) is False
    assert _parked(handle) and handle["unreadable_since"] is not None
    # recovery clears the outage clock
    rel.read_error = None
    rel.runs = [_run(sha, status="in_progress", conclusion=None)]
    _due(handle)
    assert repo_release.poll(ctx, handle) is False
    assert handle["unreadable_since"] is None
    # silent past the cap
    rel.read_error = GitHubUnreadable("GET u: HTTP 502")
    handle["unreadable_since"] = time.time() - repo_release.UNREADABLE_CAP_S - 30
    _due(handle)
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_release.CI_UNREADABLE_CLASS)
    assert rel.record()["state"] == "pushed" and rel.tag_calls == []
    assert repo_release.COORDINATOR_ASK_KEY not in ctx.meta_set


def test_tag_exists_elsewhere_is_a_charged_tag_conflict(rel: Rel) -> None:
    """If this fails a tag a human cut at another commit is retried for free
    or left without a `tag-conflict` record."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha)]
    rel.tag_outcomes[:] = [("exists", "d" * 40)]
    assert repo_release.poll(ctx, handle) is True
    _charged(ctx, rel.store)
    rec = rel.record()
    assert rec["state"] == "tag-conflict" and rec["sha"] == sha
    assert rec["ci"]["run_id"] == 500
    assert "released:" not in " ".join(rel.store.comments(42))
    assert rel.rig.lease.releases == []


def test_a_tag_error_is_attempt_free_and_the_record_stays_pushed(rel: Rel) -> None:
    """If this fails a tag push that may have landed loses the record (the
    next job would cut a second version), is charged, or leaks the token its
    helper's error text carried."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha)]
    rel.tag_outcomes[:] = [("error", f"push failed with {TOKEN}")]
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, repo_release.TAG_PUSH_CLASS)
    assert rel.record()["state"] == "pushed"
    _clean(rel, ctx)


@pytest.mark.parametrize("how", ["closed", "off", "lease-lost", "no-lease"])
def test_a_closed_repo_or_lost_lease_before_the_tag_tags_nothing(
    rel: Rel, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """If this fails a release tags after the operator closed the repo or
    another job took the lease while CI ran."""
    ctx, handle, sha = _in_ci(rel)
    rel.runs = [_run(sha)]
    red_class = repo_land.CLOSED_CLASS
    if how == "closed":
        monkeypatch.setattr(lanes, "open_repos", lambda store: frozenset({"precis"}))
    elif how == "off":
        monkeypatch.setenv("PRECIS_FACTORY", "off")
    else:
        rel.rig.lease.holder_job = 77 if how == "lease-lost" else None
        red_class = repo_land.HELD_CLASS
    assert repo_release.poll(ctx, handle) is True
    _attempt_free(ctx, rel.store, red_class)
    assert rel.tag_calls == [] and rel.record()["state"] == "pushed"


def test_kill_on_a_ci_handle_does_not_raise(rel: Rel) -> None:
    """If this fails the wall-clock or operator kill of a release parked in ci
    raises and the row is never reaped."""
    ctx, handle, _ = _in_ci(rel)
    repo_release.kill(ctx, handle)
    assert rel.signals == []


# ── the API base ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("remote", "want"),
    [
        (
            "https://github.com/retospect/catpath",
            "https://api.github.com/repos/retospect/catpath",
        ),
        (
            "https://github.com/retospect/catpath.git",
            "https://api.github.com/repos/retospect/catpath",
        ),
        (
            "https://github.com/retospect/cat.path/",
            "https://api.github.com/repos/retospect/cat.path",
        ),
    ],
)
def test_api_base_of_a_plain_github_remote(remote: str, want: str) -> None:
    """If this fails the workflow reads go to a different repo than the one
    the profile releases."""
    assert repo_release.api_base(remote) == want


@pytest.mark.parametrize(
    "remote",
    [
        "",
        "http://github.com/o/r",
        "https://gitlab.com/o/r",
        "https://user@github.com/o/r",
        "https://user:" + "tok@github.com/o/r",  # split: the credential scan
        "https://github.com:8443/o/r",
        "https://github.com.evil.example/o/r",
        "https://github.com/o",
        "https://github.com/o/r/tree/main",
        "https://github.com/o/..",
        "git@github.com:o/r.git",
        "ssh://git@github.com/o/r",
    ],
)
def test_api_base_refuses_anything_but_a_plain_github_https_remote(
    remote: str,
) -> None:
    """If this fails the GitHub token header can be aimed at another host, or
    a credentialed URL is accepted."""
    with pytest.raises(repo_release.ReleaseError):
        repo_release.api_base(remote)


def test_a_non_github_remote_is_refused_at_submit(
    rel: Rel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a profile with a non-GitHub remote gets a job that can
    only fail after its push."""
    monkeypatch.setitem(
        lanes.REPO_PROFILES,
        "catpath",
        replace(lanes.REPO_PROFILES["catpath"], remote="https://example.org/o/r"),
    )
    assert "plain https://github.com" in (
        repo_release.validate_submit(rel.store, params=PARAMS) or ""
    )


# ── the re-run POST ────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def read(self) -> bytes:
        return b""


def _fake_http(monkeypatch: pytest.MonkeyPatch, status: int) -> dict[str, Any]:
    """The pinned client and the stream replaced; records what a POST used."""
    import contextlib

    from precis.utils import http, safe_fetch
    from precis.workers import factory_git

    seen: dict[str, Any] = {}

    @contextlib.contextmanager
    def client(**kw: Any) -> Any:
        seen["client_headers"] = dict(kw.get("headers") or {})
        yield "client"

    @contextlib.contextmanager
    def stream(c: Any, method: str, url: str, /, **kw: Any) -> Any:
        seen["call"] = (method, url, kw)
        yield _Resp(status)

    def backoff(send: Any, **kw: Any) -> Any:
        seen["backoff"] = kw
        return send()

    monkeypatch.setattr(http, "http_client", client)
    monkeypatch.setattr(safe_fetch, "safe_stream", stream)
    monkeypatch.setattr(http, "with_backoff", backoff)
    monkeypatch.setattr(
        factory_git,
        "github_api_headers",
        lambda: {"Authorization": f"Bearer {TOKEN}"},
    )
    return seen


API = "https://api.github.com/repos/retospect/catpath"


def test_rerun_posts_through_safe_stream_non_idempotent_with_the_token_in_a_header(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If this fails the re-run is sent by a path that retries a POST that may
    have committed, skips the SSRF-pinned client, or puts the token anywhere
    but the client's headers."""
    seen = _fake_http(monkeypatch, 201)
    with caplog.at_level("DEBUG"):
        assert repo_release._rerun(API, 500) == (True, 201)
    method, url, kw = seen["call"]
    assert (method, url) == ("POST", f"{API}/actions/runs/500/rerun") and kw == {}
    assert seen["backoff"]["idempotent"] is False
    assert seen["backoff"]["service"] == "github"
    assert seen["client_headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in url and TOKEN not in caplog.text


@pytest.mark.parametrize(("status", "ok"), [(404, False), (422, False), (200, True)])
def test_rerun_reports_the_status(
    monkeypatch: pytest.MonkeyPatch, status: int, ok: bool
) -> None:
    """If this fails a 404 on the POST (returned, not raised, by the backoff)
    reads as success."""
    _fake_http(monkeypatch, status)
    assert repo_release._rerun(API, 500) == (ok, status)


def test_rerun_never_raises_and_reports_a_refusal_status(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If this fails a 403 (a VendorError from the backoff) or a transport
    error escapes the poll, or the log line carries more than the status."""
    from precis.utils import http

    _fake_http(monkeypatch, 201)

    def refused(send: Any, **kw: Any) -> Any:
        raise http.VendorError("github", "HTTP 403 - refused", status=403)

    monkeypatch.setattr(http, "with_backoff", refused)
    with caplog.at_level("DEBUG"):
        assert repo_release._rerun(API, 500) == (False, 403)

    def broken(send: Any, **kw: Any) -> Any:
        raise RuntimeError(f"boom {TOKEN}")

    monkeypatch.setattr(http, "with_backoff", broken)
    with caplog.at_level("DEBUG"):
        assert repo_release._rerun(API, 500) == (False, 0)
    assert TOKEN not in caplog.text


# ── the batch backstop ─────────────────────────────────────────────


def _node(
    rel: Rel,
    nid: int,
    *,
    tags: tuple[str, ...] = ("repo:catpath",),
    status: str = "open",
    meta: dict[str, Any] | None = None,
    parent: int | None = None,
) -> None:
    rel.store.add_todo(nid, status=status, tags=tags, meta=meta)
    rel.open_nodes.append({"id": nid, "parent_id": parent, "kind": "todo"})


REVIEWER = {"generate": {"role": "reviewer", "from": "td41"}}


@pytest.mark.parametrize(
    "meta", [{}, {"op": "run_slice"}, {"op": {"type": "run_slice"}}, REVIEWER]
)
def test_an_open_slice_or_reviewer_of_the_repo_holds_the_release(
    rel: Rel, meta: dict[str, Any]
) -> None:
    """If this fails a release is minted while a slice or reviewer of its
    batch is still open (a fix's reviewer has no edge to the release node)."""
    _node(rel, 43, meta=meta)
    refusal = repo_release.validate_submit(rel.store, params=PARAMS)
    assert refusal is not None and refusal.startswith("holding: ")
    assert "td43" in refusal and "catpath" in refusal


@pytest.mark.parametrize(
    "case",
    [
        "closed",
        "ask-meta",
        "ask-tag",
        "other-repo",
        "default-repo",
        "phase",
        "other-op",
    ],
)
def test_nodes_that_do_not_hold_the_release(rel: Rel, case: str) -> None:
    """If this fails a closed node, an ask, a node of another repo, a phase or
    a mechanical node blocks a release for nothing."""
    if case == "closed":
        _node(rel, 43, status="done")
    elif case == "ask-meta":
        _node(rel, 43, meta={"ask": {"blocks": "td42"}})
    elif case == "ask-tag":
        _node(rel, 43, tags=("repo:catpath", "ask-user"))
    elif case == "other-repo":
        _node(rel, 43, tags=("repo:precis",))
    elif case == "default-repo":
        _node(rel, 43, tags=())
    elif case == "phase":
        _node(rel, 43)  # a catpath phase whose only child is another repo's
        _node(rel, 44, tags=("repo:precis",), parent=43)
    else:
        _node(rel, 43, meta={"op": "repo_release"})
    assert repo_release.validate_submit(rel.store, params=PARAMS) is None


@pytest.mark.parametrize("retries", [None, 0, 2, "1"])
def test_a_release_node_without_retries_1_is_refused(rel: Rel, retries: Any) -> None:
    """If this fails a release node the dispatcher would retry blind (its
    default is two tries) is minted, and a failed release spends a second
    version before anyone is asked."""
    meta = rel.store.refs[42].meta
    meta.pop("retries")
    if retries is not None:
        meta["retries"] = retries
    refusal = repo_release.validate_submit(rel.store, params=PARAMS) or ""
    assert "meta.retries = 1" in refusal and "todo 42" in refusal


@pytest.mark.parametrize("when", ["parked", "verifying"])
def test_a_fix_opened_after_submit_stops_the_release_before_the_push(
    rel: Rel, when: str
) -> None:
    """If this fails a review's fix minted while the release waited for the
    lease or ran its check is released around: the push goes out without it."""
    if when == "parked":
        ctx, handle = _submitted(rel)
        _node(rel, 43)
        assert repo_release.poll(ctx, handle) is True
        assert rel.rig.lease.acquires == []
    else:
        ctx, handle = _prepared(rel)
        _finish(handle, 0)
        _node(rel, 43)
        assert repo_release.poll(ctx, handle) is True
        assert not rel.tree().exists()
    _attempt_free(ctx, rel.store, repo_release.BATCH_CLASS)
    assert rel.pushes == [] and "td43" in (ctx.failure or "")
    _no_pushed_record(rel)


def test_a_fix_opened_after_the_push_does_not_keep_the_release_from_its_tag(
    rel: Rel,
) -> None:
    """If this fails a pushed release commit is never tagged because a later
    fix reopened the batch."""
    ctx, handle, sha = _in_ci(rel)
    _g(rel.rig.clone, "push", "-q", "origin", f"{sha}:refs/heads/main")
    _node(rel, 43)
    # a later job (the first one died in its CI wait) resumes the pushed sha
    ctx2, handle2 = _submitted(rel)
    assert repo_release.poll(ctx2, handle2) is False, ctx2.failure
    assert handle2["phase"] == "ci" and handle2["sha"] == sha
    _due(handle2)
    rel.runs = [_run(sha)]
    assert repo_release.poll(ctx2, handle2) is True and ctx2.status == "succeeded"
    assert rel.record()["state"] == "tagged" and rel.tags["v1.3.0"] == sha


def test_a_slice_with_a_job_under_it_still_holds_the_release(rel: Rel) -> None:
    """If this fails a slice that was dispatched once (its job row hangs under
    it in the subtree) reads as a phase and the release goes out before it
    closes."""
    _node(rel, 43)
    rel.open_nodes.append({"id": 900, "parent_id": 43, "kind": "job"})
    refusal = repo_release.validate_submit(rel.store, params=PARAMS) or ""
    assert refusal.startswith("holding: ") and "td43" in refusal


def test_nodes_that_wait_on_the_release_do_not_hold_it(rel: Rel) -> None:
    """If this fails a later batch blocked-by this release (directly, through
    the pin-bump node, or as a child of a blocked phase) deadlocks it; or a
    node the release itself waits on stops holding."""
    _node(rel, 50, meta={"op": "pin_bump"}, tags=())  # blocked-by the release
    _node(rel, 51)  # a later slice, blocked-by the pin bump
    _node(rel, 52)  # a later phase, blocked-by the release
    _node(rel, 53, parent=52)  # its child slice
    rel.blocked.extend([(50, 42), (51, 50), (52, 42)])
    assert repo_release.validate_submit(rel.store, params=PARAMS) is None
    _node(rel, 54)  # upstream: the release is blocked-by it
    rel.blocked.append((42, 54))
    refusal = repo_release.validate_submit(rel.store, params=PARAMS) or ""
    assert refusal.startswith("holding: 1 ") and "td54" in refusal


def test_the_node_itself_and_nodes_inherit_the_nearest_tagged_ancestor(
    rel: Rel,
) -> None:
    """If this fails the release node holds itself, or a child of a
    repo-tagged parent with no tag of its own is read as the default repo."""
    assert repo_release.validate_submit(rel.store, params=PARAMS) is None  # itself only
    rel.store.add_todo(60, status="open", tags=("repo:catpath",))
    rel.open_nodes.append({"id": 60, "parent_id": None, "kind": "todo"})
    rel.store.add_todo(61, status="open", tags=())
    setattr(rel.store.refs[61], "parent_id", 60)  # a field the fake ref lacks
    rel.open_nodes.append({"id": 61, "parent_id": 60, "kind": "todo"})
    refusal = repo_release.validate_submit(rel.store, params=PARAMS) or ""
    assert refusal.startswith("holding: ") and "td61" in refusal
