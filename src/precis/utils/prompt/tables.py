"""Computed context tables (ADR 0038 §6/§6a), rendered as TOON.

These are *computed modules*: each builder queries live state (or a fixed
verb/kind list) and returns a TOON table via the canonical
:func:`precis.format.render_agent_table` chokepoint — never hand-written.
Four tables; three cached, one variable:

* :func:`tools_table` (cached) — the verb surface, with examples.
* :func:`kinds_table` (cached) — the code↔name legend for reading
  handles *and* choosing ``kind=`` (ADR 0038 §7).
* :func:`doc_context_table` (variable) — the working set around an
  anchor: the window (ancestors ``^``, siblings ``±N``) + references,
  with a ``how`` disclosure column mapped onto already-computed gloss /
  keywords / text (ADR 0038 §6a).
* :func:`glossary_table` (variable) — a draft's active abbreviations.

Each builder returns ``""`` when it has nothing to say, so the assembler
drops the block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.format import render_agent_table
from precis.utils.handle_registry import code_for_kind

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store._draft_ops import DraftChunk


def _table(label: str, rows: list[dict[str, str]], schema: list[str]) -> str:
    """Label line + a TOON table, or ``""`` for no rows."""
    if not rows:
        return ""
    return f"{label}\n{render_agent_table(rows, schema=schema)}"


# ── cached: tools + kinds ─────────────────────────────────────────────

#: The agent-profile verb surface. Examples are part of the table (ADR
#: 0038 §6, "tools{verb,example,what}: examples included"). Cached — the
#: same across every tick of every agent.
_TOOLS: list[dict[str, str]] = [
    {
        "verb": "get",
        "example": "get(id=dc41)",
        "what": "read a ref/chunk; dc41+1 / dc41^ for a neighbour or parent",
    },
    {
        "verb": "search",
        "example": "search(kind=paper, q='…')",
        "what": "find papers / skills / chunks by topic",
    },
    {
        "verb": "put",
        "example": "put(kind=todo, text='…')",
        "what": "mint a subtask, write a file (tex/pic/data), or cite",
    },
    {
        "verb": "edit",
        "example": "edit(id=dc41, text='…')",
        "what": "rewrite a chunk / section in place",
    },
    {
        "verb": "tag",
        "example": "tag(id=N, add=['STATUS:done'])",
        "what": "set status, ask-user:<q>, or halt:<reason>",
    },
    {
        "verb": "link",
        "example": "link(rel='blocked-by', src=B, dst=A)",
        "what": "order or relate refs (B waits on A)",
    },
]


def tools_table() -> str:
    """The cached ``tools`` table (verb · example · what)."""
    return _table(
        "## Tools (the seven verbs; kind= picks the surface)",
        _TOOLS,
        ["verb", "example", "what"],
    )


#: Curated kinds the planner/editor actually sees — *not* the full ~24
#: registry (ADR 0038 §6 "model sees scoped; server uses full"). Codes
#: are pulled from the handle registry so the legend can never drift from
#: the SSOT (a test asserts this). ``chunk`` names the addressable-chunk
#: code where one exists.
_KIND_ROWS: list[tuple[str, bool, str, str]] = [
    # (kind, has_chunk_code, what, ops)
    ("draft", True, "a document we're writing", "get put edit search"),
    ("paper", True, "an ingested paper", "get search"),
    ("patent", True, "an EPO-OPS patent", "get search"),
    ("todo", False, "a task node in the tree", "get put tag link"),
    ("citation", False, "a claim bound to a source chunk", "get put"),
    ("finding", False, "a claim being chased into the corpus", "get put"),
    ("memory", False, "a durable note / thought", "get put"),
    ("skill", False, "a how-to / style guide", "get search"),
    ("tex", True, "a workspace LaTeX section", "get put edit"),
]


def kinds_table() -> str:
    """The cached ``kinds`` legend — code/name/what/ops.

    The single legend for **reading handles** (``dc41`` → a draft chunk)
    *and* **choosing ``kind=``** (pass the long ``name``). Chunk codes
    (``dc``/``pc``) are address-only — you ``get``/``edit`` them but
    can't ``put(kind=dc, …)``."""
    rows: list[dict[str, str]] = []
    for kind, has_chunk, what, ops in _KIND_ROWS:
        code = code_for_kind(kind)
        chunk = code_for_kind(kind, chunk=True) if has_chunk else ""
        rows.append(
            {
                "code": code + (f"/{chunk}" if chunk else ""),
                "name": kind,
                "what": what,
                "ops": ops,
            }
        )
    return _table(
        "## Kinds (legend for handles AND kind=; chunk codes are address-only)",
        rows,
        ["code", "name", "what", "ops"],
    )


# ── variable: doc_context ─────────────────────────────────────────────


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def _clip(text: str, n: int = 160) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n].rstrip() + "…"


def _disclosure(views: dict[str, dict[str, str]], handle: str) -> tuple[str, str]:
    """Pick the cheapest faithful disclosure for a neighbour row.

    gist (the ``llm-v1`` BRIEF) → keywords (KeyBERT) → empty. Maps onto
    data the summarizer / keyword workers already computed (ADR 0038 §6a),
    so the table costs no new compute."""
    v = views.get(handle, {})
    summary = (v.get("summary") or "").strip()
    if summary:
        return "gist", _clip(_first_line(summary))
    kws = (v.get("keywords") or "").strip()
    if kws:
        return "keywords", _clip(kws)
    return "", ""


def doc_context_table(store: Store, anchor: str) -> str:
    """Build the variable ``doc_context`` table centred on ``anchor``.

    Window = parent (``^``) + prev/next siblings (``±1``) + the anchor
    itself; references = the anchor's outbound/inbound links
    (``cites``/``derived-from``/…). The ``how`` column is the disclosure
    level: ``verbatim`` for the anchor (you act on it), ``path`` for the
    parent, ``gist``/``keywords`` for neighbours, ``gist`` for refs.
    Returns ``""`` when the anchor chunk no longer exists.

    Two handle forms are in play (ADR 0033 vs 0036): the stored base-58
    ``chunks.handle`` (what ``block_views`` / ``chunk_connections`` key
    on) and the computed ``dc<chunk_id>`` universal handle (what relative
    nav consumes and what the agent should write in prose). We resolve
    everything through :class:`DraftChunk` objects — which carry both —
    navigating by ``dc<id>``, looking up gloss by the base-58 handle, and
    **displaying the canonical ``dc<id>``** the prompt tells the agent to
    use."""
    base = store.get_draft_chunk(anchor)
    if base is None:
        return ""
    ref_id = int(base.ref_id)

    def _dc(chunk_id: int) -> str:
        return "dc" + str(chunk_id)

    def _step(op: str) -> DraftChunk | None:
        ids = store.draft_relative_chunk_ids(_dc(base.chunk_id) + op)
        return store.get_draft_chunk(_dc(ids[0])) if ids else None

    parent = _step("^")
    prev = _step("-1")
    nxt = _step("+1")

    neighbours = [c for c in (parent, prev, nxt) if c is not None]
    views = (
        store.block_views(ref_id, [c.handle for c in neighbours]) if neighbours else {}
    )

    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(chunk: DraftChunk, what: str, how: str, details: str) -> None:
        handle = _dc(chunk.chunk_id)
        if handle in seen:
            return
        seen.add(handle)
        rows.append({"id": handle, "what": what, "how": how, "details": details})

    if parent is not None:
        _add(parent, "parent section", "path", _clip(_first_line(parent.text)))
    if prev is not None:
        how, details = _disclosure(views, prev.handle)
        _add(prev, "prev sibling", how or "—", details)
    _add(base, "current (change-request target)", "verbatim", _clip(base.text, 400))
    if nxt is not None:
        how, details = _disclosure(views, nxt.handle)
        _add(nxt, "next sibling", how or "—", details)

    # references — outbound/inbound links from the anchor (cites, prior
    # art, dream-memories), keyed by the base-58 handle the link graph
    # stores. Deduped against window rows already shown.
    for conn in store.chunk_connections(ref_id, [base.handle]).get(base.handle, []):
        ident = f"{conn['kind']}:{conn['ident']}"
        if ident in seen:
            continue
        seen.add(ident)
        rows.append(
            {
                "id": ident,
                "what": f"{conn['relation']} ({conn['direction']})",
                "how": "gist",
                "details": _clip(conn.get("title") or ""),
            }
        )

    return _table(
        "## doc_context (window + references; deepen any row with get(id=…))",
        rows,
        ["id", "what", "how", "details"],
    )


# ── variable: glossary (reusable table; the planner keeps its richer
#    prose block, but reviewers/editor fold in via this) ───────────────


def glossary_table(store: Store, draft_ref_id: int) -> str:
    """A draft's active abbreviations as a ``term/short/long`` table.

    The reusable computed-module form of the glossary (ADR 0038 §6,
    region-scoped in context; the full registry is only used by the
    off-model linkify pass). Returns ``""`` when the draft has no terms."""
    terms = store.draft_terms(draft_ref_id)  # {handle: (short, long)}
    rows = [
        {"term": short, "short": short, "long": _clip(long, 120), "handle": h}
        for h, (short, long) in sorted(terms.items(), key=lambda t: t[1][0].lower())
        if short
    ]
    return _table(
        "## Glossary (active abbreviations — use these, do not redefine)",
        rows,
        ["term", "short", "long", "handle"],
    )


__all__ = [
    "doc_context_table",
    "glossary_table",
    "kinds_table",
    "tools_table",
]
