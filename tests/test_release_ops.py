"""Dark release pass (td543733): the ``release_cut`` / ``ci_verdict_poll`` /
``release_deploy`` job types, the cut's facts flowing through the plan root
(``precis.plans.release``), the approval guard on the deploy, and the plan
tick driving a ``release-pass`` plan. Subprocess and HTTP are faked."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis import settings as psettings
from precis.plans import gates
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers.job_types import ci_red_digest as crd
from precis.workers.job_types import ci_verdict_poll as cvp
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import plan_dispatch as pd
from precis.workers.job_types import release_cut as rc
from precis.workers.job_types import release_deploy as rd
from precis.workers.job_types import run_slice as rs

pytestmark = pytest.mark.db

SHA = "c0ffee" + "1" * 34
PREV = "9" * 12
RNG = f"{PREV}..{SHA[:12]}"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    psettings.invalidate()


class Ctx:
    """The DispatchContext surface the ops use."""

    def __init__(
        self, store: Store, params: dict[str, Any], *, defer: Any = None
    ) -> None:
        self.store = store
        self.ref_id = 4242
        self.meta: dict[str, Any] = {"params": params}
        self.chunks: list[tuple[str, str]] = []
        self.failures: list[str] = []
        self.status: str | None = None
        self.stamped: dict[str, Any] = {}
        self.defer = defer

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.stamped.update(kw)

    def record_failure(self, msg: str, **_kw: Any) -> None:
        self.failures.append(msg)

    def set_status(self, value: str) -> None:
        self.status = value


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=ref_id)
    assert ref is not None
    return dict(ref.meta or {})


def _status(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _comment(store: Store, todo_id: int, text: str) -> None:
    existing = list(store.chunks.list_chunks_for_ref(todo_id))
    ord_ = max((int(c.ord) for c in existing), default=0) + 1
    store.chunks.insert_chunks(
        todo_id,
        [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": "todo_comment"})],
    )


def _jobs_under(store: Store, todo_id: int) -> list[tuple[int, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND retired_at IS NULL ORDER BY ref_id",
            (todo_id,),
        ).fetchall()
    return [(int(r[0]), dict(r[1])) for r in rows]


def _succeed(store: Store, job_id: int) -> None:
    store.add_tag(
        job_id, Tag.closed("STATUS", "succeeded"), set_by="system", replace_prefix=True
    )


def _cut(
    store: Store, *, host: str | None = None, checkout: str | None = None
) -> T.PlanMint:
    mint = T.mint_release_pass(store)
    R.record_cut(
        store,
        mint.ids["cut"],
        "r7",
        SHA,
        RNG,
        repo="o/r",
        host=host,
        checkout=checkout,
    )
    return mint


# ── registry ──────────────────────────────────────────────────────


def test_registry_and_executors() -> None:
    for name, executor in (
        ("release_cut", "ssh_node"),
        ("ci_verdict_poll", "job_inproc"),
        ("release_deploy", "ssh_node"),
    ):
        spec = get_job_type(name)
        assert spec is not None, name
        assert spec.compatible_executors == frozenset({executor})
        assert name in known_job_types()
    for name in ("release_cut", "release_deploy"):
        spec = get_job_type(name)
        assert spec is not None and spec.submit is not None and spec.poll is not None
    poll = get_job_type("ci_verdict_poll")
    assert poll is not None and poll.dispatch is not None and poll.dedupe_pending


# ── the cut flows through the plan root ─────────────────────────────


def test_record_cut_stamps_root_gate_loop_and_ask(store: Store) -> None:
    mint = _cut(store)
    rel = _meta(store, mint.root_id)["release"]
    assert (rel["rN"], rel["sha"], rel["range"], rel["repo"]) == ("r7", SHA, RNG, "o/r")
    assert rel["cut_at"].endswith("Z")
    want = {"kind": "tests-green", "args": {"range": RNG}}
    assert _meta(store, mint.ids["gate"])["gate"] == want
    assert _meta(store, mint.ids["loop"])["loop"]["until"] == want
    ask = _meta(store, mint.ids["approval"])["ask"]
    assert ask["options"] == ["deploy r7", "hold"]
    assert ask["blocks"] == f"td{mint.ids['deploy']}"
    assert R.release_of(store, mint.ids["deploy"]) == rel
    # Closed until a verdict exists; a green row opens it.
    assert not gates.evaluate(store, want, todo_id=mint.ids["gate"]).open
    assert R.record_ci_verdict(store, SHA, RNG, "green", "55") == (
        f"ci verdict recorded: {SHA[:9]} green"
    )
    assert gates.evaluate(store, want, todo_id=mint.ids["gate"]).open


def test_pending_gate_stays_closed_before_the_cut(store: Store) -> None:
    mint = T.mint_release_pass(store)
    spec = _meta(store, mint.ids["gate"])["gate"]
    assert not gates.evaluate(store, spec, todo_id=mint.ids["gate"]).open
    assert R.release_of(store, mint.ids["deploy"]) is None


def test_store_verdict_rows_match_the_script_shape(store: Store) -> None:
    R.record_ci_verdict(store, SHA, RNG, "red", "1")
    R.record_ci_verdict(store, SHA, RNG, "green", "2")
    rows = gates.read_ci_verdicts(store)
    assert [(r["sha"], r["verdict"], r["run_id"]) for r in rows] == [
        (SHA[:12], "green", "2")
    ]
    assert R.record_ci_verdict(store, SHA, RNG, "maybe").endswith("not green|red")


def test_store_prod_head_merges_landed(store: Store) -> None:
    R.record_prod_head(store, "a" * 40, ["b" * 12])
    R.record_prod_head(store, "d" * 40, ["e" * 12])
    rec = gates.read_prod_head(store)
    assert rec is not None and rec["head"] == "d" * 40
    assert rec["landed"] == ["e" * 12, "a" * 12, "b" * 12]


# ── the approval guard ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("answer", "ok"),
    [
        (None, False),
        ("answer: hold", False),
        ("answer: deploy r6", False),
        ("answer: deploy r7", True),
        ("Answer: Deploy r7 — looks fine", True),
    ],
)
def test_deploy_needs_a_deploy_answer_for_the_cut(
    store: Store, answer: str | None, ok: bool
) -> None:
    mint = _cut(store)
    if answer is not None:
        _comment(store, mint.ids["approval"], answer)
    rn, why = R.approval(store, mint.ids["deploy"])
    assert (rn == "r7") is ok, why
    refusal = rd.validate_submit(store, params={"todo_id": mint.ids["deploy"]})
    assert (refusal is None) is ok, refusal


def test_deploy_refused_before_the_cut_and_with_the_factory_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = T.mint_release_pass(store)
    refusal = rd.validate_submit(store, params={"todo_id": mint.ids["deploy"]})
    assert refusal is not None and "no cut recorded" in refusal
    R.record_cut(store, mint.ids["cut"], "r7", SHA, RNG)
    _comment(store, mint.ids["approval"], "answer: deploy r7")
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    refusal = rd.validate_submit(store, params={"todo_id": mint.ids["deploy"]})
    assert refusal is not None and "factory is off" in refusal
    assert rc.validate_submit(store, params={}) is not None


def test_deploy_node_without_an_approval_ask_is_refused(store: Store) -> None:
    mint = _cut(store)
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM links WHERE src_ref_id = %s AND relation = 'blocked-by'",
            (mint.ids["deploy"],),
        )
    rn, why = R.approval(store, mint.ids["deploy"])
    assert rn is None and "no approval ask" in why


# ── ci_verdict_poll ─────────────────────────────────────────────────


def _run(rid: int, status: str, conclusion: str | None, **kw: Any) -> dict[str, Any]:
    return {
        "id": rid,
        "path": ".github/workflows/check.yml",
        "head_sha": SHA,
        "head_branch": "release/r7",
        "status": status,
        "conclusion": conclusion,
        **kw,
    }


def test_verdict_of_picks_the_newest_check_run_on_the_release_branch() -> None:
    runs = [
        _run(1, "completed", "failure"),
        _run(3, "completed", "success"),
        _run(9, "completed", "failure", head_branch="main"),
        _run(8, "completed", "failure", path=".github/workflows/other.yml"),
    ]
    assert cvp.verdict_of(runs, SHA, "release/r7")[:2] == ("green", "3")
    assert cvp.verdict_of(runs, SHA)[:2] == ("red", "9")
    assert cvp.verdict_of([_run(4, "in_progress", None)], SHA, "release/r7")[0] == (
        "pending"
    )
    assert cvp.verdict_of([_run(5, "completed", "cancelled")], SHA, "release/r7")[
        0
    ] == ("red")
    assert cvp.verdict_of([], SHA, "release/r7")[0] == "pending"


def _fake_github(
    monkeypatch: pytest.MonkeyPatch,
    runs: list[dict[str, Any]] | None,
    status: int = 200,
) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
    calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        calls.append((url, params, headers))
        if runs is None:
            raise OSError("network down")
        return SimpleNamespace(status_code=status, json=lambda: {"workflow_runs": runs})

    monkeypatch.setattr(cvp, "_http_get", get)
    return calls


def test_poll_records_green_from_the_plan_root(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store)
    monkeypatch.setenv("GH_TOKEN", "tok")
    calls = _fake_github(monkeypatch, [_run(31, "completed", "success")])
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures == []
    url, params, headers = calls[0]
    assert url == "https://api.github.com/repos/o/r/actions/runs"
    assert params["head_sha"] == SHA
    assert headers["Authorization"] == "Bearer tok"
    assert ctx.stamped["ci_verdict_poll"]["state"] == "green"
    row = gates.read_ci_verdicts(store)[-1]
    assert (row["sha"], row["range"], row["verdict"], row["run_id"]) == (
        SHA[:12],
        RNG,
        "green",
        "31",
    )
    spec = _meta(store, mint.ids["gate"])["gate"]
    assert gates.evaluate(store, spec, todo_id=mint.ids["gate"]).open


def test_poll_records_red(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    mint = _cut(store)
    _fake_github(monkeypatch, [_run(32, "completed", "failure")])
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    cvp._dispatch(ctx, cvp.SPEC)
    assert gates.read_ci_verdicts(store)[-1]["verdict"] == "red"
    spec = _meta(store, mint.ids["gate"])["gate"]
    verdict = gates.evaluate(store, spec, todo_id=mint.ids["gate"])
    assert not verdict.open and "CI red" in verdict.why


@pytest.mark.parametrize(
    "runs", [[_run(33, "in_progress", None)], [], None], ids=["running", "none", "net"]
)
def test_poll_parks_while_pending_and_records_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, runs: Any
) -> None:
    mint = _cut(store)
    _fake_github(monkeypatch, runs)
    parks: list[tuple[datetime, str]] = []

    def defer(at: datetime, note: str) -> bool:
        parks.append((at, note))
        return True

    ctx = Ctx(store, {"todo_id": mint.ids["poll"]}, defer=defer)
    before = datetime.now(UTC)
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures == []
    assert len(parks) == 1
    wait = parks[0][0] - before
    assert timedelta(minutes=9) < wait <= timedelta(minutes=10, seconds=5)
    assert "pending" in ctx.chunks[-1][1]
    assert gates.read_ci_verdicts(store) == []


def test_poll_fails_past_the_park_cap(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store)
    _fake_github(monkeypatch, [_run(34, "queued", None)])
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]}, defer=lambda at, note: False)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(ctx.failures) == 1 and "still pending" in ctx.failures[0]


def test_poll_fails_on_a_missing_repo_or_sha(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store)
    _fake_github(monkeypatch, [], status=404)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures and "404" in ctx.failures[0]
    uncut = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": uncut.ids["poll"]})
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures and "no candidate sha" in ctx.failures[0]


def test_poll_takes_explicit_params(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(
        monkeypatch, [_run(35, "completed", "success", head_branch="main")]
    )
    ctx = Ctx(store, {"sha": SHA, "range": "x..y", "repo": "a/b"})
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures == []
    assert calls[0][0] == "https://api.github.com/repos/a/b/actions/runs"
    assert gates.read_ci_verdicts(store)[-1]["range"] == "x..y"


# ── release_cut / release_deploy (detached) ─────────────────────────


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "release").write_text("#!/bin/sh\n", encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    seen: dict[str, Any] = {"argv": []}

    def launch(argv: list[str], *, cwd: Path, log_path: Path, exit_file: Path) -> int:
        seen["argv"].append(argv)
        seen["cwd"], seen["log"], seen["exit"] = cwd, log_path, exit_file
        return 999_999

    monkeypatch.setattr(rs, "slice_root", lambda: root)
    monkeypatch.setattr(rs, "_process_identity", lambda pid: None)
    monkeypatch.setattr(rs, "_process_alive", lambda pid: True)
    monkeypatch.setattr(rc, "_run_dir", lambda: runs)
    monkeypatch.setattr(rc, "_launch", launch)
    monkeypatch.setattr(rc, "origin_repo", lambda root: "o/r")
    seen["root"] = root
    return seen


def _exit(seen: dict[str, Any], code: int, out: str) -> None:
    Path(seen["log"]).write_text(out, encoding="utf-8")
    Path(seen["exit"]).write_text(f"{code}\n", encoding="utf-8")


def test_release_cut_runs_cut_only_and_stamps_the_plan(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert ctx.failures == []
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--cut-only"]
    ]
    assert handle["root"] == str(checkout["root"])
    assert rc.poll(ctx, handle) is False  # still running
    _exit(
        checkout,
        0,
        f"release/r7 cut at {SHA[:9]}\nRELEASE_CUT: r7 {SHA}\nRELEASE_RANGE: {RNG}\n",
    )
    assert rc.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert ctx.stamped["release_cut"] == {
        "rN": "r7",
        "sha": SHA,
        "range": RNG,
        "repo": "o/r",
        "host": "node-a",
        "checkout": str(checkout["root"]),
    }
    assert ctx.chunks[-1][1].startswith(f"release_cut: r7 at {SHA}")
    rel = _meta(store, mint.root_id)["release"]
    assert rel["rN"] == "r7"
    assert (rel["host"], rel["checkout"]) == ("node-a", str(checkout["root"]))
    assert _meta(store, mint.ids["gate"])["gate"]["args"]["range"] == RNG


@pytest.mark.parametrize(
    ("code", "out", "want"),
    [
        (0, "riding release release/r6 at abc: it ships def\n", "no RELEASE_CUT line"),
        (1, "scripts/release: cannot read origin's branches\n", "exit 1"),
    ],
)
def test_release_cut_fails_without_a_cut(
    store: Store, checkout: dict[str, Any], code: int, out: str, want: str
) -> None:
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    _exit(checkout, code, out)
    assert rc.poll(ctx, handle) is True
    assert ctx.status is None and want in ctx.failures[0]
    assert R.release_of(store, mint.ids["deploy"]) is None


def test_launch_script_runs_the_named_script(
    store: Store, checkout: dict[str, Any]
) -> None:
    """``launch_script`` runs ``<root>/scripts/<script>`` — the default
    stays ``release``; ``ci_red_digest`` and ``round`` reuse the same
    detached launcher on other scripts."""
    probe = checkout["root"] / "scripts" / "ci-failed"
    probe.write_text("#!/bin/sh\n", encoding="utf-8")
    ctx = Ctx(store, {})
    handle = rc.launch_script(
        ctx, "ci_red_digest", ["445", "--probe"], script="ci-failed"
    )
    assert ctx.failures == []
    assert checkout["argv"] == [[str(probe), "445", "--probe"]]
    assert handle is not None and handle["root"] == str(checkout["root"])

    ctx = Ctx(store, {})
    assert rc.launch_script(ctx, "ci_red_digest", [], script="missing") is None
    assert "no scripts/missing in" in ctx.failures[0]


def test_release_deploy_refuses_without_approval_and_launches_with_it(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _cut(store)
    ctx = Ctx(store, {"todo_id": mint.ids["deploy"]})
    assert rd.submit(ctx, None) is None
    assert checkout["argv"] == [] and "not approved" in ctx.failures[0]

    _comment(store, mint.ids["approval"], "answer: deploy r7")
    ctx = Ctx(store, {"todo_id": mint.ids["deploy"]})
    handle = rd.submit(ctx, None)
    assert ctx.failures == []
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--deploy-only", "r7"]
    ]

    def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert args[-1] == f"{PREV}..{SHA}"
        return subprocess.CompletedProcess(args, 0, stdout=f"{SHA[:12]}\n", stderr="")

    monkeypatch.setattr(rs, "_git", git)
    nxt = f"next: verify runtime, then scripts/round deploy --confirm-runtime {SHA}"
    _exit(checkout, 3, f"RELEASE_DEPLOYED: r7 {SHA} {PREV}\n{nxt}\n")
    assert rd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    rec = gates.read_prod_head(store)
    assert rec is not None and rec["head"] == SHA and rec["landed"] == [SHA[:12]]
    assert nxt in ctx.chunks[-1][1]


def test_release_deploy_fails_on_a_refused_deploy(
    store: Store, checkout: dict[str, Any]
) -> None:
    mint = _cut(store)
    _comment(store, mint.ids["approval"], "answer: deploy r7")
    ctx = Ctx(store, {"todo_id": mint.ids["deploy"]})
    handle = rd.submit(ctx, None)
    _exit(checkout, 1, "scripts/release: release/r7 is not a live recorded release\n")
    assert rd.poll(ctx, handle) is True
    assert ctx.status is None and "exit 1" in ctx.failures[0]
    assert gates.read_prod_head(store) is None


# ── the deploy is pinned to the cut's host and checkout ──────────────


def test_deploy_dispatch_needs_the_cut_host(store: Store) -> None:
    mint = _cut(store, host="node-a", checkout="/srv/repo")
    _comment(store, mint.ids["approval"], "answer: deploy r7")
    todo = mint.ids["deploy"]
    for params in ({"todo_id": todo}, {"todo_id": todo, "target_node": "node-b"}):
        refusal = rd.validate_submit(store, params=params)
        assert refusal is not None and "'node-a'" in refusal, refusal
        assert "target_node='node-a'" in refusal
    assert (
        rd.validate_submit(store, params={"todo_id": todo, "target_node": "node-a"})
        is None
    )


def test_deploy_run_refuses_another_host_or_checkout(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = str(checkout["root"])
    mint = _cut(store, host="node-a", checkout=root)
    _comment(store, mint.ids["approval"], "answer: deploy r7")
    params = {"todo_id": mint.ids["deploy"], "target_node": "node-a"}

    # Wrong node, then no node at all: refused with the recorded host named.
    for env in ("node-b", None):
        if env is None:
            monkeypatch.delenv("PRECIS_NODE", raising=False)
        else:
            monkeypatch.setenv("PRECIS_NODE", env)
        ctx = Ctx(store, dict(params))
        assert rd.submit(ctx, None) is None
        assert "'node-a'" in ctx.failures[0], ctx.failures
    assert checkout["argv"] == []

    # Right node, other checkout: refused naming the recorded checkout.
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    monkeypatch.setattr(rs, "slice_root", lambda: Path("/elsewhere/repo"))
    ctx = Ctx(store, dict(params))
    assert rd.submit(ctx, None) is None
    assert repr(root) in ctx.failures[0], ctx.failures
    assert checkout["argv"] == []

    # Same node, same checkout: launches.
    monkeypatch.setattr(rs, "slice_root", lambda: checkout["root"])
    ctx = Ctx(store, dict(params))
    assert rd.submit(ctx, None) is not None, ctx.failures
    assert len(checkout["argv"]) == 1


def test_pin_refusal_without_a_recorded_pin_allows_anything() -> None:
    assert R.pin_refusal(None, host=None) is None
    assert R.pin_refusal({"rN": "r7"}, host=None, checkout="/x") is None
    rel = {"rN": "r7", "host": "node-a", "checkout": "/srv/repo"}
    assert R.pin_refusal(rel, host="node-a", checkout="/srv/repo") is None
    assert R.pin_refusal(rel, host="node-a") is None  # dispatch: no checkout yet


# ── the plan tick drives the plan ───────────────────────────────────


def _only_job(store: Store, todo_id: int) -> tuple[int, dict[str, Any]]:
    jobs = _jobs_under(store, todo_id)
    assert len(jobs) == 1, jobs
    return jobs[0]


@pytest.mark.parametrize("host", [None, "node-a"])
def test_tick_walks_a_release_pass_to_the_deploy(
    store: Store, host: str | None
) -> None:
    mint = T.mint_release_pass(store)
    ids = mint.ids
    res = pd.run_pass(store)
    assert res.dispatched == 1
    job, meta = _only_job(store, ids["cut"])
    assert (meta["job_type"], meta["executor"]) == ("release_cut", "ssh_node")
    assert meta["params"] == {"todo_id": ids["cut"]}

    # The cut job: record the cut, succeed. The tick closes the cut and
    # dispatches the poll on job_inproc in the same pass.
    R.record_cut(
        store,
        ids["cut"],
        "r7",
        SHA,
        RNG,
        repo="o/r",
        host=host,
        checkout="/srv/repo" if host else None,
    )
    _succeed(store, job)
    pd.run_pass(store)
    assert _status(store, ids["cut"]) == "done"
    job, meta = _only_job(store, ids["poll"])
    assert (meta["job_type"], meta["executor"]) == ("ci_verdict_poll", "job_inproc")
    assert meta["params"] == {"todo_id": ids["poll"]}
    assert pd.run_pass(store).dispatched == 0  # one poll at a time

    # The poll records green and succeeds: poll closes, then gate opens, the
    # loop closes empty and the approval waits for an answer.
    R.record_ci_verdict(store, SHA, RNG, "green", "31")
    _succeed(store, job)
    pd.run_pass(store)
    res = pd.run_pass(store)
    assert _status(store, ids["gate"]) == "done"
    assert _status(store, ids["loop"]) == "done"
    assert _jobs_under(store, ids["loop"]) == []
    assert _status(store, ids["approval"]) == "open"
    assert _jobs_under(store, ids["deploy"]) == []

    # 'hold' opens the answer gate, but the deploy is never dispatched.
    _comment(store, ids["approval"], "answer: hold")
    res = pd.run_pass(store)
    assert _status(store, ids["approval"]) == "done"
    assert _jobs_under(store, ids["deploy"]) == []
    assert any("not approved" in n for n in res.notes), res.notes

    # 'deploy r7' dispatches release_deploy.
    _comment(store, ids["approval"], "answer: deploy r7")
    pd.run_pass(store)
    _job, meta = _only_job(store, ids["deploy"])
    assert (meta["job_type"], meta["executor"]) == ("release_deploy", "ssh_node")
    # The tick pins the deploy to the node the cut ran on.
    want = {"todo_id": ids["deploy"], **({"target_node": host} if host else {})}
    assert meta["params"] == want


# ── a red verdict re-cuts (td543855) ────────────────────────────────

SHA2 = "beef" + "2" * 36
RNG2 = f"{PREV}..{SHA2[:12]}"


def _close(store: Store, todo_id: int) -> None:
    store.add_tag(
        todo_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )


def _iteration(store: Store, loop_id: int) -> dict[str, int]:
    """The loop's children keyed by their release-fix role."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'todo' AND parent_id = %s "
            "AND retired_at IS NULL ORDER BY ref_id",
            (loop_id,),
        ).fetchall()
    keys = {
        "ci_red_digest": "digest",
        "run_slice": "fix",
        "release_cut": "recut",
        "ci_verdict_poll": "poll",
    }
    out: dict[str, int] = {}
    for ref_id, meta in rows:
        key = keys[meta["op"]]
        assert key not in out, (key, rows)
        out[key] = int(ref_id)
    return out


def test_red_verdict_recuts_and_a_green_recut_opens_the_approval(
    store: Store,
) -> None:
    mint = T.mint_release_pass(store)
    ids = mint.ids
    pd.run_pass(store)
    job, _ = _only_job(store, ids["cut"])
    R.record_cut(store, ids["cut"], "r7", SHA, RNG, repo="o/r")
    _succeed(store, job)
    pd.run_pass(store)
    job, _ = _only_job(store, ids["poll"])

    # Red on r7: the poll closes, the gate stays shut, the loop mints
    # iteration 1 = digest (ci_red_digest) -> fix -> recut -> poll.
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    _succeed(store, job)
    pd.run_pass(store)
    res = pd.run_pass(store)
    assert res.loops_minted == 1, res.notes
    assert _status(store, ids["poll"]) == "done"
    assert _status(store, ids["gate"]) == "open"
    assert _meta(store, ids["gate"])["gate_result"]["open"] is False
    assert _meta(store, ids["loop"])["loop"]["count"] == 1
    it = _iteration(store, ids["loop"])
    assert set(it) == {"digest", "fix", "recut", "poll"}

    # The digest job posts its brief and closes, then the fixer's session
    # runs and closes; the tick dispatches the mechanical re-cut.
    pd.run_pass(store)
    job, meta = _only_job(store, it["digest"])
    assert meta["job_type"] == "ci_red_digest"
    assert meta["params"] == {"todo_id": it["digest"]}
    assert _jobs_under(store, it["fix"]) == []  # blocked by the digest
    assert _jobs_under(store, it["recut"]) == []  # blocked by the fix
    _succeed(store, job)
    _close(store, it["digest"])
    pd.run_pass(store)
    for job_id, _m in _jobs_under(store, it["fix"]):
        _succeed(store, job_id)
    _close(store, it["fix"])
    pd.run_pass(store)
    job, meta = _only_job(store, it["recut"])
    assert (meta["job_type"], meta["executor"]) == ("release_cut", "ssh_node")
    assert meta["params"] == {"todo_id": it["recut"]}

    # The re-cut's job records r8 on the plan: meta.release overwritten,
    # gate / loop until / approval options moved to the new range.
    notes = R.record_cut(store, it["recut"], "r8", SHA2, RNG2, repo="o/r")
    assert notes[0].startswith(f"td{mint.root_id}: meta.release = r8")
    rel = _meta(store, mint.root_id)["release"]
    assert (rel["rN"], rel["sha"], rel["range"]) == ("r8", SHA2, RNG2)
    want = {"kind": "tests-green", "args": {"range": RNG2}}
    assert _meta(store, ids["gate"])["gate"] == want
    assert _meta(store, ids["gate"])["gate_result"] is None
    assert _meta(store, ids["loop"])["loop"]["until"] == want
    assert _meta(store, ids["loop"])["loop"]["count"] == 1
    assert _meta(store, ids["approval"])["ask"]["options"] == ["deploy r8", "hold"]
    _succeed(store, job)
    pd.run_pass(store)
    assert _status(store, it["recut"]) == "done"
    job, meta = _only_job(store, it["poll"])
    assert (meta["job_type"], meta["executor"]) == ("ci_verdict_poll", "job_inproc")
    assert meta["params"] == {"todo_id": it["poll"]}
    target = cvp.resolve_target(store, meta["params"])
    assert (target["sha"], target["range"], target["branch"]) == (
        SHA2,
        RNG2,
        "release/r8",
    )

    # Green on r8: the poll closes, the gate opens on the new range, the
    # loop closes without a second iteration, the approval asks for r8.
    R.record_ci_verdict(store, SHA2, RNG2, "green", "32")
    _succeed(store, job)
    pd.run_pass(store)
    res = pd.run_pass(store)
    assert res.loops_minted == 0, res.notes
    assert _status(store, ids["gate"]) == "done"
    assert _status(store, ids["loop"]) == "done"
    assert set(_iteration(store, ids["loop"])) == {"digest", "fix", "recut", "poll"}
    assert _status(store, ids["approval"]) == "open"
    assert "ask-user" in {str(t.value) for t in store.tags_for(ids["approval"])}
    assert "deploy r8" in _meta(store, ids["approval"])["ask"]["options"]
    assert _jobs_under(store, ids["deploy"]) == []

    _comment(store, ids["approval"], "answer: deploy r8")
    pd.run_pass(store)
    _job, meta = _only_job(store, ids["deploy"])
    assert meta["job_type"] == "release_deploy"


def test_recut_is_pinned_to_the_first_cut_host(store: Store) -> None:
    mint = _cut(store, host="node-a", checkout="/srv/repo")
    ids = mint.ids
    # The first cut has no meta.release yet: not pinned.
    fresh = T.mint_release_pass(store)
    assert rc.validate_submit(store, params={"todo_id": fresh.ids["cut"]}) is None

    _close(store, fresh.root_id)
    for key in ("cut", "poll"):
        _close(store, ids[key])
    T.mint_body(store, ids["loop"], "release-fix", iteration=2)
    it = _iteration(store, ids["loop"])
    for key in ("digest", "fix"):
        _close(store, it[key])
    pd.run_pass(store)
    _job, meta = _only_job(store, it["recut"])
    assert meta["job_type"] == "release_cut"
    assert meta["params"] == {"todo_id": it["recut"], "target_node": "node-a"}

    for params in (
        {"todo_id": it["recut"]},
        {"todo_id": it["recut"], "target_node": "node-b"},
    ):
        refusal = rc.validate_submit(store, params=params)
        assert refusal is not None and "target_node='node-a'" in refusal, refusal
    assert (
        rc.validate_submit(
            store, params={"todo_id": it["recut"], "target_node": "node-a"}
        )
        is None
    )


def test_recut_reopens_a_gate_an_earlier_cut_opened(store: Store) -> None:
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "green", "31")
    _close(store, mint.ids["gate"])
    R.record_cut(store, mint.ids["cut"], "r8", SHA2, RNG2)
    assert _status(store, mint.ids["gate"]) == "open"
    gate = _meta(store, mint.ids["gate"])
    assert gate["gate"]["args"]["range"] == RNG2 and gate["gate_result"] is None
    assert not gates.evaluate(store, gate["gate"], todo_id=mint.ids["gate"]).open
    # Recording the same cut again leaves a gate it opened alone.
    R.record_ci_verdict(store, SHA2, RNG2, "green", "32")
    _close(store, mint.ids["gate"])
    R.record_cut(store, mint.ids["cut"], "r8", SHA2, RNG2)
    assert _status(store, mint.ids["gate"]) == "done"


# ── ci_red_digest (td573431) ──────────────────────────────────────


def _digest_and_fix(store: Store, loop_id: int, root: int) -> tuple[int, int]:
    """A ``ci_red_digest`` task and a ``run_slice`` fix blocked-by it —
    the release-fix body's first two nodes, minted by hand."""
    with store.tx() as conn:
        digest = store.insert_ref(
            kind="todo",
            slug=None,
            title="Digest the red run",
            meta={"op": "ci_red_digest"},
            parent_id=loop_id,
            conn=conn,
        )
        fix = store.insert_ref(
            kind="todo",
            slug=None,
            title="Fix the failures",
            meta={"op": "run_slice"},
            parent_id=loop_id,
            conn=conn,
        )
        store.add_link(
            src_ref_id=fix.id,
            dst_ref_id=digest.id,
            relation="blocked-by",
            conn=conn,
        )
        for ref in (digest, fix):
            store.add_tag(ref.id, Tag.open(f"plan:{root}"), set_by="agent", conn=conn)
            store.add_tag(
                ref.id,
                Tag.closed("STATUS", "open"),
                set_by="agent",
                replace_prefix=True,
                conn=conn,
            )
    return int(digest.id), int(fix.id)


def _ci_failed_script(checkout: dict[str, Any]) -> Path:
    path = checkout["root"] / "scripts" / "ci-failed"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


def test_digest_posts_a_masked_brief_on_the_fix_node(
    store: Store, checkout: dict[str, Any]
) -> None:
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, fix_id = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    script = _ci_failed_script(checkout)
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    assert ctx.failures == []
    assert checkout["argv"] == [[str(script), "31"]]
    token = "ghp_" + "A1b2C3d4" * 4 + "E5f6"  # 36 alnum after ghp_
    _exit(checkout, 0, f"== test-linux ==\n  FAILED t.py::x - leaked {token}\n")
    assert crd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    comments = [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(fix_id)
        if c.chunk_kind == "todo_comment"
    ]
    assert len(comments) == 1
    text = comments[0]
    assert text.startswith(
        f"brief: release r7 is red at {SHA[:9]} (range {RNG}), check.yml run 31"
    )
    assert token not in text
    assert "<redacted:github token>" in text
    assert "never skip, xfail or loosen a test" in text
    assert text in ctx.chunks[-1][1]


def test_digest_fails_without_a_red_verdict_run_id(
    store: Store, checkout: dict[str, Any]
) -> None:
    mint = _cut(store)
    # A green on the cut's sha, a red on another sha, and a red with no
    # run id — none names a run to digest.
    R.record_ci_verdict(store, SHA2, RNG2, "red", "32")
    R.record_ci_verdict(store, SHA, RNG, "red", "")
    digest_id, _fix = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    ctx = Ctx(store, {"todo_id": digest_id})
    assert crd.submit(ctx, None) is None
    assert "no red verdict with a run id for r7" in ctx.failures[0]
    assert checkout["argv"] == []


def test_digest_failure_masks_secrets_in_the_log_tail(
    store: Store, checkout: dict[str, Any]
) -> None:
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, _fix = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    _ci_failed_script(checkout)
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    token = "ghp_" + "A1b2C3d4" * 4 + "E5f6"  # 36 alnum after ghp_
    _exit(checkout, 1, f"ci-failed: gh run view 31 --log-failed: {token}\n")
    assert crd.poll(ctx, handle) is True
    assert ctx.status is None
    assert len(ctx.failures) == 1
    assert "exit 1" in ctx.failures[0]
    assert token not in ctx.failures[0]
    assert "<redacted:github token>" in ctx.failures[0]


def test_digest_auth_refusal_names_the_token_probe_masked(
    store: Store, checkout: dict[str, Any]
) -> None:
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, _fix = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    _ci_failed_script(checkout)
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    token = "ghp_" + "A1b2C3d4" * 4 + "E5f6"  # 36 alnum after ghp_
    _exit(
        checkout,
        1,
        f"ci-failed: gh run view 31 --log-failed failed (1): gh: HTTP 403: "
        f"Resource not accessible by integration {token}\n",
    )
    assert crd.poll(ctx, handle) is True
    assert ctx.status is None
    assert len(ctx.failures) == 1
    assert "HTTP 403" in ctx.failures[0]
    assert "token probe" in ctx.failures[0]
    assert "params.probe=true" in ctx.failures[0]
    assert "params.run_id=31" in ctx.failures[0]
    assert token not in ctx.failures[0]


def test_digest_fails_with_no_fix_node_to_brief(
    store: Store, checkout: dict[str, Any]
) -> None:
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    with store.tx() as conn:
        digest = store.insert_ref(
            kind="todo",
            slug=None,
            title="Digest the red run",
            meta={"op": "ci_red_digest"},
            parent_id=mint.ids["loop"],
            conn=conn,
        )
        store.add_tag(
            digest.id,
            Tag.open(f"plan:{mint.root_id}"),
            set_by="agent",
            conn=conn,
        )
    _ci_failed_script(checkout)
    ctx = Ctx(store, {"todo_id": int(digest.id)})
    handle = crd.submit(ctx, None)
    assert ctx.failures == []
    _exit(checkout, 0, "== lint ==\n  Found 1 error.\n")
    assert crd.poll(ctx, handle) is True
    assert ctx.status is None
    assert "no run_slice todo" in ctx.failures[0]


def test_digest_with_no_failing_test_closes_fix_and_the_plan_recuts(
    store: Store, checkout: dict[str, Any]
) -> None:
    """A cancelled-only / infra-killed red run leaves nothing to digest:
    the job succeeds, the fix node closes no-op without a session, and
    the loop goes on to recut + poll."""
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    for key in ("cut", "poll"):
        _close(store, mint.ids[key])
    T.mint_body(store, mint.ids["loop"], "release-fix", iteration=1)
    it = _iteration(store, mint.ids["loop"])
    _ci_failed_script(checkout)
    ctx = Ctx(store, {"todo_id": it["digest"]})
    handle = crd.submit(ctx, None)
    assert ctx.failures == []
    _exit(checkout, 0, "ci-failed: no failed-job log lines\n")
    assert crd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert "no failing test in the logs (cancelled or infra)" in ctx.chunks[-1][1]
    assert _status(store, it["fix"]) == "done"
    comments = [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(it["fix"])
        if c.chunk_kind == "todo_comment"
    ]
    assert len(comments) == 1
    assert comments[0].startswith("no-op:")
    assert "no failing test in the logs" in comments[0]

    # The loop goes on: no fix session is dispatched, the re-cut is.
    _close(store, it["digest"])
    res = pd.run_pass(store)
    assert _jobs_under(store, it["fix"]) == []
    _job, meta = _only_job(store, it["recut"])
    assert meta["job_type"] == "release_cut", res.notes


def test_digest_parks_and_recovers_a_still_running_run(
    store: Store, checkout: dict[str, Any]
) -> None:
    """A run still in progress prints the ``… lines yet`` marker: that is
    a deferral, never the no-op close — the handle parks on
    ``not_before``, nothing closes, and once the marker passes the same
    run is digested again. The finished run's marker then takes the
    usual no-op path."""
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, fix_id = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    script = _ci_failed_script(checkout)
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    assert ctx.failures == []
    _exit(
        checkout,
        0,
        "ci-failed: run 31 still in_progress; no failed-job log lines yet\n",
    )
    assert crd.poll(ctx, handle) is False  # parked, not done
    assert ctx.status is None and ctx.failures == []
    assert _status(store, fix_id) == "open"  # no no-op close
    assert "pid" not in handle and handle.get("not_before")
    assert handle["parks"] == 1
    assert "re-digest after" in ctx.chunks[-1][1]

    # Before not_before the job stays parked; at it ci-failed runs again
    # on the same run.
    assert crd.poll(ctx, handle) is False
    assert checkout["argv"] == [[str(script), "31"]]
    handle["not_before"] = "2000-01-01T00:00:00Z"
    assert crd.poll(ctx, handle) is False  # relaunched, run in flight
    assert checkout["argv"] == [[str(script), "31"], [str(script), "31"]]
    assert "not_before" not in handle and handle["run_id"] == "31"
    assert handle["parks"] == 1

    # The completed run's marker then takes the usual no-op close.
    _exit(checkout, 0, "ci-failed: no failed-job log lines\n")
    assert crd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert _status(store, fix_id) == "done"


def test_digest_still_running_past_the_park_cap_fails(
    store: Store, checkout: dict[str, Any]
) -> None:
    """Past ``_PARK_CAP`` waits a wedged in-progress run fails the job;
    the plan tick re-dispatches within the node's retries."""
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, fix_id = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    _ci_failed_script(checkout)
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    handle["parks"] = crd._PARK_CAP
    _exit(
        checkout,
        0,
        "ci-failed: run 31 still in_progress; no failed-job log lines yet\n",
    )
    assert crd.poll(ctx, handle) is True
    assert ctx.status is None
    assert len(ctx.failures) == 1
    assert "still in progress" in ctx.failures[0]
    assert _status(store, fix_id) == "open"


def test_digest_auth_check_scopes_to_the_scripts_own_lines(
    store: Store, checkout: dict[str, Any]
) -> None:
    """``_AUTH_RE`` reads the script's ``ci-failed:`` stderr lines only:
    'HTTP 403' inside digested log content (a partial digest after a
    metadata-read exit 1) never names the token probe."""
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, fix_id = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    _ci_failed_script(checkout)
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    _exit(
        checkout,
        1,
        "ci-failed: gh run view 31 --json unreadable: exit 1: boom\n"
        "== test-linux ==\n"
        "  FAILED t.py::x - fixture answered HTTP 403\n",
    )
    assert crd.poll(ctx, handle) is True
    assert len(ctx.failures) == 1
    assert "exit 1" in ctx.failures[0]
    assert "token probe" not in ctx.failures[0]
    assert _status(store, fix_id) == "open"


def test_probe_reports_the_permission_line(
    store: Store, checkout: dict[str, Any]
) -> None:
    script = _ci_failed_script(checkout)
    ctx = Ctx(store, {"probe": True, "run_id": "445"})
    handle = crd.submit(ctx, None)
    assert ctx.failures == []
    assert checkout["argv"] == [[str(script), "445", "--probe"]]
    _exit(
        checkout,
        0,
        "probe: status=403 accepted-permissions=contents=read run=445\n",
    )
    assert crd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert ctx.stamped["ci_probe"] == {
        "status": "403",
        "accepted_permissions": "contents=read",
        "run": "445",
    }
    assert (
        "probe: status=403 accepted-permissions=contents=read run=445"
        in ctx.chunks[-1][1]
    )


def test_digest_dispatch_is_pinned_to_the_cut_host(store: Store) -> None:
    mint = _cut(store, host="node-a", checkout="/srv/repo")
    for key in ("cut", "poll"):
        _close(store, mint.ids[key])
    digest_id, fix_id = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    pd.run_pass(store)
    _job, meta = _only_job(store, digest_id)
    assert meta["job_type"] == "ci_red_digest"
    assert meta["executor"] == "ssh_node"
    assert meta["params"] == {"todo_id": digest_id, "target_node": "node-a"}
    assert _jobs_under(store, fix_id) == []  # still blocked by the digest
