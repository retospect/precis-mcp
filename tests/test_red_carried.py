"""``scripts/lib/red_carried.py`` — the remote gate's red-carried verdict
(gr739541).

A red ``scripts/ship --remote`` gate lands anyway when every failing id is
also red in the check run closest behind its base, none sits in a file the
diff changes, nothing but test shards failed and the rest of the suite has
a verdict on the gate's tree. Every other case — and every unreadable
piece of evidence — must stay a refusal, so most of this file is refusals.
GitHub is an in-memory world; git is one too, except in the tests at the
end that drive the real commands in a scratch repo.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "lib" / "red_carried.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("red_carried", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MOD = _load()
Refuse = _MOD.Refuse

GATE = 900
BASE = "b" * 40
HEAD = "h" * 40
STARTED = "2026-11-20T11:44:07Z"
UPLOADED = "2026-11-20T12:00:00Z"

A_X = ("tests.test_a", "test_x")
A_Y = ("tests.test_a.TestThing", "test_y[p 1]")
B_Z = ("tests.test_b", "test_z")


def _junit(failed: list[tuple[str, str]], message: str = "assert 1 == 2") -> str:
    cases = "".join(
        f'<testcase classname="{c}" name="{n}">'
        f'<failure message="{message}">boom</failure></testcase>'
        for c, n in failed
    )
    cases += '<testcase classname="tests.test_ok" name="test_fine"/>'
    return f'<?xml version="1.0"?><testsuite name="pytest">{cases}</testsuite>'


def _job(name: str, conclusion: str | None = "success", status: str = "completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _soak_red(extra: tuple[dict, ...] = ()) -> list[dict]:
    """Gate-fast green, soak leg 6 red: the whole suite ran."""
    jobs = [_job("plan"), _job("lint"), _job("failed-first", "skipped")]
    jobs += [_job(f"gate-fast (3.13, {k})") for k in range(1, 11)]
    jobs += [
        _job(f"soak (3.13, {k})", "failure" if k == 6 else "success")
        for k in range(1, 11)
    ]
    return [*jobs, _job("verdict-soak", "failure"), *extra]


def _gate_fast_red(extra: tuple[dict, ...] = ()) -> list[dict]:
    """Gate-fast shard 3 red: check.yml skips the soak and its roll-up."""
    jobs = [_job("plan"), _job("lint"), _job("failed-first", "skipped")]
    jobs += [
        _job(f"gate-fast (3.13, {k})", "failure" if k == 3 else "success")
        for k in range(1, 11)
    ]
    return [*jobs, _job("verdict-soak", "skipped"), *extra]


class World:
    """Runs, their jobs and junit artifacts, the commit graph and the diff.

    ``arts[run]`` is one ``{name: xml}`` (or ``{name: (xml, created_at)}``)
    per poll of that run; ``views[run]`` is one run object per poll."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
        self.root = root
        self.views: dict[int, list[dict[str, Any]]] = {}
        self.arts: dict[int, list[dict[str, Any]]] = {}
        self.listed: list[dict[str, Any]] = []
        self.behind: dict[str, int] = {}
        self.diff: set[str] = {"scripts/ship"}
        self.sleeps = 0
        self.now = 1000.0
        self.art_reads: list[int] = []
        self.unreadable: set[str] = set()
        self._polls: dict[int, int] = {}
        #: artifact id -> (run, name, xml); one junit keeps its id across
        #: polls, a replaced one (other xml) gets a new id.
        self._blobs: dict[int, tuple[int, str, str]] = {}
        monkeypatch.setattr(_MOD, "run_view", self._view)
        monkeypatch.setattr(_MOD, "recent_runs", lambda: list(self.listed))
        monkeypatch.setattr(_MOD, "distance", lambda sha, base: self.behind.get(sha))
        monkeypatch.setattr(_MOD, "changed_files", lambda base, head: set(self.diff))
        monkeypatch.setattr(_MOD, "artifacts", self._artifacts)
        monkeypatch.setattr(_MOD, "junit_xml", self._xml)
        clock = SimpleNamespace(sleep=self._sleep, time=lambda: self.now)
        monkeypatch.setattr(_MOD, "time", clock)
        for name in ("test_a", "test_b", "test_ok"):
            (root / "tests").mkdir(exist_ok=True)
            (root / "tests" / f"{name}.py").write_text("", encoding="utf-8")

    def _sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds

    def _step(self, run_id: int) -> int:
        """Which poll of ``run_id`` is current (a view starts a new one)."""
        return min(self._polls.get(run_id, 1), len(self.views[run_id])) - 1

    def _view(self, run_id: int | str) -> dict[str, Any]:
        rid = int(run_id)
        self._polls[rid] = self._polls.get(rid, 0) + 1
        return self.views[rid][self._step(rid)]

    def _artifacts(self, run_id: int | str) -> dict[str, dict[str, Any]]:
        rid = int(run_id)
        self.art_reads.append(rid)
        out: dict[str, dict[str, Any]] = {}
        for name, val in self.arts.get(rid, [{}])[self._step(rid)].items():
            xml, created = val if isinstance(val, tuple) else (val, UPLOADED)
            blob = (rid, name, xml)
            known = [k for k, v in self._blobs.items() if v == blob]
            art = known[0] if known else len(self._blobs) + 1
            self._blobs[art] = blob
            out[name] = {"id": art, "name": name, "created_at": created}
        return out

    def _xml(self, art: int) -> str | None:
        _run, name, xml = self._blobs[art]
        return None if name in self.unreadable else xml

    def gate(self, failed: list[tuple[str, str]], jobs: list[dict] | None = None):
        """The red gate run, soak leg 6 red with ``failed`` in its junit."""
        self.views[GATE] = [
            {
                "status": "completed",
                "conclusion": "failure",
                "startedAt": STARTED,
                "jobs": _soak_red() if jobs is None else jobs,
            }
        ]
        self.arts[GATE] = [{"junit-10-6": _junit(failed)}]

    def gate_fast(self, failed: list[tuple[str, str]]) -> None:
        """The red gate run, gate-fast shard 3 red, soak skipped."""
        self.gate(failed, jobs=_gate_fast_red())
        self.arts[GATE] = [{"junit-fast-3": _junit(failed)}]

    def ref(
        self,
        run_id: int,
        *,
        behind: int,
        failed: list[tuple[str, str]] | None = None,
        conclusion: str = "failure",
        branch: str = "release/r341",
        event: str = "push",
        jobs: list[dict] | None = None,
        created: str = "2026-11-20T11:39:10Z",
    ) -> None:
        """A finished check run ``behind`` commits behind the base."""
        sha = f"{run_id:040d}"
        self.listed.append(
            {
                "id": run_id,
                "head_sha": sha,
                "head_branch": branch,
                "event": event,
                "status": "completed",
                "conclusion": conclusion,
                "created_at": created,
            }
        )
        self.behind[sha] = behind
        if jobs is None:
            jobs = [_job("plan"), _job("gate-fast (3.13, 1)", conclusion)]
        self.views[run_id] = [
            {"status": "completed", "conclusion": conclusion, "jobs": jobs}
        ]
        self.arts[run_id] = [{"junit-fast-1": _junit(failed or [])}]

    def decide(self, **kw: Any):
        return _MOD.decide(str(GATE), HEAD, BASE, self.root, **kw)


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    return World(monkeypatch, tmp_path)


# ── the case that lands ──────────────────────────────────────────────────


def test_every_failing_id_red_on_the_closest_run_is_carried(world: World) -> None:
    world.gate([A_X, A_Y])
    world.ref(341, behind=4, failed=[A_X, A_Y, B_Z])
    ids, ref = world.decide()
    assert ids == ["tests/test_a.py::test_x", "tests/test_a.py::TestThing::test_y[p 1]"]
    assert ref["id"] == 341


def test_main_prints_the_trailer_value_and_exits_zero(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.gate([A_X])
    world.ref(341, behind=4, failed=[A_X])
    rc = _MOD.main(
        ["--run", str(GATE), "--head", HEAD, "--base", BASE, "--root", str(world.root)]
    )
    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "tests/test_a.py::test_x; base run 341\n"
    assert "red-carried: yes" in out.err


# ── refusals on the gate run's jobs ──────────────────────────────────────


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        (_job("lint-types", "failure"), "not a failed test shard"),
        (_job("failed-first", "failure"), "not a failed test shard"),
        (_job("test-docs", "failure"), "not a failed test shard"),
        (_job("test-linux (3.13, 2)", "failure"), "not a failed test shard"),
        (_job("test-other (macos-latest, 3.13, 1)", "failure"), "not a failed test"),
        (_job("soak (3.13, 11)", "cancelled"), "not a failed test shard"),
        (_job("gate-fast (3.13, 11)", "timed_out"), "not a failed test shard"),
        (_job("soak (3.13, 11)", None, "in_progress"), "never concluded"),
    ],
)
def test_a_red_that_is_not_a_settled_test_shard_refuses(
    world: World, extra: dict, why: str
) -> None:
    world.gate([A_X], jobs=_soak_red(extra=(extra,)))
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match=why):
        world.decide()


def test_a_red_soak_rollup_with_no_red_leg_refuses(world: World) -> None:
    jobs = [j for j in _gate_fast_red() if j["name"] != "verdict-soak"]
    world.gate([A_X], jobs=[*jobs, _job("verdict-soak", "failure")])
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match="no soak leg failed"):
        world.decide(soakless_ok=True)


def test_a_red_run_with_no_failed_shard_refuses(world: World) -> None:
    world.gate([A_X], jobs=[_job("plan"), _job("gate-fast (3.13, 1)")])
    with pytest.raises(Refuse, match="no test shard failed"):
        world.decide()


# ── the rest of the suite needs a verdict too ────────────────────────────


def test_a_red_gate_fast_on_a_selected_gate_refuses_by_default(world: World) -> None:
    """check.yml skips the soak after a red gate-fast, so the files outside
    gate-fast's selection never ran on the gate's tree."""
    world.gate_fast([A_X])
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match="the soak did not run"):
        world.decide()


def test_a_red_gate_fast_is_carried_when_the_switch_accepts_it(world: World) -> None:
    world.gate_fast([A_X])
    world.ref(341, behind=4, failed=[A_X])
    ids, _ = world.decide(soakless_ok=True)
    assert ids == ["tests/test_a.py::test_x"]


def test_a_red_gate_fast_is_carried_on_a_full_gate(world: World) -> None:
    """A diff on one of the selector's FULL triggers makes gate-fast the
    whole suite: nothing is left for a soak to cover."""
    world.gate_fast([A_X])
    world.diff = {"scripts/ship", ".github/workflows/check.yml"}
    world.ref(341, behind=4, failed=[A_X])
    ids, _ = world.decide()
    assert ids == ["tests/test_a.py::test_x"]


def test_full_gate_reads_the_selectors_own_triggers() -> None:
    assert _MOD.full_gate({"docs/x.md", "scripts/test"})
    assert _MOD.full_gate({"tests/conftest.py"})
    assert not _MOD.full_gate({"scripts/ship", "scripts/lib/ship-subject.sh"})
    assert not _MOD.full_gate(set())


def test_the_switch_is_read_from_the_environment(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world.gate_fast([A_X])
    world.ref(341, behind=4, failed=[A_X])
    argv = ["--run", str(GATE), "--head", HEAD, "--base", BASE]
    argv += ["--root", str(world.root)]
    assert _MOD.main(argv) == 1
    assert "the soak did not run" in capsys.readouterr().err
    monkeypatch.setenv("PRECIS_RED_CARRIED_SOAKLESS", "1")
    assert _MOD.main(argv) == 0


# ── refusals on the gate run's junit ─────────────────────────────────────


def test_a_red_shard_without_its_junit_refuses(world: World) -> None:
    """No junit = pytest did not end, so the shard's files have no verdict."""
    world.gate([A_X])
    world.arts[GATE] = [{"junit-10-7": _junit([A_X]), "junit-fast-6": _junit([A_X])}]
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match="soak shard 6 left no readable junit"):
        world.decide()


def test_a_junit_from_an_earlier_attempt_refuses(world: World) -> None:
    """A cancelled attempt can leave a junit behind; a rerun shard that
    then dies before its upload must not be read from it."""
    world.gate([A_X])
    world.arts[GATE] = [{"junit-10-6": (_junit([A_X]), "2026-11-20T11:00:00Z")}]
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match="from an earlier attempt"):
        world.decide()


def test_a_red_shard_whose_junit_names_no_failure_refuses(world: World) -> None:
    world.gate([])
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match="names no failed test"):
        world.decide()


def test_a_failure_no_file_can_be_found_for_refuses(world: World) -> None:
    """A collection error has no node id to compare."""
    world.gate([("", "tests.test_gone")])
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match="cannot place failed test"):
        world.decide()


def test_a_crashed_worker_refuses_even_when_the_test_is_carried(world: World) -> None:
    """--max-worker-restart=0 ends the session at the crash: the junit is
    whole and names one carried failure, but the tests after it never ran."""
    world.gate([A_X])
    crash = "worker &apos;gw3&apos; crashed while running &apos;tests/test_a.py::test_x&apos;"
    world.arts[GATE] = [{"junit-10-6": _junit([A_X], message=crash)}]
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match="crashed its worker"):
        world.decide()


def test_a_failing_id_in_a_file_the_diff_changes_refuses(world: World) -> None:
    world.gate([A_X, B_Z])
    world.diff = {"scripts/ship", "tests/test_b.py"}
    world.ref(341, behind=4, failed=[A_X, B_Z])
    with pytest.raises(
        Refuse, match=r"file this diff changes: tests/test_b.py::test_z"
    ):
        world.decide()


@pytest.mark.parametrize("path", sorted(_MOD._JUDGE))
def test_a_diff_that_changes_the_judge_refuses(world: World, path: str) -> None:
    world.gate([A_X])
    world.diff = {"scripts/ship", path}
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match="changes the judge"):
        world.decide()


def test_more_ids_than_the_cap_refuses(world: World) -> None:
    world.gate([A_X, A_Y, B_Z])
    world.ref(341, behind=4, failed=[A_X, A_Y, B_Z])
    with pytest.raises(Refuse, match="over the cap of 2"):
        world.decide(max_ids=2)


# ── refusals on the reference run ────────────────────────────────────────


def test_an_id_the_reference_run_does_not_carry_refuses(world: World) -> None:
    world.gate([A_X, B_Z])
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match=r"1 of 2 failing id.*tests/test_b.py::test_z"):
        world.decide()


def test_a_green_closest_run_refuses_and_its_junits_are_not_read(
    world: World,
) -> None:
    """A rerun can turn a run green while a failed attempt's junit stays
    listed; green means no carried red, whatever that junit says."""
    world.gate([A_X])
    world.ref(341, behind=4, failed=[A_X], conclusion="success")
    world.ref(340, behind=40, failed=[A_X])
    with pytest.raises(Refuse, match="which is green"):
        world.decide()
    assert 341 not in world.art_reads and 340 not in world.art_reads


def test_a_reference_junit_that_cannot_be_read_carries_nothing(world: World) -> None:
    world.gate([A_X])
    world.ref(341, behind=4, failed=[A_X])
    world.unreadable = {"junit-fast-1"}
    with pytest.raises(Refuse, match="are not red in run 341"):
        world.decide()


def test_a_reference_too_far_behind_the_base_refuses(world: World) -> None:
    """main may have fixed the id since, and the diff broken it again."""
    world.gate([A_X])
    world.ref(341, behind=51, failed=[A_X])
    with pytest.raises(Refuse, match="over 50 commits back"):
        world.decide()
    assert world.decide(max_behind=51)[1]["id"] == 341


def test_no_run_behind_the_base_refuses(world: World) -> None:
    world.gate([A_X])
    with pytest.raises(Refuse, match="no check run with a test verdict"):
        world.decide()


# ── which run is the reference ───────────────────────────────────────────


def test_the_closest_run_decides_not_the_newest_or_the_reddest(world: World) -> None:
    """An old run red on the id proves nothing once a closer one is green."""
    world.gate([A_X])
    world.ref(330, behind=40, failed=[A_X], created="2026-11-20T12:00:00Z")
    world.ref(341, behind=4, failed=[B_Z], created="2026-11-20T11:00:00Z")
    with pytest.raises(Refuse, match="run 341"):
        world.decide()


def test_candidates_skip_main_pushes_strangers_and_the_gate_itself(
    world: World,
) -> None:
    world.ref(341, behind=4)
    world.ref(500, behind=1, branch="main")  # a main push runs no shards
    world.ref(600, behind=2, branch="main", event="schedule")  # the nightly does
    world.ref(GATE, behind=0, branch="ci/x")
    world.listed.append({"id": 700, "head_sha": "f" * 40, "head_branch": "ci/y"})
    got = [r["id"] for r in _MOD.candidates(world.listed, BASE, str(GATE))]
    assert got == [600, 341]


def test_two_runs_on_one_commit_read_the_newer_first(world: World) -> None:
    world.ref(341, behind=4, created="2026-11-20T10:00:00Z")
    world.ref(342, behind=4, created="2026-11-20T11:00:00Z")
    got = [r["id"] for r in _MOD.candidates(world.listed, BASE, str(GATE))]
    assert got == [342, 341]


def test_a_cancelled_or_shardless_run_passes_the_verdict_to_the_next(
    world: World,
) -> None:
    world.gate([A_X])
    world.ref(343, behind=1, conclusion="cancelled")
    world.ref(
        342,
        behind=2,
        conclusion="success",
        jobs=[_job("plan"), _job("test-docs"), _job("gate-fast (3.13, 1)", "skipped")],
    )
    world.ref(341, behind=4, failed=[A_X])
    ids, ref = world.decide()
    assert ref["id"] == 341 and ids == ["tests/test_a.py::test_x"]


def test_a_closest_run_red_on_failed_first_refuses(world: World) -> None:
    """It skipped its shards, so it names no id — but it is the newest
    verdict, and the cut behind it may be red on ids it has since fixed."""
    world.gate([A_X])
    world.ref(
        342,
        behind=2,
        jobs=[
            _job("plan"),
            _job("failed-first", "failure"),
            _job("gate-fast (3.13, 1)", "skipped"),
        ],
    )
    world.ref(341, behind=4, failed=[A_X])
    with pytest.raises(Refuse, match="run 342 .* failed-first"):
        world.decide()


# ── a reference run still in flight ──────────────────────────────────────


def _in_flight(world: World, polls: list[dict[str, Any]], final: str | None) -> None:
    """Run 341, running through ``polls`` (each the junits listed by then),
    then finished with ``final`` when given."""
    world.ref(341, behind=4)
    running = {
        "status": "in_progress",
        "conclusion": None,
        "jobs": [_job("gate-fast (3.13, 1)", None, "in_progress")],
    }
    world.views[341] = [running] * len(polls)
    world.arts[341] = list(polls)
    if final is not None:
        world.views[341].append(
            {
                "status": "completed",
                "conclusion": final,
                "jobs": [_job("gate-fast (3.13, 1)", final)],
            }
        )
        world.arts[341].append(polls[-1])


def test_a_running_reference_is_read_as_its_shards_upload(world: World) -> None:
    world.gate([A_X])
    _in_flight(world, [{}, {"junit-fast-1": _junit([A_X])}], final=None)
    ids, ref = world.decide()
    assert ids == ["tests/test_a.py::test_x"] and ref["id"] == 341
    assert world.sleeps == 1


def test_a_running_reference_that_ends_without_the_id_refuses(world: World) -> None:
    world.gate([A_X])
    _in_flight(world, [{}, {"junit-fast-1": _junit([B_Z])}], final="failure")
    with pytest.raises(Refuse, match="are not red in run 341"):
        world.decide()


def test_a_junit_a_rerun_replaced_no_longer_counts(world: World) -> None:
    """Poll 1 lists shard 1 red on the id; by poll 2 a rerun has uploaded a
    newer junit of the same name without it."""
    world.gate([A_X, B_Z])
    first = {"junit-fast-1": _junit([A_X]), "junit-fast-2": _junit([])}
    second = {"junit-fast-1": _junit([]), "junit-fast-2": _junit([B_Z])}
    _in_flight(world, [first, second], final="failure")
    with pytest.raises(Refuse, match=r"1 of 2 failing id.*tests/test_a.py::test_x"):
        world.decide()


def test_a_reference_still_running_at_the_deadline_refuses(world: World) -> None:
    world.gate([A_X])
    _in_flight(world, [{}] * 50, final=None)
    with pytest.raises(Refuse, match="still running"):
        world.decide(wait_min=3)
    assert world.sleeps == 3


# ── unreadable evidence ──────────────────────────────────────────────────


def test_main_exits_one_and_prints_nothing_on_stdout_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(run_id: int | str) -> dict[str, Any]:
        raise Refuse("gh run view failed (1): HTTP 502")

    monkeypatch.setattr(_MOD, "run_view", boom)
    rc = _MOD.main(
        ["--run", "1", "--head", HEAD, "--base", BASE, "--root", str(tmp_path)]
    )
    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    assert "red-carried: no — gh run view failed" in out.err


def test_lenient_read_drops_what_strict_refuses(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("", encoding="utf-8")
    xml = _junit([A_X, ("tests.test_gone", "test_q")])
    assert _MOD.failed_ids(xml, tmp_path, strict=False) == ["tests/test_a.py::test_x"]
    with pytest.raises(Refuse):
        _MOD.failed_ids(xml, tmp_path, strict=True)
    assert _MOD.failed_ids("<not xml", tmp_path, strict=False) == []
    with pytest.raises(Refuse, match="junit unreadable"):
        _MOD.failed_ids("<not xml", tmp_path, strict=True)


def test_a_crash_is_still_a_red_in_a_reference_run(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("", encoding="utf-8")
    xml = _junit([A_X], message="worker gw0 crashed while running tests/test_a.py")
    assert _MOD.failed_ids(xml, tmp_path, strict=False) == ["tests/test_a.py::test_x"]


def test_artifacts_keeps_the_newest_live_one_of_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = [
        {"id": 3, "name": "junit-fast-1", "created_at": "2026-11-20T12:30:00Z"},
        {"id": 1, "name": "junit-fast-1", "created_at": "2026-11-20T12:00:00Z"},
        {"id": 2, "name": "junit-fast-2", "created_at": "2026-11-20T12:00:00Z"},
        {
            "id": 4,
            "name": "junit-fast-2",
            "created_at": "2026-11-20T12:40:00Z",
            "expired": True,
        },
    ]
    monkeypatch.setattr(_MOD, "_gh_json", lambda args, **kw: listing)
    got = _MOD.artifacts(7)
    assert {name: art["id"] for name, art in got.items()} == {
        "junit-fast-1": 3,
        "junit-fast-2": 2,
    }
    monkeypatch.setattr(_MOD, "_gh_json", lambda args, **kw: {"message": "nope"})
    with pytest.raises(Refuse, match="not a list"):
        _MOD.artifacts(7)


def test_gh_failures_and_bad_json_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(rc: int, out: str):
        return lambda *a, **kw: subprocess.CompletedProcess(a, rc, out, "HTTP 502")

    monkeypatch.setattr(_MOD.subprocess, "run", fake(1, ""))
    with pytest.raises(Refuse, match=r"failed \(1\): HTTP 502"):
        _MOD.run_view(7)
    monkeypatch.setattr(_MOD.subprocess, "run", fake(0, "not json"))
    with pytest.raises(Refuse, match="unreadable JSON"):
        _MOD.run_view(7)
    monkeypatch.setattr(_MOD.subprocess, "run", fake(0, json.dumps([1])))
    with pytest.raises(Refuse, match="not a workflow-run object"):
        _MOD.run_view(7)


# ── the real git commands ────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """main: c1 - c2 - c3, plus a side commit off c1 that main never got."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    shas: dict[str, str] = {}
    for name in ("c1", "c2", "c3"):
        (tmp_path / f"{name}.txt").write_text(name, encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", name)
        shas[name] = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "-b", "side", shas["c1"])
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_é.py").write_text("", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "side")
    shas["side"] = _git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    return shas


def test_distance_counts_only_ancestors_of_the_base(repo: dict[str, str]) -> None:
    assert _MOD.distance(repo["c1"], repo["c3"]) == 2
    assert _MOD.distance(repo["c3"], repo["c3"]) == 0
    assert _MOD.distance(repo["c3"], repo["c1"]) is None  # ahead of the base
    assert _MOD.distance(repo["side"], repo["c3"]) is None  # never landed
    assert _MOD.distance("0" * 40, repo["c3"]) is None  # not in this clone


def test_changed_files_names_the_diff_unquoted(repo: dict[str, str]) -> None:
    assert _MOD.changed_files(repo["c1"], repo["c3"]) == {"c2.txt", "c3.txt"}
    assert _MOD.changed_files(repo["c1"], repo["side"]) == {"tests/test_é.py"}
    with pytest.raises(Refuse, match="git diff"):
        _MOD.changed_files("0" * 40, repo["c3"])
