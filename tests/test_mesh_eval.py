"""Mesh-upkeep evals (local-mesh-upkeep slice 0).

Pure tests: no DB, no model. The runner drives a stub ``call_fn``; the
acceptance library drives a fake judge.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.errors import BadInput
from precis.mesh_eval.scorers import score_item
from precis.mesh_eval.sets import MeshItem, dump_set, load_set, split_holdout
from precis.mesh_eval.stats import wilson_interval, wilson_upper

# ── stats ─────────────────────────────────────────────────────────


def test_wilson_bounds() -> None:
    lo, hi = wilson_interval(0, 0)
    assert (lo, hi) == (0.0, 1.0)
    lo, hi = wilson_interval(5, 10)
    assert 0 < lo < 0.5 < hi < 1
    lo95 = wilson_interval(0, 20)[1]
    assert lo95 < 0.2  # 0/20 should upper-bound well under the 0.10–0.15 bars
    assert wilson_upper(0, 100, z=1.645) < 0.03


def test_wilson_monotone_in_k() -> None:
    prev = 0.0
    for k in range(0, 51, 5):
        lo, hi = wilson_interval(k, 50)
        assert 0 <= lo <= hi <= 1
        assert hi >= prev
        prev = hi


# ── sets ──────────────────────────────────────────────────────────


def _item(i: int, lane: str = "merge", gold: Any = None) -> MeshItem:
    return MeshItem(
        item_id=f"i-{i}", lane=lane, prompt="p", gold=gold or {"same": True}
    )


def test_dump_load_roundtrip(tmp_path: Any) -> None:
    p = tmp_path / "set.jsonl"
    items = [_item(1), _item(2, lane="link-add", gold={"edges": []})]
    dump_set(items, {"lane": "merge", "version": "v1"}, p)
    es = load_set(p)
    assert es.lane == "merge"
    assert [i.item_id for i in es.items] == ["i-1", "i-2"]
    assert es.meta["n_items"] == 2


def test_load_rejects_truncated_and_unknown_lane(tmp_path: Any) -> None:
    p = tmp_path / "bad.jsonl"
    p.write_text(
        '{"_meta": {"lane": "merge", "n_items": 5}}\n'
        '{"item_id": "x", "lane": "merge", "prompt": "", "gold": {}}\n',
        encoding="utf-8",
    )
    with pytest.raises(BadInput, match="declares 5"):
        load_set(p)
    p.write_text(
        '{"_meta": {"lane": "merge"}}\n'
        '{"item_id": "x", "lane": "vibes", "prompt": "", "gold": {}}\n',
        encoding="utf-8",
    )
    with pytest.raises(BadInput, match="unknown lane"):
        load_set(p)


def test_split_holdout_strips_gold_into_reference(tmp_path: Any) -> None:
    items = [_item(i) for i in range(10)]
    graded, holdout = split_holdout(items, 3, seed=7)
    assert len(holdout) == 3 and len(graded) == 7
    assert all(h.gold is None and h.holdout for h in holdout)
    assert all(h.meta["reference_answer"] == {"same": True} for h in holdout)
    # Deterministic for a given seed.
    _, holdout2 = split_holdout(items, 3, seed=7)
    assert [h.item_id for h in holdout] == [h.item_id for h in holdout2]


# ── scorers ───────────────────────────────────────────────────────


def test_merge_scorer() -> None:
    item = _item(1, gold={"same": True, "survivor": "a"})
    assert score_item(item, '{"same": true, "survivor": "a"}').score == 1.0
    miss = score_item(item, '{"same": false}')
    assert miss.score == 0.0 and miss.detail["missed_merge"]
    item_neg = _item(1, gold={"same": False})
    fm = score_item(item_neg, '{"same": true, "survivor": "a"}')
    assert fm.score == 0.0 and fm.detail["false_merge"]
    assert score_item(item, "no json at all").score == 0.0


def test_link_add_scorer_f1_and_no_edge() -> None:
    item = MeshItem(
        item_id="l1",
        lane="link-add",
        prompt="",
        gold={
            "edges": [{"dst_ref_id": 5, "relation": "corroborates"}],
            "no_edge": [9],
        },
        meta={"candidates": [5, 6, 9]},
    )
    good = score_item(item, '[{"dst": 5, "relation": "corroborates"}]')
    assert good.score == 1.0
    wrong_rel = score_item(item, '[{"dst": 5, "relation": "disputes"}]')
    assert wrong_rel.score == 0.0 and wrong_rel.detail["fn"] == 1
    neg = score_item(item, '[{"dst": 9, "relation": "corroborates"}]')
    assert neg.score == 0.0 and neg.detail["no_edge_proposed"] == 1
    extra = score_item(
        item,
        '[{"dst": 5, "relation": "corroborates"}, {"dst": 6, "relation": "disputes"}]',
    )
    assert 0 < extra.score < 1.0 and extra.detail["precision"] == 0.5


def test_finding_scorer_coverage() -> None:
    item = MeshItem(
        item_id="f1",
        lane="finding",
        prompt="",
        gold={
            "claims": [
                {
                    "hub_ref_id": 1,
                    "title": "NH3 yield rate reaches 519 ug",
                    "weight": 1.0,
                },
                {
                    "hub_ref_id": 2,
                    "title": "Catalyst X doubles selectivity",
                    "weight": 2.0,
                },
            ]
        },
    )
    full = score_item(
        item,
        '["NH3 yield rate reaches 519 ug per hour", "Catalyst X doubles selectivity"]',
    )
    assert full.score == 1.0
    half = score_item(item, '["Catalyst X doubles selectivity"]')
    assert 0.5 < half.score < 0.8  # the weight-2 claim matched, weight-1 missed
    assert score_item(item, "[]").score == 0.0


def test_summarise_scorer_uses_production_gate() -> None:
    item = MeshItem(
        item_id="s1",
        lane="summarise",
        prompt="",
        gold={
            "chunk_text": "The yield was 42 percent at pH 7.",
            "nonprose": False,
            "incumbent": "(fine)",
        },
    )
    # An invented number must zero the task (the summariser's own rule).
    bad = score_item(item, "The yield was 99 percent.")
    assert bad.score == 0.0


def _cat_item() -> MeshItem:
    mentions = [
        {
            "anchor": {
                "ref_id": 1,
                "start": 10,
                "end": 15,
                "source_kind": "ref_text",
                "scheme": 1,
            },
            "kind": "value",
            "literal": "3.5",
            "raw_unit": "V",
            "context": "at 3.5 V vs RHE",
            "marker": None,
        }
    ]
    return MeshItem(
        item_id="c1",
        lane="categorise",
        prompt="prompt",
        gold={
            "mentions": [
                {
                    "index": 0,
                    "gold_key": "applied-potential",
                    "gold_measurand": "applied potential",
                }
            ]
        },
        meta={"mentions": mentions, "ref_id": 1, "sentence": "s"},
    )


def test_categorise_scorer_folded_match() -> None:
    item = _cat_item()
    reply = json.dumps(
        [
            {
                "index": 0,
                "measurand": "applied potential",
                "dimension_text": "V",
                "subject_label": "x",
            }
        ]
    )
    s = score_item(item, reply, ctx={"aliases": {"species": {}, "phrases": {}}})
    assert s.score == 1.0
    wrong = score_item(
        item,
        reply.replace("applied potential", "Faradaic efficiency"),
        ctx={"aliases": {"species": {}, "phrases": {}}},
    )
    assert wrong.score == 0.0
