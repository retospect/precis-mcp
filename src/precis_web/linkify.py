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

# Grammar moved to ``precis.utils.mentions`` so the write-time
# autolinker shares it (single source). Re-exported here under the
# historical private names every web call site already imports —
# ``_REF_PATTERN`` / ``_BARE_CONV_PATTERN`` / ``_BARE_PAPER_PATTERN``
# and the kind allowlists. See that module for the per-pattern notes.
from precis.utils.mentions import (
    BARE_CONV_PATTERN as _BARE_CONV_PATTERN,
)
from precis.utils.mentions import (
    BARE_PAPER_PATTERN as _BARE_PAPER_PATTERN,
)
from precis.utils.mentions import (
    LINKIFY_KINDS as _LINKIFY_KINDS,
)
from precis.utils.mentions import (
    LOW_SIGNAL_KINDS as _LOW_SIGNAL_KINDS,
)
from precis.utils.mentions import (
    REF_PATTERN as _REF_PATTERN,
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
    # ``whitespace-normal`` on the popover container resets the
    # ``white-space: pre-wrap`` it inherits from the parent ``<pre>``
    # on ref-detail pages — otherwise every newline in the popover
    # Jinja template becomes visible vertical whitespace and the card
    # reads like it's been double-spaced. ``max-h-72`` + ``overflow-y-auto``
    # keep very long previews inside a tidy 18rem-tall box rather than
    # growing the popover off-screen.
    # The hover/leave handlers live on the outer span (not the anchor)
    # so moving the mouse from the link onto the popover doesn't close
    # it — Alpine sees a single bounding box that includes both.
    #
    # Three robustness affordances guard against the stuck-popover
    # symptom we saw in Safari (mouseleave not always firing reliably
    # when an absolutely-positioned popover overlaps the cursor's path,
    # plus the debounce race where a delayed mouseenter overrode a
    # subsequent mouseleave):
    #
    # 1. ``setTimeout`` + ``clearTimeout`` on enter/leave — mouseleave
    #    cancels the pending hover so a quick fly-by never opens it.
    # 2. ``@ref-popover-open.window`` — when ANY popover opens, every
    #    other one listens for it and closes itself. Bounds the open
    #    set to ≤1 cluster-wide regardless of mouseleave reliability.
    # 3. ``@click.outside`` — clicking anywhere outside the span shuts
    #    the popover. Belt-and-suspenders for the Safari case where
    #    mouseleave never fires (touch input, scroll past, swipe).
    open_expr = (
        "clearTimeout(hoverTimer); "
        "hoverTimer = setTimeout(() => { "
        "hovered = true; "
        "$dispatch('ref-popover-open', { source: $el }); "
        "}, 200)"
    )
    close_expr = "clearTimeout(hoverTimer); hovered = false"
    other_open_expr = (
        "if ($event.detail.source !== $el) { "
        "clearTimeout(hoverTimer); hovered = false; "
        "}"
    )
    return (
        f'<span x-data="{{hovered: false, hoverTimer: null}}" '
        f'class="relative inline-block" '
        f'@mouseenter="{open_expr}" '
        f'@mouseleave="{close_expr}" '
        f'@click.outside="{close_expr}" '
        f'@ref-popover-open.window="{other_open_expr}">'
        f'<a class="text-sky-700 underline decoration-dotted hover:decoration-solid" '
        f'href="/r/{safe_kind}/{safe_id}{suffix_q}" '
        f'hx-get="/preview/{safe_kind}/{safe_id}" '
        f'hx-trigger="mouseenter delay:200ms once" '
        f'hx-target="next .ref-popover" hx-swap="innerHTML">'
        f"{display}</a>"
        f'<span class="ref-popover absolute z-50 top-full left-0 mt-1 w-80 '
        f'rounded-lg border border-slate-200 bg-white shadow-xl p-2 text-sm '
        f'whitespace-normal max-h-72 overflow-y-auto" '
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


#: Combined alternation so the three pattern shapes (prefixed
#: ``kind:ref``, bare conv handle, bare paper cite_key) consume a
#: given span ONCE — otherwise a sequential-substitution pass would
#: re-match cite_keys inside the anchors produced by the first pass.
#: Order in the alternation matters: longer/more specific shapes
#: first so the regex engine commits to them before falling through
#: to the broad bare paper pattern.
_COMBINED_PATTERN = re.compile(
    r"(?P<ref>"
    + _REF_PATTERN.pattern
    + r")"
    r"|"
    r"(?P<bare_conv>"
    + _BARE_CONV_PATTERN.pattern
    + r")"
    r"|"
    r"(?P<bare_paper>"
    + _BARE_PAPER_PATTERN.pattern
    + r")"
)


def _linkify_prose(prose: str) -> str:
    """Replace every ``kind:ref``, bare conv handle, and bare paper
    cite_key in plain prose with an anchor — single pass so we never
    double-match inside an anchor we just produced."""
    if not prose:
        return ""

    def _dispatch(m: re.Match[str]) -> str:
        if m.group("ref") is not None:
            kind = m.group("kind")
            raw_id = m.group("id")
            chunk = m.group("chunk")
            # Allowlist gate: skip kinds that look like ``noun:value``
            # in prose but aren't precis kinds (user:asa, tag:open).
            if kind not in _LINKIFY_KINDS or kind in _LOW_SIGNAL_KINDS:
                return m.group(0)
            return _render_anchor(kind, raw_id, chunk)
        if m.group("bare_conv") is not None:
            whole = m.group("bare_conv")
            slug = whole
            chunk = None
            if "~" in slug:
                slug, _, suffix = slug.partition("~")
                chunk = "~" + suffix
            return _render_anchor("conv", slug, chunk)
        if m.group("bare_paper") is not None:
            whole = m.group("bare_paper")
            slug = whole
            chunk = None
            if "~" in slug:
                slug, _, suffix = slug.partition("~")
                chunk = "~" + suffix
            return _render_anchor("paper", slug, chunk)
        return m.group(0)

    return _COMBINED_PATTERN.sub(_dispatch, prose)


__all__ = ["linkify_refs"]
