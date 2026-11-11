"""The release gate's red-test waiver, store-backed: the setting, the
waiver gripe, the waived verdict, ``ci_red_digest``'s wiring on a real
release-fix iteration, and the revert safety net."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis import settings as psettings
from precis.plans import gates
from precis.plans import red_waiver as W
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import Tag
from precis.workers.job_types import ci_red_digest as crd
from precis.workers.job_types import release_confirm as rcf
from precis.workers.job_types import run_slice as rs
from tests._refs import insert_gripe
from tests.test_red_waiver import r266_state

pytestmark = pytest.mark.db

SHA = "c0ffee" + "1" * 34
RNG = f"{'9' * 12}..{SHA[:12]}"
RUN = "31"
TEST_ID = "tests/test_widgets.py::test_blue"
DIGEST = (
    "== test-linux (3) ==\n"
    "  failed ids (1):\n"
    f"    {TEST_ID}\n"
    f"  FAILED {TEST_ID} - AssertionError: assert 1 == 2\n"
    "  =========== 1 failed, 40 passed in 3.2s ===========\n"
)
#: ``gh run view --json``: every job concluded, one of them red.
RUN_STATE: dict[str, Any] = {
    "status": "completed",
    "attempt": 1,
    "jobs": [
        {"name": "lint", "conclusion": "success"},
        {"name": "test-linux (3)", "conclusion": "failure"},
        {"name": "test-docs", "conclusion": "skipped"},
    ],
}
OK_PATHS = ["src/precis/handlers/gripe.py", "tests/test_widgets.py"]


@pytest.fixture(autouse=True)
def _fresh_settings() -> Any:
    psettings.invalidate()
    yield
    psettings.invalidate()


class Ctx:
    """The DispatchContext surface ``ci_red_digest`` uses."""

    def __init__(self, store: Store, params: dict[str, Any]) -> None:
        self.store = store
        self.meta: dict[str, Any] = {"params": params}
        self.chunks: list[tuple[str, str]] = []
        self.failures: list[str] = []
        self.status: str | None = None
        self.stamped: dict[str, Any] = {}

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.stamped.update(kw)

    def record_failure(self, msg: str, **_kw: Any) -> None:
        self.failures.append(msg)

    def set_status(self, value: str) -> None:
        self.status = value


def _status(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t.value) for t in store.tags_for(ref_id)}


def _meta(store: Store, kind: str, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind=kind, id=ref_id)
    assert ref is not None
    return dict(ref.meta or {})


def _comments(store: Store, todo_id: int) -> list[str]:
    return [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == "todo_comment"
    ]


def _iteration(store: Store, rn: str = "r7") -> tuple[T.PlanMint, T.PlanMint]:
    """A release pass cut as ``rn`` plus its first release-fix iteration."""
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], rn, SHA, RNG, repo="o/r")
    R.record_ci_verdict(store, SHA, RNG, "red", RUN)
    it = T.mint_release_fix(store, mint.ids["loop"], iteration=1)
    return mint, it


@pytest.fixture
def git_stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """The cut range and the culprit search, without a real repo."""
    seen: dict[str, Any] = {
        "paths": list(OK_PATHS),
        "culprits": [("abc1234", "touch widgets")],
        "state": RUN_STATE,
        "fetch_env": [],
    }

    def paths(root: Path, rng: str, *, env: Any = None) -> list[str]:
        seen["fetch_env"].append(env)
        return seen["paths"]

    monkeypatch.setattr(rs, "slice_root", lambda: tmp_path)
    monkeypatch.setattr(W, "range_paths", paths)
    monkeypatch.setattr(W, "culprit_commits", lambda root, rng, ids: seen["culprits"])
    monkeypatch.setattr(crd, "_run_state", lambda run_id: seen["state"])
    return seen


def _waiver_gripes(store: Store, rn: str) -> list[int]:
    return [g for g, _m in W._gripes_tagged(store, f"release-waiver:{rn}")]


# ── the setting ────────────────────────────────────────────────────


def test_setting_defaults_on_and_unknown_reads_off(store: Store) -> None:
    assert psettings.REGISTRY[W.SETTING_KEY].default == "on"
    assert W.waiver_enabled(store) is True  # no stored row: the default
    for value, want in (
        ("off", False),
        ("OFF", False),
        ("on", True),
        (" On ", True),
        ("true", False),
        ("", False),
        ("bogus", False),
    ):
        psettings.set_setting(W.SETTING_KEY, value, store=store)
        psettings.invalidate(W.SETTING_KEY)
        assert W.waiver_enabled(store) is want, value


def test_a_failed_settings_read_reads_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lenient resolver would fall back to the default ``on``; the
    waiver must not grant on a DB it could not read."""
    assert W.waiver_enabled(store) is True

    def boom(store: Any, key: str) -> str | None:
        raise RuntimeError("db down")

    monkeypatch.setattr(psettings, "_db_read_strict", boom)
    assert W.waiver_enabled(store) is False
    monkeypatch.setattr(psettings, "_db_read_strict", lambda s, k: None)
    assert W.waiver_enabled(store) is True  # genuinely unset: the default


# ── the gripe ──────────────────────────────────────────────────────


def _file(store: Store, rn: str = "r41", **kw: Any) -> int:
    args: dict[str, Any] = {
        "rn": rn,
        "sha": SHA,
        "rng": RNG,
        "run_id": RUN,
        "failures": {TEST_ID: "AssertionError: assert 1 == 2"},
        "culprits": [("abc1234", "touch widgets")],
    }
    args.update(kw)
    return W.file_waiver_gripe(store, **args)


def test_waiver_gripe_shape(store: Store) -> None:
    gid = _file(store)
    ref = store.get_ref(kind="gripe", id=gid)
    assert ref is not None
    assert ref.title == f"release r41 shipped with known-red tests: {TEST_ID}"
    assert ref.prio == 1
    assert _status(store, gid) == "open"
    assert {
        "fix:code",
        "broken-build",
        "release-waiver:r41",
        "project:precis",
    } <= _tags(store, gid)
    rec = dict(ref.meta)["release_waiver"]
    assert rec["tests"] == [TEST_ID] and rec["rN"] == "r41"
    assert rec["run_id"] == RUN and rec["culprits"] == ["abc1234"]
    assert dict(ref.meta)["dispatch_score"] > 0
    body = " ".join(str(c.text) for c in store.chunks.list_chunks_for_ref(gid))
    assert RUN in body and TEST_ID in body and "abc1234 touch widgets" in body


def test_waiver_gripe_is_idempotent_per_release(store: Store) -> None:
    first = _file(store)
    assert _file(store) == first
    assert _file(store, rn="r42") != first
    assert _waiver_gripes(store, "r41") == [first]


def _set_status(store: Store, gid: int, status: str) -> None:
    store.add_tag(
        gid, Tag.closed("STATUS", status), set_by="agent", replace_prefix=True
    )


def test_any_unfixed_waiver_gripe_blocks_its_ids_whatever_the_release(
    store: Store,
) -> None:
    far = _file(store, rn="r12", failures={"tests/test_old.py::test_x": ""})
    _file(store, rn="r41", failures={TEST_ID: ""})
    # not just r<N-1>: r12's id still counts from r99
    assert W.open_waived_tests(store, "r99") == {
        "tests/test_old.py::test_x",
        TEST_ID,
    }
    # the release's own gripe is excluded (a retried digest converges)
    assert W.open_waived_tests(store, "r41") == {"tests/test_old.py::test_x"}
    assert W.open_waived_tests(store, "weird") == {
        "tests/test_old.py::test_x",
        TEST_ID,
    }
    for status in sorted(W.GRIPE_FIXED_STATUSES):
        _set_status(store, far, status)
        assert W.open_waived_tests(store, "r99") == {TEST_ID}, status
    for status in ("open", "triaged", "fixing", "ready_for_fix", "parked"):
        _set_status(store, far, status)
        assert "tests/test_old.py::test_x" in W.open_waived_tests(store, "r99"), status


def test_a_non_object_waiver_meta_is_ignored(store: Store) -> None:
    """The nit: ``meta.release_waiver`` JSON null (or any non-object) is
    not a waiver record, tag or no tag."""
    with store.pool.connection() as conn:
        for i, junk in enumerate(("null", '"x"', "[]", "1", "true")):
            gid = insert_gripe(store, f"fake {i}").id
            store.add_tag(gid, Tag.open("release-waiver:r5"), set_by="agent")
            conn.execute(
                "UPDATE refs SET meta = jsonb_build_object('release_waiver', "
                "%s::jsonb) WHERE ref_id = %s",
                (junk, gid),
            )
    assert W._gripes_tagged(store, "release-waiver:r5") == []
    assert W.waiver_gripe_for(store, "r5") is None
    assert W.open_waived_tests(store, "r99") == set()
    assert W.mint_reverts(store, "r99") == []


def test_waived_verdict_opens_tests_green_and_cannot_be_redigested(
    store: Store,
) -> None:
    R.record_ci_verdict(store, SHA, RNG, "red", RUN)
    spec = {"kind": "tests-green", "args": {"range": RNG}}
    assert not gates.evaluate(store, spec, todo_id=1).open
    assert R.newest_red_run(store, SHA) == RUN
    line = W.record_waived_verdict(
        store, sha=SHA, rng=RNG, run_id=RUN, attempt=2, gripe_id=77, tests=[TEST_ID]
    )
    assert line.startswith("ci verdict recorded")
    verdict = gates.evaluate(store, spec, todo_id=1)
    assert verdict.open and "WAIVED by gr77" in verdict.why
    assert R.newest_red_run(store, SHA) is None
    [row] = [r for r in gates.read_ci_verdicts(store) if r["sha"] == SHA[:12]]
    assert row["verdict"] == "green"
    assert row["waived"] == {
        "gripe": 77,
        "tests": [TEST_ID],
        "run_id": RUN,
        "attempt": 2,
    }
    assert row["run_id"] == RUN


# ── ci_red_digest wiring ───────────────────────────────────────────


def _digest(
    store: Store, it: T.PlanMint, log: str = DIGEST
) -> tuple[bool, list[str], Ctx]:
    ctx = Ctx(store, {"todo_id": it.ids["digest"]})
    granted, steps = crd._try_waiver(
        ctx, it.ids["digest"], R.release_of(store, it.ids["digest"]) or {}, RUN, log, {}
    )
    return granted, steps, ctx


def test_grant_files_the_gripe_records_waived_and_closes_the_iteration(
    store: Store, git_stub: dict[str, Any]
) -> None:
    mint, it = _iteration(store)
    granted, steps, ctx = _digest(store, it)
    assert granted and ctx.status == "succeeded" and ctx.failures == []
    [gid] = _waiver_gripes(store, "r7")
    text = "\n".join(steps)
    for needle in (
        "release.red_waiver is on",
        "1 failing test id(s) parsed",
        "cut range",
        f"run {RUN}#1 completed, every job success/failure/skipped",
        "still-unfixed waiver gripes: none",
        "GRANTED",
        f"filed gr{gid}",
        "ci verdict recorded",
        f"waived: gr{gid}",
    ):
        assert needle in text, needle
    assert ctx.chunks[-1][0] == "job_summary"
    assert f"WAIVED by gr{gid}" in ctx.chunks[-1][1]
    assert ctx.stamped["red_waiver"]["gripe"] == gid
    assert len(git_stub["fetch_env"]) == 1  # the range read got the git env
    # fix, recut and poll are closed with the waiver note; no re-cut runs
    for key in ("fix", "recut", "poll"):
        assert _status(store, it.ids[key]) == "done", key
        assert any(
            c.startswith(f"waived: gr{gid}") for c in _comments(store, it.ids[key])
        )
    # the digest node itself is the running job's: left to its success
    assert _status(store, it.ids["digest"]) == "open"
    # the gate and the loop's until read the waived verdict as green
    gate = _meta_of_gate(store, mint)
    assert gates.evaluate(store, gate, todo_id=mint.ids["gate"]).open
    until = _meta(store, "todo", mint.ids["loop"])["loop"]["until"]
    assert gates.evaluate(store, until, todo_id=mint.ids["loop"]).open


def _meta_of_gate(store: Store, mint: T.PlanMint) -> dict[str, Any]:
    return dict(_meta(store, "todo", mint.ids["gate"])["gate"])


def test_grant_survives_a_retry_with_one_gripe(
    store: Store, git_stub: dict[str, Any]
) -> None:
    _mint, it = _iteration(store)
    first = _digest(store, it)
    assert first[0]
    # a retried digest (job re-dispatch) converges on the same gripe
    again = _digest(store, it)
    assert again[0]
    assert len(_waiver_gripes(store, "r7")) == 1


def test_refusal_leaves_the_loop_untouched_and_names_the_reason(
    store: Store, git_stub: dict[str, Any]
) -> None:
    git_stub["paths"] = [*OK_PATHS, "scripts/lib/ci_run.py"]
    mint, it = _iteration(store)
    granted, steps, ctx = _digest(store, it)
    assert not granted and ctx.status is None
    assert any("REFUSED" in s and "scripts/lib/ci_run.py" in s for s in steps)
    assert _waiver_gripes(store, "r7") == []
    assert R.newest_red_run(store, SHA) == RUN  # still red
    for key in ("fix", "recut", "poll"):
        assert _status(store, it.ids[key]) == "open"
    assert not gates.evaluate(
        store, _meta_of_gate(store, mint), todo_id=mint.ids["gate"]
    ).open


def test_off_setting_runs_the_loop_as_before(
    store: Store, git_stub: dict[str, Any]
) -> None:
    psettings.set_setting(W.SETTING_KEY, "off", store=store)
    psettings.invalidate(W.SETTING_KEY)
    _mint, it = _iteration(store)
    granted, steps, _ctx = _digest(store, it)
    assert not granted
    assert steps == [f"waiver: {W.SETTING_KEY} is off; the fix loop runs"]
    assert _waiver_gripes(store, "r7") == []


def test_a_still_unfixed_waiver_of_the_same_test_refuses_any_release(
    store: Store, git_stub: dict[str, Any]
) -> None:
    old = _file(store, rn="r2")  # long ago, never fixed
    _mint, it = _iteration(store, rn="r7")
    granted, steps, _ctx = _digest(store, it)
    assert not granted
    assert any("already waived and still unfixed" in s for s in steps)
    assert _waiver_gripes(store, "r7") == []
    # once that gripe is fixed (any fixed status) the id may be waived again
    _set_status(store, old, "verify")
    granted, _steps, _ctx = _digest(store, it)
    assert granted and len(_waiver_gripes(store, "r7")) == 1


def test_non_pytest_red_and_unreadable_range_are_refused(
    store: Store, git_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _mint, it = _iteration(store)
    granted, steps, _ctx = _digest(store, it, "== lint ==\n  Found 1 error.\n")
    assert not granted
    assert any("not a pytest red" in s for s in steps)
    monkeypatch.setattr(W, "range_paths", lambda root, rng, **kw: None)
    granted, steps, _ctx = _digest(store, it)
    assert not granted and any("could not be read" in s for s in steps)
    assert _waiver_gripes(store, "r7") == []


def _refused(store: Store, it: T.PlanMint, log: str = DIGEST) -> list[str]:
    granted, steps, ctx = _digest(store, it, log)
    assert not granted and ctx.status is None
    assert _waiver_gripes(store, "r7") == []
    assert R.newest_red_run(store, SHA) == RUN  # the red verdict is untouched
    for key in ("fix", "recut", "poll"):
        assert _status(store, it.ids[key]) == "open"
    return steps


@pytest.mark.parametrize(
    "odd", ["cancelled", "timed_out", "startup_failure", "action_required", "neutral"]
)
def test_a_run_with_a_job_that_never_concluded_is_refused(
    store: Store, git_stub: dict[str, Any], odd: str
) -> None:
    git_stub["state"] = {
        "status": "completed",
        "jobs": [*RUN_STATE["jobs"], {"name": "test-linux (5)", "conclusion": odd}],
    }
    _mint, it = _iteration(store)
    steps = _refused(store, it)
    assert any("not complete" in s and f"test-linux (5)={odd}" in s for s in steps)


R266_DIGEST = (
    "== gate-fast (3.13, 4) ==\n"
    "  failed ids (1):\n"
    "    tests/test_widgets.py::test_blue\n"
    "  FAILED tests/test_widgets.py::test_blue - AssertionError: assert 1 == 2\n"
    "  =========== 1 failed, 40 passed in 3.2s ===========\n"
    "== gate-fast (3.13, 8) ==\n"
    "  failed ids (1):\n"
    "    tests/test_gadgets.py::test_red\n"
    "  FAILED tests/test_gadgets.py::test_red - ValueError: boom\n"
    "  =========== 1 failed, 39 passed in 3.0s ===========\n"
)


def test_the_waiver_grants_on_the_real_r266_red_run_shape(
    store: Store, git_stub: dict[str, Any]
) -> None:
    """N1 (proof): run 42843464447's jobs — two failed gate-fast shards,
    every conditional job (coverage-db, test-other, failed-first,
    test-docs, the soak/test-linux matrix, verdict-soak, publish) skipped.
    The waiver must grant on that, with the marker bound to the run."""
    git_stub["state"] = r266_state()
    git_stub["culprits"] = [("abc1234", "touch widgets and gadgets")]
    _mint, it = _iteration(store)
    granted, steps, ctx = _digest(store, it, R266_DIGEST)
    assert granted and ctx.status == "succeeded" and ctx.failures == [], steps
    [gid] = _waiver_gripes(store, "r7")
    [row] = [r for r in gates.read_ci_verdicts(store) if r["sha"] == SHA[:12]]
    assert row["waived"] == {
        "gripe": gid,
        "tests": [
            "tests/test_widgets.py::test_blue",
            "tests/test_gadgets.py::test_red",
        ],
        "run_id": RUN,
        "attempt": 1,
    }


def test_a_run_whose_attempt_is_unreadable_cannot_be_bound(
    store: Store, git_stub: dict[str, Any]
) -> None:
    git_stub["state"] = {k: v for k, v in RUN_STATE.items() if k != "attempt"}
    _mint, it = _iteration(store)
    assert any("unbindable" in s for s in _refused(store, it))


# ── a run still in progress parks, it is not refused for good ──────


def _handle(tmp_path: Path) -> dict[str, Any]:
    return {"run_id": RUN, "exit_file": str(tmp_path / "exit")}


def test_an_in_progress_run_parks_the_digest_then_grants_when_it_completes(
    store: Store, git_stub: dict[str, Any], tmp_path: Path
) -> None:
    """N2: the waiver needs the whole run. While it is in progress the
    digest parks (``_park_for_run``: ``not_before``, a park counted) and
    changes nothing; the re-digest after completion grants."""
    git_stub["state"] = {**RUN_STATE, "status": "in_progress"}
    mint, it = _iteration(store)
    ctx = Ctx(store, {"todo_id": it.ids["digest"]})
    handle = _handle(tmp_path)
    params = {"todo_id": it.ids["digest"]}
    assert crd._digest_done(ctx, handle, params, DIGEST) is False  # still running
    assert handle["parks"] == 1 and handle["not_before"]
    assert ctx.stamped["compute_handle"] is handle
    assert ctx.status is None and ctx.failures == []
    assert any("park 1/" in text for kind, text in ctx.chunks if kind == "job_event")
    assert _waiver_gripes(store, "r7") == []
    assert R.newest_red_run(store, SHA) == RUN
    for key in ("fix", "recut", "poll"):
        assert _status(store, it.ids[key]) == "open"
    # the run completes; the re-digest (handle carries its park count) grants
    git_stub["state"] = RUN_STATE
    handle.pop("not_before")
    assert crd._digest_done(ctx, handle, params, DIGEST) is True
    assert ctx.status == "succeeded"
    assert len(_waiver_gripes(store, "r7")) == 1
    gate = _meta_of_gate(store, mint)
    assert gates.evaluate(store, gate, todo_id=mint.ids["gate"]).open


def test_a_run_that_never_completes_fails_the_job_at_the_park_cap(
    store: Store, git_stub: dict[str, Any], tmp_path: Path
) -> None:
    git_stub["state"] = {**RUN_STATE, "status": "queued"}
    _mint, it = _iteration(store)
    ctx = Ctx(store, {"todo_id": it.ids["digest"]})
    handle = {**_handle(tmp_path), "parks": crd._PARK_CAP}
    assert crd._digest_done(ctx, handle, {"todo_id": it.ids["digest"]}, DIGEST) is True
    assert ctx.status is None and ctx.failures
    assert "queued" in ctx.failures[0] and "parks" in ctx.failures[0]
    assert _waiver_gripes(store, "r7") == []


def test_an_unreadable_run_state_is_refused(
    store: Store, git_stub: dict[str, Any]
) -> None:
    git_stub["state"] = None
    _mint, it = _iteration(store)
    assert any("unreadable" in s for s in _refused(store, it))


def test_the_failed_first_red_is_refused(
    store: Store, git_stub: dict[str, Any]
) -> None:
    _mint, it = _iteration(store)
    log = DIGEST.replace("test-linux (3)", "failed-first")
    steps = _refused(store, it, log)
    assert any("full test matrix never ran" in s for s in steps)


def test_a_collection_error_is_refused(store: Store, git_stub: dict[str, Any]) -> None:
    _mint, it = _iteration(store)
    log = DIGEST + "  ERROR tests/test_broken.py - ImportError: nope\n"
    assert any("not a test" in s or "collection" in s for s in _refused(store, it, log))


def test_a_count_mismatch_is_refused(store: Store, git_stub: dict[str, Any]) -> None:
    _mint, it = _iteration(store)
    log = DIGEST.replace("1 failed, 40", "3 failed, 38")
    assert any("summary counts 3" in s for s in _refused(store, it, log))


def test_no_attributable_culprit_refuses_the_waiver(
    store: Store, git_stub: dict[str, Any]
) -> None:
    git_stub["culprits"] = []
    _mint, it = _iteration(store)
    steps = _refused(store, it)
    assert any("no attributable culprit" in s for s in steps)


def test_a_failed_settings_read_refuses_at_the_digest(
    store: Store, git_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(store: Any, key: str) -> str | None:
        raise RuntimeError("db down")

    _mint, it = _iteration(store)
    monkeypatch.setattr(psettings, "_db_read_strict", boom)
    granted, steps, ctx = _digest(store, it)
    assert not granted and ctx.status is None
    assert steps == [f"waiver: {W.SETTING_KEY} is off; the fix loop runs"]
    assert _waiver_gripes(store, "r7") == []


def test_unrecorded_verdict_fails_the_job_but_keeps_one_gripe(
    store: Store, git_stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _mint, it = _iteration(store)
    monkeypatch.setattr(
        W, "record_waived_verdict", lambda store, **kw: "ci verdict not recorded: x"
    )
    granted, _steps, ctx = _digest(store, it)
    assert granted and ctx.status is None
    assert "verdict was not recorded" in ctx.failures[0]
    assert len(_waiver_gripes(store, "r7")) == 1
    assert _status(store, it.ids["fix"]) == "open"


def test_iteration_nodes_are_the_digest_dependents_only(store: Store) -> None:
    mint, it = _iteration(store)
    nodes = W.iteration_nodes(store, it.ids["digest"])
    assert nodes == sorted([it.ids["fix"], it.ids["recut"], it.ids["poll"]])
    assert mint.ids["gate"] not in nodes and mint.ids["loop"] not in nodes


# ── the revert safety net ──────────────────────────────────────────


def _revert_roots(store: Store, gid: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND retired_at IS NULL "
            "AND meta ->> 'release_revert_of' = %s ORDER BY ref_id",
            (str(gid),),
        ).fetchall()
    return [int(r[0]) for r in rows]


def test_next_confirm_mints_one_revert_for_an_unfixed_waiver(store: Store) -> None:
    gid = _file(store, rn="r41")
    assert W.mint_reverts(store, "r41") == []  # the waived release itself
    [(g, root)] = W.mint_reverts(store, "r42")
    assert g == gid
    assert _revert_roots(store, gid) == [root]
    ref = store.get_ref(kind="todo", id=root)
    assert ref is not None and ref.title == f"Revert the culprits of gr{gid}"
    meta = dict(ref.meta)
    assert meta["claim_band"] == "release" and meta["release_revert_of"] == gid
    # the build node carries the revert brief and the gripe report
    with store.pool.connection() as conn:
        [(build,)] = conn.execute(
            "SELECT ref_id FROM refs WHERE parent_id = %s AND kind = 'todo' "
            "AND title = %s",
            (root, f"Revert the culprits of gr{gid}"),
        ).fetchall()
    brief = " ".join(str(c.text) for c in store.chunks.list_chunks_for_ref(build))
    assert "abc1234" in brief and "git revert" in brief
    assert f"gr{gid}" in brief and "moot: <sha>" in brief
    assert TEST_ID in brief  # the report follows the brief verbatim
    # idempotent: later confirms mint nothing more
    assert W.mint_reverts(store, "r42") == []
    assert W.mint_reverts(store, "r43") == []
    assert _revert_roots(store, gid) == [root]


def test_no_revert_when_the_fix_landed_or_was_ruled_wontfix(store: Store) -> None:
    done = _file(store, rn="r41")
    wont = _file(store, rn="r40", failures={"tests/test_o.py::t": ""})
    store.add_tag(
        done, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
    )
    store.add_tag(
        wont, Tag.closed("STATUS", "wontfix"), set_by="agent", replace_prefix=True
    )
    assert W.mint_reverts(store, "r45") == []
    assert _revert_roots(store, done) == [] and _revert_roots(store, wont) == []


@pytest.mark.parametrize("status", sorted(W.GRIPE_FIXED_STATUSES))
def test_every_fixed_status_spares_the_revert(store: Store, status: str) -> None:
    """verify / auto_verify / user_verify / in_review are fixes awaiting
    confirmation, not unfixed gripes."""
    gid = _file(store, rn="r41")
    _set_status(store, gid, status)
    assert W.mint_reverts(store, "r50") == []
    assert _revert_roots(store, gid) == []


@pytest.mark.parametrize("status", ["open", "triaged", "fixing", "needs_spec"])
def test_unfixed_statuses_still_get_the_revert(store: Store, status: str) -> None:
    gid = _file(store, rn="r41")
    _set_status(store, gid, status)
    assert [g for g, _r in W.mint_reverts(store, "r50")] == [gid]


def _chain_nodes(store: Store, root: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE t.namespace = 'OPEN' AND t.value = %s ORDER BY r.ref_id",
            (f"plan:{root}",),
        ).fetchall()
    return [int(r[0]) for r in rows]


def test_a_live_fix_chain_defers_the_revert(store: Store) -> None:
    """The gripe's own fix chain (what gripe_dispatch mints) has an open or
    doing node: the fix is in flight, so no revert yet. Once every node is
    closed and the gripe is still unfixed, the revert fires."""
    gid = _file(store, rn="r41")
    chain = T.mint_gripe_fix_direct(store, gripe_id=gid)
    assert W.mint_reverts(store, "r50") == []  # all nodes open
    nodes = _chain_nodes(store, chain.root_id)
    assert chain.root_id in nodes and len(nodes) >= 4
    for n in nodes:
        _set_status(store, n, "doing")
    assert W.mint_reverts(store, "r50") == []  # doing counts as live
    for n in nodes:
        _set_status(store, n, "done")
    [(g, _root)] = W.mint_reverts(store, "r50")
    assert g == gid
    # the revert chain itself never counts as the fix chain: still one
    assert W.mint_reverts(store, "r51") == []
    assert len(_revert_roots(store, gid)) == 1


def _age_chain(store: Store, root: int, hours: float) -> list[int]:
    """Backdate everything that counts as chain activity: the nodes'
    updated_at, every tag stamp on them, and the jobs under them."""
    nodes = _chain_nodes(store, root)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - make_interval(hours => %s) "
            "WHERE ref_id = ANY(%s) OR (kind = 'job' AND parent_id = ANY(%s))",
            (hours, nodes, nodes),
        )
        conn.execute(
            "UPDATE ref_tags SET created_at = now() - make_interval(hours => %s) "
            "WHERE ref_id = ANY(%s)",
            (hours, nodes),
        )
    return nodes


def test_a_stalled_fix_chain_no_longer_defers_the_revert(store: Store) -> None:
    """N4: an open/doing node only counts while the chain moved within
    24 h. Boundary: 23 h defers, 25 h does not."""
    gid = _file(store, rn="r41")
    chain = T.mint_gripe_fix_direct(store, gripe_id=gid)
    _age_chain(store, chain.root_id, 23)
    assert W.mint_reverts(store, "r50") == []  # still active a day-less ago
    _age_chain(store, chain.root_id, 25)
    [(g, _root)] = W.mint_reverts(store, "r50")
    assert g == gid


def test_any_recent_activity_on_the_chain_keeps_it_live(store: Store) -> None:
    a = _file(store, rn="r41")
    b = _file(store, rn="r42", failures={"tests/test_o.py::t": ""})
    c = _file(store, rn="r43", failures={"tests/test_p.py::t": ""})
    chains = {g: T.mint_gripe_fix_direct(store, gripe_id=g) for g in (a, b, c)}
    nodes = {g: _age_chain(store, ch.root_id, 72) for g, ch in chains.items()}
    with store.pool.connection() as conn:
        # a: one node touched an hour ago
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '1 hour' WHERE ref_id = %s",
            (nodes[a][1],),
        )
        # b: only a STATUS stamp is fresh (a status flip, no refs.updated_at bump)
        conn.execute(
            "UPDATE ref_tags SET created_at = now() WHERE ref_id = %s", (nodes[b][1],)
        )
    # c: a job under a node was touched recently
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="run_slice",
        meta={"job_type": "run_slice"},
        parent_id=nodes[c][1],
    )
    assert job.id
    reverted = {g for g, _r in W.mint_reverts(store, "r50")}
    assert reverted == set()  # all three still look live
    # age c's job too: now only c has gone stale
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '72 hours' WHERE ref_id = %s",
            (job.id,),
        )
    assert {g for g, _r in W.mint_reverts(store, "r50")} == {c}


def test_only_gripes_with_a_release_waiver_tag_get_reverts(store: Store) -> None:
    ordinary = insert_gripe(store, "an ordinary gripe")
    assert ordinary
    assert W.mint_reverts(store, "r50") == []


def test_the_land_job_inherits_the_builds_claim_band(
    store: Store, tmp_path: Path
) -> None:
    todo = store.insert_ref(kind="todo", slug=None, title="build", meta={})
    other = store.insert_ref(kind="todo", slug=None, title="plain build", meta={})
    banded = Ctx(store, {})
    banded.meta = {"params": {}, "plan_root": todo.id, "claim_band": "release"}
    plain = Ctx(store, {})
    plain.meta = {"params": {}, "plan_root": other.id}
    for ctx, tid, want in ((banded, todo.id, "release"), (plain, other.id, None)):
        job = rs._mint_land(ctx, tid, tmp_path, None)
        meta = _meta(store, "job", job)
        assert meta["job_type"] == "land_slice"
        assert meta.get("claim_band") == want


def test_release_confirm_mints_the_revert_and_reports_it(store: Store) -> None:
    gid = _file(store, rn="r41")
    ctx = Ctx(store, {})
    lines = rcf._waiver_reverts(ctx, "r42")
    assert len(lines) == 1 and f"gr{gid}" in lines[0]
    assert len(_revert_roots(store, gid)) == 1
    assert rcf._waiver_reverts(ctx, "r43") == []
