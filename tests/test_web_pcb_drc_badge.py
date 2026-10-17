"""The pcb detail page's DRC badge takes both shapes ``argue.vitals``
yields: a {rule: count} mapping or a string status ("stale, re-check").
A str used to hit ``drc.items()`` and 500 the page."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis_web.deps import templates


def _render(drc: object) -> str:
    tpl = templates.env.get_template("pcb/_drc_badge.html.j2")
    return " ".join(tpl.render(drc=drc).split())


def test_string_status_renders_as_text() -> None:
    out = _render("stale, re-check")
    assert "DRC: stale, re-check" in out
    assert "no recorded errors" not in out


def test_string_status_is_escaped() -> None:
    out = _render("<b>x</b>")
    assert "&lt;b&gt;" in out
    assert "<b>" not in out


def test_mapping_renders_counts() -> None:
    out = _render({"CLEARANCE": 3, "X_RC": 1})
    assert "CLEARANCE 3 · X_RC 1" in out
    assert "no recorded errors" not in out


@pytest.mark.parametrize("empty", [{}, None])
def test_empty_is_clean(empty: object) -> None:
    assert "DRC: no recorded errors" in _render(empty)


def test_detail_template_includes_badge() -> None:
    filename = templates.env.get_template("pcb/detail.html.j2").filename
    assert filename is not None
    src = Path(filename).read_text(encoding="utf-8")
    assert "_drc_badge.html.j2" in src
    assert "drc.items()" not in src
