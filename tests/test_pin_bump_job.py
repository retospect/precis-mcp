"""pin_bump, the job half (td659522 step 4 R2): submit through the commit.

Built on ``test_repo_land``'s rig and ``test_repo_release_job``'s style: a
real "precis" origin and clone in tmp holding the four pin files in their real
line shapes (``test_pin_bump``'s fixtures, with the shas swapped for real
ones), a real tmp "catpath" repo standing in for the dependency (the source
mirror is a local bare repo fetched from it), the tag oracle and the
credentialed fetches faked at the ``factory_git_mirror`` seam, and the
detached launch by a recorder that a test "finishes" by writing the exit and
result files. The fake token only ever appears in the env those seams get.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from precis.plans import lanes
from precis.store.types import Tag
from precis.workers import _mint, factory_git_mirror
from precis.workers.job_types import land_slice, repo_release, run_slice
from precis.workers.job_types import pin_bump as pb
from tests import test_pin_bump as tpb
from tests.test_land_slice import _Ctx, _Store
from tests.test_repo_land import Rig, _g, rig  # noqa: F401

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: the runs go through sh"
)

PIN, REL = 77, 78
PARAMS = {"todo_id": PIN, "release_todo": REL, "pin_repo": "catpath"}
TS = "%Y-%m-%dT%H:%M:%SZ"
ORIGIN = "https://github.com/fake-owner/precis"
REMOTE = "https://github.com/retospect/catpath"
# What the rig puts in the credentialed env: nothing else may ever carry it.
TOKEN = "ghp_" + "".join(chr(65 + (i * 7) % 26) for i in range(36))
KEY = "ab12" * 16
DERIVE_RED = "ERROR lock is stale\n✖ gate lock failed: uv lock --check\n"


class _PinStore(_Store):
    """The land_slice fake store plus the ``tx`` the land mint opens."""

    @contextmanager
    def tx(self) -> Iterator[None]:
        yield None


@dataclass
class Pin:
    rig: Rig
    store: _PinStore
    tmp: Path
    cat: Path
    old: str = ""
    new: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    unreadable: set[str] = field(default_factory=set)
    oracle: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    mirrors: list[tuple[str, str, Path, dict[str, str]]] = field(default_factory=list)
    mirror_fails: bool = False
    signals: list[tuple[int, int]] = field(default_factory=list)
    ctx: _Ctx | None = None
    # the land mint seam: every call, the live job per idem key, a forced error
    mints: list[dict[str, Any]] = field(default_factory=list)
    live: dict[str, int] = field(default_factory=dict)
    mint_error: Exception | None = None
    # the serialise scan's rows per job type: (job id, params.todo_id, terminal)
    jobs: dict[str, list[tuple[int, int | None, bool]]] = field(default_factory=dict)
    # the batch: plan rows, blocked-by edges, (slice, gripe) links
    rows: list[dict[str, Any]] = field(default_factory=list)
    edges: list[tuple[int, int]] = field(default_factory=list)
    links: list[tuple[int, int]] = field(default_factory=list)

    # -- the catpath stand-in -------------------------------------------
    def cat_commit(
        self,
        version: str,
        *,
        deps: tuple[str, ...] = ("numpy",),
        requires: str = ">=3.10",
        build: str = "hatchling",
        optional: str = "ase",
        note: str = "",
    ) -> str:
        """A commit on the catpath stand-in's current branch; its sha."""
        text = (
            f'[project]\nname = "autocatpath"\nversion = "{version}"\n'
            f"dependencies = {json.dumps(list(deps))}\n"
            f'requires-python = "{requires}"\n\n'
            f'[project.optional-dependencies]\nextra = ["{optional}"]\n\n'
            f'[build-system]\nrequires = ["{build}"]\nbuild-backend = "x"\n\n'
            f'[tool.note]\ntext = "{note}"\n'
        )
        (self.cat / "pyproject.toml").write_text(text, encoding="utf-8")
        _g(self.cat, "add", "-A")
        _g(self.cat, "commit", "-q", "-m", f"release {version} {note}")
        return _g(self.cat, "rev-parse", "HEAD")

    def release(self, sha: str, version: str = "0.50.0", **over: Any) -> None:
        """Tag ``version`` at ``sha`` and stamp the release node's record."""
        tag = f"v{version}"
        self.tags[tag] = sha
        record = {
            "repo": "catpath",
            "package": "autocatpath",
            "version": version,
            "tag": tag,
            "sha": sha,
            "state": "tagged",
        }
        self.store.refs[REL].meta["repo_release"] = {**record, **over}

    def cut(self, version: str = "0.50.0", **kw: Any) -> str:
        sha = self.cat_commit(version, **kw)
        self.release(sha, version)
        return sha

    # -- precis ----------------------------------------------------------
    def precis_commit(self, files: Mapping[str, str | None]) -> str:
        """A commit on the clone's main (``None`` removes the file), pushed."""
        clone = self.rig.clone
        for name, text in files.items():
            path = clone / name
            if text is None:
                _g(clone, "rm", "-q", "--", name)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            _g(clone, "add", "--", name)
        _g(clone, "commit", "-q", "-m", f"chore: move {time.monotonic_ns()}")
        _g(clone, "push", "-q", "origin", "main")
        return _g(clone, "rev-parse", "HEAD")

    def tip(self) -> str:
        return _g(self.rig.origin, "rev-parse", "refs/heads/main")

    def texts(self, version: str, sha: str) -> dict[str, str]:
        """The four precis files fully moved to ``version`` / ``sha``."""
        base = self.base_texts()
        return {
            "pyproject.toml": pb.set_pyproject_pin(
                base["pyproject.toml"], "autocatpath", version=version, sha=sha
            ),
            "uv.lock": pb.set_lock_pin(
                base["uv.lock"],
                "autocatpath",
                version=version,
                sha=sha,
                old_sha=self.old,
            ),
            "deploy/group_vars/all.yml": pb.set_group_vars_floor(
                base["deploy/group_vars/all.yml"], "autocatpath_min_version", version
            ),
            "tests/test_coverage_job.py": pb.set_anchor_literals(
                base["tests/test_coverage_job.py"], key="e" * 64, engine=version
            ),
        }

    def base_texts(self) -> dict[str, str]:
        return {
            "pyproject.toml": tpb.PYPROJECT.replace(tpb.OLD, self.old),
            "uv.lock": tpb.LOCK.replace(tpb.OLD, self.old),
            "deploy/group_vars/all.yml": tpb.GROUP_VARS,
            "tests/test_coverage_job.py": tpb.ANCHORS,
        }

    # -- the job ---------------------------------------------------------
    def tree(self) -> Path:
        return self.rig.clone / ".claude" / "worktrees" / "pin-td77"

    def record(self) -> Any:
        return self.store.refs[PIN].meta.get("pin_bump")

    def branch_head(self) -> str | None:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/slice/77"],
            cwd=str(self.rig.clone),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None

    def run_dir(self) -> Path:
        return run_slice.run_dir(PIN)


@pytest.fixture
def pin(
    rig: Rig,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Pin:
    store = _PinStore()
    store.add_todo(PIN, tags=("repo:precis",))
    store.add_todo(REL, tags=("repo:catpath",))
    cat = tmp_path / "catpath-src"
    _g(tmp_path, "init", "-q", "-b", "main", str(cat))
    p = Pin(rig, store, tmp_path, cat)
    p.old = p.cat_commit("0.49.0")
    p.precis_commit(p.base_texts())
    p.new = p.cut("0.50.0")
    profile = replace(lanes.REPO_PROFILES["catpath"], pinner="pin_bump")
    monkeypatch.setitem(lanes.REPO_PROFILES, "catpath", profile)
    monkeypatch.setattr(
        lanes, "open_repos", lambda store: frozenset({"precis", "catpath"})
    )
    monkeypatch.setenv(factory_git_mirror.ORIGIN_URL_ENV, ORIGIN)

    def ls_remote_tag(url: str, tag: str, *, env: Any) -> str | None:
        assert url == REMOTE
        p.oracle.append((tag, dict(env)))
        return None if tag in p.unreadable else p.tags.get(tag, "")

    def fetch_source_mirror(
        url: str, ref: str, *, mirror: Path, env: Any
    ) -> str | None:
        p.mirrors.append((url, ref, mirror, dict(env)))
        if p.mirror_fails:
            return None
        mirror.parent.mkdir(parents=True, exist_ok=True)
        _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(mirror))
        _g(mirror, "fetch", "-q", str(cat), "+refs/heads/main:refs/heads/main")
        return _g(mirror, "rev-parse", "refs/heads/main")

    def mint_job_once(store: Any, **kw: Any) -> tuple[int, bool]:
        if p.mint_error is not None:
            raise p.mint_error
        p.mints.append(kw)
        key = kw["idem_key"]
        if key in p.live:
            return p.live[key], True
        p.live[key] = 5000 + len(p.mints)
        return p.live[key], False

    monkeypatch.setattr(_mint, "mint_job_once", mint_job_once)
    monkeypatch.setattr(pb, "_recent_jobs", lambda store, jt: p.jobs.get(jt, []))
    monkeypatch.setattr(repo_release, "_plan_rows", lambda store, root: p.rows)
    monkeypatch.setattr(repo_release, "_blocked_edges", lambda store, ids: p.edges)
    monkeypatch.setattr(
        pb, "_gripe_links", lambda store, ids: [x for x in p.links if x[0] in ids]
    )
    monkeypatch.setattr(factory_git_mirror, "ls_remote_tag", ls_remote_tag)
    monkeypatch.setattr(factory_git_mirror, "fetch_source_mirror", fetch_source_mirror)
    monkeypatch.setattr(
        repo_release, "_auth_env", lambda: {"PATH": "/bin", "GH_FAKE": TOKEN}
    )
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: False)
    monkeypatch.setattr(pb, "REAP_GRACE_S", 0)

    def no_docker() -> Any:
        raise AssertionError("a test reached the real docker image inspect")

    monkeypatch.setattr(pb, "_inspect_gate_image", no_docker)
    monkeypatch.setattr(
        run_slice, "_signal_group", lambda pgid, sig: p.signals.append((pgid, int(sig)))
    )
    return p


def _ctx(pin: Pin, **params: Any) -> _Ctx:
    ctx = _Ctx(pin.store, {**PARAMS, **params})
    pin.ctx = ctx
    return ctx


def _submitted(pin: Pin, **params: Any) -> tuple[_Ctx, dict[str, Any]]:
    ctx = _ctx(pin, **params)
    handle = pb.submit(ctx, None)
    assert isinstance(handle, dict), ctx.failure
    return ctx, handle


def _derived(pin: Pin, **params: Any) -> tuple[_Ctx, dict[str, Any]]:
    """A job past submit and the resolve poll: the derive is launched."""
    ctx, handle = _submitted(pin, **params)
    assert pb.poll(ctx, handle) is False, ctx.failure
    assert handle["phase"] == "derive", (handle, ctx.failure)
    return ctx, handle


def _finish(handle: dict[str, Any], code: int, log_text: str = "") -> None:
    Path(handle["log"]).write_text(log_text, encoding="utf-8")
    Path(handle["exit_file"]).write_text(f"{code}\n", encoding="utf-8")


def _result(pin: Pin, data: Any, name: str = pb.RESULT_FILE) -> None:
    text = data if isinstance(data, str) else json.dumps(data)
    (pin.run_dir() / name).write_text(text, encoding="utf-8")


def _good_result(pin: Pin, key: str = KEY, engine: str = "0.50.0") -> None:
    _result(pin, {"key": key, "engine": engine})


def _verify_result(pin: Pin, key: str = KEY, engine: str = "0.50.0") -> None:
    """What the verify run's own derive step writes."""
    _result(pin, {"key": key, "engine": engine}, pb.VERIFY_RESULT_FILE)


def _verifying(pin: Pin, key: str = KEY) -> tuple[_Ctx, dict[str, Any]]:
    ctx, handle = _derived(pin)
    _good_result(pin, key)
    _finish(handle, 0)
    assert pb.poll(ctx, handle) is False, ctx.failure
    assert handle["phase"] == "verify", (handle, ctx.failure)
    return ctx, handle


def _committed(pin: Pin) -> tuple[_Ctx, dict[str, Any]]:
    """A job through the verify run: parked in ``handoff``."""
    ctx, handle = _verifying(pin)
    _verify_result(pin)
    _finish(handle, 0, "5 passed\n")
    assert pb.poll(ctx, handle) is False, ctx.failure
    assert handle["phase"] == "handoff", (handle, ctx.failure)
    return ctx, handle


def _attempt_free(ctx: _Ctx, red_class: str) -> None:
    assert ctx.status == "failed", ctx.failure
    assert ctx.meta_set[run_slice.ATTEMPT_FREE_KEY] == "infra-pin-bump"
    assert ctx.meta_set["red_class"] == red_class
    assert pb.COORDINATOR_ASK_KEY not in ctx.meta_set


def _charged(ctx: _Ctx) -> None:
    assert ctx.status == "failed", ctx.failure
    assert run_slice.ATTEMPT_FREE_KEY not in ctx.meta_set


def _asks(ctx: _Ctx, needle: str) -> None:
    """Charged, with a coordinator ask naming ``needle``."""
    _charged(ctx)
    assert needle in ctx.meta_set[pb.COORDINATOR_ASK_KEY], ctx.meta_set


def _nothing_left(pin: Pin) -> None:
    """The failure left no pin tree, branch, building record or launch."""
    assert pin.branch_head() is None
    assert not pin.tree().exists()
    assert pin.record() is None
    assert pin.store.tag_strings(PIN) == {"STATUS:doing", "repo:precis"}
    assert pin.store.comments(PIN)[-1].startswith("handoff: pin bump FAILED:")


def _clean(pin: Pin, ctx: _Ctx) -> None:
    """No job output carries the token, and the oracle did see it (so the
    check is not vacuous)."""
    assert pin.oracle and TOKEN in pin.oracle[0][1].values()
    blobs = [
        json.dumps(ctx.meta_set, default=str),
        str(ctx.failure),
        json.dumps(ctx.chunks),
        json.dumps(pin.store.refs[PIN].meta, default=str),
        json.dumps(pin.store.refs[REL].meta, default=str),
        "\n".join(pin.store.comments(PIN) + pin.store.comments(REL)),
        json.dumps(
            [(a, {k: str(v) for k, v in kw.items()}) for a, kw in pin.rig.launches]
        ),
    ]
    for blob in blobs:
        assert TOKEN not in blob
    for run_dir_file in pin.run_dir().iterdir():
        if run_dir_file.is_file():
            assert TOKEN not in run_dir_file.read_text(
                encoding="utf-8", errors="replace"
            )


def _due_now(handle: dict[str, Any]) -> None:
    handle["not_before"] = "2000-01-01T00:00:00Z"


def _show(pin: Pin, rev: str, path: str) -> str:
    """``path`` at ``rev`` byte for byte (``_g`` strips the last newline)."""
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=str(pin.rig.clone),
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode("utf-8")


# ── submit and admission ───────────────────────────────────────────


def test_submit_writes_nothing_and_returns_a_parked_resolve_handle(pin: Pin) -> None:
    """If this fails submit fetches, mirrors, cuts a tree or writes the node
    before the worker polls, or its parked handle is not the shape poll reads."""
    ctx, handle = _submitted(pin)
    assert handle["op"] == "pin_bump" and handle["phase"] == "resolve"
    assert handle["pin_repo"] == "catpath" and handle["release_todo"] == REL
    due = datetime.strptime(handle["not_before"], TS).replace(tzinfo=UTC)
    assert abs((due - datetime.now(UTC)).total_seconds()) < 5
    assert "pid" not in handle and "sha" not in handle
    assert ctx.meta_set["compute_handle"] is handle
    assert ctx.meta_set["compute_handle_released"] is False
    assert ctx.meta_set["deadline"] == handle["deadline"] > time.time()
    assert ctx.failure is None and ctx.status is None and ctx.chunks == []
    assert pin.rig.fetches == [] and pin.rig.launches == []
    assert pin.oracle == [] and pin.mirrors == []
    assert pin.store.comments(PIN) == [] and pin.record() is None
    assert not pin.tree().exists() and pin.branch_head() is None


def test_a_poll_before_not_before_does_nothing(pin: Pin) -> None:
    """If this fails a parked resolve handle works before it is due."""
    ctx, handle = _submitted(pin)
    handle["not_before"] = "2999-01-01T00:00:00Z"
    assert pb.poll(ctx, handle) is False
    assert pin.rig.fetches == [] and pin.oracle == []


def _off(m: pytest.MonkeyPatch) -> str:
    m.setenv("PRECIS_FACTORY", "off")
    return "factory is off"


def _no_pinner(m: pytest.MonkeyPatch) -> str:
    m.setitem(
        lanes.REPO_PROFILES,
        "catpath",
        replace(lanes.REPO_PROFILES["catpath"], pinner=None),
    )
    return "names no pinner"


def _closed(m: pytest.MonkeyPatch) -> str:
    m.setattr(lanes, "open_repos", lambda store: frozenset({"precis"}))
    return "does not list"


@pytest.mark.parametrize("case", [_off, _no_pinner, _closed])
def test_a_job_that_may_not_run_is_refused_at_submit(
    pin: Pin, monkeypatch: pytest.MonkeyPatch, case: Any
) -> None:
    """If this fails submit admits a pin bump with the factory off, for a repo
    whose profile names no pinner, or one factory.open_repos does not list."""
    needle = case(monkeypatch)
    ctx = _ctx(pin)
    assert pb.submit(ctx, None) is None
    assert needle in str(ctx.failure) and ctx.status == "failed"
    assert pb.validate_submit(pin.store, params=dict(PARAMS)) is not None


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"todo_id": 999}, "not found"),
        ({"todo_id": 0}, "positive integer"),
        ({"release_todo": 999}, "not found"),
        ({"release_todo": 0}, "positive integer"),
        ({"release_todo": True}, "positive integer"),
        ({"pin_repo": ""}, "no params.pin_repo"),
        ({"pin_repo": "nowhere"}, "names no pinner"),
        ({"pin_repo": "precis"}, "names no pinner"),
        ({"extra": 1}, "unknown params"),
    ],
)
def test_bad_params_are_refused(pin: Pin, over: dict[str, Any], needle: str) -> None:
    """If this fails a bad param set (missing todos, a non-pinner repo, an
    extra key) reaches the poll."""
    assert needle in str(pb.validate_submit(pin.store, params={**PARAMS, **over}))
    ctx = _ctx(pin, **over)
    assert pb.submit(ctx, None) is None and ctx.status == "failed"


def test_a_closed_pin_node_or_a_wontdo_release_node_is_refused(pin: Pin) -> None:
    """If this fails a pin bump runs for a closed node or for a release node
    that was abandoned; a done release node is fine."""
    pin.store.add_todo(79, status="won't-do")
    assert "closed" in str(
        pb.validate_submit(pin.store, params={**PARAMS, "release_todo": 79})
    )
    assert "closed" in str(
        pb.validate_submit(pin.store, params={**PARAMS, "todo_id": 79})
    )
    pin.store.add_todo(80, status="done")
    assert pb.validate_submit(pin.store, params={**PARAMS, "release_todo": 80}) is None


def test_no_real_profile_names_the_pinner_yet() -> None:
    """If this fails a real profile now names pin_bump as its pinner: the
    switch (R7) has landed, and this guard must go with it."""
    assert [n for n, p in lanes.REPO_PROFILES.items() if p.pinner == pb.NAME] == []
    assert "names no pinner" in str(
        pb.validate_submit(None, params={**PARAMS, "pin_repo": "catpath"})
    )


def test_spec_declares_three_required_params() -> None:
    """If this fails the schema admits other params than the three."""
    assert pb.SPEC.name == "pin_bump"
    assert pb.PARAMS_SCHEMA["required"] == ["todo_id", "release_todo", "pin_repo"]
    assert pb.PARAMS_SCHEMA["additionalProperties"] is False


# ── resolve refusals ───────────────────────────────────────────────


def _no_record(p: Pin) -> None:
    del p.store.refs[REL].meta["repo_release"]


def _untagged(p: Pin) -> None:
    p.store.refs[REL].meta["repo_release"]["state"] = "pushed"


def _other_repo(p: Pin) -> None:
    p.store.refs[REL].meta["repo_release"]["repo"] = "precis"


def _other_package(p: Pin) -> None:
    p.store.refs[REL].meta["repo_release"]["package"] = "othereng"


def _bad_version(p: Pin) -> None:
    p.store.refs[REL].meta["repo_release"]["version"] = "0.50"


def _tag_mismatch(p: Pin) -> None:
    p.store.refs[REL].meta["repo_release"]["tag"] = "v9.9.9"


def _bad_sha(p: Pin) -> None:
    p.store.refs[REL].meta["repo_release"]["sha"] = "not-a-sha"


def _tag_elsewhere(p: Pin) -> None:
    p.tags["v0.50.0"] = p.old


def _tag_absent(p: Pin) -> None:
    p.tags.clear()


def _unpinned(p: Pin) -> None:
    p.precis_commit({"pyproject.toml": '[project]\nname = "demo"\nversion = "1"\n'})


def _off_main(p: Pin) -> None:
    _g(p.cat, "checkout", "-q", "-b", "side", p.old)
    side = p.cat_commit("0.50.0", note="side")
    _g(p.cat, "checkout", "-q", "main")
    p.release(side)


def _version_differs(p: Pin) -> None:
    p.release(p.new, "0.50.1")


def _below_the_floor(p: Pin) -> None:
    p.cut("0.40.0")


@pytest.mark.parametrize(
    ("case", "needle"),
    [
        (_no_record, "carries no release record"),
        (_untagged, "is not tagged"),
        (_other_repo, "another repo"),
        (_other_package, "another package"),
        (_bad_version, "X.Y.Z"),
        (_tag_mismatch, "no matching tag"),
        (_bad_sha, "no matching tag"),
        (_tag_elsewhere, "not at the release sha"),
        (_tag_absent, "absent"),
        (_unpinned, "does not pin"),
        (_off_main, "not an ancestor"),
        (_version_differs, "does not say autocatpath 0.50.1"),
        (_below_the_floor, "below precis's floor"),
    ],
)
def test_a_release_that_cannot_be_pinned_is_a_charged_coordinator_ask(
    pin: Pin, case: Any, needle: str
) -> None:
    """If this fails a missing or untagged release record, a record for the
    wrong repo or package, a tag the oracle does not return, a sha off the
    dependency's main, a version its pyproject does not carry, or a version
    below the floor is built anyway, or is retried blind (no ask)."""
    case(pin)
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _asks(ctx, needle)
    _nothing_left(pin)
    assert pin.rig.launches == []
    _clean_if_oracle(pin, ctx)


def _clean_if_oracle(pin: Pin, ctx: _Ctx) -> None:
    if pin.oracle:
        _clean(pin, ctx)


def test_a_missing_precis_file_is_charged_without_an_ask(pin: Pin) -> None:
    """If this fails a pin file absent at the tip is retried as
    infrastructure, or read as a coordinator question."""
    pin.precis_commit({"deploy/group_vars/all.yml": None})
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _charged(ctx)
    assert pb.COORDINATOR_ASK_KEY not in ctx.meta_set
    assert "deploy/group_vars/all.yml" in str(ctx.failure)
    _nothing_left(pin)


def test_an_unreadable_tag_oracle_is_attempt_free(pin: Pin) -> None:
    """If this fails a GitHub that cannot be read is charged to the bump."""
    pin.unreadable.add("v0.50.0")
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "tag-oracle")
    _nothing_left(pin)


def test_a_failed_source_mirror_is_attempt_free(pin: Pin) -> None:
    """If this fails a mirror fetch that did not work is charged."""
    pin.mirror_fails = True
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "pin-source")
    _nothing_left(pin)


def test_a_failed_precis_fetch_is_attempt_free(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails an unfetchable precis origin is charged."""
    monkeypatch.setattr(factory_git_mirror, "fetch_ref_into", lambda *a, **k: None)
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "origin-fetch")
    assert pin.oracle == []
    _nothing_left(pin)


def test_the_precis_tip_is_fetched_from_the_trusted_origin_url(pin: Pin) -> None:
    """If this fails the precis fetch reads its URL from anywhere but the
    pinned origin."""
    _derived(pin)
    assert pin.rig.fetches == [ORIGIN]


def test_the_oracle_and_the_mirror_fetch_get_the_credentialed_env(pin: Pin) -> None:
    """If this fails the token no longer reaches the helpers that need it (so
    the no-token checks elsewhere would pass vacuously), or the mirror is not
    a job-owned path under the mirror root."""
    _derived(pin)
    assert pin.oracle[0][0] == "v0.50.0" and TOKEN in pin.oracle[0][1].values()
    url, ref, mirror, env = pin.mirrors[0]
    assert (url, ref) == (REMOTE, "main") and TOKEN in env.values()
    root = factory_git_mirror.mirror_root(pin.rig.clone)
    assert mirror == root / "pin-td77.git"


# ── dependency drift, no-op, past, half-moved ──────────────────────


@pytest.mark.parametrize(
    "drift",
    [
        {"deps": ("numpy", "scipy")},
        {"requires": ">=3.11"},
        {"build": "setuptools"},
        {"optional": "rdkit"},
    ],
)
def test_a_moved_dependency_set_needs_a_human_uv_lock(
    pin: Pin, drift: dict[str, Any]
) -> None:
    """If this fails a release that changed catpath's dependencies,
    optional dependencies, requires-python or build system is text-edited into
    a lock that would be wrong."""
    pin.cut("0.50.0", **drift)
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _asks(ctx, "uv lock")
    _nothing_left(pin)
    assert pin.rig.launches == []


def test_changes_that_do_not_touch_the_resolution_inputs_pass(pin: Pin) -> None:
    """If this fails the drift check refuses on any pyproject change, not
    just the four resolution inputs."""
    pin.cut("0.50.0", note="only a tool table changed")
    _derived(pin)


def test_an_old_pinned_sha_the_mirror_lacks_counts_as_drift(pin: Pin) -> None:
    """If this fails a pinned sha that is not in the mirror is waved through
    the drift check."""
    unknown = "ab" * 20
    files = pin.base_texts()
    pin.precis_commit(
        {
            k: v.replace(pin.old, unknown)
            for k, v in files.items()
            if k in ("pyproject.toml", "uv.lock")
        }
    )
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _asks(ctx, "uv lock")


def test_a_pin_that_already_carries_the_release_is_a_no_op_success(pin: Pin) -> None:
    """If this fails a coherent pin is rebuilt, or the no-op leaves a tree, a
    branch or a launch behind, or does not park in the final phase."""
    pin.precis_commit(pin.texts("0.50.0", pin.new))
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is False
    assert ctx.failure is None and ctx.status is None
    assert handle["phase"] == "handoff" and handle["noop"] is True
    rec = pin.record()
    assert rec["state"] == "committed" and rec["noop"] is True
    assert rec["commit"] is None and rec["branch"] is None and rec["worktree"] is None
    assert (rec["version"], rec["sha"], rec["tag"]) == ("0.50.0", pin.new, "v0.50.0")
    assert (rec["from_version"], rec["from_sha"]) == ("0.50.0", pin.new)
    assert pin.rig.launches == [] and pin.branch_head() is None
    assert not pin.tree().exists()
    assert pin.store.comments(PIN) == []
    _clean(pin, ctx)


def test_a_pin_already_past_the_release_is_a_no_op_not_a_step_back(pin: Pin) -> None:
    """If this fails an older release moves the pin down, or a release that
    is behind the pin is a coordinator question."""
    newer = pin.cut("0.51.0")
    pin.precis_commit(pin.texts("0.51.0", newer))
    pin.release(pin.new, "0.50.0")
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is False
    assert handle["phase"] == "handoff"
    rec = pin.record()
    assert rec["noop"] is True and "already past" in rec["note"]
    assert pin.rig.launches == [] and pin.branch_head() is None


def test_a_half_moved_pin_is_finished_leaving_the_moved_file_alone(pin: Pin) -> None:
    """If this fails a pin with only its pyproject moved is refused or
    re-edited byte-differently, or the commit does not finish the other three
    files."""
    moved = pb.set_pyproject_pin(
        pin.base_texts()["pyproject.toml"], "autocatpath", version="0.50.0", sha=pin.new
    )
    tip = pin.precis_commit({"pyproject.toml": moved})
    ctx, handle = _committed(pin)
    commit = handle["commit"]
    assert pin.record()["from_sha"] == pin.new
    changed = _g(pin.rig.clone, "diff", "--name-only", tip, commit).splitlines()
    assert changed == sorted(
        ["deploy/group_vars/all.yml", "tests/test_coverage_job.py", "uv.lock"]
    )
    assert _show(pin, commit, "pyproject.toml") == moved
    full = pin.texts("0.50.0", pin.new)
    assert _show(pin, commit, "uv.lock") == full["uv.lock"]
    assert (
        _show(pin, commit, "deploy/group_vars/all.yml")
        == full["deploy/group_vars/all.yml"]
    )


# ── tree, edit and the derive launch ───────────────────────────────


def test_the_tree_holds_the_three_edits_uncommitted_on_the_slice_branch(
    pin: Pin,
) -> None:
    """If this fails the worktree and branch are not what land_slice expects
    for params {todo_id, worktree, branch}, or the edits are committed before
    the derive, or the anchors were touched early."""
    tip = pin.tip()
    ctx, handle = _derived(pin)
    tree = pin.tree()
    assert tree.is_dir() and pin.branch_head() == tip
    assert tree.parent == run_slice.worktrees_dir(pin.rig.clone)
    assert _g(tree, "rev-parse", "--abbrev-ref", "HEAD") == "slice/77"
    modified = _g(tree, "status", "--porcelain").splitlines()
    assert sorted(line.split()[-1] for line in modified) == [
        "deploy/group_vars/all.yml",
        "pyproject.toml",
        "uv.lock",
    ]
    full = pin.texts("0.50.0", pin.new)
    for path in ("pyproject.toml", "uv.lock", "deploy/group_vars/all.yml"):
        assert (tree / path).read_text(encoding="utf-8") == full[path]
    assert (tree / "tests/test_coverage_job.py").read_text(
        encoding="utf-8"
    ) == tpb.ANCHORS
    building = pin.record()
    assert building["state"] == "building" and building["branch"] == "slice/77"
    assert handle["made"] is True and handle["tip"] == tip
    assert (handle["from_version"], handle["from_sha"]) == ("0.49.0", pin.old)
    # a landing for this branch would be params {todo_id, worktree, branch}
    assert Path(handle["log"]).parent == pin.run_dir()
    _clean(pin, ctx)


def test_the_derive_runs_credential_free_against_the_job_mirror(pin: Pin) -> None:
    """If this fails the derive launch carries a credential, misses the
    insteadOf rewrite of either URL form, runs with ambient git config or
    prompts, or puts its venv inside the tree."""
    ctx, handle = _derived(pin)
    assert len(pin.rig.launches) == 1
    argv, kw = pin.rig.launches[0]
    script = Path(argv[1])
    assert argv[0] == "/bin/sh" and script.parent == pin.run_dir()
    assert kw["cwd"] == pin.tree()
    env = kw["env"]
    assert TOKEN not in json.dumps(env) and TOKEN not in script.read_text(
        encoding="utf-8"
    )
    assert not any(
        land_slice._LAND_ENV_SECRET_RE.search(k)
        for k in env
        if not k.startswith("GIT_CONFIG_")
    )
    assert "GH_FAKE" not in env and "GH_TOKEN" not in env
    mirror = pin.mirrors[0][2]
    count = int(env["GIT_CONFIG_COUNT"])
    rewrites = {
        env[f"GIT_CONFIG_VALUE_{i}"]: env[f"GIT_CONFIG_KEY_{i}"] for i in range(count)
    }
    assert rewrites == {
        REMOTE + ".git": f"url.{mirror}.insteadOf",
        REMOTE: f"url.{mirror}.insteadOf",
    }
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    venv = Path(env["UV_PROJECT_ENVIRONMENT"])
    assert venv.parent == pin.run_dir() and pin.tree() not in venv.parents
    assert not any(k.startswith("PRECIS_") for k in env)
    text = script.read_text(encoding="utf-8")
    assert "uv lock --check" in text and "uv sync --frozen --extra catalyst" in text
    assert "uv run --frozen --extra catalyst python -I" in text
    derive_py = pin.run_dir() / pb.DERIVE_PY
    assert derive_py.read_text(encoding="utf-8") == pb.DERIVE_SCRIPT_PY
    assert str(derive_py) in text and "0.50.0" in text
    assert str(pin.tree()) not in str(derive_py)
    assert handle["pid"] == 4242 and handle["pgid"] == 4242
    assert handle["deadline"] > time.time()
    _clean(pin, ctx)


def test_a_running_derive_keeps_waiting(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a derive that is still running is judged, or the poll
    does not keep the handle persisted."""
    ctx, handle = _derived(pin)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: True)
    assert pb.poll(ctx, handle) is False
    assert ctx.status is None and ctx.meta_set["compute_handle"] is handle


# ── the derive's end ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "result",
    [
        None,
        "not json",
        "[]",
        {"key": KEY},
        {"key": KEY, "engine": "0.50.0", "extra": 1},
        {"key": KEY.upper(), "engine": "0.50.0"},
        {"key": KEY[:-1], "engine": "0.50.0"},
        {"key": KEY + "0", "engine": "0.50.0"},
        {"key": "z" * 64, "engine": "0.50.0"},
        {"key": 12, "engine": "0.50.0"},
        {"key": KEY, "engine": "0.49.0"},
        {"key": KEY, "engine": None},
        "x" * 2000,
    ],
)
def test_a_result_that_is_not_a_64_hex_key_for_the_target_is_charged_and_uncommitted(
    pin: Pin, result: Any
) -> None:
    """If this fails anything but exactly a lowercase 64-hex key and the
    target engine is accepted from the attacker-writable result file, or a
    commit exists after a refusal."""
    tip = pin.tip()
    ctx, handle = _derived(pin)
    if result is not None:
        _result(pin, result)
    _finish(handle, 0)
    assert pb.poll(ctx, handle) is True
    _charged(ctx)
    assert "64-hex" in str(ctx.failure)
    assert pin.tip() == tip
    _nothing_left(pin)
    assert len(pin.rig.launches) == 1  # no verify run


def test_a_green_derive_commits_the_four_files_and_launches_the_verify(
    pin: Pin,
) -> None:
    """If this fails the commit is not exactly the four files on the tip, the
    anchors do not carry the derived key and engine, the identity or subject
    is wrong, or the verify run is not the pin tests in the committed tree."""
    tip = pin.tip()
    ctx, handle = _verifying(pin)
    commit = handle["commit"]
    clone = pin.rig.clone
    assert pin.branch_head() == commit
    assert _g(clone, "rev-list", "--parents", "-n", "1", commit) == f"{commit} {tip}"
    assert sorted(_g(clone, "diff", "--name-only", tip, commit).splitlines()) == sorted(
        pb.PIN_PATHS
    )
    assert _g(clone, "log", "-1", "--format=%s", commit) == (
        "build(deps): bump autocatpath to v0.50.0 (td77)"
    )
    body = _g(clone, "log", "-1", "--format=%b", commit)
    assert (
        "v0.50.0" in body and pin.new in body and f"td{REL}" in body and pin.old in body
    )
    assert _g(clone, "log", "-1", "--format=%an <%ae>", commit) == (
        "precis factory <factory-release@users.noreply.github.com>"
    )
    full = pin.texts("0.50.0", pin.new)
    for path in ("pyproject.toml", "uv.lock", "deploy/group_vars/all.yml"):
        assert _show(pin, commit, path) == full[path]
    anchors = _show(pin, commit, "tests/test_coverage_job.py")
    assert anchors == pb.set_anchor_literals(tpb.ANCHORS, key=KEY, engine="0.50.0")
    assert pb.pin_is_coherent(
        _show(pin, commit, "pyproject.toml"),
        _show(pin, commit, "uv.lock"),
        _show(pin, commit, "deploy/group_vars/all.yml"),
        "autocatpath",
        version="0.50.0",
        sha=pin.new,
    )
    numstat = _g(clone, "diff", "--numstat", tip, commit).splitlines()
    assert len(numstat) == 4
    assert sorted(numstat) == [  # added/removed lines per file, well under the bound
        "1\t1\tdeploy/group_vars/all.yml",
        "2\t2\tpyproject.toml",
        "2\t2\ttests/test_coverage_job.py",
        "4\t4\tuv.lock",
    ]
    # the verify run: the committed tree, the same credential-free env
    argv, kw = pin.rig.launches[1]
    assert kw["cwd"] == pin.tree() and TOKEN not in json.dumps(kw["env"])
    assert _g(pin.tree(), "rev-parse", "HEAD") == commit
    assert _g(pin.tree(), "status", "--porcelain") == ""
    script = Path(argv[1]).read_text(encoding="utf-8")
    assert "uv run --frozen --extra catalyst pytest -q" in script
    for test in (
        "tests/test_coverage_pin_lock.py",
        "tests/test_deploy_wheelhouse_floor.py",
        "tests/test_coverage_job.py::test_anchor_key_pins_jo482106s_normalised_key",
    ):
        assert test in script
    assert pin.record()["state"] == "building"  # committed only once verified
    _clean(pin, ctx)


def test_a_tree_the_derive_forged_into_never_reaches_the_commit(pin: Pin) -> None:
    """If this fails a file the derive added, staged, rewrote or symlinked in
    the tree (it runs repo code as the worker's user) is committed, or the
    forged tree is still there when the verify run starts."""
    tip = pin.tip()
    ctx, handle = _derived(pin)
    tree = pin.tree()
    (tree / "evil.txt").write_text("x\n", encoding="utf-8")
    _g(tree, "add", "evil.txt")
    (tree / "src").mkdir(exist_ok=True)
    (tree / "src" / "a.py").write_text("x = 'forged'\n", encoding="utf-8")
    (tree / "tests" / "test_coverage_job.py").write_text(
        "forged = 1\n", encoding="utf-8"
    )
    (tree / "uv.lock").unlink()
    (tree / "uv.lock").symlink_to(pin.tmp / "elsewhere.lock")
    (tree / "pyproject.toml").write_text("forged = 1\n", encoding="utf-8")
    _good_result(pin)
    _finish(handle, 0)
    assert pb.poll(ctx, handle) is False, ctx.failure
    commit = handle["commit"]
    names = _g(pin.rig.clone, "ls-tree", "-r", "--name-only", commit).splitlines()
    assert "evil.txt" not in names
    assert sorted(
        _g(pin.rig.clone, "diff", "--name-only", tip, commit).splitlines()
    ) == sorted(pb.PIN_PATHS)
    full = pin.texts("0.50.0", pin.new)
    assert _show(pin, commit, "pyproject.toml") == full["pyproject.toml"]
    assert _show(pin, commit, "uv.lock") == full["uv.lock"]
    assert _show(pin, commit, "src/a.py") == "x = 1\n"
    assert not (tree / "evil.txt").exists()
    assert not (tree / "uv.lock").is_symlink()
    assert _g(tree, "status", "--porcelain") == ""


def test_a_commit_that_is_not_a_pure_pin_bump_is_refused(pin: Pin) -> None:
    """If this fails the proof of the commit accepts a second commit, an
    extra path, a rewrite past the line bound or text the worker did not
    write."""
    clone, tip = pin.rig.clone, pin.tip()
    expected = pin.texts("0.50.0", pin.new)

    def commit(files: dict[str, str], parent: str = tip, n: int = 1) -> str:
        _g(clone, "checkout", "-q", "-b", f"t{time.monotonic_ns()}", parent)
        for _ in range(n):
            for name, text in files.items():
                (clone / name).parent.mkdir(parents=True, exist_ok=True)
                (clone / name).write_text(text, encoding="utf-8")
            _g(clone, "add", "-A", "--", *files)
            _g(clone, "commit", "-q", "-m", "x", "--allow-empty")
            files = {k: v + "#\n" for k, v in files.items()}
        sha = _g(clone, "rev-parse", "HEAD")
        _g(clone, "checkout", "-q", "main")
        return sha

    good = commit(expected)
    assert pb._commit_violation(clone, tip, good, expected) is None
    assert "only child" in str(
        pb._commit_violation(clone, tip, commit(expected, n=2), expected)
    )
    assert "rather than the pin files" in str(
        pb._commit_violation(clone, tip, commit({**expected, "README.md": "x\n"}), {})
    )
    wide = {**expected, "uv.lock": expected["uv.lock"] + "x = 1\n" * 12}
    assert "more than 8" in str(pb._commit_violation(clone, tip, commit(wide), wide))
    assert "not the text" in str(
        pb._commit_violation(clone, tip, good, {**expected, "uv.lock": "other\n"})
    )
    assert "only child" in str(pb._commit_violation(clone, tip, tip, expected))


# ── derive and verify endings ──────────────────────────────────────


@pytest.mark.parametrize("how", ["137", "gone"])
def test_a_killed_derive_is_attempt_free_and_drops_what_it_made(
    pin: Pin, how: str
) -> None:
    """If this fails a derive killed from outside is charged, or leaves the
    tree, branch or building record behind."""
    ctx, handle = _derived(pin)
    if how == "137":
        _finish(handle, 137, "Killed\n")
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "gate infrastructure")
    _nothing_left(pin)


def test_past_its_deadline_the_derive_is_killed_attempt_free(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a derive past its wall deadline keeps running, signals
    the wrong group, or is charged."""
    ctx, handle = _derived(pin)
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: True)
    handle["deadline"] = time.time() - 5
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "timed-out")
    assert pin.signals == [(4242, 15)]
    _nothing_left(pin)


@pytest.mark.parametrize(
    "line",
    [
        "error: error sending request for url (https://pypi.org/simple/x/)",
        "error: Connection reset by peer",
        "dns error: failed to lookup address information",
        "HTTP status server error (503 Service Unavailable) for url",
        "fatal: unable to access: Could not resolve host: github.com",
    ],
)
def test_a_transport_failure_in_the_log_is_attempt_free(pin: Pin, line: str) -> None:
    """If this fails a registry, DNS or 5xx failure is charged to the bump."""
    ctx, handle = _derived(pin)
    _finish(
        handle, 2, f"{line}\n✖ gate sync failed: uv sync --frozen --extra catalyst\n"
    )
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "pin-transport")
    _nothing_left(pin)


@pytest.mark.parametrize(
    ("log_text", "command"),
    [
        (DERIVE_RED, "uv lock --check"),
        (
            "error: Failed to build `autocatpath`\n"
            "✖ gate sync failed: uv sync --frozen --extra catalyst\n",
            "uv sync --frozen --extra catalyst",
        ),
        (
            "pin_bump: installed autocatpath is 0.49.0, not the target 0.50.0\n"
            "✖ gate derive failed: uv run --frozen --extra catalyst python -I x\n",
            "uv run --frozen",
        ),
    ],
)
def test_a_red_lock_check_build_or_version_assert_is_charged(
    pin: Pin, log_text: str, command: str
) -> None:
    """If this fails a stale lock, a build failure or the version assert is
    retried as infrastructure, or leaves the tree and branch."""
    ctx, handle = _derived(pin)
    _finish(handle, 1, log_text)
    assert pb.poll(ctx, handle) is True
    _charged(ctx)
    assert command in str(ctx.failure) and ctx.meta_set["exit_code"] == 1
    assert pb.COORDINATOR_ASK_KEY not in ctx.meta_set
    _nothing_left(pin)


def test_a_red_verify_is_charged_with_the_masked_tail_and_drops_the_tree(
    pin: Pin,
) -> None:
    """If this fails a red pin test lands, keeps the committed branch, or
    carries a credential-shaped string from the log into the comment."""
    ctx, handle = _verifying(pin)
    assert pin.branch_head() == handle["commit"]
    leaked = "ghp_" + "".join(chr(97 + (i * 3) % 26) for i in range(36))
    _finish(
        handle,
        1,
        f"FAILED tests/test_coverage_pin_lock.py - AssertionError {leaked}\n"
        "✖ gate verify failed: uv run --frozen --extra catalyst pytest -q x\n",
    )
    assert pb.poll(ctx, handle) is True
    _charged(ctx)
    note = pin.store.comments(PIN)[-1]
    assert "test_coverage_pin_lock" in note and leaked not in note
    assert leaked not in str(ctx.failure) and "test_coverage_pin_lock" in str(
        ctx.failure
    )
    _nothing_left(pin)
    assert pin.record() is None


def test_a_killed_verify_is_attempt_free(pin: Pin) -> None:
    """If this fails a verify killed from outside is charged or leaves the
    committed branch behind."""
    ctx, handle = _verifying(pin)
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "gate infrastructure")
    _nothing_left(pin)


def test_a_moved_branch_head_after_the_verify_is_never_stamped(pin: Pin) -> None:
    """If this fails a verify run that moved slice/<id> (it runs repo tests as
    the worker's user) still stamps the committed record."""
    ctx, handle = _verifying(pin)
    _g(
        pin.tree(),
        "-c",
        "user.name=x",
        "-c",
        "user.email=x@x",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "moved",
    )
    _finish(handle, 0, "ok\n")
    assert pb.poll(ctx, handle) is True
    _charged(ctx)
    assert "no longer points" in str(ctx.failure)
    _nothing_left(pin)


# ── committed, handoff, resume ─────────────────────────────────────


def test_a_green_verify_stamps_the_record_and_parks_in_handoff(pin: Pin) -> None:
    """If this fails the committed record lacks a field the next round reads,
    the job ends instead of parking, or the tree is not clean on the branch."""
    tip = pin.tip()
    ctx, handle = _committed(pin)
    commit = pin.branch_head()
    assert commit == handle["commit"] and commit != tip
    rec = pin.record()
    assert rec == {
        "state": "committed",
        "repo": "catpath",
        "package": "autocatpath",
        "version": "0.50.0",
        "sha": pin.new,
        "tag": "v0.50.0",
        "from_version": "0.49.0",
        "from_sha": pin.old,
        "commit": commit,
        "base": tip,
        "release_todo": REL,
        "worktree": str(pin.tree()),
        "branch": "slice/77",
        "job": 901,
        "image_rev": pin.new,
    }
    assert ctx.status is None and ctx.failure is None
    assert handle["phase"] == "handoff" and handle["commit"] == commit
    assert ctx.meta_set["compute_handle"] is handle
    due = datetime.strptime(handle["not_before"], TS).replace(tzinfo=UTC)
    assert (due - datetime.now(UTC)).total_seconds() < 5
    assert pin.tree().is_dir() and _g(pin.tree(), "status", "--porcelain") == ""
    assert _g(pin.tree(), "rev-parse", "HEAD") == commit
    assert pin.store.comments(PIN) == []
    _clean(pin, ctx)


def _handoff(pin: Pin) -> tuple[_Ctx, dict[str, Any]]:
    """A job parked in ``handoff`` and due now."""
    ctx, handle = _committed(pin)
    _due_now(handle)
    return ctx, handle


def test_the_handoff_mints_one_land_and_succeeds(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails the committed bump mints no land (or a wrong one: idem
    key, params, parent), does not stamp the record handed, or does not
    succeed the job with the summary the dispatcher's readers expect."""
    monkeypatch.setenv("PRECIS_NODE", "spruce")
    ctx, handle = _handoff(pin)
    commit = handle["commit"]
    assert pb.poll(ctx, handle) is True
    assert ctx.status == "succeeded" and ctx.failure is None
    (mint,) = pin.mints
    assert mint["job_type"] == "land_slice" and mint["executor"] == "ssh_node"
    assert mint["idem_key"] == "land_slice:td77" and mint["parent_id"] == PIN
    assert mint["params"] == {
        "todo_id": PIN,
        "worktree": str(pin.tree()),
        "branch": "slice/77",
        "target_node": "spruce",
    }
    rec = pin.record()
    assert rec["state"] == "handed" and rec["land_job"] == 5001
    assert rec["commit"] == commit and rec["worktree"] == str(pin.tree())
    assert ctx.chunks[-1] == (
        "job_summary",
        f"pin_bump td77: v0.50.0 committed {commit[:9]}, handed to land_slice jo5001",
    )
    assert ctx.meta_set["land_job"] == 5001
    assert pin.tree().is_dir() and pin.branch_head() == commit
    assert pin.store.comments(PIN) == []
    _clean(pin, ctx)


def test_a_second_poll_of_a_handed_bump_mints_nothing(pin: Pin) -> None:
    """If this fails a repeated poll of the final phase mints a second land."""
    ctx, handle = _handoff(pin)
    assert pb.poll(ctx, handle) is True
    assert pb.poll(ctx, handle) is True
    assert len(pin.mints) == 1 and ctx.status == "succeeded"
    assert pin.record()["land_job"] == 5001


def test_a_resumed_job_adopts_a_live_land_instead_of_minting_another(
    pin: Pin,
) -> None:
    """If this fails a job resumed onto a committed record whose land is
    already live (the stamp died after the mint) duplicates the land."""
    _c, first = _committed(pin)
    pin.live["land_slice:td77"] = 777
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is False
    assert handle["phase"] == "handoff" and handle["commit"] == first["commit"]
    _due_now(handle)
    assert pb.poll(ctx, handle) is True
    assert pin.live == {"land_slice:td77": 777}
    assert pin.record()["state"] == "handed" and pin.record()["land_job"] == 777


def test_a_failed_mint_is_attempt_free_and_the_next_job_resumes_to_it(
    pin: Pin,
) -> None:
    """If this fails a failed land mint is charged, drops the committed
    branch or tree, or the next job rebuilds instead of resuming the
    hand-off."""
    ctx, handle = _handoff(pin)
    commit = handle["commit"]
    pin.mint_error = RuntimeError("db is down")
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "pin-handoff")
    assert pin.record()["state"] == "committed" and pin.record()["commit"] == commit
    assert pin.branch_head() == commit and pin.tree().is_dir()
    assert pin.store.comments(PIN)[-1].startswith("handoff: pin bump FAILED:")
    pin.mint_error = None
    launches = len(pin.rig.launches)
    ctx2, handle2 = _submitted(pin)
    assert pb.poll(ctx2, handle2) is False
    assert handle2["phase"] == "handoff" and len(pin.rig.launches) == launches
    _due_now(handle2)
    assert pb.poll(ctx2, handle2) is True
    assert ctx2.status == "succeeded" and len(pin.mints) == 1
    assert pin.record()["state"] == "handed"


def test_a_branch_moved_before_the_handoff_mints_nothing(pin: Pin) -> None:
    """If this fails a land is minted for a branch whose head is no longer
    the committed commit."""
    ctx, handle = _handoff(pin)
    _g(
        pin.tree(),
        "-c",
        "user.name=x",
        "-c",
        "user.email=x@x",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "moved",
    )
    assert pb.poll(ctx, handle) is True
    _charged(ctx)
    assert pin.mints == [] and pin.record()["state"] == "committed"


def _no_op_job(pin: Pin) -> tuple[_Ctx, dict[str, Any]]:
    pin.precis_commit(pin.texts("0.50.0", pin.new))
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is False
    assert handle["phase"] == "handoff" and handle["noop"] is True
    _due_now(handle)
    return ctx, handle


def test_a_no_op_waits_for_the_image_before_closing(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7 (replaces the no-op's own close): a no-op writes ``pinned:``
    (never ``landed:``), mints no land and keeps its node open until the gate
    image carries the lock's sha, then closes it. If this fails a no-op
    unblocks the glue on a stale image, or closes with a lie."""
    monkeypatch.setenv("PRECIS_NODE", "spruce")
    ctx, handle = _no_op_job(pin)
    tip = pin.tip()
    assert pb.poll(ctx, handle) is False
    assert ctx.status is None and ctx.failure is None
    assert handle["phase"] == "image" and handle["noop"] is True
    pinned = f"pinned: autocatpath v0.50.0 already at {pin.new[:9]} (no-op)"
    assert pin.store.comments(PIN) == [pinned]
    assert "STATUS:doing" in pin.store.tag_strings(PIN)
    rec = pin.record()
    assert rec["state"] == "landed" and rec["noop"] is True
    assert rec["landed_sha"] == tip and rec["image_rev"] == pin.new
    _gate_image(monkeypatch, "main")
    assert pb.poll(ctx, handle) is False
    assert "STATUS:doing" in pin.store.tag_strings(PIN)
    assert pin.record()["image"]["reason"] == "stale"
    _gate_image(monkeypatch, pin.new)
    _due_now(handle)
    assert pb.poll(ctx, handle) is True
    assert ctx.status == "succeeded" and ctx.failure is None
    assert "STATUS:done" in pin.store.tag_strings(PIN)
    assert pin.store.comments(PIN) == [
        pinned,
        f"image: precis-dev on spruce carries autocatpath {pin.new[:9]}; "
        "glue unblocked",
    ]
    assert pin.mints == [] and pin.branch_head() is None
    assert not any("landed:" in c for c in pin.store.comments(PIN))
    assert ctx.chunks[-1][0] == "job_summary" and "node closed" in ctx.chunks[-1][1]


def test_a_no_op_resolves_the_batch_gripes_with_the_precis_tip(pin: Pin) -> None:
    """If this fails the no-op's closing tail skips the gripe resolution or
    names the release sha instead of the precis tip."""
    _batch(pin)
    ctx, handle = _no_op_job(pin)
    tip = pin.tip()
    assert pb.poll(ctx, handle) is False
    assert [c.text for c in pin.store.chunks.list_chunks_for_ref(91)] == [
        f"resolved: {tip[:9]} (main, release pending) — autocatpath v0.50.0, td81"
    ]
    assert "STATUS:auto_verify" in pin.store.tag_strings(91)


def test_a_no_op_leaves_a_node_closed_by_the_coordinator_alone(pin: Pin) -> None:
    """If this fails a no-op re-opens or re-closes a node a ruling already
    closed won't-do."""
    ctx, handle = _no_op_job(pin)
    pin.store.add_tag(PIN, Tag.closed("STATUS", "won't-do"), replace_prefix=True)
    assert pb.poll(ctx, handle) is False
    assert pb.poll(ctx, handle) is True
    assert ctx.status == "succeeded" and ctx.failure is None
    assert "STATUS:won't-do" in pin.store.tag_strings(PIN)
    assert "STATUS:done" not in pin.store.tag_strings(PIN)


# ── the gate image wait (F7) ───────────────────────────────────────


def _gate_image(
    monkeypatch: pytest.MonkeyPatch,
    label: str | None = None,
    *,
    rc: int = 0,
    err: str = "",
    exc: Exception | None = None,
) -> list[int]:
    """Stand ``docker image inspect precis-dev`` at the seam: an image whose
    rev label is ``label`` (``None``: no labels at all), a non-zero exit with
    ``err``, or ``exc`` raised. Returns the call log."""
    calls: list[int] = []

    def inspect() -> subprocess.CompletedProcess[str]:
        calls.append(1)
        if exc is not None:
            raise exc
        labels = (
            None if label is None else {pb.REV_LABEL: label, pb.GIT_SHA_LABEL: "f" * 40}
        )
        out = f"sha256:{'9' * 64} {json.dumps(labels)}\n" if rc == 0 else ""
        return subprocess.CompletedProcess(["docker"], rc, stdout=out, stderr=err)

    monkeypatch.setattr(pb, "_inspect_gate_image", inspect)
    return calls


def _image_wait(pin: Pin) -> tuple[_Ctx, dict[str, Any]]:
    """A bump handed off and landed (the land's ``finish``, at the precis tip:
    a commit on main), then the dispatcher's next job past its resolve poll:
    parked in ``image``, due."""
    ctx, handle = _handoff(pin)
    assert pb.poll(ctx, handle) is True
    pb.finish(pin.store, PIN, pin.tip())
    oracle, mirrors = len(pin.oracle), len(pin.mirrors)
    ctx2, handle2 = _submitted(pin)
    assert pb.poll(ctx2, handle2) is False, ctx2.failure
    assert handle2["phase"] == "image" and handle2["noop"] is False
    assert (len(pin.oracle), len(pin.mirrors)) == (oracle, mirrors)
    return ctx2, handle2


def test_image_phase_closes_the_node_when_the_gate_image_carries_the_pin(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a landed pin whose gate image carries it stays open
    (the glue never unblocks), or closes without the record saying why."""
    monkeypatch.setenv("PRECIS_NODE", "spruce")
    ctx, handle = _image_wait(pin)
    _gate_image(monkeypatch, pin.new)
    assert pb.poll(ctx, handle) is True
    assert ctx.status == "succeeded" and ctx.failure is None
    assert "STATUS:done" in pin.store.tag_strings(PIN)
    assert pin.store.comments(PIN)[-1] == (
        f"image: precis-dev on spruce carries autocatpath {pin.new[:9]}; glue unblocked"
    )
    image = pin.record()["image"]
    assert image["ready"] is True and image["reason"] == "match"
    assert (image["want"], image["have"], image["host"]) == (pin.new, pin.new, "spruce")
    assert image["git_sha"] == "f" * 40 and image["image_id"] == "sha256:" + "9" * 64
    assert pin.record()["state"] == "landed"
    _clean(pin, ctx)


def test_a_stale_label_parks_without_an_attempt(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails an image built before the pin (its label the old engine)
    closes the node, or the wait fails the job instead of parking. The rig's
    origin/main still locks the old sha over the land, as a main that moved
    the pin back would: the main-lock path must not take it."""
    monkeypatch.setenv("PRECIS_NODE", "spruce")
    ctx, handle = _image_wait(pin)
    _gate_image(monkeypatch, pin.old)
    assert pb.poll(ctx, handle) is False
    assert ctx.status is None and ctx.failure is None
    assert "STATUS:doing" in pin.store.tag_strings(PIN)
    image = pin.record()["image"]
    assert (image["reason"], image["have"], image["want"]) == (
        "stale",
        pin.old,
        pin.new,
    )
    assert image["host"] == "spruce" and "ready" not in image
    assert image["since"] == image["checked_at"]
    due = datetime.strptime(handle["not_before"], TS).replace(tzinfo=UTC)
    left = (due - datetime.now(UTC)).total_seconds()
    assert pb.IMAGE_PARK_S - 10 < left <= pb.IMAGE_PARK_S
    assert handle["phase"] == "image"
    assert pb.poll(ctx, handle) is False  # not due: no check at all


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"rc": 1, "err": "Error: No such image: precis-dev"}, "absent"),
        ({"rc": 1, "err": "Error response from daemon: No such object"}, "absent"),
        ({"label": None}, "unlabelled"),
        ({"label": ""}, "unlabelled"),
        ({"label": "main"}, "stale"),
    ],
)
def test_an_absent_or_unlabelled_image_counts_as_stale(
    pin: Pin, monkeypatch: pytest.MonkeyPatch, over: dict[str, Any], reason: str
) -> None:
    """If this fails a missing image, one with no rev label, or one built
    against an unpinned ``main`` reads as carrying the pin."""
    ctx, handle = _image_wait(pin)
    _gate_image(monkeypatch, **{"label": None, **over})
    assert pb.poll(ctx, handle) is False
    assert ctx.status is None
    assert pin.record()["image"]["reason"] == reason
    assert reason in pb.STALE_REASONS
    assert "STATUS:doing" in pin.store.tag_strings(PIN)


@pytest.mark.parametrize(
    "over",
    [
        {"exc": FileNotFoundError("docker")},
        {"exc": subprocess.TimeoutExpired(["docker"], 30)},
        {"rc": 1, "err": "Cannot connect to the Docker daemon at unix:///x.sock"},
    ],
)
def test_an_unreachable_docker_parks_as_infra_not_stale(
    pin: Pin, monkeypatch: pytest.MonkeyPatch, over: dict[str, Any]
) -> None:
    """If this fails docker that cannot answer fails the job, closes the
    node, or reads as a stale image (an immediate rebuild ask)."""
    ctx, handle = _image_wait(pin)
    _gate_image(monkeypatch, **over)
    assert pb.poll(ctx, handle) is False
    assert ctx.status is None and ctx.failure is None
    image = pin.record()["image"]
    assert image["reason"] == pb.UNREACHABLE_REASON and image["have"] is None
    assert "STATUS:doing" in pin.store.tag_strings(PIN)


def test_a_later_pin_on_main_counts_as_carrying_the_pin(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realistic descendant (default, no Reto question): a later pin B of
    the same package lands on main before A's image is rebuilt, and the
    image is built from main, labelled B's sha. A closes: B is what
    origin/main's lock carries and A's land is under that tip. Nothing is
    fetched. If this fails A waits forever for an image no one will build."""
    ctx, handle = _image_wait(pin)
    newer = pin.cut("0.51.0")
    pin.precis_commit(pin.texts("0.51.0", newer))  # pin B lands on main
    mirrors, oracle = len(pin.mirrors), len(pin.oracle)
    _gate_image(monkeypatch, newer)
    assert pb.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failure
    image = pin.record()["image"]
    assert image["ready"] is True and image["reason"] == "pinned-on-main"
    assert image["have"] == newer and image["want"] == pin.new
    assert (len(pin.mirrors), len(pin.oracle)) == (mirrors, oracle)


def test_a_main_pin_does_not_count_when_the_land_is_not_under_main(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails main's locked sha passes for a pin whose own land is not
    on that main (the label says nothing about A then)."""
    ctx, handle = _image_wait(pin)
    branch = pin.branch_head()  # the bump's own commit, never merged to main
    assert branch is not None and branch != pin.tip()
    pin.store.refs[PIN].meta["pin_bump"]["landed_sha"] = branch
    newer = pin.cut("0.51.0")
    pin.precis_commit(pin.texts("0.51.0", newer))
    _gate_image(monkeypatch, newer)
    assert pb.poll(ctx, handle) is False
    assert pin.record()["image"]["reason"] == "stale"


def test_a_descendant_already_in_the_mirror_counts(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror's path: catpath's main was already past the release when
    the bump mirrored it, and the image was built against that main
    (``scripts/build-image``'s fallback). Any other 40-hex label stays
    stale."""
    newer = pin.cat_commit("0.50.1")  # on catpath main before the bump runs
    ctx, handle = _image_wait(pin)
    _gate_image(monkeypatch, "0" * 40)  # 40-hex, not in the mirror
    assert pb.poll(ctx, handle) is False
    assert pin.record()["image"]["reason"] == "stale"
    _gate_image(monkeypatch, newer)
    _due_now(handle)
    assert pb.poll(ctx, handle) is True
    image = pin.record()["image"]
    assert image["ready"] is True and image["reason"] == "newer"
    assert image["have"] == newer and image["want"] == pin.new


def test_a_no_op_whose_landed_stamp_fails_is_attempt_free(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a no-op whose ``landed`` stamp the store refused goes
    on to the image wait with a ``committed`` record, and that wait then
    charges the node an attempt."""
    ctx, handle = _no_op_job(pin)
    real = pin.store.stamp_ref_meta

    def refuse(ref_id: int, updates: dict[str, Any]) -> None:
        record = updates.get("pin_bump")
        if isinstance(record, dict) and record.get("state") == "landed":
            raise RuntimeError("store down")
        real(ref_id, updates)

    monkeypatch.setattr(pin.store, "stamp_ref_meta", refuse)
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "pin-record")
    assert pin.record()["state"] == "committed"


def test_repeated_stale_polls_stamp_and_report_once(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails every 2-minute poll rewrites the record (and its
    ``since``, so the unreachable ask never comes) or adds a job event."""
    ctx, handle = _image_wait(pin)
    _gate_image(monkeypatch, pin.old)
    stamps: list[dict[str, Any]] = []
    real = pin.store.stamp_ref_meta

    def spy(ref_id: int, updates: dict[str, Any]) -> None:
        stamps.append(updates)
        real(ref_id, updates)

    monkeypatch.setattr(pin.store, "stamp_ref_meta", spy)
    for _ in range(3):
        _due_now(handle)
        assert pb.poll(ctx, handle) is False
    events = [t for k, t in ctx.chunks if k == "job_event" and " is stale " in t]
    assert len(stamps) == 1 and len(events) == 1
    _gate_image(monkeypatch, rc=1, err="Error: No such image: precis-dev")
    _due_now(handle)
    assert pb.poll(ctx, handle) is False
    assert len(stamps) == 2
    assert [t for k, t in ctx.chunks if k == "job_event" and " is absent " in t]
    assert pin.record()["image"]["reason"] == "absent"


def test_the_image_check_runs_docker_without_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If this fails the image check runs anything but a plain ``docker
    image inspect`` of precis-dev, or a credential in the worker env reaches
    it (the docker endpoint knobs still do)."""
    monkeypatch.setenv("PRECIS_FACTORY_GIT_TOKEN", TOKEN)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("PRECIS_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/docker.sock")
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, 0, stdout="sha256:x null\n", stderr="")

    monkeypatch.setattr(pb.subprocess, "run", fake_run)
    pb._inspect_gate_image()
    ((argv, kw),) = seen
    assert argv == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}} {{json .Config.Labels}}",
        "precis-dev",
    ]
    env = kw["env"]
    assert TOKEN not in json.dumps(env) and TOKEN not in json.dumps(argv)
    assert "GH_TOKEN" not in env and "PRECIS_FACTORY_GIT_TOKEN" not in env
    assert env["DOCKER_HOST"] == "unix:///tmp/docker.sock"
    assert kw["timeout"] and kw.get("shell") is not True


def test_a_landed_record_resumes_straight_into_the_image_wait(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails the job after the land re-reads the release, fetches,
    or is refused (a repo closed to the factory after the land would strand
    the node open); ``_image_wait`` also pins that it never fetches."""
    ctx, handle = _handoff(pin)
    assert pb.poll(ctx, handle) is True
    pb.finish(pin.store, PIN, "c" * 40)
    monkeypatch.setattr(lanes, "open_repos", lambda store: frozenset({"precis"}))
    pin.store.refs[REL].meta.pop("repo_release")  # never read again
    params = {**PARAMS, "target_node": "spruce"}
    assert pb.validate_submit(pin.store, params=params) is None
    oracle, mirrors = len(pin.oracle), len(pin.mirrors)
    ctx2, handle2 = _submitted(pin, target_node="spruce")
    assert pb.poll(ctx2, handle2) is False, ctx2.failure
    assert handle2["phase"] == "image"
    assert (len(pin.oracle), len(pin.mirrors)) == (oracle, mirrors)
    assert any("has landed; waiting" in t for _, t in ctx2.chunks)
    # a record that has not landed is still refused for the closed repo
    pin.store.refs[PIN].meta["pin_bump"]["state"] = "handed"
    assert pb.validate_submit(pin.store, params=params) is not None


def test_a_node_closed_by_hand_ends_the_wait_quietly(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a node the coordinator closed (or a hand ``scripts/ship``
    landed) keeps a job polling docker, fails it, or reopens the node."""
    ctx, handle = _image_wait(pin)
    calls = _gate_image(monkeypatch, pin.old)
    comments = list(pin.store.comments(PIN))
    pin.store.add_tag(PIN, Tag.closed("STATUS", "won't-do"), replace_prefix=True)
    assert pb.poll(ctx, handle) is True
    assert ctx.status == "succeeded" and ctx.failure is None
    assert calls == [] and pin.store.comments(PIN) == comments
    assert "STATUS:won't-do" in pin.store.tag_strings(PIN)


def test_image_ready_closes_the_open_image_ask(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails the rebuild ask the dispatcher filed stays open after
    the node closed itself, or an ask already settled is written again."""
    monkeypatch.setenv("PRECIS_NODE", "spruce")
    ctx, handle = _image_wait(pin)
    pin.store.add_todo(300, status="open")
    pin.store.refs[PIN].meta[pb.IMAGE_ASK_KEY] = {"ask": 300, "want": pin.new}
    _gate_image(monkeypatch, pin.new)
    assert pb.poll(ctx, handle) is True
    assert "STATUS:done" in pin.store.tag_strings(300)
    assert pin.store.comments(300) == [
        f"answer: precis-dev on spruce now carries {pin.new[:9]}; td77 closed itself"
    ]
    assert pb.poll(ctx, handle) is True  # the node is closed: quiet
    assert len(pin.store.comments(300)) == 1


def test_the_dispatchers_restated_keys_match() -> None:
    """If this fails plan_dispatch reads a key or reason the image wait does
    not write (the two modules do not import each other)."""
    from precis.workers.job_types import plan_dispatch as pd

    assert pd.PIN_RECORD_KEY == pb.RECORD_KEY
    assert pd.PIN_IMAGE_ASK_KEY == pb.IMAGE_ASK_KEY
    assert pd.PIN_GATE_IMAGE == pb.GATE_IMAGE
    assert pd.PIN_IMAGE_STALE == pb.STALE_REASONS
    assert pd.PIN_IMAGE_UNREACHABLE == pb.UNREACHABLE_REASON


def test_gate_image_matches_compose_and_dockerfile_label() -> None:
    """If this fails the wait inspects an image the gate does not run in, or
    reads a label ``scripts/build-image`` does not write."""
    repo = Path(__file__).resolve().parents[1]
    compose = (repo / "docker" / "dev" / "compose.yaml").read_text(encoding="utf-8")
    assert f"    image: {pb.GATE_IMAGE}\n" in compose
    dockerfile = (repo / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert f"LABEL {pb.REV_LABEL}=$AUTOCATPATH_REV" in dockerfile
    assert f"{pb.GIT_SHA_LABEL}=$PRECIS_GIT_SHA" in dockerfile
    build = (repo / "scripts" / "build-image").read_text(encoding="utf-8")
    assert "autocatpath_locked_sha uv.lock" in build
    assert "AUTOCATPATH_REV=${AUTOCATPATH_REV}" in build


# ── finish: the gripe walk ─────────────────────────────────────────

SHA = "c" * 40
GRIPE_STATUS = {91: "open", 92: "triaged", 93: "auto_verify", 94: "open"}


def _batch(pin: Pin) -> None:
    """root 70: PIN -> REL -> {81 catpath done, 82 hexfold done, 83 open};
    81 -> 85 (done catpath, upstream through 81); 84 (done catpath) is not
    upstream; gripes 91..97 hang off them. The pin record is ``handed``."""
    store = pin.store
    store.add_tag(PIN, Tag.open("plan:70"), set_by="system")
    store.add_todo(70)
    store.refs[REL].meta["op"] = "repo_release"
    done = {"landed_repo": "catpath"}
    store.add_todo(81, status="done", meta=dict(done))
    store.add_todo(82, status="done", meta={"landed_repo": "hexfold"})
    store.add_todo(83, status="open", meta=dict(done))
    store.add_todo(84, status="done", meta=dict(done))
    store.add_todo(85, status="done", meta=dict(done))
    for gid, status in {**GRIPE_STATUS, 95: "open", 96: "open", 97: "done"}.items():
        store.add_todo(gid, status=status)
    pin.rows = [
        {"id": i, "parent_id": None, "kind": "todo"}
        for i in (70, PIN, REL, 81, 82, 83, 84, 85)
    ]
    pin.edges = [(PIN, REL), (REL, 81), (REL, 82), (REL, 83), (81, 85)]
    pin.links = [
        (81, 91),
        (81, 92),
        (85, 92),
        (81, 93),
        (82, 94),
        (83, 95),
        (84, 96),
        (81, 97),
    ]
    store.refs[PIN].meta["pin_bump"] = {
        "state": "handed",
        "repo": "catpath",
        "package": "autocatpath",
        "version": "0.50.0",
        "commit": "d" * 40,
    }


def test_finish_walks_exactly_the_closure_and_resolves_each_gripe_once(
    pin: Pin,
) -> None:
    """If this fails finish resolves a gripe of another repo's slice, an open
    slice, a slice not upstream or an already settled gripe, resolves one
    twice, or omits the gripe/slice lists from the record."""
    _batch(pin)
    pb.finish(pin.store, PIN, SHA)
    want = "resolved: ccccccccc (main, release pending) — autocatpath v0.50.0, "
    assert pin.store.comments(91) == []  # gripe comments are not todo comments
    assert [c.text for c in pin.store.chunks.list_chunks_for_ref(91)] == [want + "td81"]
    assert [c.text for c in pin.store.chunks.list_chunks_for_ref(92)] == [
        want + "td81, td85"
    ]
    for gid in (91, 92):
        assert "STATUS:auto_verify" in pin.store.tag_strings(gid)
    for gid in (93, 94, 95, 96, 97):
        assert pin.store.chunks.list_chunks_for_ref(gid) == []
    assert pin.store.tag_strings(93) == {"STATUS:auto_verify"}
    assert pin.store.tag_strings(97) == {"STATUS:done"}
    for gid in (94, 95, 96):
        assert pin.store.tag_strings(gid) == {"STATUS:open"}
    rec = pin.record()
    assert rec["state"] == "landed" and rec["landed_sha"] == SHA
    assert rec["gripes"] == [91, 92] and rec["slices"] == [81, 85]


def test_a_second_finish_writes_nothing_new(pin: Pin) -> None:
    """If this fails a repeated finish duplicates a comment, rewrites the
    record or drops the lists the first call stored."""
    _batch(pin)
    pb.finish(pin.store, PIN, SHA)
    before = (
        json.dumps(pin.store.refs[PIN].meta, sort_keys=True),
        {g: len(pin.store.chunks.list_chunks_for_ref(g)) for g in (91, 92)},
        {g: sorted(pin.store.tag_strings(g)) for g in (91, 92, 93)},
    )
    pb.finish(pin.store, PIN, SHA)
    after = (
        json.dumps(pin.store.refs[PIN].meta, sort_keys=True),
        {g: len(pin.store.chunks.list_chunks_for_ref(g)) for g in (91, 92)},
        {g: sorted(pin.store.tag_strings(g)) for g in (91, 92, 93)},
    )
    assert before == after
    assert before[1] == {91: 1, 92: 1}


def test_finish_follows_a_phase_gate_to_its_children(pin: Pin) -> None:
    """If this fails a slice under a phase the pin chain is blocked by is not
    in the batch (a blocked-by on a phase gates its children)."""
    _batch(pin)
    pin.store.add_todo(86, status="doing")  # a phase
    pin.store.add_todo(87, status="done", meta={"landed_repo": "catpath"})
    pin.store.add_todo(98, status="open")
    pin.rows += [
        {"id": 86, "parent_id": 70, "kind": "todo"},
        {"id": 87, "parent_id": 86, "kind": "todo"},
    ]
    pin.edges.append((REL, 86))
    pin.links.append((87, 98))
    pb.finish(pin.store, PIN, SHA)
    assert pin.record()["slices"] == [81, 85, 87]
    assert pin.record()["gripes"] == [91, 92, 98]


def test_finish_never_raises(pin: Pin, monkeypatch: pytest.MonkeyPatch) -> None:
    """If this fails a broken walk, a refusing store or a missing record makes
    finish raise into the land that already happened."""
    pb.finish(pin.store, PIN, SHA)  # no record at all
    _batch(pin)

    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("store down")

    real = pb._batch_slices
    monkeypatch.setattr(pb, "_batch_slices", boom)
    pb.finish(pin.store, PIN, SHA)
    assert pin.record()["state"] == "landed"  # stamped before the walk
    monkeypatch.setattr(pb, "_batch_slices", real)
    monkeypatch.setattr(pin.store, "stamp_ref_meta", boom)
    pb.finish(pin.store, PIN, SHA)  # the stamp refusing does not raise either


def test_one_failing_gripe_does_not_stop_the_others(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a gripe whose write fails leaves the rest of the batch
    unresolved."""
    _batch(pin)
    real = pb._resolve_gripe

    def flaky(store: Any, gid: int, *a: Any) -> bool:
        if gid == 91:
            raise RuntimeError("write failed")
        return real(store, gid, *a)

    monkeypatch.setattr(pb, "_resolve_gripe", flaky)
    pb.finish(pin.store, PIN, SHA)
    assert pin.record()["gripes"] == [92]
    assert "STATUS:auto_verify" in pin.store.tag_strings(92)


# ── serialise ──────────────────────────────────────────────────────


def test_another_nodes_live_pin_bump_holds_the_submit(pin: Pin) -> None:
    """If this fails a second pin bump can start while another node's is in
    flight; a holding: refusal means no job is minted."""
    pin.jobs["pin_bump"] = [(900, 66, False)]
    held = pb.validate_submit(pin.store, params=dict(PARAMS))
    assert held is not None and held.startswith("holding:") and "jo900" in held


def test_the_same_nodes_own_or_finished_jobs_do_not_hold(pin: Pin) -> None:
    """If this fails a retry of the same node, or another node's finished
    bump, holds the submit."""
    pin.jobs["pin_bump"] = [(900, PIN, False), (901, 66, True)]
    pin.jobs["land_slice"] = [(902, PIN, False)]
    assert pb.validate_submit(pin.store, params=dict(PARAMS)) is None


def test_a_live_land_of_a_pin_node_holds_but_an_ordinary_land_does_not(
    pin: Pin,
) -> None:
    """If this fails a pin node's land in flight does not hold the next bump,
    or an unrelated slice's land does."""
    pin.store.add_todo(66, meta={"pin_bump": {"state": "handed"}})
    pin.store.add_todo(67)
    pin.jobs["land_slice"] = [(903, 67, False)]
    assert pb.validate_submit(pin.store, params=dict(PARAMS)) is None
    pin.jobs["land_slice"] = [(904, 66, False), (903, 67, False)]
    held = pb.validate_submit(pin.store, params=dict(PARAMS))
    assert held is not None and held.startswith("holding:") and "jo904" in held
    pin.jobs["land_slice"] = [(904, 66, True)]
    assert pb.validate_submit(pin.store, params=dict(PARAMS)) is None


def test_an_unreadable_job_scan_holds_rather_than_admits(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a store that cannot list jobs lets two bumps run."""

    def boom(store: Any, jt: str) -> Any:
        raise RuntimeError("no pool")

    monkeypatch.setattr(pb, "_recent_jobs", boom)
    held = pb.validate_submit(pin.store, params=dict(PARAMS))
    assert held is not None and held.startswith("holding:")


def test_the_serialise_scan_is_bounded_and_reads_terminal_from_the_tags() -> None:
    """If this fails the scan reads every job row, or mistakes a tagged
    terminal job for a live one."""

    class _Conn:
        sql: list[tuple[str, tuple[Any, ...]]] = []

        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, sql: str, args: tuple[Any, ...]) -> _Conn:
            self.sql.append((sql, args))
            return self

        def fetchall(self) -> list[tuple[int, str | None]]:
            return [(12, "66"), (11, None), (10, "65")]

    class _Pool:
        def connection(self) -> _Conn:
            return _Conn()

    class _St:
        pool = _Pool()

        def tags_for(self, jid: int) -> list[Tag]:
            return [Tag.closed("STATUS", "succeeded" if jid == 10 else "running")]

    rows = pb._recent_jobs(_St(), "pin_bump")
    assert rows == [(12, 66, False), (11, None, False), (10, 65, True)]
    (sql, args), *_ = _Conn.sql
    assert (
        "LIMIT" in sql and args == ("pin_bump", pb.SCAN_LIMIT) and pb.SCAN_LIMIT == 50
    )


def test_registered_in_every_table_the_job_types_use() -> None:
    """If this fails pin_bump is missing from the job-type registry, the
    service registry, the describers or the factory pipeline band."""
    from precis.workers import job_describe, registry
    from precis.workers.executors import _common
    from precis.workers.job_types import _BUILTIN_JOB_TYPES, get_job_type

    assert get_job_type("pin_bump") is pb.SPEC
    assert "pin_bump" in _BUILTIN_JOB_TYPES
    assert "pin_bump" in _common.FACTORY_PIPELINE_JOB_TYPES
    assert "pin_bump" in job_describe.DESCRIBERS
    (svc,) = [s for s in registry.SERVICES if s.name == "pin_bump"]
    assert svc.placement == frozenset({"repo_checkout", "git"})
    assert _common.placement_requires(
        {"job_type": "pin_bump", "params": {"pin_repo": "catpath"}}
    ) == frozenset({"repo_checkout", "git"})


def test_a_committed_record_still_at_the_branch_head_is_resumed(pin: Pin) -> None:
    """If this fails a fresh job redoes a bump whose commit is still the
    branch head on the same tip instead of going straight to the handoff."""
    _ctx_done, first = _committed(pin)
    commit, launches, before = (
        first["commit"],
        len(pin.rig.launches),
        dict(pin.record()),
    )
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is False
    assert handle["phase"] == "handoff" and handle["commit"] == commit
    assert len(pin.rig.launches) == launches and pin.branch_head() == commit
    assert pin.record() == before
    assert any("resumes the committed bump" in text for _, text in ctx.chunks)


@pytest.mark.parametrize("how", ["tip-moved", "head-moved", "tree-gone"])
def test_any_other_committed_record_starts_fresh_from_a_clean_tree(
    pin: Pin, how: str
) -> None:
    """If this fails a committed record whose tip moved, whose branch head
    moved or whose tree is gone is resumed instead of rebuilt."""
    _c, first = _committed(pin)
    old_commit = first["commit"]
    if how == "tip-moved":
        pin.precis_commit({"README.md": "moved\n"})
    elif how == "head-moved":
        _g(
            pin.tree(),
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@x",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "moved",
        )
    else:
        _g(pin.rig.clone, "worktree", "remove", "--force", str(pin.tree()))
    tip = pin.tip()
    launches = len(pin.rig.launches)
    ctx, handle = _derived(pin)
    assert len(pin.rig.launches) == launches + 1
    assert pin.branch_head() == tip and pin.branch_head() != old_commit
    assert pin.record()["state"] == "building"
    assert sorted(
        line.split()[-1]
        for line in _g(pin.tree(), "status", "--porcelain").splitlines()
    ) == ["deploy/group_vars/all.yml", "pyproject.toml", "uv.lock"]
    assert handle["made"] is True


def test_a_branch_this_job_did_not_make_is_refused_and_never_dropped(pin: Pin) -> None:
    """If this fails a pre-existing slice/<id> branch or pin tree with no
    record of ours is deleted or built over."""
    _g(pin.rig.clone, "branch", "slice/77", pin.tip())
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _asks(ctx, "did not make")
    assert pin.branch_head() == pin.tip()
    assert pin.rig.launches == [] and pin.record() is None


def test_a_foreign_pin_tree_is_refused_and_left_alone(pin: Pin) -> None:
    """If this fails a pin-td<id> directory this job did not make is removed."""
    foreign = pin.tree()
    foreign.mkdir(parents=True)
    (foreign / "mine.txt").write_text("x\n", encoding="utf-8")
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _asks(ctx, "did not make")
    assert (foreign / "mine.txt").exists() and pin.branch_head() is None


# ── kill ───────────────────────────────────────────────────────────


def test_kill_on_parked_handles_does_nothing(pin: Pin) -> None:
    """If this fails killing a parked resolve or handoff handle raises, or
    drops the committed bump waiting for its land."""
    ctx, resolve = _submitted(pin)
    pb.kill(ctx, resolve)
    pb.kill(ctx, "garbage")
    ctx2, handoff = _committed(pin)
    pin.signals.clear()  # the finished phases' groups were reaped on the way
    pb.kill(ctx2, handoff)
    assert pin.tree().is_dir() and pin.branch_head() == handoff["commit"]
    assert pin.record()["state"] == "committed" and pin.signals == []


@pytest.mark.parametrize("phase", ["derive", "verify"])
def test_kill_on_a_live_run_signals_its_group_and_drops_what_it_made(
    pin: Pin, phase: str
) -> None:
    """If this fails a killed run's group is not signalled, or its tree,
    branch and building record are left behind."""
    ctx, handle = _derived(pin) if phase == "derive" else _verifying(pin)
    pin.signals.clear()  # the finished phases' groups were reaped on the way
    pb.kill(ctx, handle)
    assert pin.signals == [(4242, 15)]
    assert pin.branch_head() is None and not pin.tree().exists()
    assert pin.record() is None


# ── the derive script itself ───────────────────────────────────────


def _stub_engine(tmp: Path, version: str) -> Path:
    stub = tmp / "stub"
    for pkg, files in {
        "autocatpath": {"__init__.py": f'__version__ = "{version}"\n'},
        "precis_pathway": {
            "__init__.py": "",
            "coverage_job.py": "def with_defaults(cfg):\n    return {'cfg': cfg}\n",
            "runner.py": (
                "import hashlib, json\n"
                "def coverage_anchor_key(c):\n"
                "    return hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()\n"
            ),
        },
    }.items():
        (stub / pkg).mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (stub / pkg / name).write_text(text, encoding="utf-8")
    return stub


def _run_script(
    tmp: Path, engine: str, anchor_text: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    script = tmp / "derive.py"
    script.write_text(pb.DERIVE_SCRIPT_PY, encoding="utf-8")
    anchor = tmp / "anchors.py"
    anchor.write_text(anchor_text, encoding="utf-8")
    result = tmp / "result.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "0.50.0",
            str(anchor),
            "JO482106_CONFIG",
            str(result),
        ],
        cwd=str(tmp),
        env={"PYTHONPATH": str(_stub_engine(tmp, engine)), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    return proc, result


ANCHOR_MODULE = (
    'JO482106_CONFIG = {"name": "x", "search": {"seeds": [0, 1]}, "poisons": ["CO"]}\n'
    'raise RuntimeError("the module was imported")\n'
)


def test_the_derive_script_prints_nothing_and_writes_the_key_and_engine(
    tmp_path: Path,
) -> None:
    """If this fails the worker-written script prints, executes the test
    module instead of reading its assignment, or writes anything but
    {key, engine} computed from with_defaults(JO482106_CONFIG)."""
    proc, result = _run_script(tmp_path, "0.50.0", ANCHOR_MODULE)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "" and proc.stderr == ""
    cfg = {"name": "x", "search": {"seeds": [0, 1]}, "poisons": ["CO"]}
    want = hashlib.sha256(json.dumps({"cfg": cfg}, sort_keys=True).encode()).hexdigest()
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "key": want,
        "engine": "0.50.0",
    }


def test_the_derive_script_refuses_the_wrong_installed_engine(tmp_path: Path) -> None:
    """If this fails a venv that installed another engine than the target
    still yields a key."""
    proc, result = _run_script(tmp_path, "0.49.0", ANCHOR_MODULE)
    assert proc.returncode != 0 and "not the target 0.50.0" in proc.stderr
    assert not result.exists()


@pytest.mark.parametrize(
    "text", ["X = 1\n", ANCHOR_MODULE.splitlines()[0] + "\n" + "JO482106_CONFIG = {}\n"]
)
def test_the_derive_script_needs_exactly_one_config_assignment(
    tmp_path: Path, text: str
) -> None:
    """If this fails a missing or doubled JO482106_CONFIG yields a key."""
    proc, result = _run_script(tmp_path, "0.50.0", text)
    assert proc.returncode != 0 and "exactly once" in proc.stderr
    assert not result.exists()


# ── review fixes: independent verify, direction, nesting, races ────


def test_a_finished_phases_group_is_signalled_before_the_next_phase_starts(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a process the derive or verify left running survives its
    exit file: nothing TERMs then KILLs the phase's process group before the
    worker reads results, launches the verify or stamps the record."""
    seen: list[tuple[int, int, int, str]] = []

    def signal_group(pgid: int, sig: int) -> None:
        state = str((pin.record() or {}).get("state"))
        seen.append((pgid, int(sig), len(pin.rig.launches), state))

    monkeypatch.setattr(run_slice, "_signal_group", signal_group)
    ctx, handle = _derived(pin)
    _good_result(pin)
    _finish(handle, 0)
    assert pb.poll(ctx, handle) is False, ctx.failure
    # both signals went out while only the derive had launched
    assert seen == [(4242, 15, 1, "building"), (4242, 9, 1, "building")]
    assert len(pin.rig.launches) == 2
    seen.clear()
    _verify_result(pin)
    _finish(handle, 0, "5 passed\n")
    assert pb.poll(ctx, handle) is False, ctx.failure
    assert seen == [(4242, 15, 2, "building"), (4242, 9, 2, "building")]
    assert pin.record()["state"] == "committed"


def test_a_phase_group_that_is_already_gone_is_fine(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a group with no member left (the usual case) makes the
    poll raise or fail instead of going on."""

    def gone(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(run_slice, "_signal_group", gone)
    _verifying(pin)


def test_the_verify_is_an_independent_check_in_its_own_venv(pin: Pin) -> None:
    """If this fails the verify re-uses the derive's venv or result file, does
    not sync and re-derive the engine and key before the tests, reads a stale
    leftover, or the venvs and result files outlive the job."""
    rdir = pin.run_dir()
    ctx, handle = _derived(pin)
    stale = rdir / pb.VERIFY_VENV_DIR
    stale.mkdir()
    (stale / "left-over").write_text("x", encoding="utf-8")
    (rdir / pb.VERIFY_RESULT_FILE).write_text("{}", encoding="utf-8")
    (rdir / pb.VENV_DIR).mkdir()
    _good_result(pin)
    _finish(handle, 0)
    assert pb.poll(ctx, handle) is False, ctx.failure
    (_, derive_kw), (argv, verify_kw) = pin.rig.launches
    derive_venv = Path(derive_kw["env"]["UV_PROJECT_ENVIRONMENT"])
    verify_venv = Path(verify_kw["env"]["UV_PROJECT_ENVIRONMENT"])
    assert derive_venv == rdir / pb.VENV_DIR
    assert verify_venv == rdir / pb.VERIFY_VENV_DIR != derive_venv
    assert not stale.exists() and not derive_venv.exists()  # removed, not reused
    assert not (rdir / pb.VERIFY_RESULT_FILE).exists()
    script = Path(argv[1]).read_text(encoding="utf-8")
    sync = script.index("uv sync --frozen --extra catalyst")
    derive = script.index("python -I")
    tests = script.index("pytest -q")
    assert sync < derive < tests and "uv lock --check" not in script
    assert pb.VERIFY_RESULT_FILE in script and f" {rdir / pb.RESULT_FILE}" not in script
    assert (rdir / pb.DERIVE_PY).read_text(encoding="utf-8") == pb.DERIVE_SCRIPT_PY
    _verify_result(pin)
    verify_venv.mkdir()
    _finish(handle, 0, "5 passed\n")
    assert pb.poll(ctx, handle) is False, ctx.failure
    assert not verify_venv.exists()
    assert not (rdir / pb.RESULT_FILE).exists()
    assert not (rdir / pb.VERIFY_RESULT_FILE).exists()
    assert (rdir / pb.VERIFY_LOG).exists()  # logs stay for the operator


@pytest.mark.parametrize(
    "second",
    [
        {"key": "cd34" * 16, "engine": "0.50.0"},
        {"key": KEY, "engine": "0.49.0"},
        "not json",
        None,
    ],
)
def test_a_verify_that_recomputes_another_key_or_engine_is_charged(
    pin: Pin, second: Any
) -> None:
    """If this fails a verify whose own derive step disagrees with the
    committed key or engine (or wrote nothing, with the derive's result file
    still lying there) hands a land off or keeps the branch."""
    ctx, handle = _verifying(pin)
    if second is not None:
        _result(pin, second, pb.VERIFY_RESULT_FILE)
    assert (pin.run_dir() / pb.RESULT_FILE).exists()
    _finish(handle, 0, "5 passed\n")
    assert pb.poll(ctx, handle) is True
    _charged(ctx)
    assert "verify recomputed" in str(ctx.failure)
    _nothing_left(pin)
    assert pin.mints == [] and pin.record() is None
    assert not (pin.run_dir() / pb.RESULT_FILE).exists()


@pytest.mark.parametrize(
    ("phase", "step"),
    [("derive", "derive"), ("verify", "derive"), ("verify", "verify")],
)
def test_a_timed_out_word_from_python_or_pytest_stays_charged(
    pin: Pin, phase: str, step: str
) -> None:
    """If this fails the transport classifier reads the output of the python
    or pytest steps, so a test that prints "timed out" turns a red attempt-free."""
    ctx, handle = _derived(pin) if phase == "derive" else _verifying(pin)
    _finish(
        handle,
        1,
        "uv sync output\nFAILED test_x - operation timed out\n"
        f"✖ gate {step} failed: uv run --frozen x\n",
    )
    assert pb.poll(ctx, handle) is True
    _charged(ctx)
    assert pin.record() is None and pin.branch_head() is None


def test_a_transport_failure_in_the_verify_sync_is_attempt_free(pin: Pin) -> None:
    """If this fails the verify's own sync step is no longer read for
    transport failures."""
    ctx, handle = _verifying(pin)
    _finish(
        handle,
        2,
        "error: error sending request for url\n"
        "✖ gate sync failed: uv sync --frozen --extra catalyst\n",
    )
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "pin-transport")


def _merge_side(p: Pin) -> tuple[str, str]:
    """Catpath main with a merge of two lines from the old pin: ``(side sha,
    main-side sha)``, both ancestors of main and neither of the other."""
    _g(p.cat, "checkout", "-q", "-b", "side", p.old)
    side = p.cat_commit("0.50.0", note="side")
    _g(p.cat, "checkout", "-q", "main")
    _g(p.cat, "merge", "-q", "--no-ff", "-m", "merge", "side")
    return side, p.new


def test_the_same_sha_with_every_text_coherent_is_a_no_op(pin: Pin) -> None:
    """If this fails a pin already on the release sha is rebuilt."""
    pin.precis_commit(pin.texts("0.50.0", pin.new))
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is False and handle["noop"] is True
    assert "already carries" in pin.record()["note"] and pin.branch_head() is None


def test_a_release_behind_the_pinned_sha_is_already_past_whatever_the_version(
    pin: Pin,
) -> None:
    """If this fails a release sha that is an ancestor of the pinned sha moves
    the pin back when the version strings are equal or higher."""
    later = pin.cat_commit("0.50.0", note="later")
    pin.precis_commit(pin.texts("0.50.0", later))
    pin.release(pin.new, "0.50.0")  # same version string, older sha
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is False and handle["noop"] is True
    assert "already past" in pin.record()["note"] and pin.branch_head() is None
    assert pin.rig.launches == []


def test_a_release_ahead_of_the_pinned_sha_bumps_even_at_the_same_version(
    pin: Pin,
) -> None:
    """If this fails a newer sha that carries the same version string is not
    bumped to."""
    pin.precis_commit(pin.texts("0.50.0", pin.new))
    later = pin.cut("0.50.0", note="later")
    ctx, handle = _derived(pin)
    assert handle["sha"] == later and handle["from_sha"] == pin.new
    assert "noop" not in handle and pin.branch_head() == pin.tip()


def test_a_pin_and_release_on_different_lines_are_a_charged_ask(pin: Pin) -> None:
    """If this fails shas that are not ancestors of each other are bumped or
    no-op'd, or are retried blind."""
    side, main_side = _merge_side(pin)
    pin.precis_commit(pin.texts("0.50.0", side))
    pin.release(main_side, "0.50.0")
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _asks(ctx, "not ancestors of each other")
    _nothing_left(pin)
    assert pin.rig.launches == []


def test_an_unreadable_ancestry_is_attempt_free(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a mirror git cannot read for the pin ordering is charged."""
    real = repo_release._on_base
    calls: list[tuple[str, str]] = []

    def on_base(root: Path, sha: str, tip: str) -> bool | None:
        calls.append((sha, tip))
        return None if len(calls) > 1 else real(root, sha, tip)

    monkeypatch.setattr(repo_release, "_on_base", on_base)
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _attempt_free(ctx, "release-read")
    _nothing_left(pin)


def test_a_deploy_floor_above_the_release_is_a_charged_ask_never_an_edit(
    pin: Pin,
) -> None:
    """If this fails group_vars' floor is lowered to the release version."""
    gv = tpb.GROUP_VARS.replace(
        'autocatpath_min_version: "0.49.0"', 'autocatpath_min_version: "0.55.0"'
    )
    pin.precis_commit({"deploy/group_vars/all.yml": gv})
    ctx, handle = _submitted(pin)
    assert pb.poll(ctx, handle) is True
    _asks(ctx, "floor is never lowered")
    _nothing_left(pin)
    assert pin.rig.launches == []


def test_finish_follows_a_phase_inside_a_phase(pin: Pin) -> None:
    """If this fails a slice two phases down from the node the pin chain is
    blocked by is left out of the batch (the children step expanded one
    level)."""
    _batch(pin)
    for nid, status in ((86, "doing"), (88, "doing"), (89, "done")):
        pin.store.add_todo(
            nid,
            status=status,
            meta={"landed_repo": "catpath"} if nid == 89 else None,
        )
    pin.store.add_todo(99, status="open")
    pin.rows += [
        {"id": 86, "parent_id": 70, "kind": "todo"},
        {"id": 88, "parent_id": 86, "kind": "todo"},
        {"id": 89, "parent_id": 88, "kind": "todo"},
    ]
    pin.edges.append((REL, 86))
    pin.links.append((89, 99))
    pb.finish(pin.store, PIN, SHA)
    assert pin.record()["slices"] == [81, 85, 89]
    assert pin.record()["gripes"] == [91, 92, 99]


def test_a_land_that_finished_before_the_handoff_stamp_is_not_overwritten(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails the hand-off stamp writes ``handed`` over a record the
    fast land already moved to ``landed``."""
    ctx, handle = _handoff(pin)
    real = _mint.mint_job_once

    def fast_land(store: Any, **kw: Any) -> tuple[int, bool]:
        out = real(store, **kw)
        pb.finish(store, PIN, SHA)  # the land ran and finished inside the mint
        return out

    monkeypatch.setattr(_mint, "mint_job_once", fast_land)
    assert pb.poll(ctx, handle) is True
    assert ctx.status == "succeeded" and ctx.failure is None
    assert pin.record()["state"] == "landed" and pin.record()["landed_sha"] == SHA
    assert "land_job" not in pin.record() and len(pin.mints) == 1


def test_a_commit_made_before_a_crash_is_adopted_not_charged(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a worker that died after the branch moved, before the
    handle was saved, charges the replayed poll a failed compare-and-swap
    instead of adopting the commit that is already exactly the pin bump."""
    ctx, handle = _derived(pin)
    _good_result(pin)
    _finish(handle, 0)

    def crash(*a: Any, **kw: Any) -> Any:
        raise OSError("worker died")

    with monkeypatch.context() as m:
        m.setattr(pb, "_checkout_tree", crash)
        with pytest.raises(OSError, match="worker died"):
            pb.poll(ctx, handle)
    head = pin.branch_head()
    assert head is not None and head != pin.tip() and "commit" not in handle
    assert pb.poll(ctx, handle) is False, ctx.failure
    assert handle["phase"] == "verify" and handle["commit"] == head
    assert pin.branch_head() == head and ctx.status is None
    assert _g(pin.rig.clone, "rev-list", "--count", f"{pin.tip()}..{head}") == "1"


# ── the lander's exemption (step 4 R3) ─────────────────────────────


def _landable(pin: Pin) -> tuple[Path, str, str]:
    """A committed bump as the lander meets it: the node carries the op and
    the tree is a clean checkout of the commit. ``(tree, base, commit)``."""
    _committed(pin)
    pin.store.refs[PIN].meta["op"] = "pin_bump"
    record = pin.record()
    return pin.tree(), str(record["base"]), str(record["commit"])


def _land_refusal(pin: Pin, tree: Path, base: str) -> str | None:
    paths = run_slice.changed_paths(tree, base)
    assert paths is not None
    return pb.land_refusal(pin.store, PIN, tree, base, paths)


def test_the_landers_check_passes_the_nodes_own_commit(pin: Pin) -> None:
    """If this fails the lander keeps deploy/group_vars/all.yml denied for
    the pin bump's own commit (every bump ends in a coordinator ask), or
    stops passing it once main has moved on or the record reads handed."""
    tree, base, _ = _landable(pin)
    assert _land_refusal(pin, tree, base) is None
    pin.store.refs[PIN].meta["pin_bump"]["state"] = "handed"
    moved = pin.precis_commit({"README.md": "main moved\n"})
    assert _land_refusal(pin, tree, moved) is None


def test_the_landers_check_passes_ships_rebase_of_the_commit(pin: Pin) -> None:
    """If this fails a re-land after a failed ship is refused: ship rebased
    the branch onto the moved main, so its head is a new sha carrying the
    same change."""
    tree, _, commit = _landable(pin)
    moved = pin.precis_commit({"README.md": "main moved\n"})
    _g(tree, "rebase", "-q", moved)
    assert _g(tree, "rev-parse", "HEAD") != commit
    assert _land_refusal(pin, tree, moved) is None


def _amend_other_line(tree: Path) -> str:
    """Rewrite the head so group_vars also changes a second line; its sha."""
    path = tree / "deploy" / "group_vars" / "all.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("tail: 1", "tail: 2"),
        encoding="utf-8",
    )
    _g(tree, "commit", "-q", "-a", "--amend", "--no-edit")
    return _g(tree, "rev-parse", "HEAD")


def _spoil_op(p: Pin, tree: Path) -> None:
    del p.store.refs[PIN].meta["op"]


def _spoil_record(p: Pin, tree: Path) -> None:
    del p.store.refs[PIN].meta["pin_bump"]


def _spoil_landed(p: Pin, tree: Path) -> None:
    p.record()["state"] = "landed"


def _spoil_noop(p: Pin, tree: Path) -> None:
    p.record()["noop"] = True


def _spoil_extra_file(p: Pin, tree: Path) -> None:
    (tree / "README.md").write_text("extra\n", encoding="utf-8")


def _spoil_dirty_pin_file(p: Pin, tree: Path) -> None:
    with (tree / "uv.lock").open("a", encoding="utf-8") as fh:
        fh.write("# edited after the commit\n")


def _spoil_second_commit(p: Pin, tree: Path) -> None:
    _g(tree, "commit", "-q", "--allow-empty", "-m", "chore: one more")


def _spoil_rewritten_head(p: Pin, tree: Path) -> None:
    _amend_other_line(tree)


def _spoil_record_adopts_rewrite(p: Pin, tree: Path) -> None:
    p.record()["commit"] = _amend_other_line(tree)


def _spoil_lower_version(p: Pin, tree: Path) -> None:
    p.record()["version"] = "0.40.0"


def _spoil_other_version(p: Pin, tree: Path) -> None:
    p.record()["version"] = "0.51.0"


@pytest.mark.parametrize(
    ("spoil", "needle"),
    [
        (_spoil_op, "not a pin-bump node"),
        (_spoil_record, "not a pin-bump node"),
        (_spoil_landed, "not committed or handed"),
        (_spoil_noop, "not committed or handed"),
        (_spoil_extra_file, "outside the pin files"),
        (_spoil_dirty_pin_file, "not the head commit's"),
        (_spoil_second_commit, "not one commit on top"),
        (_spoil_rewritten_head, "nor a rebase of it"),
        (_spoil_record_adopts_rewrite, "changes more than its"),
        (_spoil_lower_version, "does not raise"),
        (_spoil_other_version, "changes more than its"),
    ],
)
def test_the_landers_check_refuses_anything_but_the_pure_bump(
    pin: Pin, spoil: Any, needle: str
) -> None:
    """If this fails the human-only group_vars file can reach main through a
    pin node with more than the one floor line raised to the record's
    version, from a todo that is no pin node, or from a branch that is not
    the single proven commit."""
    tree, base, _ = _landable(pin)
    spoil(pin, tree)
    assert needle in str(_land_refusal(pin, tree, base))


def test_the_landers_check_refuses_a_merge_commit_head(pin: Pin) -> None:
    """If this fails a merge commit whose tree equals the pin bump's passes
    as the bump: its second parent can carry anything."""
    tree, base, commit = _landable(pin)
    merge = _g(
        tree, "commit-tree", f"{commit}^{{tree}}", "-p", base, "-p", commit, "-m", "m"
    )
    _g(tree, "reset", "-q", "--hard", merge)
    assert "not one commit on top" in str(_land_refusal(pin, tree, base))


def test_the_landers_check_refuses_a_second_floor_line_under_the_head(
    pin: Pin,
) -> None:
    """If this fails a rebased bump passes on a parent that carries two
    floor lines: the one-line proof has nothing single to compare with."""
    tree, base, commit = _landable(pin)
    _g(tree, "reset", "-q", "--hard", base)
    with (tree / "deploy" / "group_vars" / "all.yml").open("a", encoding="utf-8") as fh:
        fh.write('autocatpath_min_version: "0.49.0"\n')
    _g(tree, "commit", "-q", "-a", "-m", "chore: a second floor line")
    twice = _g(tree, "rev-parse", "HEAD")
    _g(tree, "cherry-pick", commit)
    assert "does not raise" in str(_land_refusal(pin, tree, twice))


def test_a_rebased_head_needs_a_verified_land_base(pin: Pin) -> None:
    """If this fails a made-up tracking ref admits group_vars: the branch
    hides a commit rewriting the file under a pin-shaped head, the ref
    points at that commit, and ship later lands both on the real main.
    The record's own commit still passes on a ref name (its parent is the
    record's base)."""
    tree, base, commit = _landable(pin)
    paths = list(pb.PIN_PATHS)
    assert pb.land_refusal(pin.store, PIN, tree, "main", paths) is None
    _g(tree, "reset", "-q", "--hard", base)
    path = tree / "deploy" / "group_vars" / "all.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("# comment:", "# rewritten:"),
        encoding="utf-8",
    )
    _g(tree, "commit", "-q", "-a", "-m", "chore: hidden rewrite")
    hidden = _g(tree, "rev-parse", "HEAD")
    _g(tree, "cherry-pick", commit)
    _g(tree, "update-ref", "refs/remotes/origin/main", hidden)
    with pytest.raises(pb.LandUnreadable, match="not a verified tip"):
        _land_refusal(pin, tree, "origin/main")
    # against the real tip the hidden commit shows: two commits, not one
    assert "not one commit on top" in str(_land_refusal(pin, tree, base))


def test_what_git_cannot_answer_is_unreadable_not_a_verdict(
    pin: Pin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If this fails a git read that failed admits the file, or is reported
    as a verdict on the branch (the lander would cap the node for a ruling
    instead of building the bump again)."""
    tree, _, commit = _landable(pin)
    with pytest.raises(pb.LandUnreadable, match="git could not read"):
        pb.land_refusal(pin.store, PIN, tree, "no-such-ref", list(pb.PIN_PATHS))
    moved = pin.precis_commit({"README.md": "main moved\n"})
    _g(tree, "rebase", "-q", moved)
    monkeypatch.setattr(pb, "_change", lambda *a: None)
    with pytest.raises(pb.LandUnreadable, match="git could not read"):
        _land_refusal(pin, tree, moved)
    monkeypatch.setattr(pb, "_is_pin_op", lambda meta: 1 / 0)
    with pytest.raises(pb.LandUnreadable, match="the check itself failed"):
        _land_refusal(pin, tree, moved)
