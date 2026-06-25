"""Tests for the inline ``kind:ref`` linkifier filter.

Pure Python — no Postgres, no FastAPI. The HTML routes that consume the
filter's output (``/preview/...``, ``/r/...``) are exercised in
``test_routes.py`` with the FakeStore fixture.
"""

from __future__ import annotations

from precis_web.linkify import linkify_refs


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


def test_anchor_has_htmx_lazy_preview_attributes() -> None:
    """The preview fragment loads via htmx on first hover only."""
    out = str(linkify_refs("paper:acheson26"))
    assert 'hx-trigger="mouseenter delay:200ms once"' in out


def test_only_one_popover_open_at_a_time() -> None:
    """When one popover opens it dispatches ``ref-popover-open``; every
    other popover listens via ``@ref-popover-open.window`` and closes
    itself. Bounds the open set to ≤1 even if mouseleave misfires."""
    out = str(linkify_refs("paper:acheson26"))
    assert "$dispatch('ref-popover-open'" in out
    assert "@ref-popover-open.window=" in out


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


# --- Draft superset grammar (ADR 0033 §8) -----------------------------------
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


def test_display_link_target_is_escaped_no_attribute_breakout() -> None:
    out = str(linkify_refs('[x](https://e.com" onclick="alert(1))'))
    # the double-quote in the URL must be escaped, never closing the attr
    assert 'onclick="alert(1)"' not in out


# --- Reader rendering: markdown subset + compact sigils (ADR 0033) -----------


def test_markdown_bold_rendered_when_enabled() -> None:
    out = str(linkify_refs("yield **2.63 mmol** today", markdown=True))
    assert "<strong>2.63 mmol</strong>" in out


def test_markdown_inline_code_rendered() -> None:
    out = str(linkify_refs("call `embed_one(q)` now", markdown=True))
    assert "<code" in out and "embed_one(q)" in out


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
    assert '<span class="pa-pop">polyethyleneimine</span>' in out
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
