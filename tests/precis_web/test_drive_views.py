"""Drive's sidebar "Saved views" (the former top-nav Browse presets)."""

from __future__ import annotations

import re

from precis_web.drive_views import active_view


def _sidebar(html: str) -> str:
    m = re.search(r"data-drive-views>(.*?)</ul>", html, re.S)
    assert m, "Saved views block missing"
    return m.group(1)


def test_active_view_matches_request(client) -> None:
    html = client.get("/drive?k=cad&folder=*&sort=recency").text
    side = _sidebar(html)
    active = re.findall(r'<a href="([^"]*)"[^>]*aria-current="page"', side)
    assert len(active) == 1 and "k=cad" in active[0]
    assert "aria-current" not in _sidebar(client.get("/drive").text)
    assert "k=draft" in _sidebar(client.get("/drive?k=draft").text)


def test_active_view_helper() -> None:
    assert active_view(["todo"], ["level:recurring"]) == "schedules"
    assert active_view(["todo"], []) == ""
    assert active_view(["paper", "draft"], []) == ""
    # Resolved-filter matching (review td703478): extra qualifier tags or a
    # different resolved kind must not light a preset.
    assert active_view(["paper"], []) == "papers"
    assert active_view(["cad"], ["PRIO:high"]) == ""


def test_active_view_tracks_resolved_filters(client) -> None:
    """The highlight matches the effective filters, not the raw URL params:
    a ``/kind`` token in ``q`` overrides ``k=``, and a ``tag:`` qualifier in
    ``q`` filters the view further — either way ``k=cad`` alone in the URL
    must not light CAD (review td703478)."""
    html = client.get("/drive?k=cad&q=/paper").text
    active = re.findall(r'<a href="([^"]*)"[^>]*aria-current="page"', _sidebar(html))
    assert len(active) == 1 and "k=paper" in active[0]
    assert "aria-current" not in _sidebar(
        client.get("/drive?k=cad&q=tag:PRIO:high").text
    )


def test_schedules_view_active(client) -> None:
    """The Schedules preset's ``tag=level:recurring`` sentinel is translated
    to ``has_schedule`` for the query and restored for paging — the highlight
    must still fire."""
    html = client.get("/drive?submitted=1&k=todo&tag=level:recurring&sort=recency").text
    active = re.findall(r'<a href="([^"]*)"[^>]*aria-current="page"', _sidebar(html))
    assert len(active) == 1 and "tag=level:recurring" in active[0]


def test_rxn_nav_link_and_sidebar_links(client) -> None:
    """Reaction ledger stays a top-level nav link (gr664411 ruling — not
    folded into Drive's New form); Nanopubs links its workbench."""
    html = client.get("/drive").text
    # The /rxn anchor renders in the header nav, before Drive's New form.
    assert html.index('href="/rxn"') < html.index('data-tour="drive-new"')
    assert 'href="/nanopub"' in _sidebar(html)
