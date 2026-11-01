"""``search(..., args={'canon': True})`` — resolve the canon pre-pass.

The opt-in lexical-leg expansion (gr548267). This module is the resolver the
dispatcher calls before kind dispatch; the rewrite itself, the guards and the
ContextVar the store reads live in :mod:`precis.utils.canon` (see its
docstring for why the lexical legs read a ContextVar).

For each query token and adjacent 2-gram (2-grams first, so a longer match
consumes its tokens) the resolver does an exact, then a case-insensitive,
lookup of the surface against

* taxon ``meta.name`` / ``meta.slug`` / ``meta.aliases``
  (:meth:`Store.taxon_find_by_term`), and
* ``kind='alias'`` rows with ``rel`` in ``same-as`` / ``short-for``, whose
  *other* side is then looked up as a taxon term.

Exactly one taxon is a **unique hit**: its name and aliases join the lexical
leg as an OR branch and refs with an ``instance-of`` edge into it (the node's
descendant closure — the walk ``under=`` uses, :func:`resolve_under`) earn a
soft rank bonus. Two taxa for one surface is **ambiguous** and does nothing
but name itself on the ``canon:`` line. An alias row whose other side is no
taxon expands nothing — the canonical form is the taxon.
"""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput
from precis.reading.concepts import normalize_name
from precis.utils import canon as _canon
from precis.utils import handle_registry
from precis.utils.canon import MAX_BOOST_IDS, MAX_TERMS_PER_HIT, CanonHit, CanonState
from precis.utils.text import slugify

#: Modes that are exact-term by contract — canon never rewrites them.
EXACT_MODES = frozenset({"lexical", "verbatim", "literal"})

_ALIAS_RELS = frozenset({"same-as", "short-for"})
_NEXT = "search(q='TOF', args={'canon': True})"


def pop_canon_flag(args: dict[str, Any]) -> bool:
    """Remove ``canon`` from a search call's args (top level and ``args=``)
    and return whether it is on. A non-boolean or doubly-given value is
    refused — never read as a guess."""
    extras = args.get("__extras__")
    vals: list[Any] = []
    if args.get("canon") is not None:
        vals.append(args.pop("canon"))
    else:
        args.pop("canon", None)
    if isinstance(extras, dict) and "canon" in extras:
        v = extras.pop("canon")
        if v is not None:
            vals.append(v)
    if len(vals) > 1:
        raise BadInput(
            "canon= was passed twice — top-level and inside args=",
            next=_NEXT,
        )
    if not vals:
        return False
    if not isinstance(vals[0], bool):
        raise BadInput(
            f"canon= must be true or false, got {vals[0]!r}",
            options=["True", "False"],
            next=_NEXT,
        )
    return vals[0]


def refuse_exact_mode(mode: Any) -> None:
    """``canon=True`` with an exact-term ``mode=`` has two readings (rewrite
    anyway, or honour the exact mode) — refuse rather than pick one."""
    m = str(mode or "").strip().lower()
    if m in EXACT_MODES:
        raise BadInput(
            f"canon=True does not combine with mode={m!r} — that mode matches "
            "the typed words exactly and canon would rewrite them",
            next=f"drop mode={m!r} to expand: {_NEXT}; or drop canon= to stay exact",
        )


def _is_exact(meta: dict[str, Any], text: str) -> bool:
    return (
        meta.get("name") == text
        or meta.get("slug") == text
        or text in (meta.get("aliases") or [])
    )


def _taxa_for_term(store: Any, text: str) -> list[int]:
    """Live taxon ids for ``text``: the exact-spelling matches when any, else
    every case/whitespace-insensitive match."""
    ids = store.taxon_find_by_term(slug=slugify(text), norm=normalize_name(text))
    if len(ids) < 2:
        return list(ids)
    exact = [
        i
        for i in ids
        if (r := store.get_ref(kind="taxon", id=i)) is not None
        and _is_exact(r.meta or {}, text)
    ]
    return exact or list(ids)


def _taxa_for_surface(hub: Any, text: str) -> list[int]:
    store = hub.store
    found = set(_taxa_for_term(store, text))
    alias = hub.handler_for("alias")
    if alias is None:
        return sorted(found)
    for ref in alias.lookup(text):
        p = (ref.meta or {}).get("alias") or {}
        if p.get("rel") not in _ALIAS_RELS:
            continue
        a, b = str(p.get("a", "")), str(p.get("b", ""))
        other = b if a.lower() == text.lower() else a
        found.update(_taxa_for_term(store, other))
    return sorted(found)


def _hit_for(store: Any, surface: str, taxon_id: int) -> CanonHit | None:
    ref = store.get_ref(kind="taxon", id=taxon_id)
    if ref is None:
        return None
    meta = ref.meta or {}
    name = str(meta.get("name") or ref.title or "")
    seen: set[str] = set()
    terms: list[str] = []
    for t in [name, *(str(a) for a in (meta.get("aliases") or []))]:
        k = " ".join(t.lower().split())
        if k and k not in seen:
            seen.add(k)
            terms.append(t.strip())
    return CanonHit(surface, taxon_id, name, tuple(terms[:MAX_TERMS_PER_HIT]))


def resolve(hub: Any, q: Any) -> CanonState:
    """Run the pre-pass for ``q``; an empty state when nothing resolves."""
    text = q if isinstance(q, str) else ""
    state = CanonState(q=text)
    store = getattr(hub, "store", None)
    if not text.strip() or store is None or hub.handler_for("taxon") is None:
        return state
    taken: set[int] = set()
    for s in _canon.surfaces(text):
        if any(k in taken for k in range(s.start, s.end)):
            continue
        ids = _taxa_for_surface(hub, s.text)
        if not ids:
            continue
        if len(ids) > 1:
            state.ambiguous.append((s.text, ids))
            continue
        hit = _hit_for(store, s.text, ids[0])
        if hit is not None:
            state.hits.append(hit)
            taken.update(range(s.start, s.end))
    if state.hits:
        from precis.runtime.under_facet import resolve_under

        boost: set[int] = set()
        for h in state.hits:
            _note, inst = resolve_under(
                hub, handle_registry.format_handle("taxon", h.taxon_id)
            )
            boost.update(inst)
        state.boost_ids = tuple(sorted(boost)[:MAX_BOOST_IDS])
    return state


def canon_line(state: CanonState) -> str:
    """The one line printed above the result: what was rewritten to what."""
    parts = [
        f"{h.surface} → {handle_registry.format_handle('taxon', h.taxon_id)} {h.name}"
        for h in state.hits
    ]
    parts += [
        f"{s!r} is ambiguous ({', '.join(handle_registry.format_handle('taxon', i) for i in ids)})"
        " — not expanded"
        for s, ids in state.ambiguous
    ]
    if not parts:
        parts = ["no unique taxon/alias match in q"] if state.q.strip() else ["no q="]
    line = "canon: " + "; ".join(parts)
    if state.hits and not state.applied:
        line += " (no lexical leg of this search used it)"
    return line
