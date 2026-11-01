"""Canon pre-pass — rewrite a search query's *lexical* leg from taxon/alias.

``search(q='TOF', args={'canon': True})``: the dispatcher
(:mod:`precis.runtime.canon`) looks each query token and 2-gram up against
taxon name/slug/aliases and ``kind='alias'`` rows, and on a unique hit binds a
:class:`CanonState` for the duration of the call. The store's lexical legs
(``search_chunks_lexical``, the ``lex`` CTE of ``search_chunks_fused``,
``search_refs_lexical`` and the two ``count_*_lexical`` companions) pass their
tsquery text through :func:`lexical_q`; the semantic leg never does, because
embedding ``'TOF or "turnover frequency"'`` would only blur the vector.

**Why a ContextVar and not a kwarg.** The lexical tsquery is built in five
store methods that dozens of handler search paths reach through
``fused_ref_hits`` / ``search_chunks`` / ``search_chunks_multi``. Threading a
parameter through every handler would touch each one's result assembly;
a ContextVar read at the single point where the tsquery text is bound keeps
the handlers untouched. The cross-kind fan-out submits workers with
``contextvars.copy_context().run``, so the state follows into its threads;
:class:`CanonState` is a mutable object shared by reference, which is how
:attr:`CanonState.applied` reports back to the dispatcher whether any leg
actually used the rewrite.

**The rewrite keeps the original query as one OR branch.** An expansion is
distributed, not substituted: ``'TOF of catalyst'`` becomes
``'TOF of catalyst or "turnover frequency" of catalyst'`` so each branch keeps
the query's AND semantics (a bare ``a b or c`` would parse as ``(a & b) | c``
and drop ``b`` from the expanded branch). A branch can only add candidates.

Off by default: with no state bound :func:`lexical_q` returns ``q`` untouched,
so a search without ``canon`` is byte-identical to before.

Guards, applied here and in the resolver: a quoted phrase is never expanded
into or out of; a ``-negated`` word is never a lookup surface; a surface under
:data:`MIN_SURFACE_CHARS` characters is skipped unless it is the whole query.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

#: A matched surface shorter than this is skipped unless ``q`` is a single
#: token — 'ph', 'rt', 'of' are too ambiguous to rewrite inside a sentence.
MIN_SURFACE_CHARS = 4

#: Lookup budget: at most this many 2-grams + tokens hit the database.
MAX_SURFACES = 24

#: Terms per hit (canonical name + aliases) and OR branches per query. Bounds
#: the tsquery a broad taxon with many aliases could otherwise produce.
MAX_TERMS_PER_HIT = 6
MAX_BRANCHES = 24

#: Cap on the instance ids handed to the rank bonus.
MAX_BOOST_IDS = 5000

#: Soft rank bonus added to the fused (RRF) score of a ref with an
#: ``instance-of`` edge into a hit node. UNCALIBRATED first guess: one RRF leg
#: contributes ``1/(60+rank)``, so 0.004 lifts a hit about twenty ranks on one
#: leg and never decides alone. Revisit once traces show lift.
RANK_BONUS = 0.004

#: A quoted phrase (an unclosed quote runs to the end, as in
#: ``websearch_to_tsquery``) or one whitespace-delimited word.
_PIECE_RE = re.compile(r'"[^"]*(?:"|$)|\S+')
_EDGE_PUNCT_RE = re.compile(r"^\W+|\W+$")
_BARE_TERM_RE = re.compile(r"\w+")


@dataclass(frozen=True)
class Piece:
    """One whitespace word (or quoted phrase) of a query.

    ``key`` is the lookup surface (lowercased core, ``''`` for an unusable
    piece); ``prefix``/``suffix`` are the punctuation stripped off its edges,
    restored around a replacement term.
    """

    text: str
    prefix: str
    core: str
    suffix: str
    usable: bool

    @property
    def key(self) -> str:
        return self.core.lower() if self.usable else ""


def pieces(q: str) -> list[Piece]:
    """Split ``q`` into pieces; quoted phrases and ``-negated`` words are
    kept but unusable as lookup surfaces."""
    out: list[Piece] = []
    for m in _PIECE_RE.finditer(q):
        text = m.group(0)
        if text.startswith('"') or text.startswith("-"):
            out.append(Piece(text, "", "", "", False))
            continue
        core = _EDGE_PUNCT_RE.sub("", text)
        if not core:
            out.append(Piece(text, "", "", "", False))
            continue
        start = text.index(core)
        out.append(Piece(text, text[:start], core, text[start + len(core) :], True))
    return out


@dataclass(frozen=True)
class Surface:
    """A lookup candidate: one usable token or two adjacent usable tokens."""

    key: str
    text: str
    start: int
    end: int


def surfaces(q: str) -> list[Surface]:
    """Candidate surfaces of ``q``, 2-grams first (longest match wins),
    capped at :data:`MAX_SURFACES`. Short surfaces are dropped here unless
    ``q`` is a single piece."""
    ps = pieces(q)
    out: list[Surface] = []
    for width in (2, 1):
        for i in range(len(ps) - width + 1):
            span = ps[i : i + width]
            if not all(p.usable for p in span):
                continue
            key = " ".join(p.key for p in span)
            if len(key) < MIN_SURFACE_CHARS and len(ps) > 1:
                continue
            out.append(Surface(key, " ".join(p.core for p in span), i, i + width))
    return out[:MAX_SURFACES]


@dataclass(frozen=True)
class CanonHit:
    """A unique taxon hit for ``surface`` and the terms to OR in for it."""

    surface: str
    taxon_id: int
    name: str
    terms: tuple[str, ...]

    @property
    def key(self) -> str:
        return " ".join(self.surface.lower().split())


@dataclass
class CanonState:
    """What one canon pre-pass resolved. Mutable only in :attr:`applied`."""

    q: str
    hits: list[CanonHit] = field(default_factory=list)
    ambiguous: list[tuple[str, list[int]]] = field(default_factory=list)
    boost_ids: tuple[int, ...] = ()
    applied: bool = False

    def trace_meta(self) -> dict[str, Any]:
        """The expansion as a JSON-able dict for the search-trace row
        (``meta.canon``, gr548261)."""
        return {
            "q": self.q,
            "hits": [
                {
                    "surface": h.surface,
                    "taxon": h.taxon_id,
                    "name": h.name,
                    "terms": list(h.terms),
                }
                for h in self.hits
            ],
            "ambiguous": [{"surface": s, "taxa": ids} for s, ids in self.ambiguous],
            "boost": len(self.boost_ids),
            "applied": self.applied,
        }


_STATE: ContextVar[CanonState | None] = ContextVar("precis_canon", default=None)


def bind(state: CanonState | None) -> Token[CanonState | None]:
    return _STATE.set(state)


def unbind(token: Token[CanonState | None]) -> None:
    _STATE.reset(token)


def current() -> CanonState | None:
    return _STATE.get()


def _term_text(term: str) -> str:
    """One OR-branch term as websearch syntax: a bare word stays bare, anything
    else (a phrase, punctuation, the operator word ``or``) is quoted."""
    t = term.replace('"', " ").strip()
    if _BARE_TERM_RE.fullmatch(t) and t.lower() != "or":
        return t
    return f'"{" ".join(t.split())}"'


def rewrite(q: str, hits: list[CanonHit]) -> str:
    """``q`` OR-ed with its alias-substituted branches, or ``q`` unchanged
    when no hit's surface occurs in it (outside quotes) or the branch cap
    would be exceeded."""
    ps = pieces(q)
    by_key = {h.key: h for h in hits}
    taken: set[int] = set()
    reps: list[tuple[int, int, CanonHit]] = []
    for width in (2, 1):
        for i in range(len(ps) - width + 1):
            span = range(i, i + width)
            if any(k in taken for k in span) or not all(ps[k].usable for k in span):
                continue
            key = " ".join(ps[k].key for k in span)
            hit = by_key.get(key)
            if hit is None or (len(key) < MIN_SURFACE_CHARS and len(ps) > 1):
                continue
            reps.append((i, i + width, hit))
            taken.update(span)
    if not reps:
        return q
    reps.sort(key=lambda r: r[0])
    branches: list[list[str | None]] = [[p.text for p in ps]]
    for i, j, hit in reps:
        terms = [t for t in hit.terms if " ".join(t.lower().split()) != hit.key]
        if not terms or len(branches) * (1 + len(terms)) > MAX_BRANCHES:
            continue
        extra: list[list[str | None]] = []
        for branch in branches:
            for term in terms:
                nb = list(branch)
                nb[i] = f"{ps[i].prefix}{_term_text(term)}{ps[j - 1].suffix}"
                for k in range(i + 1, j):
                    nb[k] = None
                extra.append(nb)
        branches += extra
    if len(branches) == 1:
        return q
    return " or ".join(" ".join(w for w in b if w is not None) for b in branches)


def lexical_q(q: str) -> str:
    """The tsquery text a lexical leg should bind for ``q``.

    Identity unless a canon state with hits is bound and ``q`` contains a hit
    surface; marks the state ``applied`` when it rewrites.
    """
    state = _STATE.get()
    if state is None or not state.hits:
        return q
    out = rewrite(q, state.hits)
    if out != q:
        state.applied = True
    return out


def boost_ref_ids() -> list[int] | None:
    """Ref ids earning :data:`RANK_BONUS` in the fused score, or ``None``
    (no state / nothing to boost). Marks the state ``applied``."""
    state = _STATE.get()
    if state is None or not state.boost_ids:
        return None
    state.applied = True
    return list(state.boost_ids)
