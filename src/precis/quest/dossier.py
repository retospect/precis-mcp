"""Dossier — the living research synthesis a *process* owns.

Dossier-owned-by-process (quest layer slice 4a): a dossier belongs to a
**process, never an artifact** — ``owner_id`` is any ref (a quest today, but
also a standing topic review or a paper-writing pipeline); a "paper" is a
render/export of a process's dossier. The ``dossier-of``/``has-dossier``
relation carries no kind constraint (migration 0067) — widening the owner
set is migration-free, the coupling lives only in this module's Python.

An owning process keeps *two* records: the append-only ``quest_log``
LOGBOOK (episodic, immutable; :mod:`precis.quest.logbook`) and the DOSSIER —
a ``draft`` held via ``dossier-of`` (semantic: current understanding, best
leads, ruled-out, open questions), **rewritten every research cycle** and
doubling as the loop's *rolling context* (each tick reads the compact
dossier, not the whole logbook — context stays bounded).

The dossier body splits into a **narrative** (whole-rewritten prose) and a
**pinned ledger**. The narrative is **many small paragraph-level chunks,
one thought each**, unpinned: every :func:`rewrite_dossier` retires the
WHOLE existing unpinned set and inserts a fresh chunk per paragraph
(delete+reinsert, never in-place ``edit_text``, so per-chunk
embedding/summary recomputes per thought) — there is no stable narrative
chunk handle across ticks; :func:`read_narrative` joins whatever unpinned
chunks exist, in reading order, into one document. Growth is a
code-enforced ratchet, not a fixed cap: a rewrite may outgrow the previous
narrative by more than ~15%+50 words only with same-tick progress evidence
— :mod:`precis.quest.narrative_budget`, ``tick.py``'s
``_apply_narrative_gate``.

The **pinned ledger** is the *strategic* attempt tree: one node per
tried/abandoned/open *direction* (a whole direction, not a single ruled-out
structure — that per-candidate ledger lives on ``structure`` tags,
``tick.py``'s ``_ruled_out_handles``), children as refinements/variants,
each carrying a status (``open``/``active``/``tried``/``ruled-out``) — so a
rewrite can't silently drop a rule-out from free prose, and a whole
abandoned branch reads as a subtree, not an ambiguous flat list.

Storage is **real chunks, not a markdown blob**: a container chunk
(``meta.pinned='ledger'``, no content) holds each tree node as its own
CHILD chunk (``parent_chunk_id`` nesting; :meth:`DraftStore.reading_order`'s
DFS order = tree order), stamped ``meta.pinned='ledger-node'`` (excluded
from rewritable prose by the same truthy-``pinned`` filter in
:func:`rewrite_dossier`/:func:`read_narrative`) with status as a closed-axis
chunk tag (``ATTEMPT:<status>``, :data:`precis.store.types._CLOSED_VOCAB`).
:func:`add_attempt`/:func:`mark_attempt` are the only mutators
(:func:`append_ledger_entry` is the legacy pre-tree entry point, kept for
existing callers — adds a depth-0 node under the mapped status); every other
read goes through :class:`AttemptNode`, the in-memory forest, unaffected by
the storage move. Ruling out a node is a *stored*, per-node fact only — an
open/active descendant's status is never overwritten by an ancestor's
rule-out; the ancestor's shadow over it, and collapsing an
all-tried/ruled-out subtree to one summary line, are both **rendering-level**
(:func:`ledger_do_not_repropose`). A legacy markdown ledger (single-blob or
the older ``## Tried``/``## Ruled out``/``## Open`` three-section shape)
still reads via :func:`_parse_ledger`, and converts lazily in place on first
access (:func:`_migrate_legacy_ledger`) — ordered so a crash mid-conversion
leaves the legacy text intact (never destroy the source before the
replacement durably exists). :func:`read_ledger` re-renders the live forest
via :func:`_render_ledger` (no stored blob read); :func:`ledger_do_not_repropose`
accepts either the forest or legacy markdown text — the tick *prompt* shape
(:mod:`precis.quest.tick`) is unchanged. :func:`read_dossier` still joins
the whole body into one string (``view='dossier'`` + history); only the
tick prompt separates narrative from ledger (:func:`read_narrative`,
:func:`read_ledger`).

A third pinned family, the **dialectic blocks** (quest-dossier-dialectic
§Mechanism), holds the per-hypothesis dialectic — support/counter/
discriminating-experiment, one block per live hypothesis finding — in the
same op-mutated, never-rewritten shape as the ledger (:func:`read_dialectic`,
:func:`apply_dialectic_op`).
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
#: The precedence order a near-duplicate merge advances ALONG (never
#: backwards) — :data:`_STATUSES`' own order, ``open < active < tried <
#: ruled-out``. Used by :func:`add_attempt`'s upsert path: merging a
#: near-dup ``add`` whose status is further along than the matched node's
#: own advances it there; a status earlier in this order never regresses
#: the matched node (see :func:`_merge_near_dup_status`).
_STATUS_RANK: dict[str, int] = {s: i for i, s in enumerate(_STATUSES)}

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

    Guards :func:`_render_ledger`'s tick-prompt projection: it writes a
    node's text verbatim after its ``- [status] `` prefix, so an unmangled
    embedded newline (raw untrusted model JSON via the tick's ``ledger_ops``)
    would render as an extra bullet line a model could mistake for a real
    sibling. Applied at both the storage boundary (:func:`add_attempt`'s
    stored text) and the match boundary (:func:`_match_nodes`) — a node's
    stored text never contains a newline.
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
    near-dup scan in :func:`add_attempt`.
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
    the forest regardless of branch — :func:`add_attempt`'s upsert path
    (dossier dedup-before-insert): a rephrased repeat of an already-pinned
    direction, wherever it lives, should merge into it rather than mint a
    sibling. Ties broken by highest overlap ratio; a node with no
    significant tokens (:func:`_sig_tokens`) is never a candidate on
    either side of the comparison, so two short same-named siblings in
    different branches (the ``parent=``-disambiguated case) are untouched.
    A node whose element signature conflicts with ``text``'s
    (:func:`_elements_conflict` — e.g. an Rh branch vs a Ru attempt) is
    likewise never a candidate, however similar the surrounding prose.
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


#: Truncation length for one node's text in :func:`ledger_open_nodes` — the
#: prompt block is a compact "what's already open" reminder, not a full
#: ledger re-render (that's :func:`read_ledger`'s job).
_OPEN_NODE_TRUNCATE = 140


def ledger_open_nodes(ledger: list[AttemptNode] | str) -> str:
    """The pinned ledger's ``open``/``active`` directions — a compact
    status+text bullet per node, each truncated to
    :data:`_OPEN_NODE_TRUNCATE` chars — the upsert counterpart to
    :func:`ledger_do_not_repropose`'s tried/ruled-out list.

    Model-facing purpose: the proposer only ever saw the tried/ruled-out
    constraint, so an already-pinned OPEN direction was invisible to it —
    "when in doubt, add" then meant a rephrased repeat of something already
    on the ledger (dossier-hygiene design's motivating prod defect: ~8
    near-copies of the same "identify rate-limiting step / side
    product / poison" entries). Showing the open queue lets the model
    `mark`/refine an existing node via `ledger_ops` instead of re-adding
    it — :func:`add_attempt`'s near-dup merge (see its docstring) already
    catches the case where the model adds one anyway, so this is a
    prompt-quality aid, not the correctness backstop.

    Takes the forest directly or the legacy markdown text, mirroring
    :func:`ledger_do_not_repropose`. ``"(none yet)"`` when nothing
    qualifies.
    """
    roots = _parse_ledger(ledger) if isinstance(ledger, str) else ledger
    lines: list[str] = []
    for n, _parent in _flatten_with_parent(roots):
        if n.status not in ("open", "active"):
            continue
        text = n.text
        if len(text) > _OPEN_NODE_TRUNCATE:
            text = text[: _OPEN_NODE_TRUNCATE - 1].rstrip() + "…"
        lines.append(f"- [{n.status}] {text}")
    return "\n".join(lines) if lines else "(none yet)"


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
    text is blank, so ``bool(container.text.strip())`` is false on any later
    access).

    Ordering invariant: the legacy text is blanked ONLY after
    :func:`_materialize_legacy_forest` returns — a crash before that line
    leaves the legacy text intact for the next access to retry (retry-safe
    against double-writing, see that function). Never destroy the source
    before the replacement durably exists.
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
        if pinned in (
            _LEDGER_NODE_PINNED,
            _DIALECTIC_HYP_PINNED,
            _DIALECTIC_ENTRY_PINNED,
        ):
            continue
        if pinned == "ledger":
            parts.append(_render_ledger(_load_ledger_nodes(store, did)))
        elif pinned == _DIALECTIC_PINNED:
            parts.append(_render_dialectic(store, _load_dialectic_blocks(store, did)))
        else:
            parts.append(c.text)
    text = "\n\n".join(parts)
    return did, handle, text


def read_narrative(store: Store, owner_id: int) -> str:
    """The model-rewritten narrative, reassembled into one document (no
    pinned chunk, no heading).

    Feeds the tick prompt's ``{dossier}`` slot — the ledger is surfaced
    separately (:func:`read_ledger`) as an explicit constraint, and the
    frontier tree is a code-rendered artifact, neither folded into the
    rewritable prose. Excludes ANY pinned chunk (``meta.pinned`` truthy —
    ``"ledger"`` or ``"frontier-tree"``), not just the ledger, so a future
    pinned chunk needs no code change here. Returns ``""`` when the owner
    has no dossier, or has no narrative chunks yet.

    **Symmetric with :func:`rewrite_dossier` by contract**: the write side
    replaces the WHOLE unpinned set every rewrite (retire-all + insert-fresh
    — module docstring), so joining every live unpinned chunk here, in
    reading order, is always exactly what the last rewrite wrote — never a
    mix of fresh and stranded chunks, even one externally introduced between
    ticks (retiring the whole set each rewrite removes it by construction).
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
    """The pinned ledger's markdown rendering (:func:`_render_ledger` over
    :func:`_load_ledger_nodes`) — a thin, on-the-fly re-projection of the
    live forest (storage is real chunks; see module docstring), preserving
    the tick-prompt markdown shape (:mod:`precis.quest.tick`).

    Heals a pre-A dossier with no ledger yet (:func:`ensure_ledger_chunk`)
    and migrates a legacy markdown ledger on first access (see
    :func:`_load_ledger_nodes`) — always returns a well-formed (if empty)
    rendering, migration-free.
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


def _set_node_status(
    store: Store, dossier_id: int, node: AttemptNode, status: str
) -> None:
    """Replace ``node``'s own ``ATTEMPT:`` chunk tag with ``status`` —
    the storage primitive behind :func:`mark_attempt` (text-addressed) and
    :func:`add_attempt`'s near-dup status-advance merge (chunk-addressed,
    so it can update the exact matched node without re-running
    :func:`_match_nodes` — a global near-dup match is resolved by chunk
    identity, not by re-parsing its text, which could re-hit an ambiguous
    same-text-in-two-branches collision the first lookup already resolved).
    """
    assert node.handle is not None  # every loaded node carries a handle
    ord_ = _chunk_ord(store, dossier_id, node.handle)
    store.add_tag(
        dossier_id, Tag.closed("ATTEMPT", status), pos=ord_, replace_prefix=True
    )


def add_attempt(
    store: Store,
    owner_id: int,
    text: str,
    parent: str | None = None,
    status: str = "open",
) -> bool:
    """Add one node to the pinned attempt tree; return ``True`` iff a NEW
    node chunk was created.

    Blank ``text`` is a no-op. ``status`` clamps to ``"open"`` when not one
    of :data:`_STATUSES`.

    **Dedup-before-insert.** The WHOLE ledger (every branch, not just
    ``parent``'s siblings) is scanned for a near-dup of ``text``
    (:func:`_find_near_dup_node` — token-Jaccard, the measure
    :mod:`precis.quest.tick` also uses for hypothesis dedup). A hit UPSERTS,
    never appends: same status → no-op; different status → advances the
    match to the incoming status only if further along :data:`_STATUS_RANK`
    (``open < active < tried < ruled-out``, via :func:`_set_node_status`) —
    never regresses. A match ignores the requested ``parent`` (reused where
    it already lives, never re-parented). A node with no significant tokens
    (:func:`_sig_tokens`, e.g. `"c"`) never near-dup-matches, so same-named
    leaves in different branches stay distinct via ``parent=``
    disambiguation (:func:`_match_nodes`).

    Absent a near-dup, the narrower guard still applies: exact
    byte-identical text+status among ``parent``'s siblings is also a no-op.
    ``parent`` (optional) is the exact text of an existing node the new one
    joins as a child — matched trimmed + case-insensitive
    (:func:`_match_nodes`); zero or >1 matches is a no-op (never a guess).
    ``parent=None`` adds a root (depth-0) node under the ledger CONTAINER
    chunk.

    ``text`` is whitespace-normalized (:func:`_normalize_node_text`) before
    storage or comparison. Creates at most one new chunk
    (:func:`_write_node_chunk`) — no whole-ledger rewrite. Heals a
    pre-real-chunks dossier lacking a ledger chunk on the way in.
    """
    stripped_text = _normalize_node_text(text)
    if not stripped_text:
        return False
    st = status if status in _STATUSES else "open"
    container_handle, did, roots = _ledger_roots(store, owner_id)

    near_dup = _find_near_dup_node(roots, stripped_text)
    if near_dup is not None:
        if st != near_dup.status and _STATUS_RANK[st] > _STATUS_RANK[near_dup.status]:
            _set_node_status(store, did, near_dup, st)
        return False

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
    Applied via :func:`_set_node_status` (``add_tag(...,
    replace_prefix=True)`` — the v1 "at most one value per closed prefix on
    a target" invariant), not a whole-ledger rewrite. Heals a pre-A dossier
    lacking a ledger chunk on the way in.
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
    _set_node_status(store, did, matches[0], status)
    return True


@dataclass
class DedupMerge:
    """One near-duplicate cluster collapsed by :func:`dedup_ledger` — its
    dry-run/real-run report unit. ``prior_status``/``new_status`` differ
    only when a cluster member's status was further along
    :data:`_STATUS_RANK` than the survivor's own."""

    survivor_text: str
    survivor_handle: str
    prior_status: str
    new_status: str
    #: ``(text, status)`` of every node the survivor absorbed.
    absorbed: list[tuple[str, str]] = field(default_factory=list)


def _cluster_ledger_nodes(
    nodes: list[tuple[AttemptNode, int]],
) -> list[list[tuple[AttemptNode, int]]]:
    """Group ``(node, chunk_ord)`` pairs into near-duplicate clusters
    (:func:`_is_near_dup` — not reimplemented here), oldest node
    (lowest ``ord``, an insertion-serial — :func:`_chunk_ord`) first.

    Greedy single pass: the oldest not-yet-clustered node anchors a new
    cluster, and every remaining node whose text near-dups the ANCHOR's
    (not each other — one-vs-anchor, mirroring :func:`_find_near_dup_node`'s
    own one-vs-forest comparison rather than requiring full pairwise
    transitivity) joins it. Only clusters with more than one member are
    returned — a lone node is not a merge."""
    ordered = sorted(nodes, key=lambda pair: pair[1])
    clusters: list[list[tuple[AttemptNode, int]]] = []
    used: set[int] = set()
    for i, (anchor, anchor_ord) in enumerate(ordered):
        if anchor_ord in used:
            continue
        cluster = [(anchor, anchor_ord)]
        used.add(anchor_ord)
        for node, ord_ in ordered[i + 1 :]:
            if ord_ in used:
                continue
            if _is_near_dup(node.text, [anchor.text]):
                cluster.append((node, ord_))
                used.add(ord_)
        if len(cluster) > 1:
            clusters.append(cluster)
    return clusters


def dedup_ledger(
    store: Store, owner_id: int, *, dry_run: bool = False
) -> list[DedupMerge]:
    """One-off cleanup of an existing ledger that accumulated near-duplicate
    attempt nodes before :func:`add_attempt`'s upsert discipline
    (dossier-hygiene design) landed.

    Groups every ledger node into near-duplicate clusters
    (:func:`_cluster_ledger_nodes`, token-Jaccard via :func:`_is_near_dup` —
    the same measure :func:`add_attempt`'s upsert path and
    :func:`_find_near_dup_node` use, not reimplemented here). Per cluster:
    the OLDEST node (lowest chunk ``ord``) survives; its status advances to
    the most-advanced status found anywhere in the cluster
    (:data:`_STATUS_RANK`, never regresses — :func:`_set_node_status`, same
    primitive :func:`mark_attempt` uses); every other cluster member's live
    children are re-parented onto the survivor (``move_chunk(...,
    {"into": survivor_handle})``) before the member itself is retired
    (``retire_chunk`` — delete, never an in-place update, matching every
    other ledger mutator in this module).

    ``dry_run=True`` computes and returns the same report without writing
    anything. Returns one :class:`DedupMerge` per cluster with more than one
    member, in no particular order; a ledger with no near-duplicates
    returns ``[]``. Idempotent: re-running after a real (non-dry) merge
    finds no more clusters, since only one survivor per cluster remains.
    """
    _container_handle, did, roots = _ledger_roots(store, owner_id)
    nodes_with_ord: list[tuple[AttemptNode, int]] = []
    for node, _parent in _flatten_with_parent(roots):
        assert node.handle is not None  # every loaded node carries a handle
        nodes_with_ord.append((node, _chunk_ord(store, did, node.handle)))
    merges: list[DedupMerge] = []
    for cluster in _cluster_ledger_nodes(nodes_with_ord):
        (survivor, _survivor_ord), *absorbed = cluster
        assert survivor.handle is not None
        best_status = survivor.status
        for node, _ord in absorbed:
            if _STATUS_RANK[node.status] > _STATUS_RANK[best_status]:
                best_status = node.status
        merges.append(
            DedupMerge(
                survivor_text=survivor.text,
                survivor_handle=survivor.handle,
                prior_status=survivor.status,
                new_status=best_status,
                absorbed=[(node.text, node.status) for node, _ord in absorbed],
            )
        )
        if dry_run:
            continue
        for node, _ord in absorbed:
            assert node.handle is not None
            for child in node.children:
                assert child.handle is not None
                store.drafts.move_chunk(
                    child.handle,
                    {"into": survivor.handle},
                    source={"reason": "quest-dossier-dedup"},
                )
            store.drafts.retire_chunk(
                node.handle, source={"reason": "quest-dossier-dedup"}
            )
        if best_status != survivor.status:
            _set_node_status(store, did, survivor, best_status)
    return merges


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


#: A lone markdown heading line (``## Best leads`` etc.) — used by
#: :func:`_split_narrative_paragraphs` to fold a heading into its following
#: paragraph rather than mint it its own chunk (a heading alone carries no
#: topical content of its own to embed/search on).
_HEADING_LINE_RE = re.compile(r"^#{1,6}\s")


def _split_narrative_paragraphs(markdown: str) -> list[str]:
    """Split a narrative rewrite into paragraph-level chunk texts — one
    thought each (the narrative-chunking design, module docstring:
    "otherwise it's semantic mush").

    Splits on blank-line paragraph boundaries (the same rule
    :func:`precis.store._draft_ops._split_blocks` uses for a generic
    ``put``, duplicated here rather than imported — a private helper of a
    different module, and this one additionally folds headings, below). A
    block that is *only* a single markdown heading line is merged into the
    paragraph that follows it, when there is one, rather than kept as its
    own chunk. Blank/whitespace-only blocks are dropped — never an empty
    chunk. An entirely blank ``markdown`` returns ``[]``.
    """
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    raw = [p.strip() for p in text.split("\n\n")]
    raw = [p for p in raw if p]
    out: list[str] = []
    i = 0
    while i < len(raw):
        block = raw[i]
        if _HEADING_LINE_RE.match(block) and "\n" not in block and i + 1 < len(raw):
            out.append(block + "\n\n" + raw[i + 1])
            i += 2
        else:
            out.append(block)
            i += 1
    return out


def rewrite_dossier(store: Store, owner_id: int, markdown: str) -> int:
    """Whole-rewrite the owner's dossier NARRATIVE to ``markdown``; return its
    ref id.

    Splits ``markdown`` into paragraph-level chunks
    (:func:`_split_narrative_paragraphs`) and replaces the WHOLE existing
    unpinned narrative set with them: every prior narrative chunk is
    RETIRED (soft-deleted) and every new paragraph inserted fresh — never
    an in-place ``edit_text`` — so the per-chunk embedding/summary cascade
    re-runs per paragraph, and because the paragraph count varies tick to
    tick there is no stable 1:1 chunk to edit onto anyway. ANY pinned chunk
    (``meta.pinned`` truthy: the ledger, and the code-regenerated frontier
    tree — Slice 4c-4) is untouched, same exclusion as before each survives
    every rewrite byte-identical. New chunks land immediately before the
    ledger container (:func:`ensure_ledger_chunk`), preserving the
    narrative-before-ledger reading order every other reader assumes.

    A blank/whitespace-only ``markdown`` retires the old narrative chunks
    and leaves none in their place — :func:`read_narrative` then degrades to
    ``""``, same as a fresh, not-yet-ticked dossier.
    """
    did = ensure_dossier(store, owner_id)
    chunks = store.drafts.reading_order(did)
    old_body = [
        c
        for c in chunks
        if c.chunk_kind != "heading" and not (c.meta or {}).get("pinned")
    ]
    ledger_handle = _ensure_ledger_chunk_for_ref(store, did)
    paragraphs = _split_narrative_paragraphs(markdown)
    for c in old_body:
        store.drafts.retire_chunk(c.handle, source={"reason": "quest-tick"})
    for p in paragraphs:
        store.drafts.add_chunks(
            ref_id=did,
            chunk_kind="paragraph",
            text=p,
            at={"before": ledger_handle},
            split=False,
        )
    return did


# --- Dialectic blocks (quest-dossier-dialectic §Mechanism) -------------------
#
# The per-hypothesis dialectic lives OUTSIDE the rewritable narrative, in the
# same pinned-chunk shape as the ledger: one `meta.pinned='dialectic'`
# container per dossier; one child BLOCK chunk per live hypothesis
# (`meta.pinned='dialectic-hyp'`, `meta.hypothesis=<finding ref id>` — the
# statement/motivation/testable_by live on the finding ref, never restated);
# ENTRY chunks as block children (`meta.pinned='dialectic-entry'`,
# `meta.role=support|counter|experiment`). The model maintains blocks only
# through `dialectic_ops` (:func:`apply_dialectic_op`) — it never rewrites
# them, so the structure cannot flatten the way tick-4's ###-skeleton did.
# `support`/`counter` entries mint real evidence edges (`supports` /
# `contradicts` → the hypothesis finding) from their inline handles at apply
# time, so the dialectic is a queryable graph, not a document shaped like one.

_DIALECTIC_PINNED = "dialectic"
_DIALECTIC_HYP_PINNED = "dialectic-hyp"
_DIALECTIC_ENTRY_PINNED = "dialectic-entry"
_DIALECTIC_ROLES: tuple[str, ...] = ("support", "counter", "experiment")
#: Evidence-edge relation minted per role — the cited handle SUPPORTS /
#: CONTRADICTS the hypothesis finding. `experiment` mints no edge at apply
#: time: its `tests` edge (migration 0142, measurement → hypothesis) is
#: minted later by the measurement-ruling pass
#: (:func:`precis.quest.rulings.mint_measurement_rulings`) once a trusted
#: measurement actually runs the pre-registration.
_DIALECTIC_EDGE_RELATION: dict[str, str] = {
    "support": "supports",
    "counter": "contradicts",
}
_DIALECTIC_SEED = ""
#: An inline evidence handle in an entry's text, e.g. ``[fi263615]``,
#: ``[st262842]``, ``[pc2837304]`` — two-letter code + decimal id, the
#: universal handle grammar (:mod:`precis.utils.handle_registry`).
_DIALECTIC_HANDLE_RE = re.compile(r"\[([a-z]{2}\d+)\]")
#: Edge fan-out cap per support/counter entry (see
#: :func:`_mint_evidence_edges`).
_DIALECTIC_MAX_EDGES_PER_ENTRY = 8


@dataclass
class DialecticEntry:
    """One dialectic entry — a support/counter why-clause or the block's
    discriminating experiment. ``handle`` is the entry chunk's own ``dc``
    handle (stable across ticks — entries are never whole-rewritten)."""

    role: str
    text: str
    handle: str | None = None
    #: Experiment entries only — measurement rulings already minted for this
    #: pre-registration (``{key: ruling finding id}``, written by
    #: :func:`precis.quest.rulings.mint_measurement_rulings`); the render
    #: surfaces each as a ``measured:`` line and the pass skips minted keys.
    rulings: dict[str, Any] = field(default_factory=dict)


@dataclass
class DialecticBlock:
    """One live hypothesis's dialectic block. ``settled`` non-empty collapses
    the render to that one linked sentence (entries are kept as history)."""

    hypothesis_id: int
    entries: list[DialecticEntry] = field(default_factory=list)
    settled: str = ""
    handle: str | None = None
    chunk_id: int | None = None


def _ensure_dialectic_chunk_for_ref(store: Store, dossier_id: int) -> str:
    """The dialectic sibling of :func:`_ensure_ledger_chunk_for_ref` —
    creates the ``meta.pinned='dialectic'`` container (appended at the END of
    the doc, after the ledger, so :func:`rewrite_dossier`'s
    insert-before-ledger placement keeps reading order stable:
    narrative → ledger → dialectic) if absent, else returns its handle."""
    chunks = store.drafts.reading_order(dossier_id)
    found = _find_pinned_chunk(chunks, _DIALECTIC_PINNED)
    if found is not None:
        return str(found.handle)
    created = store.drafts.add_chunks(
        ref_id=dossier_id, chunk_kind="paragraph", text=_DIALECTIC_SEED, split=False
    )
    handle = str(created[0].handle)
    store.drafts.patch_chunk_meta(handle, {"pinned": _DIALECTIC_PINNED})
    return handle


def ensure_dialectic_chunk(store: Store, owner_id: int) -> str:
    """Return the handle of the owner's pinned dialectic container chunk,
    creating dossier and container as needed (mirrors
    :func:`ensure_ledger_chunk`'s lazy healing — a live owner grows its
    dialectic on first access, migration-free)."""
    did = ensure_dossier(store, owner_id)
    return _ensure_dialectic_chunk_for_ref(store, did)


def _load_dialectic_blocks(store: Store, dossier_id: int) -> list[DialecticBlock]:
    """The dialectic forest, loaded from real chunks. Returns ``[]`` when the
    dossier has no dialectic container yet. :meth:`DraftStore.reading_order`'s
    DFS pre-order puts a block before its entries, so one pass builds it."""
    chunks = store.drafts.reading_order(dossier_id)
    container = _find_pinned_chunk(chunks, _DIALECTIC_PINNED)
    if container is None:
        return []
    by_chunk_id: dict[int, DialecticBlock] = {}
    out: list[DialecticBlock] = []
    for c in chunks:
        meta = c.meta or {}
        pinned = meta.get("pinned")
        if pinned == _DIALECTIC_HYP_PINNED and c.parent_chunk_id == container.chunk_id:
            try:
                hid = int(str(meta.get("hypothesis")))
            except ValueError:
                continue
            block = DialecticBlock(
                hypothesis_id=hid,
                settled=str(meta.get("settled") or ""),
                handle=str(c.handle),
                chunk_id=c.chunk_id,
            )
            by_chunk_id[c.chunk_id] = block
            out.append(block)
        elif pinned == _DIALECTIC_ENTRY_PINNED and c.parent_chunk_id in by_chunk_id:
            role = str(meta.get("role") or "")
            if role in _DIALECTIC_ROLES:
                raw_rulings = meta.get("rulings")
                by_chunk_id[c.parent_chunk_id].entries.append(
                    DialecticEntry(
                        role=role,
                        text=c.text,
                        handle=str(c.handle),
                        rulings=dict(raw_rulings)
                        if isinstance(raw_rulings, dict)
                        else {},
                    )
                )
    return out


def _hypothesis_is_refuted(store: Store, finding_id: int) -> bool:
    """True when the finding carries ``STATUS:refuted``. NOTE:
    :class:`~precis.store.Tag`'s ``namespace`` is the tag-KIND discriminator
    (``closed``/``flag``/``open``); the closed prefix lives in ``prefix`` —
    filtering on ``namespace == "STATUS"`` silently never matches
    (docs/backlog/quest-status-tag-prefix-misread.md)."""
    try:
        tags = store.tags_for(finding_id)
    except Exception:
        return False
    return any(
        str(getattr(t, "prefix", "") or "") == "STATUS"
        and str(getattr(t, "value", "") or "") == "refuted"
        for t in tags
    )


def _render_dialectic(store: Store, blocks: list[DialecticBlock]) -> str:
    """Markdown projection of the dialectic forest for the tick prompt and
    the whole-body dossier view — code-rendered (never model-authored), same
    standing as :func:`_render_ledger`'s output."""
    if not blocks:
        return "(no dialectic blocks yet)"
    lines: list[str] = []
    for b in blocks:
        ref = store.get_ref(kind="finding", id=b.hypothesis_id)
        title = str(getattr(ref, "title", "") or "").strip() or "(finding missing)"
        head = f"- [fi{b.hypothesis_id}] {title}"
        if _hypothesis_is_refuted(store, b.hypothesis_id):
            lines.append(head + " — REFUTED (do not re-propose)")
            continue
        if b.settled:
            lines.append(head + f" — SETTLED: {b.settled}")
            continue
        lines.append(head)
        by_role: dict[str, list[DialecticEntry]] = {}
        for e in b.entries:
            by_role.setdefault(e.role, []).append(e)
        for role in _DIALECTIC_ROLES:
            for e in by_role.get(role, []):
                lines.append(f"  - {role}: {e.text}")
                # Code-minted measurement rulings on this pre-registration
                # (:func:`precis.quest.rulings.mint_measurement_rulings`) —
                # the tick's cue to interpret: support/counter/settle citing
                # the ruling's handle, per the pre-registered branch.
                for fid in e.rulings.values():
                    try:
                        rid = int(fid)
                    except (TypeError, ValueError):
                        continue
                    rref = store.get_ref(kind="finding", id=rid)
                    rtitle = (
                        str(getattr(rref, "title", "") or "").strip()
                        or "(ruling missing)"
                    )
                    lines.append(f"  - measured: [fi{rid}] {rtitle}")
        if not by_role.get("experiment"):
            lines.append(
                "  - experiment: (MISSING — every live hypothesis needs its "
                "discriminating experiment; emit one via `dialectic_ops`)"
            )
    return "\n".join(lines)


def read_dialectic(store: Store, owner_id: int) -> str:
    """The dialectic blocks' markdown rendering — a thin on-the-fly
    projection of the live chunks (never a stored blob), mirroring
    :func:`read_ledger`. Heals a dossier with no dialectic container yet."""
    ensure_dialectic_chunk(store, owner_id)
    did = dossier_ref_id(store, owner_id)
    assert did is not None  # ensure_dialectic_chunk just guaranteed a dossier
    return _render_dialectic(store, _load_dialectic_blocks(store, did))


def _resolve_hypothesis_id(store: Store, raw: object) -> int | None:
    """``"fi263615"`` / ``"[fi263615]"`` / ``263615`` → the finding's ref id,
    or ``None`` when the handle is malformed, isn't a finding, or doesn't
    resolve to a live ref (degrade-don't-crash, the ledger-op contract)."""
    from precis.utils import handle_registry

    text = str(raw or "").strip().strip("[]")
    if not text:
        return None
    if text.isdigit():
        fid = int(text)
    else:
        parsed = handle_registry.parse(text)
        if parsed is None:
            return None
        kind, is_chunk, fid = parsed
        if kind != "finding" or is_chunk:
            return None
    ref = store.get_ref(kind="finding", id=fid)
    return int(ref.id) if ref is not None else None


def _chunk_owner_ref_id(store: Store, chunk_id: int) -> int | None:
    """The ``ref_id`` a chunk handle's chunk belongs to (evidence handles in
    entry text may be universal CHUNK handles, e.g. ``pc<id>``; the edge
    targets the owning ref)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    return int(row[0]) if row is not None else None


def _mint_evidence_edges(store: Store, hypothesis_id: int, role: str, text: str) -> int:
    """Mint one ``supports``/``contradicts`` link per resolvable inline
    handle in ``text`` → the hypothesis finding. Idempotent (``add_link``'s
    unique-tuple no-op); a handle that doesn't resolve, points at the
    hypothesis itself, or FK-fails is skipped, never a raise. Returns the
    number of edges written (including re-writes of existing ones)."""
    from precis.utils import handle_registry

    relation = _DIALECTIC_EDGE_RELATION.get(role)
    if relation is None:
        return 0
    minted = 0
    # Cap per entry: each handle costs DB round-trips and writes into the
    # corpus-wide links graph — a handle-stuffed model payload must not fan
    # out unboundedly. A why-clause legitimately cites a few handles, not 8+.
    handles = list(dict.fromkeys(_DIALECTIC_HANDLE_RE.findall(text)))[
        :_DIALECTIC_MAX_EDGES_PER_ENTRY
    ]
    for h in handles:
        parsed = handle_registry.parse(h)
        if parsed is None:
            continue
        _kind, is_chunk, hid = parsed
        ev_ref = _chunk_owner_ref_id(store, hid) if is_chunk else hid
        if ev_ref is None or ev_ref == hypothesis_id:
            continue
        try:
            store.add_link(
                src_ref_id=ev_ref,
                dst_ref_id=hypothesis_id,
                relation=relation,
                meta={"dialectic": role},
            )
        except Exception:
            log.info(
                "dialectic: evidence edge %s -%s-> fi%s not minted",
                h,
                relation,
                hypothesis_id,
            )
            continue
        minted += 1
    return minted


def _has_anchor_handle(text: str) -> bool:
    """True when ``text`` carries at least one WELL-FORMED inline handle
    (``[xx123]`` that :func:`handle_registry.parse` accepts). The
    support/counter anchor gate: an unanchored argument can live in prose
    but can never enter the dialectic record (operator ruling 2026-08-29 —
    prose is never a primary source). Well-formed, not necessarily
    edge-mintable: a ``[ql…]`` logbook anchor is legitimate evidence even
    though it mints no ref edge."""
    from precis.utils import handle_registry

    return any(
        handle_registry.parse(h) is not None for h in _DIALECTIC_HANDLE_RE.findall(text)
    )


def _find_dialectic_block(
    blocks: list[DialecticBlock], hypothesis_id: int
) -> DialecticBlock | None:
    for b in blocks:
        if b.hypothesis_id == hypothesis_id:
            return b
    return None


def _ensure_dialectic_block(
    store: Store, dossier_id: int, container_handle: str, hypothesis_id: int
) -> tuple[DialecticBlock, bool]:
    """``(block, created)`` — the hypothesis's block, minted under the
    container if absent. Idempotent on ``meta.hypothesis``."""
    blocks = _load_dialectic_blocks(store, dossier_id)
    found = _find_dialectic_block(blocks, hypothesis_id)
    if found is not None:
        return found, False
    created = store.drafts.add_chunks(
        ref_id=dossier_id,
        chunk_kind="paragraph",
        text=f"[fi{hypothesis_id}]",
        at={"into": container_handle},
        split=False,
    )
    chunk = created[0]
    store.drafts.patch_chunk_meta(
        chunk.handle,
        {"pinned": _DIALECTIC_HYP_PINNED, "hypothesis": hypothesis_id},
    )
    block = DialecticBlock(
        hypothesis_id=hypothesis_id,
        handle=str(chunk.handle),
        chunk_id=chunk.chunk_id,
    )
    return block, True


def apply_dialectic_op(store: Store, owner_id: int, op: dict[str, Any]) -> bool:
    """Apply one ``dialectic_ops`` payload entry; return ``True`` iff it
    changed something. Ops address blocks by the hypothesis's **fi handle**
    (stable real ids — no ledger-style quote-the-exact-text ambiguity):

    * ``open`` — ensure the block exists (idempotent). On a settled block,
      re-opens it (clears the settle sentence).
    * ``support`` / ``counter`` — append one why-clause entry. MUST carry at
      least one well-formed inline handle (:func:`_has_anchor_handle` — the
      anchor gate: unanchored argument never enters the record); near-dup
      text among the block's same-role entries is a no-op
      (:func:`_is_near_dup`, the ledger's measure). Inline evidence handles
      mint ``supports``/``contradicts`` edges → the hypothesis finding.
    * ``experiment`` — upsert IN PLACE (one discriminating experiment per
      block; ``predicts`` — the pre-registered branch predictions — is
      folded into the text). Editing keeps the entry chunk's ``dc`` id.
    * ``settle`` — collapse the block's render to ``text`` (one linked
      sentence; entries kept as history). Optional ``ruling`` handle is
      recorded on the block.

    Every CONTRACT failure is a silent ``False`` (bad shape, unresolvable
    hypothesis, blank text) — the ledger-op degrade-don't-crash convention.
    A raw DB/store exception does propagate; the tick's ``dialectic_ops``
    loop wraps each call (same as ``ledger_ops``) so a raise never crashes
    the tick, and logs unapplied ops for diagnosability.
    """
    kind = str(op.get("op") or "").strip()
    if kind not in {"open", "support", "counter", "experiment", "settle"}:
        return False
    hid = _resolve_hypothesis_id(store, op.get("hypothesis"))
    if hid is None:
        return False
    container_handle = ensure_dialectic_chunk(store, owner_id)
    did = dossier_ref_id(store, owner_id)
    assert did is not None
    block, created = _ensure_dialectic_block(store, did, container_handle, hid)

    if kind == "open":
        if block.settled:
            assert block.handle is not None
            store.drafts.patch_chunk_meta(block.handle, {"settled": ""})
            return True
        return created

    text = _normalize_node_text(str(op.get("text") or ""))
    if not text:
        # A bare open-by-side-effect: the block now exists even though the
        # entry op itself carried nothing usable.
        return created

    if kind == "settle":
        assert block.handle is not None
        ruling = str(op.get("ruling") or "").strip().strip("[]")
        store.drafts.patch_chunk_meta(
            block.handle, {"settled": text, "settled_ruling": ruling}
        )
        return True

    if kind == "experiment":
        predicts = _normalize_node_text(str(op.get("predicts") or ""))
        if predicts:
            text = f"{text} (predicts: {predicts})"
        existing = next((e for e in block.entries if e.role == "experiment"), None)
        if existing is not None:
            if existing.text == text:
                return False
            assert existing.handle is not None
            store.drafts.edit_text(
                existing.handle, text, source={"reason": "quest-dialectic"}
            )
            return True
    else:  # support / counter
        if not _has_anchor_handle(text):
            return False
        same_role = [e.text for e in block.entries if e.role == kind]
        if _is_near_dup(text, same_role) or text in same_role:
            return False

    assert block.handle is not None
    created_chunks = store.drafts.add_chunks(
        ref_id=did,
        chunk_kind="paragraph",
        text=text,
        at={"into": block.handle},
        split=False,
    )
    store.drafts.patch_chunk_meta(
        created_chunks[0].handle,
        {"pinned": _DIALECTIC_ENTRY_PINNED, "role": kind},
    )
    _mint_evidence_edges(store, hid, kind, text)
    return True


__all__ = [
    "AttemptNode",
    "DedupMerge",
    "DialecticBlock",
    "DialecticEntry",
    "add_attempt",
    "append_ledger_entry",
    "apply_dialectic_op",
    "dedup_ledger",
    "dossier_ref_id",
    "ensure_dialectic_chunk",
    "ensure_dossier",
    "ensure_ledger_chunk",
    "ledger_do_not_repropose",
    "ledger_open_nodes",
    "mark_attempt",
    "paper_ref_id",
    "read_dialectic",
    "read_dossier",
    "read_ledger",
    "read_narrative",
    "rewrite_dossier",
    "update_frontier_tree",
]
