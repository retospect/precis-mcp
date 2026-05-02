"""RandomHandler — stateless random value generator.

Pass a DSL expression as ``id=`` (or ``q=``); the response is the
rolled / picked / nearest value. Five forms:

- **Dice** — ``d20``, ``2d6``, ``3d6+3``, ``4d8-1`` (``NdM[±K]``;
  N defaults to 1).
- **Integer** — ``int(LO..HI)`` (uniform inclusive).
- **Choice** — ``choice(A|B|C)`` (uniform pick from pipe-separated
  options; whitespace trimmed around each option).
- **Neighbor** — ``neighbor(kind:id[~pos])`` (top-K vector-nearest
  blocks; needs ``store`` + ``embedder``; the source block is
  excluded).
- **Chunk** — ``chunk(kind:id)`` (one random block from the ref;
  needs ``store``).

Randomness goes through :mod:`secrets` (CSPRNG) — same choice as
:class:`OracleHandler`, reads intentionally for a "roll the dice"
semantic and avoids mis-seeded ``random`` state leaking across
requests. Callers that need reproducible sequences seed their own
``random.Random`` instance outside the MCP surface; the MCP itself
is deliberately non-deterministic.

No DB writes, no state, no side effects. Neighbor / chunk read the
store but never mutate.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._link_target import parse_link_target
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.utils.next_block import render_next_section

# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------
#
# Each DSL form compiles to a single regex matched against the
# stripped input. The first match wins; we try in the order most-
# likely-to-conflict-last so a typo like ``d20x`` doesn't silently
# fall through to ``choice(...)`` (it won't — every regex is
# anchored ``^…$``).
#
# Whitespace around the operators is tolerated for the bracketed
# forms (``int( 1 .. 100 )`` is fine); the dice form does not
# accept whitespace because ``d`` is a lowercase letter and
# ``2 d 6`` looks like prose. Case-sensitive on the literal ``d``
# / ``int`` / ``choice`` / ``neighbor`` / ``chunk`` keywords so
# ``D20`` is rejected — teaching one spelling keeps the recovery
# hints unambiguous.

_DICE_RE = re.compile(r"^(\d*)d(\d+)([+-]\d+)?$")
_INT_RE = re.compile(r"^int\(\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*\)$")
_CHOICE_RE = re.compile(r"^choice\((.+)\)$", re.DOTALL)
_NEIGHBOR_RE = re.compile(r"^neighbor\((.+)\)$", re.DOTALL)
_CHUNK_RE = re.compile(r"^chunk\((.+)\)$", re.DOTALL)

# Safety caps. A single ``9999d999999`` request is cheap in CPU
# terms but the rendered body (``rolls: 1, 5, 3, …``) blows the
# token budget. The limits are generous — real dice games stay
# well under both.
_DICE_MAX_N = 1000  # number of dice per roll
_DICE_MAX_M = 1_000_000  # sides per die

# Default top-K for neighbor. Five is enough to see a pattern
# without paginating; callers that want more pass ``top_k=N``.
_NEIGHBOR_DEFAULT_K = 5
_NEIGHBOR_MAX_K = 50


class RandomHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="random",
        title="Random",
        description=(
            "Stateless CSPRNG-backed random value generator. "
            "Pass a DSL expression as `id`: dice (`3d6+3`), integer "
            "(`int(1..100)`), choice (`choice(a|b|c)`), vector "
            "neighbor (`neighbor(paper:slug~42)`), or random chunk "
            "(`chunk(paper:slug)`)."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
    )

    def __init__(self, *, hub: Hub) -> None:
        # Stateless for dice / int / choice. Neighbor / chunk read
        # ``hub.store`` and ``hub.embedder`` lazily at request time,
        # so a store-less deployment still exposes the roll-and-pick
        # half of the surface — we just raise BadInput when the
        # caller tries a ref-backed form without a wired store.
        #
        # We hold onto ``hub`` here (in addition to the base class's
        # :meth:`_register_with` assignment) so tests that
        # instantiate the handler directly — bypassing the dispatch
        # boot path — still see the wired store / embedder when they
        # reach neighbor / chunk.
        self.hub = hub

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        top_k: int | None = None,
        **_kw: Any,
    ) -> Response:
        expr = _coerce_expr(id, q)

        m = _DICE_RE.match(expr)
        if m is not None:
            return _roll_dice(expr, m)

        m = _INT_RE.match(expr)
        if m is not None:
            return _roll_int(expr, m)

        m = _CHOICE_RE.match(expr)
        if m is not None:
            return _roll_choice(expr, m)

        m = _NEIGHBOR_RE.match(expr)
        if m is not None:
            return _roll_neighbor(
                expr,
                m.group(1).strip(),
                hub=self.hub,
                top_k=top_k,
            )

        m = _CHUNK_RE.match(expr)
        if m is not None:
            return _roll_chunk(expr, m.group(1).strip(), hub=self.hub)

        # Nothing matched — emit the full grammar as recovery.
        raise BadInput(
            f"random expression {expr!r} does not match any supported form",
            next=(
                "try one of: '2d6+3' (dice), 'int(1..100)' (integer), "
                "'choice(a|b|c)' (pick), 'neighbor(paper:slug~42)' "
                "(vector-nearest), 'chunk(paper:slug)' (random block)"
            ),
        )


# ---------------------------------------------------------------------------
# DSL evaluators
# ---------------------------------------------------------------------------


def _roll_dice(expr: str, m: re.Match[str]) -> Response:
    """Evaluate an ``NdM[±K]`` expression.

    Returns a body of the form ``NdM[±K] = TOTAL`` with a
    ``rolls:`` trailer listing each individual face so the caller
    can sanity-check (and so a GM can verify / rerule individual
    dice). ``N=1`` elides the list because one die and the total
    are the same number.
    """
    n_str, m_sides_str, mod_str = m.group(1), m.group(2), m.group(3)
    # ``d20`` → N=1.
    n = int(n_str) if n_str else 1
    m_sides = int(m_sides_str)
    modifier = int(mod_str) if mod_str else 0

    if n < 1:
        raise BadInput(
            f"dice count must be >= 1, got {n}",
            next="try 'd20' (single die) or '3d6' (three dice)",
        )
    if m_sides < 2:
        raise BadInput(
            f"dice must have at least 2 sides, got {m_sides}",
            next="try 'd6' (cube) or 'd20' (standard RPG die)",
        )
    if n > _DICE_MAX_N:
        raise BadInput(
            f"dice count {n} exceeds cap of {_DICE_MAX_N}",
            next=f"try {_DICE_MAX_N}d{m_sides} or split across multiple rolls",
        )
    if m_sides > _DICE_MAX_M:
        raise BadInput(
            f"dice sides {m_sides} exceeds cap of {_DICE_MAX_M}",
            next=f"try 'int(1..{m_sides})' for a single huge-range draw",
        )

    # ``secrets.randbelow(m_sides)`` returns [0, m_sides); we want
    # [1, m_sides] for face values. Add 1 per roll.
    rolls = [secrets.randbelow(m_sides) + 1 for _ in range(n)]
    total = sum(rolls) + modifier

    body = f"{expr} = {total}"
    if n > 1 or modifier != 0:
        parts = []
        if n > 1:
            parts.append(f"rolls: {', '.join(str(r) for r in rolls)}")
        if modifier != 0:
            sign = "+" if modifier > 0 else ""
            parts.append(f"modifier: {sign}{modifier}")
        body += f"  ({'; '.join(parts)})"
    body += render_next_section(
        [
            (f"get(kind='random', id={expr!r})", "roll again"),
            (
                "get(kind='skill', id='precis-random-help')",
                "full random DSL reference",
            ),
        ]
    )
    return Response(body=body)


def _roll_int(expr: str, m: re.Match[str]) -> Response:
    """Evaluate an ``int(LO..HI)`` expression (inclusive both ends)."""
    lo, hi = int(m.group(1)), int(m.group(2))
    if lo > hi:
        raise BadInput(
            f"int() range is empty: lo={lo} > hi={hi}",
            next=f"swap the bounds: get(kind='random', id='int({hi}..{lo})')",
        )
    # ``randbelow(span)`` returns [0, span); add ``lo`` to shift
    # into [lo, hi]. span = hi - lo + 1 because both ends inclusive.
    span = hi - lo + 1
    value = lo + secrets.randbelow(span)
    body = f"{expr} = {value}"
    body += render_next_section(
        [
            (f"get(kind='random', id={expr!r})", "draw again"),
        ]
    )
    return Response(body=body)


def _roll_choice(expr: str, m: re.Match[str]) -> Response:
    """Evaluate a ``choice(A|B|C)`` expression.

    Splits on ``|``, trims whitespace, rejects empty / whitespace-
    only options. An empty option list is BadInput — users who
    literally want ``choice()`` get a clear error rather than a
    silent empty pick.
    """
    raw = m.group(1)
    options = [opt.strip() for opt in raw.split("|")]
    options = [opt for opt in options if opt]
    if not options:
        raise BadInput(
            f"choice() has no options: {expr!r}",
            next="try 'choice(heads|tails)' or 'choice(yes|no|maybe)'",
        )
    pick = options[secrets.randbelow(len(options))]
    body = f"{expr} = {pick}"
    if len(options) > 1:
        body += f"  (picked from {len(options)} options)"
    body += render_next_section(
        [
            (f"get(kind='random', id={expr!r})", "pick again"),
        ]
    )
    return Response(body=body)


def _roll_neighbor(
    expr: str,
    target_str: str,
    *,
    hub: Any,
    top_k: int | None,
) -> Response:
    """Evaluate a ``neighbor(kind:id[~pos])`` expression.

    Resolves the target via the canonical link-target parser, reads
    the source block's embedding, and asks the store for its top-K
    vector-nearest neighbours (excluding itself). Ref-level targets
    (``neighbor(paper:wang2020)``) aren't supported today — a ref
    has no embedding, only its blocks do. The caller gets a clear
    BadInput pointing at the selector form.
    """
    store = getattr(hub, "store", None)
    embedder = getattr(hub, "embedder", None)
    if store is None:
        raise BadInput(
            "neighbor() requires a wired store; this deployment is stateless",
            next="dice / int / choice work without a store",
        )
    if embedder is None:
        raise BadInput(
            "neighbor() requires a wired embedder; this deployment has none",
            next="dice / int / choice work without an embedder",
        )

    target = parse_link_target(target_str, store=store)
    if target.pos is None:
        raise BadInput(
            f"neighbor({target_str!r}) requires a block selector "
            f"(e.g. '~42' or '~slug'); refs have no embedding of their own",
            next=(
                f"get(kind={target.kind!r}, id={target_str.split(':', 1)[1]!r}) "
                "to list available block positions, then neighbor(...~N)"
            ),
        )

    source_block = store.get_block(target.ref_id, pos=target.pos, with_embedding=True)
    if source_block is None or source_block.embedding is None:
        raise NotFound(
            f"neighbor({target_str!r}): block has no embedding",
            next=(
                "blocks need an embedding for vector search; re-ingest the "
                "ref or check the source block has text"
            ),
        )

    k = _NEIGHBOR_DEFAULT_K if top_k is None else int(top_k)
    if k < 1 or k > _NEIGHBOR_MAX_K:
        raise BadInput(
            f"top_k must be in [1, {_NEIGHBOR_MAX_K}], got {top_k!r}",
            next=f"try top_k={_NEIGHBOR_DEFAULT_K}",
        )

    # Ask for k+1 so we can drop the source block itself (distance
    # ~0) and still return k results. ``max_distance=None`` — for
    # exploration queries we want the nearest regardless of a
    # similarity floor (the whole point is "what's nearby").
    rows = store.search_blocks_semantic(
        query_vec=list(source_block.embedding),
        limit=k + 1,
        max_distance=None,
    )
    # Filter out the source block.
    neighbors = [
        (block, ref, dist)
        for (block, ref, dist) in rows
        if block.id != source_block.id
    ][:k]

    lines = [f"# {expr}", f"_{len(neighbors)} nearest block(s)_", ""]
    if not neighbors:
        lines.append("(no other embedded blocks in the corpus)")
    else:
        for i, (block, ref, dist) in enumerate(neighbors, 1):
            handle = _format_block_handle(ref, block)
            preview = _first_line(block.text)
            lines.append(f"{i}. `{handle}`  (distance {dist:.3f})")
            if preview:
                lines.append(f"   {preview}")
    body = "\n".join(lines)

    hints: list[tuple[str, str]] = [
        (f"get(kind='random', id={expr!r})", "same query, fresh results"),
    ]
    if neighbors:
        top_block, top_ref, _ = neighbors[0]
        top_handle = _format_block_handle(top_ref, top_block)
        hints.append(
            (
                f"get(kind={top_ref.kind!r}, id={top_handle.split(':', 1)[1]!r})",
                "read the top neighbour",
            )
        )
    body += render_next_section(hints)
    return Response(body=body)


def _roll_chunk(expr: str, target_str: str, *, hub: Any) -> Response:
    """Evaluate a ``chunk(kind:id)`` expression — pick one random
    block from a ref."""
    store = getattr(hub, "store", None)
    if store is None:
        raise BadInput(
            "chunk() requires a wired store; this deployment is stateless",
            next="dice / int / choice work without a store",
        )
    # ``parse_link_target`` resolves either ref- or block-level
    # targets. We accept only the ref-level form here — asking for
    # a random chunk of a specific block doesn't mean anything.
    target = parse_link_target(target_str, store=store)
    if target.pos is not None:
        raise BadInput(
            f"chunk({target_str!r}) points at a single block; "
            f"chunk() picks from a ref",
            next=f"drop the selector: get(kind='random', id='chunk({target_str.rsplit('~', 1)[0]})')",
        )

    blocks = store.list_blocks_for_ref(target.ref_id)
    if not blocks:
        raise NotFound(
            f"chunk({target_str!r}): ref has no blocks",
            next=f"check content exists: get(kind={target.kind!r}, id={target_str.split(':', 1)[1]!r})",
        )

    block = blocks[secrets.randbelow(len(blocks))]
    # Re-resolve the ref for titling / handle formatting.
    ref = store.get_ref(kind=target.kind, id=target_str.split(":", 1)[1])
    assert ref is not None  # parse_link_target already validated
    handle = _format_block_handle(ref, block)

    body = (
        f"# {expr}\n"
        f"_block {block.pos} of {len(blocks)} — `{handle}`_\n\n"
        f"{block.text}"
    )
    body += render_next_section(
        [
            (f"get(kind='random', id={expr!r})", "draw a different chunk"),
            (
                f"get(kind={target.kind!r}, id={target_str.split(':', 1)[1]!r})",
                "read the full ref",
            ),
        ]
    )
    return Response(body=body)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_expr(id: str | int | None, q: str | None) -> str:
    """Accept the expression via ``id=`` or ``q=``. Either works;
    canonical examples teach ``id=`` because ``random`` returns a
    *value*, not a search result."""
    if isinstance(id, str) and id.strip():
        return id.strip()
    if isinstance(id, int):
        return str(id)
    if isinstance(q, str) and q.strip():
        return q.strip()
    raise BadInput(
        "random requires an expression as `id` (or `q`)",
        next="try get(kind='random', id='2d6+3') or 'int(1..100)'",
    )


def _format_block_handle(ref: Any, block: Any) -> str:
    """Render a ``kind:identifier~pos`` handle for a block.

    Slug kinds use ``ref.slug``; numeric kinds use ``ref.id``.
    Both are valid link targets, so the agent can copy-paste.
    """
    ident = ref.slug if ref.slug else str(ref.id)
    return f"{ref.kind}:{ident}~{block.pos}"


def _first_line(text: str, *, max_chars: int = 80) -> str:
    """First non-empty line of ``text``, clipped to ``max_chars``."""
    for line in text.splitlines():
        s = line.strip()
        if s:
            if len(s) > max_chars:
                return s[: max_chars - 1].rstrip() + "…"
            return s
    return ""
