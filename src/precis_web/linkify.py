"""Inline ``kind:ref`` linkifier for prose surfaces.

Scans rendered text for ``kind:slug`` / ``kind:#id`` / ``kind:N``
patterns and replaces each match with an anchor that:

* Hover (with a 200 ms grace delay) → htmx fetches a tiny preview card
  from ``/preview/{kind}/{id}`` and renders it in a sibling popover.
* Click → navigates to ``/r/{kind}/{id}`` which redirects to the
  ref's canonical view (paper viewer, tasks dashboard with focus,
  generic refs detail page).

Input is treated as **plain text** and HTML-escaped: every caller
passes a raw store field (a todo title, a memory/conv body, console
output) that may legitimately contain ``<``, ``>``, or ``&`` — e.g. a
planner prompt with placeholder syntax like ``q='<title or DOI>'`` or
``text='<claim>'``. The only HTML this filter emits is the anchor
markup it generates for each match; surrounding prose is escaped so a
literal ``<title>`` in a title can never open a real ``<title>``
element (which flips the tokenizer to RAWTEXT and swallows the rest of
the page, silently killing every inline ``<script>`` after it) or
inject script (stored XSS). See ``test_untrusted_html_is_escaped``.

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

Claim rendering (R2): a caller may pass a ``claims`` side-channel — the
frozenset of Taproot hub-cite heads in the render window — and a bracket
cite whose head is in it renders as a violet claim anchor
(:func:`_render_claim_hub`): click → ``/claim/<head>``, hover →
``/preview/claim/<head>``. ``claims`` defaults to ``None`` (off) outside
the smartdraft/paper readers, so every other surface renders unchanged.
"""

from __future__ import annotations

import re
import uuid
from html import escape

from markupsafe import Markup

from precis.utils import handle_registry
from precis.utils.handles import is_handle as _is_handle

# Grammar moved to ``precis.utils.mentions`` so the write-time
# autolinker shares it (single source). Re-exported here under the
# historical private names every web call site already imports —
# ``_REF_PATTERN`` / ``_BARE_CONV_PATTERN`` / ``_BARE_PAPER_PATTERN``
# and the kind allowlists. See that module for the per-pattern notes.
from precis.utils.mentions import (
    AUTHORING_PATTERN as _AUTHORING_PATTERN,
)
from precis.utils.mentions import (
    BARE_BRACKET_REF_PATTERN as _BARE_BRACKET_REF_PATTERN,
)
from precis.utils.mentions import (
    BARE_CONV_PATTERN as _BARE_CONV_PATTERN,
)
from precis.utils.mentions import (
    BARE_PAPER_PATTERN as _BARE_PAPER_PATTERN,
)
from precis.utils.mentions import (
    DISPLAY_LINK_PATTERN as _DISPLAY_LINK_PATTERN,
)
from precis.utils.mentions import (
    DRAFT_CITE_PATTERN as _DRAFT_CITE_PATTERN,
)
from precis.utils.mentions import (
    LINKIFY_KINDS as _LINKIFY_KINDS,
)
from precis.utils.mentions import (
    LOW_SIGNAL_KINDS as _LOW_SIGNAL_KINDS,
)
from precis.utils.mentions import (
    PAGE_ANCHOR_HREF_RE,
    strip_page_anchor_links,
)
from precis.utils.mentions import (
    REF_PATTERN as _REF_PATTERN,
)


def _render_anchor(
    kind: str, raw_id: str, chunk: str | None, *, label: str | None = None
) -> str:
    """Build the per-match anchor + sibling popover slot.

    The anchor's ``href`` points at ``/r/{kind}/{id}`` (the resolver
    redirector) so right-click → "Open in new tab" still works without
    needing JS. htmx + Alpine drive the hover preview.

    ``label`` overrides the visible text — used by the draft display-link
    form ``[text](kind:id)`` so the reader sees ``text``, not the raw
    handle. The href / preview target are unchanged.
    """
    safe_kind = escape(kind)
    # Strip a leading ``#`` from numeric refs so the URL path stays
    # clean: ``memory:#6184`` and ``memory:6184`` both route to
    # ``/r/memory/6184``.
    cleaned_id = raw_id.lstrip("#")
    safe_id = escape(cleaned_id)
    if label is not None:
        display = escape(label)
    else:
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
    href = f"/r/{safe_kind}/{safe_id}{suffix_q}"
    # Carry the chunk into the preview too, so a ``paper:slug~N`` hover
    # shows chunk N's quote (not the paper's first chunk).
    return _anchor_html(
        href=href,
        preview_url=f"/preview/{safe_kind}/{safe_id}{suffix_q}",
        label=display,
    )


# Anchor CSS for the lighter external-link anchor (no hover popover).
_LINK_CLASS = "text-sky-700 underline decoration-dotted hover:decoration-solid"


#: Default anchor styling for inline refs (chips override via anchor_cls).
_ANCHOR_CLS = "text-sky-700 underline decoration-dotted hover:decoration-solid"

#: Compact-reader styling for a paper citation, split local vs external. A
#: *local* cite — a paper chunk we hold in the corpus — keeps the familiar
#: sky ``§``; an *external* cite — a paper we don't have ingested — pops amber
#: with a ``↗`` glyph, so a reader scanning the prose sees at a glance which
#: citations are grounded in a source they can open. ``local=None`` (every
#: non-draft call site) means "don't distinguish": local styling, unchanged.
_CITE_LOCAL_CLS = _ANCHOR_CLS
_CITE_EXTERNAL_CLS = "text-amber-600 underline decoration-dotted hover:decoration-solid"

#: A Taproot claim-hub cite — a violet marker so it reads distinctly from a
#: sky ``§`` paper cite. Hover shows the claim + its evidence; click opens the
#: ``/claim`` page.
_CLAIM_ANCHOR_CLS = "text-violet-700 underline decoration-dotted hover:decoration-solid"
_CLAIM_SIGIL = "◆"

#: Named window target for a paper citation (the claim-UX paper-at-position
#: behaviour): successive clicks on paper cites reuse ONE side window instead of
#: spawning a new tab per click, so a reader can keep the manuscript and the
#: cited paper open side-by-side. Only the compact ``§`` glyph cite
#: (:func:`_render_compact_cite`) and a paper-*chunk* (``pc<id>``) universal
#: handle get this — every other anchor keeps the ordinary ``target="_blank"``
#: (a fresh tab per ref).
_PAPER_WINDOW = "precis-paper"

#: The four reserved ``target`` keywords. A named window (anything NOT in this
#: set — e.g. ``precis-paper``) must NOT carry ``rel="noopener"``: per the HTML
#: spec noopener makes the browser treat any custom target name like ``_blank``,
#: so every click would spawn a fresh tab instead of reusing the one named
#: window (the whole point of :data:`_PAPER_WINDOW`). Reserved keywords keep
#: ``noopener`` — a fresh ``_blank`` tab should not get a live ``window.opener``
#: back into the manuscript. Named reuse is same-origin internal navigation, so
#: the retained opener is harmless.
_RESERVED_TARGETS = frozenset({"_blank", "_self", "_parent", "_top"})

#: Inline claim-hub cite grammar — the same heads + pins
#: :data:`precis.utils.pub_id_lookup.PLACEHOLDER_RE` mines, but with named
#: groups so :func:`_linkify_prose`'s dispatch stays a group-name check. It
#: sits AFTER the display-link pattern in :data:`_COMBINED_PATTERN` (so a
#: ``[method](target)`` display link whose text is six lowercase letters is
#: consumed whole first) and BEFORE the bare-bracket pattern (so a hub
#: ``[fi123]`` becomes a claim anchor rather than a generic finding anchor).
_CLAIM_CITE_PATTERN = re.compile(
    r"\[(?P<claimhead>(?:fi[0-9]+)|(?:[a-z2-7]{6}))"
    r"(?:(?P<claimop>[>+])(?P<claimpins>[a-z]{2}[0-9]+(?:,[a-z]{2}[0-9]+)*))?\]"
)


def _cite_style(key: str, local: frozenset[str] | None) -> tuple[str, str]:
    """``(anchor_cls, glyph)`` for a compact paper citation keyed by ``key``
    (a normalised ``pc``/``pa`` handle or a ``§`` slug). External — amber
    ``↗`` — when a ``local`` set is supplied and ``key`` isn't in it; local —
    sky ``§`` — otherwise (including the ``local is None`` legacy default)."""
    if local is not None and key not in local:
        return _CITE_EXTERNAL_CLS, "↗"
    return _CITE_LOCAL_CLS, "§"


#: Seed content for a hover-preview card, replaced by the htmx swap when the
#: fetch lands. The card is REVEALED on a 200ms hover-intent timer that is
#: deliberately independent of the fetch (``hx-trigger="mouseenter once"``
#: fires immediately, and the result is cached for every later hover) — so a
#: preview slower than 200ms used to paint an empty white box for the gap, the
#: "sometimes it shows nicely, sometimes just an empty box" flake. It is the
#: FIRST hover on any given anchor that can be slow; every one after it hits
#: the already-swapped card, which is why the same chip looked fine on a
#: retry. Seeding the card means the slow case reads as "loading…", never as
#: a blank card. A failed fetch is rewritten to a terminal message by the
#: delegated ``htmx:responseError``/``htmx:sendError`` listener in
#: templates/base.html.j2 — ``once`` means there is no second attempt, so the
#: placeholder must not be left sitting there claiming to still be loading.
_POPOVER_PLACEHOLDER = (
    '<span class="ref-popover-status block px-1 py-1.5 text-xs text-slate-400">'
    "loading…</span>"
)


def _anchor_html(
    *,
    href: str,
    preview_url: str,
    label: str,
    anchor_cls: str = _ANCHOR_CLS,
    target: str = "_blank",
    extra_attrs: str = "",
) -> str:
    """The shared hover-preview anchor. An ``<a href>`` (so right-click /
    open-in-new-tab work without JS) wrapped in an Alpine/htmx span that
    eagerly fetches a popover card from ``preview_url`` on hover, then
    shows it after a hover-intent delay. ``href``, ``preview_url`` and
    ``label`` must already be HTML-safe.

    ``target`` defaults to ``"_blank"`` (a fresh tab per click); a caller
    passing a named window (e.g. ``"precis-paper"``)
    gets one reused window across successive clicks instead.
    ``extra_attrs`` is spliced verbatim onto the ``<a>`` tag (already
    HTML-safe, caller-built — e.g. ``data-claim-head="…"``) so a call site
    can carry extra hooks for client-side JS without every anchor needing
    to know about them.

    Single source for every reference surface — ``kind:ref`` mentions AND
    ``¶`` draft-chunk cross-refs — so hover-preview + click-navigate are
    identical across kinds.

    The popover card is a ``<template x-teleport="body">`` (gripe 56806):
    Alpine relocates its content to ``<body>`` at init, so it always
    escapes whatever ``overflow:auto`` pane it was linkified into (a
    clipping ancestor clips a ``position:absolute``/``fixed`` descendant
    regardless of z-index — physical relocation is the only fix). The
    teleported clone stays in the WRAPPER's reactive scope (Alpine tracks
    ``x-show``/event listeners/``@click.outside`` against the originating
    component, not the DOM location it's rendered at), so the existing
    open/close state machine below still drives it unchanged. Because the
    clone is a document-order-detached ``<body>`` child, ``hx-target``
    can't use a DOM-adjacency selector any more — each anchor mints its
    own popover id and targets it directly.

    Cross-anchor coordination (only ≤1 popover open at a time; Escape /
    an outside scroll closes it) used to be N *window*-scoped listeners —
    ``@ref-popover-open.window`` / ``@keydown.escape.window`` /
    ``@scroll.window.capture`` — one triple per anchor, so a citation-heavy
    draft (thousands of refs) fired thousands of capture-phase handlers
    on every scroll tick (gr171760). That coordination now lives in ONE
    delegated ``window`` listener pair, installed once page-wide by
    ``templates/base.html.j2`` as ``window.__refPopover`` (``open(el)`` /
    ``release(el)``, tracking the single currently-open wrapper element
    and reaching into its Alpine scope via ``Alpine.$data(el)``). Each
    anchor only calls into that shared object — it attaches no window
    listener of its own. ``@click.outside`` stays per-anchor (Alpine's own
    ``outside`` directive, unrelated cost shape — one document click
    listener either way, not one per scroll/keydown tick).
    """
    # ``whitespace-normal`` on the popover container resets the
    # ``white-space: pre-wrap`` it inherits from the parent ``<pre>``
    # on ref-detail pages — otherwise every newline in the popover
    # Jinja template becomes visible vertical whitespace and the card
    # reads like it's been double-spaced. ``max-h-72`` + ``overflow-y-auto``
    # keep very long previews inside a tidy 18rem-tall box rather than
    # growing the popover off-screen.
    # The hover/leave handlers live on the outer span (not the anchor)
    # so moving the mouse from the link onto the popover doesn't close
    # it — Alpine sees a single bounding box that includes both. Now that
    # the popover is teleported out to <body> it is no longer physically
    # inside that bounding box, so the card gets its OWN mouseenter/leave
    # pair below that shares the same component state — see the "pointer
    # bridge" note on ``delayed_close_expr``.
    #
    # Robustness affordances guard against the stuck-popover / vanishes-
    # too-soon symptoms we've hit in the wild:
    #
    # 1. ``setTimeout`` + ``clearTimeout`` on enter/leave — mouseleave
    #    cancels the pending hover so a quick fly-by never opens it.
    # 2. ``window.__refPopover.open($el)`` (gr171760) — closes whichever
    #    OTHER popover is currently open (cluster-wide ≤1 open at a time)
    #    by calling straight into its Alpine scope, no event needed. See
    #    the module-level note below ``_anchor_html`` for why this replaced
    #    a per-anchor ``@ref-popover-open.window`` listener.
    # 3. ``@click.outside`` — clicking anywhere outside the span shuts
    #    the popover. Belt-and-suspenders for the Safari case where
    #    mouseleave never fires (touch input, scroll past, swipe). Alpine
    #    treats the teleported card as "inside" for this purpose, so a
    #    click on the card itself does not count as outside.
    # 4. A DELAYED close (~120ms), cancellable by a mouseenter on EITHER
    #    the wrapper (re-entering the link) or the teleported card —
    #    otherwise the physical gap between the link and a now-teleported,
    #    independently-positioned card would fire mouseleave and close it
    #    before the pointer arrives (gripe 56806 regression #1).
    # 5. Escape and an OUTSIDE window scroll also close the popover, since a
    #    ``position:fixed`` card computed from a point-in-time bounding rect
    #    won't track a scrolling page — handled by the single delegated
    #    ``window`` listener below (gr171760), not a per-anchor one.
    open_expr = (
        "clearTimeout(hoverTimer); clearTimeout(closeTimer); "
        "hoverTimer = setTimeout(() => { "
        "const r = $el.getBoundingClientRect(); "
        "const below = r.bottom + 8 + 200 < window.innerHeight; "
        "const left = Math.max(4, Math.min(r.left, window.innerWidth - 392)); "
        "popStyle = 'position:fixed;z-index:100;left:' + left + 'px;' + "
        "(below ? ('top:' + (r.bottom + 4) + 'px') "
        ": ('bottom:' + (window.innerHeight - r.top + 4) + 'px')); "
        "window.__refPopover.open($el); "
        "hovered = true; "
        "}, 200)"
    )
    # Delayed close — shared by the wrapper's mouseleave and the
    # teleported card's own mouseleave, so leaving either one starts the
    # same countdown and re-entering either one cancels it. Also releases
    # ``$el`` from the shared "currently open" registry (gr171760) if it's
    # still the one holding it, so a later Escape/scroll is a no-op instead
    # of re-closing an already-closed popover.
    delayed_close_expr = (
        "clearTimeout(hoverTimer); clearTimeout(closeTimer); "
        "closeTimer = setTimeout(() => { hovered = false; "
        "window.__refPopover.release($el); }, 120)"
    )
    # Immediate close — click-outside wants the card gone right away, not
    # after a travel grace.
    immediate_close_expr = (
        "clearTimeout(hoverTimer); clearTimeout(closeTimer); hovered = false; "
        "window.__refPopover.release($el);"
    )
    pop_id = f"refpop-{uuid.uuid4().hex[:10]}"
    attrs = f" {extra_attrs}" if extra_attrs else ""
    # See _RESERVED_TARGETS: a named window must not carry noopener or the
    # browser treats it like _blank and never reuses the one tab.
    rel = ' rel="noopener"' if target in _RESERVED_TARGETS else ""
    return (
        f'<span x-data="{{hovered: false, hoverTimer: null, closeTimer: null, '
        f"popStyle: ''}}\" "
        f'class="relative inline-block" data-popid="{pop_id}" '
        f'@mouseenter="{open_expr}" '
        f'@mouseleave="{delayed_close_expr}" '
        f'@click.outside="{immediate_close_expr}">'
        f'<a class="{anchor_cls}"{attrs} '
        f'href="{href}" target="{target}"{rel} '
        f'hx-get="{preview_url}" '
        f'hx-trigger="mouseenter once" '
        f'hx-target="#{pop_id}" hx-swap="innerHTML">'
        f"{label}</a>"
        f'<template x-teleport="body">'
        f'<span id="{pop_id}" x-ref="card" class="ref-popover w-96 '
        f"rounded-lg border border-slate-200 bg-white shadow-xl px-2 pb-2 pt-2 "
        f'text-sm whitespace-normal max-h-96 overflow-y-auto" '
        f'x-show="hovered" x-cloak :style="popStyle" '
        f'@mouseenter="clearTimeout(closeTimer); hovered = true" '
        f'@mouseleave="{delayed_close_expr}">{_POPOVER_PLACEHOLDER}</span>'
        f"</template>"
        f"</span>"
    )


def _render_chunk_anchor(handle: str, label: str) -> str:
    """A ``¶<handle>`` draft-chunk cross-ref. Same hover-preview +
    click-navigate as a ``kind:ref`` anchor: hover fetches a chunk card
    from ``/preview/chunk/<handle>``; click navigates via ``/c/<handle>``
    (which redirects into the draft reader, anchored at that chunk).

    A ``¶`` whose token isn't a minted handle (a 6-char base-58 code) —
    e.g. an LLM that wrote a numeric id ``¶45650`` — can never resolve, so
    flag it visibly instead of emitting a dead-looking live anchor."""
    if not _is_handle(handle):
        return (
            f'<span style="color:#b91c1c;text-decoration:underline wavy #f87171;'
            f'cursor:help" title="unresolved chunk reference — not a valid '
            f'¶handle (must be a 6-char code, not a numeric id)">'
            f"{escape(label)}</span>"
        )
    safe_h = escape(handle)
    return _anchor_html(
        href=f"/c/{safe_h}",
        preview_url=f"/preview/chunk/{safe_h}",
        label=escape(label),
    )


def _render_ext_anchor(url: str, label: str) -> str:
    """An external ``[text](https://…)`` link. ``url`` is escaped (quotes
    included) into the href so it can't break out of the attribute."""
    return (
        f'<a class="{_LINK_CLASS}" href="{escape(url)}" '
        f'rel="noopener nofollow" target="_blank">{escape(label)}</a>'
    )


def _render_display_link(disp: str, tgt: str, raw: str) -> str:
    """``[disp](target)`` — render an anchor showing ``disp`` when the
    target is a recognised reference; otherwise leave the literal text
    (so prose like ``[see](note)`` survives untouched)."""
    label = disp or tgt
    if tgt.startswith("¶"):
        return _render_chunk_anchor(tgt[1:], label)
    if tgt.startswith("§"):
        m = _DRAFT_CITE_PATTERN.fullmatch(tgt)
        if m is not None:
            return _render_anchor(
                "paper", m.group("slug"), m.group("chunk"), label=label
            )
    if tgt.startswith(("http://", "https://")):
        return _render_ext_anchor(tgt, label)
    universal = _render_universal_handle(tgt, label)
    if universal is not None:
        return universal
    m = _REF_PATTERN.fullmatch(tgt)
    if m is not None and m.group("kind") in _LINKIFY_KINDS:
        if m.group("kind") not in _LOW_SIGNAL_KINDS:
            return _render_anchor(
                m.group("kind"), m.group("id"), m.group("chunk"), label=label
            )
    if PAGE_ANCHOR_HREF_RE.fullmatch(tgt):
        # Marker's inert PDF page-anchor citation — collapse to a plain
        # bracketed ``[11]`` rather than leaking the raw markdown. Scoped to
        # this href shape only, so the general "unresolved target stays
        # literal" behaviour below (load-bearing for authored [see](note))
        # is unchanged. See mentions.strip_page_anchor_links.
        return escape(strip_page_anchor_links(raw))
    return escape(raw)  # not a reference target — keep the literal


def _render_bare_bracket(
    bare: str,
    *,
    compact: bool = False,
    local: frozenset[str] | None = None,
    callouts: dict[str, str] | None = None,
) -> str:
    """``[¶h]`` / ``[§p~n]`` — a sigil ref with no display text.

    In ``compact`` mode (the draft reader) the verbose handle is replaced
    by a 1-char superscript sigil so it doesn't break the reading flow;
    the hover popover + sidebar carry the meaning. A ``[dc…]`` that is a
    render-policy part carries its numeral instead (``callouts``).
    """
    # The universal form: ``[me6184]`` / ``[dc41]`` / ``[pc10]`` — a handle
    # is a ref to something, rendered as an anchor (the 2-char prefix says
    # what it is). In compact mode a chunk handle collapses to a kind sigil
    # (§ paper / Ⓟ patent / ¶ other — see the helper).
    universal = _render_universal_handle(
        bare, bare, compact=compact, local=local, callouts=callouts
    )
    if universal is not None:
        return universal
    if bare.startswith("¶"):
        handle = bare[1:]
        # An invalid token (e.g. a numeric ``¶45650``) never resolves —
        # flag it in both modes rather than emit a dead 1-char sigil.
        if compact and _is_handle(handle):
            # Full-size 1-char ¶ — keeps the sentence flowing but stays an
            # easy hover/click target (a superscript was too small to grab).
            return _anchor_html(
                href=f"/c/{escape(handle)}",
                preview_url=f"/preview/chunk/{escape(handle)}",
                label="¶",
            )
        return _render_chunk_anchor(handle, bare)
    m = _DRAFT_CITE_PATTERN.fullmatch(bare)
    if m is not None:
        if compact:
            return _render_compact_cite(m.group("slug"), m.group("chunk"), local=local)
        return _render_anchor("paper", m.group("slug"), m.group("chunk"), label=bare)
    return escape(f"[{bare}]")


def _render_claim_hub(
    head: str,
    *,
    claims: frozenset[str] | None,
    compact: bool = False,
    local: frozenset[str] | None = None,
    callouts: dict[str, str] | None = None,
) -> str:
    """A Taproot claim-hub cite (``[fi123]`` / ``[<pub_id>]``, optionally
    pinned) whose ``head`` is a known hub for this window → a violet claim
    anchor: hover shows the claim + its evidence, click opens ``/claim``.

    A head that ISN'T a hub (or a call site with no ``claims`` map) falls
    back to the ordinary rendering of the bare ``head`` — the export-only
    pin (`>`/`+` …) is dropped either way (a reader shows the cite, not the
    bibliography directive), so ``[fi42>pa5]`` renders identically to
    ``[fi42]``, and nothing that used to be a plain finding anchor / literal
    changes.
    """
    if claims is None or head not in claims:
        return _render_bare_bracket(
            head, compact=compact, local=local, callouts=callouts
        )
    safe_head = escape(head)
    label = _CLAIM_SIGIL if compact else escape(head)
    return _anchor_html(
        href=f"/claim/{safe_head}",
        preview_url=f"/preview/claim/{safe_head}",
        label=label,
        anchor_cls=_CLAIM_ANCHOR_CLS,
        # Smartdraft's diamond↔rail sync + docked claim pane (smartdraft-
        # claim-ux slice 2) key off this attribute — additive, so every
        # other cite kind's anchor markup is unchanged.
        extra_attrs=f'data-claim-head="{safe_head}"',
    )


def _render_compact_cite(
    slug: str, chunk: str | None, *, local: frozenset[str] | None = None
) -> str:
    """A citation as a full-size 1-char marker (compact draft reader): sky
    ``§`` when the cited paper is one we hold, amber ``↗`` when it's an
    external reference (``local`` decides; ``None`` keeps the sky ``§``)."""
    safe_slug = escape(slug)
    suffix = f"?chunk={escape(chunk[1:])}" if chunk else ""
    anchor_cls, glyph = _cite_style(slug, local)
    return _anchor_html(
        href=f"/r/paper/{safe_slug}{suffix}",
        preview_url=f"/preview/paper/{safe_slug}{suffix}",
        label=glyph,  # full-size 1-char marker (easy to hover), flow intact
        anchor_cls=anchor_cls,
        # href already lands at the cited chunk (the ``?chunk=`` suffix
        # above); the named window means successive clicks reuse ONE paper
        # tab instead of spawning one per citation.
        target=_PAPER_WINDOW,
    )


# Inline-markdown: render **bold**, *italic*, `code`, and <sub>/<sup>. The same
# subset the LaTeX/docx exporters render (precis.export.{latex,docx}) so the
# reader, PDF and Word show one thing. Only SINGLE-``*`` italic is honoured —
# ``_`` italic stays literal because it collides with LaTeX subscripts
# (``$x_1$``). Math is left as $…$ for client-side KaTeX (stashed here so ``*``
# inside a formula, e.g. ``$a*b$``, is never mistaken for emphasis).
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
#: Single-``*`` emphasis — the exact guard docx uses (export/docx ``_SPAN``):
#: no adjacent ``*``/word char (so ``**bold**`` and ``a*b`` are excluded) and
#: no space just inside the stars (so a stray ``2 * 3`` multiplication is left
#: alone). Applied AFTER bold, so a consumed ``**…**`` never reaches it.
_MD_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*]+?)(?<!\s)\*(?!\w)")
#: ``$$…$$`` (display) before ``$…$`` (inline) — stashed verbatim for KaTeX.
_MD_MATH = re.compile(r"\$\$.+?\$\$|\$[^$]+\$", re.DOTALL)
#: Empty-base chemistry math (``C$_{60}$`` / ``UO$_2^{2+}$``): the author put
#: the base outside the ``$…$``, which KaTeX renders as a floating subscript
#: with an empty base. Pull the preceding token in (``C$_{60}$`` → ``$C_{60}$``)
#: — the SAME repair the exporters do (export.latex ``_EMPTY_BASE_MATH`` /
#: ``preprocess_draft_inline``), run before math is stashed.
_MD_EMPTY_BASE_MATH = re.compile(r"(?<!\w)([A-Za-z0-9)\]]+)\$([_^][^$]+)\$")
# Authors mix HTML sub/sup into prose (``NH<sub>2</sub>``, ``g<sup>-1</sup>``).
# After escaping they're ``&lt;sub&gt;…&lt;/sub&gt;``; re-promote that exact
# allowlisted pair back to real tags (content stays escaped → safe).
_MD_SUB = re.compile(r"&lt;sub&gt;(.+?)&lt;/sub&gt;")
_MD_SUP = re.compile(r"&lt;sup&gt;(.+?)&lt;/sup&gt;")


def _md_inline(escaped: str) -> str:
    """Render the bold / italic / code / sub / sup markdown subset over
    ALREADY-ESCAPED text.

    Math and code spans are stashed first (so ``**``/``*`` inside a formula or
    backticks isn't treated as emphasis) and restored last — math verbatim (for
    KaTeX), code wrapped. Operating on escaped text keeps it injection-safe: we
    only ever add a fixed allowlist of wrappers (``<strong>`` / ``<em>`` /
    ``<code>`` / ``<sub>`` / ``<sup>``), never reinterpreting content as HTML.
    """
    # Repair empty-base chemistry math BEFORE stashing, so ``C$_{60}$`` becomes
    # a well-formed ``$C_{60}$`` KaTeX renders (parity with the exporters).
    escaped = _MD_EMPTY_BASE_MATH.sub(r"$\1\2$", escaped)

    stash: list[tuple[str, str]] = []

    def _hide(kind: str, body: str) -> str:
        stash.append((kind, body))
        return f"\x00{len(stash) - 1}\x00"

    # Carve out verbatim spans first — math ($…$, kept for KaTeX) then code.
    s = _MD_MATH.sub(lambda m: _hide("math", m.group(0)), escaped)
    s = _MD_CODE.sub(lambda m: _hide("code", m.group(1)), s)

    # Bold before italic so a ``**…**`` run is consumed before single-* italic.
    s = _MD_BOLD.sub(r"<strong>\1</strong>", s)
    s = _MD_ITALIC.sub(r"<em>\1</em>", s)
    s = _MD_SUB.sub(r"<sub>\1</sub>", s)
    s = _MD_SUP.sub(r"<sup>\1</sup>", s)

    def _restore(m: re.Match[str]) -> str:
        kind, body = stash[int(m.group(1))]
        if kind == "math":
            return body  # verbatim $…$ for client-side KaTeX
        return f'<code class="rounded bg-slate-100 px-1 text-[0.9em]">{body}</code>'

    return re.sub(r"\x00(\d+)\x00", _restore, s)


#: Per-kind 1-char sigil for a *compact* chunk-handle anchor (the draft
#: reader). A chunk handle points into some kind's body; the sigil says
#: which kind without spelling the verbose code, keeping the reading flow.
#: ``¶`` (paragraph) is the generic "a block" default — draft cross-refs
#: (``dc41``) fall into it, matching the legacy ``[¶h]`` form. Specific
#: kinds override with a glyph that reads as *its* citation:
#:   * paper  → ``§`` — a cited section (same glyph as the ``[§slug~n]`` form)
#:   * patent → ``Ⓟ`` — a cited patent passage (full-size circled P, U+24C5,
#:     not the small ``℗`` sound-recording mark)
#:   * structure → ``⚛`` — a cited simulation structure (``[stNNN]``,
#:     U+269B ATOM SYMBOL); without an entry here compact mode fell through
#:     to the verbose handle (``st245406``) mid-prose (qu164903 dossier
#:     audit, slice A item 3 — shipped item; git log keeps the backlog file)
#: Notes / dreams etc. are never inline chunk refs (they surface in the
#: sidebar Connections panel), so they need no entry here.
_CHUNK_SIGIL: dict[str, str] = {"paper": "§", "patent": "Ⓟ", "structure": "⚛"}
_CHUNK_SIGIL_DEFAULT = "¶"


def _render_universal_handle(
    handle: str,
    label: str,
    *,
    compact: bool = False,
    local: frozenset[str] | None = None,
    callouts: dict[str, str] | None = None,
) -> str | None:
    """An universal handle (``dc41`` chunk, ``pc10`` paper chunk,
    ``me5`` record, …) → an anchor. The one rule: a handle is a ref to
    something. A chunk navigates via ``/c/<handle>`` (which resolves draft
    AND paper/other chunks) with its quote on hover from
    ``/preview/chunk/<handle>``; a record via ``/r/<kind>/<pk>``. ``None`` if
    ``handle`` isn't a well-formed universal handle.

    In ``compact`` mode (the draft reader) a *chunk* handle collapses to a
    full-size, kind-specific sigil (:data:`_CHUNK_SIGIL`: ``§`` paper, ``Ⓟ``
    patent, ``¶`` for any other block) — easy hover/click target, no verbose
    code breaking the flow; the popover + click carry the meaning. Record
    handles (``me5``) keep their label — they aren't paragraph pointers.
    Compact only collapses the *bare* handle; a display link
    (``[text](dc41)``) keeps its text."""
    parsed = handle_registry.parse(handle)
    if parsed is None:
        return None
    kind, is_chunk, pk = parsed
    if is_chunk:
        norm = handle_registry.normalize(handle)
        h = escape(norm)
        # A render-policy part reference shows its **numeral** — the
        # display label the callout series assigns at render — in place of the
        # verbose handle / ¶ sigil, still hovering the part. Takes precedence
        # over the sigil since the numeral *is* the reference in patent prose.
        numeral = callouts.get(norm) if callouts else None
        if numeral is not None:
            anchor_cls = _ANCHOR_CLS
            shown = escape(str(numeral))
        # Full-size sigil (not a <sup>) so it stays an easy hover/click target.
        # A *paper* chunk cite (``pc10``) additionally colours local vs external
        # (sky ``§`` / amber ``↗``); other chunk kinds keep the neutral sigil.
        elif compact and kind == "paper":
            anchor_cls, shown = _cite_style(norm, local)
        else:
            anchor_cls = _ANCHOR_CLS
            shown = (
                _CHUNK_SIGIL.get(kind, _CHUNK_SIGIL_DEFAULT)
                if compact
                else escape(label)
            )
        return _anchor_html(
            href=f"/c/{h}",
            preview_url=f"/preview/chunk/{h}",
            label=shown,
            anchor_cls=anchor_cls,
            # A paper-chunk handle (``pc10``) already lands at the cited
            # passage (``/c/<h>`` resolves through the universal-chunk →
            # ``/r/paper/<id>?chunk=<ord>`` redirect chain); the named
            # window reuses one paper tab across successive citations
            # — other chunk kinds keep ``_blank``.
            target=_PAPER_WINDOW if kind == "paper" else "_blank",
        )
    # A record handle. In the compact draft reader an inline *evidence
    # citation* — a paper/patent referenced in the prose, e.g. ``[pa42624]``
    # — collapses to its 1-char sigil (``§`` / ``Ⓟ``), the same treatment a
    # ``paper:slug`` cite or a paper *chunk* handle already gets. Without
    # this a run of cites (``[pa1][pa2][pa3]``) rendered as the verbose
    # ``pa1pa2pa3`` run-on. Other record kinds (memory, conv, …) keep their
    # label — they surface as Connections chips, not inline citations.
    if compact and kind in _CHUNK_SIGIL:
        if kind == "paper":
            anchor_cls, sigil = _cite_style(handle_registry.normalize(handle), local)
        else:
            anchor_cls, sigil = _ANCHOR_CLS, _CHUNK_SIGIL[kind]
        return _anchor_html(
            href=f"/r/{kind}/{pk}",
            preview_url=f"/preview/{kind}/{pk}",
            label=sigil,
            anchor_cls=anchor_cls,
        )
    return _anchor_html(
        href=f"/r/{kind}/{pk}", preview_url=f"/preview/{kind}/{pk}", label=escape(label)
    )


def _render_authoring(
    addr: str,
    *,
    compact: bool = False,
    local: frozenset[str] | None = None,
    callouts: dict[str, str] | None = None,
) -> str:
    """``[[<handle>]]`` — the universal reference form. A handle resolves
    to an anchor (chunk / record); a legacy ``[[kind:id]]`` authoring link
    surfaces its inner handle for discoverability when it is a known kind;
    anything else stays literal. A ``[[dc…]]`` render-policy part shows its
    numeral (``callouts``)."""
    universal = _render_universal_handle(
        addr, addr, compact=compact, local=local, callouts=callouts
    )
    if universal is not None:
        return universal
    m = _REF_PATTERN.fullmatch(addr)
    if m is not None and m.group("kind") in _LINKIFY_KINDS:
        if m.group("kind") not in _LOW_SIGNAL_KINDS:
            return _render_anchor(m.group("kind"), m.group("id"), m.group("chunk"))
    return escape(f"[[{addr}]]")


#: Splits rendered HTML into tag (``<…>``) vs text runs so the abbrev
#: highlighter only ever rewrites the text between tags — never inside an
#: attribute, href, or tag name.
_TAG_SPLIT = re.compile(r"(<[^>]+>)")


def _term_pop_html(entry: object) -> str:
    """Inner HTML for a registry surface's ``.pa-pop`` tooltip.

    A plain glossary/patent entry (``{definition}`` or a bare ``str``) renders
    just the definition, exactly as before. A manufacturing **part** entry adds
    optional rows from its attribute bag — MPN, manufacturer, and a datasheet
    link (an ``<a>`` that stays clickable because hovering it keeps ``:hover``
    on the enclosing ``.pa``). A term carrying a dedicated ``abbrev`` (gripe
    56690) shows it too, e.g. hovering ``stereolithography`` surfaces ``STL``.
    Every value is HTML-escaped here."""
    if isinstance(entry, str):
        return f'<span class="pa-def">{escape(entry)}</span>'
    e = entry if isinstance(entry, dict) else {}
    parts = [f'<span class="pa-def">{escape(str(e.get("definition", "")))}</span>']
    abbrev = e.get("abbrev")
    if abbrev:
        parts.append(f'<span class="pa-attr">{escape(str(abbrev))}</span>')
    mpn = e.get("mpn")
    if mpn:
        parts.append(f'<span class="pa-attr">MPN {escape(str(mpn))}</span>')
    mfr = e.get("manufacturer")
    if mfr:
        parts.append(f'<span class="pa-attr">{escape(str(mfr))}</span>')
    url = e.get("url")
    if url:
        parts.append(
            f'<a class="pa-link" href="{escape(str(url))}" '
            f'target="_blank" rel="noopener nofollow">datasheet ↗</a>'
        )
    return "".join(parts)


def _highlight_abbrevs(html: str, terms: dict[str, object]) -> str:
    """Wrap each occurrence of a known surface ``short`` (in the *text* runs of
    already-rendered HTML) in an ``<abbr>`` carrying an **instant** custom
    tooltip — the record rides in a nested ``.pa-pop`` span shown on hover/focus
    via CSS (``.pa`` styling lives in the draft reader's ``<style>``). We
    deliberately avoid the native ``title=`` tooltip: its ~1s browser-controlled
    show-delay is the "lag" — the content is already inline, so there's nothing
    to precompute, only a faster way to reveal it.

    ``terms`` maps each string surface (an abbreviation ``short``, a part name,
    a ``surface_forms`` alias, or an ``mpn``) to either a bare definition
    ``str`` (a glossary term / inline pair) or a rich ``TermEntry`` dict (a
    manufacturing part — definition + attribute bag).

    Operates on the final HTML: splits off ``<…>`` tag runs and only rewrites
    the plain-text runs between them, so a surface that happens to look like
    part of an attribute / handle is never touched. Longest surfaces first so
    ``RNA-seq`` wins over ``RNA``. The matched text is already HTML-escaped; the
    record is escaped in :func:`_term_pop_html`.

    The matcher is compiled **once** for the whole HTML (not per call site), so
    a long draft doesn't recompile the alternation per chunk.
    """
    if not terms:
        return html
    shorts = sorted((s for s in terms if s), key=len, reverse=True)
    if not shorts:
        return html
    # Match the defined surface, plus an optional plural / possessive
    # inflection (``FET`` → ``FETs`` / ``FET's``) so an inflected mention
    # inherits the same hover. We only store the base form; the suffix is
    # matched here, not in ``defined_terms``.
    # Trailing guard is ``(?!\w)`` (not ``(?![\w-])``) so a defined surface
    # used as a hyphenated-compound prefix still highlights its base —
    # ``GNR`` in ``GNR-FETs`` / ``GNR-based``. The leading ``(?<![\w-])``
    # still prevents matching inside a longer token (e.g. ``AGNR``).
    pat = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(s) for s in shorts) + r")"
        r"(?:s|es|'s|’s)?(?!\w)"
    )

    def _wrap(m: re.Match[str]) -> str:
        short = m.group(1)  # the defined key
        shown = m.group(0)  # surface + any inflection, e.g. "FETs"
        return (
            f'<abbr class="pa" tabindex="0">{shown}'
            f'<span class="pa-pop">{_term_pop_html(terms[short])}</span></abbr>'
        )

    parts = _TAG_SPLIT.split(html)
    for i, part in enumerate(parts):
        if not part.startswith("<"):
            parts[i] = pat.sub(_wrap, part)
    return "".join(parts)


def linkify_refs(
    value: str,
    footnotes: dict[tuple[str, str, str | None], int] | None = None,
    *,
    markdown: bool = False,
    compact: bool = False,
    abbrevs: dict[str, object] | None = None,
    local: frozenset[str] | None = None,
    callouts: dict[str, str] | None = None,
    claims: frozenset[str] | None = None,
) -> Markup:
    """Replace ``kind:ref`` mentions in ``value`` with hover-preview anchors.

    ``value`` is treated as **plain text**: all of it is HTML-escaped
    except for the anchor markup this filter generates per match. This
    is the safe contract for every call site — they all pass raw store
    fields (titles, bodies, console output), never trusted HTML — and
    it closes the page-corruption / stored-XSS hole that a verbatim
    passthrough opened (a literal ``<title>`` / ``<script>`` in a title
    would otherwise render as a live element).

    ``footnotes`` — optional ``{(kind, id, chunk): N}`` map (the
    References-panel numbering on memory detail pages). When a prefixed
    ``kind:ref`` mention's key is present, a ``[N]`` superscript anchor
    (linking to ``#ref-N``) is appended after its hover anchor. This is
    composed *inside* the escaping pass so the marker HTML is the only
    live markup — the body never has raw ``<a>`` spliced into it (which
    the old pre-injection path did, and which the escaping rewrite would
    otherwise neutralise).

    ``local`` — the draft reader's local-vs-external citation set (see
    :func:`_cite_style`): the normalised ``pc``/``pa`` handles and ``§``
    slugs whose paper we actually hold. A compact paper cite not in the set
    renders as an amber ``↗`` external marker instead of the sky ``§``.
    ``None`` (every non-draft call site) keeps the uniform ``§``.

    ``callouts`` — the draft reader's ``{normalised dc-handle: numeral}`` map
    for ``assign="render"`` registry parts: a bare ``[[dc…]]`` /
    ``[dc…]`` reference to such a part renders as its **numeral** (e.g. ``105``)
    instead of the ``¶`` sigil, still hover-previewing the part. ``None`` (every
    non-part reference / call site) is unchanged.

    ``claims`` — the reader's set of hub-cite heads (``fi123`` / a 6-char
    pub_id) that resolve to a live ``TAPROOT:claim`` hub in this window. A
    matching ``[head]`` / ``[head>…]`` / ``[head+…]`` cite renders as a violet
    claim anchor (hover = claim + evidence, click = ``/claim``). A head not in
    the set — or ``None`` (every non-reader call site) — keeps its prior
    rendering, so this is a no-op until a reader opts in.

    Returns a :class:`markupsafe.Markup` instance so Jinja's autoescape
    treats the result as already-safe HTML.
    """
    if not value:
        return Markup("")
    html = _linkify_prose(
        str(value),
        footnotes,
        markdown=markdown,
        compact=compact,
        local=local,
        callouts=callouts,
        claims=claims,
    )
    if abbrevs:
        html = _highlight_abbrevs(html, abbrevs)
    return Markup(html)


def _footnote_marker(n: int) -> str:
    """``[N]`` superscript anchor jumping to the References-panel entry."""
    return (
        f'<sup class="text-sky-700 ml-0.5">'
        f'<a href="#ref-{n}" class="hover:underline">[{n}]</a></sup>'
    )


#: Combined alternation so the three pattern shapes (prefixed
#: ``kind:ref``, bare conv handle, bare paper cite_key) consume a
#: given span ONCE — otherwise a sequential-substitution pass would
#: re-match cite_keys inside the anchors produced by the first pass.
#: Order in the alternation matters: longer/more specific shapes
#: first so the regex engine commits to them before falling through
#: to the broad bare paper pattern.
#: The draft bracket forms come FIRST so ``[text](memory:1)`` is consumed
#: whole (display link) rather than the inner ``memory:1`` matching the
#: bare ``kind:ref`` shape. Authoring (``[[…]]``) precedes the display
#: form so it wins on doubled brackets. The bracket groups carry unique
#: names (auth / disp+tgt / bare) so dispatch stays a group-name check.
_COMBINED_PATTERN = re.compile(
    _AUTHORING_PATTERN.pattern
    + r"|"
    + _DISPLAY_LINK_PATTERN.pattern
    + r"|"
    + _CLAIM_CITE_PATTERN.pattern
    + r"|"
    + _BARE_BRACKET_REF_PATTERN.pattern
    + r"|"
    r"(?P<ref>" + _REF_PATTERN.pattern + r")"
    r"|"
    r"(?P<bare_conv>" + _BARE_CONV_PATTERN.pattern + r")"
    r"|"
    r"(?P<bare_paper>" + _BARE_PAPER_PATTERN.pattern + r")"
)


def _linkify_prose(
    prose: str,
    footnotes: dict[tuple[str, str, str | None], int] | None = None,
    *,
    markdown: bool = False,
    compact: bool = False,
    local: frozenset[str] | None = None,
    callouts: dict[str, str] | None = None,
    claims: frozenset[str] | None = None,
) -> str:
    """Replace every ``kind:ref``, bare conv handle, and bare paper
    cite_key in plain prose with an anchor — single pass so we never
    double-match inside an anchor we just produced.

    Text *between* matches (and any match that falls through to plain
    text) is HTML-escaped; only ``_render_anchor`` emits live markup.
    Walking the matches by hand (rather than ``re.sub``) lets us escape
    the inter-match gaps — ``re.sub`` would copy them through verbatim.

    ``markdown`` renders the bold/code subset over the escaped gaps (the
    draft reader). ``compact`` collapses bare ``§``/``¶`` refs to a 1-char
    superscript sigil so they don't break reading flow."""
    if not prose:
        return ""

    def _gap(text: str) -> str:
        e = escape(text)
        return _md_inline(e) if markdown else e

    def _dispatch(m: re.Match[str]) -> str:
        # Draft bracket forms — checked first; their groups
        # are consumed before the bare ``kind:ref`` alternatives.
        if m.group("auth") is not None:
            return _render_authoring(
                m.group("auth"), compact=compact, local=local, callouts=callouts
            )
        if m.group("disp") is not None:
            return _render_display_link(m.group("disp"), m.group("tgt"), m.group(0))
        if m.group("claimhead") is not None:
            return _render_claim_hub(
                m.group("claimhead"),
                claims=claims,
                compact=compact,
                local=local,
                callouts=callouts,
            )
        if m.group("bare") is not None:
            return _render_bare_bracket(
                m.group("bare"), compact=compact, local=local, callouts=callouts
            )
        if m.group("ref") is not None:
            kind = m.group("kind")
            raw_id = m.group("id")
            chunk = m.group("chunk")
            # Allowlist gate: skip kinds that look like ``noun:value``
            # in prose but aren't precis kinds (user:asa, tag:open).
            if kind not in _LINKIFY_KINDS or kind in _LOW_SIGNAL_KINDS:
                return escape(m.group(0))
            # Compact draft reader: a bare ``paper:slug~n`` citation also
            # collapses to a ``§`` superscript so it doesn't break flow.
            if compact and kind == "paper":
                return _render_compact_cite(raw_id, chunk, local=local)
            anchor = _render_anchor(kind, raw_id, chunk)
            if footnotes:
                # Footnote numbering keys on the bare id (no leading ``#``)
                # — same shape ``mentions.extract_handles`` produced.
                n = footnotes.get((kind, raw_id.lstrip("#"), chunk))
                if n is not None:
                    anchor += _footnote_marker(n)
            return anchor
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
            if compact:
                return _render_compact_cite(slug, chunk, local=local)
            return _render_anchor("paper", slug, chunk)
        return escape(m.group(0))

    out: list[str] = []
    last = 0
    for m in _COMBINED_PATTERN.finditer(prose):
        out.append(_gap(prose[last : m.start()]))
        out.append(_dispatch(m))
        last = m.end()
    out.append(_gap(prose[last:]))
    return "".join(out)


_CHIP_CLS = (
    "inline-block max-w-[12rem] truncate rounded bg-slate-100 px-1.5 py-0.5 "
    "text-sky-700 hover:bg-slate-200 align-middle"
)


def popover_chip(
    label: str, href: str, preview_url: str | None, *, warn: str | None = None
) -> Markup:
    """A sidebar reference chip — chip-styled, carrying the same lazy
    hover-preview popover as an inline ref when ``preview_url`` is given
    (so the cited quote shows on hover). External links (no preview) get
    a plain new-tab chip. ``label`` / ``href`` are escaped here.

    When ``warn`` is given, a red ▲ is appended (with ``warn`` as its
    tooltip) — used by the draft reader to flag a cited paper whose PDF is
    held but missing on disk. Chip + marker are wrapped in one inline-flex
    span so they stay glued together when the chip row wraps."""
    safe_label = escape(label)
    if preview_url is None:
        anchor = (
            f'<a class="{_CHIP_CLS}" href="{escape(href)}" '
            f'target="_blank" rel="noopener nofollow">{safe_label}</a>'
        )
    else:
        anchor = _anchor_html(
            href=escape(href),
            preview_url=escape(preview_url),
            label=safe_label,
            anchor_cls=_CHIP_CLS,
        )
    if not warn:
        return Markup(anchor)
    safe_warn = escape(warn)
    return Markup(
        f'<span class="inline-flex items-center gap-0.5">{anchor}'
        f'<span class="text-rose-600" role="img" title="{safe_warn}" '
        f'aria-label="{safe_warn}">&#9650;</span></span>'
    )


def render_markdown(value: str) -> Markup:
    """Render the bold / code / sub / sup markdown subset on plain text —
    no ref-linking (so it's safe to use inside a hover popover without
    spawning nested ref anchors). Math ($…$) is left for client KaTeX."""
    if not value:
        return Markup("")
    return Markup(_md_inline(escape(str(value))))


# ── TOON / TSV tabular rendering ─────────────────────────────────────
#
# Handler ``get`` output is plain text, but a lot of it is *tabular*:
# every ``Next:`` hint block is a TOON table (tab-separated rows, the
# header wrapped in ``{...}`` — see ``precis.format.toon``), and the
# oracle kind is used as a general tab-delimited text share. Inside a
# ``<pre>`` those tabs land on fixed 8-col tab-stops, so the columns
# don't line up and the tabularity is invisible. ``linkify_toon`` splits
# the body into prose runs (linkified + whitespace-preserved as before)
# and tab-separated runs (rendered as aligned HTML ``<table>``s), so a
# shared table reads as a table on the web.

_TOON_TAB = "\t"


def _toon_header_cells(cells: list[str]) -> list[str] | None:
    """If ``cells`` is a TOON braced header (``{col1<TAB>col2}``), return
    the de-braced column names; else ``None``.

    The braces are visible markers on the first cell's open and the last
    cell's close (``precis.format.toon._braced_header``), so we peel one
    ``{`` off the front and one ``}`` off the back.
    """
    if not cells:
        return None
    first, last = cells[0], cells[-1]
    if not (first.startswith("{") and last.endswith("}")):
        return None
    out = list(cells)
    out[0] = out[0][1:]
    out[-1] = out[-1][:-1]
    return out


_TOON_TD = (
    "border border-slate-200 px-2 py-1 align-top text-slate-700 "
    "whitespace-pre-wrap break-words"
)
_TOON_TH = (
    "border border-slate-200 bg-slate-50 px-2 py-1 text-left "
    "font-semibold text-slate-600 whitespace-pre-wrap break-words"
)


def _render_toon_table(
    run: list[str],
    footnotes: dict[tuple[str, str, str | None], int] | None,
) -> str:
    """Render a run of tab-separated lines as an aligned HTML table.

    Cell text is linkified (so ``kind:ref`` handles inside a cell stay
    clickable) via the same escaping pass the surrounding prose uses; a
    braced first row becomes a ``<thead>`` header.
    """
    rows = [line.split(_TOON_TAB) for line in run]
    header = _toon_header_cells(rows[0])
    if header is not None:
        rows = rows[1:]
    ncols = max((len(r) for r in rows), default=0)
    if header is not None:
        ncols = max(ncols, len(header))

    def _cell(text: str, cls: str) -> str:
        return f'<td class="{cls}">{_linkify_prose(text, footnotes)}</td>'

    parts: list[str] = ['<table class="my-2 border-collapse text-xs font-sans">']
    if header is not None:
        cells = header + [""] * (ncols - len(header))
        head = "".join(
            f'<th class="{_TOON_TH}">{_linkify_prose(c, footnotes)}</th>' for c in cells
        )
        parts.append(f"<thead><tr>{head}</tr></thead>")
    parts.append("<tbody>")
    for row in rows:
        padded = row + [""] * (ncols - len(row))
        tds = "".join(_cell(c, _TOON_TD) for c in padded)
        parts.append(f"<tr>{tds}</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def linkify_toon(
    value: str,
    footnotes: dict[tuple[str, str, str | None], int] | None = None,
) -> Markup:
    """Linkify a plain-text body, rendering tab-separated runs as tables.

    A drop-in replacement for ``linkify_refs`` on ref-detail bodies. Runs
    of consecutive lines that each contain a tab are rendered as an
    aligned ``<table>`` (a TOON ``{...}`` header row becomes a ``<thead>``);
    everything else is linkified prose in a ``whitespace-pre-wrap`` block,
    exactly as before. A lone tab-bearing line with no ``{...}`` header is
    left as prose — the guard keeps an incidental tab from becoming a
    one-row table.

    Returns :class:`markupsafe.Markup` (safe HTML — cells + prose are
    escaped by :func:`_linkify_prose`).
    """
    if not value:
        return Markup("")
    lines = str(value).split("\n")
    out: list[str] = []
    prose: list[str] = []

    def _flush_prose() -> None:
        if not prose:
            return
        html = _linkify_prose("\n".join(prose), footnotes)
        out.append(f'<div class="whitespace-pre-wrap">{html}</div>')
        prose.clear()

    i, n = 0, len(lines)
    while i < n:
        if _TOON_TAB in lines[i]:
            run: list[str] = []
            while i < n and _TOON_TAB in lines[i]:
                run.append(lines[i])
                i += 1
            has_header = _toon_header_cells(run[0].split(_TOON_TAB)) is not None
            if has_header or len(run) >= 2:
                _flush_prose()
                out.append(_render_toon_table(run, footnotes))
            else:
                prose.extend(run)
        else:
            prose.append(lines[i])
            i += 1
    _flush_prose()
    return Markup("".join(out))


#: A cloze deletion with the cN index + answer + optional ``::hint`` split
#: out for display. Mirrors ``precis.handlers.anki._CLOZE_RE`` (which drops
#: the index + hint); here we keep them to render the card legibly.
_CLOZE_DISPLAY_RE = re.compile(r"\{\{c(\d+)::(.+?)(?:::(.+?))?\}\}", re.DOTALL)


def render_cloze(text: str) -> Markup:
    """Render an Anki cloze body legibly instead of printing raw markup.

    ``{{c1::answer::hint}}`` → the *answer*, styled as a highlighted
    deletion with the cloze index as a small superscript and the hint (if
    any) on hover. Everything outside a deletion is HTML-escaped verbatim
    (the same untrusted-text contract as :func:`linkify_refs`), so a card
    body can't inject markup. Text with no cloze deletion renders as plain
    escaped prose — a safe no-op.
    """
    out: list[str] = []
    pos = 0
    for m in _CLOZE_DISPLAY_RE.finditer(text or ""):
        out.append(escape(text[pos : m.start()]))
        idx, answer, hint = m.group(1), m.group(2), m.group(3)
        title = f"cloze c{escape(idx)}"
        if hint:
            title += f" · hint: {escape(hint)}"
        out.append(
            f'<span class="rounded bg-amber-100 text-amber-900 px-1 '
            f'border-b border-dotted border-amber-400" title="{title}">'
            f"{escape(answer)}"
            f'<sup class="text-[0.6em] text-amber-500 ml-0.5">c{escape(idx)}</sup>'
            f"</span>"
        )
        pos = m.end()
    out.append(escape(text[pos:] if text else ""))
    return Markup("".join(out))


__all__ = [
    "linkify_refs",
    "linkify_toon",
    "popover_chip",
    "render_cloze",
    "render_markdown",
]
