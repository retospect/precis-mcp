"""kg-mesh-loop slice 0 gold set — schema and handle form.

The committed Pd-hydride retrieval gold
(``scripts/llm_eval/gold_set/retrieval_qu164903.jsonl``) must load through
the retrieval harness and carry well-formed extra fields: every evidence
ref is a refs-backed handle and every gold chunk is among the evidence.
Form only — whether the refs exist on prod is the harness run's job.
"""

from __future__ import annotations

import json
from pathlib import Path

from precis.mesh_eval.retrieval import load_gold
from precis.utils import handle_registry

_GOLD = (
    Path(__file__).parent.parent / "scripts/llm_eval/gold_set/retrieval_qu164903.jsonl"
)
_TYPES = {"lookup", "cross-paper", "stance", "grounding"}
_STATUSES = {"answered", "open"}


def _rows() -> list[dict[str, object]]:
    lines = _GOLD.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_gold_loads_through_harness() -> None:
    gs = load_gold(_GOLD)
    assert gs.meta["quest"] == "qu164903"
    assert gs.meta["n_items"] == len(gs.records) >= 1
    assert all(r.quest == "qu164903" for r in gs.records)


def test_gold_extra_fields_well_formed() -> None:
    records = [r for r in _rows() if "_meta" not in r]
    ids = [r["id"] for r in records]
    assert len(ids) == len(set(ids))
    for r in records:
        rid = r["id"]
        assert r["type"] in _TYPES, rid
        assert r["status"] in _STATUSES, rid
        assert isinstance(r["answer"], str) and r["answer"].strip(), rid
        evidence = r["evidence"]
        assert isinstance(evidence, list) and evidence, rid
        kinds = []
        for h in evidence:
            parsed = handle_registry.parse(h)
            assert parsed is not None, (rid, h)
            kinds.append(parsed[0])
        assert "finding" in kinds, rid
        gold = r["gold_chunks"]
        assert isinstance(gold, list)
        assert set(gold) <= set(evidence), rid
