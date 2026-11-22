"""The batch eye — ``get(kind=K, id=[...], extent=<rung>)`` as ONE combined
render, not one eye block per id (gr659493 comment 7, td760288).

The owner's ruling: "the fisheye should be combined if it is meaningful (ie,
5 chunks from one paper will show up together, linked resources only shown
once (if a, b, c each links to d, d is only shown once)". So this module
builds the combined view from the renderer's collected structures
(:mod:`precis.utils.eye_render`), never by merging rendered text:

- **Focus blocks, grouped by parent.** Requested handles on one ref (several
  ``pc`` chunks of one paper, a quest and its log chunks) form one group:
  one head, then the doc split opened at every requested chunk at once
  (:func:`~precis.utils.eye_render._fisheye_split` takes several ords) or
  the note body per handle. Groups appear at their first id's position;
  a missing or deleted id is a one-line entry in that same order.
- **One merged first hop.** Every group's ring (:func:`_first_hop`) is
  pooled: a neighbour reached from several requested refs renders once,
  under the first group/label it reads with, and its row ends ``← a, b,
  c`` naming them (a requester reaching it through another relation is
  named ``c (cites)``). Parallel edges fold like a single eye folds them.
  A label's cap is the single eye's cap per focus, and every focus keeps
  the rows its own eye would show; an overflow names the per-focus
  single-id ``q='<label>'`` call.
- **Bodies excerpted** to :data:`BATCH_BODY_CAP`, each ending in the call
  that reads it whole, so the merged ring survives the transport budget.
- **One second hop** over the merged first hop, every requested ref
  excluded as "the focus"; ``+recall`` stays per requested ref (one
  section each, the focus named in its heading) — similarity is not an
  edge, so there is nothing to merge.

A handle the shared note/doc render does not own — a draft/plan section
(the reading-order tree eye), a skill, a fetch-cache record (whose body
notice decides what renders) — keeps its whole single eye as its own entry.
``q=`` (expand one group of one focus) has no batch meaning; the caller
(:meth:`precis.protocol.Handler.eye`) refuses it before reaching here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from precis._result_budget import size_cell
from precis.utils import handle_registry
from precis.utils.entity_courtesy import safe_entity_states
from precis.utils.eye_render import (
    _CACHE_EYE_KINDS,
    _CONSIDERED_ROW_CAP,
    _DOC_KINDS,
    _NEIGHBOR_GROUP_CAP,
    _SKILL_HANDLE_PREFIX,
    _TREE_KINDS,
    TODO_LONE_LEAF,
    RingFilter,
    _doc_block,
    _entity_handles_for_refs,
    _entity_row_chips,
    _first_hop,
    _live_note_ref,
    _more_hint_factory,
    _note_block,
    _recall,
    _render_first_hop,
    _RingNeighbour,
    _row_chips,
    _second_hop,
    parse_extent,
    render_eye,
    ring_row_key,
)
from precis.utils.refeye import RING_GROUP_KIND_RANK, RING_GROUPS
from precis.workers.working_set import Extent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from precis.store.store import Store

#: Chars of each focus body (note body, verbatim chunk center) a batch eye
#: shows, ending in the cut line that names the full read. The transport
#: budget fits a response by cutting its tail, and every focus block
#: renders before the merged ring — at the single eye's 4000 chars, seven
#: long bodies would push the ring out entirely.
BATCH_BODY_CAP = 600

#: Requested handles a batch's second-hop heading lists in its call.
_SECOND_HOP_NAMED_FOCI = 3


@dataclass(frozen=True, slots=True)
class EyeFocus:
    """One requested id of a batch eye, resolved by its kind's handler
    (:meth:`precis.protocol.Handler.eye_focus`): the handle the renderer
    focuses, and the lead the kind prints above its eye (finding's
    claim-hub posture; ``""`` for most kinds)."""

    handle: str
    header: str = ""


@dataclass(slots=True)
class _Group:
    """The requested handles that share one ref."""

    kind: str
    ref: Any
    foci: list[EyeFocus] = field(default_factory=list)
    chunk_ords: list[int] = field(default_factory=list)
    whole: bool = False

    @property
    def ref_id(self) -> int:
        return int(self.ref.id)

    @property
    def handle(self) -> str:
        return handle_registry.format_handle(self.kind, self.ref_id)


RingKey = tuple[str, str]


def render_eye_batch(
    store: Store,
    items: Sequence[EyeFocus | str],
    extent: Extent | str | int,
    *,
    ring: RingFilter | dict[str, Any] | None = None,
) -> str:
    """The combined eye on ``items`` in caller order — an :class:`EyeFocus`
    per live id, a ready one-line entry (``"fi12  not found"``) per dead
    one. Raises ``ValueError`` like :func:`render_eye` (a bad rung, a bad
    ring filter, a handle that stopped resolving)."""
    ext, recall = parse_extent(extent)
    filt = RingFilter.parse(ring)
    slots: list[_Group | str] = []
    groups: dict[int, _Group] = {}
    for item in items:
        if isinstance(item, str):
            slots.append(item)
            continue
        target = _target(store, item.handle)
        if target is None:
            slots.append(
                item.header + render_eye(store, item.handle, extent, ring=ring)
            )
            continue
        kind, ref, ord_ = target
        group = groups.get(int(ref.id))
        if group is None:
            group = groups[int(ref.id)] = _Group(kind, ref)
            slots.append(group)
        if any(f.handle == item.handle for f in group.foci):
            continue
        group.foci.append(item)
        if ord_ is None:
            group.whole = True
        else:
            group.chunk_ords.append(ord_)
    sections = [
        slot if isinstance(slot, str) else _group_block(store, slot, ext, recall)
        for slot in slots
    ]
    live = list(groups.values())
    if live and ext >= Extent.HOP1:
        sections.extend(_merged_rings(store, live, ext, ring=filt))
    if recall:
        # A doc eye at kwd/summary is its head line alone, rings and
        # recall included (``_render_doc_eye``'s early return).
        sections.extend(
            _recall(store, g.ref_id, g.kind, ring=filt, label=f" · {g.handle}")
            for g in live
            if not (g.kind in _DOC_KINDS and ext <= Extent.TOC)
        )
    return "\n\n".join(s for s in sections if s)


def _target(store: Store, handle: str) -> tuple[str, Any, int | None] | None:
    """``(kind, live ref, chunk ord or None)`` for a handle the combined
    render owns; ``None`` for one that keeps its own single eye."""
    if handle.startswith(_SKILL_HANDLE_PREFIX):
        return None
    parsed = handle_registry.parse(handle)
    if parsed is None:
        raise ValueError(f"eye: unresolvable handle {handle!r}")
    kind, is_chunk, _pk = parsed
    if kind in _TREE_KINDS or (kind in _CACHE_EYE_KINDS and not is_chunk):
        return None
    if kind not in _DOC_KINDS:
        return kind, _live_note_ref(store, handle, kind), None
    rh = store.resolve_handle(handle)
    if rh is None:
        raise ValueError(f"eye: no live {kind} for {handle!r}")
    ref_id = int(rh.ref_id)
    ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    if ref is None or getattr(ref, "retired_at", None) is not None:
        raise ValueError(f"eye: no live {kind} ref for {handle!r}")
    ord_ = int(rh.chunk_ord) if is_chunk and rh.chunk_ord is not None else None
    return kind, ref, ord_


def _group_block(store: Store, group: _Group, ext: Extent, recall: bool) -> str:
    """One group's focus block: the kind leads (deduped), the head once,
    then the doc split at every requested chunk or the note body per
    handle; a todo's ``— tree —`` ring rides its own block."""
    header = "".join(dict.fromkeys(f.header for f in group.foci))
    if group.kind in _DOC_KINDS:
        block = _doc_block(
            store,
            group.ref,
            group.kind,
            ext,
            chunk_ords=group.chunk_ords,
            whole=group.whole,
            body_cap=BATCH_BODY_CAP,
        )
        return header + block
    block, _body = _note_block(
        store,
        group.ref,
        group.kind,
        ext,
        handles=[f.handle for f in group.foci],
        recall=recall,
        body_cap=BATCH_BODY_CAP,
    )
    if group.kind == "todo" and ext >= Extent.HOP1:
        from precis.todo.work_view import render_tree_ring

        block += "\n\n" + (render_tree_ring(store, group.ref_id) or TODO_LONE_LEAF)
    return header + block


def _fold(a: _RingNeighbour, b: _RingNeighbour) -> _RingNeighbour:
    """Two foci's rows on one neighbour as one row: edges summed, passages,
    focus-side ``via`` chunks and whys unioned in first-seen order."""

    def uniq(xs: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(dict.fromkeys(xs))

    return replace(
        a,
        edges=a.edges + b.edges,
        passages=uniq(a.passages + b.passages),
        via=uniq(a.via + b.via),
        suspect=a.suspect or b.suspect,
        reasons=uniq(a.reasons + b.reasons),
        verdict=a.verdict or b.verdict,
    )


def _tier(
    row: tuple[int, int | None],
    kept: set[tuple[int, int | None]],
    reach: dict[tuple[int, int | None], set[str]],
) -> int:
    """A merged label's row order: kept rows shared by several foci, then
    the other kept rows (some focus's single eye shows them), then the rest."""
    if row not in kept:
        return 2
    return 0 if len(reach[row]) > 1 else 1


def _merged_rings(
    store: Store, groups: list[_Group], ext: Extent, *, ring: RingFilter | None
) -> list[str]:
    """The ``— linked (1 hop) —`` section over every group, each neighbour
    once with the requesters named, then the second hop at ``fisheye+2hop``.

    Each label's cap is the single eye's cap times the number of groups,
    and every group keeps the rows its own single eye would show there
    (a row shared by several groups is counted once and listed first), so
    pooling never drops a focus's rows behind another's. An overflow line
    names, per focus with rows still hidden, the single-id call that lists
    them (the batch itself refuses ``q=``)."""
    entries: list[tuple[RingKey, _RingNeighbour, str]] = []
    refs: dict[int, Any] = {}
    own: dict[str, dict[RingKey, list[_RingNeighbour]]] = {}
    for g in groups:
        first, got = _first_hop(store, g.ref_id, ring=ring)
        refs.update(got)
        own[g.handle] = first
        entries.extend((key, n, g.handle) for key, ns in first.items() for n in ns)
    hops = 2 if ext >= Extent.HOP2 else 1
    if not entries:
        if hops == 1 and any(g.kind == "todo" for g in groups):
            # A todo's ``— tree —`` ring already rides its block; the single
            # eye prints no empty-ring line beside it either.
            return []
        return [f"no structural neighbours at {hops} hop{'s' if hops == 2 else ''}"]
    order = {group: i for i, group in enumerate(RING_GROUPS)}

    def rank(key: RingKey) -> tuple[int, str]:
        return order[key[0]], key[1]

    # Each neighbour renders under the first group/label (render order) any
    # requester reaches it by; a requester on another label is named with it.
    home: dict[tuple[int, int | None], RingKey] = {}
    for key, n, _src in entries:
        row = ring_row_key(n)
        if row not in home or rank(key) < rank(home[row]):
            home[row] = key
    merged: dict[RingKey, dict[tuple[int, int | None], _RingNeighbour]] = {}
    names: dict[tuple[int, int | None], list[str]] = {}
    reach: dict[tuple[int, int | None], set[str]] = {}
    for key, n, src in entries:
        row = ring_row_key(n)
        reach.setdefault(row, set()).add(src)
        at_home = key == home[row]
        name = src if at_home else f"{src} ({key[1]})"
        named = names.setdefault(row, [])
        if name not in named:
            named.append(name)
        if at_home:
            bucket = merged.setdefault(key, {})
            bucket[row] = _fold(bucket[row], n) if row in bucket else n
    hop1: dict[RingKey, list[_RingNeighbour]] = {}
    hints: dict[int, str] = {}
    factories: dict[str, Any] = {}
    for key, bucket in merged.items():
        group, label = key
        cap = _CONSIDERED_ROW_CAP if group == "Considered" else _NEIGHBOR_GROUP_CAP
        # The rows each focus's own single eye shows under this label.
        kept = {
            row
            for ns in (h1.get(key, []) for h1 in own.values())
            for row in map(ring_row_key, ns[:cap])
            if home[row] == key
        }
        kind_rank = RING_GROUP_KIND_RANK.get(group) or ()
        pri = {k: i for i, k in enumerate(kind_rank)}
        # Stable on pooled (caller) order within a tier and kind rank.
        ordered = sorted(
            (
                _tier(ring_row_key(n), kept, reach),
                pri.get(str(getattr(refs[n.ref_id], "kind", "")), len(pri)),
                i,
                n,
            )
            for i, n in enumerate(bucket.values())
        )
        ns = [n for _tier_, _kind, _i, n in ordered]
        hop1[key] = ns
        shown = {ring_row_key(n) for n in ns[: cap * len(groups)]}
        calls: list[str] = []
        for g in groups:
            mine = own[g.handle].get(key, [])
            if all(
                ring_row_key(n) in shown for n in mine if home[ring_row_key(n)] == key
            ):
                continue
            if g.handle not in factories:
                factories[g.handle] = _more_hint_factory(store, g.ref_id, own[g.handle])
            hint = factories[g.handle]
            calls.append(
                hint(label, mine)
                if hint is not None
                else f" · get(kind={g.kind!r}, id={g.handle!r}, "
                f"extent='fisheye+1hop', q={label!r})"
            )
        # ``_render_first_hop`` hands back the very list it overflows on.
        hints[id(ns)] = "".join(calls)
    printed = {
        n.ref_id: refs[n.ref_id] for ns in hop1.values() for n in ns if n.ref_id in refs
    }
    chips = _row_chips(store, printed, ring=ring)
    states = safe_entity_states(store, list(_entity_handles_for_refs(refs).values()))
    _entity_row_chips(store, refs, chips, states)
    sizes = {
        rid: size_cell(n)
        for rid, n in store.chunks.stored_body_byte_counts(list(printed)).items()
    }
    kinds = {g.kind for g in groups}
    out = [
        _render_first_hop(
            hop1,
            refs,
            chips=chips,
            sizes=sizes,
            via_kind=next(iter(kinds)) if len(kinds) == 1 else None,
            sources={row: ", ".join(named) for row, named in names.items()},
            cap_scale=len(groups),
            more_hint=lambda _label, ns: hints.get(id(ns), ""),
        )
    ]
    if ext >= Extent.HOP2:
        named_foci = ", ".join(g.handle for g in groups[:_SECOND_HOP_NAMED_FOCI])
        if len(groups) > _SECOND_HOP_NAMED_FOCI:
            named_foci += ", …"
        second = _second_hop(
            store,
            groups[0].ref_id,
            hop1,
            ring=ring,
            states=states,
            foci={g.ref_id for g in groups},
            expand_how=(
                f"get(id=<one of {named_foci}>, extent='fisheye+2hop', "
                "q='<kind>:<label>')"
            ),
        )
        out.append(second or "no structural neighbours at 2 hops")
    return out
