"""Tests for the inline ``kind:ref`` linkifier filter.

Pure Python — no Postgres, no FastAPI. The HTML routes that consume the
filter's output (``/preview/...``, ``/r/...``) are exercised in
``test_routes.py`` with the FakeStore fixture.
"""

from __future__ import annotations

import re

from precis_web.linkify import linkify_refs, render_cloze


def test_no_refs_passes_through_unchanged() -> None:
    assert str(linkify_refs("just plain prose here")) == "just plain prose here"


def test_simple_paper_ref_becomes_anchor() -> None:
    out = str(linkify_refs("See paper:acheson26 for details."))
    assert 'href="/r/paper/acheson26"' in out
    assert "paper:acheson26" in out  # display preserved
    assert 'hx-get="/preview/paper/acheson26"' in out


def test_numeric_ref_strips_hash_in_url() -> None:
    out = str(linkify_refs("memory:#6184 covers this."))
    assert 'href="/r/memory/6184"' in out
    # Display preserves the user's literal text including the ``#``.
    assert "memory:#6184" in out


def test_numeric_ref_without_hash_resolves_same() -> None:
    out = str(linkify_refs("memory:6184 covers this."))
    assert 'href="/r/memory/6184"' in out


def test_paper_chunk_address_carried_through() -> None:
    out = str(linkify_refs("paper:acheson26~7 page reference"))
    assert 'href="/r/paper/acheson26?chunk=7"' in out
    # Display shows the full address.
    assert "paper:acheson26~7" in out


def test_paper_chunk_range_address() -> None:
    out = str(linkify_refs("paper:inamuddin21~5..9 for this."))
    assert "?chunk=5..9" in out


def test_paper_page_address_uses_p_prefix() -> None:
    out = str(linkify_refs("paper:inamuddin21~p23 talks about it."))
    assert "?chunk=p23" in out


def test_multiple_refs_in_one_string() -> None:
    out = str(linkify_refs("see paper:foo and memory:42 for context"))
    assert 'href="/r/paper/foo"' in out
    assert 'href="/r/memory/42"' in out


def test_untrusted_html_is_escaped_not_rendered() -> None:
    """Regression — a raw ``<title or DOI>`` placeholder in a planner
    prompt used to render as a live ``<title>`` element, flipping the
    HTML tokenizer to RAWTEXT and swallowing the rest of the page
    (every inline ``<script>`` after it stopped firing — the Tasks
    filter/collapse buttons went dead with no JS error). Input is plain
    text now: angle brackets are escaped, never opened as a tag."""
    out = str(linkify_refs("search q='<title or DOI>' then mint put(kind='finding')"))
    assert "<title" not in out  # no live element
    assert "&lt;title or DOI&gt;" in out


def test_script_injection_is_escaped() -> None:
    """Stored-XSS guard: a todo/memory body containing a ``<script>`` is
    escaped to inert text, not executed."""
    out = str(linkify_refs("<script>alert(1)</script>"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_literal_html_tags_in_text_are_escaped_but_ref_still_links() -> None:
    """A literal ``<code>`` in plain text is escaped (it is not a real
    code block — every caller passes text, not HTML), and a real
    ``kind:ref`` mention beside it still linkifies."""
    out = str(linkify_refs("Use <code>X</code> with paper:acheson26"))
    assert "<code>" not in out
    assert "&lt;code&gt;" in out
    assert 'href="/r/paper/acheson26"' in out


def test_foreign_anchor_markup_is_escaped() -> None:
    """An ``<a href=...>`` typed into a body is inert text, not a live
    link — only the anchors this filter generates are trusted."""
    raw = 'click <a href="elsewhere">paper:foo</a> here'
    out = str(linkify_refs(raw))
    assert '<a href="elsewhere">' not in out
    assert "&lt;a href=" in out
    # Our own generated anchor for the ref is still emitted.
    assert 'href="/r/paper/foo"' in out


def test_word_boundary_prevents_runon_capture() -> None:
    """``memory:6184foobar`` should NOT capture — the regex requires that
    the id be followed by a non-word character (or end of string)."""
    out = str(linkify_refs("ids like memory:6184foobar should not link"))
    assert 'href="/r/memory/' not in out


def test_anchor_uses_settimeout_for_hover_delay() -> None:
    """The hover delay rides through a setTimeout/clearTimeout pair on the
    outer span — NOT Alpine's ``.debounce`` modifier. The debounce form
    had a race where a delayed mouseenter would fire after mouseleave
    already closed the popover, leaving an orphaned card on screen."""
    out = str(linkify_refs("paper:acheson26"))
    assert "@mouseenter.debounce" not in out  # the bug — must stay removed
    # clearTimeout on both enter (idempotency) and leave (cancel pending).
    assert "clearTimeout(hoverTimer)" in out
    assert "setTimeout(() => {" in out


def test_anchor_has_htmx_eager_preview_attributes() -> None:
    """gripe 56681 residual: the preview fragment starts fetching the
    INSTANT the pointer enters (no 200ms wait), so it's already loaded by
    the time the (separately-debounced) hover-intent SHOWS the card. Only
    once — the fetch is cached after the first hover."""
    out = str(linkify_refs("paper:acheson26"))
    assert 'hx-trigger="mouseenter once"' in out
    assert "delay:200ms" not in out  # that delay now gates the SHOW, not the fetch


def test_popover_teleported_to_body() -> None:
    """gripe 56806: the popover card lives in a ``<template x-teleport=
    "body">`` so Alpine relocates it to <body> at init, escaping any
    overflow-clipped ancestor pane (smartdraft's reader columns)."""
    out = str(linkify_refs("paper:acheson26"))
    assert '<template x-teleport="body">' in out
    assert "ref-popover" in out


def test_popover_card_is_seeded_so_a_slow_fetch_is_never_a_blank_box() -> None:
    """The 200ms hover-intent timer reveals the card independently of the
    fetch, so a preview slower than that used to paint an empty white box —
    the "sometimes it shows nicely, sometimes just an empty box" flake (and
    only ever on the FIRST hover of an anchor, since ``once`` caches the
    result for the rest of the page's life). The card ships with a
    ``loading…`` seed that the htmx swap replaces."""
    out = str(linkify_refs("paper:acheson26"))
    card = out[out.index('<template x-teleport="body">') :]
    assert "loading…" in card
    assert "ref-popover-status" in card  # the hook base.html.j2 rewrites on error
    # …and it really is INSIDE the card (so hx-swap="innerHTML" clears it),
    # not a sibling that would survive the swap and stack above the preview.
    assert 'hx-swap="innerHTML"' in out
    assert card.rstrip().endswith("</span></template></span>")


def test_popover_hx_target_is_unique_id_not_dom_adjacency() -> None:
    """Once teleported, the card is no longer a DOM sibling of the anchor,
    so ``hx-target`` can't rely on ``next .ref-popover`` any more — each
    anchor mints its own popover id and targets it directly."""
    out = str(linkify_refs("paper:acheson26"))
    assert 'hx-target="next .ref-popover"' not in out
    m = re.search(r'id="(refpop-[0-9a-f]+)"', out)
    assert m is not None
    assert f'hx-target="#{m.group(1)}"' in out


def test_teleported_card_has_its_own_pointer_bridge() -> None:
    """gripe 56806 regression #1: moving the pointer from the (now
    teleported) trigger across the gap onto the card must not close it —
    the card gets its own mouseenter (cancel the pending close) / mouseleave
    (schedule one) pair sharing the wrapper's component state."""
    out = str(linkify_refs("paper:acheson26"))
    card_start = out.index('<template x-teleport="body">')
    card = out[card_start:]
    assert '@mouseenter="clearTimeout(closeTimer); hovered = true"' in card
    assert "closeTimer = setTimeout(() => { hovered = false; " in card


def test_wrapper_close_is_delayed_not_immediate() -> None:
    """The wrapper's mouseleave used to close instantly; now it schedules a
    ~120ms close so the pointer has time to reach the teleported card."""
    out = str(linkify_refs("paper:acheson26"))
    enter_idx = out.index("@mouseenter=")
    leave_idx = out.index("@mouseleave=", enter_idx)
    wrapper_leave = out[leave_idx : out.index('"', out.index('"', leave_idx) + 1)]
    assert "closeTimer = setTimeout" in wrapper_leave


def test_anchor_has_no_per_anchor_window_listeners() -> None:
    """gr171760: an anchor-heavy draft (thousands of refs) used to attach
    THREE window-scoped listeners PER anchor (``@scroll.window.capture``,
    ``@keydown.escape.window``, ``@ref-popover-open.window``) — thousands of
    capture-phase handlers firing on every scroll tick. That coordination
    now lives in one delegated listener pair installed once, page-wide, by
    ``templates/base.html.j2`` (``window.__refPopover``) — so a single
    anchor's markup must carry none of these three any more."""
    out = str(linkify_refs("paper:acheson26"))
    assert "@scroll.window.capture=" not in out
    assert "@keydown.escape.window=" not in out
    assert "@ref-popover-open.window=" not in out
    assert "$dispatch('ref-popover-open'" not in out


def test_anchor_delegates_open_close_to_shared_popover_registry() -> None:
    """Each anchor calls into the shared ``window.__refPopover`` registry
    instead of managing cross-anchor coordination itself: ``open($el)`` on
    hover-in (closes whichever OTHER popover is open, tracks this one) and
    ``release($el)`` on every close path (mouseleave-delay / click-outside /
    card mouseleave), so a later page-wide Escape/scroll is a no-op once
    this popover has already closed."""
    out = str(linkify_refs("paper:acheson26"))
    assert "window.__refPopover.open($el)" in out
    assert out.count("window.__refPopover.release($el)") >= 2
    # The wrapper carries its popover id as a data attribute so the shared,
    # delegated scroll listener (base.html.j2) can find its teleported card
    # without needing Alpine's own (per-component) ``$refs``.
    m = re.search(r'id="(refpop-[0-9a-f]+)"', out)
    assert m is not None
    assert f'data-popid="{m.group(1)}"' in out


def test_popover_card_own_mouseleave_still_shares_close_expr() -> None:
    """The teleported card's own mouseleave still schedules the SAME
    delayed-close (which also releases it from the shared registry) as the
    wrapper's — leaving either one starts one countdown, re-entering either
    one cancels it (gripe 56806's "pointer bridge")."""
    out = str(linkify_refs("paper:acheson26"))
    card_start = out.index('<template x-teleport="body">')
    card = out[card_start:]
    assert "closeTimer = setTimeout(() => { hovered = false; " in card
    assert "window.__refPopover.release($el); }, 120)" in card


def test_popover_closes_on_click_outside() -> None:
    """Belt-and-suspenders for Safari, where touch/scroll can leave a
    popover open with no follow-up mouseleave."""
    out = str(linkify_refs("paper:acheson26"))
    assert "@click.outside=" in out


def test_hover_listeners_on_outer_span_not_anchor() -> None:
    """Listeners must live on the outer wrapper so moving the cursor
    from the anchor onto the popover doesn't fire mouseleave on the
    anchor and close the popover before the user can read it."""
    out = str(linkify_refs("paper:acheson26"))
    # The outer x-data span carries the handlers — find the open of
    # the x-data span and verify @mouseenter is on it, not on <a>.
    open_idx = out.index("<span x-data")
    anchor_idx = out.index("<a class=")
    enter_idx = out.index("@mouseenter")
    assert open_idx < enter_idx < anchor_idx


def test_empty_string_returns_empty() -> None:
    assert str(linkify_refs("")) == ""


def test_none_value_returns_empty() -> None:
    # `value` isn't Optional, but the `if not value` guard makes None a
    # genuinely handled case (Jinja passes raw, possibly-NULL DB fields).
    assert str(linkify_refs(None)) == ""  # type: ignore[arg-type]


# ---- Allowlist gate (no false positives on prose tokens) -------------


def test_user_colon_handle_is_NOT_linkified() -> None:
    """``user:asa`` is prose shorthand, not a precis kind. Must fall
    through to plain text so the resolver doesn't get a 404 request."""
    out = str(linkify_refs("asked user:asa about it"))
    assert "/r/user/asa" not in out
    assert "user:asa" in out
    assert "<a" not in out


def test_note_colon_thing_is_NOT_linkified() -> None:
    out = str(linkify_refs("note:keep this in mind"))
    assert "/r/note/" not in out
    assert "<a" not in out


def test_tag_colon_value_is_NOT_linkified() -> None:
    """``tag:open`` etc. are ambient tag namespaces, not refs."""
    out = str(linkify_refs("filed under tag:open and tier:dream"))
    assert "/r/tag/" not in out
    assert "/r/tier/" not in out
    assert "<a" not in out


def test_real_kind_in_allowlist_still_linkifies() -> None:
    """Regression check — the allowlist gate must not break the
    happy path for every kind we DO want as a link."""
    for kind in [
        "memory",
        "todo",
        "paper",
        "patent",
        "youtube",
        "perplexity-research",
    ]:
        out = str(linkify_refs(f"see {kind}:foo for context"))
        assert f"/r/{kind}/foo" in out, f"{kind} should linkify"


# ---- Popover layout flags (whitespace + max-height) ------------------


def test_popover_breaks_inherited_pre_whitespace() -> None:
    """The popover lives inside a ``<pre class='whitespace-pre-wrap'>``
    on detail pages. Without ``whitespace-normal`` on the popover
    container the popover's own template newlines become visible
    vertical gaps in the rendered card."""
    out = str(linkify_refs("paper:acheson26"))
    assert "whitespace-normal" in out


def test_popover_caps_height_for_long_content() -> None:
    """Long titles / body previews must stay inside a scrollable box
    rather than growing the popover off-screen."""
    out = str(linkify_refs("paper:acheson26"))
    assert "max-h-96" in out  # widened for cite quotes (≤ ~20 lines)
    assert "overflow-y-auto" in out


# ---- Path-shape slugs (conv handles) ---------------------------------


def test_prefixed_conv_path_slug_linkifies() -> None:
    """``conv:discord/<server>/<channel>/<thread>`` was getting cut at
    the first ``/`` because the id-group rejected slashes."""
    handle = "discord/1490327108830892182/1515091538529619979/1515091538529619979"
    out = str(linkify_refs(f"see conv:{handle} for context"))
    assert f"/r/conv/{handle}" in out
    assert f"conv:{handle}" in out  # display preserved


def test_prefixed_conv_path_slug_with_chunk_address() -> None:
    """The ``~N`` chunk suffix rides through path slugs too."""
    handle = "discord/1490327108830892182/1515091538529619979/1515091538529619979"
    out = str(linkify_refs(f"conv:{handle}~31"))
    assert f"/r/conv/{handle}?chunk=31" in out


def test_bare_discord_handle_linkifies_to_conv() -> None:
    """Asa-bot emits ``discord/<server>/<channel>/<thread>`` without a
    ``conv:`` prefix in memory bodies. The linkifier maps the bare
    handle to the ``conv`` kind."""
    handle = "discord/1490327108830892182/1515091538529619979/1515091538529619979"
    out = str(linkify_refs(f"continued from {handle} earlier"))
    assert f"/r/conv/{handle}" in out


def test_bare_discord_handle_with_chunk_suffix() -> None:
    handle = "discord/1490327108830892182/1515091538529619979/1515091538529619979"
    out = str(linkify_refs(f"see {handle}~31"))
    assert f"/r/conv/{handle}?chunk=31" in out


def test_bare_discord_handle_requires_all_three_path_segments() -> None:
    """``discord/general`` is prose, not a conv handle — don't linkify."""
    out = str(linkify_refs("posted in discord/general"))
    assert "/r/conv/" not in out
    assert "<a" not in out


# ---- Bare paper cite_keys -------------------------------------------


def test_bare_paper_cite_key_with_chunk_address_linkifies() -> None:
    """``xu25f~12`` is unambiguously a paper chunk pointer — the
    chunk suffix disambiguates it from prose."""
    out = str(linkify_refs("see xu25f~12 for the proof"))
    assert "/r/paper/xu25f?chunk=12" in out


def test_bare_paper_cite_key_three_letter_surname() -> None:
    """Bare cite_keys without a chunk suffix need ≥3 letters of
    surname to dodge false positives like ``ml22``."""
    out = str(linkify_refs("acheson26 covers the topic"))
    assert "/r/paper/acheson26" in out


def test_bare_paper_cite_key_with_disambig_letter() -> None:
    """``futrell25b`` (the et-al disambig suffix) is a real shape."""
    out = str(linkify_refs("see futrell25b"))
    assert "/r/paper/futrell25b" in out


def test_bare_paper_two_letter_no_chunk_does_NOT_match() -> None:
    """``ml22`` / ``ai99`` are false-positive risks — require ≥3 letters
    of surname when there's no chunk suffix."""
    out = str(linkify_refs("the ml22 conference and ai99 problem"))
    assert "/r/paper/ml22" not in out
    assert "/r/paper/ai99" not in out


def test_bare_paper_two_letter_with_chunk_DOES_match() -> None:
    """With a chunk suffix the pattern relaxes: ``xu25~3`` is plausibly
    a paper chunk pointer even with a 2-letter surname."""
    out = str(linkify_refs("xu25~3 has the data"))
    assert "/r/paper/xu25?chunk=3" in out


def test_prefixed_paper_doesnt_double_linkify_into_anchor() -> None:
    """After ``paper:acheson26`` becomes an anchor, the bare-cite-key
    pass must NOT re-match ``acheson26`` inside the anchor — that would
    nest <a> tags and break the popover."""
    out = str(linkify_refs("paper:acheson26 and acheson26"))
    # Exactly two anchor opens — one for the prefixed match, one for
    # the bare cite_key in the second half. Not three.
    assert out.count("<a ") == 2


def test_prose_word_not_linkified() -> None:
    """Plain prose words without the cite_key shape don't get linkified."""
    out = str(linkify_refs("the morning paper was good"))
    assert "<a " not in out


def test_html5_not_linkified_only_one_digit() -> None:
    """``html5`` has only ONE digit — the pattern requires exactly 2."""
    out = str(linkify_refs("html5 spec"))
    assert "/r/paper/html5" not in out


def test_covid19_IS_linkified_known_acceptable_false_positive() -> None:
    """``covid19`` shaped exactly like a cite_key (5 letters + 2 digits).
    We accept this as a known false positive — the resolver 404s cleanly
    so the hover popover just shows 'no such paper'. The cost of a tight
    enough regex to exclude it would also exclude real surnames like
    ``covid``."""
    out = str(linkify_refs("covid19 study"))
    assert "/r/paper/covid19" in out


# --- Draft superset grammar -----------------------------------
# The same filter highlights the bracket / sigil forms a draft chunk may
# carry, in addition to the bare ``kind:ref`` mentions above.


def test_display_link_to_kind_ref_shows_text_not_handle() -> None:
    out = str(linkify_refs("see [the intro](memory:6184) please"))
    assert ">the intro<" in out  # display text is the anchor label
    assert "/r/memory/6184" in out  # …pointing at the resolver
    assert "memory:6184" not in out  # raw handle is hidden behind the text


def test_display_link_to_paper_chunk_carries_address() -> None:
    out = str(linkify_refs("as [Miller](paper:miller89~4) showed"))
    assert ">Miller<" in out and "/r/paper/miller89" in out
    assert "chunk=4" in out


def test_display_link_section_sigil_points_at_chunk_route() -> None:
    out = str(linkify_refs("recall [the setup](¶5BL5xQ) above"))
    assert ">the setup<" in out and 'href="/c/5BL5xQ"' in out


def test_citation_sigil_resolves_to_paper() -> None:
    out = str(linkify_refs("per [Miller](§miller89~4)"))
    assert "/r/paper/miller89" in out and "chunk=4" in out and ">Miller<" in out


def test_bare_bracket_xref_renders_handle_anchor() -> None:
    out = str(linkify_refs("see [¶5BL5xQ]"))
    assert 'href="/c/5BL5xQ"' in out and ">¶5BL5xQ<" in out


def test_universal_handle_renders_anchor() -> None:
    # The one rule: a handle in brackets is a ref to something. A chunk
    # handle navigates via /c/, a record handle via /r/<kind>/<pk>.
    out = str(linkify_refs("see [dc41] and [me5]"))
    assert 'href="/c/dc41"' in out
    assert 'href="/r/memory/5"' in out


def test_pinned_finding_handle_renders_same_anchor_as_bare() -> None:
    # Taproot slice A2 (Phase 2): `[fi42>pa5]` / `[fi42+pa5]` is an
    # export-time directive, not reader content — the reader drops the
    # pin and renders the SAME finding anchor as the bare handle. The
    # popover id is a random per-call nonce, so normalize it before
    # comparing.
    def _norm(s: str) -> str:
        return re.sub(r"refpop-[0-9a-f]{10}", "refpop-X", s)

    bare = _norm(str(linkify_refs("see [fi42]")))
    replace_pin = _norm(str(linkify_refs("see [fi42>pa5,pc9]")))
    supplement_pin = _norm(str(linkify_refs("see [fi42+pa5]")))
    assert bare == replace_pin == supplement_pin
    assert 'href="/r/finding/42"' in bare


def test_paper_chunk_handle_renders_hoverable_anchor() -> None:
    # A paper-chunk handle [pc10] is a ref to a paper chunk — it must hover
    # (the chunk preview) + click through, same as a draft chunk, not be
    # left dead. Routes resolve any chunk kind via /c/ + /preview/chunk/.
    out = str(linkify_refs("supported by [pc10]"))
    assert 'href="/c/pc10"' in out
    assert 'hx-get="/preview/chunk/pc10"' in out or "/preview/chunk/pc10" in out


def test_paper_chunk_handle_is_section_sigil_in_compact() -> None:
    # In the draft reader (compact) a paper-chunk handle collapses to a §
    # citation sigil (hover carries the meaning); a draft chunk stays ¶.
    paper = str(linkify_refs("text [pc10] here", compact=True))
    assert "/preview/chunk/pc10" in paper and ">§<" in paper
    draft = str(linkify_refs("text [dc41] here", compact=True))
    assert "/preview/chunk/dc41" in draft and ">¶<" in draft


def test_paper_record_handle_is_section_sigil_in_compact() -> None:
    # In the draft reader (compact) an inline paper *record* citation
    # [pa42624] collapses to a § sigil too (not just paper *chunk* handles)
    # so a run of cites [pa1][pa2][pa3] reads as §§§ markers rather than the
    # verbose "pa1pa2pa3" run-on. The anchor points at the paper record.
    out = str(linkify_refs("pathogens [pa42624][pa3655]", compact=True))
    assert out.count(">§<") == 2
    assert "/r/paper/42624" in out and "/r/paper/3655" in out
    assert "pa42624" not in out  # the verbose label is gone


def test_record_handle_keeps_label_when_not_compact() -> None:
    # Outside the compact reader a paper record handle keeps its full label.
    out = str(linkify_refs("see [pa42624] here"))
    assert "/r/paper/42624" in out and ">pa42624<" in out


def test_non_evidence_record_keeps_label_in_compact() -> None:
    # A non-citation record kind (memory) keeps its label even in compact —
    # it's a Connections pointer, not an inline cite.
    out = str(linkify_refs("see [me5] here", compact=True))
    assert "/r/memory/5" in out and ">me5<" in out


def test_non_handle_bracket_stays_literal() -> None:
    # A bracketed non-handle isn't a ref — left as prose.
    out = str(linkify_refs("see [the note] below"))
    assert "[the note]" in out and "href" not in out


def test_display_link_to_handle_renders_anchor() -> None:
    out = str(linkify_refs("[the intro](dc41)"))
    assert 'href="/c/dc41"' in out and ">the intro<" in out


def test_bare_bracket_citation_renders_paper_anchor() -> None:
    out = str(linkify_refs("see [§miller89~4]"))
    assert "/r/paper/miller89" in out and ">§miller89~4<" in out


def test_authoring_link_surfaces_inner_handle() -> None:
    out = str(linkify_refs("background [[memory:7]] informs this"))
    # the [[ ]] wrapper is dropped; the inner handle becomes an anchor
    assert "/r/memory/7" in out and "[[" not in out


def test_external_display_link_opens_new_tab() -> None:
    out = str(linkify_refs("visit [DDG](https://duckduckgo.com)"))
    assert ">DDG<" in out and 'href="https://duckduckgo.com"' in out
    assert "nofollow" in out


def test_unrecognised_display_target_stays_literal() -> None:
    # ``[see](note)`` is prose, not a reference — left untouched (escaped)
    out = str(linkify_refs("a [see](note) here"))
    assert "[see](note)" in out and "<a" not in out


def test_marker_page_anchor_cite_renders_clean_bracket() -> None:
    # Marker turns an inline citation "[11]" into a link [11](#page-5-0) whose
    # target is inert PDF-viewer nav chrome. It must render as a plain "[11]",
    # NOT leak the raw markdown (contrast the [see](note) case above, whose
    # literal fallback is load-bearing and stays untouched).
    out = str(linkify_refs("our previous Letter [11](#page-5-0)."))
    assert "[11](#page-5-0)" not in out
    assert "(#page-5-0)" not in out
    assert "[11]" in out
    assert out.count("11") == 1  # no duplication
    assert "<a" not in out  # no dead anchor emitted


def test_display_link_target_is_escaped_no_attribute_breakout() -> None:
    out = str(linkify_refs('[x](https://e.com" onclick="alert(1))'))
    # the double-quote in the URL must be escaped, never closing the attr
    assert 'onclick="alert(1)"' not in out


# --- Reader rendering: markdown subset + compact sigils -----------


def test_markdown_bold_rendered_when_enabled() -> None:
    out = str(linkify_refs("yield **2.63 mmol** today", markdown=True))
    assert "<strong>2.63 mmol</strong>" in out


def test_markdown_inline_code_rendered() -> None:
    out = str(linkify_refs("call `embed_one(q)` now", markdown=True))
    assert "<code" in out and "embed_one(q)" in out


def test_markdown_italic_single_star_rendered() -> None:
    # Single-* emphasis → <em> (parity with the LaTeX/docx exporters). ** stays
    # bold, a spaced multiplication is left alone, and _ italic is NOT rendered
    # (it collides with $x_1$ subscripts).
    out = str(
        linkify_refs("a *directly bonded* pair, **bold**, 2 * 3, x_1_", markdown=True)
    )
    assert "<em>directly bonded</em>" in out
    assert "<strong>bold</strong>" in out and "<em>bold</em>" not in out
    assert "2 * 3" in out  # spaced multiplication untouched
    assert "<em>" in out and out.count("<em>") == 1  # the _..._ did not italicise


def test_markdown_star_inside_math_not_emphasis() -> None:
    # A * inside $…$ (multiplication) must stay math, not become <em>.
    out = str(linkify_refs("the product $a*b$ here", markdown=True))
    assert "$a*b$" in out and "<em>" not in out


def test_markdown_empty_base_math_folds_base_in() -> None:
    # Chemistry math with the base outside the $…$ (C$_{60}$) is repaired to
    # $C_{60}$ so KaTeX renders a proper subscripted base — parity with export.
    out = str(linkify_refs("the C$_{60}$ cage and UO$_2^{2+}$ ion", markdown=True))
    assert "$C_{60}$" in out and "$UO_2^{2+}$" in out


def test_markdown_off_by_default_keeps_raw() -> None:
    out = str(linkify_refs("a **b** c"))
    assert "<strong>" not in out and "**b**" in out


def test_markdown_escapes_before_wrapping() -> None:
    out = str(linkify_refs("**<script>**", markdown=True))
    assert "<strong>" in out and "<script>" not in out and "&lt;script&gt;" in out


def test_compact_citation_is_one_char_marker() -> None:
    out = str(linkify_refs("see [§kong24~2] here", compact=True))
    assert ">§</a>" in out  # full-size 1-char marker (easy hover), not <sup>
    assert "/r/paper/kong24?chunk=2" in out
    assert "§kong24~2" not in out  # verbose handle hidden


def test_compact_xref_is_one_char_marker() -> None:
    out = str(linkify_refs("recall [¶aB3xQ9]", compact=True))
    assert ">¶</a>" in out and 'href="/c/aB3xQ9"' in out


def test_compact_universal_chunk_handle_uses_kind_sigil() -> None:
    # A universal *chunk* handle collapses to a kind-specific sigil in the
    # compact reader (not the verbose code): a paper block (``pc123``) reads
    # as a citation §, a draft block (``dc41``) as a paragraph ¶ — both still
    # navigate via /c/ and carry the hover preview.
    out = str(linkify_refs("see [pc123] and [dc41]", compact=True))
    assert ">§</a>" in out and ">¶</a>" in out
    assert 'href="/c/pc123"' in out and 'href="/c/dc41"' in out
    # verbose code hidden from the visible label (still fine in href/preview).
    assert ">pc123</a>" not in out and ">dc41</a>" not in out


def test_compact_patent_chunk_handle_is_circle_p() -> None:
    # A patent block (``pk7``) reads as Ⓟ (full-size circled P) in the
    # compact reader — not the small ℗ sound-recording mark.
    out = str(linkify_refs("see [pk7]", compact=True))
    assert ">Ⓟ</a>" in out and 'href="/c/pk7"' in out
    assert ">pk7</a>" not in out


def test_compact_structure_handle_is_atom_sigil() -> None:
    # qu164903-dossier-audit-residuals slice A item 3: a cited simulation
    # structure ([stNNN], a record handle — structure has no chunk code)
    # collapses to a compact ⚛ sigil in the draft reader, not the verbose
    # "st245406" mid-prose.
    out = str(linkify_refs("see [st245406] for the relaxed geometry", compact=True))
    assert ">⚛</a>" in out and 'href="/r/structure/245406"' in out
    assert ">st245406</a>" not in out


def test_compact_universal_record_handle_stays_verbose() -> None:
    # A *record* handle (``me5``) isn't a paragraph pointer — it keeps its
    # label even in compact mode (only chunk handles collapse to ¶).
    out = str(linkify_refs("background [me5] here", compact=True))
    assert ">me5</a>" in out and 'href="/r/memory/5"' in out


def test_non_compact_universal_chunk_handle_stays_verbose() -> None:
    # Outside the reader the handle keeps its readable code.
    out = str(linkify_refs("see [pc123]"))
    assert ">pc123</a>" in out and 'href="/c/pc123"' in out


def test_compact_display_link_to_chunk_keeps_text() -> None:
    # A display link chose its text — compact must NOT clobber it with ¶.
    out = str(linkify_refs("recall [the block](dc41) above", compact=True))
    assert ">the block</a>" in out and 'href="/c/dc41"' in out


def test_non_compact_citation_stays_verbose() -> None:
    out = str(linkify_refs("see [§kong24~2]"))
    assert "§kong24~2" in out


def test_chunk_address_carried_into_preview_hover() -> None:
    out = str(linkify_refs("paper:kong24~2"))
    assert "/preview/paper/kong24?chunk=2" in out


# --- sub/sup + render_markdown + new-tab -------------------------------------


def test_markdown_renders_sub_and_sup() -> None:
    out = str(linkify_refs("NH<sub>2</sub> at 3.1 mmol g<sup>-1</sup>", markdown=True))
    assert "<sub>2</sub>" in out and "<sup>-1</sup>" in out


def test_render_markdown_filter_no_ref_anchors() -> None:
    from precis_web.linkify import render_markdown

    out = str(render_markdown("see **CO2** `code` x<sub>2</sub> and paper:kong24~2"))
    assert "<strong>CO2</strong>" in out and "<sub>2</sub>" in out and "<code" in out
    # render_markdown does NOT linkify refs (no nested anchors in popovers)
    assert "/r/paper/kong24" not in out and "<a " not in out


def test_render_markdown_escapes_unknown_html() -> None:
    from precis_web.linkify import render_markdown

    out = str(render_markdown("<script>alert(1)</script> <sub>ok</sub>"))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<sub>ok</sub>" in out  # allowlisted tag still promoted


def test_ref_anchor_opens_new_tab() -> None:
    out = str(linkify_refs("paper:kong24~2"))
    assert 'target="_blank"' in out and 'rel="noopener"' in out


# ---- Paper cite anchors reuse a named window + land at the cited passage ---
# (the claim-UX paper-at-position behaviour): the compact § cite and a paper-chunk
# (pc<id>) universal handle both already carry the cited chunk in their href
# (the existing ``?chunk=`` deep-link — the paper reader jumps to it,
# routes_web.papers._cited_chunk + the reader's ``paperDoc()`` JS); this pins
# that they ALSO share one ``target="precis-paper"`` window instead of
# spawning a new tab per citation.


def test_compact_paper_cite_targets_named_window() -> None:
    out = str(linkify_refs("see [§kong24~2] here", compact=True))
    assert 'target="precis-paper"' in out
    assert "/r/paper/kong24?chunk=2" in out  # href already lands at the passage
    # A named target must NOT carry rel="noopener": the HTML spec makes the
    # browser treat noopener'd custom names like _blank, so successive clicks
    # would spawn fresh tabs instead of reusing the one window.
    assert "noopener" not in out


def test_paper_chunk_handle_targets_named_window() -> None:
    out = str(linkify_refs("supported by [pc10]", compact=True))
    assert 'target="precis-paper"' in out
    assert 'href="/c/pc10"' in out  # /c/ resolves through to ?chunk=<ord>
    assert "noopener" not in out  # else the named window is never reused


def test_non_paper_chunk_handle_keeps_default_target() -> None:
    """A draft-chunk handle (¶) isn't a paper citation — it must keep the
    ordinary ``_blank`` target, not the paper window."""
    out = str(linkify_refs("see [dc41] here", compact=True))
    assert 'target="precis-paper"' not in out
    assert 'target="_blank"' in out


def test_paper_record_handle_bare_cite_keeps_default_target() -> None:
    """Only the § compact cite / pc-handle chunk anchors get the named
    window — a non-compact paper ref (outside the compact reader) is
    untouched, still a fresh tab per click."""
    out = str(linkify_refs("paper:kong24~2"))
    assert 'target="precis-paper"' not in out
    assert 'target="_blank"' in out


def test_abbrev_highlight_wraps_known_tokens() -> None:
    """A defined abbreviation is wrapped in an instant-tooltip <abbr.pa>
    (definition in .pa-pop, NOT the laggy native title); the longest short
    wins; word boundaries are respected."""
    out = str(
        linkify_refs(
            "PEI loaded; PEINE is different; mention PEI again.",
            abbrevs={"PEI": "polyethyleneimine"},
        )
    )
    assert out.count('<abbr class="pa"') == 2  # two standalone PEI, not PEINE
    # The definition rides in a .pa-def span inside .pa-pop (rich
    # hover — a part additionally shows MPN/manufacturer/datasheet rows).
    assert '<span class="pa-pop"><span class="pa-def">polyethyleneimine</span>' in out
    assert "title=" not in out  # no native tooltip (that was the lag)
    assert "PEINE" in out  # untouched (PEI is not a whole token there)


def test_abbrev_highlight_skips_tags_and_attrs() -> None:
    """The pass only rewrites text runs — never inside an anchor's href /
    attributes (so an abbrev that collides with a slug is safe)."""
    out = str(
        linkify_refs(
            "see paper:PEI~2 then PEI",
            compact=True,
            abbrevs={"PEI": "polyethyleneimine"},
        )
    )
    # the slug PEI inside the citation href is NOT wrapped …
    assert "/r/paper/PEI" in out
    # … but the bare PEI in prose IS.
    assert '<abbr class="pa"' in out


def test_abbrev_highlight_noop_without_dict() -> None:
    out = str(linkify_refs("PEI everywhere", abbrevs=None))
    assert "<abbr" not in out


def test_abbrev_highlight_covers_plural_inflection() -> None:
    """A defined short form's plural / possessive inflection (FET → FETs /
    FET's) inherits the same hover-definition — we store only the base."""
    out = str(
        linkify_refs(
            "one FET, several FETs, the FET's gate",
            abbrevs={"FET": "field-effect transistor"},
        )
    )
    assert out.count('<abbr class="pa"') == 3
    assert ">FETs<span" in out  # the plural form is the visible text
    assert out.count("field-effect transistor") == 3


def test_abbrev_highlight_shows_dedicated_abbrev_field() -> None:
    """A term's dedicated ``abbrev`` (gripe 56690) — distinct from the
    generic ``short``/``surface_forms`` bag — rides in the rich hover
    record and renders as an attribute row, mirroring MPN/manufacturer."""
    out = str(
        linkify_refs(
            "stereolithography is a common process",
            abbrevs={
                "stereolithography": {
                    "definition": "a 3D printing process",
                    "abbrev": "STL",
                }
            },
        )
    )
    assert '<abbr class="pa"' in out
    assert '<span class="pa-attr">STL</span>' in out


def test_invalid_pilcrow_ref_flagged_not_anchored() -> None:
    """A ¶ token that isn't a minted 6-char handle (e.g. a numeric id an
    LLM invented, ¶45650) renders as a flagged span, never a live anchor —
    in both compact and verbose modes, bracketed or display-link form."""
    for text in ("see [¶45650]", "[the intro](¶45650)"):
        for compact in (True, False):
            out = str(linkify_refs(text, compact=compact))
            assert "unresolved chunk reference" in out
            assert "/c/45650" not in out
    # a real handle still resolves to a live anchor
    ok = str(linkify_refs("see [¶1asdf1]", compact=True))
    assert "/c/1asdf1" in ok


# ── TOON / TSV tabular rendering (linkify_toon) ──────────────────────


def test_toon_next_block_renders_as_table() -> None:
    """A ``Next:`` TOON block (braced header + tab-separated rows) renders
    as an aligned HTML table with a ``<thead>``; the prose above stays a
    whitespace-preserved, non-table block."""
    from precis_web.linkify import linkify_toon

    body = (
        "A proverb.\n\nNext:\n"
        "{if you want to\texecute this call}\n"
        "consult again (random pick)\tget(id='or50948')\n"
        "see all 39 entries\tget(id='or50948/index')"
    )
    out = str(linkify_toon(body))
    assert "<table" in out
    assert "<thead>" in out
    assert ">if you want to</th>" in out
    assert ">execute this call</th>" in out
    # two body rows, four data cells
    assert out.count("<tr>") == 3  # header row + 2 body rows
    # cell content is HTML-escaped (no raw quote breaking the attribute)
    assert "get(id=&#x27;or50948&#x27;)" in out
    # prose preserved outside the table
    assert "A proverb." in out
    assert "whitespace-pre-wrap" in out


def test_toon_lone_tab_line_stays_prose() -> None:
    """A single incidental tab-bearing line (no braced header) is left as
    prose — the guard keeps a stray tab from becoming a one-row table."""
    from precis_web.linkify import linkify_toon

    out = str(linkify_toon("a line\twith one tab and nothing else"))
    assert "<table" not in out
    assert "a line" in out


def test_toon_headerless_multiline_tsv_renders_as_table() -> None:
    """Two+ consecutive tab lines with no ``{...}`` header still tabularise
    (a plain TSV share) — but with no ``<thead>``."""
    from precis_web.linkify import linkify_toon

    out = str(linkify_toon("a\t1\nb\t2\nc\t3"))
    assert "<table" in out
    assert "<thead>" not in out
    assert out.count("<tr>") == 3


def test_toon_ref_handles_in_cells_are_linkified() -> None:
    """A ``kind:ref`` handle inside a table cell stays a clickable anchor."""
    from precis_web.linkify import linkify_toon

    out = str(linkify_toon("see this\tpaper:acheson26\nand that\tmemory:6184"))
    assert 'href="/r/paper/acheson26"' in out
    assert 'href="/r/memory/6184"' in out


def test_toon_ragged_rows_padded() -> None:
    """A row with fewer cells than the header is padded so the table stays
    rectangular."""
    from precis_web.linkify import linkify_toon

    out = str(linkify_toon("{a\tb\tc}\nx\ty"))
    # header has 3 cols; the single body row is padded to 3 <td>s
    assert out.count("<td") == 3


# ── render_cloze: Anki cloze bodies render as highlighted deletions ─────


def test_render_cloze_highlights_deletion_and_hides_braces() -> None:
    out = str(render_cloze("Paris is the {{c1::capital}} of France."))
    # The raw double-brace markup never reaches the reader.
    assert "{{" not in out and "}}" not in out
    # The answer text is shown, styled as a deletion, tagged with its index.
    assert "capital" in out
    assert "<span" in out
    assert "c1" in out


def test_render_cloze_carries_hint_on_hover() -> None:
    out = str(render_cloze("The capital is {{c1::Paris::a French city}}."))
    assert "Paris" in out
    assert "a French city" in out  # hint surfaced (in the title=)
    assert "::" not in out  # the hint separator is consumed, not printed


def test_render_cloze_escapes_untrusted_text() -> None:
    out = str(render_cloze("<script>alert(1)</script> {{c1::x}}"))
    # Surrounding prose is escaped — no live element injected.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_cloze_no_deletion_is_plain_escaped_text() -> None:
    assert str(render_cloze("no cloze here <b>")) == "no cloze here &lt;b&gt;"


# ── Taproot claim-hub cites (the `claims` side-channel) ─────────────────
# A hub cite head (`fi<id>` / a 6-char pub_id) in the `claims` set renders a
# violet claim anchor; a head not in the set keeps its prior rendering, so
# passing no `claims` (every non-reader call site) is a no-op.


def test_claim_hub_head_renders_claim_anchor() -> None:
    out = str(linkify_refs("see [fi123]", compact=True, claims=frozenset({"fi123"})))
    assert 'href="/claim/fi123"' in out
    assert 'hx-get="/preview/claim/fi123"' in out
    assert "◆" in out  # compact claim sigil


def test_claim_hub_anchor_carries_data_claim_head() -> None:
    """The claim-UX diamond↔rail sync: the prose ◆ carries
    ``data-claim-head`` so the reader's diamond↔rail-sync JS can find every
    element citing the same hub."""
    out = str(linkify_refs("see [fi123]", compact=True, claims=frozenset({"fi123"})))
    assert 'data-claim-head="fi123"' in out


def test_pub_id_head_renders_claim_anchor_when_hub() -> None:
    out = str(linkify_refs("x [ab23cd] y", compact=True, claims=frozenset({"ab23cd"})))
    assert 'href="/claim/ab23cd"' in out


def test_pinned_hub_cite_is_recognised() -> None:
    out = str(linkify_refs("[fi123>pc9]", compact=True, claims=frozenset({"fi123"})))
    assert 'href="/claim/fi123"' in out


def test_fi_head_without_claims_map_stays_generic_finding_anchor() -> None:
    """No `claims` → prior behaviour: a bare `[fi123]` is a generic finding
    anchor, not a claim anchor."""
    out = str(linkify_refs("[fi123]", compact=True))
    assert 'href="/r/finding/123"' in out
    assert "/claim/" not in out


def test_non_hub_fi_head_falls_back_even_with_claims_map() -> None:
    out = str(linkify_refs("[fi123]", compact=True, claims=frozenset({"fi999"})))
    assert 'href="/r/finding/123"' in out
    assert "/claim/" not in out


def test_pub_id_shaped_token_stays_literal_when_not_a_hub() -> None:
    assert str(linkify_refs("[ab23cd]", compact=True)) == "[ab23cd]"


def test_claim_pattern_does_not_eat_display_links() -> None:
    """Regression: `[method](paper:5)` is a display link whose text is six
    lowercase letters — the claim pattern must not consume `[method]` and
    strand `(paper:5)`."""
    out = str(linkify_refs("[method](paper:5)", compact=True))
    assert 'href="/r/paper/5"' in out
    assert ">method</a>" in out
    assert "/claim/" not in out


def test_six_char_chunk_handle_unaffected_by_claim_pattern() -> None:
    """A 6-char paper-chunk handle `[pc2345]` still resolves to its chunk
    anchor (routed through the claim fallback, unchanged)."""
    out = str(linkify_refs("[pc2345]", compact=True))
    assert 'href="/c/pc2345"' in out
    assert "/claim/" not in out


# ---- gr171760: the page-wide delegated popover registry --------------
#
# The registry itself is a browser-side singleton (``window.__refPopover``
# in ``templates/base.html.j2``) with no server-side unit surface — these
# checks are necessarily structural (the script text every page ships),
# not behavioral (that needs a real DOM + Alpine runtime, out of reach for
# this pure-Python test module). They pin the delegation contract every
# ``_anchor_html`` call site above already exercises the calling half of.


def _base_template_script() -> str:
    from pathlib import Path

    import precis_web

    path = Path(precis_web.__file__).parent / "templates" / "base.html.j2"
    return path.read_text(encoding="utf-8")


def test_base_template_installs_one_delegated_popover_registry() -> None:
    """``window.__refPopover`` is defined exactly once, page-wide (not per
    anchor), and exposes the ``open``/``release`` calls every anchor's
    markup invokes (see ``test_anchor_delegates_open_close_to_shared_
    popover_registry``)."""
    html = _base_template_script()
    assert html.count("window.__refPopover = ") == 1
    assert "open(el) {" in html
    assert "release(el) {" in html
    # gr171760 also caught a real regression: this template renders on
    # EVERY page (including error pages), and a couple of routes' tests
    # assert no bare, empty ``()`` leaks into an error page's rendered
    # text (an unrelated stale-template-substitution symptom) — so this
    # script must never introduce one (no zero-argument IIFE/call).
    assert "()" not in html


def test_base_template_rewrites_a_failed_preview_off_its_loading_seed() -> None:
    """The card now ships seeded with ``loading…`` so a slow fetch never
    paints a blank box — which means a fetch that FAILS must be rewritten, or
    the card would sit there claiming to load forever (the anchor's
    ``hx-trigger`` is ``mouseenter once``: there is no second attempt).
    Delegated once page-wide, like the open/close registry above."""
    html = _base_template_script()
    assert html.count("document.addEventListener('htmx:responseError'") == 1
    assert html.count("document.addEventListener('htmx:sendError'") == 1
    # …and it only rewrites a card still showing the seed, so it can never
    # clobber a preview that already swapped in.
    assert "querySelector" in html and "ref-popover-status" in html
    assert "preview unavailable" in html


def test_base_template_registry_installs_one_scroll_and_keydown_listener() -> None:
    """The Escape-closes / outside-scroll-closes behavior that used to be a
    ``@keydown.escape.window`` + ``@scroll.window.capture`` pair PER anchor
    is now exactly one ``keydown`` and one capture-phase ``scroll`` listener
    on ``window``, regardless of how many refs the page renders."""
    html = _base_template_script()
    assert html.count("window.addEventListener('keydown'") == 1
    assert html.count("window.addEventListener(\n        'scroll',") == 1
    assert "e.key === 'Escape'" in html
    # Same outside-the-card guard the old per-anchor scroll handler had,
    # just resolved via the tracked element's popover id instead of Alpine's
    # per-component ``$refs``.
    assert "card.contains(e.target)" in html


def test_base_template_release_compares_by_popid_not_raw_element() -> None:
    """Review fix: ``release(el)`` is called from BOTH the wrapper's own
    close handlers (``$el`` = the wrapper, which ``openEl`` tracks) AND the
    teleported card's own mouseleave (``$el`` = the CARD — a different DOM
    node than ``openEl``, since the wrapper and card share only their
    popover id, not identity). Comparing raw element identity (``openEl ===
    el``) would silently never match — and never clear ``openEl`` — on the
    card-mouseleave path, leaving a stale reference until the next
    open/Escape/scroll lazily reconciled it. ``release`` must instead
    resolve both sides to their shared popover id (``popIdOf``) before
    comparing."""
    html = _base_template_script()
    assert "function popIdOf(el)" in html
    release_start = html.index("release(el) {")
    release_body = html[release_start : html.index("},", release_start)]
    assert "popIdOf(openEl) === popIdOf(el)" in release_body
    # The old (buggy) raw-identity comparison must be gone from this method.
    assert "openEl === el" not in release_body
