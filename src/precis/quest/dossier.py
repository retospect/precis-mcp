"""Dossier — the living research synthesis a *process* owns.

Slice 4a of the quest layer (``quest-layer`` (git-only) §Two memories),
generalized per dossier-owned-by-process:
a dossier belongs to a **process, never an artifact**. A quest is the process
that owns one today, but the owner is now **any ref** (``owner_id``) — a
standing topic review or a paper-writing pipeline can own a dossier by the same
rule, and a "paper" is just a render/export of a process's dossier. The owner
coupling lived entirely in this module's Python (the ``dossier-of`` /
``has-dossier`` relation is already owner-agnostic — migration 0067, no kind
constraint), so widening it is migration-free.

An owning process keeps *two* records: the append-only ``quest_log`` LOGBOOK
(episodic — what happened, when, immutable; :mod:`precis.quest.logbook`) and the
DOSSIER — a ``draft`` the owner holds via ``dossier-of`` (semantic — the current
understanding, best leads, what's ruled out, open questions), **rewritten every
research cycle**. The dossier doubles as the autonomous loop's *rolling
context*: each tick reads the compact dossier rather than replaying the whole
logbook, so context stays bounded.

Dossier-owned-by-process splits the dossier body into a **narrative**
paragraph (the whole-rewritten prose synthesis, ``edit_text``'d in place each
tick, stable handle, ``prev_text`` history for free, and — since the
attempt-tree ledger (dossier-hygiene design — quest package docstring) — held to a
code-enforced growth ratchet, not a fixed cap: a rewrite may only outgrow the
previous narrative by more than ~15%+50 words when the tick shows visible
progress, see :mod:`precis.quest.narrative_budget` and ``tick.py``'s
``_apply_narrative_gate``) plus a **pinned ledger** — the *strategic* attempt
tree, one node per tried/abandoned/open *direction* (a whole direction, not a
single ruled-out structure — that per-candidate ledger already lives on
``structure`` tags, see ``tick.py``'s ``_ruled_out_handles``), children as
refinements/variants of their parent, each carrying a status (``open`` /
``active`` / ``tried`` / ``ruled-out``) — so the loop can't silently lose its
own trail on a rewrite that drops a rule-out from the free prose (the
autocatpath dead-3-days spin), and so a whole abandoned branch (try a, then
b, then c-with-x and c-with-y) reads as a subtree, not an ambiguous flat
list.

The ledger's storage is **real chunks, not a markdown blob**: a container
chunk (``meta.pinned='ledger'``, set via ``patch_chunk_meta`` — the
persona-threads plan-marker precedent, no new chunk_kind, no migration) holds
no content of its own; each tree node is its own CHILD chunk
(``parent_chunk_id`` nesting, :meth:`DraftStore.reading_order`'s DFS order = tree
order), stamped ``meta.pinned='ledger-node'`` (so the narrative-body filters
in :func:`rewrite_dossier`/:func:`read_narrative` — which already exclude
ANY truthy ``meta.pinned`` — keep every node out of the rewritable prose for
free) and carrying its status as a closed-axis chunk tag
(``ATTEMPT:<status>``, :data:`precis.store.types._CLOSED_VOCAB`'s
``"ATTEMPT"`` entry). This replaced an earlier design (one ``## Attempts``
markdown blob in the container chunk, model-authored bullets parsed back with
a hand-rolled indentation grammar): it rendered as literal markdown in the
smartdraft web view, and a node's only identity was its exact text — an
op that didn't match dropped silently. :func:`add_attempt` /
:func:`mark_attempt` are the only mutators (:func:`append_ledger_entry` is
the pre-tree three-section entry point, kept for its existing callers — it
now adds a depth-0 tree node under the mapped status); every OTHER read of
the ledger still goes through :class:`AttemptNode` — the in-memory forest is
unchanged by the storage move, so the tree semantics below are unaffected.
Ruling out a node is a *stored*, per-node fact only — an open/active
descendant's own stored status is never overwritten; a ruled-out ancestor's
shadow over it, and the collapse of a subtree that is entirely
tried/ruled-out to one summary line, are both **rendering-level**
(:func:`ledger_do_not_repropose`). A legacy three-section OR single-blob
markdown ledger (pre-real-chunks, or the older ``## Tried`` / ``## Ruled
out`` / ``## Open`` shape) still reads correctly: :func:`_parse_ledger`
remains the legacy reader, and a dossier whose container chunk still holds
that markdown is converted in place — node chunks + tags materialized, then
the container blanked — lazily on first access (see
:func:`_migrate_legacy_ledger`), migration-free, and ordered so a crash
mid-conversion leaves the legacy text intact (never destroy the one copy
before the replacement durably exists — this loss already happened in prod,
dossier 202546, Aug 2026). :func:`read_ledger` and :func:`ledger_do_not_repropose`
still hand back / accept markdown text respectively (the tick *prompt*'s
shape, :mod:`precis.quest.tick`, is unchanged) — :func:`read_ledger`
re-renders the live forest via :func:`_render_ledger` rather than reading a
stored blob, and :func:`ledger_do_not_repropose` accepts either the forest
directly or (a thin back-compat shim) that same markdown text, so no caller
needed to change. :func:`read_dossier` still joins the whole body into one
string (the ``view='dossier'`` handler + history rely on it) — the ledger's
contribution is the same rendered markdown, standing in for its now-several
underlying chunks; only the tick *prompt* separates narrative from ledger
(:func:`read_narrative`, :func:`read_ledger`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.store import Tag

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from precis.store import Store

_RELATION = "dossier-of"

#: The owner's reader-facing PAPER draft — a SEPARATE draft from the
#: dossier (the dossier is the internal thinking substrate; the paper is
#: what a human reads). Mirrors ``dossier-of`` exactly (asymmetric,
#: auto-mirrored inverse ``has-paper``), but this module does not mint the
#: paper draft — that pipeline is unbuilt (docs/backlog/paper-writing-pipeline.md). :func:`paper_ref_id` is a read-only resolver so callers
#: (the quest web dashboard) can link to a paper when one exists and
#: degrade gracefully when it doesn't. Keep in sync with the `relations`
#: seed in migration 0089_paper_of_relation.sql.
_PAPER_RELATION = "paper-of"

_SEED = (
    "_(No synthesis yet — this dossier is rewritten each research cycle. The "
    "first tick will replace this seed with the current understanding, "
    "the best leads so far, what's been ruled out, and the open questions.)_"
)

#: The pinned ledger's node statuses (dossier-hygiene design). ``open`` is
#: the default for a freshly added node; ``active`` marks a direction
#: currently being pursued; ``tried``/``ruled-out`` are both "do not
#: re-propose" — the model-safe vocabulary, and every clamp target for a
#: caller-supplied status that doesn't match falls back to ``"open"``.
_STATUSES: tuple[str, ...] = ("open", "active", "tried", "ruled-out")
#: Statuses that mean "don't re-propose this direction" — both an own status
#: and (rendering-level, see :func:`ledger_do_not_repropose`) an inherited one.
_DEAD_STATUSES = frozenset({"tried", "ruled-out"})

#: The single heading for the nested attempt tree, replacing the old
#: three-section ledger (still parsed for backward compatibility, below).
_ATTEMPTS_HEADING = "## Attempts"
#: Legacy three-section ledger headings → the status a bullet under them
#: becomes (depth-0 node, no tree structure — pre-attempt-tree ledgers, and
#: still `append_ledger_entry`'s own vocabulary).
_LEGACY_HEADINGS: dict[str, str] = {
    "## Tried": "tried",
    "## Ruled out": "ruled-out",
    "## Open": "open",
}
_LEDGER_PLACEHOLDER = "(none yet)"
#: A node bullet's optional status prefix, e.g. ``[ruled-out] c with x``.
_STATUS_PREFIX_RE = re.compile(r"^\[(?P<status>[a-z-]+)\]\s*")
#: Spaces of indentation per tree depth (nested bullets under ``## Attempts``,
#: :func:`_render_ledger`'s / :func:`_parse_ledger`'s markdown shape only —
#: storage nesting is real ``parent_chunk_id``, see :func:`_load_ledger_nodes`).
_INDENT_WIDTH = 2
#: ``meta.pinned`` value stamped on every ledger tree-node chunk (as opposed
#: to ``"ledger"`` on the one container chunk they nest under) — the
#: narrative-body filters in :func:`rewrite_dossier`/:func:`read_narrative`
#: already exclude ANY truthy ``meta.pinned``, so this needs no filter change
#: there; it exists so :func:`_load_ledger_nodes` can pick node chunks out
#: from an ordinary child paragraph.
_LEDGER_NODE_PINNED = "ledger-node"


@dataclass
class AttemptNode:
    """One node of the pinned ledger's attempt tree.

    ``status`` is always the node's own STORED fact — never overwritten by a
    ruled-out ancestor (that shadowing, plus the dead-subtree collapse, is
    rendering-level only, see :func:`ledger_do_not_repropose`). ``children``
    are refinements/variants of this direction. ``handle`` is the node's own
    chunk handle (``meta.pinned='ledger-node'``) — set on every node loaded
    from storage (:func:`_load_ledger_nodes`), ``None`` only for a node that
    exists solely as a parsed-but-not-yet-written :class:`AttemptNode` (the
    legacy-markdown parse result before :func:`_migrate_legacy_ledger`
    materializes it).
    """

    text: str
    status: str
    children: list[AttemptNode] = field(default_factory=list)
    handle: str | None = None


def _parse_ledger(text: str) -> list[AttemptNode]:
    """Parse the pinned ledger chunk's markdown into a forest of root nodes.

    Tolerant of the placeholder line and of anything outside a recognised
    heading (dropped) — a human editing the ledger by hand only needs to keep
    a heading for their edits to round-trip. Two formats parse:

    * the current nested tree under ``## Attempts`` — ``- [status] text``,
      2 spaces of indentation per depth (:data:`_INDENT_WIDTH`), a missing/
      unrecognised status clamps to ``"open"``;
    * the legacy flat ``## Tried`` / ``## Ruled out`` / ``## Open`` ledger —
      each bullet (no status prefix) becomes a depth-0 node whose status is
      that heading's — so a pre-attempt-tree ledger parses without loss and
      needs no migration.
    """
    roots: list[AttemptNode] = []
    mode: str | None = None  # "tree" | a legacy status | None (unrecognised)
    stack: list[tuple[int, AttemptNode]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _ATTEMPTS_HEADING:
            mode, stack = "tree", []
            continue
        if stripped in _LEGACY_HEADINGS:
            mode = _LEGACY_HEADINGS[stripped]
            continue
        if stripped.startswith("## "):
            mode = None  # an unrecognised heading — drop until a known one
            continue
        if mode is None or not stripped.startswith("- "):
            continue
        bullet = stripped[2:].strip()
        if not bullet or bullet == _LEDGER_PLACEHOLDER:
            continue
        if mode == "tree":
            m = _STATUS_PREFIX_RE.match(bullet)
            status = m.group("status") if m else "open"
            if status not in _STATUSES:
                status = "open"
            node_text = bullet[m.end() :].strip() if m else bullet
            if not node_text:
                continue
            indent = len(line) - len(line.lstrip(" "))
            depth = indent // _INDENT_WIDTH
            node = AttemptNode(text=node_text, status=status, children=[])
            while stack and stack[-1][0] >= depth:
                stack.pop()
            (stack[-1][1].children if stack else roots).append(node)
            stack.append((depth, node))
        else:
            roots.append(AttemptNode(text=bullet, status=mode, children=[]))
    return roots


def _render_ledger(roots: list[AttemptNode]) -> str:
    """Serialize a forest of :class:`AttemptNode` back to the ledger's
    markdown — one ``## Attempts`` heading, nested ``- [status] text``
    bullets, :data:`_INDENT_WIDTH` spaces per depth. Always the node's own
    STORED status — no inheritance/collapse (that's rendering-level, see
    :func:`ledger_do_not_repropose`), so this round-trips exactly through
    :func:`_parse_ledger`."""
    lines = [_ATTEMPTS_HEADING]

    def walk(nodes: list[AttemptNode], depth: int) -> None:
        indent = " " * (_INDENT_WIDTH * depth)
        for n in nodes:
            lines.append(f"{indent}- [{n.status}] {n.text}")
            walk(n.children, depth + 1)

    if roots:
        walk(roots, 0)
    else:
        lines.append(f"- {_LEDGER_PLACEHOLDER}")
    return "\n".join(lines) + "\n"


#: A fresh ledger container chunk starts blank — the tree lives in its
#: (initially absent) child node chunks now, not in the container's own
#: ``text``. :func:`read_ledger`'s markdown rendering of an empty forest
#: still shows :data:`_LEDGER_PLACEHOLDER` (via :func:`_render_ledger`); only
#: the on-disk seed changed.
_LEDGER_SEED = ""


def _flatten_with_parent(
    roots: list[AttemptNode],
) -> list[tuple[AttemptNode, AttemptNode | None]]:
    """Every node in the forest paired with its immediate parent (``None``
    for a root) — the shared traversal behind node addressing."""
    out: list[tuple[AttemptNode, AttemptNode | None]] = []

    def walk(nodes: list[AttemptNode], parent: AttemptNode | None) -> None:
        for n in nodes:
            out.append((n, parent))
            walk(n.children, n)

    walk(roots, None)
    return out


def _normalize_node_text(text: str) -> str:
    """Trim AND collapse all internal whitespace — including embedded
    newlines — to single spaces.

    ``add_attempt``/``mark_attempt``'s ``text``/``node``/``parent`` arrive as
    raw, untrusted model JSON (the tick's ``ledger_ops`` payload op). A plain
    ``.strip()`` leaves an embedded newline alone — and even though a node's
    identity is now a real chunk (not a markdown re-parse), :func:`read_ledger`
    / :func:`ledger_do_not_repropose` still project the forest back to
    markdown for the tick prompt via :func:`_render_ledger`, which writes a
    node's text verbatim after its ``- [status] `` prefix — so an embedded
    ``"\\n- [ruled-out] fabricated"`` would still render, in that PROMPT view,
    as an EXTRA physical bullet line a model could mistake for a real
    sibling entry. Collapsing here, at both the storage boundary
    (:func:`add_attempt`'s stored text) and the match boundary
    (:func:`_match_nodes`, used by both functions' node/parent lookups),
    keeps storage and matching consistent — a node's stored text can never
    contain a newline to begin with.
    """
    return " ".join(text.split())


def _match_nodes(
    roots: list[AttemptNode], text: str, parent: str | None = None
) -> list[AttemptNode]:
    """Nodes whose text matches ``text`` — trimmed, whitespace-collapsed,
    case-insensitive (the addressing rule: exact
    node text, no id bookkeeping, because the model sees the ledger in its
    prompt and can quote it exactly; :func:`_normalize_node_text` guards
    against an embedded newline forging a bullet-line match). ``parent``
    narrows to nodes whose immediate parent's text also matches, the
    documented disambiguator when the same text appears in two branches.
    Zero or >1 matches is the caller's cue to no-op (ambiguous/unmatched —
    never a guess)."""
    target = _normalize_node_text(text).casefold()
    pairs = _flatten_with_parent(roots)
    matches = [
        (n, p) for n, p in pairs if _normalize_node_text(n.text).casefold() == target
    ]
    if parent is not None:
        ptarget = _normalize_node_text(parent).casefold()
        matches = [
            (n, p)
            for n, p in matches
            if p is not None and _normalize_node_text(p.text).casefold() == ptarget
        ]
    return [n for n, _p in matches]


def _subtree_all_dead(node: AttemptNode) -> bool:
    """True when ``node`` and every descendant is ``tried``/``ruled-out`` —
    the dead-subtree collapse trigger."""
    return node.status in _DEAD_STATUSES and all(
        _subtree_all_dead(c) for c in node.children
    )


def _subtree_size(node: AttemptNode) -> int:
    """Node count of ``node``'s subtree, itself included."""
    return 1 + sum(_subtree_size(c) for c in node.children)


def _do_not_repropose_lines(
    nodes: list[AttemptNode], *, ancestor_ruled_out: bool = False
) -> list[str]:
    """The "do not re-propose" bullet lines for ``nodes`` — RENDER-level
    inheritance + dead-subtree collapse (never mutates a node's own stored
    status, see :class:`AttemptNode`).

    A node's *effective* status is ``ruled-out`` when its own stored status
    is ``ruled-out`` OR an ancestor's is — so an open/active descendant of a
    ruled-out direction still reads as ruled out here, though its own stored
    status is untouched (:func:`mark_attempt` never rewrites a descendant). A
    subtree that is entirely ``tried``/``ruled-out`` collapses to its root
    line plus a variant count (e.g. ``… (4 variants, all ruled out)``) — the
    full per-node detail survives in the pinned chunk's edit history
    (``prev_text``) and the logbook, just not repeated here every tick.
    """
    lines: list[str] = []
    for n in nodes:
        effective_ruled_out = ancestor_ruled_out or n.status == "ruled-out"
        if _subtree_all_dead(n):
            count = _subtree_size(n)
            label = "ruled-out" if effective_ruled_out else "tried"
            plural = "" if count == 1 else "s"
            lines.append(
                f"- [{label}] {n.text} … ({count} variant{plural}, all ruled out)"
            )
            continue
        own_dead = n.status in _DEAD_STATUSES
        if own_dead or (ancestor_ruled_out and n.status in ("open", "active")):
            label = "ruled-out" if effective_ruled_out else n.status
            lines.append(f"- [{label}] {n.text}")
        lines.extend(
            _do_not_repropose_lines(n.children, ancestor_ruled_out=effective_ruled_out)
        )
    return lines


def ledger_do_not_repropose(ledger: list[AttemptNode] | str) -> str:
    """The pinned ledger's "do NOT re-propose these directions" prompt block
    — tried/ruled-out nodes (own or inherited from a ruled-out ancestor),
    with a fully-dead subtree collapsed to one summary line (see
    :func:`_do_not_repropose_lines`). ``open``/``active`` directions with no
    ruled-out ancestor are excluded — those are the exploration queue, not a
    constraint. ``"(nothing pinned yet)"`` when nothing qualifies.

    Takes the forest directly (the primary form — matches storage, no
    markdown round-trip needed). Also accepts the legacy markdown TEXT
    (:func:`read_ledger`'s return shape) as a thin back-compat shim —
    :mod:`precis.quest.tick`'s ``_ledger_constraints`` still calls this with
    ``read_ledger``'s text, so that call site needed no change when the
    ledger's storage moved off markdown.
    """
    roots = _parse_ledger(ledger) if isinstance(ledger, str) else ledger
    lines = _do_not_repropose_lines(roots)
    return "\n".join(lines) if lines else "(nothing pinned yet)"


#: The second pinned chunk (Slice 4c-4): the candidate lineage tree. Unlike
#: the ledger (model-authored, additive), this one is entirely CODE-
#: regenerated in place every tick via :func:`update_frontier_tree` — see
#: that function's docstring.
_FRONTIER_TREE_PINNED = "frontier-tree"
_FRONTIER_TREE_SEED = "_(No candidates yet.)_\n"


def dossier_ref_id(store: Store, owner_id: int) -> int | None:
    """The ref id of the owner's dossier draft, or ``None`` if it has none.

    Resolution is via the ``dossier-of`` edge, **not** the denormalized
    ``meta.dossier_of_owner`` back-pointer — so a pre-0064-§B dossier carrying
    only the legacy ``meta.dossier_of_quest`` key resolves identically, with no
    migration or backfill. Excludes a soft-deleted dossier draft
    — the ``dossier-of`` link row can outlive a ``delete()`` of the draft
    itself, and a caller resolving this id should see "no dossier" rather
    than a tombstoned ref.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT l.src_ref_id FROM links l "
            "JOIN refs r ON r.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = %s "
            "AND r.deleted_at IS NULL LIMIT 1",
            (owner_id, _RELATION),
        ).fetchone()
    return int(row[0]) if row else None


def paper_ref_id(store: Store, owner_id: int) -> int | None:
    """The ref id of the owner's reader-facing PAPER draft, or ``None``.

    Resolved via the ``paper-of`` edge — the same shape as
    :func:`dossier_ref_id`, but a distinct draft: the dossier is the
    process's internal rewritten synthesis, the paper is a separate,
    human-facing projection of it (docs/decisions/0064-dossier-thinking-
    substrate-and-paper-projection.md). Nothing in this module creates a
    paper draft — that pipeline doesn't exist yet — so this always
    returns ``None`` until some other writer links one in with
    ``rel='paper-of'``. Excludes a soft-deleted paper draft — see
    :func:`dossier_ref_id`.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT l.src_ref_id FROM links l "
            "JOIN refs r ON r.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = %s "
            "AND r.deleted_at IS NULL LIMIT 1",
            (owner_id, _PAPER_RELATION),
        ).fetchone()
    return int(row[0]) if row else None


def _owner_title(store: Store, owner_id: int) -> str:
    """The owner ref's title (any kind), used only as the dossier's default name.

    A direct ``refs`` read rather than ``store.get_ref(kind=…, id=…)`` — the
    latter requires a hardcoded ``kind`` (that was the quest coupling
    dossier-owned-by-process removes), and the title is only a cosmetic
    seed, so no ``Store`` abstraction is warranted.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
    return str(row[0]) if row and row[0] else f"ref {owner_id}"


def _find_pinned_chunk(chunks: list[Any], pinned: str) -> Any | None:
    """The non-heading chunk whose ``meta.pinned == pinned`` — shared by the
    ledger (``"ledger"``) and the frontier tree (``"frontier-tree"``, Slice
    4c-4); each pinned value picks out one distinct chunk."""
    for c in chunks:
        if c.chunk_kind != "heading" and (c.meta or {}).get("pinned") == pinned:
            return c
    return None


def _ensure_ledger_chunk_for_ref(store: Store, dossier_id: int) -> str:
    """:func:`ensure_ledger_chunk`, given an already-resolved dossier ref id.

    Internal — avoids the ``ensure_dossier`` ↔ ``ensure_ledger_chunk``
    recursion (``ensure_dossier`` calls this directly after seeding the
    narrative rather than going back through the public entry point).
    """
    chunks = store.drafts.reading_order(dossier_id)
    found = _find_pinned_chunk(chunks, "ledger")
    if found is not None:
        return str(found.handle)
    created = store.drafts.add_chunks(
        ref_id=dossier_id, chunk_kind="paragraph", text=_LEDGER_SEED, split=False
    )
    handle = str(created[0].handle)
    store.drafts.patch_chunk_meta(handle, {"pinned": "ledger"})
    return handle


def _ensure_frontier_tree_chunk_for_ref(store: Store, dossier_id: int) -> str:
    """The frontier-tree sibling of :func:`_ensure_ledger_chunk_for_ref` —
    creates the ``meta.pinned='frontier-tree'`` chunk (seeded empty) if
    absent, else returns its existing handle. Internal — see
    :func:`update_frontier_tree` for the public, code-regenerating entry
    point."""
    chunks = store.drafts.reading_order(dossier_id)
    found = _find_pinned_chunk(chunks, _FRONTIER_TREE_PINNED)
    if found is not None:
        return str(found.handle)
    created = store.drafts.add_chunks(
        ref_id=dossier_id,
        chunk_kind="paragraph",
        text=_FRONTIER_TREE_SEED,
        split=False,
    )
    handle = str(created[0].handle)
    store.drafts.patch_chunk_meta(handle, {"pinned": _FRONTIER_TREE_PINNED})
    return handle


def update_frontier_tree(store: Store, owner_id: int) -> str:
    """Regenerate the owner's pinned frontier-tree chunk from the current
    candidate lineage (:func:`precis.quest.frontier.render_frontier_tree`).

    Called at the end of each quest tick, **after harvest**, so the tree
    reflects freshly-measured barriers/energies (:mod:`precis.quest.tick`).
    Unlike the ledger (model-authored, additive across ticks), this chunk is
    entirely **code**-regenerated — whole-rewritten in place on every call,
    never touched by the model, and excluded from the narrative rewrite the
    same way the ledger is (:func:`rewrite_dossier`, :func:`read_narrative`
    — both filter on ANY ``meta.pinned`` value now, not just ``"ledger"``).
    Creates the dossier (and the chunk) if the owner has none yet. Returns
    the chunk's handle.
    """
    from precis.quest.frontier import render_frontier_tree

    did = ensure_dossier(store, owner_id)
    handle = _ensure_frontier_tree_chunk_for_ref(store, did)
    markdown = render_frontier_tree(store, owner_id)
    store.drafts.edit_text(handle, markdown, source={"reason": "quest-frontier-tree"})
    return handle


def ensure_ledger_chunk(store: Store, owner_id: int) -> str:
    """Return the handle of the owner's pinned ledger chunk.

    Idempotent, and heals a dossier that predates dossier-owned-by-process (narrative-only,
    no ledger chunk yet) by creating+pinning one lazily — a live owner grows
    its ledger on its next read/append rather than needing a migration.
    Creates the dossier itself (via :func:`ensure_dossier`) if the owner has
    none yet.
    """
    did = ensure_dossier(store, owner_id)
    return _ensure_ledger_chunk_for_ref(store, did)


def ensure_dossier(store: Store, owner_id: int, *, title: str | None = None) -> int:
    """Return the owner's dossier ref id, creating a seeded draft if absent.

    ``owner_id`` is any ref — a quest today, or any other process that owns a
    living synthesis. Idempotent: the ``create_draft`` dup-guard
    enforces one dossier per owner, but we look up first so a concurrent/second
    call returns the existing id rather than raising. A fresh dossier is seeded
    with BOTH the narrative body and the pinned ledger; an *existing* dossier
    is returned as-is (a pre-A dossier heals its ledger lazily via
    :func:`ensure_ledger_chunk`, not here — see the module docstring).
    """
    existing = dossier_ref_id(store, owner_id)
    if existing is not None:
        return existing
    stmt = _owner_title(store, owner_id).splitlines()[0]
    ref, _heading = store.drafts.create_draft(
        name=f"dossier-{owner_id}",
        title=title or f"Dossier — {stmt[:80]}",
        project_ref_id=owner_id,
        meta={"dossier_of_owner": owner_id},
        relation=_RELATION,
    )
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text=_SEED, split=False
    )
    _ensure_ledger_chunk_for_ref(store, ref.id)
    return int(ref.id)


def _chunk_ord(store: Store, ref_id: int, handle: str) -> int:
    """The integer ``chunks.ord`` for ``handle`` — the identity
    :meth:`TagsMixin.add_tag`'s ``pos=`` addresses (it resolves ``(ref_id,
    pos)`` straight to ``chunks.ord``, confirmed against
    ``_resolve_chunk_id``/``_lookup_chunk_id`` in ``store/_tags_ops.py`` /
    ``store/_mappers.py``). NOT the same thing as the fractional ``pos``
    sort-key string :class:`DraftChunk` exposes for tree ordering — and
    :class:`DraftChunk` (from :meth:`DraftStore.reading_order` /
    :meth:`DraftStore.add_chunks`) doesn't carry ``ord`` at all, so a caller
    that needs it for a specific chunk does a targeted lookup here; a caller
    that needs it for every chunk of a ref uses the established bulk pattern,
    :meth:`DraftStore.chunk_ord_map` (``chunk_id -> ord``, e.g.
    ``workers/classify.py``'s claim query)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ord FROM chunks WHERE ref_id = %s AND handle = %s",
            (ref_id, handle),
        ).fetchone()
    assert row is not None, f"chunk handle {handle!r} not found on ref {ref_id}"
    return int(row[0])


def _attempt_statuses(store: Store, chunk_ids: list[int]) -> dict[int, str]:
    """``chunk_id -> status`` for every ledger-node chunk in ``chunk_ids``,
    read from its ``ATTEMPT:<status>`` chunk tag in one query. A node chunk
    with no ATTEMPT tag (shouldn't happen — every writer stamps one, see
    :func:`_write_node_chunk`) is simply absent from the map; callers default
    to ``"open"``, matching the old parser's clamp-unrecognised-to-open
    behaviour."""
    if not chunk_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ct.chunk_id, t.value FROM chunk_tags ct "
            "JOIN tags t ON t.tag_id = ct.tag_id "
            "WHERE ct.chunk_id = ANY(%s) AND t.namespace = 'ATTEMPT'",
            (chunk_ids,),
        ).fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def _write_node_chunk(
    store: Store, dossier_id: int, parent_handle: str, text: str, status: str
) -> Any:
    """Create one ledger-node chunk as a child of ``parent_handle`` (the
    ledger container, or another node's own chunk) and stamp it: ``split=
    False`` so a multi-line node text can never fan out into several chunks,
    ``meta.pinned='ledger-node'`` (:data:`_LEDGER_NODE_PINNED` — keeps it out
    of the narrative body for free, see the module docstring), and its
    ``ATTEMPT:<status>`` chunk tag (``replace_prefix=True`` — harmless here
    since a freshly created chunk carries no prior tag, but keeps this the
    same call :func:`mark_attempt` uses to swap an existing one). Returns the
    created :class:`~precis.store._draft_ops.DraftChunk`. Shared by
    :func:`add_attempt` and the legacy-migration materializer
    (:func:`_materialize_legacy_forest`)."""
    created = store.drafts.add_chunks(
        ref_id=dossier_id,
        chunk_kind="paragraph",
        text=text,
        at={"into": parent_handle},
        split=False,
    )
    chunk = created[0]
    store.drafts.patch_chunk_meta(chunk.handle, {"pinned": _LEDGER_NODE_PINNED})
    ord_ = _chunk_ord(store, dossier_id, chunk.handle)
    store.add_tag(
        dossier_id, Tag.closed("ATTEMPT", status), pos=ord_, replace_prefix=True
    )
    return chunk


def _node_children(
    store: Store, dossier_id: int, parent_chunk_id: int | None
) -> list[tuple[AttemptNode, int]]:
    """Live ledger-node children of a container/node chunk_id, paired with
    each node's own ``chunk_id`` (:class:`AttemptNode` itself only carries
    the ``handle``, and recursion needs the id to keep descending).
    Parent-chunk_id-scoped — unlike :func:`_match_nodes`'s tree-wide text
    search, this only looks at one level — used solely by the legacy-
    migration materializer's crash-recovery dedup
    (:func:`_materialize_legacy_forest`)."""
    chunks = store.drafts.reading_order(dossier_id)
    kids = [
        c
        for c in chunks
        if c.parent_chunk_id == parent_chunk_id
        and (c.meta or {}).get("pinned") == _LEDGER_NODE_PINNED
    ]
    if not kids:
        return []
    statuses = _attempt_statuses(store, [c.chunk_id for c in kids])
    return [
        (
            AttemptNode(
                text=c.text,
                status=statuses.get(c.chunk_id, "open"),
                children=[],
                handle=str(c.handle),
            ),
            c.chunk_id,
        )
        for c in kids
    ]


def _materialize_legacy_forest(
    store: Store,
    dossier_id: int,
    container_handle: str,
    container_chunk_id: int,
    forest: list[AttemptNode],
) -> None:
    """Write a legacy-markdown-parsed ``forest`` as real ledger-node chunks
    nested under the container — the materialization step of
    :func:`_migrate_legacy_ledger`. Recurses depth-first; at each level, a
    node whose text+status already exists among the parent's LIVE children is
    reused rather than duplicated, which is what makes a retried backfill
    (the caller never blanks the legacy source text until this returns
    without raising, so a crash mid-conversion just re-runs it) safe against
    double-creating the nodes an earlier, interrupted attempt already wrote.
    """

    def walk(
        parent_handle: str, parent_chunk_id: int, nodes: list[AttemptNode]
    ) -> None:
        existing = {
            (n.text, n.status): (n, chunk_id)
            for n, chunk_id in _node_children(store, dossier_id, parent_chunk_id)
        }
        for n in nodes:
            hit = existing.get((n.text, n.status))
            if hit is not None:
                existing_node, chunk_id = hit
                handle = existing_node.handle
                assert handle is not None
            else:
                created = _write_node_chunk(
                    store, dossier_id, parent_handle, n.text, n.status
                )
                handle, chunk_id = str(created.handle), created.chunk_id
            walk(handle, chunk_id, n.children)

    walk(container_handle, container_chunk_id, forest)


def _migrate_legacy_ledger(store: Store, dossier_id: int, container: Any) -> None:
    """Convert a legacy markdown ledger (the pre-real-chunks ``##
    Attempts``/three-section blob living in the container chunk's own
    ``text``) into node chunks + ``ATTEMPT:`` tags, in place. Triggered
    lazily by :func:`_load_ledger_nodes` on first access to a dossier whose
    container still holds non-blank text; idempotent (a migrated container's
    text is blank, so a later access's ``bool(container.text.strip())``
    check is false and this never re-runs).

    Ordering matters: the legacy text is the ONE copy of the ledger's data
    until every node it describes is durably written, so it is blanked ONLY
    after :func:`_materialize_legacy_forest` returns — a crash before that
    line leaves the legacy text intact for the next access to retry (that
    retry is itself safe against double-writing, see
    :func:`_materialize_legacy_forest`). Losing the ledger silently restarts
    a quest's own trail; this exact loss already happened in prod
    (dossier 202546, Aug 2026 — see the ``read_narrative`` docstring for the
    sibling incident that motivated ``body[0]``-only reads).
    """
    forest = _parse_ledger(container.text)
    if forest:
        _materialize_legacy_forest(
            store, dossier_id, str(container.handle), container.chunk_id, forest
        )
    store.drafts.edit_text(
        container.handle, "", source={"reason": "quest-ledger-migrate"}
    )


def _load_ledger_nodes(store: Store, dossier_id: int) -> list[AttemptNode]:
    """The pinned ledger's attempt forest, loaded from real chunks — each
    node its own child chunk of the ``pinned='ledger'`` container
    (``parent_chunk_id`` nesting), tagged ``ATTEMPT:<status>`` — rather than
    parsed from a markdown blob (the storage move this module went through;
    see the module docstring). :meth:`Store.reading_order`'s DFS pre-order
    already puts a parent chunk before its children, so one pass builds the
    tree.

    Migrates a legacy markdown ledger in place on first access
    (:func:`_migrate_legacy_ledger`) when the container chunk still holds
    non-blank text — idempotent, see that function's docstring.

    Returns ``[]`` when the dossier has no ledger container chunk yet (the
    caller is responsible for :func:`ensure_ledger_chunk` first — every
    public entry point that reaches this already does, via
    :func:`_ledger_roots`).
    """
    chunks = store.drafts.reading_order(dossier_id)
    container = _find_pinned_chunk(chunks, "ledger")
    if container is None:
        return []
    if container.text.strip():
        _migrate_legacy_ledger(store, dossier_id, container)
        chunks = store.drafts.reading_order(dossier_id)  # re-fetch post-migration
    node_chunks = [
        c for c in chunks if (c.meta or {}).get("pinned") == _LEDGER_NODE_PINNED
    ]
    if not node_chunks:
        return []
    statuses = _attempt_statuses(store, [c.chunk_id for c in node_chunks])
    by_chunk_id: dict[int, AttemptNode] = {}
    roots: list[AttemptNode] = []
    for c in node_chunks:
        node = AttemptNode(
            text=c.text,
            status=statuses.get(c.chunk_id, "open"),
            children=[],
            handle=str(c.handle),
        )
        by_chunk_id[c.chunk_id] = node
        if c.parent_chunk_id == container.chunk_id:
            roots.append(node)
        elif c.parent_chunk_id is not None:
            parent = by_chunk_id.get(c.parent_chunk_id)
            if parent is not None:
                parent.children.append(node)
            # else: a node chunk whose parent is retired/not itself a node
            # chunk — dropped, mirroring reading_order's own exclusion of a
            # subtree unreachable from a live root.
    return roots


def read_dossier(store: Store, owner_id: int) -> tuple[int | None, str | None, str]:
    """``(dossier_ref_id, body_handle, body_text)`` for the owner.

    Returns ``(None, None, "")`` when the owner has no dossier yet. The body
    is every non-heading chunk in reading order (narrative + pinned ledger,
    once both exist), joined — the ``view='dossier'`` handler + history read
    this whole-body join; only the tick *prompt* separates narrative from
    ledger (:func:`read_narrative`, :func:`read_ledger`). The ledger's
    contribution is its rendered markdown (:func:`_render_ledger` over
    :func:`_load_ledger_nodes`), standing in for its now-several underlying
    node chunks — those are skipped individually here so the join doesn't
    show each node's bare text out of context, blank lines where the
    (now-empty) container chunk used to carry the whole tree, or lose the
    tree's status/indentation shape.
    """
    did = dossier_ref_id(store, owner_id)
    if did is None:
        return None, None, ""
    chunks = store.drafts.reading_order(did)
    body = [c for c in chunks if c.chunk_kind != "heading"]
    handle = body[0].dc if body else None
    parts: list[str] = []
    for c in body:
        pinned = (c.meta or {}).get("pinned")
        if pinned == _LEDGER_NODE_PINNED:
            continue
        if pinned == "ledger":
            parts.append(_render_ledger(_load_ledger_nodes(store, did)))
        else:
            parts.append(c.text)
    text = "\n\n".join(parts)
    return did, handle, text


def read_narrative(store: Store, owner_id: int) -> str:
    """The model-rewritten narrative only (no pinned chunk, no heading).

    Feeds the tick prompt's ``{dossier}`` slot — the ledger is surfaced
    separately (:func:`read_ledger`) as an explicit constraint, and the
    frontier tree is a code-rendered artifact, neither folded into the
    rewritable prose. Excludes ANY pinned chunk (``meta.pinned`` truthy —
    ``"ledger"`` or ``"frontier-tree"``), not just the ledger, so a future
    pinned chunk needs no code change here. Returns ``""`` when the owner
    has no dossier.

    **Symmetric with :func:`rewrite_dossier` by contract**: the write side
    only ever touches ``body[0]``, so the read side returns only ``body[0]``.
    This used to join every unpinned body chunk, which is identical under the
    invariant "a dossier has exactly one narrative chunk" — and silently
    catastrophic once that invariant breaks. It broke in prod (quest 202469 /
    dossier 202546, Aug 2026): a generic draft-hygiene todo refragmented the
    narrative into 13 chunks, after which ``rewrite_dossier`` kept updating
    only the first while this function fed the model all 13 — 8 of them frozen
    at their pre-refactor state — under the prompt banner "the living
    synthesis". The model had no signal that most of its "current"
    understanding was weeks stale. Taking ``body[0]`` restores the contract;
    the ``len(body) > 1`` warning makes a future divergence visible instead of
    silent, since the read side degrading quietly is what let this run for 16
    ticks unnoticed.
    """
    did = dossier_ref_id(store, owner_id)
    if did is None:
        return ""
    chunks = store.drafts.reading_order(did)
    body = [
        c
        for c in chunks
        if c.chunk_kind != "heading" and not (c.meta or {}).get("pinned")
    ]
    if not body:
        return ""
    if len(body) > 1:
        log.warning(
            "dossier %s (owner %s) has %d unpinned body chunks; expected 1. "
            "Reading body[0] only — the extras are stranded (never rewritten) "
            "and are NOT fed to the tick prompt.",
            did,
            owner_id,
            len(body),
        )
    return str(body[0].text)


def read_ledger(store: Store, owner_id: int) -> str:
    """The pinned ledger's markdown rendering (:func:`_render_ledger` over
    :func:`_load_ledger_nodes`) — a thin, on-the-fly re-projection of the
    live forest, not a stored blob. The ledger's actual storage is real
    chunks (see the module docstring); this function exists so callers that
    want the tick-prompt markdown shape (:mod:`precis.quest.tick`) don't need
    to change, and so most of this module's own tests (substring/format
    assertions against the rendered ledger) keep working unchanged across
    the storage move.

    Heals a pre-A dossier with no ledger yet (:func:`ensure_ledger_chunk`)
    and migrates a legacy markdown ledger in place on first access (see
    :func:`_load_ledger_nodes`), so this always returns a well-formed (if
    empty) rendering for any live owner, migration-free.
    """
    ensure_ledger_chunk(store, owner_id)
    did = dossier_ref_id(store, owner_id)
    assert did is not None  # ensure_ledger_chunk just guaranteed a dossier
    return _render_ledger(_load_ledger_nodes(store, did))


def _ledger_roots(store: Store, owner_id: int) -> tuple[str, int, list[AttemptNode]]:
    """``(container_handle, dossier_id, roots)`` of the owner's pinned
    ledger — the shared read-modify-write preamble for :func:`add_attempt` /
    :func:`mark_attempt` / :func:`append_ledger_entry`. Heals a pre-A dossier
    lacking a ledger chunk on the way in (:func:`ensure_ledger_chunk`), and
    migrates a legacy markdown ledger in place on first access
    (:func:`_load_ledger_nodes`)."""
    handle = ensure_ledger_chunk(store, owner_id)
    did = dossier_ref_id(store, owner_id)
    assert did is not None  # ensure_ledger_chunk just guaranteed a dossier
    return handle, did, _load_ledger_nodes(store, did)


def add_attempt(
    store: Store,
    owner_id: int,
    text: str,
    parent: str | None = None,
    status: str = "open",
) -> bool:
    """Add one node to the pinned attempt tree; return ``True`` iff added.

    A blank ``text`` is a no-op. ``status`` is clamped to ``"open"`` when it
    isn't one of :data:`_STATUSES`. ``parent`` (optional) is the exact text of
    an existing node the new one becomes a child of — matched trimmed +
    case-insensitive (:func:`_match_nodes`); zero or >1 matches is a no-op
    (ambiguous/unmatched, never a guess). ``parent=None`` adds a new root
    (depth-0) node, as a child of the ledger CONTAINER chunk. Idempotent: a
    node with byte-identical text AND status already among the target's
    children is skipped, not duplicated — the tree generalization of
    :func:`append_ledger_entry`'s existing dedup. ``text`` is whitespace-
    normalized (:func:`_normalize_node_text`) before storage — an embedded
    newline (raw, untrusted model JSON via the tick's ``ledger_ops``) would
    otherwise render, in the tick-prompt markdown view, as an extra physical
    bullet line a model could mistake for a fabricated sibling. Creates one
    new chunk (:func:`_write_node_chunk`) — no whole-ledger rewrite, unlike
    the pre-real-chunks design. Heals a pre-A dossier lacking a ledger chunk
    on the way in.
    """
    stripped_text = _normalize_node_text(text)
    if not stripped_text:
        return False
    st = status if status in _STATUSES else "open"
    container_handle, did, roots = _ledger_roots(store, owner_id)
    if parent is not None:
        matches = _match_nodes(roots, parent)
        if len(matches) != 1:
            return False
        target_node = matches[0]
        target_children = target_node.children
        parent_handle = target_node.handle
        assert parent_handle is not None  # every loaded node carries a handle
    else:
        target_children = roots
        parent_handle = container_handle
    if any(n.text == stripped_text and n.status == st for n in target_children):
        return False
    _write_node_chunk(store, did, parent_handle, stripped_text, st)
    return True


def mark_attempt(
    store: Store,
    owner_id: int,
    node: str,
    status: str,
    parent: str | None = None,
) -> bool:
    """Set an existing attempt node's status; return ``True`` iff applied.

    ``node`` is matched by exact text — trimmed, whitespace-normalized
    (:func:`_normalize_node_text`), case-insensitive (:func:`_match_nodes`);
    ``parent`` disambiguates when the same text appears in two branches
    (also normalized before matching). Zero or >1 matches, or a ``status``
    outside :data:`_STATUSES`, is a no-op — degrade-don't-crash, never a
    guess. Only the matched node's own stored status changes; a descendant's
    status is untouched (an inherited-ruled-out shadow, and a dead-subtree
    collapse, are both rendering-level — see :func:`ledger_do_not_repropose`).
    Applied by REPLACING the node chunk's ``ATTEMPT:`` tag
    (``add_tag(..., replace_prefix=True)`` — the v1 "at most one value per
    closed prefix on a target" invariant), not a whole-ledger rewrite. Heals
    a pre-A dossier lacking a ledger chunk on the way in.
    """
    if status not in _STATUSES:
        return False
    node_text = _normalize_node_text(node or "")
    if not node_text:
        return False
    _container_handle, did, roots = _ledger_roots(store, owner_id)
    matches = _match_nodes(roots, node_text, parent)
    if len(matches) != 1:
        return False
    target = matches[0]
    assert target.handle is not None  # every loaded node carries a handle
    ord_ = _chunk_ord(store, did, target.handle)
    store.add_tag(did, Tag.closed("ATTEMPT", status), pos=ord_, replace_prefix=True)
    return True


#: `append_ledger_entry`'s legacy ``section`` vocabulary → the attempt-tree
#: status it maps to (identity today, kept as an explicit table since the two
#: vocabularies are allowed to diverge later). An unrecognised section clamps
#: to ``"open"`` — unchanged behavior from the pre-tree ledger.
_SECTION_TO_STATUS: dict[str, str] = {
    "tried": "tried",
    "ruled-out": "ruled-out",
    "open": "open",
}


def append_ledger_entry(store: Store, owner_id: int, section: str, text: str) -> bool:
    """Add one depth-0 attempt node under ``section``'s status.

    The pre-attempt-tree entry point, kept for its existing callers (the
    tick's ``ledger_add`` payload op): ``section`` is one of ``tried`` /
    ``ruled-out`` / ``open``, an unrecognised value clamps to ``open``, and
    this is exactly :func:`add_attempt` with ``parent=None`` and
    ``status=<mapped section>`` — same dedup, same blank-text no-op, same
    healing. New callers should reach for :func:`add_attempt` /
    :func:`mark_attempt` directly when they need a child node or a status
    change on an existing one.
    """
    return add_attempt(
        store, owner_id, text, status=_SECTION_TO_STATUS.get(section, "open")
    )


def rewrite_dossier(store: Store, owner_id: int, markdown: str) -> int:
    """Whole-rewrite the owner's dossier NARRATIVE to ``markdown``; return its
    ref id.

    Ensures the dossier exists, then edits the narrative body chunk in place
    (``edit_text`` logs ``prev_text``) — ANY pinned chunk (``meta.pinned``
    truthy: the ledger, and the code-regenerated frontier tree — Slice 4c-4)
    is explicitly excluded, so each survives every rewrite byte-identical. If somehow there is no narrative chunk yet, one is added.
    """
    did = ensure_dossier(store, owner_id)
    chunks = store.drafts.reading_order(did)
    body = [
        c
        for c in chunks
        if c.chunk_kind != "heading" and not (c.meta or {}).get("pinned")
    ]
    if body:
        # edit_text keys on the legacy ``.handle`` (the ``¶`` anchor), not the
        # universal ``.dc`` display handle — mirror the draft handler.
        store.drafts.edit_text(
            body[0].handle, markdown, source={"reason": "quest-tick"}
        )
    else:  # pragma: no cover - ensure_dossier always seeds a narrative body
        store.drafts.add_chunks(
            ref_id=did, chunk_kind="paragraph", text=markdown, split=False
        )
    return did


__all__ = [
    "AttemptNode",
    "add_attempt",
    "append_ledger_entry",
    "dossier_ref_id",
    "ensure_dossier",
    "ensure_ledger_chunk",
    "ledger_do_not_repropose",
    "mark_attempt",
    "paper_ref_id",
    "read_dossier",
    "read_ledger",
    "read_narrative",
    "rewrite_dossier",
    "update_frontier_tree",
]
