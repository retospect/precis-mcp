"""Real-PG tests for the Health tab's release-pass panel (gr606988).

``precis_web.release_passes.release_passes`` reads what the dark release
ops write: the ``release-pass`` plan root's ``meta.release``, the plan
nodes' STATUS + blocked-by edges, the ``release_cut`` jobs'
``meta.release_cut`` (the r<N> history — a re-cut stays in the pass but
takes a new number) and the ``release.*`` app_settings rows. These tests
seed that state with the ops' own writers
(:func:`precis.plans.templates.mint_release_pass`,
:func:`precis.plans.release.record_cut` / ``record_ci_verdict`` /
``record_prod_head``) so the panel's SQL is exercised against the rows
it will actually meet.
"""

from __future__ import annotations

from typing import Any

from precis.plans import release as R
from precis.plans import templates as T
from precis.store import Store, Tag
from precis_web.release_passes import release_passes

SHA = "a" * 40
SHA2 = "b" * 40
RNG = f"{'c' * 12}..{SHA[:12]}"
RNG2 = f"{SHA[:12]}..{SHA2[:12]}"


def _row(store: Store, root_id: int) -> dict[str, Any]:
    rows = {p["ref_id"]: p for p in release_passes(store)["passes"]}
    return rows[root_id]


def _cut(store: Store, mint: T.PlanMint, rn: str, sha: str, rng: str) -> None:
    R.record_cut(store, mint.ids["cut"], rn, sha, rng, repo="o/r")


def _cut_job(store: Store, node_id: int, rn: str, sha: str, rng: str) -> None:
    """The ``meta.release_cut`` a finished ``release_cut`` job carries —
    the worker stamps it on the job ref (``ctx.set_meta``), parented to
    the plan node it served."""
    store.insert_ref(
        kind="job",
        slug=None,
        title=f"release_cut td{node_id}",
        meta={
            "job_type": "release_cut",
            "release_cut": {"rN": rn, "sha": sha, "range": rng},
        },
        parent_id=node_id,
    )


def _poll_job(store: Store, node_id: int, sha: str, rng: str, run_id: str = "") -> None:
    """The ``meta.ci_verdict_poll`` a parked ``ci_verdict_poll`` job
    carries (``ctx.set_meta`` on the job ref, parented to the poll node)
    — the only record of an in-flight run's id while its verdict is out."""
    store.insert_ref(
        kind="job",
        slug=None,
        title=f"ci_verdict_poll td{node_id}",
        meta={
            "job_type": "ci_verdict_poll",
            "ci_verdict_poll": {
                "sha": sha,
                "range": rng,
                "state": "pending",
                "run_id": run_id,
            },
        },
        parent_id=node_id,
    )


def _ruling_ask(store: Store, root_id: int, *, blocks: str = "") -> Any:
    """The ruling ask the tick files under a pass root — the exhausted
    release-fix loop's ``_mint_loop_ask`` shape (parented to the root,
    tagged ``plan:<root>``, ``blocks`` the root; ``blocks="nothing"``
    gives the informational no-run stall ask instead)."""
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title="release r7: fix iterations ended red — human call",
        meta={
            "ask": {
                "kind": "ruling",
                "options": ["close the pass", "hand release with /go"],
                "blocks": blocks or f"td{root_id}",
                "context": "release r7: fix iterations ended red",
            }
        },
        parent_id=root_id,
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "open"), set_by="system", replace_prefix=True
    )
    store.add_tag(ref.id, Tag.open(f"plan:{root_id}"), set_by="system")
    return ref


def _done(store: Store, todo_id: int) -> None:
    store.add_tag(
        todo_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )


def test_open_pass_row_shows_cut_range_verdict_run_and_step(store: Store) -> None:
    mint = T.mint_release_pass(store)
    _cut(store, mint, "r7", SHA, RNG)
    R.record_ci_verdict(store, SHA, RNG, "green", "555")
    R.record_prod_head(store, SHA, [SHA[:12]])

    p = _row(store, mint.root_id)

    assert p["open"] is True and p["status"] == "open"
    assert p["rns"] == "r7"
    assert p["range"] == RNG
    assert p["gate"]["verdict"] == "green"
    assert p["gate"]["run_url"] == "https://github.com/o/r/actions/runs/555"
    assert p["loop"] == "0/3"
    # Freshly minted: the cut node is the only unblocked step.
    assert p["step"] == "cutting the release"
    assert [s["role"] for s in p["steps"]] == [
        "cut",
        "poll",
        "gate",
        "loop",
        "approval",
        "deploy",
        "confirm",
    ]
    assert [s["state"] for s in p["steps"]] == [
        "active",
        "pending",
        "pending",
        "pending",
        "pending",
        "pending",
        "pending",
    ]
    assert p["served"] is True

    # The panel head carries prod's served sha.
    prod = release_passes(store)["prod_head"]
    assert prod is not None and prod["sha"] == SHA


def test_step_advances_with_closed_blockers_and_loop_count(store: Store) -> None:
    mint = T.mint_release_pass(store)
    _cut(store, mint, "r7", SHA, RNG)

    _done(store, mint.ids["cut"])
    _done(store, mint.ids["poll"])
    p = _row(store, mint.root_id)
    # No verdict on the cut yet: gate + loop are both unblocked.
    assert p["step"] == "awaiting the CI verdict"

    # A recorded red makes the fix loop the live stage; a minted
    # iteration stamps meta.loop.count.
    R.record_ci_verdict(store, SHA, RNG, "red", "77")
    ref = store.get_ref(kind="todo", id=mint.ids["loop"])
    assert ref is not None
    loop = dict(ref.meta["loop"])
    loop["count"] = 1
    store.stamp_ref_meta(mint.ids["loop"], {"loop": loop})
    p = _row(store, mint.root_id)
    assert p["step"] == "fix loop — iteration 1 of 3"
    assert p["loop"] == "1/3"
    assert p["gate"]["verdict"] == "red"

    # Green on the range lets the tick close the gate and the loop (the
    # approval ask, blocked-by the gate, is then the live step — minted
    # ask-user while release.auto_deploy is off).
    R.record_ci_verdict(store, SHA2, RNG, "green", "88")
    _done(store, mint.ids["gate"])
    _done(store, mint.ids["loop"])
    p = _row(store, mint.root_id)
    assert p["step"].startswith("awaiting deploy approval")


def test_recut_renumbers_inside_the_one_pass_row(store: Store) -> None:
    """gr606988's detail: r259's loop-1 re-cut became r260 under the same
    pass root — one row naming ``r259 → r260``, never two rows."""
    mint = T.mint_release_pass(store)
    _cut(store, mint, "r259", SHA, RNG)
    _cut_job(store, mint.ids["cut"], "r259", SHA, RNG)

    # The fix loop mints an iteration; its recut node runs a second
    # release_cut job, which records the new number on the same root.
    fix = T.mint_release_fix(store, mint.ids["loop"], iteration=1)
    R.record_cut(store, fix.ids["recut"], "r260", SHA2, RNG2, repo="o/r")
    _cut_job(store, fix.ids["recut"], "r260", SHA2, RNG2)

    p = _row(store, mint.root_id)
    assert p["rns"] == "r259 → r260"
    assert p["range"] == RNG2
    assert p["sha12"] == SHA2[:12]


def test_pass_without_a_cut_yet(store: Store) -> None:
    mint = T.mint_release_pass(store)

    p = _row(store, mint.root_id)

    assert p["rns"] == "—"
    assert p["range"] == ""
    assert p["gate"]["verdict"] == "none"
    assert p["step"] == "cutting the release"
    assert p["served"] is None


def test_closed_pass_lists_after_open_ones(store: Store) -> None:
    mint = T.mint_release_pass(store)
    _cut(store, mint, "r9", SHA, RNG)
    _done(store, mint.root_id)

    passes = release_passes(store)["passes"]
    row = {p["ref_id"]: p for p in passes}[mint.root_id]
    assert row["open"] is False
    assert row["step"] == "done"
    # Closed rows sort after every open pass.
    open_idx = [i for i, p in enumerate(passes) if p["open"]]
    own_idx = [i for i, p in enumerate(passes) if p["ref_id"] == mint.root_id][0]
    assert all(i < own_idx for i in open_idx)


def test_pending_poll_job_surfaces_the_running_ci(store: Store) -> None:
    """While the verdict is still out, the parked poll job's stamp is the
    only record of the in-flight run — the panel shows its link and state
    instead of a bare "pending" (td609076)."""
    mint = T.mint_release_pass(store)
    _cut(store, mint, "r7", SHA, RNG)
    _done(store, mint.ids["cut"])
    _poll_job(store, mint.ids["poll"], SHA, RNG, run_id="4321")

    p = _row(store, mint.root_id)

    assert p["gate"]["verdict"] == "pending"
    assert p["gate"]["run_id"] == "4321"
    assert p["gate"]["run_url"] == "https://github.com/o/r/actions/runs/4321"
    assert p["step"] == "CI running — run 4321"

    # A stamp for an earlier cut never poses as the live CI: a re-cut
    # makes the old poll's sha/range stale until the next poll lands.
    _cut(store, mint, "r8", SHA2, RNG2)
    p = _row(store, mint.root_id)
    assert p["gate"]["verdict"] == "pending"
    assert p["gate"]["run_id"] == ""
    assert p["step"] == "recording the CI verdict"


def test_exhausted_loop_ruling_outranks_the_red_gate(store: Store) -> None:
    """The exhausted release-fix loop's ruling ask parks the pass on a
    human call — the step names the ask, not "CI red" (td609076)."""
    mint = T.mint_release_pass(store)
    _cut(store, mint, "r7", SHA, RNG)
    R.record_ci_verdict(store, SHA, RNG, "red", "77")
    _done(store, mint.ids["cut"])
    _done(store, mint.ids["poll"])
    _done(store, mint.ids["loop"])

    # Gate open + red on record: the pre-fix display.
    assert _row(store, mint.root_id)["step"] == "CI red"

    ask = _ruling_ask(store, mint.root_id)
    assert _row(store, mint.root_id)["step"] == f"awaiting a ruling — td{ask.id}"


def test_informational_ask_stays_below_the_pipeline(store: Store) -> None:
    """A ``blocks: "nothing"`` ask (the poll's no-run stall note) is a
    record, not a decision — it never masks live pipeline activity."""
    mint = T.mint_release_pass(store)
    _cut(store, mint, "r7", SHA, RNG)
    _done(store, mint.ids["cut"])
    _ruling_ask(store, mint.root_id, blocks="nothing")

    p = _row(store, mint.root_id)
    assert p["step"] == "recording the CI verdict"


def test_panel_shape_when_no_pass_seeded(store: Store) -> None:
    """The four reads degrade cleanly — assert the shape, not emptiness
    (other tests on the shared test DB may leave passes behind)."""
    out = release_passes(store)
    assert isinstance(out["passes"], list)
    assert all(isinstance(p["handle"], str) for p in out["passes"])
