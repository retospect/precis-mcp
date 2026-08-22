"""``/manual`` — the user-facing manual served from packaged markdown.

The interesting properties are structural, not cosmetic: every chapter
must be discoverable and renderable (a chapter that 500s is worse than no
chapter), the nav link must exist on every page (the manual is what you
reach for when you are lost — an unreachable one is pointless), and the
slug must not be a filesystem path.
"""

from __future__ import annotations

import pytest

from precis_web.routes.manual import MANUAL_DIR, _chapters, _split


def test_every_shipped_chapter_is_discovered() -> None:
    """Every ``NN-slug.md`` in the manual dir becomes a chapter, in order."""
    on_disk = sorted(p.name for p in MANUAL_DIR.glob("*.md"))
    chapters = _chapters()
    assert len(chapters) == len(on_disk), f"undiscovered chapters in {on_disk}"
    assert [c.number for c in chapters] == sorted(c.number for c in chapters)
    # Each carries the two things the index renders.
    for c in chapters:
        assert c.title, f"{c.slug} has no H1"
        assert c.blurb, f"{c.slug} has no opening paragraph"


def test_index_lists_every_chapter(client) -> None:
    r = client.get("/manual")
    assert r.status_code == 200
    for c in _chapters():
        assert f'href="/manual/{c.slug}"' in r.text
        assert c.title in r.text


@pytest.mark.parametrize("chapter", _chapters(), ids=lambda c: c.slug)
def test_chapter_renders(client, chapter) -> None:
    """Each chapter renders to real HTML — not a traceback, not raw
    markdown. Headings are the load-bearing check: a body that arrived
    unrendered would still be 200 with the text present."""
    r = client.get(f"/manual/{chapter.slug}")
    assert r.status_code == 200
    assert "<h2>" in r.text
    assert chapter.title in r.text


def test_unknown_chapter_is_404_not_a_path_read(client) -> None:
    assert client.get("/manual/nope").status_code == 404
    # Traversal resolves against the discovered slug set, never the FS.
    assert client.get("/manual/..%2f..%2fapp.py").status_code == 404


def test_nav_carries_the_manual_link(client) -> None:
    """On an ordinary page, not just the manual's own — the link is only
    useful to someone who is somewhere else and lost. Top-level, so it
    must survive without opening a dropdown."""
    r = client.get("/drive")
    assert r.status_code == 200
    assert 'href="/manual"' in r.text


def test_split_skips_callouts() -> None:
    """A chapter opening with a blockquote callout gets an empty blurb
    rather than a stray quote fragment on the index card."""
    title, blurb, _ = _split("# T\n\n> a callout\n\nreal body\n")
    assert title == "T"
    assert blurb == ""


def test_blurb_is_plain_text() -> None:
    """Inline emphasis is stripped — the index renders the blurb as text."""
    _, blurb, _ = _split("# T\n\nA **bold** and *slanted* `bit`.\n")
    assert blurb == "A bold and slanted bit."


def test_h1_is_dropped_wherever_it_sits() -> None:
    """Title detection and H1-stripping must agree. They once didn't:
    detection scanned every line, stripping only handled an H1 on line 1,
    so a chapter with a leading blank line rendered its title twice."""
    title, _, body = _split("\n\n# T\n\nbody text\n")
    assert title == "T"
    assert "# T" not in body
    assert "body text" in body


def test_chapter_without_h1_keeps_its_whole_body() -> None:
    """No H1 to drop means nothing is dropped — a malformed chapter loses
    its title, not its first line."""
    title, _, body = _split("first line\n\nsecond line\n")
    assert title == ""
    assert "first line" in body


def test_rendered_body_has_no_duplicate_title(client) -> None:
    """The end-to-end version of the above, over the real chapters."""
    for c in _chapters():
        r = client.get(f"/manual/{c.slug}")
        assert r.text.count(f"<h1>{c.title}</h1>") == 0, c.slug
