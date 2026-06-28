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
"""

from __future__ import annotations

import re
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


def _anchor_html(
    *, href: str, preview_url: str, label: str, anchor_cls: str = _ANCHOR_CLS
) -> str:
    """The shared hover-preview anchor. An ``<a href>`` (so right-click /
    open-in-new-tab work without JS) wrapped in an Alpine/htmx span that
    lazily fetches a popover card from ``preview_url`` on hover. ``href``,
    ``preview_url`` and ``label`` must already be HTML-safe.

    Single source for every reference surface — ``kind:ref`` mentions AND
    ``¶`` draft-chunk cross-refs — so hover-preview + click-navigate are
    identical across kinds.
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
        f'<a class="{anchor_cls}" '
        f'href="{href}" target="_blank" rel="noopener" '
        f'hx-get="{preview_url}" '
        f'hx-trigger="mouseenter delay:200ms once" '
        f'hx-target="next .ref-popover" hx-swap="innerHTML">'
        f"{label}</a>"
        # No top margin: the popover sits flush under the label so the
        # cursor can travel from the link onto the card without crossing a
        # dead gap (an absolute popover is out of flow, so a ``mt-1`` gap
        # falls outside the hover span and fires mouseleave → the card
        # vanished before you could click its "open →"). ``pt-2`` keeps the
        # content visually off the label. ``z-[100]`` beats the sidebar's
        # column dividers / change-box so the card isn't painted under them.
        f'<span class="ref-popover absolute z-[100] top-full left-0 w-96 '
        f"rounded-lg border border-slate-200 bg-white shadow-xl px-2 pb-2 pt-2 "
        f'text-sm whitespace-normal max-h-96 overflow-y-auto" '
        f'x-show="hovered" x-cloak></span>'
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
    return escape(raw)  # not a reference target — keep the literal


def _render_bare_bracket(bare: str, *, compact: bool = False) -> str:
    """``[¶h]`` / ``[§p~n]`` — a sigil ref with no display text.

    In ``compact`` mode (the draft reader) the verbose handle is replaced
    by a 1-char superscript sigil so it doesn't break the reading flow;
    the hover popover + sidebar carry the meaning.
    """
    # The universal form: ``[me6184]`` / ``[dc41]`` / ``[pc10]`` — a handle
    # is a ref to something, rendered as an anchor (the 2-char prefix says
    # what it is). In compact mode a chunk handle collapses to a kind sigil
    # (§ paper / Ⓟ patent / ¶ other — see the helper).
    universal = _render_universal_handle(bare, bare, compact=compact)
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
            return _render_compact_cite(m.group("slug"), m.group("chunk"))
        return _render_anchor("paper", m.group("slug"), m.group("chunk"), label=bare)
    return escape(f"[{bare}]")


def _render_compact_cite(slug: str, chunk: str | None) -> str:
    """A citation as a 1-char ``§`` superscript (compact draft reader)."""
    safe_slug = escape(slug)
    suffix = f"?chunk={escape(chunk[1:])}" if chunk else ""
    return _anchor_html(
        href=f"/r/paper/{safe_slug}{suffix}",
        preview_url=f"/preview/paper/{safe_slug}{suffix}",
        label="§",  # full-size 1-char marker (easy to hover), flow intact
    )


# Inline-markdown: render **bold**, `code`, and <sub>/<sup> only. ``_``/``*``
# italic is deliberately NOT rendered — it collides with LaTeX subscripts
# ($x_1$) and is more trouble than it's worth in scientific prose. (Math
# itself is left as $…$ for client-side KaTeX.)
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
# Authors mix HTML sub/sup into prose (``NH<sub>2</sub>``, ``g<sup>-1</sup>``).
# After escaping they're ``&lt;sub&gt;…&lt;/sub&gt;``; re-promote that exact
# allowlisted pair back to real tags (content stays escaped → safe).
_MD_SUB = re.compile(r"&lt;sub&gt;(.+?)&lt;/sub&gt;")
_MD_SUP = re.compile(r"&lt;sup&gt;(.+?)&lt;/sup&gt;")


def _md_inline(escaped: str) -> str:
    """Render the bold / code / sub / sup markdown subset over
    ALREADY-ESCAPED text.

    Code spans are stashed first (so ``**`` inside backticks isn't bolded)
    and restored last. Operating on escaped text keeps it injection-safe —
    we only ever add a fixed allowlist of wrappers (``<strong>`` /
    ``<code>`` / ``<sub>`` / ``<sup>``), never reinterpret arbitrary
    content as HTML.
    """
    stash: list[str] = []

    def _hide(m: re.Match[str]) -> str:
        stash.append(m.group(1))
        return f"\x00{len(stash) - 1}\x00"

    s = _MD_CODE.sub(_hide, escaped)
    s = _MD_BOLD.sub(r"<strong>\1</strong>", s)
    s = _MD_SUB.sub(r"<sub>\1</sub>", s)
    s = _MD_SUP.sub(r"<sup>\1</sup>", s)

    def _restore(m: re.Match[str]) -> str:
        body = stash[int(m.group(1))]
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
#: Notes / dreams etc. are never inline chunk refs (they surface in the
#: sidebar Connections panel), so they need no entry here.
_CHUNK_SIGIL: dict[str, str] = {"paper": "§", "patent": "Ⓟ"}
_CHUNK_SIGIL_DEFAULT = "¶"


def _render_universal_handle(
    handle: str, label: str, *, compact: bool = False
) -> str | None:
    """An ADR 0036 universal handle (``dc41`` chunk, ``pc10`` paper chunk,
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
        h = escape(handle_registry.normalize(handle))
        # Full-size sigil (not a <sup>) so it stays an easy hover/click target.
        shown = (
            _CHUNK_SIGIL.get(kind, _CHUNK_SIGIL_DEFAULT) if compact else escape(label)
        )
        return _anchor_html(
            href=f"/c/{h}", preview_url=f"/preview/chunk/{h}", label=shown
        )
    # A record handle. In the compact draft reader an inline *evidence
    # citation* — a paper/patent referenced in the prose, e.g. ``[pa42624]``
    # — collapses to its 1-char sigil (``§`` / ``Ⓟ``), the same treatment a
    # ``paper:slug`` cite or a paper *chunk* handle already gets. Without
    # this a run of cites (``[pa1][pa2][pa3]``) rendered as the verbose
    # ``pa1pa2pa3`` run-on. Other record kinds (memory, conv, …) keep their
    # label — they surface as Connections chips, not inline citations.
    if compact and kind in _CHUNK_SIGIL:
        return _anchor_html(
            href=f"/r/{kind}/{pk}",
            preview_url=f"/preview/{kind}/{pk}",
            label=_CHUNK_SIGIL[kind],
        )
    return _anchor_html(
        href=f"/r/{kind}/{pk}", preview_url=f"/preview/{kind}/{pk}", label=escape(label)
    )


def _render_authoring(addr: str, *, compact: bool = False) -> str:
    """``[[<handle>]]`` — the universal reference form. A handle resolves
    to an anchor (chunk / record); a legacy ``[[kind:id]]`` authoring link
    surfaces its inner handle for discoverability when it is a known kind;
    anything else stays literal."""
    universal = _render_universal_handle(addr, addr, compact=compact)
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


def _highlight_abbrevs(html: str, abbrevs: dict[str, str]) -> str:
    """Wrap each occurrence of a known abbreviation ``short`` (in the
    *text* runs of already-rendered HTML) in an ``<abbr>`` carrying an
    **instant** custom tooltip — the definition rides in a nested
    ``.pa-pop`` span shown on hover/focus via CSS (``.pa`` styling lives
    in the draft reader's ``<style>``). We deliberately avoid the native
    ``title=`` tooltip: its ~1s browser-controlled show-delay is the "lag"
    — the definition is already inline, so there's nothing to precompute,
    only a faster way to reveal it.

    Operates on the final HTML: splits off ``<…>`` tag runs and only
    rewrites the plain-text runs between them, so an abbreviation that
    happens to look like part of an attribute / handle is never touched.
    Longest shorts first so ``RNA-seq`` wins over ``RNA``. The matched
    text is already HTML-escaped; the definition is escaped here.

    The matcher is compiled **once** for the whole HTML (not per call
    site), so a long draft doesn't recompile the alternation per chunk.
    """
    if not abbrevs:
        return html
    shorts = sorted((s for s in abbrevs if s), key=len, reverse=True)
    if not shorts:
        return html
    # Match the defined short, plus an optional plural / possessive
    # inflection (``FET`` → ``FETs`` / ``FET's``) so an inflected mention
    # inherits the same hover-definition. We only store the base form; the
    # suffix is matched here, not in ``defined_abbrevs``.
    # Trailing guard is ``(?!\w)`` (not ``(?![\w-])``) so a defined acronym
    # used as a hyphenated-compound prefix still highlights its base —
    # ``GNR`` in ``GNR-FETs`` / ``GNR-based``. The leading ``(?<![\w-])``
    # still prevents matching inside a longer token (e.g. ``AGNR``).
    pat = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(s) for s in shorts) + r")"
        r"(?:s|es|'s|’s)?(?!\w)"
    )

    def _wrap(m: re.Match[str]) -> str:
        short = m.group(1)  # the defined key
        shown = m.group(0)  # short + any inflection, e.g. "FETs"
        return (
            f'<abbr class="pa" tabindex="0">{shown}'
            f'<span class="pa-pop">{escape(abbrevs[short])}</span></abbr>'
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
    abbrevs: dict[str, str] | None = None,
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

    Returns a :class:`markupsafe.Markup` instance so Jinja's autoescape
    treats the result as already-safe HTML.
    """
    if not value:
        return Markup("")
    html = _linkify_prose(str(value), footnotes, markdown=markdown, compact=compact)
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
        # Draft bracket forms (ADR 0033 §8) — checked first; their groups
        # are consumed before the bare ``kind:ref`` alternatives.
        if m.group("auth") is not None:
            return _render_authoring(m.group("auth"), compact=compact)
        if m.group("disp") is not None:
            return _render_display_link(m.group("disp"), m.group("tgt"), m.group(0))
        if m.group("bare") is not None:
            return _render_bare_bracket(m.group("bare"), compact=compact)
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
                return _render_compact_cite(raw_id, chunk)
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
                return _render_compact_cite(slug, chunk)
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


def popover_chip(label: str, href: str, preview_url: str | None) -> Markup:
    """A sidebar reference chip — chip-styled, carrying the same lazy
    hover-preview popover as an inline ref when ``preview_url`` is given
    (so the cited quote shows on hover). External links (no preview) get
    a plain new-tab chip. ``label`` / ``href`` are escaped here."""
    safe_label = escape(label)
    if preview_url is None:
        return Markup(
            f'<a class="{_CHIP_CLS}" href="{escape(href)}" '
            f'target="_blank" rel="noopener nofollow">{safe_label}</a>'
        )
    return Markup(
        _anchor_html(
            href=escape(href),
            preview_url=escape(preview_url),
            label=safe_label,
            anchor_cls=_CHIP_CLS,
        )
    )


def render_markdown(value: str) -> Markup:
    """Render the bold / code / sub / sup markdown subset on plain text —
    no ref-linking (so it's safe to use inside a hover popover without
    spawning nested ref anchors). Math ($…$) is left for client KaTeX."""
    if not value:
        return Markup("")
    return Markup(_md_inline(escape(str(value))))


__all__ = ["linkify_refs", "popover_chip", "render_markdown"]
