"""Batch W / S2: shortcuts.js is loaded and the nav chords are unique."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src/precis_web"
BASE = (ROOT / "templates/base.html.j2").read_text(encoding="utf-8")

# Chord -> where it goes (rulings: drafts and papers live in Drive, so g r / g p).
CHORDS = {
    "g d": "/drive",
    "g r": "k=draft",
    "g p": "k=paper",
    "g t": "/todo",
    "g g": "/gripes",
    "g s": "/status",
    "g a": "/alerts",
    "g n": "/needs-you",
    "g m": "/manual",
}


def test_base_loads_shortcuts_js_beside_tour():
    assert '<script src="/static/shortcuts.js" defer></script>' in BASE
    assert '<script src="/static/tour.js" defer></script>' in BASE
    assert (ROOT / "static/shortcuts.js").is_file()


def test_nav_chords_are_unique_and_point_where_ruled(client):
    html = client.get("/drive").text
    anchors = re.findall(r'<a href="([^"]*)"[^>]*data-shortcut="(g [a-z])"', html)
    chords = [c for _, c in anchors]
    assert len(chords) == len(set(chords)), chords
    assert set(chords) == set(CHORDS), chords
    for href, chord in anchors:
        assert CHORDS[chord] in href, (chord, href)


def test_search_slash_is_bound_on_header_and_drive_page(client):
    html = client.get("/drive").text
    assert html.count('data-shortcut="/"') == 2  # header box + Drive's own box


# ── S3: list cursor and detail-page keys ──────────────────────────────

TEMPLATES = ROOT / "templates"


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_list_templates_emit_scope_and_rows():
    for name in (
        "drive/index.html.j2",
        "todo/dashboard.html.j2",
        "gripes/list.html.j2",
        "agentlogs/list.html.j2",
    ):
        src = _tpl(name)
        assert 'data-shortcut-scope="list"' in src, name
        assert "data-row" in src, name


def test_todo_page_renders_list_scope(client):
    assert 'data-shortcut-scope="list"' in client.get("/todo").text


def test_detail_templates_bind_e_and_back_binds_u():
    for name in (
        "gripes/detail.html.j2",
        "refs/detail.html.j2",
        "smartdraft/view.html.j2",
    ):
        assert 'data-shortcut="e"' in _tpl(name), name
    assert 'data-shortcut="u"' in _tpl("_drive_back.html.j2")


def test_graph_page_no_longer_owns_a_key_handler():
    src = _tpl("graph/focus.html.j2")
    assert "keydown.window" not in src
    assert "key(ev)" not in src
    js = (ROOT / "static/shortcuts.js").read_text(encoding="utf-8")
    assert "data-ring" in js or "a[data-ring]" in js
