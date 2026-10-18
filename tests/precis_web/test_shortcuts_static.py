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
