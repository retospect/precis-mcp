"""``ci_rerun`` (gr588028): the ``validated:`` non-repro path's one write —
``scripts/ci-rerun <run_id>`` re-fires the red check.yml run's failed legs,
once per run id. Detached ssh_node submit/poll like ``ci_red_digest``; the
gh call and process seams are faked. Also covers
``run_slice._fix_norepro``'s ruling and
``precis.plans.release.ci_rerun_fired``'s once-per-run-id read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis import settings as psettings
from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import Tag
from precis.workers._mint import mint_background_job
from precis.workers.job_types import (
    ci_rerun as cir,
)
from precis.workers.job_types import (
    get_job_type,
    known_job_types,
    run_slice,
)
from precis.workers.job_types import release_cut as rc

SHA = "a" * 40
RNG = f"{'b' * 40}..{SHA}"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    psettings.invalidate()


class Ctx:
    """The DispatchContext surface the op uses (test_release_ops' shape)."""

    def __init__(self, store: Store, params: dict[str, Any]) -> None:
        self.store = store
        self.ref_id = 4343
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


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "ci-rerun").write_text("#!/bin/sh\n", encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    seen: dict[str, Any] = {"argv": []}

    def launch(
        argv: list[str],
        *,
        cwd: Path,
        log_path: Path,
        exit_file: Path,
        extra_env: dict[str, str] | None = None,
    ) -> int:
        seen["argv"].append(argv)
        seen["cwd"], seen["log"], seen["exit"] = cwd, log_path, exit_file
        return 999_999

    monkeypatch.setattr(run_slice, "slice_root", lambda: root)
    monkeypatch.setattr(run_slice, "_process_identity", lambda pid: None)
    monkeypatch.setattr(run_slice, "_process_alive", lambda pid: True)
    monkeypatch.setattr(rc, "_run_dir", lambda: runs)
    monkeypatch.setattr(rc, "_launch", launch)
    seen["root"] = root
    return seen


def _exit(seen: dict[str, Any], code: int, out: str) -> None:
    Path(seen["log"]).write_text(out, encoding="utf-8")
    Path(seen["exit"]).write_text(f"{code}\n", encoding="utf-8")


def _fix_plan(store: Store) -> tuple[Any, dict[str, int]]:
    """A release pass with a recorded cut and the release-fix loop's
    iteration minted (digest -> fix -> recut -> poll)."""
    mint = T.mint_release_pass(store)
    R.record_cut(store, mint.ids["cut"], "r7", SHA, RNG, repo="o/r", host="node-a")
    it = T.mint_body(store, mint.ids["loop"], "release-fix", iteration=1)
    return mint, it.ids


def test_registered() -> None:
    spec = get_job_type("ci_rerun")
    assert spec is not None
    assert spec.compatible_executors == frozenset({"ssh_node"})
    assert spec.submit is not None and spec.poll is not None
    assert "ci_rerun" in known_job_types()


def test_submit_launches_ci_rerun_on_the_newest_red_run(
    store: Store, checkout: dict[str, Any]
) -> None:
    """The run id comes from the newest red verdict row on the cut sha —
    the same pick the digest makes; the argv is ``ci-rerun <run_id>``.
    A red run on ANOTHER sha is never picked."""
    _mint, it = _fix_plan(store)
    R.record_ci_verdict(store, "f" * 40, f"{'e' * 40}..{'f' * 40}", "red", "29")
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    ctx = Ctx(store, {"todo_id": it["fix"]})
    handle = cir.submit(ctx, None)
    assert ctx.failures == []
    assert checkout["argv"] == [[str(checkout["root"] / "scripts" / "ci-rerun"), "31"]]
    assert handle is not None and handle["run_id"] == "31"


def test_submit_refuses_a_green_or_missing_verdict(
    store: Store, checkout: dict[str, Any]
) -> None:
    """Never rerun a run that is not red: no red verdict row on the cut
    sha fails the job before any gh call."""
    _mint, it = _fix_plan(store)
    R.record_ci_verdict(store, SHA, RNG, "green", "30")
    ctx = Ctx(store, {"todo_id": it["fix"]})
    assert cir.submit(ctx, None) is None
    assert "no red verdict" in ctx.failures[0]
    assert checkout["argv"] == []


def test_submit_refuses_a_supplied_run_id(
    store: Store, checkout: dict[str, Any]
) -> None:
    """A supplied ``params.run_id`` is refused outright — the only run
    id is the cut's newest red verdict row; nothing launches."""
    _mint, it = _fix_plan(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    ctx = Ctx(store, {"todo_id": it["fix"], "run_id": "77"})
    assert cir.submit(ctx, None) is None
    assert "run_id is not accepted" in ctx.failures[0]
    assert checkout["argv"] == []
    refusal = cir.validate_submit(store, params={"todo_id": it["fix"], "run_id": "77"})
    assert refusal is not None and "run_id is not accepted" in refusal


def test_submit_refuses_a_plan_without_release_meta(
    store: Store, checkout: dict[str, Any]
) -> None:
    """A todo whose plan carries no ``meta.release`` (no recorded cut)
    is refused — there is no cut sha to pick the run from."""
    mint = T.mint_release_pass(store)  # no record_cut: no meta.release
    it = T.mint_body(store, mint.ids["loop"], "release-fix", iteration=1).ids
    ctx = Ctx(store, {"todo_id": it["fix"]})
    assert cir.submit(ctx, None) is None
    assert "meta.release" in ctx.failures[0]
    assert checkout["argv"] == []
    refusal = cir.validate_submit(store, params={"todo_id": it["fix"]})
    assert refusal is not None and "meta.release" in refusal


def test_submit_refuses_a_todo_under_no_release_plan(
    store: Store, checkout: dict[str, Any]
) -> None:
    """A todo that is not under a release plan at all is refused the
    same way — no ``plan:`` root, no ``meta.release``."""
    ref = store.insert_ref(kind="todo", slug=None, title="a lone todo", meta={})
    ctx = Ctx(store, {"todo_id": int(ref.id)})
    assert cir.submit(ctx, None) is None
    assert "meta.release" in ctx.failures[0]
    assert checkout["argv"] == []


def test_poll_settles_then_succeeds_with_the_run_id(
    store: Store, checkout: dict[str, Any]
) -> None:
    """An accepted rerun parks once (~5 min) — GitHub needs the beat to
    flip the run to in_progress — then the job succeeds carrying
    ``meta.ci_rerun.run_id`` for the tick's verdict-poll mint."""
    _mint, it = _fix_plan(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    ctx = Ctx(store, {"todo_id": it["fix"]})
    handle = cir.submit(ctx, None)
    assert cir.poll(ctx, handle) is False  # the script is still running
    _exit(
        checkout,
        0,
        "ci-rerun: run 31 rerun of the failed jobs requested\nci-rerun: attempt=1\n",
    )
    assert cir.poll(ctx, handle) is False  # parked for the settle window
    assert "not_before" in handle
    handle["not_before"] = "2000-01-01T00:00:00Z"
    assert cir.poll(ctx, handle) is True
    assert ctx.status == "succeeded", ctx.failures
    assert ctx.stamped["ci_rerun"] == {
        "run_id": "31",
        "rerun_attempt_before": 1,
    }


def test_poll_auth_refusal_names_the_probe(
    store: Store, checkout: dict[str, Any]
) -> None:
    """A 403 — the credential lacks Actions write — fails with a clear
    message naming the hand check; only the script's own ``ci-rerun:``
    lines are scanned."""
    _mint, it = _fix_plan(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    ctx = Ctx(store, {"todo_id": it["fix"]})
    handle = cir.submit(ctx, None)
    _exit(
        checkout,
        1,
        "ci-rerun: gh run rerun 31 --failed failed (1): "
        "gh: Resource not accessible by integration (HTTP 403)\n",
    )
    assert cir.poll(ctx, handle) is True
    assert ctx.status is None and len(ctx.failures) == 1
    assert "auth refusal" in ctx.failures[0]
    assert "scripts/ci-rerun --probe" in ctx.failures[0]


def test_poll_ordinary_failure_shows_the_tail(
    store: Store, checkout: dict[str, Any]
) -> None:
    _mint, it = _fix_plan(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    ctx = Ctx(store, {"todo_id": it["fix"]})
    handle = cir.submit(ctx, None)
    _exit(checkout, 1, "ci-rerun: gh run rerun 31 --failed failed (1): run not found\n")
    assert cir.poll(ctx, handle) is True
    assert ctx.status is None and "exit 1" in ctx.failures[0]


def test_factory_off_refuses(
    store: Store, checkout: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_FACTORY", "off")
    _mint, it = _fix_plan(store)
    refusal = cir.validate_submit(store, params={"todo_id": it["fix"]})
    assert refusal is not None and "PRECIS_FACTORY=off" in refusal
    ctx = Ctx(store, {"todo_id": it["fix"]})
    assert cir.submit(ctx, None) is None
    assert "PRECIS_FACTORY=off" in ctx.failures[0]
    assert checkout["argv"] == []


def test_validate_submit_pins_the_cut_host(store: Store) -> None:
    _mint, it = _fix_plan(store)
    refusal = cir.validate_submit(
        store, params={"todo_id": it["fix"], "target_node": "node-b"}
    )
    assert refusal is not None and "node-a" in refusal
    assert (
        cir.validate_submit(
            store, params={"todo_id": it["fix"], "target_node": "node-a"}
        )
        is None
    )


def test_ci_rerun_fired_is_once_per_run_id(store: Store) -> None:
    """Any job carrying ``ci_rerun:<run_id>`` — any status — counts: the
    once-per-run-id rule outlives the job's active window."""
    _mint, it = _fix_plan(store)
    assert not R.ci_rerun_fired(store, "31")
    with store.tx() as conn:
        mint_background_job(
            store,
            conn=conn,
            job_type="ci_rerun",
            executor="ssh_node",
            title="ci_rerun",
            params={"todo_id": it["fix"]},
            parent_id=it["fix"],
            idem_key="ci_rerun:31",
        )
    assert R.ci_rerun_fired(store, "31")
    assert not R.ci_rerun_fired(store, "32")


def test_newest_red_run_picks_the_newest_red_row_on_the_sha(store: Store) -> None:
    _mint, _it = _fix_plan(store)
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    assert R.newest_red_run(store, SHA) == "31"
    R.record_ci_verdict(store, SHA, RNG, "red", "33")
    assert R.newest_red_run(store, SHA) == "33"
    # A green on the same sha replaces the row (dedupe by sha): no red left.
    R.record_ci_verdict(store, SHA, RNG, "green", "33")
    assert R.newest_red_run(store, SHA) is None


def test_fix_norepro_rulings(store: Store) -> None:
    """``rerun`` only on the release-fix loop's fix node under a
    ``release-pass`` root; ``spent`` once the run id's rerun fired;
    ``None`` everywhere else."""
    mint, it = _fix_plan(store)
    fix, digest = it["fix"], it["digest"]
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    assert run_slice._fix_norepro(store, fix) == "rerun"
    # Not a fix node: the digest itself, a non-release plan task, and a
    # fix-shaped todo under no release-pass root all stay None.
    assert run_slice._fix_norepro(store, digest) is None
    assert run_slice._fix_norepro(store, mint.ids["cut"]) is None
    with store.tx() as conn:
        mint_background_job(
            store,
            conn=conn,
            job_type="ci_rerun",
            executor="ssh_node",
            title="ci_rerun",
            params={"todo_id": fix},
            parent_id=fix,
            idem_key="ci_rerun:31",
        )
    assert run_slice._fix_norepro(store, fix) == "spent"


def test_fix_norepro_without_the_release_pass_tag_is_none(store: Store) -> None:
    """A fix-shaped link outside a ``release-pass`` plan is not this
    path: the root tag is part of the ruling."""
    mint, it = _fix_plan(store)
    fix = it["fix"]
    store.remove_tag(mint.root_id, Tag.open("release-pass"))
    R.record_ci_verdict(store, SHA, RNG, "red", "31")
    assert run_slice._fix_norepro(store, fix) is None
