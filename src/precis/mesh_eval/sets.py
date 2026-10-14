"""Frozen eval-set model — one JSONL file per lane per version.

A set file is a header line then one JSON object per item::

    {"_meta": {"lane": "categorise", "version": "categorise-v1",
               "built_at": "2026-10-14T…Z", "source": "…", "seed": "…",
               "reference": {"frontier_retest": 0.794},
               "scorer_ctx": {"aliases": {"species": …, "phrases": …}}}}
    {"item_id": "cat-259507", "lane": "categorise", "prompt": "…",
     "messages": null, "gold": {…}, "holdout": false, "meta": {…}}

``gold`` is the frozen label; its shape is per lane (see
:mod:`.scorers`). ``holdout: true`` items carry ``gold: null`` — the
80-item human holdout (20 per lane) is *selected and stored* here and
labelled by the operator later (``local-mesh-upkeep.md`` §1, Reto
2026-10-03): never tune a prompt on it. A holdout item keeps
``meta.reference_answer`` (the frontier's own answer) so "no more errors
than the frontier" is computable once human labels exist.

``meta.reference`` on the header holds the numbers a bar read needs:
``frontier_retest`` (the frontier's own test-retest agreement on this
set — the auto-apply agreement bar) and ``frontier_cost_per_item_usd``
when known.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.errors import BadInput

#: The slice-0 lane vocabulary — the four spec task sets plus summarise,
#: which reuses the production summariser path.
LANES: tuple[str, ...] = (
    "categorise",
    "link-add",
    "finding",
    "merge",
    "summarise",
)


@dataclass(frozen=True, slots=True)
class MeshItem:
    """One eval item: a prompt (or verbatim message list), its gold, and
    provenance."""

    item_id: str
    lane: str
    prompt: str
    gold: dict[str, Any] | None
    messages: list[dict[str, str]] | None = None
    holdout: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class EvalSet:
    """A loaded set: the header's meta plus the item list."""

    meta: dict[str, Any]
    items: list[MeshItem]

    @property
    def lane(self) -> str:
        return str(self.meta.get("lane") or "")

    @property
    def scorer_ctx(self) -> dict[str, Any]:
        ctx = self.meta.get("scorer_ctx")
        return dict(ctx) if isinstance(ctx, dict) else {}

    @property
    def reference(self) -> dict[str, Any]:
        ref = self.meta.get("reference")
        return dict(ref) if isinstance(ref, dict) else {}

    def graded(self) -> list[MeshItem]:
        """The scored items — the holdout is never part of a run."""
        return [i for i in self.items if not i.holdout]

    def reserved(self) -> list[MeshItem]:
        return [i for i in self.items if i.holdout]


def dump_set(items: list[MeshItem], meta: dict[str, Any], path: str | Path) -> Path:
    """Write ``meta`` + ``items`` as JSONL (UTF-8, one object per line).

    The header records ``n_items`` itself so a truncated write is
    detectable on load.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = {**meta, "set": "mesh-eval", "n_items": len(items)}
    with p.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for it in items:
            fh.write(json.dumps(_item_to_json(it), ensure_ascii=False) + "\n")
    return p


def _item_to_json(it: MeshItem) -> dict[str, Any]:
    return {
        "item_id": it.item_id,
        "lane": it.lane,
        "prompt": it.prompt,
        "messages": it.messages,
        "gold": it.gold,
        "holdout": it.holdout,
        "meta": it.meta,
        "max_tokens": it.max_tokens,
    }


def load_set(path: str | Path) -> EvalSet:
    """Load a JSONL set; validate the header and item lanes."""
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BadInput(f"mesh eval: cannot read set {p}: {exc}") from exc
    meta: dict[str, Any] = {}
    items: list[MeshItem] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BadInput(f"mesh eval: {p} line {i + 1}: {exc}") from exc
        if not isinstance(row, dict):
            raise BadInput(f"mesh eval: {p} line {i + 1} is not an object")
        if "_meta" in row:
            meta = dict(row["_meta"])
            continue
        lane = str(row.get("lane") or "")
        if lane not in LANES:
            raise BadInput(
                f"mesh eval: {p} line {i + 1} has unknown lane {lane!r}",
                options=list(LANES),
            )
        gold = row.get("gold")
        items.append(
            MeshItem(
                item_id=str(row.get("item_id") or f"item-{i}"),
                lane=lane,
                prompt=str(row.get("prompt") or ""),
                messages=row.get("messages"),
                gold=dict(gold) if isinstance(gold, dict) else None,
                holdout=bool(row.get("holdout", False)),
                meta=dict(row.get("meta") or {}),
                max_tokens=(int(row["max_tokens"]) if row.get("max_tokens") else None),
            )
        )
    if not meta:
        raise BadInput(f"mesh eval: {p} has no _meta header line")
    declared = meta.get("n_items")
    if declared is not None and int(declared) != len(items):
        raise BadInput(
            f"mesh eval: {p} declares {declared} items but holds {len(items)}"
        )
    return EvalSet(meta=meta, items=items)


def split_holdout(
    items: list[MeshItem], n_holdout: int, *, seed: int = 0
) -> tuple[list[MeshItem], list[MeshItem]]:
    """Deterministically reserve ``n_holdout`` items for the human holdout.

    Selection is a seeded shuffle over ``item_id`` order, so two builders
    run with the same seed and pool make the same cut. Holdout items are
    returned with ``gold`` stripped to ``None`` and the label moved into
    ``meta.reference_answer`` — the operator's later label is what counts;
    the frontier answer stays available for the "no more errors than the
    frontier" comparison.
    """
    import random
    from dataclasses import replace

    if n_holdout <= 0:
        return items, []
    order = list(range(len(items)))
    random.Random(seed).shuffle(order)
    holdout_idx = set(order[:n_holdout])
    graded: list[MeshItem] = []
    holdout: list[MeshItem] = []
    for i, it in enumerate(items):
        if i in holdout_idx:
            holdout.append(
                replace(
                    it,
                    gold=None,
                    holdout=True,
                    meta={**it.meta, "reference_answer": it.gold},
                )
            )
        else:
            graded.append(it)
    return graded, holdout


__all__ = ["LANES", "EvalSet", "MeshItem", "dump_set", "load_set", "split_holdout"]
