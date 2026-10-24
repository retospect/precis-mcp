"""Directions — a quest's attempt tree as ``todo`` refs.

Retire-dossier slice 2 (``docs/backlog/retire-dossier.md``): the pinned
ledger's attempt tree moves off dossier chunks (``meta.pinned='ledger-node'``
+ ``ATTEMPT:<status>`` chunk tags) onto real todos — a **direction** is a
todo ``part-of`` the quest (a root) or nested under a direction by
``refs.parent_id`` (a strand is a direction with children). Directions are
NOT work: the rotation fence (``store._todo_sql.direction_todo_sql``)
keeps them out of ``view='doable'``/``'queue'``, the nursery sweep, the
todo count surfaces, and ``gaps.quest_momentum``'s ``open_todo_servers``.

This module also owns the ledger's **text logic** — the in-memory forest
(:class:`AttemptNode`), the quoted-text addressing (:func:`_match_nodes`),
the near-duplicate upsert machinery (:func:`_find_near_dup_node`,
:func:`_is_near_dup`, :func:`_elements_conflict`), the do-not-repropose
render (:func:`ledger_do_not_repropose` / :func:`_do_not_repropose_lines`)
and the bounded open-list render (:func:`ledger_open_nodes` + the
:data:`_OPEN_LEDGER_BUDGET_CHARS_DEFAULT` budget). :mod:`precis.quest.dossier`
re-imports all of it — its storage (chunks, pinned tags, ``add_attempt`` /
``mark_attempt``) is unchanged and keeps working for unflagged quests.

**Status mapping** (the boundary of the module): the model speaks the
ledger's vocabulary — ``open``/``active``/``tried``/``ruled-out`` — while
the todo row stores the closed todo ``STATUS`` axis
(``store/types.py::_KIND_STATUS_VALUES``, migration 0176). The maps are
:data:`LEDGER_TO_TODO_STATUS` (write direction; ``idea`` clamps to
``open``, unknowns clamp to ``open`` like ``add_attempt`` always did) and
:data:`TODO_TO_LEDGER_STATUS` (read direction; ``blocked``/``paused`` stay
on the open list so the model still sees the direction, ``auto-timeout``
maps to ``tried`` only so the reverse map is total — no direction carries
``meta.auto_check``). The four statuses the ledger uses round-trip
exactly.

The ``nest`` op has two forms (:func:`nest_direction` moves a direction
under an existing strand; :func:`open_umbrella` mints a new umbrella
strand todo and moves ≥ 3 members under it — Reto 2026-10-24, opened by
the tick above 40 live childless roots).

``quest_evidence`` is the single door every reader of a quest's ``serves``
set goes through after slice 5: ``serves`` the quest OR any todo in its
direction tree (evidence re-homed onto a direction still counts).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.store import Tag

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.types import Ref

#: The relation a direction root carries to its quest (child → parent).
#: Registered by migration 0095 for components, constraint-free on
#: todo → quest — pinned by
#: ``test_part_of_todo_to_quest_passes_constraints``
#: (``tests/test_relation_constraints.py``).
_DIRECTION_RELATION = "part-of"

# ── the status maps (module boundary) ─────────────────────────────

#: The pinned ledger's node statuses (dossier-hygiene design). ``open`` is
#: the default for a freshly added node; ``active`` marks a direction
#: currently being pursued; ``tried``/``ruled-out`` are both "do not
#: re-propose" — the model-safe vocabulary, and every clamp target for a
#: caller-supplied status that doesn't match falls back to ``"open"``.
_STATUSES: tuple[str, ...] = ("open", "active", "tried", "ruled-out")
#: Statuses that mean "don't re-propose this direction" — both an own status
#: and (rendering-level, see :func:`ledger_do_not_repropose`) an inherited one.
_DEAD_STATUSES = frozenset({"tried", "ruled-out"})
#: The precedence order a near-duplicate merge advances ALONG (never
#: backwards) — :data:`_STATUSES`' own order, ``open < active < tried <
#: ruled-out``. Used by :func:`add_direction`'s upsert path (and
#: ``dossier.add_attempt``'s): merging a near-dup ``add`` whose status is
#: further along than the matched node's own advances it there; a status
#: earlier in this order never regresses the matched node.
_STATUS_RANK: dict[str, int] = {s: i for i, s in enumerate(_STATUSES)}

#: Ledger/model-side status → todo ``STATUS`` (spec §Status mapping):
#: ``open``→open, ``active``→doing, ``tried``→done, ``ruled-out``→won't-do,
#: ``idea`` (registered, never written) → open. Anything else clamps to
#: ``open`` — the same clamp ``add_attempt`` applied to unknown statuses.
LEDGER_TO_TODO_STATUS: dict[str, str] = {
    "open": "open",
    "active": "doing",
    "tried": "done",
    "ruled-out": "won't-do",
    "idea": "open",
}

#: Todo ``STATUS`` → the model-side status a direction renders with:
#: ``open``/``blocked``/``paused`` → open (blocked/paused are human- or
#: agent-set and stay on the open list so the model sees the direction and
#: does not re-add it), ``doing`` → active, ``done``/``auto-timeout`` →
#: tried, ``won't-do`` → ruled-out. Total over the closed todo vocabulary.
TODO_TO_LEDGER_STATUS: dict[str, str] = {
    "open": "open",
    "blocked": "open",
    "paused": "open",
    "doing": "active",
    "done": "tried",
    "auto-timeout": "tried",
    "won't-do": "ruled-out",
}

# ── the in-memory forest ──────────────────────────────────────────


@dataclass
class AttemptNode:
    """One node of an attempt tree — the pinned ledger's (dossier storage)
    or a quest's direction forest (:func:`read_direction_forest`).

    ``status`` is always the node's own STORED fact — never overwritten by a
    ruled-out ancestor (that shadowing, plus the dead-subtree collapse, is
    rendering-level only, see :func:`ledger_do_not_repropose`). For a
    direction todo it is the MODEL-side status (:data:`TODO_TO_LEDGER_STATUS`
    applied at load; the stored ``STATUS`` tag carries the todo value).
    ``children`` are refinements/variants of this direction. ``handle`` is
    the node's own chunk handle (``meta.pinned='ledger-node'``) when loaded
    from a dossier (:func:`dossier._load_ledger_nodes`), the ``td<N>``
    handle when loaded from todos; ``None`` only for a node that exists
    solely as a parsed-but-not-yet-written :class:`AttemptNode` (the
    legacy-markdown parse result before
    :func:`dossier._migrate_legacy_ledger` materializes it). ``seq`` is a
    monotonic recency proxy — the node chunk's own ``chunk_id`` on dossier
    storage (higher = created later; a node's ``chunk_id`` never changes
    across a `mark`, only its ``ATTEMPT:`` tag does, so this reflects
    add-order, not last-marked time), the todo's ``ref_id`` for a
    direction, or a parse-order counter for a node materialized from
    legacy markdown text (:func:`_parse_ledger`) that has no chunk_id yet.
    Used by :func:`ledger_open_nodes` to prioritize which open directions
    survive its budget cutoff (gr263256). ``ref_id`` is the todo's ref id
    for a direction (``None`` on dossier-loaded nodes — those are chunk
    addressed). ``parent_unresolved`` is the ``parent`` text an ``add``
    named but that matched zero or several nodes, so the node was kept as
    a root (``meta.parent_unresolved``; gr462891) — ``None`` for a node
    placed as asked. :func:`ledger_open_nodes` shows it to the model.
    """

    text: str
    status: str
    children: list[AttemptNode] = field(default_factory=list)
    handle: str | None = None
    seq: int = 0
    parent_unresolved: str | None = None
    ref_id: int | None = None


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


# ── the markdown serialization (the dossier ledger's text shape) ───

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
#: storage nesting is real ``parent_chunk_id`` on the dossier,
#: ``refs.parent_id`` on directions).
_INDENT_WIDTH = 2


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

    Each node's :attr:`AttemptNode.seq` is a simple top-to-bottom parse-order
    counter (this text form carries no real ``chunk_id``) — later bullets get
    a higher ``seq``, the same "more recent" direction storage-backed nodes
    use (``dossier._load_ledger_nodes``).
    """
    roots: list[AttemptNode] = []
    mode: str | None = None  # "tree" | a legacy status | None (unrecognised)
    stack: list[tuple[int, AttemptNode]] = []
    next_seq = 0
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
            node = AttemptNode(text=node_text, status=status, children=[], seq=next_seq)
            next_seq += 1
            while stack and stack[-1][0] >= depth:
                stack.pop()
            (stack[-1][1].children if stack else roots).append(node)
            stack.append((depth, node))
        else:
            roots.append(
                AttemptNode(text=bullet, status=mode, children=[], seq=next_seq)
            )
            next_seq += 1
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


def _normalize_node_text(text: str) -> str:
    """Trim AND collapse all internal whitespace — including embedded
    newlines — to single spaces.

    Guards :func:`_render_ledger`'s tick-prompt projection: it writes a
    node's text verbatim after its ``- [status] `` prefix, so an unmangled
    embedded newline (raw untrusted model JSON via the tick's ``ledger_ops``)
    would render as an extra bullet line a model could mistake for a real
    sibling. Applied at both the storage boundary (:func:`add_direction`'s /
    ``dossier.add_attempt``'s stored text) and the match boundary
    (:func:`_match_nodes`) — a node's stored text never contains a newline.
    """
    return " ".join(text.split())


# ── quoted-text addressing ─────────────────────────────────────────

#: Shortest quoted prefix :func:`_match_nodes` will resolve when no node
#: matches exactly. Shorter prefixes are too likely to hit several nodes, and
#: a short quote is not evidence of which node the model meant.
_PREFIX_MATCH_MIN_CHARS = 40


def _prefix_query(text: str) -> str | None:
    """``text`` normalized for the prefix pass — casefolded, a trailing
    ``…``/``...`` and whitespace stripped — or ``None`` when what remains is
    shorter than :data:`_PREFIX_MATCH_MIN_CHARS`."""
    q = _normalize_node_text(text).casefold()
    for ellipsis in ("…", "..."):
        if q.endswith(ellipsis):
            q = q[: -len(ellipsis)]
            break
    q = q.rstrip()
    return q if len(q) >= _PREFIX_MATCH_MIN_CHARS else None


def _resolve_text(
    pairs: list[tuple[AttemptNode, AttemptNode | None]], text: str
) -> list[tuple[AttemptNode, AttemptNode | None]]:
    """``pairs`` whose node text equals ``text`` (normalized, casefolded);
    only if none do, those that START with it (see :func:`_prefix_query`)."""
    target = _normalize_node_text(text).casefold()
    exact = [
        (n, p) for n, p in pairs if _normalize_node_text(n.text).casefold() == target
    ]
    if exact:
        return exact
    prefix = _prefix_query(text)
    if prefix is None:
        return []
    return [
        (n, p)
        for n, p in pairs
        if _normalize_node_text(n.text).casefold().startswith(prefix)
    ]


def _match_nodes(
    roots: list[AttemptNode], text: str, parent: str | None = None
) -> list[AttemptNode]:
    """Nodes whose text matches ``text`` — trimmed, whitespace-collapsed,
    case-insensitive (the addressing rule: quoted
    node text, no id bookkeeping, because the model sees the ledger in its
    prompt and can quote it; :func:`_normalize_node_text` guards
    against an embedded newline forging a bullet-line match). Exact equality
    wins; only when nothing matches exactly does a unique-prefix pass run: a
    quote of at least :data:`_PREFIX_MATCH_MIN_CHARS` chars (a trailing
    ``…``/``...`` ignored) matches nodes whose text starts with it — models
    routinely quote the head of a long node. ``parent`` narrows to nodes
    whose immediate parent's text also matches (same two-pass rule), the
    documented disambiguator when the same text appears in two branches.
    Zero or >1 matches is the caller's cue to no-op (ambiguous/unmatched —
    never a guess)."""
    matches = _resolve_text(_flatten_with_parent(roots), text)
    if parent is not None:
        # Resolve the parent text over every node, then keep the candidates
        # whose immediate parent is one of the resolved nodes.
        parents = {
            id(n) for n, _p in _resolve_text(_flatten_with_parent(roots), parent)
        }
        matches = [(n, p) for n, p in matches if p is not None and id(p) in parents]
    return [n for n, _p in matches]


# ── near-duplicate detection (the upsert machinery) ───────────────

#: Jaccard overlap of significant tokens above which two ledger-node texts
#: (or two hypotheses, :mod:`precis.quest.tick`'s own use of this same
#: pair) count as "the same thought restated" — shared with
#: :mod:`precis.quest.tick`'s hypothesis-dedup (``tick.py`` imports
#: :func:`_sig_tokens`/:func:`_is_near_dup`/this constant from here rather
#: than keeping its own copy — a prod dossier had ~8 near-copies of the
#: same "identify rate-limiting step / side product / poison" ledger
#: entries, the same spin the hypothesis-dedup constant was already tuned
#: for). ``0.6`` on purpose ties BOTH call sites to one knob.
_HYP_DUP_JACCARD = 0.6


def _sig_tokens(text: str) -> set[str]:
    """Lowercased word tokens ≥4 chars — a cheap topical fingerprint.

    The length floor is what lets a short label (``"c"``, ``"path 1"``) —
    common in tests and in a model's terse sibling-variant names — carry
    NO significant tokens at all, so :func:`_is_near_dup` never merges two
    such labels just because they share a short common word. It's also why
    two same-named nodes in different branches (the documented
    ``parent=``-disambiguated case) stay distinct under the global
    near-dup scan in :func:`add_direction` / ``dossier.add_attempt``.
    """
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 4}


#: Two-letter chemical symbols recognised by :func:`_element_signature`,
#: minus the ones that collide with common English words as capitalised
#: sentence-initial tokens (In, As, At, No, Be, He). One-letter symbols
#: (H, N, O, C, …) are excluded wholesale — in NO→NH₃ prose they name
#: reagent atoms, not the candidate's distinguishing dopant. Two texts
#: whose element signatures are non-empty and UNEQUAL are never near-dups,
#: however high their token Jaccard: :func:`_sig_tokens` drops all <4-char
#: words, so without this guard "Rh substitutional SAA on Pd(111)" and
#: "Ru substitutional SAA on Pd(111)" — distinct candidates — fingerprint
#: identically (a real qu164903 dedup dry-run clustered Rh/Ru/Fe/Co into
#: one node, and Au with Ag). Unequal-but-overlapping sets ({Ag,Cu,Zn} vs
#: {Ag,Cu,Zn,Au}) also stay distinct — conservative on purpose: a missed
#: merge is clutter, a wrong merge silently deletes a branch's identity.
_ELEMENT_SYMBOLS = frozenset(
    # Deliberately incomplete — In, Y, W are excluded: they collide with
    # common English words as capitalised tokens (see docstring above).
    [
        "Li",
        "Ne",
        "Na",
        "Mg",
        "Al",
        "Si",
        "Cl",
        "Ar",
        "Ca",
        "Sc",
        "Ti",
        "Cr",
        "Mn",
        "Fe",
        "Co",
        "Ni",
        "Cu",
        "Zn",
        "Ga",
        "Ge",
        "Se",
        "Br",
        "Kr",
        "Rb",
        "Sr",
        "Zr",
        "Nb",
        "Mo",
        "Tc",
        "Ru",
        "Rh",
        "Pd",
        "Ag",
        "Cd",
        "Sn",
        "Sb",
        "Te",
        "Xe",
        "Cs",
        "Ba",
        "La",
        "Ce",
        "Pr",
        "Nd",
        "Pm",
        "Sm",
        "Eu",
        "Gd",
        "Tb",
        "Dy",
        "Ho",
        "Er",
        "Tm",
        "Yb",
        "Lu",
        "Hf",
        "Ta",
        "Re",
        "Os",
        "Ir",
        "Pt",
        "Au",
        "Hg",
        "Tl",
        "Pb",
        "Bi",
        "Po",
        "Rn",
        "Fr",
        "Ra",
        "Ac",
        "Th",
        "Pa",
        "Np",
        "Pu",
        "Am",
        "Cm",
        "Bk",
        "Cf",
        "Es",
        "Fm",
        "Md",
        "Lr",
        "Rf",
        "Db",
        "Sg",
        "Bh",
        "Hs",
        "Mt",
        "Ds",
        "Rg",
        "Cn",
        "Nh",
        "Fl",
        "Mc",
        "Lv",
        "Ts",
        "Og",
    ]
)


def _element_signature(text: str) -> frozenset[str]:
    """Case-sensitive two-letter element symbols appearing as standalone
    word tokens in ``text`` (``"Rh-sub on Pd(111)"`` → ``{"Rh", "Pd"}``).
    See :data:`_ELEMENT_SYMBOLS` for what's deliberately excluded."""
    return frozenset(re.findall(r"\b[A-Z][a-z]\b", text or "")) & _ELEMENT_SYMBOLS


def _elements_conflict(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` name different chemistry — both carry a
    non-empty element signature and the signatures differ — which vetoes a
    near-dup match regardless of token overlap."""
    ea, eb = _element_signature(a), _element_signature(b)
    return bool(ea) and bool(eb) and ea != eb


def _is_near_dup(text: str, existing: list[str]) -> bool:
    """True when ``text`` restates any of ``existing`` (token Jaccard ≥
    :data:`_HYP_DUP_JACCARD`, and no element-signature conflict —
    :func:`_elements_conflict`). A ``text`` with no significant tokens
    (:func:`_sig_tokens`) never matches anything — see that function's
    docstring."""
    toks = _sig_tokens(text)
    if not toks:
        return False
    for other in existing:
        ot = _sig_tokens(other)
        if not ot:
            continue
        if _elements_conflict(text, other):
            continue
        inter = len(toks & ot)
        union = len(toks | ot)
        if union and inter / union >= _HYP_DUP_JACCARD:
            return True
    return False


def _find_near_dup_node(roots: list[AttemptNode], text: str) -> AttemptNode | None:
    """The ledger-wide (not sibling-scoped) node whose text is the closest
    near-duplicate of ``text``, or ``None`` when nothing crosses
    :data:`_HYP_DUP_JACCARD`. Unlike :func:`_match_nodes` (exact-text
    addressing, used for `parent`/`node` lookups), this scans EVERY node in
    the forest regardless of branch — :func:`add_direction`'s /
    ``dossier.add_attempt``'s upsert path (dedup-before-insert): a
    rephrased repeat of an already-pinned direction, wherever it lives,
    should merge into it rather than mint a sibling. Ties broken by highest
    overlap ratio; a node with no significant tokens (:func:`_sig_tokens`)
    is never a candidate on either side of the comparison, so two short
    same-named siblings in different branches (the
    ``parent=``-disambiguated case) are untouched. A node whose element
    signature conflicts with ``text``'s (:func:`_elements_conflict` — e.g.
    an Rh branch vs a Ru attempt) is likewise never a candidate, however
    similar the surrounding prose.
    """
    toks = _sig_tokens(text)
    if not toks:
        return None
    best: tuple[float, AttemptNode] | None = None
    for n, _parent in _flatten_with_parent(roots):
        ntoks = _sig_tokens(n.text)
        if not ntoks:
            continue
        if _elements_conflict(text, n.text):
            continue
        inter = len(toks & ntoks)
        union = len(toks | ntoks)
        ratio = inter / union if union else 0.0
        if ratio >= _HYP_DUP_JACCARD and (best is None or ratio > best[0]):
            best = (ratio, n)
    return best[1] if best is not None else None


# ── the do-not-repropose render ────────────────────────────────────


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
    status is untouched (``mark_attempt``/``mark_direction`` never rewrite a
    descendant). A subtree that is entirely ``tried``/``ruled-out``
    collapses to its root line plus a variant count (e.g. ``… (4 variants,
    all ruled out)``) — the full per-node detail survives in the graph
    (the todo tree) and the logbook, just not repeated here every tick.
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
    """The "do NOT re-propose these directions" prompt block
    — tried/ruled-out nodes (own or inherited from a ruled-out ancestor),
    with a fully-dead subtree collapsed to one summary line (see
    :func:`_do_not_repropose_lines`). ``open``/``active`` directions with no
    ruled-out ancestor are excluded — those are the exploration queue, not a
    constraint. ``"(nothing pinned yet)"`` when nothing qualifies.

    Takes the forest directly (the primary form — matches storage, no
    markdown round-trip needed; the direction forest is
    :func:`read_direction_forest`'s return). Also accepts the legacy
    markdown TEXT (``dossier.read_ledger``'s return shape) as a thin
    back-compat shim — :mod:`precis.quest.tick`'s ``_ledger_constraints``
    still calls this with ``read_ledger``'s text, so that call site needed
    no change when the ledger's storage moved off markdown.
    """
    roots = _parse_ledger(ledger) if isinstance(ledger, str) else ledger
    lines = _do_not_repropose_lines(roots)
    return "\n".join(lines) if lines else "(nothing pinned yet)"


# ── the open-list render (budgeted by node count) ──────────────────

#: Env var overriding :data:`_OPEN_LEDGER_BUDGET_CHARS_DEFAULT` — mirrors the
#: ``PRECIS_QUEST_*`` budget-knob convention used elsewhere in the package
#: (:mod:`precis.quest.allocator`, :mod:`precis.quest.cascade`,
#: :mod:`precis.quest.tick`).
_OPEN_LEDGER_BUDGET_ENV = "PRECIS_QUEST_LEDGER_OPEN_BUDGET_CHARS"
#: Default char budget for the WHOLE rendered open-ledger section
#: (:func:`ledger_open_nodes`'s return value) — generous: a full,
#: untruncated node line is well under 200 chars for a typical direction, so
#: this comfortably fits 60+ nodes before cutting off, while still bounding
#: the pathological case that motivated this budget (gr263256: 188 open
#: nodes, this section alone 55.6k chars of a 90.3k-char prompt, because the
#: OLD per-node truncation made most nodes un-quotable — see
#: :func:`ledger_open_nodes`'s docstring). Cuts by NODE COUNT, never by
#: truncating a node's own text — the fix's whole point is that what renders
#: here must be exactly what :func:`_match_nodes` will accept (a unique
#: prefix of at least :data:`_PREFIX_MATCH_MIN_CHARS` chars also resolves).
_OPEN_LEDGER_BUDGET_CHARS_DEFAULT = 12_000


#: Longest ``parent_unresolved`` text :func:`ledger_open_nodes` echoes in an
#: ``(unplaced: …)`` marker — enough to recognise the intended parent without
#: spending the open-list budget on a second copy of a long node.
_UNPLACED_PARENT_SHOW_CHARS = 120


def _open_ledger_budget_chars() -> int:
    """:data:`_OPEN_LEDGER_BUDGET_ENV`, parsed as an int, falling back to
    :data:`_OPEN_LEDGER_BUDGET_CHARS_DEFAULT` when unset or non-numeric."""
    raw = os.environ.get(_OPEN_LEDGER_BUDGET_ENV)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _OPEN_LEDGER_BUDGET_CHARS_DEFAULT


def ledger_open_nodes(ledger: list[AttemptNode] | str) -> str:
    """The ``open``/``active`` directions — a compact status+text bullet
    per node — the upsert counterpart to
    :func:`ledger_do_not_repropose`'s tried/ruled-out list.

    Model-facing purpose: the proposer only ever saw the tried/ruled-out
    constraint, so an already-pinned OPEN direction was invisible to it —
    "when in doubt, add" then meant a rephrased repeat of something already
    on the ledger (dossier-hygiene design's motivating prod defect: ~8
    near-copies of the same "identify rate-limiting step / side
    product / poison" entries). Showing the open queue lets the model
    `mark`/refine an existing node instead of re-adding it —
    :func:`add_direction`'s near-dup merge (see its docstring) already
    catches the case where the model adds one anyway, so this is a
    prompt-quality aid, not the correctness backstop.

    **A node's text is NEVER truncated here** (gr263256 fix — this function
    used to cut every node to 140 chars, but `mark`/`add` addressing
    (:func:`_match_nodes`) requires the model to quote a node's text back
    EXACTLY: truncating the very thing that must be quotable made most
    nodes (78% of open nodes on the audited quest, all longer than 140
    chars) permanently unaddressable — every `mark` targeting one silently
    no-opped, so the model just re-added the same direction as a new node
    every tick instead, and the section accreted (measured: 188 open nodes,
    55.6k of a 90.3k-char prompt). What renders here must always be exactly
    what :func:`_match_nodes` will accept (a unique prefix of at least
    :data:`_PREFIX_MATCH_MIN_CHARS` chars also resolves).

    Instead, **the budget is on node COUNT, not text length**
    (:data:`_OPEN_LEDGER_BUDGET_CHARS_DEFAULT`, env-tunable via
    :data:`_OPEN_LEDGER_BUDGET_ENV`) — mirrors the fill-to-budget/report-the-
    remainder shape :mod:`precis.quest.tick`'s `_served_papers_detail` uses
    for the literature section (commit d8e2ddc4), applied to whole nodes
    here rather than truncated snippets. Nodes are ordered by priority
    before filling: ``active`` before ``open`` (a direction currently being
    pursued is the one most likely to need a `mark` this tick), then
    most-recently-added first within a status tier (:attr:`AttemptNode.seq`
    — newer directions are the freshest ask and the likeliest to otherwise
    get re-proposed if dropped from view). At least one node always shows
    when any open/active node exists, even if it alone exceeds the budget
    (avoids a false-empty section). When the budget cuts a node off
    entirely, a trailing line reports how many were omitted — worded to
    discourage re-adding: an omitted node is simply not addressable THIS
    tick, which is still strictly better than the old truncate-but-show
    behaviour (visible yet equally unaddressable).

    A node kept as a root because its ``add`` named an unresolvable ``parent``
    (:attr:`AttemptNode.parent_unresolved`) carries an `` (unplaced: meant
    under "…")`` suffix after its text, so the model can re-home it.

    Takes the forest directly or the legacy markdown text, mirroring
    :func:`ledger_do_not_repropose`. ``"(none yet)"`` when nothing
    qualifies.
    """
    roots = _parse_ledger(ledger) if isinstance(ledger, str) else ledger
    open_nodes = [
        n
        for n, _parent in _flatten_with_parent(roots)
        if n.status in ("open", "active")
    ]
    if not open_nodes:
        return "(none yet)"
    ordered = sorted(
        open_nodes, key=lambda n: (0 if n.status == "active" else 1, -n.seq)
    )
    budget = _open_ledger_budget_chars()
    lines: list[str] = []
    used = 0
    shown = 0
    for n in ordered:
        line = f"- [{n.status}] {n.text}"
        if n.parent_unresolved:
            # Suffix, outside the quotable node text (like the status prefix):
            # tells the model this root was meant to be a child.
            meant = n.parent_unresolved
            if len(meant) > _UNPLACED_PARENT_SHOW_CHARS:
                meant = meant[:_UNPLACED_PARENT_SHOW_CHARS].rstrip() + "…"
            line += f' (unplaced: meant under "{meant}")'
        if shown and used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    omitted = len(ordered) - shown
    if omitted > 0:
        plural = "" if omitted == 1 else "s"
        lines.append(
            f"(+{omitted} more open direction{plural} pinned but not shown this "
            "tick for space — they still exist and are still open; do NOT "
            're-add them or treat their absence here as "not yet raised". '
            "They are not addressable THIS tick (you cannot `mark` a node you "
            "cannot see), but will resurface in a future tick as budget allows.)"
        )
    return "\n".join(lines)


# ── todo-backed directions ─────────────────────────────────────────

#: A ``td<N>`` handle — the model's other way of addressing a direction
#: (the tick's ``direction_ops`` speak ``td`` handles or quoted text).
_TODO_HANDLE_RE = re.compile(r"^td(\d+)$")


def direction_ref_ids(store: Store, quest_id: int) -> set[int]:
    """The ref ids of ``quest_id``'s direction tree: every live todo
    ``part-of`` the quest (either stored orientation of the
    ``part-of``/``contains`` pair) plus its ``parent_id`` descendants.
    Empty set for a quest with no directions. This is the per-quest
    read the SQL predicate ``store._todo_sql.direction_todo_sql`` applies
    globally (the rotation fence) — the two must stay semantically equal.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE roots(ref_id) AS (
                SELECT l.src_ref_id FROM links l
                  JOIN refs s ON s.ref_id = l.src_ref_id
                 WHERE l.relation = 'part-of' AND l.dst_ref_id = %s
                   AND s.kind = 'todo' AND s.retired_at IS NULL
                UNION
                SELECT l.dst_ref_id FROM links l
                  JOIN refs s ON s.ref_id = l.dst_ref_id
                 WHERE l.relation = 'contains' AND l.src_ref_id = %s
                   AND s.kind = 'todo' AND s.retired_at IS NULL
            ), tree(ref_id, parent_id) AS (
                SELECT ref_id, NULL::bigint FROM roots
                UNION
                SELECT c.ref_id, c.parent_id FROM refs c
                  JOIN tree t ON c.parent_id = t.ref_id
                 WHERE c.kind = 'todo' AND c.retired_at IS NULL
            )
            SELECT ref_id, parent_id FROM tree
            """,
            (quest_id, quest_id),
        ).fetchall()
    return {int(r[0]) for r in rows}


def _direction_statuses(store: Store, ref_ids: list[int]) -> dict[int, str]:
    """``ref_id -> model-side status`` for the direction todos, read from
    the ``STATUS`` tag in one query and mapped through
    :data:`TODO_TO_LEDGER_STATUS`. An untagged todo reads ``open`` (the
    same default every todo surface uses)."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rt.ref_id, t.value FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = ANY(%s) AND t.namespace = 'STATUS'",
            (ref_ids,),
        ).fetchall()
    raw = {int(r[0]): str(r[1]) for r in rows}
    return {
        rid: TODO_TO_LEDGER_STATUS.get(raw.get(rid, "open"), "open") for rid in ref_ids
    }


def read_direction_forest(store: Store, quest_id: int) -> list[AttemptNode]:
    """The quest's live direction forest as :class:`AttemptNode` roots —
    the todo-backed counterpart of ``dossier._load_ledger_nodes``.

    Roots are the todos ``part-of`` the quest (children nested by
    ``refs.parent_id``); a member whose ``parent_id`` points outside the
    direction set (or nowhere) reads as a root even without its own
    ``part-of`` edge. Each node's ``status`` is MODEL-side
    (:data:`TODO_TO_LEDGER_STATUS`), ``handle`` the ``td<N>`` handle,
    ``seq``/``ref_id`` the ref id, ``parent_unresolved`` from
    ``meta.parent_unresolved``. Children are ordered by ref id (creation
    order — the same recency direction ``_load_ledger_nodes``'s
    ``seq=chunk_id`` gives the dossier forest).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE roots(ref_id) AS (
                SELECT l.src_ref_id FROM links l
                  JOIN refs s ON s.ref_id = l.src_ref_id
                 WHERE l.relation = 'part-of' AND l.dst_ref_id = %s
                   AND s.kind = 'todo' AND s.retired_at IS NULL
                UNION
                SELECT l.dst_ref_id FROM links l
                  JOIN refs s ON s.ref_id = l.dst_ref_id
                 WHERE l.relation = 'contains' AND l.src_ref_id = %s
                   AND s.kind = 'todo' AND s.retired_at IS NULL
            ), tree(ref_id, parent_id) AS (
                SELECT ref_id, NULL::bigint FROM roots
                UNION
                SELECT c.ref_id, c.parent_id FROM refs c
                  JOIN tree t ON c.parent_id = t.ref_id
                 WHERE c.kind = 'todo' AND c.retired_at IS NULL
            )
            -- A member that keeps its own part-of edge after being nested
            -- under a strand emits TWO tree rows (seed NULL-parent + the
            -- real child row); DISTINCT ON keeps one, and the
            -- (parent_id IS NULL) sort makes the real parent win.
            SELECT DISTINCT ON (t.ref_id) t.ref_id, t.parent_id, r.title, r.meta
              FROM tree t JOIN refs r ON r.ref_id = t.ref_id
             ORDER BY t.ref_id, (t.parent_id IS NULL)
            """,
            (quest_id, quest_id),
        ).fetchall()
    if not rows:
        return []
    ids = [int(r[0]) for r in rows]
    statuses = _direction_statuses(store, ids)
    members = set(ids)
    by_id: dict[int, AttemptNode] = {}
    for rid, _pid, title, meta in rows:
        rid = int(rid)
        by_id[rid] = AttemptNode(
            text=str(title or ""),
            status=statuses.get(rid, "open"),
            children=[],
            handle=f"td{rid}",
            seq=rid,
            parent_unresolved=(meta or {}).get("parent_unresolved") or None,
            ref_id=rid,
        )
    # A member can arrive twice: once as a ``part-of`` seed (pid NULL) and
    # once by its real ``parent_id`` — e.g. a direction that was nested
    # under a strand keeps its root edge. The real parent wins.
    attached: dict[int, int] = {
        int(rid): int(pid)
        for rid, pid, _t, _m in rows
        if pid is not None and int(pid) in members
    }
    roots: list[AttemptNode] = []
    for rid in sorted(by_id):
        node = by_id[rid]
        pid = attached.get(rid)
        if pid is None:
            roots.append(node)
        else:
            by_id[pid].children.append(node)
    return roots


def _set_direction_status(store: Store, node: AttemptNode, status: str) -> None:
    """Write ``status`` (model-side) onto ``node``'s todo as its ``STATUS``
    tag — the storage primitive behind :func:`mark_direction` and
    :func:`add_direction`'s near-dup status-advance merge (ref-addressed,
    so it can update the exact matched node without re-running
    :func:`_match_nodes` — the same reason ``dossier._set_node_status``
    is chunk-addressed)."""
    assert node.ref_id is not None  # every direction node carries a ref_id
    store.add_tag(
        node.ref_id,
        Tag.closed("STATUS", LEDGER_TO_TODO_STATUS[status]),
        replace_prefix=True,
    )


def _resolve_node(forest: list[AttemptNode], node: str) -> AttemptNode | None:
    """The one forest node ``node`` addresses — a ``td<N>`` handle matched
    on ref id, else quoted text through :func:`_match_nodes` (exact, then
    unique ≥40-char prefix). ``None`` on zero or ambiguous matches —
    degrade-don't-crash, never a guess."""
    m = _TODO_HANDLE_RE.match(node.strip())
    if m:
        rid = int(m.group(1))
        for n, _p in _flatten_with_parent(forest):
            if n.ref_id == rid:
                return n
        return None
    matches = _match_nodes(forest, node)
    return matches[0] if len(matches) == 1 else None


def add_direction(
    store: Store,
    quest_id: int,
    text: str,
    parent: str | None = None,
    status: str = "open",
) -> bool:
    """Add one direction todo to the quest's attempt tree; return ``True``
    iff a NEW todo was minted. The todo-backed counterpart of
    ``dossier.add_attempt`` — same contract, same docstring rules.

    Blank ``text`` is a no-op. ``status`` is MODEL-side
    (``open``/``active``/``tried``/``ruled-out``); anything else clamps to
    ``"open"``, then maps through :data:`LEDGER_TO_TODO_STATUS` onto the
    todo's ``STATUS`` tag.

    **Dedup-before-insert.** The WHOLE forest (every branch, not just
    ``parent``'s siblings) is scanned for a near-dup of ``text``
    (:func:`_find_near_dup_node`). A hit UPSERTS, never appends: same
    status → no-op; different status → advances the match to the incoming
    status only if further along :data:`_STATUS_RANK` — never regresses.
    A match ignores the requested ``parent`` (reused where it already
    lives, never re-parented). A node with no significant tokens
    (:func:`_sig_tokens`, e.g. ``"c"``) never near-dup-matches, so
    same-named leaves in different branches stay distinct via ``parent=``
    disambiguation (:func:`_match_nodes`).

    Absent a near-dup, the narrower guard still applies: exact
    byte-identical text+status among ``parent``'s siblings is also a no-op.
    ``parent`` (optional) addresses an existing node — a ``td<N>`` handle
    or quoted text (:func:`_resolve_node`). A ``parent`` that matches zero
    or >1 nodes is NOT a reason to drop the direction: it is added as a
    root instead (logged) — dropping lost the direction and
    cascade-failed every later op naming it (gr462891) — and its
    ``meta.parent_unresolved`` keeps the intended parent for the open
    list's ``(unplaced: …)`` marker. ``parent=None`` adds a root:
    ``parent_id`` NULL, ``part-of`` the quest through the checked link
    door (:func:`apply_link_ops` — the relation's acyclic walk refuses a
    placement that would loop the quest under its own direction).

    ``text`` is whitespace-normalized (:func:`_normalize_node_text`)
    before storage or comparison — a direction todo's title never
    contains a newline. The write runs the todo guards
    (:func:`guards.check_depth_under`) on a nested add, so the
    ``MAX_DEPTH`` wall is the same one every todo respects.
    """
    from precis.todo import guards
    from precis.utils.link_tag_ops import apply_link_ops

    stripped_text = _normalize_node_text(text)
    if not stripped_text:
        return False
    st = status if status in _STATUSES else "open"
    roots = read_direction_forest(store, quest_id)

    near_dup = _find_near_dup_node(roots, stripped_text)
    if near_dup is not None:
        if st != near_dup.status and _STATUS_RANK[st] > _STATUS_RANK[near_dup.status]:
            _set_direction_status(store, near_dup, st)
        return False

    parent_id: int | None = None
    unresolved: str | None = None
    target_node: AttemptNode | None = None
    if parent is not None:
        target_node = _resolve_node(roots, parent)
        if target_node is not None:
            parent_id = target_node.ref_id
        else:
            unresolved = _normalize_node_text(parent) or None
            log.info(
                "add_direction: quest %s parent %r unresolved; added as a root",
                quest_id,
                parent,
            )
    target_children = target_node.children if target_node is not None else roots
    if any(n.text == stripped_text and n.status == st for n in target_children):
        return False
    if parent_id is not None:
        guards.check_depth_under(store, parent_id)

    meta: dict[str, Any] = {}
    if unresolved:
        meta["parent_unresolved"] = unresolved
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=stripped_text,
        parent_id=parent_id,
        meta=meta,
    )
    store.add_tag(ref.id, Tag.closed("STATUS", LEDGER_TO_TODO_STATUS[st]))
    if parent_id is None:
        apply_link_ops(
            store,
            ref.id,
            link=f"quest:{quest_id}",
            unlink=None,
            rel=_DIRECTION_RELATION,
        )
    return True


def mark_direction(
    store: Store,
    quest_id: int,
    node: str,
    status: str,
    parent: str | None = None,
) -> bool:
    """Set an existing direction's status; return ``True`` iff applied.
    The todo-backed counterpart of ``dossier.mark_attempt``.

    ``node`` is a ``td<N>`` handle or quoted text — trimmed,
    whitespace-normalized, case-insensitive, with the unique-prefix
    fallback (:func:`_resolve_node` / :func:`_match_nodes`); ``parent``
    disambiguates the quoted-text form when the same text appears in two
    branches. Zero or >1 matches, or a ``status`` outside
    :data:`_STATUSES`, is a no-op — degrade-don't-crash, never a guess.
    Only the matched todo's own ``STATUS`` changes; a descendant's is
    untouched (an inherited-ruled-out shadow, and a dead-subtree collapse,
    are both rendering-level — see :func:`ledger_do_not_repropose`).
    """
    if status not in _STATUSES:
        return False
    node_text = _normalize_node_text(node or "")
    if not node_text:
        return False
    roots = read_direction_forest(store, quest_id)
    if _TODO_HANDLE_RE.match(node_text):
        target = _resolve_node(roots, node_text)
    else:
        matches = _match_nodes(roots, node_text, parent)
        target = matches[0] if len(matches) == 1 else None
    if target is None:
        return False
    _set_direction_status(store, target, status)
    return True


def nest_direction(store: Store, quest_id: int, node: str, under: str) -> bool:
    """Move the direction ``node`` under the direction ``under`` (a strand)
    by re-pointing its ``parent_id``; return ``True`` iff applied. Both
    operands address inside the quest's forest (:func:`_resolve_node` —
    ``td<N>`` handle or quoted text). The todo guards
    (:func:`guards.check_no_cycle`, :func:`guards.check_reparent_depth`)
    refuse a loop or a move past ``MAX_DEPTH`` — same wall every todo
    re-parent respects. Unresolvable operands no-op (``False``), matching
    :func:`mark_direction`'s never-a-guess rule."""
    from precis.todo import guards

    roots = read_direction_forest(store, quest_id)
    child = _resolve_node(roots, node)
    parent = _resolve_node(roots, under)
    if child is None or parent is None or child.ref_id == parent.ref_id:
        return False
    assert child.ref_id is not None and parent.ref_id is not None
    guards.check_no_cycle(store, child_id=child.ref_id, parent_id=parent.ref_id)
    guards.check_reparent_depth(
        store, child_id=child.ref_id, new_parent_id=parent.ref_id
    )
    store.set_parent(child.ref_id, parent.ref_id)
    return True


_UMBRELLA_MIN_MEMBERS = 3


def open_umbrella(
    store: Store, quest_id: int, text: str, members: list[str]
) -> int | None:
    """Open an umbrella **strand** — a fresh direction todo ``part-of``
    the quest — and move ``members`` under it. Returns the umbrella's ref
    id, or ``None`` when fewer than :data:`_UMBRELLA_MIN_MEMBERS`
    distinct members resolve inside the quest's forest (the tick's
    ``nest`` op only opens an umbrella with ≥ 3 members — Reto
    2026-10-24). ``text`` is the umbrella direction the model authored —
    a strand title, not a synthetic label. A member that already sits
    under a strand is still moved (nest-under beats staying a stray
    root); the umbrella itself is a root like any other direction."""
    from precis.todo import guards
    from precis.utils.link_tag_ops import apply_link_ops

    stripped = _normalize_node_text(text)
    if not stripped:
        return None
    roots = read_direction_forest(store, quest_id)
    targets: dict[int, AttemptNode] = {}
    for m in members:
        node = _resolve_node(roots, m) if m else None
        if node is not None and node.ref_id is not None:
            targets[node.ref_id] = node
    if len(targets) < _UMBRELLA_MIN_MEMBERS:
        return None
    ref = store.insert_ref(kind="todo", slug=None, title=stripped)
    store.add_tag(ref.id, Tag.closed("STATUS", LEDGER_TO_TODO_STATUS["open"]))
    apply_link_ops(
        store,
        ref.id,
        link=f"quest:{quest_id}",
        unlink=None,
        rel=_DIRECTION_RELATION,
    )
    for rid in targets:
        guards.check_no_cycle(store, child_id=rid, parent_id=ref.id)
        guards.check_reparent_depth(store, child_id=rid, new_parent_id=ref.id)
        store.set_parent(rid, ref.id)
    return int(ref.id)


def open_list(store: Store, quest_id: int) -> str:
    """The quest's open/active directions rendered for the tick prompt —
    :func:`ledger_open_nodes` over :func:`read_direction_forest`, model-side
    statuses (a ``blocked``/``paused`` todo shows as ``open``; a ``doing``
    todo shows as ``active``)."""
    return ledger_open_nodes(read_direction_forest(store, quest_id))


def do_not_repropose(store: Store, quest_id: int) -> str:
    """The quest's "do NOT re-propose" list —
    :func:`ledger_do_not_repropose` over :func:`read_direction_forest`:
    ``done``/``auto-timeout`` todos read ``tried``, ``won't-do`` reads
    ``ruled-out``, an open descendant of a ruled-out strand is shadowed."""
    return ledger_do_not_repropose(read_direction_forest(store, quest_id))


def quest_evidence(store: Store, quest_id: int) -> list[Ref]:
    """Live refs that ``serves`` the quest OR any todo in its direction
    tree — the single door every reader of a quest's serves set uses once
    evidence re-homes onto directions (retire-dossier step 6, slice 5).
    A ``serves`` edge onto a direction counts exactly like one onto the
    quest, so a re-home move changes no count. Ordered by ref id."""
    ids = sorted(direction_ref_ids(store, quest_id) | {int(quest_id)})
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT src_ref_id FROM links "
            "WHERE relation = 'serves' AND dst_ref_id = ANY(%s)",
            (ids,),
        ).fetchall()
    refs = store.fetch_refs_by_ids({int(r[0]) for r in rows})
    return [
        r for i in sorted(refs) if (r := refs[i]) is not None and r.retired_at is None
    ]
