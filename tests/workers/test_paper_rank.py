"""Tests for the ``paper_rank`` deterministic reading-priority pass.

End-to-end (claim/score/write) runs against real PG (the ``store`` fixture,
no LLM/network); the PageRank power-iteration is exercised as a pure unit
test on top. See ``workers/paper_rank.py``'s module docstring for the rubric
+ feynman provenance.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.store import Store
from precis.workers.paper_rank import (
    PAPER_RANK_VERSION,
    _compute_pagerank,
    run_paper_rank_pass,
    top_ranked_papers,
)
from tests.workers._helpers import seed_chunk, seed_ref


def _set_meta(store: Store, ref_id: int, meta: dict[str, Any]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (Jsonb(meta), ref_id),
        )
        conn.commit()


def _set_year(store: Store, ref_id: int, year: int) -> None:
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET year = %s WHERE ref_id = %s", (year, ref_id))
        conn.commit()


def _paper_rank_meta(store: Store, ref_id: int) -> dict[str, Any] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'paper_rank' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return row[0] if row and row[0] else None


_RIGOROUS_ABSTRACT = (
    "We present an empirical evaluation and ablation study with a strong "
    "baseline comparison. We report the metric, result, and benchmark "
    "against the dataset, with a confidence interval and error bar to "
    "quantify statistical significance and variance. Code, checkpoint, and "
    "artifact are released on github as an open source repository to "
    "reproduce every result; see our repository for the dataset."
)


# ── 1. ordering (backlog acceptance test) ─────────────────────────────────


class TestOrdering:
    def test_high_rigor_paper_ranks_above_thin_stub(self, store: Store) -> None:
        # Thin stub: no openalex block at all (so it's not even a graph
        # node), no abstract, no body chunks, no year, no pdf.
        thin_id = seed_ref(store, title="A thin stub")

        rigor_id = seed_ref(store, title="A rigorous study")
        seed_chunk(store, ref_id=rigor_id, text=_RIGOROUS_ABSTRACT, ord=0)
        _set_year(store, rigor_id, 2020)
        _set_meta(
            store,
            rigor_id,
            {
                "abstract": _RIGOROUS_ABSTRACT,
                "openalex": {
                    "id": "W1",
                    "fwci": 5.0,
                    "cited_by_count": 200,
                    # W2 doesn't resolve to any corpus paper's openalex id
                    # (the thin stub carries none) -- a dangling reference,
                    # not a graph edge; the citation graph stays edge-free.
                    "referenced_works": ["W2"],
                    "oa_status": "gold",
                },
            },
        )

        result = run_paper_rank_pass(store, batch_size=100)
        assert result["claimed"] == 2
        assert result["ok"] == 2
        assert result["failed"] == 0

        rigor_meta = _paper_rank_meta(store, rigor_id)
        thin_meta = _paper_rank_meta(store, thin_id)
        assert rigor_meta is not None
        assert thin_meta is not None

        top = top_ranked_papers(store, limit=10)
        assert top[0]["ref_id"] == rigor_id
        assert rigor_meta["read_first"] > thin_meta["read_first"]


# ── 2. idempotency ─────────────────────────────────────────────────────────


class TestIdempotency:
    def test_second_run_writes_nothing(self, store: Store) -> None:
        ref_id = seed_ref(store, title="Some paper")
        seed_chunk(store, ref_id=ref_id, text=_RIGOROUS_ABSTRACT, ord=0)
        _set_meta(store, ref_id, {"abstract": _RIGOROUS_ABSTRACT})

        first = run_paper_rank_pass(store, batch_size=100)
        assert first == {"claimed": 1, "ok": 1, "failed": 0}
        first_block = _paper_rank_meta(store, ref_id)
        assert first_block is not None
        assert first_block["version"] == PAPER_RANK_VERSION

        second = run_paper_rank_pass(store, batch_size=100)
        assert second == {"claimed": 0, "ok": 0, "failed": 0}
        second_block = _paper_rank_meta(store, ref_id)
        assert second_block == first_block  # byte-for-byte, incl. computed_at


# ── 3. renormalization ──────────────────────────────────────────────────────


class TestRenormalization:
    def test_missing_year_and_openalex_id_drops_three_signals(
        self, store: Store
    ) -> None:
        ref_id = seed_ref(store, title="No year, no openalex id")
        seed_chunk(store, ref_id=ref_id, text=_RIGOROUS_ABSTRACT, ord=0)
        _set_meta(store, ref_id, {"abstract": _RIGOROUS_ABSTRACT})
        # No year set (NULL), no openalex block at all.

        run_paper_rank_pass(store, batch_size=100)
        block = _paper_rank_meta(store, ref_id)
        assert block is not None
        unavailable = set(block["unavailable"])
        assert unavailable == {"citation_impact", "graph_prestige", "citation_velocity"}

        components = block["components"]
        methodology = components["methodology"]
        reproducibility = components["reproducibility"]
        assert methodology is not None
        assert reproducibility is not None
        expected = round((0.1 * methodology + 0.1 * reproducibility) / 0.2, 1)
        assert block["read_first"] == expected


# ── 4. retraction cap ────────────────────────────────────────────────────


class TestRetraction:
    def test_retracted_paper_capped_at_20(self, store: Store) -> None:
        ref_id = seed_ref(store, title="A retracted-but-rigorous paper")
        seed_chunk(store, ref_id=ref_id, text=_RIGOROUS_ABSTRACT, ord=0)
        _set_year(store, ref_id, 2020)
        _set_meta(
            store,
            ref_id,
            {
                "abstract": _RIGOROUS_ABSTRACT,
                "openalex": {
                    "id": "W10",
                    "fwci": 9.0,
                    "cited_by_count": 500,
                    "is_retracted": True,
                    "oa_status": "gold",
                },
            },
        )

        run_paper_rank_pass(store, batch_size=100)
        block = _paper_rank_meta(store, ref_id)
        assert block is not None
        assert block["retracted"] is True
        assert block["read_first"] <= 20.0


# ── 5. marker cache / fingerprint short-circuit ──────────────────────────


class TestMarkerCache:
    def test_fingerprint_change_triggers_rescan_unchanged_paper_skipped(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import precis.workers.paper_rank as pr_mod

        changing_id = seed_ref(store, title="Will grow a body chunk")
        _set_meta(store, changing_id, {"abstract": _RIGOROUS_ABSTRACT})

        stable_id = seed_ref(store, title="Stays exactly the same")
        seed_chunk(store, ref_id=stable_id, text="plain prose, no markers here", ord=0)

        run_paper_rank_pass(store, batch_size=100)
        stable_before = _paper_rank_meta(store, stable_id)
        assert stable_before is not None

        # Grow the changing paper's body -> its fingerprint changes.
        seed_chunk(store, ref_id=changing_id, text=_RIGOROUS_ABSTRACT, ord=0)

        calls: list[list[int]] = []
        real_fetch = pr_mod._fetch_body_text

        def _counting_fetch(store_arg: Store, ref_ids: list[int]) -> dict[int, str]:
            calls.append(list(ref_ids))
            return real_fetch(store_arg, ref_ids)

        monkeypatch.setattr(pr_mod, "_fetch_body_text", _counting_fetch)

        result = run_paper_rank_pass(store, batch_size=100)
        assert result["ok"] >= 1

        # Only the changed paper's id was ever handed to the chunk-fetch
        # helper; the untouched paper's cached markers were reused.
        fetched_ids = {rid for batch in calls for rid in batch}
        assert changing_id in fetched_ids
        assert stable_id not in fetched_ids

        stable_after = _paper_rank_meta(store, stable_id)
        assert stable_after == stable_before  # untouched, not rescanned

        changed_block = _paper_rank_meta(store, changing_id)
        assert changed_block is not None
        assert changed_block["markers"]["method"] > 0


# ── 6. PageRank unit test ────────────────────────────────────────────────


class TestComputePagerank:
    def test_most_cited_node_ranks_highest_and_ranks_sum_to_one(self) -> None:
        # 1 -> 3, 2 -> 3: node 3 is cited by both, node 1/2 cite but aren't
        # cited by anyone.
        nodes = [1, 2, 3]
        edges = [(1, 3), (2, 3)]
        pr = _compute_pagerank(nodes, edges)
        assert pr[3] > pr[1]
        assert pr[3] > pr[2]
        assert sum(pr.values()) == pytest.approx(1.0, abs=1e-6)
