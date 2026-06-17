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


def test_ref_inside_code_block_left_alone() -> None:
    raw = "Use <code>paper:acheson26</code> in your put call."
    out = str(linkify_refs(raw))
    # The <code> block is preserved verbatim.
    assert "<code>paper:acheson26</code>" in out
    # No anchor for the bracketed mention.
    assert 'href="/r/paper/acheson26"' not in out


def test_ref_inside_pre_block_left_alone() -> None:
    raw = "<pre>get(kind='paper', id='acheson26')</pre>"
    out = str(linkify_refs(raw))
    assert "<pre>" in out
    # Filter shouldn't touch the inside (pre is verbatim).
    assert out.count('href="/r/paper/') == 0


def test_existing_anchor_not_double_wrapped() -> None:
    raw = 'click <a href="elsewhere">paper:foo</a> here'
    out = str(linkify_refs(raw))
    # The original <a> is preserved as-is.
    assert '<a href="elsewhere">paper:foo</a>' in out
    # The original anchor block is one continuous skip-zone; no nested anchor.
    assert out.count('href="/r/paper/') == 0


def test_word_boundary_prevents_runon_capture() -> None:
    """``memory:6184foobar`` should NOT capture — the regex requires that
    the id be followed by a non-word character (or end of string)."""
    out = str(linkify_refs("ids like memory:6184foobar should not link"))
    assert 'href="/r/memory/' not in out


def test_anchor_has_hover_delay_via_alpine_modifiers() -> None:
    """The hover-200ms requirement rides in via Alpine ``debounce``."""
    out = str(linkify_refs("paper:acheson26"))
    assert "@mouseenter.debounce.200ms" in out


def test_anchor_has_htmx_lazy_preview_attributes() -> None:
    """The preview fragment loads via htmx on first hover only."""
    out = str(linkify_refs("paper:acheson26"))
    assert 'hx-trigger="mouseenter delay:200ms once"' in out


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
    assert "max-h-72" in out
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
