"""Inline ``kind:ref`` linkifier for prose surfaces.

Scans rendered text for ``kind:slug`` / ``kind:#id`` / ``kind:N``
patterns and replaces each match with an anchor that:

* Hover (with a 200 ms grace delay) → htmx fetches a tiny preview card
  from ``/preview/{kind}/{id}`` and renders it in a sibling popover.
* Click → navigates to ``/r/{kind}/{id}`` which redirects to the
  ref's canonical view (paper viewer, tasks dashboard with focus,
  generic refs detail page).

Skip zones — the filter does not touch text inside ``<code>``,
``<pre>``, or already-anchored ``<a>`` segments. Those carry verbatim
content (code samples, URL strings) where linkifying would mis-fire.

Pattern: ``<kind>:<ref>`` where

* ``kind`` is a lowercase identifier ``[a-z][a-z0-9-]*``;
* ``ref`` is one of:

  - a slug: ``[A-Za-z][A-Za-z0-9_-]*`` (no internal slashes — those
    are reserved for path views and we want the LLM-emitted bare
    ``paper:slug~7`` form to fall through cleanly);
  - an explicit numeric: ``#?[0-9]+`` (``memory:6184`` or
    ``memory:#6184``).

Optional trailing ``~N`` (chunk address) is captured into the anchor's
URL fragment but the popover ignores it for now — chunk-level previews
are a follow-on.

The filter validates ``kind`` lazily — every match is rendered as an
anchor regardless of whether the kind is registered. Unknown kinds 404
on the preview / redirect route; the popover then renders an "unknown
kind" stub. Cheap, no per-render dependency on the live ``Hub``.
"""

from __future__ import annotations

import re
from html import escape

from markupsafe import Markup

#: Match ``kind:ref`` patterns inside prose. The trailing assertion
#: ``(?!\w)`` keeps the match from greedy-consuming a longer word
#: (e.g. doesn't claim ``memory:6184foo`` — the ``foo`` would otherwise
#: become an anchor with a busted id).
#:
#: The optional ``~suffix`` covers three address shapes used by the
#: paper handler + a fourth we add for direct PDF page jumps:
#:
#: * ``~N``     — chunk at ``ord=N`` (existing paper addressing).
#: * ``~N..M``  — chunk range (existing).
#: * ``~pN``    — PDF page N (new — bypasses the chunk → page lookup;
#:   useful when the agent already knows the page number).
_REF_PATTERN = re.compile(
    r"\b"
    r"(?P<kind>[a-z][a-z0-9-]*)"
    r":"
    r"(?P<id>"
    r"#?[0-9]+"
    r"|"
    r"[A-Za-z][A-Za-z0-9_-]*"
    r")"
    r"(?P<chunk>~(?:p[0-9]+|[0-9]+(?:\.\.[0-9]+)?))?"
    r"(?!\w)"
)

#: Spans of input the linkifier must leave alone. Each pattern matches
#: an opening tag through its closing tag; everything outside these is
#: prose we can transform. We compile a single alternation so the
#: tokenizer is one linear scan.
_SKIP_PATTERN = re.compile(
    r"<code\b[^>]*>.*?</code>"
    r"|<pre\b[^>]*>.*?</pre>"
    r"|<a\b[^>]*>.*?</a>",
    flags=re.DOTALL | re.IGNORECASE,
)

#: Kinds that aren't worth linkifying — they're either ambient (the
#: word "skill" appears in prose constantly, ``skill:precis-overview``
#: would be over-eager) or low-signal (``tag``, ``link``). The filter
#: still emits an anchor for these *only* when the user typed the
#: full ``kind:`` prefix; we just don't go hunting for them. (This is
#: vestigial: the pattern requires an explicit ``kind:`` prefix, so
#: there's no overreach risk today. Kept for future extension when we
#: might want to linkify bare ``#42`` shorthand.)
_LOW_SIGNAL_KINDS: frozenset[str] = frozenset({"tag", "link"})


def _render_anchor(kind: str, raw_id: str, chunk: str | None) -> str:
    """Build the per-match anchor + sibling popover slot.

    The anchor's ``href`` points at ``/r/{kind}/{id}`` (the resolver
    redirector) so right-click → "Open in new tab" still works without
    needing JS. htmx + Alpine drive the hover preview.
    """
    safe_kind = escape(kind)
    # Strip a leading ``#`` from numeric refs so the URL path stays
    # clean: ``memory:#6184`` and ``memory:6184`` both route to
    # ``/r/memory/6184``.
    cleaned_id = raw_id.lstrip("#")
    safe_id = escape(cleaned_id)
    display = f"{safe_kind}:{escape(raw_id)}"
    if chunk:
        display += escape(chunk)
    # The ``~suffix`` rides into the resolver as a query param so the
    # redirector can decide what to do per-kind (paper → PDF#page=N;
    # other kinds → ignore the suffix and land on the ref overview).
    suffix_q = ""
    if chunk:
        # ``chunk`` here is the regex group including the leading ``~``;
        # the resolver expects it without.
        suffix_q = f"?chunk={escape(chunk[1:])}"
    return (
        f'<span x-data="{{hovered: false}}" class="relative inline-block">'
        f'<a class="text-sky-700 underline decoration-dotted hover:decoration-solid" '
        f'href="/r/{safe_kind}/{safe_id}{suffix_q}" '
        f'hx-get="/preview/{safe_kind}/{safe_id}" '
        f'hx-trigger="mouseenter delay:200ms once" '
        f'hx-target="next .ref-popover" hx-swap="innerHTML" '
        f'@mouseenter.debounce.200ms="hovered = true" '
        f'@mouseleave="hovered = false">'
        f"{display}</a>"
        f'<span class="ref-popover absolute z-50 top-full left-0 mt-1 w-80 '
        f'rounded-lg border border-slate-200 bg-white shadow-xl p-2 text-sm" '
        f'x-show="hovered" x-cloak></span>'
        f"</span>"
    )


def linkify_refs(value: str) -> Markup:
    """Replace ``kind:ref`` mentions in ``value`` with hover-preview anchors.

    Input may already contain HTML — anchors / `<code>` / `<pre>`
    blocks are detected and passed through verbatim. Outside those
    skip zones, plain-text ``kind:ref`` mentions become anchors.

    Returns a :class:`markupsafe.Markup` instance so Jinja's autoescape
    treats the result as already-safe HTML.
    """
    if not value:
        return Markup("")
    text = str(value)
    out_parts: list[str] = []
    last = 0
    for m in _SKIP_PATTERN.finditer(text):
        # Process the prose stretch since the previous skip-zone.
        prose = text[last : m.start()]
        out_parts.append(_linkify_prose(prose))
        # Pass the skip zone through unchanged.
        out_parts.append(m.group(0))
        last = m.end()
    out_parts.append(_linkify_prose(text[last:]))
    return Markup("".join(out_parts))


def _linkify_prose(prose: str) -> str:
    """Replace every ``kind:ref`` in plain prose with an anchor."""
    if not prose:
        return ""

    def _sub(m: re.Match[str]) -> str:
        kind = m.group("kind")
        raw_id = m.group("id")
        chunk = m.group("chunk")
        if kind in _LOW_SIGNAL_KINDS:
            # Vestigial — see module docstring. Fall through to plain
            # text if a future caller adds these to the blocklist.
            return m.group(0)
        return _render_anchor(kind, raw_id, chunk)

    return _REF_PATTERN.sub(_sub, prose)


__all__ = ["linkify_refs"]
