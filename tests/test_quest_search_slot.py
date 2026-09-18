"""``dispatch_search`` + its WIP-slot spend in ``_stage_compute`` — item 4 of
docs/backlog/global-structure-search-gofee-agox.md, round 2 (struct_search
already writes back candidates; this is the quest opt-in that mints the
job).  No AGOX/MACE here: ``dispatch_search`` only mints the ``struct_search``
job row, it never runs the search — real dispatch is
``tests/test_struct_search.py``'s territory.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.quest import compute as compute_mod
from precis.quest.compute import dispatch_search
from precis.quest.tick import run_quest_tick

_SEED_SPEC = json.dumps(
    {"ops": [{"op": "slab", "element": "Pd", "size": [3, 3, 4], "fix_layers": 2}]}
)

_SEARCH_CFG: dict[str, Any] = {
    "box": [[0.0, 1.0], [0.0, 1.0], [0.5, 0.8]],
    "add": {"Pd": 2, "N": 1, "O": 1},
}


def _mk_quest(store: Any, *, slug: str = "q-search-slot") -> int:
    # quest is a numeric-slug kind — insert_ref(slug=...) is rejected.
    ref = store.insert_ref(kind="quest", slug=None, title=f"a striving ({slug})")
    return int(ref.id)


def _mk_seed(store: Any) -> Any:
    StructureHandler(hub=Hub(store=store)).put(id="pd_search_seed", text=_SEED_SPEC)
    return store.get_ref(kind="structure", id="pd_search_seed")


def _serve(store: Any, quest_id: int, ref_id: int) -> None:
    store.add_link(src_ref_id=ref_id, dst_ref_id=quest_id, relation="serves")


def _job_count_for_parent(store: Any, parent_ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND retired_at IS NULL",
            (parent_ref_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _job_ids_for_parent(store: Any, parent_ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND retired_at IS NULL ORDER BY ref_id",
            (parent_ref_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


class TestDispatchSearch:
    def test_no_meta_search_is_a_noop(self, store: Any) -> None:
        quest_id = _mk_quest(store)
        seed_ref = _mk_seed(store)
        _serve(store, quest_id, seed_ref.id)

        assert dispatch_search(store, quest_id) is None
        assert _job_count_for_parent(store, seed_ref.id) == 0

    def test_seed_not_serving_skips_with_a_note(self, store: Any) -> None:
        quest_id = _mk_quest(store)
        seed_ref = _mk_seed(store)
        # deliberately not wired `serves` — the seed doesn't serve this quest.
        store.stamp_ref_meta(
            quest_id, {"search": {"seed": seed_ref.slug, **_SEARCH_CFG}}
        )

        note = dispatch_search(store, quest_id)

        assert note is not None
        assert note.startswith("search skipped:")
        assert "does not serve" in note
        assert _job_count_for_parent(store, seed_ref.id) == 0

    def test_dispatches_one_job_then_dedupes_on_idem_key(self, store: Any) -> None:
        quest_id = _mk_quest(store)
        seed_ref = _mk_seed(store)
        _serve(store, quest_id, seed_ref.id)
        store.stamp_ref_meta(
            quest_id, {"search": {"seed": seed_ref.slug, **_SEARCH_CFG}}
        )

        note = dispatch_search(store, quest_id)
        assert note is not None
        assert note.startswith("search[gofee] dispatched for pd_search_seed")
        assert _job_count_for_parent(store, seed_ref.id) == 1

        job_row = store.get_ref(
            kind="job", id=next(iter(_job_ids_for_parent(store, seed_ref.id)))
        )
        assert job_row is not None
        params = (job_row.meta or {}).get("params") or {}
        assert params["seed_ref_id"] == seed_ref.id
        assert params["box"] == _SEARCH_CFG["box"]
        assert params["add"] == _SEARCH_CFG["add"]
        assert params["algo"] == "gofee"
        assert params["budget"] == 200
        assert (
            (job_row.meta or {})
            .get("idem_key", "")
            .startswith(f"struct_search:{quest_id}:")
        )

        # A second call against the SAME (unchanged) config collapses onto the
        # existing job -- no second row, and the note names it explicitly.
        note2 = dispatch_search(store, quest_id)
        assert note2 is not None
        assert "idem_key already exists as job" in note2
        assert _job_count_for_parent(store, seed_ref.id) == 1

    def test_changing_box_re_arms_a_fresh_search(self, store: Any) -> None:
        quest_id = _mk_quest(store)
        seed_ref = _mk_seed(store)
        _serve(store, quest_id, seed_ref.id)
        store.stamp_ref_meta(
            quest_id, {"search": {"seed": seed_ref.slug, **_SEARCH_CFG}}
        )
        dispatch_search(store, quest_id)
        assert _job_count_for_parent(store, seed_ref.id) == 1

        widened = dict(_SEARCH_CFG)
        widened["box"] = [[0.0, 1.0], [0.0, 1.0], [0.4, 0.9]]
        store.stamp_ref_meta(quest_id, {"search": {"seed": seed_ref.slug, **widened}})

        note = dispatch_search(store, quest_id)
        assert note is not None
        assert note.startswith("search[gofee] dispatched")
        assert _job_count_for_parent(store, seed_ref.id) == 2


class TestSearchSlotInTick:
    """The tick-level seam: an opted-in search spends the WIP slot, so the
    same tick's LLM proposal (which would otherwise consume it) is dropped
    to a lead instead of dispatched."""

    def _stub_run_compute_step(self, monkeypatch: Any) -> list[list[dict[str, Any]]]:
        """Mirrors ``TestCommitReRepromptLadder._stub_run_compute_step`` in
        ``tests/test_quest_tick.py``: a non-empty ``proposals`` list
        "dispatches" (records one sim); an empty one dispatches nothing."""
        calls: list[list[dict[str, Any]]] = []

        def _fake(
            _store: Any,
            _quest_id: int,
            proposals: list[dict[str, Any]],
            *,
            hub: Any = None,
            dispatch: bool = True,
            by: str = "agent",
        ) -> Any:
            proposals = list(proposals or [])
            calls.append(proposals)
            n = 1 if proposals else 0
            return compute_mod.ComputeStep(
                candidates_created=n,
                sims_dispatched=n,
                results_harvested=0,
                ruled_out=0,
                notes=[],
                graduated=0,
            )

        monkeypatch.setattr(compute_mod, "run_compute_step", _fake)
        return calls

    @staticmethod
    def _disp(_req: Any) -> Any:
        return SimpleNamespace(
            data={
                "logbook": [],
                "dossier_markdown": "",
                "proposals": [
                    {
                        "name": "Fe adatom",
                        "rationale": "an untried dopant",
                        "structure": {
                            "ops": [{"op": "slab", "element": "Fe", "size": [2, 2, 2]}]
                        },
                    }
                ],
            },
            text="",
            error=None,
            cost_usd=0.01,
            paused=False,
        )

    def test_dispatched_search_spends_the_wip_slot(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_run_compute_step(monkeypatch)

        quest_id = _mk_quest(store)
        seed_ref = _mk_seed(store)
        _serve(store, quest_id, seed_ref.id)
        store.stamp_ref_meta(
            quest_id, {"search": {"seed": seed_ref.slug, **_SEARCH_CFG}}
        )

        out = run_quest_tick(store, quest_id, dispatch_fn=self._disp, compute=True)

        assert out.status == "succeeded"
        # the search spent the (default = 1) WIP slot -> run_compute_step
        # never saw the LLM's Fe-adatom proposal this tick.
        assert calls == [[]]
        # `dispatched` counts the search itself, so the stall counter resets
        # rather than treating this as a dry tick.
        assert out.sims_dispatched == 1
        assert _job_count_for_parent(store, seed_ref.id) == 1

    def test_no_opt_in_leaves_the_slot_for_the_llm_proposal(
        self, store: Any, monkeypatch: Any
    ) -> None:
        calls = self._stub_run_compute_step(monkeypatch)

        quest_id = _mk_quest(store)
        seed_ref = _mk_seed(store)
        _serve(store, quest_id, seed_ref.id)
        # No `meta.search` -- unchanged behaviour: the LLM's own proposal
        # gets the whole slot.

        out = run_quest_tick(store, quest_id, dispatch_fn=self._disp, compute=True)

        assert out.status == "succeeded"
        assert len(calls) == 1 and len(calls[0]) == 1
        assert _job_count_for_parent(store, seed_ref.id) == 0
