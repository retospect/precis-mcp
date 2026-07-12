"""FigureHandler tests — DB-backed (store fixture). Requires migration 0057."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.figure import FigureHandler

_CIRCLE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<circle id="face" cx="50" cy="50" r="30" fill="green"/></svg>'
)


@pytest.fixture
def figure(store):
    return FigureHandler(hub=Hub(store=store))


def test_put_creates_and_lists(figure):
    resp = figure.put(id="mascot", title="Mascot")
    assert "created figure 'mascot'" in resp.body
    listing = figure.get()
    assert "mascot" in listing.body


def test_put_duplicate_rejected(figure):
    figure.put(id="dup")
    with pytest.raises(BadInput):
        figure.put(id="dup")


def test_get_renders_source_and_vocab(figure):
    figure.put(id="m", title="M", text=_CIRCLE, vocab="green circles are foos")
    body = figure.get(id="m").body
    assert "SVG source" in body
    assert "circle" in body
    assert "Shared vocabulary" in body
    assert "green circles are foos" in body
    assert "fn" in body  # the source node handle


def test_bare_figure_has_no_seed_prose(figure):
    # Vocab / notes are born EMPTY — the "what this doc is for" text is
    # instruction (in the prompt/skill), never stored content.
    figure.put(id="m")
    body = figure.get(id="m").body
    assert "Shared vocabulary" not in body  # no seed chunk yet
    assert "Implementation notes" not in body


def test_edit_notes(figure):
    figure.put(id="m")
    figure.edit(id="m", notes="the face is <g id='face'>")
    body = figure.get(id="m").body
    assert "Implementation notes" in body
    assert "id='face'" in body


def test_put_with_source_viewbox_wins(figure):
    figure.put(id="m", text=_CIRCLE)
    body = figure.get(id="m").body
    assert "100×100" in body  # from the source's own viewBox


def test_edit_text_updates_source(figure):
    figure.put(id="m")
    figure.edit(id="m", text=_CIRCLE)
    assert "circle" in figure.get(id="m").body


def test_edit_rejects_invalid_svg(figure):
    figure.put(id="m")
    with pytest.raises(BadInput):
        figure.edit(id="m", text="<svg><rect></svg>")


def test_edit_rejects_non_svg_root(figure):
    figure.put(id="m")
    with pytest.raises(BadInput):
        figure.edit(id="m", text="<rect/>")


def test_source_is_sanitized(figure):
    figure.put(id="m")
    figure.edit(
        id="m",
        text='<svg xmlns="http://www.w3.org/2000/svg"><script>evil()</script>'
        '<rect x="1" y="1" width="2" height="2"/></svg>',
    )
    body = figure.get(id="m").body
    assert "script" not in body.lower()
    assert "rect" in body


def test_edit_vocab(figure):
    figure.put(id="m")
    figure.edit(id="m", vocab="green circles are foos")
    assert "green circles are foos" in figure.get(id="m").body


def test_edit_viewbox(figure):
    figure.put(id="m")
    figure.edit(id="m", viewbox="0 0 512 384")
    assert "512×384" in figure.get(id="m").body


def test_edit_rejects_bad_viewbox(figure):
    figure.put(id="m")
    with pytest.raises(BadInput):
        figure.edit(id="m", viewbox="0 0 -5 10")


def test_get_node_by_handle(figure):
    figure.put(id="m", text=_CIRCLE)
    # find the fn<id> handle from the render
    body = figure.get(id="m").body
    handle = next(tok for tok in body.split() if tok.startswith("fn"))
    node = figure.get(id=handle).body
    assert "figure_node" in node
    assert "circle" in node


def test_lint_surfaces_out_of_bounds(figure):
    figure.put(id="m")
    oob = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect id="big" x="90" y="90" width="50" height="50"/></svg>'
    )
    resp = figure.edit(id="m", text=oob)
    assert "lint" in resp.body.lower()
    assert "Lints" in figure.get(id="m").body


def test_delete_retires(figure):
    figure.put(id="m")
    figure.delete(id="m")
    with pytest.raises(NotFound):
        figure.get(id="m")


def test_get_missing_raises(figure):
    with pytest.raises(NotFound):
        figure.get(id="nope")
