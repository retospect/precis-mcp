"""source-backfill 8a.2 — the visibility-scoped link-rollup logic (pure).

The structural stance's *map*: per section, roll up *all* its outbound links and
summarise where they go, at a granularity that **follows the target's
visibility**. A link resolves to the chunk the reader actually *sees* it as — the
nearest visible node on the path from the target up to the root:

* target para **open** → the link points **right at it**;
* target para **collapsed** but its enclosing section **open** → a
  **section-level aggregate** (``N links between §2 and §3``);
* the whole branch collapsed → the **root** (coarsest) node it rolls up under.

Cross-ref targets (a link into another draft / a paper) don't resolve to a chunk
here — we hold no tree for them — so they carry their ``dst_ref_id`` out: held
sources get named individually by the renderer, the rest fold into a per-section
long tail (``30 links → 8 other papers``).

**This module is deliberately store-free** — it operates on plain ``int`` maps
(``parent_of``, ``demand``) and edge tuples, so it unit-tests against a
hand-built tree with no DB. The composer overlay (8a.3) supplies the maps from
``reading_order`` + the assembled ``demand`` and formats the result. ``demand``
values are ``Extent`` (an ``IntEnum``); visibility is just ``> 0``, so a test may
pass bare ints.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChunkEdge:
    """A chunk-level outbound edge reduced to what the rollup needs.

    ``src_chunk_id`` / ``dst_chunk_id`` are ``None`` for a ref-level endpoint
    (the whole-doc link, not a chunk); ``dst_ref_id`` is always the target ref.
    Built from a :class:`~precis.store.types.Link` (which now carries the raw
    chunk-id endpoints, source-backfill 8a.1)."""

    src_chunk_id: int | None
    dst_chunk_id: int | None
    dst_ref_id: int
    relation: str


@dataclass(frozen=True, slots=True)
class NamedTarget:
    """One named (src-section → target) aggregate. Exactly one of ``dst_chunk``
    (an in-doc visible chunk id) / ``dst_ref`` (a cross-ref target ref id) is
    set; ``src`` is the visible src-section chunk id."""

    src: int
    dst_chunk: int | None
    dst_ref: int | None
    count: int


@dataclass(frozen=True, slots=True)
class TailBucket:
    """The per-section long tail: the cross-ref targets we didn't name
    individually (unheld sources, or overflow past ``top_k``), folded to a
    count of links over a count of distinct targets."""

    src: int
    links: int
    targets: int


@dataclass(frozen=True, slots=True)
class LinkRollup:
    named: tuple[NamedTarget, ...]
    tail: tuple[TailBucket, ...]

    def __bool__(self) -> bool:
        return bool(self.named or self.tail)


def _is_visible(demand: Mapping[int, object], cid: int) -> bool:
    # Extent is an IntEnum (NONE == 0); a chunk renders iff its demand > NONE.
    # Absent → NONE → collapsed. ``> 0`` covers both the enum and bare-int tests.
    return demand.get(cid, 0) > 0  # type: ignore[operator]


def coarsest_visible_ancestor(
    chunk_id: int | None,
    *,
    parent_of: Mapping[int, int | None],
    demand: Mapping[int, object],
) -> int | None:
    """Resolve a target chunk to the chunk the reader actually *sees* it as.

    Walk from ``chunk_id`` up the ``parent_of`` chain and return the first node
    (self included) that is visible (``demand`` extent > NONE) — the visible
    representative. If the target is open, that's the target; if it's collapsed
    under an open section, that's the section. If nothing up to the root is
    visible (the whole branch is collapsed), fall back to the root ancestor —
    the coarsest node, where a fully-collapsed run rolls up. Returns ``None``
    when ``chunk_id`` is ``None`` or not in this doc's tree (a cross-ref /
    cross-doc target — resolved to a ref by the caller).
    """
    if chunk_id is None or chunk_id not in parent_of:
        return None
    node: int | None = chunk_id
    last = chunk_id
    while node is not None:
        if _is_visible(demand, node):
            return node
        last = node
        node = parent_of.get(node)
    return last  # whole branch collapsed → coarsest (root) node


def rollup_edges(
    edges: Iterable[ChunkEdge],
    *,
    this_ref_id: int,
    parent_of: Mapping[int, int | None],
    demand: Mapping[int, object],
    held_ref_ids: Container[int],
    top_k: int = 8,
) -> LinkRollup:
    """Group outbound ``edges`` into per-section aggregates against the assembled
    visibility.

    Both endpoints resolve to their visible representative:

    * **in-doc target** (``dst_ref_id == this_ref_id`` with a chunk) → the
      target's visible ancestor is named; a link that collapses onto its own
      section (``dst == src``) is dropped as noise;
    * **cross-ref target** → named individually if held (``dst_ref_id in
      held_ref_ids``), else folded into the section's long tail.

    ``top_k`` bounds the named list *per section*: the lowest-count targets past
    it fold into the tail too, so a section with hundreds of edges renders a
    handful of named ones plus ``… → N more``. A ref-level src edge (no
    ``src_chunk_id``) isn't section-attributable and is skipped in v1.
    """
    named: dict[tuple[int, tuple[str, int]], int] = defaultdict(int)
    tail_links: dict[int, int] = defaultdict(int)
    tail_targets: dict[int, set[tuple[str, int]]] = defaultdict(set)

    for e in edges:
        src = coarsest_visible_ancestor(
            e.src_chunk_id, parent_of=parent_of, demand=demand
        )
        if src is None:
            continue  # ref-level / out-of-tree src: not section-attributable
        in_doc = e.dst_ref_id == this_ref_id and e.dst_chunk_id is not None
        if in_doc:
            dst = coarsest_visible_ancestor(
                e.dst_chunk_id, parent_of=parent_of, demand=demand
            )
            if dst is None or dst == src:
                continue  # unresolvable, or a self-loop after collapse — noise
            named[(src, ("c", dst))] += 1
        elif e.dst_ref_id == this_ref_id:
            continue  # a whole-doc ref-level self link — noise
        elif e.dst_ref_id in held_ref_ids:
            named[(src, ("r", e.dst_ref_id))] += 1
        else:
            tail_links[src] += 1
            tail_targets[src].add(("r", e.dst_ref_id))

    # Bound the named list per section; overflow folds into the tail.
    by_src: dict[int, list[tuple[tuple[str, int], int]]] = defaultdict(list)
    for (src, dkey), c in named.items():
        by_src[src].append((dkey, c))

    named_out: list[NamedTarget] = []
    for src, items in by_src.items():
        items.sort(key=lambda t: (-t[1], t[0]))  # count desc, then stable by key
        for dkey, c in items[:top_k]:
            kind, val = dkey
            named_out.append(
                NamedTarget(
                    src=src,
                    dst_chunk=val if kind == "c" else None,
                    dst_ref=val if kind == "r" else None,
                    count=c,
                )
            )
        for dkey, c in items[top_k:]:
            tail_links[src] += c
            tail_targets[src].add(dkey)

    named_out.sort(key=lambda n: (n.src, -n.count, n.dst_chunk or 0, n.dst_ref or 0))
    tail_out = [
        TailBucket(src=src, links=tail_links[src], targets=len(tail_targets[src]))
        for src in sorted(tail_links)
        if tail_links[src]
    ]
    return LinkRollup(named=tuple(named_out), tail=tuple(tail_out))
