"""Regression tests pinning the MCP-critic 2026-05-02 file-kinds fixes.

One test file covers every finding from the audit in
``grimoire/agents/mcp-critic.md`` so a bisect lands on the exact
fix commit. Each test names the finding severity and adds a short
note so an agent re-reading the file can skip to the relevant
contract.

The tests exercise :class:`MarkdownHandler` — since ``markdown``,
``plaintext`` and ``tex`` now share a single code path through
:class:`PlaintextHandler`, any fix that passes the markdown suite
applies to the other two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.markdown import MarkdownHandler
from precis.handlers.plaintext import PlaintextHandler
from precis.utils.file_id import (
    canonicalize_path_id,
    format_write_result,
    nearest_slugs,
    parse_line_range,
)
from precis.utils.search_header import detect_score_cliff

# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def md_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture
def handler(hub: Hub, md_root: Path) -> MarkdownHandler:
    return MarkdownHandler(hub=hub, root=md_root)


@pytest.fixture
def pt_handler(hub: Hub, md_root: Path) -> PlaintextHandler:
    return PlaintextHandler(hub=hub, root=md_root)


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── CRITICAL-C: hint-loop triangle ─────────────────────────────────


def test_hint_loop_put_create_suggests_reachable_calls(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """MCP critic CRITICAL-C: the ``file already exists`` hint must
    not bounce agents around an unrecoverable triangle.

    Previously the hint suggested
    ``edit(..., mode='replace', text=...)`` without a selector, which
    ``edit`` rejected with a hint back to ``put(mode='replace')``,
    which this handler also rejects — infinite loop. The new hint
    lands on calls that actually run (``get(/toc)`` + selector-
    bearing ``edit(mode='replace')`` OR ``edit(mode='find-replace')``).
    """
    _write(md_root, "exists.md", "# Hello\n\nFirst.\n")
    with pytest.raises(BadInput) as excinfo:
        handler.put(id="exists", text="duplicate", mode="create")
    hint = excinfo.value.next or ""
    # The hint must mention a concrete recoverable call, not the
    # old selector-less edit(mode='replace') cul-de-sac.
    assert "mode='replace'" not in hint or "~<block>" in hint
    # Should point at /toc for block discovery (canonical escape).
    assert "/toc" in hint
    # Should offer the find-replace splice as an alternative.
    assert "find-replace" in hint


def test_hint_loop_replace_without_selector_suggests_reachable_call(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """The ``replace requires a block selector`` error's hint must
    target ``edit(kind=..., id='<slug>~<block>', mode='replace', ...)``
    — the same verb, with the selector added — rather than
    round-tripping through ``put(mode='replace')`` which this kind
    rejects.
    """
    _write(md_root, "doc.md", "# Title\n\nBody.\n")
    handler.get(id="doc")  # ensure ingested
    with pytest.raises(BadInput) as excinfo:
        handler.edit(id="doc", mode="replace", text="nope")
    hint = excinfo.value.next or ""
    # Hint must send the caller to edit(...) — not to put(...).
    assert "edit(kind='markdown'" in hint
    assert "put(kind='markdown'" not in hint
    # Must clearly require the selector.
    assert "~" in hint


# ── CRITICAL-C: path-shaped id silently collapses ──────────────────


def test_create_with_path_form_id_canonicalises_to_slug(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """MCP critic CRITICAL-C: ``id='notes/meeting.md'`` used to
    collapse the path at the first ``/`` (``notes/meeting.md`` →
    slug ``notes``, view dropped, file created at ``notes.md``).
    The documented path form must now canonicalise to
    ``notes--meeting`` and create ``notes/meeting.md`` on disk.
    """
    handler.put(
        id="notes/meeting.md",
        text="# Meeting\n\nAgenda item.\n",
        mode="create",
    )
    # File exists at the documented location — not at
    # ``PRECIS_ROOT/notes.md``.
    assert (md_root / "notes" / "meeting.md").exists()
    assert not (md_root / "notes.md").exists()
    # Ref is addressable by both forms.
    resp_slug = handler.get(id="notes--meeting")
    assert "Meeting" in resp_slug.body
    resp_path = handler.get(id="notes/meeting.md")
    assert "Meeting" in resp_path.body


def test_canonicalize_path_id_unit() -> None:
    """Pin the canonicaliser's contract — a ``/``-containing id with
    a known extension is reshaped to slug-form; slug-form and
    extensionless paths pass through."""
    exts = (".md", ".markdown")
    # Path form → slug form.
    assert canonicalize_path_id("notes/meeting.md", extensions=exts) == "notes--meeting"
    # Path form with selector is preserved.
    assert (
        canonicalize_path_id("notes/meeting.md~conclusion", extensions=exts)
        == "notes--meeting~conclusion"
    )
    # Path form with view is preserved.
    assert (
        canonicalize_path_id("notes/meeting.md/toc", extensions=exts)
        == "notes--meeting/toc"
    )
    # Slug form is unchanged.
    assert canonicalize_path_id("notes--meeting", extensions=exts) == "notes--meeting"
    # Unknown extension is not canonicalised (don't collide with
    # another kind's file grammar).
    assert (
        canonicalize_path_id("notes/meeting.txt", extensions=exts)
        == "notes/meeting.txt"
    )


# ── MAJOR-C: Track-A line-range addressing ─────────────────────────


def test_track_a_line_range_resolves_to_block(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """MCP critic MAJOR-C: ``~L<n>-<m>`` used to be parsed as a
    literal block slug and always returned NotFound. It now
    resolves to every block intersecting the range."""
    _write(
        md_root,
        "file.md",
        "# Title\n\nFirst paragraph is here.\n\nSecond paragraph is here.\n",
    )
    handler.get(id="file")
    resp = handler.get(id="file~L3-3")
    # Line 3 = first paragraph body (H1 on L1, blank L2, body L3).
    assert "First paragraph" in resp.body
    # Header names block + line span so follow-up calls have both
    # Track-A and Track-B coordinates.
    assert "(block " in resp.body
    assert "L3" in resp.body


def test_track_a_single_line_resolves(handler: MarkdownHandler, md_root: Path) -> None:
    """``~L<n>`` (single-line form) resolves to the block containing
    that line."""
    _write(md_root, "note.md", "# Top\n\nbody.\n")
    handler.get(id="note")
    resp = handler.get(id="note~L1")
    # Heading block is L1.
    assert "Top" in resp.body


def test_track_a_out_of_range_errors_with_hint(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """A line range with no intersecting block raises NotFound (not
    a silent empty response)."""
    _write(md_root, "short.md", "# X\n\nbody.\n")
    handler.get(id="short")
    with pytest.raises(NotFound, match="no block intersects"):
        handler.get(id="short~L99-105")


def test_parse_line_range_unit() -> None:
    """Helper unit test — pins the ``L<a>-<b>`` grammar."""
    sel = parse_line_range("L10-20", raw_id="file~L10-20")
    assert sel is not None
    assert sel.line_start == 10
    assert sel.line_end == 20
    # Single-line form: end == start.
    sel = parse_line_range("L7", raw_id="file~L7")
    assert sel is not None
    assert sel.line_start == 7 and sel.line_end == 7
    # Non-line-range returns None (fall-through to slug/pos lookup).
    assert parse_line_range("conclusion", raw_id="x~conclusion") is None
    # Invalid range (start > end) raises.
    with pytest.raises(BadInput, match="invalid line range"):
        parse_line_range("L20-10", raw_id="x~L20-10")


# ── MAJOR-C: unified edit-mode response shape ──────────────────────


@pytest.mark.parametrize(
    "mode,kwargs",
    [
        ("append", {"text": "appended text"}),
        ("replace", {"text": "Rewritten.", "_use_selector": True}),
        (
            "find-replace",
            {"find": "Original", "text": "Renamed"},
        ),
        ("insert", {"find": "Original", "where": "before", "text": "PRE: "}),
    ],
)
def test_edit_response_shape_uniform(
    handler: MarkdownHandler,
    md_root: Path,
    mode: str,
    kwargs: dict,
) -> None:
    """MCP critic MAJOR-C: every edit-mode response must carry
    ``<verb> block N '<slug>' (L<a>-<b>) in '<file-slug>'``.

    Previously ``append`` and ``replace`` returned no line info;
    ``find-replace`` and ``insert`` returned no block slug. Agents
    had to re-fetch ``/toc`` to learn where their write landed.
    """
    _write(
        md_root,
        "doc.md",
        "# Header\n\n## First\n\nOriginal body.\n",
    )
    handler.get(id="doc")
    if kwargs.pop("_use_selector", False):
        call_id = "doc~first"
    else:
        call_id = "doc"
    resp = handler.edit(id=call_id, mode=mode, **kwargs)
    # Same regex across every mode — this is the point.
    import re

    pattern = (
        r"(appended|replaced|edited|inserted) block \d+ '[^']+' "
        r"\(L\d+(?:-\d+)?\) in 'doc'"
    )
    assert re.search(pattern, resp.body), (
        f"edit(mode={mode!r}) response {resp.body!r} does not match "
        f"unified shape {pattern!r}"
    )


def test_format_write_result_unit() -> None:
    """Pin the formatter's shape directly so a downstream edit can't
    drift the response without failing this test."""
    out = format_write_result(
        verb="replaced",
        file_slug="notes--meeting",
        block_pos=3,
        block_slug="conclusion",
        line_start=42,
        line_end=58,
    )
    assert "replaced" in out
    assert "block 3" in out
    assert "'conclusion'" in out
    assert "(L42-58)" in out
    assert "'notes--meeting'" in out
    # Multi-span suffix.
    multi = format_write_result(
        verb="edited",
        file_slug="x",
        block_pos=0,
        block_slug="a",
        line_start=1,
        line_end=1,
        span_count=3,
    )
    assert "[3 spans]" in multi


# ── MAJOR-C: NotFound options= nearest-match hints ─────────────────


def test_slug_miss_returns_difflib_options(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """MCP critic MAJOR-C: a NotFound on block slug miss must list
    nearest matches in ``options=`` so a one-character typo doesn't
    force a /toc round-trip."""
    _write(
        md_root,
        "doc.md",
        "# Title\n\n## Section one\n\nA.\n\n"
        "## Section two\n\nB.\n\n## Section three\n\nC.\n",
    )
    handler.get(id="doc")
    with pytest.raises(NotFound) as excinfo:
        # One-character typo — the existing slug is 'section-three'.
        handler.get(id="doc~section-thre")
    opts = excinfo.value.options or []
    assert opts, "NotFound should carry options= on slug miss"
    assert "section-three" in opts


def test_nearest_slugs_unit() -> None:
    """Unit-level pin so the helper can't silently change behaviour."""
    matches = nearest_slugs(
        "section-thre",
        ["section-one", "section-two", "section-three", "intro"],
    )
    assert "section-three" in matches


# ── MAJOR-C: NotFound includes prefix shorthand matches ────────────


def test_prefix_shorthand_unique_resolves(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """MCP critic MINOR-C: a caller who types the clean prefix of a
    hash-suffixed slug gets auto-resolved to the full slug, saving a
    /toc round-trip."""
    _write(md_root, "doc.md", "# X\n\n## First section\n\nBody.\n")
    handler.get(id="doc")
    # Find the hash-suffixed paragraph slug (something like
    # 'body-<hash>') and try addressing by the clean prefix.
    resp_toc = handler.get(id="doc/toc")
    _ = resp_toc.body  # force render so ingest completes

    # Now probe a prefix shorthand. The paragraph slug for "Body."
    # contains a content hash; the bare-text prefix is "body".
    # Because there's exactly one such slug, prefix lookup resolves.
    resp = handler.get(id="doc~body")
    assert "Body." in resp.body


def test_prefix_shorthand_ambiguous_options(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """When the prefix matches more than one slug, the error lists
    every candidate via ``options=`` rather than silently picking
    one (MCP critic MINOR-C 2026-05-02)."""
    _write(
        md_root,
        "doc.md",
        # Two paragraphs starting with the same words → same prefix
        # but distinct hash tails.
        "# X\n\nthe fox jumps over the fence.\n\nthe fox jumps over the moon.\n",
    )
    handler.get(id="doc")
    with pytest.raises(NotFound) as excinfo:
        handler.get(id="doc~the")
    opts = excinfo.value.options or []
    # More than one candidate — every slug matching the prefix.
    assert len(opts) > 1
    # Each option starts with the query prefix (that's the whole
    # point of prefix-shorthand disambiguation).
    assert all(opt.startswith("the-") for opt in opts)


# ── MINOR-C: file-level delete requires confirm= ───────────────────


def test_file_level_delete_requires_confirm_string(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """MCP critic MINOR-C: whole-file delete must be reachable from
    the API to match whole-file create, but gated behind an explicit
    confirm string so a typo can't nuke the wrong file.
    """
    _write(md_root, "tmp.md", "# throwaway\n")
    handler.get(id="tmp")
    # Without confirm — BadInput that names the exact confirm
    # string.
    with pytest.raises(BadInput) as excinfo:
        handler.delete(id="tmp")
    assert "confirm='delete-file-tmp'" in str(excinfo.value)
    # With the right confirm string — succeeds.
    resp = handler.delete(id="tmp", confirm="delete-file-tmp")
    assert "deleted file" in resp.body.lower()
    assert not (md_root / "tmp.md").exists()


def test_file_level_delete_rejects_wrong_confirm(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """A ``confirm='True'`` or a mis-spelled slug in the confirm
    string is rejected — the string must name the exact slug
    being deleted."""
    _write(md_root, "keep.md", "# safe\n")
    handler.get(id="keep")
    # ``confirm=True`` alone is insufficient — the confirm string
    # must encode the slug.
    with pytest.raises(BadInput):
        handler.delete(id="keep", confirm="True")
    with pytest.raises(BadInput):
        handler.delete(id="keep", confirm="delete-file-other")
    # File still exists — the guard worked.
    assert (md_root / "keep.md").exists()


# ── NIT: tag/link response pluralisation ───────────────────────────


def test_tag_response_pluralises(handler: MarkdownHandler, md_root: Path) -> None:
    """MCP critic NIT: ``+2 tag`` used to slip through as a typo —
    now ``+2 tags`` with a trailing s when count > 1."""
    _write(md_root, "tags.md", "# X\n\nA.\n")
    handler.get(id="tags")
    resp_two = handler.tag(id="tags", add=["draft", "topic-probe"])
    assert "+2 tags" in resp_two.body
    # Single tag stays singular.
    resp_one = handler.tag(id="tags", add=["pinned-by-test"])
    assert "+1 tag" in resp_one.body
    assert "+1 tags" not in resp_one.body


# ── MINOR-$: search confidence-cliff marker ────────────────────────


def test_detect_score_cliff_unit() -> None:
    """Unit: the cliff detector reports the strong-count only when
    a real cliff exists."""
    # Clear cliff: top hit dwarfs the tail.
    assert detect_score_cliff([1.0, 0.2, 0.18, 0.15]) == 1
    # No cliff: all scores similar.
    assert detect_score_cliff([0.9, 0.85, 0.8, 0.7]) is None
    # Single hit: nothing to report.
    assert detect_score_cliff([0.99]) is None
    # Empty input.
    assert detect_score_cliff([]) is None


# ── Path-form / slug-form symmetry ─────────────────────────────────


def test_path_form_and_slug_form_address_same_file(
    handler: MarkdownHandler, md_root: Path
) -> None:
    """The docs promise both forms resolve the same file. Pin it
    explicitly for every verb that takes an ``id=`` (MCP critic
    CRITICAL-C 2026-05-02)."""
    handler.put(id="notes/x.md", text="# notesx\n\nbody.\n", mode="create")
    r1 = handler.get(id="notes/x.md")
    r2 = handler.get(id="notes--x")
    assert r1.body == r2.body


# ── plaintext line-range regression (inherited via same code path) ──


def test_plaintext_track_a_line_range(
    pt_handler: PlaintextHandler, md_root: Path
) -> None:
    """Since MarkdownHandler now inherits from PlaintextHandler, a
    plaintext regression protects the shared parser against regressions
    that would only show up in the markdown path (MCP critic
    MAJOR-C 2026-05-02)."""
    _write(
        md_root,
        "log.txt",
        "first paragraph.\n\nsecond paragraph spans\nmultiple lines.\n\nthird.\n",
    )
    pt_handler.get(id="log")
    resp = pt_handler.get(id="log~L3-4")
    # Lines 3-4 intersect the second paragraph.
    assert "second paragraph" in resp.body
    assert "(block " in resp.body
