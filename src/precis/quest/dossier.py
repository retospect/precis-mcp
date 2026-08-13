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

Dossier-owned-by-process splits the dossier body into **two chunks**: a **narrative**
paragraph (the whole-rewritten prose synthesis, ``edit_text``'d in place each
tick, stable handle, ``prev_text`` history for free, and — since the
attempt-tree ledger (dossier-hygiene design — quest package docstring) — held to a
code-enforced growth ratchet, not a fixed cap: a rewrite may only outgrow the
previous narrative by more than ~15%+50 words when the tick shows visible
progress, see :mod:`precis.quest.narrative_budget` and ``tick.py``'s
``_apply_narrative_gate``) and one **pinned ledger** paragraph
(``meta.pinned='ledger'``, set via
``patch_chunk_meta`` — the persona-threads plan-marker precedent, no new
chunk_kind, no migration) that survives every whole-rewrite untouched,
mutated only by explicit :func:`add_attempt` / :func:`mark_attempt` calls
(:func:`append_ledger_entry` is the pre-tree three-section entry point, kept
for its existing callers — it now adds a depth-0 tree node under the
mapped status). The ledger holds the *strategic* attempt tree — one node per
tried/abandoned/open *direction* (a whole direction, not a single ruled-out
structure — that per-candidate ledger already lives on ``structure`` tags,
see ``tick.py``'s ``_ruled_out_handles``), children as refinements/variants
of their parent, each carrying a status (``open`` / ``active`` / ``tried`` /
``ruled-out``) — so the loop can't silently lose its own trail on a rewrite
that drops a rule-out from the free prose (the autocatpath dead-3-days spin),
and so a whole abandoned branch (try a, then b, then c-with-x and c-with-y)
reads as a subtree, not an ambiguous flat list. Ruling out a node is a
*stored*, per-node fact only — an open/active descendant's own stored status
is never overwritten; a ruled-out ancestor's shadow over it, and the
collapse of a subtree that is entirely tried/ruled-out to one summary line,
are both **rendering-level** (:func:`ledger_do_not_repropose`), so the raw
pinned chunk always round-trips exactly for a human editor or a later
:func:`add_attempt`/:func:`mark_attempt` node-text match. A legacy
three-section ledger (``## Tried`` / ``## Ruled out`` / ``## Open``) still
parses — each bullet becomes a depth-0 node, status = its section — so no
migration/backfill is needed. :func:`read_dossier` still joins the whole body
(the ``view='dossier'`` handler + history rely on it); only the tick *prompt*
separates narrative from ledger (:func:`read_narrative`, :func:`read_ledger`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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
#: Spaces of indentation per tree depth (nested bullets under ``## Attempts``).
_INDENT_WIDTH = 2


@dataclass
class AttemptNode:
    """One node of the pinned ledger's attempt tree.

    ``status`` is always the node's own STORED fact — never overwritten by a
    ruled-out ancestor (that shadowing, plus the dead-subtree collapse, is
    rendering-level only, see :func:`ledger_do_not_repropose`). ``children``
    are refinements/variants of this direction.
    """

    text: str
    status: str
    children: list[AttemptNode] = field(default_factory=list)


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


_LEDGER_SEED = _render_ledger([])


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
    ``.strip()`` leaves an embedded newline alone, and :func:`_render_ledger`
    writes a node's text verbatim after its ``- [status] `` prefix — so an
    embedded ``"\\n- [ruled-out] fabricated"`` would render as an EXTRA
    physical bullet line and re-parse (:func:`_parse_ledger`) as a
    fabricated sibling node next read. Collapsing here, at both the storage
    boundary (:func:`add_attempt`'s stored text) and the match boundary
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


def ledger_do_not_repropose(ledger_text: str) -> str:
    """The pinned ledger's "do NOT re-propose these directions" prompt block
    — tried/ruled-out nodes (own or inherited from a ruled-out ancestor),
    with a fully-dead subtree collapsed to one summary line (see
    :func:`_do_not_repropose_lines`). ``open``/``active`` directions with no
    ruled-out ancestor are excluded — those are the exploration queue, not a
    constraint. Feeds the tick prompt (:mod:`precis.quest.tick`'s
    ``_ledger_constraints``); ``"(nothing pinned yet)"`` when nothing
    qualifies.
    """
    lines = _do_not_repropose_lines(_parse_ledger(ledger_text))
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


def read_dossier(store: Store, owner_id: int) -> tuple[int | None, str | None, str]:
    """``(dossier_ref_id, body_handle, body_text)`` for the owner.

    Returns ``(None, None, "")`` when the owner has no dossier yet. The body
    is every non-heading chunk in reading order (narrative + pinned ledger,
    once both exist), joined — the ``view='dossier'`` handler + history read
    this whole-body join; only the tick *prompt* separates narrative from
    ledger (:func:`read_narrative`, :func:`read_ledger`).
    """
    did = dossier_ref_id(store, owner_id)
    if did is None:
        return None, None, ""
    chunks = store.drafts.reading_order(did)
    body = [c for c in chunks if c.chunk_kind != "heading"]
    text = "\n\n".join(c.text for c in body)
    handle = body[0].dc if body else None
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
    return "\n\n".join(c.text for c in body)


def read_ledger(store: Store, owner_id: int) -> str:
    """The pinned ledger chunk's raw markdown text.

    Heals a pre-A dossier with no ledger yet (:func:`ensure_ledger_chunk`),
    so this always returns a well-formed (if empty) ledger for any live
    owner, migration-free.
    """
    handle = ensure_ledger_chunk(store, owner_id)
    did = dossier_ref_id(store, owner_id)
    assert did is not None  # ensure_ledger_chunk just guaranteed a dossier
    for c in store.drafts.reading_order(did):
        if c.handle == handle:
            return str(c.text)
    return ""  # pragma: no cover - handle was just resolved above


def _ledger_roots(store: Store, owner_id: int) -> tuple[str, int, list[AttemptNode]]:
    """``(handle, dossier_id, roots)`` of the owner's pinned ledger, parsed —
    the shared read-modify-write preamble for :func:`add_attempt` /
    :func:`mark_attempt` / :func:`append_ledger_entry`. Heals a pre-A dossier
    lacking a ledger chunk on the way in (:func:`ensure_ledger_chunk`)."""
    handle = ensure_ledger_chunk(store, owner_id)
    did = dossier_ref_id(store, owner_id)
    assert did is not None  # ensure_ledger_chunk just guaranteed a dossier
    chunk = next(c for c in store.drafts.reading_order(did) if c.handle == handle)
    return handle, did, _parse_ledger(chunk.text)


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
    (depth-0) node. Idempotent: a node with byte-identical text AND status
    already among the target's children is skipped, not duplicated — the
    tree generalization of :func:`append_ledger_entry`'s existing dedup.
    ``text`` is whitespace-normalized (:func:`_normalize_node_text`) before
    storage — an embedded newline (raw, untrusted model JSON via the tick's
    ``ledger_ops``) would otherwise render as an extra physical bullet line
    and re-parse as a fabricated sibling node. Heals a pre-A dossier lacking
    a ledger chunk on the way in.
    """
    stripped_text = _normalize_node_text(text)
    if not stripped_text:
        return False
    st = status if status in _STATUSES else "open"
    handle, _did, roots = _ledger_roots(store, owner_id)
    if parent is not None:
        matches = _match_nodes(roots, parent)
        if len(matches) != 1:
            return False
        target_children = matches[0].children
    else:
        target_children = roots
    if any(n.text == stripped_text and n.status == st for n in target_children):
        return False
    target_children.append(AttemptNode(text=stripped_text, status=st, children=[]))
    store.drafts.edit_text(
        handle, _render_ledger(roots), source={"reason": "quest-ledger"}
    )
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
    Heals a pre-A dossier lacking a ledger chunk on the way in.
    """
    if status not in _STATUSES:
        return False
    node_text = _normalize_node_text(node or "")
    if not node_text:
        return False
    handle, _did, roots = _ledger_roots(store, owner_id)
    matches = _match_nodes(roots, node_text, parent)
    if len(matches) != 1:
        return False
    matches[0].status = status
    store.drafts.edit_text(
        handle, _render_ledger(roots), source={"reason": "quest-ledger"}
    )
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
