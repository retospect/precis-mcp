"""``/manual`` — the user-facing manual served from packaged markdown.

The interesting properties are structural, not cosmetic: every chapter
must be discoverable and renderable (a chapter that 500s is worse than no
chapter), the nav link must exist on every page (the manual is what you
reach for when you are lost — an unreachable one is pointless), and the
slug must not be a filesystem path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import precis_web.routes.manual as manual_mod
from precis_web.routes.manual import (
    MANUAL_DIR,
    TOUR_DIR,
    _chapters,
    _split,
    _tours,
    tour_slug_for_path,
)


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


# ── in-app guided tour (docs/backlog/user-guide-demo.md, slice 1) ──────────
#
# tour.js fetches these manifests over the network at ?tour=<slug> time —
# the interesting properties are the same shape as the chapters above: a
# shipped manifest must be discoverable and well-formed (a malformed one
# must 500 loudly rather than serve garbage tour.js can't render), and
# every anchor a manifest names must actually exist as a ``data-tour``
# attribute somewhere in the templates — otherwise the tour silently
# highlights nothing and nobody notices until a user files a gripe.

#: src/precis_web/templates — the tripwire test greps every template under
#: here for ``data-tour="<anchor>"``, so a template rename/reshuffle that
#: drops an anchor (rather than the deliberate cross-file edit this repo's
#: convention expects) fails the suite instead of shipping a dead tour step.
TEMPLATES_DIR = MANUAL_DIR.parent / "templates"

_DATA_TOUR_RE = re.compile(r'data-tour="([a-z0-9-]+)"')


def _shipped_tour_manifests() -> list[Path]:
    return sorted(TOUR_DIR.glob("*.json"))


def _template_data_tour_anchors() -> set[str]:
    anchors: set[str] = set()
    for path in TEMPLATES_DIR.rglob("*.j2"):
        anchors |= set(_DATA_TOUR_RE.findall(path.read_text(encoding="utf-8")))
    return anchors


def test_every_shipped_tour_is_discovered() -> None:
    on_disk = {p.stem.split("-", 1)[1] for p in _shipped_tour_manifests()}
    assert on_disk, "no tour manifests shipped under src/precis_web/manual/tour/"
    assert set(_tours()) == on_disk


@pytest.mark.parametrize("path", _shipped_tour_manifests(), ids=lambda p: p.name)
def test_tour_manifest_schema(path: Path) -> None:
    """Every shipped manifest matches the shape ``tour.js`` and the
    ``/manual/tour/{slug}.json`` endpoint both assume: required keys, a
    non-empty step list, and every step carrying a non-empty anchor/
    heading/text and a placement tour.js knows how to render."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    for key in ("title", "route", "steps"):
        assert key in data, f"{path.name} missing {key!r}"
    assert isinstance(data["title"], str) and data["title"].strip()
    assert isinstance(data["route"], str) and data["route"].strip()
    steps = data["steps"]
    assert isinstance(steps, list) and steps, f"{path.name} has no steps"
    for i, step in enumerate(steps):
        assert isinstance(step, dict), f"{path.name} step {i} is not an object"
        for key in ("anchor", "heading", "text", "placement"):
            assert isinstance(step.get(key), str) and step[key].strip(), (
                f"{path.name} step {i} missing/empty {key!r}"
            )
        assert step["placement"] in {"top", "bottom", "left", "right"}, (
            f"{path.name} step {i} has an unknown placement {step['placement']!r}"
        )


def test_tour_anchors_all_exist_in_some_template() -> None:
    """Anti-drift tripwire: every ``anchor`` any shipped manifest names must
    appear as ``data-tour="<anchor>"`` on some element somewhere under
    templates/ — a template refactor that drops or renames one without
    updating the manifest silently breaks that tour step (missing highlight,
    ``tour.js`` just logs a console.warn nobody's watching)."""
    template_anchors = _template_data_tour_anchors()
    for path in _shipped_tour_manifests():
        data = json.loads(path.read_text(encoding="utf-8"))
        for step in data["steps"]:
            assert step["anchor"] in template_anchors, (
                f"{path.name}: anchor {step['anchor']!r} has no matching "
                f'data-tour="{step["anchor"]}" in any template'
            )


def test_tour_manifest_endpoint_serves_a_real_slug(client) -> None:
    r = client.get("/manual/tour/drive.json")
    assert r.status_code == 200
    body = r.json()
    assert body["title"]
    assert body["route"]
    assert body["steps"]
    for step in body["steps"]:
        assert set(step) >= {"anchor", "heading", "text", "placement"}


def test_tour_manifest_endpoint_404s_for_unknown_slug(client) -> None:
    assert client.get("/manual/tour/nope.json").status_code == 404


def test_malformed_tour_manifest_fails_loudly(tmp_path, monkeypatch) -> None:
    """A manifest that fails validation must raise, not get silently
    dropped from the tour set — the endpoint turns this into a 500 with a
    log line (routes/manual.py::manual_tour), never a quiet gap."""
    bad_dir = tmp_path / "tour"
    bad_dir.mkdir()
    (bad_dir / "01-broken.json").write_text(
        json.dumps({"title": "Broken", "route": "/x", "steps": []}), encoding="utf-8"
    )
    monkeypatch.setattr(manual_mod, "TOUR_DIR", bad_dir)
    manual_mod._tours.cache_clear()
    try:
        with pytest.raises(ValueError, match="non-empty list"):
            manual_mod._tours()
    finally:
        manual_mod._tours.cache_clear()


def test_tour_for_chapter_matches_by_slug() -> None:
    """``writing-a-paper`` is the one shipped chapter/tour slug pair that
    currently coincides — the manual chapter's "take the tour" link."""
    tour = manual_mod._tour_for_chapter("writing-a-paper")
    assert tour is not None
    assert tour["title"]


def test_tour_for_chapter_none_when_no_match() -> None:
    assert manual_mod._tour_for_chapter("publishing-claims") is None


def test_base_template_loads_tour_js(client) -> None:
    r = client.get("/manual")
    assert r.status_code == 200
    assert '<script src="/static/tour.js" defer></script>' in r.text


# ── tour_slug_for_path — server-side match behind the header "?" button ────
# (nav.py::nav_badges, base.html.j2). No client fetch: the request path is
# matched against the already-cached manifests' ``route`` fields.


def test_tour_slug_for_path_literal_route() -> None:
    """``/drive`` (01-drive.json) matches the bare path, exactly."""
    assert tour_slug_for_path("/drive") == "drive"


def test_tour_slug_for_path_literal_route_does_not_match_subpath() -> None:
    """A literal route is an exact-segment match — it must not swallow a
    deeper path the way a prefix match would."""
    assert tour_slug_for_path("/drive/anything") is None


def test_tour_slug_for_path_placeholder_matches_one_segment() -> None:
    """``/smartdraft/{id}`` (02-writing-a-paper.json) matches exactly one
    non-empty segment in that position."""
    assert tour_slug_for_path("/smartdraft/dr173020") == "writing-a-paper"


def test_tour_slug_for_path_placeholder_requires_the_segment() -> None:
    """Neither zero segments (the bare route) nor two (past the
    placeholder) satisfy a ``{id}`` route."""
    assert tour_slug_for_path("/smartdraft") is None
    assert tour_slug_for_path("/smartdraft/a/b") is None


def test_tour_slug_for_path_no_match_is_none() -> None:
    assert tour_slug_for_path("/nope/not/a/route") is None


def test_tour_slug_for_path_first_match_wins(tmp_path, monkeypatch) -> None:
    """Two manifests whose routes both match a path: the earlier one by
    ``NN-slug`` filename order wins, deterministically."""
    tour_dir = tmp_path / "tour"
    tour_dir.mkdir()
    manifest = {
        "title": "T",
        "route": "/x/{id}",
        "steps": [{"anchor": "a", "heading": "H", "text": "t", "placement": "top"}],
    }
    (tour_dir / "01-first.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tour_dir / "02-second.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(manual_mod, "TOUR_DIR", tour_dir)
    manual_mod._tours.cache_clear()
    try:
        assert tour_slug_for_path("/x/anything") == "first"
    finally:
        manual_mod._tours.cache_clear()


# ── the header "?" tour-launch button (nav.py::nav_badges + base.html.j2) ──


def test_tour_button_renders_on_a_page_with_a_matching_tour(client) -> None:
    r = client.get("/drive")
    assert r.status_code == 200
    assert "data-tour-launch" in r.text
    assert 'href="/drive?tour=drive"' in r.text
    assert 'title="Take a tour of this page"' in r.text


def test_tour_button_absent_on_a_page_without_a_matching_tour(client) -> None:
    r = client.get("/manual")
    assert r.status_code == 200
    assert "data-tour-launch" not in r.text


def test_tour_button_href_keeps_other_params_drops_stale_tour_step(
    client,
) -> None:
    r = client.get("/drive", params={"q": "nanotube", "tour": "stale", "step": "3"})
    assert r.status_code == 200
    # Jinja autoescapes the attribute, so a literal `&` renders as `&amp;`.
    assert 'href="/drive?q=nanotube&amp;tour=drive"' in r.text
