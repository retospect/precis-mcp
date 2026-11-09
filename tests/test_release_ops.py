"""Dark release pass (td543733): the ``release_cut`` / ``ci_verdict_poll`` /
``release_deploy`` / ``release_confirm`` job types, the cut's facts flowing
through the plan root (``precis.plans.release``), the approval guard on the
deploy, and the plan tick driving a ``release-pass`` plan. Subprocess and
HTTP are faked."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis import settings as psettings
from precis.plans import gates
from precis.plans import pr_class as P
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.workers.job_types import ci_red_digest as crd
from precis.workers.job_types import ci_verdict_poll as cvp
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import plan_dispatch as pd
from precis.workers.job_types import release_confirm as rcf
from precis.workers.job_types import release_cut as rc
from precis.workers.job_types import release_deploy as rd
from precis.workers.job_types import run_slice as rs

pytestmark = pytest.mark.db

SHA = "c0ffee" + "1" * 34
PREV = "9" * 12
RNG = f"{PREV}..{SHA[:12]}"


#: The fake ``origin/prod..origin/main`` range the engine-review gate
#: reads (gr597625): tests put ``release_poll._GitLand`` commits here.
_GIT_LANDS: list[Any] = []


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    monkeypatch.delenv("PRECIS_RELEASE_HEALTH_URL", raising=False)
    psettings.invalidate()
    _GIT_LANDS.clear()
    monkeypatch.setattr(rc._rp, "_git_lands", lambda: (list(_GIT_LANDS), "5" * 40))


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
        ("release_confirm", "ssh_node"),
        ("release_poll", "job_inproc"),
    ):
        spec = get_job_type(name)
        assert spec is not None, name
        assert spec.compatible_executors == frozenset({executor})
        assert name in known_job_types()
    for name in ("release_cut", "release_deploy", "release_confirm"):
        spec = get_job_type(name)
        assert spec is not None and spec.submit is not None and spec.poll is not None
    for name in ("ci_verdict_poll", "release_poll"):
        poll = get_job_type(name)
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


def _rerun_poll_ctx(store: Store, todo_id: int, before: int = 1) -> Ctx:
    """A poll ctx as the tick mints it for a ``validated:`` rerun
    (gr588028 F4): ``rerun_of`` + the pre-rerun attempt baseline."""
    ctx = Ctx(store, {"todo_id": todo_id})
    ctx.meta["rerun_of"] = "31"
    ctx.meta["rerun_attempt_before"] = before
    return ctx


def test_a_rerun_poll_reads_a_stale_attempt_as_pending(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr588028 F4: the run id survives a rerun — a completed red
    verdict AT the pre-rerun attempt is the OLD verdict: pending
    (re-park), never recorded, and the job meta shows what it saw."""
    mint = _cut(store)
    _fake_github(monkeypatch, [_run(31, "completed", "failure", run_attempt=1)])
    parks: list[tuple[datetime, str]] = []
    ctx = _rerun_poll_ctx(store, mint.ids["poll"], before=1)

    def _park(at: datetime, note: str) -> bool:
        parks.append((at, note))
        return True

    ctx.defer = _park
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures == []
    assert len(parks) == 1
    stamped = ctx.stamped["ci_verdict_poll"]
    assert stamped["state"] == "pending"
    assert stamped["run_attempt"] == 1
    assert "pre-rerun baseline" in ctx.chunks[-1][1]
    assert gates.read_ci_verdicts(store) == []


def test_a_rerun_poll_records_red_past_the_baseline(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A red verdict on a HIGHER attempt is the rerun's own — recorded
    (the tick then re-arms the digest)."""
    mint = _cut(store)
    _fake_github(monkeypatch, [_run(31, "completed", "failure", run_attempt=2)])
    ctx = _rerun_poll_ctx(store, mint.ids["poll"], before=1)
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures == []
    assert ctx.stamped["ci_verdict_poll"]["state"] == "red"
    assert ctx.stamped["ci_verdict_poll"]["run_attempt"] == 2
    assert gates.read_ci_verdicts(store)[-1]["verdict"] == "red"


def test_a_rerun_poll_records_green_past_the_baseline(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A green verdict on a higher attempt is fresh — recorded (the
    tick closes the fix node)."""
    mint = _cut(store)
    _fake_github(monkeypatch, [_run(31, "completed", "success", run_attempt=2)])
    ctx = _rerun_poll_ctx(store, mint.ids["poll"], before=1)
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures == []
    assert gates.read_ci_verdicts(store)[-1]["verdict"] == "green"


def test_an_ordinary_poll_ignores_the_attempt(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No baseline, no freshness check: a plain poll records the
    completed verdict whatever the run's attempt."""
    mint = _cut(store)
    _fake_github(monkeypatch, [_run(36, "completed", "failure", run_attempt=1)])
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures == []
    assert ctx.stamped["ci_verdict_poll"]["state"] == "red"
    assert "run_attempt" not in ctx.stamped["ci_verdict_poll"]
    assert gates.read_ci_verdicts(store)[-1]["verdict"] == "red"


def test_poll_refuses_a_sha_already_on_prod(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr582590: a sha prod already runs is a stale cut — the poll refuses
    to record a verdict that would arm a deploy of a shipped sha."""
    mint = _cut(store)
    R.record_prod_head(store, SHA, [])
    calls = _fake_github(monkeypatch, [_run(31, "completed", "success")])
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures and "already on prod" in ctx.failures[0]
    assert gates.read_ci_verdicts(store) == []
    assert calls == []  # refused before the GitHub read


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


# ── the no-run ladder (gr589270) ────────────────────────────────────


def _aged_cut(store: Store, minutes: float) -> T.PlanMint:
    """A minted pass whose cut was recorded ``minutes`` ago — the
    ladder's clock is ``meta.release.cut_at``."""
    mint = T.mint_release_pass(store)
    R.record_cut(
        store,
        mint.ids["cut"],
        "r7",
        SHA,
        RNG,
        repo="o/r",
        now=datetime.now(UTC) - timedelta(minutes=minutes),
    )
    return mint


def _poll_comments(store: Store, todo_id: int) -> list[str]:
    return [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == "todo_comment"
    ]


def _fake_gh(
    monkeypatch: pytest.MonkeyPatch, rc: int = 0, stderr: str = ""
) -> list[list[str]]:
    calls: list[list[str]] = []

    def gh(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, rc, stdout="", stderr=stderr)

    monkeypatch.setattr(cvp, "_gh", gh)
    return calls


def _parks(ctx: Ctx) -> list[tuple[datetime, str]]:
    parks: list[tuple[datetime, str]] = []

    def defer(at: datetime, note: str) -> bool:
        parks.append((at, note))
        return True

    ctx.defer = defer
    return parks


def _ruling_asks(store: Store, root: int) -> list[tuple[int, dict[str, Any]]]:
    """``(id, meta)`` of the ruling asks minted under the plan root."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'todo' "
            "AND parent_id = %s AND retired_at IS NULL "
            "AND meta -> 'ask' ->> 'kind' = 'ruling' ORDER BY ref_id",
            (root,),
        ).fetchall()
    return [(int(r[0]), dict(r[1])) for r in rows]


def test_no_run_at_10_min_pends_without_stall_writes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr589270: a push event still queueing at 10 min is ordinary
    pending — parked, no note, no dispatch, no marker."""
    mint = _aged_cut(store, 10)
    _fake_github(monkeypatch, [])
    gh = _fake_gh(monkeypatch)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    parks = _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures == [] and len(parks) == 1
    assert _poll_comments(store, mint.ids["poll"]) == []
    assert gh == []
    assert "ci_verdict_stall" not in _meta(store, mint.ids["poll"])


def test_no_run_at_21_min_posts_one_stall_note(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past 20 min the poll comments once on the poll node — a second
    tick adds none (the node marker dedupes) — and dispatches nothing."""
    mint = _aged_cut(store, 21)
    _fake_github(monkeypatch, [])
    gh = _fake_gh(monkeypatch)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    [note] = _poll_comments(store, mint.ids["poll"])
    assert f"no check.yml run on release/r7 for {SHA[:9]} after 20 min" in note
    assert gh == []
    assert _meta(store, mint.ids["poll"])["ci_verdict_stall"]["sha"] == SHA
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert _poll_comments(store, mint.ids["poll"]) == [note]


def test_no_run_at_41_min_dispatches_the_gate_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past 40 min: one ``gh workflow run check.yml --ref release/r7``
    through the fake seam — and the dispatch marker survives a second
    tick, so the gate never re-fires for the same sha."""
    mint = _aged_cut(store, 41)
    _fake_github(monkeypatch, [])
    gh = _fake_gh(monkeypatch)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert gh == [
        ["workflow", "run", "check.yml", "--ref", "release/r7", "--repo", "o/r"]
    ]
    comments = _poll_comments(store, mint.ids["poll"])
    assert any("after 20 min" in c for c in comments)
    assert any("workflow_dispatch" in c for c in comments)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(gh) == 1


def test_no_run_at_91_min_files_one_ask_on_the_plan_root(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past 90 min the note and the dispatch have fired, and one
    records-only ruling ask sits under the plan root — once."""
    mint = _aged_cut(store, 91)
    _fake_github(monkeypatch, [])
    _fake_gh(monkeypatch)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    asks = _ruling_asks(store, mint.root_id)
    assert len(asks) == 1
    ask_id, meta = asks[0]
    assert meta["ask"]["options"] == ["acknowledged"]
    assert meta["ask"]["blocks"] == "nothing"
    assert "no check.yml run on release/r7" in meta["ask"]["context"]
    tags = {str(t.value) for t in store.tags_for(ask_id)}
    assert {"ask-user", "showstopper", f"plan:{mint.root_id}"} <= tags
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(_ruling_asks(store, mint.root_id)) == 1


def test_a_run_appearing_takes_the_normal_path(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ladder only walks a confirmed absence: a run on the branch
    at any age is the ordinary verdict — no notes, no dispatch."""
    mint = _aged_cut(store, 45)
    _fake_github(monkeypatch, [_run(44, "completed", "success")])
    gh = _fake_gh(monkeypatch)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    cvp._dispatch(ctx, cvp.SPEC)
    assert ctx.failures == []
    assert gates.read_ci_verdicts(store)[-1]["verdict"] == "green"
    assert gh == []
    assert _poll_comments(store, mint.ids["poll"]) == []


def test_a_refused_dispatch_is_noted_not_looped(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 403 from ``gh workflow run`` (the token's Actions write scope
    is unverified, gr588028) is one comment plus the marker — the poll
    keeps watching, it does not loop the dispatch."""
    mint = _aged_cut(store, 45)
    _fake_github(monkeypatch, [])
    gh = _fake_gh(monkeypatch, rc=1, stderr="gh: Resource not accessible (HTTP 403)")
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(gh) == 1
    comments = _poll_comments(store, mint.ids["poll"])
    assert any("refused" in c and "403" in c for c in comments)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(gh) == 1


def test_an_unreadable_github_walks_no_rung(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``runs`` ``None`` is not a confirmed absence: past 40 min an
    unreadable GitHub is still plain pending — no note, no dispatch."""
    mint = _aged_cut(store, 45)
    _fake_github(monkeypatch, None)
    gh = _fake_gh(monkeypatch)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    parks = _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(parks) == 1
    assert gh == []
    assert _poll_comments(store, mint.ids["poll"]) == []


def test_a_comment_failure_after_the_dispatch_cannot_re_fire(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-shot claim is stamped BEFORE ``gh`` runs: a comment-write
    failure after a refused dispatch still leaves the marker, so the
    next tick does not re-dispatch (the repeat-dispatch bug)."""
    mint = _aged_cut(store, 45)
    _fake_github(monkeypatch, [])
    gh = _fake_gh(monkeypatch, rc=1, stderr="gh: Resource not accessible (HTTP 403)")
    real_comment = cvp._node_comment

    def flaky_comment(store_: Any, todo_id: int, text: str, **kw: Any) -> None:
        if "gh workflow run" in text:
            raise RuntimeError("comment write failed")
        real_comment(store_, todo_id, text, **kw)

    monkeypatch.setattr(cvp, "_node_comment", flaky_comment)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(gh) == 1
    assert "dispatched" in _meta(store, mint.ids["poll"])["ci_verdict_stall"]
    monkeypatch.setattr(cvp, "_node_comment", real_comment)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(gh) == 1
    # The lost detail comment is not retried — only the note remains.
    [note] = _poll_comments(store, mint.ids["poll"])
    assert "after 20 min" in note


def test_a_failed_note_stamp_writes_neither_note_nor_marker(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note and its marker commit in one transaction: a stamp
    failure rolls the comment back too, and the next tick posts the
    note exactly once."""
    mint = _aged_cut(store, 21)
    _fake_github(monkeypatch, [])
    _fake_gh(monkeypatch)
    real_stamp = store.stamp_ref_meta

    def flaky_stamp(ref_id: int, updates: dict[str, Any], **kw: Any) -> None:
        marker = updates.get(cvp.STALL_META_KEY, {})
        if isinstance(marker, dict) and "noted" in marker:
            raise RuntimeError("stamp failed")
        real_stamp(ref_id, updates, **kw)

    monkeypatch.setattr(store, "stamp_ref_meta", flaky_stamp)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert _poll_comments(store, mint.ids["poll"]) == []
    assert "ci_verdict_stall" not in _meta(store, mint.ids["poll"])
    monkeypatch.setattr(store, "stamp_ref_meta", real_stamp)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    [note] = _poll_comments(store, mint.ids["poll"])
    assert "after 20 min" in note


def test_an_unstampable_claim_never_invokes_gh(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim precedes the call: when the dispatch marker cannot
    persist, ``gh`` is not invoked at all — an unrecorded dispatch is
    the repeat-dispatch bug."""
    mint = _aged_cut(store, 45)
    _fake_github(monkeypatch, [])
    gh = _fake_gh(monkeypatch)
    real_stamp = store.stamp_ref_meta

    def flaky_stamp(ref_id: int, updates: dict[str, Any], **kw: Any) -> None:
        marker = updates.get(cvp.STALL_META_KEY, {})
        if isinstance(marker, dict) and "dispatched" in marker:
            raise RuntimeError("stamp failed")
        real_stamp(ref_id, updates, **kw)

    monkeypatch.setattr(store, "stamp_ref_meta", flaky_stamp)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert gh == []
    assert "dispatched" not in _meta(store, mint.ids["poll"])["ci_verdict_stall"]
    monkeypatch.setattr(store, "stamp_ref_meta", real_stamp)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(gh) == 1


def test_the_ask_and_its_marker_commit_together(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 90-min ask mints in the same transaction as the ``asked``
    marker: a stamp failure leaves no ask, and the next tick files
    exactly one."""
    mint = _aged_cut(store, 91)
    _fake_github(monkeypatch, [])
    gh = _fake_gh(monkeypatch)
    real_stamp = store.stamp_ref_meta

    def flaky_stamp(ref_id: int, updates: dict[str, Any], **kw: Any) -> None:
        marker = updates.get(cvp.STALL_META_KEY, {})
        if isinstance(marker, dict) and "asked" in marker:
            raise RuntimeError("stamp failed")
        real_stamp(ref_id, updates, **kw)

    monkeypatch.setattr(store, "stamp_ref_meta", flaky_stamp)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert _ruling_asks(store, mint.root_id) == []
    marker = _meta(store, mint.ids["poll"])["ci_verdict_stall"]
    assert "asked" not in marker and "dispatched" in marker
    monkeypatch.setattr(store, "stamp_ref_meta", real_stamp)
    ctx = Ctx(store, {"todo_id": mint.ids["poll"]})
    _parks(ctx)
    cvp._dispatch(ctx, cvp.SPEC)
    assert len(_ruling_asks(store, mint.root_id)) == 1
    assert len(gh) == 1


# ── release_cut / release_deploy (detached) ─────────────────────────


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "release").write_text("#!/bin/sh\n", encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    seen: dict[str, Any] = {"argv": [], "env": []}

    def launch(
        argv: list[str],
        *,
        cwd: Path,
        log_path: Path,
        exit_file: Path,
        extra_env: dict[str, str] | None = None,
    ) -> int:
        seen["argv"].append(argv)
        seen["env"].append(extra_env)
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


# ── the job types write the shared release.round row (gr575362) ────


def _cut_log(rn: str = "r7", sha: str = SHA) -> str:
    return (
        f"release/{rn} cut at {sha[:9]}\n"
        f"RELEASE_CUT: {rn} {sha}\n"
        f"RELEASE_RANGE: {PREV}..{sha[:12]}\n"
    )


def _seed_round(store: Store, n: int, state: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "n": n,
        "branch": f"release/r{n}",
        "base": SHA,
        "state": state,
        "owner": {"host": "node-b", "checkout": "/elsewhere"},
        "state_at": "2026-01-01T00:00:00Z",
    }
    row.update(over)
    R.record_round(store, row)
    return row


def _run_cut(
    store: Store, checkout: dict[str, Any], todo_id: int, out: str | None = None
) -> Ctx:
    ctx = Ctx(store, {"todo_id": todo_id})
    handle = rc.submit(ctx, None)
    assert handle is not None and ctx.failures == []
    _exit(checkout, 0, out or _cut_log())
    assert rc.poll(ctx, handle) is True
    return ctx


def test_release_cut_poll_writes_the_shared_round_row(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = T.mint_release_pass(store)
    ctx = _run_cut(store, checkout, mint.ids["cut"])
    assert ctx.status == "succeeded", ctx.failures
    row = R.read_round(store)
    rel = _meta(store, mint.root_id)["release"]
    assert row == {
        "n": 7,
        "branch": "release/r7",
        "base": SHA,
        "state": "cut",
        "owner": {"host": "node-a", "checkout": str(checkout["root"])},
        "pass": f"td{mint.root_id}",
        "state_at": rel["cut_at"],
        "cut_at": rel["cut_at"],
    }
    assert "wrote r7 cut" in ctx.chunks[-1][1]


def test_release_cut_poll_leaves_a_same_n_row_with_a_later_state(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The script's own publish already wrote r7: the job leaves it."""
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = T.mint_release_pass(store)
    seeded = _seed_round(store, 7, "deployed")
    ctx = _run_cut(store, checkout, mint.ids["cut"])
    assert ctx.status == "succeeded", ctx.failures
    assert R.read_round(store) == seeded
    assert "already on record" in ctx.chunks[-1][1]


def test_release_cut_poll_leaves_a_higher_n_row(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = T.mint_release_pass(store)
    seeded = _seed_round(store, 8, "cut")
    ctx = _run_cut(store, checkout, mint.ids["cut"])
    assert ctx.status == "succeeded", ctx.failures
    assert R.read_round(store) == seeded
    assert "the row is r8" in ctx.chunks[-1][1]


def test_release_cut_poll_recut_moves_a_lower_n_row(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-cut r<N+1> moves the row — the old round is over."""
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = T.mint_release_pass(store)
    _seed_round(store, 6, "confirmed")
    ctx = _run_cut(store, checkout, mint.ids["cut"])
    assert ctx.status == "succeeded", ctx.failures
    row = R.read_round(store)
    assert row is not None
    assert row["n"] == 7 and row["state"] == "cut"
    assert row["owner"]["host"] == "node-a"


def test_release_cut_poll_unknown_host_writes_no_row(
    store: Store, checkout: dict[str, Any]
) -> None:
    mint = T.mint_release_pass(store)
    ctx = _run_cut(store, checkout, mint.ids["cut"])
    assert ctx.status == "succeeded", ctx.failures
    assert R.read_round(store) is None
    assert "host is unknown" in ctx.chunks[-1][1]


def _deploy_handle(store: Store, mint: T.PlanMint) -> tuple[Ctx, dict[str, Any]]:
    _comment(store, mint.ids["approval"], "answer: deploy r7")
    ctx = Ctx(store, {"todo_id": mint.ids["deploy"], "target_node": "node-a"})
    handle = rd.submit(ctx, None)
    assert handle is not None, ctx.failures
    return ctx, handle


def _cut_round_row(store: Store, checkout: dict[str, Any], mint: T.PlanMint) -> None:
    """The row ``release_cut.poll`` writes for the ``_cut`` pass."""
    R.update_round(
        store,
        {
            "n": 7,
            "branch": "release/r7",
            "base": SHA,
            "state": "cut",
            "owner": {"host": "node-a", "checkout": str(checkout["root"])},
            "pass": f"td{mint.root_id}",
        },
        create=True,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize("code", [0, 3], ids=["exit0", "exit3"])
def test_release_deploy_poll_advances_the_row(
    store: Store,
    checkout: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = _cut(store, host="node-a", checkout=str(checkout["root"]))
    _cut_round_row(store, checkout, mint)
    ctx, handle = _deploy_handle(store, mint)
    _exit(checkout, code, f"RELEASE_DEPLOYED: r7 {SHA} {PREV}\n")
    assert rd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    row = R.read_round(store)
    assert row is not None
    assert row["state"] == "deployed"
    assert row["state_at"] > "2026-01-01T00:00:00Z"
    assert row["deploy"]["sha"] == SHA
    assert row["deploy"]["rollout_at"].endswith("Z")
    assert row["owner"] == {"host": "node-a", "checkout": str(checkout["root"])}


def test_release_deploy_poll_never_creates_a_row(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = _cut(store, host="node-a", checkout=str(checkout["root"]))
    ctx, handle = _deploy_handle(store, mint)
    _exit(checkout, 0, f"RELEASE_DEPLOYED: r7 {SHA} {PREV}\n")
    assert rd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert R.read_round(store) is None
    assert "no row on record" in ctx.chunks[-1][1]


def test_release_deploy_poll_never_lowers_a_terminal_row(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = _cut(store, host="node-a", checkout=str(checkout["root"]))
    seeded = _seed_round(store, 7, "confirmed")
    ctx, handle = _deploy_handle(store, mint)
    _exit(checkout, 0, f"RELEASE_DEPLOYED: r7 {SHA} {PREV}\n")
    assert rd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert R.read_round(store) == seeded
    assert "not a move forward" in ctx.chunks[-1][1]


def test_release_deploy_poll_omits_a_guessed_sha_without_a_deployed_line(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``RELEASE_DEPLOYED`` line: the row still advances to ``deployed``
    (the rollout did happen), but it guesses no ``deploy.sha`` — the plan's
    sha is not the deployed head — and writes no ``rollout_at`` that is
    really the poll time. ``pinned_deploy_sha`` then falls back to the
    prod head."""
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = _cut(store, host="node-a", checkout=str(checkout["root"]))
    _cut_round_row(store, checkout, mint)
    ctx, handle = _deploy_handle(store, mint)
    _exit(checkout, 3, "rollout already done\n")
    assert rd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    row = R.read_round(store)
    assert row is not None
    assert row["state"] == "deployed"
    assert row["state_at"] > "2026-01-01T00:00:00Z"
    dep = row.get("deploy") or {}
    assert "sha" not in dep and "rollout_at" not in dep


def test_release_deploy_poll_survives_a_release_of_db_error(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``release_of`` sits inside the round write's try: a database error
    there is a summary note, never a job failure after a rollout."""
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    mint = _cut(store, host="node-a", checkout=str(checkout["root"]))
    _cut_round_row(store, checkout, mint)
    ctx, handle = _deploy_handle(store, mint)
    _exit(checkout, 3, "rollout already done\n")

    def boom(store: Store, todo_id: int) -> None:
        raise OSError("db gone")

    monkeypatch.setattr(R, "release_of", boom)
    assert rd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert "release.round not advanced" in ctx.chunks[-1][1]


# ── release_cut supersedes the clone's stale local round (td575911) ──


def _local_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Any = (),
    *,
    open_: bool = True,
    write: bool = True,
) -> None:
    """The clone's own ``precis-round/round.json``, patched in through the
    ``rc._round_json_path`` seam: ``releases=None`` is no git answer (no
    local record), a ``str`` is written raw (an unparsable file),
    ``write=False`` returns the path but leaves the file missing."""
    if releases is None:
        monkeypatch.setattr(rc, "_round_json_path", lambda root: None)
        return
    path = tmp_path / "precis-round" / "round.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if write:
        text = (
            releases
            if isinstance(releases, str)
            else json.dumps({"n": 1, "open": open_, "releases": releases})
        )
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(rc, "_round_json_path", lambda root: path)


def _stale_record(n: int = 7, **over: Any) -> dict[str, Any]:
    """The local ``round.json`` record a dead deploy leaves behind —
    ``scripts/round supersede``'s preconditions."""
    rec: dict[str, Any] = {
        "branch": f"release/r{n}",
        "n": n,
        "base": SHA,
        "state": "deployed",
        "deployment": {
            "sha": SHA,
            "started_at": "2026-11-06T10:00:00Z",
            "rollout_at": "2026-11-06T10:05:00Z",
        },
    }
    rec.update(over)
    return rec


def _walk_superseded_cut(
    store: Store, checkout: dict[str, Any], mint: T.PlanMint
) -> Ctx:
    """The whole supersede -> cut walk: submit launches ``round
    supersede``, exit 0 swaps the handle onto the ``--cut-only`` launch,
    a cut log succeeds the job."""
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert handle is not None and handle.get("phase") == "supersede"
    _exit(checkout, 0, "r7 superseded (origin/prod abc is past def)\n")
    assert rc.poll(ctx, handle) is False
    _exit(checkout, 0, _cut_log())
    assert rc.poll(ctx, handle) is True
    return ctx


def test_stale_local_round_reads_the_one_supersedable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    assert rc._stale_local_round(tmp_path) == 7
    _local_round(tmp_path, monkeypatch, [_stale_record(), _stale_record(n=8)])
    assert rc._stale_local_round(tmp_path) is None  # two candidates: not "the one"
    _local_round(tmp_path, monkeypatch, "{not json")
    assert rc._stale_local_round(tmp_path) is None


def test_release_cut_supersedes_a_stale_local_round_when_prod_moved_past_it(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clone's record says r7 deployed, the shared row says r7
    confirmed by another clone — prod moved past it. The cut's first act
    is the record-only ``scripts/round supersede r7`` (the shared row
    untouched — ``PRECIS_GRIPE_NOTE=0`` skips the publish), then the cut."""
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    script = _round_script(checkout)
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    _seed_round(store, 7, "confirmed")
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert ctx.failures == []
    assert handle is not None and handle["phase"] == "supersede"
    assert checkout["argv"] == [[str(script), "supersede", "r7"]]
    assert checkout["env"] == [{"PRECIS_GRIPE_NOTE": "0"}]

    _exit(checkout, 0, "r7 superseded (origin/prod abc is past def)\n")
    assert rc.poll(ctx, handle) is False  # swapped onto the cut
    assert checkout["argv"][-1] == [
        str(checkout["root"] / "scripts" / "release"),
        "--cut-only",
    ]
    assert "PRECIS_GRIPE_NOTE" not in (checkout["env"][-1] or {})
    assert "superseded stale local r7" in ctx.chunks[-1][1]

    _exit(checkout, 0, _cut_log())
    assert rc.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert "superseded stale local r7, prod moved past it" in ctx.chunks[-1][1]
    assert ctx.stamped["stale_round_superseded"] == {"n": 7}


def test_release_cut_supersede_leaves_the_shared_row_unchanged(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    _round_script(checkout)
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    _seed_round(store, 7, "confirmed")
    before = R._round_raw(store)
    mint = T.mint_release_pass(store)
    ctx = _walk_superseded_cut(store, checkout, mint)
    assert ctx.status == "succeeded", ctx.failures
    assert R._round_raw(store) == before


def test_release_cut_supersedes_when_a_later_round_is_confirmed(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed LATER round also proves prod moved past r7 — a
    coordinator released r7 and r8 by hand. The supersede names the
    local record's number."""
    script = _round_script(checkout)
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    _seed_round(store, 8, "confirmed")
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert ctx.failures == []
    assert handle is not None and handle["phase"] == "supersede"
    assert checkout["argv"] == [[str(script), "supersede", "r7"]]


def test_release_cut_without_a_shared_row_launches_the_cut_as_today(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is not None
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--cut-only"]
    ]


def test_release_cut_with_a_smaller_shared_n_does_not_supersede(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    _seed_round(store, 6, "confirmed")
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is not None
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--cut-only"]
    ]


@pytest.mark.parametrize(
    "state", ["cut", "deploying", "deployed", "superseded", "abandoned"]
)
def test_release_cut_with_a_non_terminal_shared_state_does_not_supersede(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Only ``confirmed`` proves the release rolled out elsewhere; every
    other shared state — the open lifecycle, a rollout journal, the
    non-proof terminals — launches the cut as today."""
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    _seed_round(store, 7, state)
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is not None
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--cut-only"]
    ]


def test_release_cut_with_a_live_release_deploy_job_does_not_supersede(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live ``release_deploy`` job may still land its own ``deployed``
    write — the supersede waits. A terminal one does not block."""
    script = _round_script(checkout)
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    _seed_round(store, 7, "confirmed")
    mint = T.mint_release_pass(store)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="release_deploy job",
        meta={"job_type": "release_deploy"},
        parent_id=mint.ids["deploy"],
    )
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is not None
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--cut-only"]
    ]

    store.add_tag(
        int(job.id),
        Tag.closed("STATUS", "succeeded"),
        set_by="system",
        replace_prefix=True,
    )
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert handle is not None and handle["phase"] == "supersede"
    assert checkout["argv"][-1] == [str(script), "supersede", "r7"]


def test_release_cut_supersede_failure_fails_the_job_and_cuts_nothing(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _round_script(checkout)
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    _seed_round(store, 7, "confirmed")
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert handle is not None and handle["phase"] == "supersede"
    _exit(
        checkout,
        1,
        "scripts/round: supersede refused — origin/prod does not contain "
        "r7's pinned sha — a partial rollout\n",
    )
    assert rc.poll(ctx, handle) is True
    assert len(ctx.failures) == 1
    assert "no cut launched" in ctx.failures[0]
    assert "does not contain" in ctx.failures[0]  # the log tail
    assert len(checkout["argv"]) == 1  # only the supersede launch


def test_release_cut_supersede_launch_failure_cuts_nothing(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _round_script(checkout)
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    _seed_round(store, 7, "confirmed")
    mint = T.mint_release_pass(store)

    def boom(argv: list[str], **_kw: Any) -> int:
        checkout["argv"].append(argv)
        raise OSError("exec denied")

    monkeypatch.setattr(rc, "_launch", boom)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is None
    assert len(ctx.failures) == 1 and "launch failed" in ctx.failures[0]
    assert checkout["argv"] == [[str(script), "supersede", "r7"]]  # one attempt


def test_release_cut_factory_off_runs_nothing(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    _local_round(tmp_path, monkeypatch, [_stale_record()])
    _seed_round(store, 7, "confirmed")
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is None
    assert "factory is off" in ctx.failures[0]
    assert checkout["argv"] == []


@pytest.mark.parametrize(
    ("releases", "open_", "write"),
    [
        pytest.param(None, True, True, id="no-local-record"),
        pytest.param("{not json", True, True, id="unparsable"),
        pytest.param([], True, False, id="file-missing"),
        pytest.param([_stale_record()], False, True, id="closed-round"),
        pytest.param([_stale_record(state="confirmed")], True, True, id="confirmed"),
        pytest.param([_stale_record(state="superseded")], True, True, id="superseded"),
        pytest.param([_stale_record(state="abandoned")], True, True, id="abandoned"),
        pytest.param(
            [
                _stale_record(
                    deployment={
                        "sha": SHA,
                        "rollout_at": "2026-11-06T10:05:00Z",
                        "runtime_confirmed_at": "2026-11-06T11:00:00Z",
                    }
                )
            ],
            True,
            True,
            id="runtime-confirmed",
        ),
        pytest.param(
            [_stale_record(state="deploying", cut_pending=True)],
            True,
            True,
            id="cut-pending",
        ),
        pytest.param(
            [{"branch": "release/r7", "n": 7, "base": SHA, "state": "cut"}],
            True,
            True,
            id="open-cut-record",
        ),
    ],
)
def test_release_cut_leaves_a_local_record_that_is_not_stale(
    store: Store,
    checkout: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    releases: Any,
    open_: bool,
    write: bool,
) -> None:
    """Every non-stale local shape — no record, a missing or unparsable
    file, a closed round, a terminal or runtime-confirmed record, a
    pending cut, and the deferred hazard of an open ``cut`` record — is
    left alone: no supersede, the cut launches as today even with a
    shared confirmed r7 row."""
    _seed_round(store, 7, "confirmed")
    _local_round(tmp_path, monkeypatch, releases, open_=open_, write=write)
    mint = T.mint_release_pass(store)
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert handle is not None and handle.get("phase") is None
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--cut-only"]
    ]


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
    store: Store, monkeypatch: pytest.MonkeyPatch
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
    # runs and closes; the tick dispatches the mechanical re-cut. The
    # re-cut recomputes the newest fully-reviewed head (gr592271).
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
    _fake_gh_api(monkeypatch, main_sha=SHA2)
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


def test_recut_is_pinned_to_the_first_cut_host(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _fake_gh_api(monkeypatch, main_sha=SHA2)  # the re-cut recomputes the head
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
    # gr597599: the fix node's escape when main is already green.
    assert "finish with `moot: <sha>` and no commit" in text
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
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
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
    _fake_gh_api(monkeypatch, main_sha=SHA2)  # the re-cut recomputes the head
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


def _todo_comments(store: Store, todo_id: int) -> list[str]:
    return [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == "todo_comment"
    ]


def _gh_stub_responses(jobs: list[dict[str, Any]], logs: dict[str, str]) -> Any:
    """A ``crd._gh`` fake: the run's jobs list on the runs endpoint, the
    per-job log text on each ``actions/jobs/<id>/logs`` read."""

    def fake(args: list[str]) -> Any:
        endpoint = args[-1]
        if "/actions/runs/" in endpoint:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"jobs": jobs}), stderr=""
            )
        for jid, text in logs.items():
            if f"/actions/jobs/{jid}/logs" in endpoint:
                return SimpleNamespace(returncode=0, stdout=text, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake


def test_digest_blind_sections_are_rescued_through_the_job_log(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr587536 (coordinator addition): the shared stream carried only
    step headers, so the digest is blind; the job itself re-reads the
    failed jobs' logs and posts the FAILED lines it finds — never a
    header-only brief."""
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, fix_id = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    _ci_failed_script(checkout)
    monkeypatch.setattr(
        crd,
        "_gh",
        _gh_stub_responses(
            [{"name": "gate-fast (3.13, 1)", "id": 555, "conclusion": "failure"}],
            {
                "555": (
                    "\x1b[36m+ pytest -q\x1b[0m\n"
                    "FAILED tests/x.py::t - assert 1 == 2\n"
                    "= 1 failed, 9 passed in 3.2s =\n"
                )
            },
        ),
    )
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    assert ctx.failures == []
    _exit(
        checkout,
        0,
        "== gate-fast (3.13, 1) ==\n"
        "  (no signal lines — last 2 of 2 lines)\n"
        "  next: if the job log has FAILED lines, re-read it via gh api\n"
        "  Run pytest\n",
    )
    assert crd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    comments = _todo_comments(store, fix_id)
    assert len(comments) == 1
    text = comments[0]
    assert text.startswith("brief:")
    assert "jobs-logs read" in text
    assert "FAILED tests/x.py::t - assert 1 == 2" in text
    assert "1 failed, 9 passed in 3.2s" in text


def test_digest_still_blind_after_the_rescue_blocks_the_fix(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No FAILED lines anywhere — even in the job logs: the fix node is
    never dispatched on an empty brief. The plan and the fix node each
    get one ``blocked:`` note per run (a retried digest does not
    double-post) and the job fails so the tick retries, then files the
    ask."""
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, fix_id = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    _ci_failed_script(checkout)
    monkeypatch.setattr(
        crd,
        "_gh",
        _gh_stub_responses(
            [{"name": "gate-fast (3.13, 1)", "id": 555, "conclusion": "failure"}],
            {"555": "+ pytest -q\nenv:\n"},
        ),
    )
    blind = (
        "== gate-fast (3.13, 1) ==\n"
        "  (no signal lines — last 1 of 1 lines)\n"
        "  Run pytest\n"
    )
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    _exit(checkout, 0, blind)
    assert crd.poll(ctx, handle) is True
    assert ctx.status is None
    assert "digest is blind" in ctx.failures[0]
    assert _status(store, fix_id) == "open"  # not dispatched, not closed
    comments = _todo_comments(store, fix_id)
    assert len(comments) == 1
    assert comments[0].startswith("blocked:")
    assert "run 31" in comments[0]
    assert "empty brief" in comments[0]
    # The plan carries the same note — the blocked state is a plan-level
    # fact, not only a fix-node one.
    plan_blocked = [
        c for c in _todo_comments(store, mint.root_id) if c.startswith("blocked:")
    ]
    assert plan_blocked == [comments[0]]

    # A retried digest on the same run posts no second blocked: note.
    ctx2 = Ctx(store, {"todo_id": digest_id})
    handle2 = crd.submit(ctx2, None)
    _exit(checkout, 0, blind)
    assert crd.poll(ctx2, handle2) is True
    assert "digest is blind" in ctx2.failures[0]
    assert _todo_comments(store, fix_id) == comments
    assert [
        c for c in _todo_comments(store, mint.root_id) if c.startswith("blocked:")
    ] == plan_blocked


def test_digest_partly_blind_posts_the_signal_it_has(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One shard's FAILED lines plus another's step-header tail is not
    'a blind digest': the rescue still runs for the blind section and
    the brief posts what it has."""
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, fix_id = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    _ci_failed_script(checkout)
    monkeypatch.setattr(
        crd,
        "_gh",
        _gh_stub_responses(
            [{"name": "gate-fast (3.13, 2)", "id": 556, "conclusion": "failure"}],
            {"556": "+ pytest -q\n"},
        ),
    )
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    _exit(
        checkout,
        0,
        "== gate-fast (3.13, 1) ==\n"
        "  FAILED tests/a.py::t1 - boom\n"
        "== gate-fast (3.13, 2) ==\n"
        "  (no signal lines — last 1 of 1 lines)\n"
        "  Run pytest\n",
    )
    assert crd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    comments = _todo_comments(store, fix_id)
    assert len(comments) == 1
    assert comments[0].startswith("brief:")
    assert "FAILED tests/a.py::t1 - boom" in comments[0]


def test_digest_rescue_survives_a_blind_dump_past_the_cap(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rescued FAILED lines are the brief's only real signal: a
    blind dump past ``_DIGEST_CAP`` must not push them off the posted
    comment — the log tail loses the room, the rescue keeps it."""
    mint = _cut(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    digest_id, fix_id = _digest_and_fix(store, mint.ids["loop"], mint.root_id)
    _ci_failed_script(checkout)
    monkeypatch.setattr(
        crd,
        "_gh",
        _gh_stub_responses(
            [{"name": "gate-fast (3.13, 1)", "id": 555, "conclusion": "failure"}],
            {"555": "+ pytest -q\nFAILED tests/x.py::t - assert 1 == 2\n"},
        ),
    )
    ctx = Ctx(store, {"todo_id": digest_id})
    handle = crd.submit(ctx, None)
    assert ctx.failures == []
    _exit(
        checkout,
        0,
        "== gate-fast (3.13, 1) ==\n"
        "  (no signal lines — last 2 of 2 lines)\n"
        + "  step noise\n"
        * (crd._DIGEST_CAP // 11 + 50),
    )
    assert crd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    comments = _todo_comments(store, fix_id)
    assert len(comments) == 1
    text = comments[0]
    assert text.startswith("brief:")
    assert "jobs-logs read" in text
    assert "FAILED tests/x.py::t - assert 1 == 2" in text


# ── release_confirm (td573432) ────────────────────────────────────


def _round_script(checkout: dict[str, Any]) -> Path:
    path = checkout["root"] / "scripts" / "round"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


def _walk_to_confirm(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[T.PlanMint, int, dict[str, Any]]:
    """Walk a release pass to a succeeded deploy on ``node-a``; the tick
    then dispatches the confirm node. Returns ``(mint, confirm job id,
    the job's params)``."""
    mint = T.mint_release_pass(store)
    ids = mint.ids
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    pd.run_pass(store)
    job, _ = _only_job(store, ids["cut"])
    R.record_cut(
        store,
        ids["cut"],
        "r7",
        SHA,
        RNG,
        repo="o/r",
        host="node-a",
        checkout=str(checkout["root"]),
    )
    _cut_round_row(store, checkout, mint)  # the row release_cut.poll writes
    _succeed(store, job)
    pd.run_pass(store)
    job, _ = _only_job(store, ids["poll"])
    R.record_ci_verdict(store, SHA, RNG, "green", "31")
    _succeed(store, job)
    pd.run_pass(store)
    pd.run_pass(store)
    _comment(store, ids["approval"], "answer: deploy r7")
    pd.run_pass(store)
    job, meta = _only_job(store, ids["deploy"])
    assert meta["params"] == {"todo_id": ids["deploy"], "target_node": "node-a"}
    ctx = Ctx(store, dict(meta["params"]))
    handle = rd.submit(ctx, None)
    assert handle is not None, ctx.failures
    _exit(checkout, 3, f"RELEASE_DEPLOYED: r7 {SHA} {PREV}\n")
    assert rd.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    _succeed(store, job)
    pd.run_pass(store)
    pd.run_pass(store)
    job_id, meta = _only_job(store, ids["confirm"])
    assert (meta["job_type"], meta["executor"]) == ("release_confirm", "ssh_node")
    assert meta["params"] == {"todo_id": ids["confirm"], "target_node": "node-a"}
    return mint, job_id, dict(meta["params"])


def _round_row(n: int, state: str, deploy: dict[str, Any] | None = None) -> str:
    row: dict[str, Any] = {
        "n": n,
        "branch": f"release/r{n}",
        "base": "b" * 40,
        "state": state,
        "owner": {"peer": "main", "host": "node-a"},
    }
    if deploy:
        row["deploy"] = deploy
    return json.dumps(row)


def _fail_job(store: Store, job_id: int, reason: str) -> None:
    """What the executor does when the op fails the job: a job_event tail
    plus STATUS:failed — the retry-cap ask reads both."""
    store.chunks.insert_chunks(
        job_id, [ChunkInsert(ord=0, text=reason, meta={"chunk_kind": "job_event"})]
    )
    store.add_tag(
        job_id, Tag.closed("STATUS", "failed"), set_by="system", replace_prefix=True
    )


def _asks_under(store: Store, root_id: int) -> list[int]:
    """Ask todos under the plan root (the retry-cap / reland asks)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' AND parent_id = %s "
            "AND retired_at IS NULL",
            (root_id,),
        ).fetchall()
    out = []
    for (rid,) in rows:
        tags = {t.value for t in store.tags_for(int(rid))}
        if {"ask-user", "showstopper"} <= tags:
            out.append(int(rid))
    return out


def test_confirm_after_deploy_exit_3_confirms_and_closes(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _round_script(checkout)
    mint, _job, params = _walk_to_confirm(store, checkout, monkeypatch)
    store.record_heartbeat("node-a", meta={"code_sha": SHA})
    # The script already published its own confirm values; the job merges,
    # never overwrites.
    psettings.set_setting(
        "release.round",
        _round_row(
            7,
            "deployed",
            {
                "sha": SHA,
                "confirmed_at": "2026-11-06T11:00:00Z",
                "evidence": "the script's evidence",
            },
        ),
        store=store,
    )
    ctx = Ctx(store, params)
    handle = rcf.submit(ctx, None)
    assert handle is not None, ctx.failures
    text = handle["evidence"]
    assert "web: not attested (release.health_url unset)" in text
    assert checkout["argv"][-1] == [
        str(script),
        "deploy",
        "--confirm-runtime",
        SHA,
        "--runtime-evidence",
        text,
    ]
    _exit(checkout, 0, "confirmed\n")
    assert rcf.poll(ctx, handle) is False  # swapped onto round close
    assert handle["phase"] == "close"
    assert checkout["argv"][-1] == [str(script), "close"]
    _exit(checkout, 0, "closed\n")
    assert rcf.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    rel = _meta(store, mint.root_id)["release"]
    assert rel["confirmed"]["sha"] == SHA
    row = R.read_round(store)
    assert row is not None
    assert row["deploy"]["confirmed_at"] == "2026-11-06T11:00:00Z"
    assert row["deploy"]["evidence"] == "the script's evidence"


def test_probe_mismatch_files_an_ask_and_confirms_nothing(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _round_script(checkout)
    mint, job_id, params = _walk_to_confirm(store, checkout, monkeypatch)
    store.record_heartbeat("node-a", meta={"code_sha": SHA})
    store.record_heartbeat("node-b", meta={"code_sha": SHA2})
    psettings.set_setting(
        "release.round", _round_row(7, "deployed", {"sha": SHA}), store=store
    )
    launches = list(checkout["argv"])
    ctx = Ctx(store, params)
    assert rcf.submit(ctx, None) is None
    reason = ctx.failures[0]
    assert "probe failed" in reason
    assert "node-b" in reason and SHA2[:12] in reason
    assert checkout["argv"] == launches  # nothing launched

    _fail_job(store, job_id, reason)
    res = pd.run_pass(store)
    assert res.asks == 1
    asks = _asks_under(store, mint.root_id)
    assert len(asks) == 1
    meta = _meta(store, asks[0])
    assert "node-b" in meta["ask"]["context"]
    assert SHA2[:12] in meta["ask"]["context"]
    assert "probe failed" in meta["ask"]["context"]
    with store.pool.connection() as conn:
        link = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = 'blocked-by'",
            (mint.ids["confirm"], asks[0]),
        ).fetchone()
    assert link is not None
    assert "confirmed" not in _meta(store, mint.root_id)["release"]
    assert R.read_round(store) == json.loads(_round_row(7, "deployed", {"sha": SHA}))


def test_web_sha_mismatch_fails_the_confirm(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _round_script(checkout)
    _mint, _job, params = _walk_to_confirm(store, checkout, monkeypatch)
    store.record_heartbeat("node-a", meta={"code_sha": SHA})
    url = "https://health.example.invalid/healthz"
    psettings.set_setting(rcf.HEALTH_URL_KEY, url, store=store)
    monkeypatch.setattr(
        rcf,
        "_http_get",
        lambda _url: SimpleNamespace(
            status_code=200, headers={rcf.BOOT_SHA_HEADER: SHA2}
        ),
    )
    launches = list(checkout["argv"])
    ctx = Ctx(store, params)
    assert rcf.submit(ctx, None) is None
    reason = ctx.failures[0]
    assert "web reports" in reason and SHA2[:12] in reason
    assert url not in reason and "health.example.invalid" not in reason
    assert checkout["argv"] == launches


def test_confirm_close_failure_still_succeeds(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _round_script(checkout)
    _mint, _job, params = _walk_to_confirm(store, checkout, monkeypatch)
    store.record_heartbeat("node-a", meta={"code_sha": SHA})
    ctx = Ctx(store, params)
    handle = rcf.submit(ctx, None)
    assert handle is not None, ctx.failures
    _exit(checkout, 0, "confirmed\n")
    assert rcf.poll(ctx, handle) is False
    assert checkout["argv"][-1] == [str(script), "close"]
    _exit(checkout, 1, "scripts/round: close failed\n")
    assert rcf.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert ctx.stamped["release_confirm"] == {"rN": "r7", "sha": SHA}
    summary = ctx.chunks[-1][1]
    assert "scripts/round close exit 1" in summary
    assert "deployed/r7" in summary


def test_confirm_close_launch_failure_still_succeeds(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close that never launches still succeeds the job: the release is
    already confirmed and ``scripts/release`` retries the close."""
    script = _round_script(checkout)
    _mint, _job, params = _walk_to_confirm(store, checkout, monkeypatch)
    store.record_heartbeat("node-a", meta={"code_sha": SHA})
    ctx = Ctx(store, params)
    handle = rcf.submit(ctx, None)
    assert handle is not None, ctx.failures
    _exit(checkout, 0, "confirmed\n")
    script.unlink()  # scripts/round gone: the close cannot launch
    assert rcf.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert ctx.failures == []
    assert ctx.stamped["release_confirm"] == {"rN": "r7", "sha": SHA}
    summary = ctx.chunks[-1][1]
    assert "scripts/round close failed to launch" in summary
    assert "deployed/r7" in summary


def test_release_confirm_advances_the_row_to_confirmed(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk's own deploy write left the row ``deployed``; the
    confirm moves it to ``confirmed`` with a fresh ``state_at``."""
    _round_script(checkout)
    _mint, _job, params = _walk_to_confirm(store, checkout, monkeypatch)
    store.record_heartbeat("node-a", meta={"code_sha": SHA})
    row = R.read_round(store)
    assert row is not None and row["state"] == "deployed"
    assert row["deploy"]["sha"] == SHA and row["deploy"]["rollout_at"]
    row["state_at"] = "2026-01-01T00:00:00Z"  # date it so the new stamp shows
    R.record_round(store, row)
    ctx = Ctx(store, params)
    handle = rcf.submit(ctx, None)
    assert handle is not None, ctx.failures
    _exit(checkout, 0, "confirmed\n")
    assert rcf.poll(ctx, handle) is False
    _exit(checkout, 0, "closed\n")
    assert rcf.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    row = R.read_round(store)
    assert row is not None and row["state"] == "confirmed"
    assert row["state_at"] > "2026-01-01T00:00:00Z"
    assert row["deploy"]["confirmed_at"].endswith("Z")


def test_record_confirm_takes_the_sha_from_the_prod_head(
    store: Store,
) -> None:
    """gr575362 review round 3: a fallback deploy (no ``RELEASE_DEPLOYED``
    line) left the row ``deployed`` with no ``deploy.sha``, and the confirm
    skipped it forever. When the prod head names the confirmed sha the row
    takes it as the deployed sha and moves to ``confirmed``; a prod head on
    another sha keeps the skip."""
    mint = _cut(store)
    _seed_round(store, 7, "deployed", deploy={"rollout_at": "2026-01-01T00:30:00Z"})
    R.record_prod_head(store, SHA, [])
    notes = R.record_confirm(store, mint.ids["deploy"], SHA, "evidence")
    assert any("confirmed" in n for n in notes)
    row = R.read_round(store)
    assert row is not None and row["state"] == "confirmed"
    assert row["deploy"]["sha"] == SHA
    assert row["deploy"]["rollout_at"] == "2026-01-01T00:30:00Z"

    # The prod head on another sha is not deploy evidence for this one.
    _seed_round(store, 7, "deployed", deploy={"rollout_at": "2026-01-01T00:30:00Z"})
    R.record_prod_head(store, SHA2, [])
    notes = R.record_confirm(store, mint.ids["deploy"], SHA, "evidence")
    assert any("deploy.sha absent" in n for n in notes)
    row = R.read_round(store)
    assert row is not None and row["state"] == "deployed"
    assert "sha" not in row["deploy"]


def _stale_beat(store: Store, host: str, code_sha: str, minutes: int = 10) -> None:
    """A heartbeat at ``code_sha`` written *before* the deploy finished:
    the host has not restarted since the rollout. ``minutes`` backdates
    the beat (still inside the 60-minute liveness window)."""
    store.record_heartbeat(host, meta={"code_sha": code_sha})
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE host_heartbeat SET ts = now() - make_interval(mins => %s) "
            "WHERE host = %s",
            (minutes, host),
        )


def test_probe_parks_until_the_worker_restarts(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat predating the deploy finish parks the probe; a fresh
    beat at the release sha on the re-probe launches the confirm."""
    script = _round_script(checkout)
    _mint, _job, params = _walk_to_confirm(store, checkout, monkeypatch)
    _stale_beat(store, "node-a", SHA2)
    launches = list(checkout["argv"])
    ctx = Ctx(store, params)
    handle = rcf.submit(ctx, None)
    assert handle is not None and handle["phase"] == "probe", ctx.failures
    assert handle["rn"] == "r7" and handle["sha"] == SHA
    assert ctx.failures == []
    assert checkout["argv"] == launches  # nothing launched while waiting
    # The worker restarts and beats the release sha: the re-probe launches.
    store.record_heartbeat("node-a", meta={"code_sha": SHA})
    handle["not_before"] = "2000-01-01T00:00:00Z"
    assert rcf.poll(ctx, handle) is False
    assert handle["phase"] == "confirm"
    text = handle["evidence"]
    assert checkout["argv"][-1] == [
        str(script),
        "deploy",
        "--confirm-runtime",
        SHA,
        "--runtime-evidence",
        text,
    ]


def test_probe_still_stale_at_the_deadline_files_an_ask(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Still not restarted once the confirm window has passed: the probe
    fails naming the host, its last heartbeat and its sha — the plan
    tick's retry-cap ask goes to a human."""
    _round_script(checkout)
    mint, job_id, params = _walk_to_confirm(store, checkout, monkeypatch)
    _stale_beat(store, "node-a", SHA2, minutes=30)
    launches = list(checkout["argv"])
    ctx = Ctx(store, params)
    handle = rcf.submit(ctx, None)
    assert handle is not None and handle["phase"] == "probe", ctx.failures
    assert checkout["argv"] == launches
    # Re-date the deploy 11 minutes back — still after the beat, but the
    # 10-minute window has passed: the wait is over, the host failed.
    # _deployed_at prefers the round row's deploy.rollout_at, which the
    # walk's release_deploy.poll wrote.
    row = R.read_round(store)
    assert row is not None and row["state"] == "deployed"
    old = (datetime.now(UTC) - timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row["deploy"]["rollout_at"] = old
    row["state_at"] = old
    R.record_round(store, row)
    handle["not_before"] = "2000-01-01T00:00:00Z"
    assert rcf.poll(ctx, handle) is True
    reason = ctx.failures[0]
    assert "probe failed" in reason
    assert "node-a" in reason and "last heartbeat" in reason
    assert SHA2[:12] in reason and SHA[:12] in reason
    assert checkout["argv"] == launches  # nothing launched

    _fail_job(store, job_id, reason)
    res = pd.run_pass(store)
    assert res.asks == 1
    asks = _asks_under(store, mint.root_id)
    assert len(asks) == 1
    meta = _meta(store, asks[0])
    assert "node-a" in meta["ask"]["context"]
    assert "last heartbeat" in meta["ask"]["context"]
    assert "confirmed" not in _meta(store, mint.root_id)["release"]


# ── the engine-review gate on the cut (td574937 E1) ────────────────
#
# ``release_cut.validate_submit`` and ``submit`` refuse while a reviewer
# on an engine-stamped ``land_slice`` job is open or its FIX is unlanded
# — the same gate ``release_poll.run_pass`` runs; no asks are filed here.


def _reviewer(
    store: Store,
    from_id: int,
    *,
    status: str = "open",
    review: dict[str, Any] | None = None,
) -> int:
    meta: dict[str, Any] = {"generate": {"role": "reviewer", "from": f"td{from_id}"}}
    if review is not None:
        meta["review"] = review
    ref = store.insert_ref(
        kind="todo", slug=None, title=f"review td{from_id}", meta=meta
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    return int(ref.id)


def _engine_land(
    store: Store, build_id: int, paths: list[str] | None, *, sha: str | None = None
) -> int:
    sha = sha or (f"{build_id:x}" + "f" * 40)[:40]  # a realistic, per-build sha
    meta: dict[str, Any] = {
        "job_type": "land_slice",
        "params": {"todo_id": build_id},
        "landed_sha": sha,  # only a SUCCEEDED land can block
    }
    if paths is not None:
        meta["engine_paths"] = paths
    ref = store.insert_ref(kind="job", slug=None, title="land_slice", meta=meta)
    # The gate's source of truth is the commit in the cut range; the
    # job is only its sha → todo lookup (gr597625).
    _GIT_LANDS.append(
        rc._rp._GitLand(
            sha=sha, subject="", committed_at=None, paths=tuple(paths or [])
        )
    )
    return int(ref.id)


def _fake_gh_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compare: Any = None,
    main_sha: str = "5" * 40,
) -> None:
    """``release_cut``'s re-cut path reads GitHub through
    ``pr_class._get_json``: the compare against the scan's pin
    (td601005 — the compare's head is ``prod...<pin>``, no longer a
    ``prod...main`` literal). ``resolve_repo`` is stubbed to ``o/r`` so
    no checkout or setting is needed."""

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        if "/compare/prod..." in url:
            data: Any = compare or {
                "status": "ahead",
                "ahead_by": 1,
                "behind_by": 0,
                "commits": [{"sha": main_sha}],
            }
        else:
            data = {}
        return SimpleNamespace(status_code=200, json=lambda: data)

    monkeypatch.setattr(P, "_http_get", get)
    monkeypatch.setattr(P, "resolve_repo", lambda *a, **k: "o/r")


def _release_target(store: Store, root: int, sha: str, upto: list[str]) -> None:
    """The ``meta.release_target`` ``release_poll`` stamps at mint
    (gr592271): the cut sha and the sha12s at-or-before it."""
    store.stamp_ref_meta(root, {"release_target": {"sha": sha, "upto": upto}})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/ship")
def test_open_engine_reviewer_blocks_a_cut(
    store: Store, checkout: dict[str, Any]
) -> None:
    mint = T.mint_release_pass(store)
    _engine_land(store, 42, ["scripts/ship"])
    rid = _reviewer(store, 42)
    refusal = rc.validate_submit(store, params={"todo_id": mint.ids["cut"]})
    assert refusal is not None
    assert refusal.startswith("release_cut:")
    assert f"td{rid}" in refusal and "td42" in refusal
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is None
    assert checkout["argv"] == []
    assert ctx.failures and f"td{rid}" in ctx.failures[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/ship")
def test_non_engine_or_resolved_review_does_not_block_a_cut(
    store: Store, checkout: dict[str, Any]
) -> None:
    mint = T.mint_release_pass(store)
    _engine_land(store, 42, ["docs/a.md"])  # a non-engine diff
    _reviewer(store, 42)
    _engine_land(store, 43, ["scripts/ship"])
    _reviewer(store, 43, status="done", review={"verdict": "LAND"})
    assert rc.validate_submit(store, params={"todo_id": mint.ids["cut"]}) is None
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert handle is not None and checkout["argv"] != []


# ── the recorded cut target (gr592271) ─────────────────────────────
#
# A pass minted by the new poll carries ``meta.release_target =
# {sha, upto}``: the cut runs ``--cut-only --sha <sha>`` and only the
# lands inside ``upto`` may hold it — a newer land's open review sits
# past the cut. A re-cut (``meta.release`` set) recomputes the newest
# fully-reviewed head instead: the recorded window is stale once the
# fix landed.


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/ship")
def test_the_recorded_target_cuts_at_its_sha_despite_a_pending_review(
    store: Store, checkout: dict[str, Any]
) -> None:
    """gr592271: the pass recorded cut sha A; a newer engine land's open
    review sits past the ``upto`` window and does not hold the cut."""
    a, b = "a" * 40, "b" * 40
    mint = T.mint_release_pass(store, main_sha=a)
    _release_target(store, mint.root_id, a, [a[:12]])
    _engine_land(store, 42, ["scripts/ship"], sha=b)
    _reviewer(store, 42)
    assert rc.validate_submit(store, params={"todo_id": mint.ids["cut"]}) is None
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert ctx.failures == [] and handle is not None
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--cut-only", "--sha", a]
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/ship")
def test_a_land_inside_the_recorded_window_holds_the_cut(
    store: Store, checkout: dict[str, Any]
) -> None:
    """A land whose sha sits inside the recorded ``upto`` window —
    pushed before the cut but stamped after the poll's scan — gates it
    until cleared."""
    a = "a" * 40
    mint = T.mint_release_pass(store, main_sha=a)
    _release_target(store, mint.root_id, a, [a[:12], "f" * 12])
    _engine_land(store, 42, ["scripts/ship"], sha="f" * 40)
    rid = _reviewer(store, 42)
    refusal = rc.validate_submit(store, params={"todo_id": mint.ids["cut"]})
    assert refusal is not None and refusal.startswith("release_cut:")
    assert f"td{rid}" in refusal and "td42" in refusal
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is None
    assert checkout["argv"] == []
    assert ctx.failures and f"td{rid}" in ctx.failures[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/ship")
def test_a_recorded_cut_holds_while_the_fix_commit_is_outside(
    store: Store, checkout: dict[str, Any]
) -> None:
    """td601005: land A inside the recorded window cleared on a FIX
    whose corrective commit sits OUTSIDE the window — the cut must
    carry the fix, so it still holds."""
    a = "a" * 40
    mint = T.mint_release_pass(store, main_sha=a)
    _release_target(store, mint.root_id, a, [a[:12]])
    _engine_land(store, 42, ["scripts/ship"], sha=a)
    fix = store.insert_ref(kind="todo", slug=None, title="the fix", meta=None)
    _comment(store, int(fix.id), f"landed: {'f' * 9} — fix")
    _reviewer(store, 42, status="done", review={"verdict": "FIX", "task": int(fix.id)})
    refusal = rc.validate_submit(store, params={"todo_id": mint.ids["cut"]})
    assert refusal is not None and refusal.startswith("release_cut:")
    assert "corrective commit" in refusal and "td42" in refusal
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is None
    assert checkout["argv"] == []
    assert ctx.failures and "corrective commit" in ctx.failures[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/ship")
def test_the_recut_recomputes_the_newest_reviewed_head(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release-fix re-cut's recorded target is stale once the fix
    landed — it recomputes the newest fully-reviewed head and cuts
    THAT."""
    mint = _cut(store)  # meta.release set: the re-cut path
    _fake_gh_api(monkeypatch, main_sha=SHA2)
    assert rc.validate_submit(store, params={"todo_id": mint.ids["cut"]}) is None
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert ctx.failures == [] and handle is not None
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--cut-only", "--sha", SHA2]
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/ship")
def test_the_recut_stays_blocked_on_a_pending_review(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-cut's recompute is fail-closed too: an uncleared land at
    the window's start holds it, naming the pending review."""
    mint = _cut(store)
    b, c = "b" * 40, "c" * 40
    _fake_gh_api(
        monkeypatch,
        compare={
            "status": "ahead",
            "ahead_by": 2,
            "behind_by": 0,
            "commits": [{"sha": b}, {"sha": c}],
        },
    )
    _engine_land(store, 42, ["scripts/ship"], sha=b)
    rid = _reviewer(store, 42)
    refusal = rc.validate_submit(store, params={"todo_id": mint.ids["cut"]})
    assert refusal is not None
    assert f"td{rid}" in refusal and "td42" in refusal
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is None
    assert checkout["argv"] == []
    assert ctx.failures and f"td{rid}" in ctx.failures[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/ship")
def test_the_recut_fails_closed_on_an_unverifiable_fix(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td601285: the re-cut's compare comes back ahead with no commit
    list and the FIX's corrective sha names no scanned commit — the cut
    refuses instead of falling back to the pin."""
    mint = _cut(store)
    a = "a" * 40
    _fake_gh_api(
        monkeypatch,
        compare={"status": "ahead", "ahead_by": 1, "behind_by": 0},
    )
    _engine_land(store, 42, ["scripts/ship"], sha=a)
    fix = store.insert_ref(kind="todo", slug=None, title="the fix", meta=None)
    _comment(store, int(fix.id), f"landed: {'0' * 40} — fix")
    _reviewer(store, 42, status="done", review={"verdict": "FIX", "task": int(fix.id)})
    refusal = rc.validate_submit(store, params={"todo_id": mint.ids["cut"]})
    assert refusal is not None and refusal.startswith("release_cut:")
    assert "corrective commit" in refusal and "0" * 12 in refusal
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    assert rc.submit(ctx, None) is None
    assert checkout["argv"] == []
    assert ctx.failures and "corrective commit" in ctx.failures[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/ship")
def test_the_recut_orders_on_the_scan_when_the_compare_list_is_empty(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """td601285: no commit list in the re-cut's compare — the scan's
    own ordering stands in: [A FIX, C fixes A] recomputes to C, the
    cut CARRIES the corrective commit."""
    mint = _cut(store)
    a, c = "a" * 40, "c" * 40
    _fake_gh_api(
        monkeypatch,
        compare={"status": "ahead", "ahead_by": 2, "behind_by": 0},
    )
    _engine_land(store, 42, ["scripts/ship"], sha=a)
    fix = store.insert_ref(kind="todo", slug=None, title="the fix", meta=None)
    _comment(store, int(fix.id), f"landed: {c} — fix")
    _reviewer(store, 42, status="done", review={"verdict": "FIX", "task": int(fix.id)})
    # The fix commit sits inside the scanned window (a non-engine
    # path: in ``scanned`` without being a gate unit itself).
    _GIT_LANDS.append(
        rc._rp._GitLand(sha=c, subject="", committed_at=None, paths=("docs/a.md",))
    )
    assert rc.validate_submit(store, params={"todo_id": mint.ids["cut"]}) is None
    ctx = Ctx(store, {"todo_id": mint.ids["cut"]})
    handle = rc.submit(ctx, None)
    assert ctx.failures == [] and handle is not None
    assert checkout["argv"] == [
        [str(checkout["root"] / "scripts" / "release"), "--cut-only", "--sha", c]
    ]
