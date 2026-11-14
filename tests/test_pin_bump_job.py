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
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from precis.plans import lanes
from precis.workers import factory_git_mirror
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


@dataclass
class Pin:
    rig: Rig
    store: _Store
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
    store = _Store()
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

    monkeypatch.setattr(factory_git_mirror, "ls_remote_tag", ls_remote_tag)
    monkeypatch.setattr(factory_git_mirror, "fetch_source_mirror", fetch_source_mirror)
    monkeypatch.setattr(
        repo_release, "_auth_env", lambda: {"PATH": "/bin", "GH_FAKE": TOKEN}
    )
    monkeypatch.setattr(land_slice, "_process_alive", lambda pid: False)
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


def _result(pin: Pin, data: Any) -> None:
    text = data if isinstance(data, str) else json.dumps(data)
    (pin.run_dir() / pb.RESULT_FILE).write_text(text, encoding="utf-8")


def _good_result(pin: Pin, key: str = KEY, engine: str = "0.50.0") -> None:
    _result(pin, {"key": key, "engine": engine})


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


def test_spec_declares_three_required_params_and_is_registered_nowhere() -> None:
    """If this fails the schema admits other params, or the spec leaked into
    the registry before the switch round."""
    assert pb.SPEC.name == "pin_bump"
    assert pb.PARAMS_SCHEMA["required"] == ["todo_id", "release_todo", "pin_repo"]
    assert pb.PARAMS_SCHEMA["additionalProperties"] is False
    from precis.workers import job_types

    assert "pin_bump" not in job_types._BUILTIN_JOB_TYPES
    assert "pin_bump" not in job_types._REGISTRY


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
    }
    assert ctx.status is None and ctx.failure is None
    assert handle["phase"] == "handoff" and handle["commit"] == commit
    assert ctx.meta_set["compute_handle"] is handle
    due = datetime.strptime(handle["not_before"], TS).replace(tzinfo=UTC)
    assert 100 < (due - datetime.now(UTC)).total_seconds() < 130
    assert pin.tree().is_dir() and _g(pin.tree(), "status", "--porcelain") == ""
    assert _g(pin.tree(), "rev-parse", "HEAD") == commit
    assert pin.store.comments(PIN) == []
    _clean(pin, ctx)


def test_the_handoff_stub_re_parks_every_120_s_and_does_nothing_else(pin: Pin) -> None:
    """If this fails the final-phase stub finishes the job, mints something,
    or touches the tree, branch or record."""
    ctx, handle = _committed(pin)
    rec, head, launches = dict(pin.record()), pin.branch_head(), len(pin.rig.launches)
    assert pb.poll(ctx, handle) is False  # not due: nothing at all
    first = handle["not_before"]
    _due_now(handle)
    assert pb.poll(ctx, handle) is False
    assert (
        handle["not_before"] != "2000-01-01T00:00:00Z" and handle["not_before"] >= first
    )
    assert ctx.status is None
    assert pin.record() == rec and pin.branch_head() == head
    assert len(pin.rig.launches) == launches and pin.tree().is_dir()


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
