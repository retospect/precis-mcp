"""Sources/Cited tabs + per-row fetch (paper-viewer-nav slice 3, web half).

- ``GET /papers/{id}/refs/{sources|cited}`` — the lazily-loaded tab
  fragment: numbered bibliography (sources), held+S2 union (cited),
  ``ensure_s2_neighbors`` backfill-on-open.
- ``POST /papers/{id}/fetch-ref`` — the single-row mint/reuse-and-queue
  affordance, htmx-aware.
- The shared reader tab strip carries the two new tab labels.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

import precis_web.routes.papers as papers_routes


def _nb(
    *,
    s2_id: str | None = None,
    doi: str | None = None,
    title: str = "Some paper",
    year: int | None = 2020,
    held_ref_id: int | None = None,
) -> SimpleNamespace:
    """A minimal stand-in for ``store.types.S2Neighbor`` (only the fields
    the routes read: s2_id/doi/title/year/held_ref_id)."""
    return SimpleNamespace(
        s2_id=s2_id, doi=doi, title=title, year=year, held_ref_id=held_ref_id
    )


@pytest.fixture(autouse=True)
def _no_network_ensure_s2_neighbors(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every test in this module hits the fragment endpoint, which calls
    ``ensure_s2_neighbors`` unconditionally — monkeypatch it at the
    routes-module import site (not the defining module) so no test
    accidentally reaches Semantic Scholar, and record the ref_ids it was
    called with for the one test that asserts on it."""
    calls: list[int] = []

    def _fake(store: Any, ref_id: int, **kw: Any) -> bool:
        calls.append(ref_id)
        return False

    monkeypatch.setattr(papers_routes, "ensure_s2_neighbors", _fake)
    return calls


# ── GET /papers/{id}/refs/sources ──────────────────────────────────────


def test_sources_fragment_renders_numbered_held_and_nonheld_rows(
    client, runtime
) -> None:
    runtime.store.s2_neighbors[(10, "cites")] = [
        _nb(s2_id="S2HELD", title="A held reference", year=2019, held_ref_id=11),
        _nb(s2_id="S2EXT", doi="10.1/ext", title="An external reference", year=2021),
    ]
    resp = client.get("/papers/10/refs/sources")
    assert resp.status_code == 200
    text = resp.text
    # Numbered, bibliography order.
    assert "1." in text
    assert "2." in text
    # Held row (ref 11 = jones2025 in the fixture pool) links locally.
    assert "/papers/jones2025" in text
    assert "Another paper" in text  # ref 11's title
    # Non-held row: title shown + off-site links + a Fetch button (it
    # carries both s2_id and doi).
    assert "An external reference" in text
    assert "https://www.semanticscholar.org/paper/S2EXT" in text
    assert "https://doi.org/10.1/ext" in text
    assert "scholar.google.com" in text
    assert "Fetch" in text


def test_sources_fragment_no_fetch_button_without_identifiers(client, runtime) -> None:
    """A title-only neighbour (no doi, no s2_id) renders links-only — no
    Fetch button, since there's nothing to fetch by."""
    runtime.store.s2_neighbors[(10, "cites")] = [
        _nb(title="Untraceable reference", year=1998),
    ]
    resp = client.get("/papers/10/refs/sources")
    assert resp.status_code == 200
    assert "Untraceable reference" in resp.text
    assert "Fetch" not in resp.text
    # Still gets a title-search Scholar link even with no identifier.
    assert "scholar.google.com" in resp.text


def test_sources_fragment_empty_shows_note_not_error(client, runtime) -> None:
    resp = client.get("/papers/10/refs/sources")
    assert resp.status_code == 200
    assert "no bibliography data" in resp.text


def test_refs_fragment_calls_ensure_s2_neighbors(
    client, _no_network_ensure_s2_neighbors
) -> None:
    calls = _no_network_ensure_s2_neighbors
    client.get("/papers/10/refs/sources")
    assert calls == [10]
    client.get("/papers/10/refs/cited")
    assert calls == [10, 10]


def test_refs_fragment_unknown_paper_errors(client) -> None:
    resp = client.get("/papers/999999/refs/sources")
    assert resp.status_code == 400  # NotFound -> PrecisError handler


def test_refs_fragment_unknown_direction_errors(client) -> None:
    resp = client.get("/papers/10/refs/bogus")
    assert resp.status_code == 400


def test_refs_fragment_non_paper_docfamily_ref_errors(
    client, _no_network_ensure_s2_neighbors
) -> None:
    """Sources/Cited are paper-only — a ``pres`` (in ``_DOC_FAMILY`` for the
    shared reader's other sidebar endpoints, but with no S2 neighbour
    graph) 404s here, and never reaches the S2 backfill."""
    resp = client.get("/papers/60/refs/sources")
    assert resp.status_code == 404
    assert _no_network_ensure_s2_neighbors == []


def test_sources_fragment_held_cite_shown_when_s2_list_elided(client, runtime) -> None:
    """Some publishers elide the S2 reference list (e.g. Elsevier) — the
    Sources tab must still show held outgoing ``cites`` links, unnumbered
    (the bibliography ordinal only means anything S2-side)."""
    runtime.store.cites_out[10] = [SimpleNamespace(dst_ref_id=11)]
    resp = client.get("/papers/10/refs/sources")
    assert resp.status_code == 200
    assert "/papers/jones2025" in resp.text


def test_sources_fragment_dedupes_held_row_against_s2_list(client, runtime) -> None:
    """A held cited paper also present in the S2 references list renders
    once, as the numbered S2 row."""
    runtime.store.cites_out[10] = [SimpleNamespace(dst_ref_id=11)]
    runtime.store.s2_neighbors[(10, "cites")] = [
        _nb(s2_id="S2SAME", title="Same paper", year=2025, held_ref_id=11),
    ]
    resp = client.get("/papers/10/refs/sources")
    assert resp.status_code == 200
    assert resp.text.count("/papers/jones2025") == 1


# ── GET /papers/{id}/refs/cited ─────────────────────────────────────────


def test_cited_fragment_unions_and_dedupes_held_row(client, runtime) -> None:
    """Ref 11 (jones2025) cites this paper both as a held ``cites`` link
    AND shows up in the S2 cited_by list (same paper) — must render once,
    as held."""
    runtime.store.cites_in[10] = [SimpleNamespace(src_ref_id=11)]
    runtime.store.s2_neighbors[(10, "cited_by")] = [
        _nb(s2_id="S2SAME", title="Another paper", year=2025, held_ref_id=11),
        _nb(s2_id="S2OTHER", title="A citing paper we do not hold", year=2022),
    ]
    resp = client.get("/papers/10/refs/cited")
    assert resp.status_code == 200
    text = resp.text
    # Held row appears exactly once.
    assert text.count("/papers/jones2025") == 1
    assert "A citing paper we do not hold" in text


def test_cited_fragment_held_citer_absent_from_s2_still_shown(client, runtime) -> None:
    """A held incoming ``cites`` link with no matching S2 cited_by row
    (S2 hasn't indexed it, or the TTL hasn't refreshed) still renders —
    it's a real citation regardless of what S2 knows."""
    runtime.store.cites_in[10] = [SimpleNamespace(src_ref_id=11)]
    resp = client.get("/papers/10/refs/cited")
    assert resp.status_code == 200
    assert "/papers/jones2025" in resp.text


def test_cited_fragment_empty_shows_note(client, runtime) -> None:
    resp = client.get("/papers/10/refs/cited")
    assert resp.status_code == 200
    assert "no citations" in resp.text


# ── POST /papers/{id}/fetch-ref ─────────────────────────────────────────


def test_fetch_ref_missing_identifiers_is_client_error_not_500(client, runtime) -> None:
    resp = client.post("/papers/10/fetch-ref", data={"title": "No id here"})
    assert resp.status_code == 400
    assert runtime.calls == []  # never reached dispatch


def test_fetch_ref_unknown_ref_errors(client, runtime) -> None:
    """No owning-ref guard existed before: any ref_id could dispatch a
    mint. An unknown ref_id 404s before the ``put`` dispatch."""
    resp = client.post("/papers/999999/fetch-ref", data={"doi": "10.1/ext"})
    assert resp.status_code == 404
    assert runtime.calls == []


def test_fetch_ref_non_paper_ref_errors(client, runtime) -> None:
    """Same owning-ref guard as ``reviewed`` — a ``pres`` ref_id (in
    ``_DOC_FAMILY`` for the shared reader, but no S2 neighbour graph)
    404s before the ``put`` dispatch too."""
    resp = client.post("/papers/60/fetch-ref", data={"doi": "10.1/ext"})
    assert resp.status_code == 404
    assert runtime.calls == []


def test_fetch_ref_mints_stub_requeues_and_stamps_held(client, runtime) -> None:
    runtime.store.identifier_lookup[("doi", "10.1/ext")] = 200
    resp = client.post(
        "/papers/10/fetch-ref",
        data={"doi": "10.1/EXT", "title": "An external reference", "year": "2021"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "put"
    assert args["kind"] == "paper"
    assert args["identifier"] == "doi:10.1/ext"  # lowercased
    assert args["verify"] is False
    assert args["title"] == "An external reference"
    assert args["year"] == 2021
    # Queue-jump scoped to the one minted/reused ref, widened id_kinds.
    assert runtime.store.requeue_ref_id_calls == [[200]]
    # held_ref_id stamped on the matching s2_neighbors row(s).
    assert runtime.store.s2_neighbor_held_updates == [(10, 200, None, "10.1/ext")]


def test_fetch_ref_by_s2_id(client, runtime) -> None:
    runtime.store.identifier_lookup[("s2", "S2XYZ")] = 201
    resp = client.post(
        "/papers/10/fetch-ref", data={"s2_id": "S2XYZ"}, follow_redirects=False
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert args["identifier"] == "s2:S2XYZ"
    assert runtime.store.s2_neighbor_held_updates == [(10, 201, "S2XYZ", None)]


def test_fetch_ref_htmx_returns_row_fragment(client, runtime) -> None:
    runtime.store.identifier_lookup[("doi", "10.1/ext")] = 200
    resp = client.post(
        "/papers/10/fetch-ref",
        data={"doi": "10.1/ext", "direction": "sources", "n": "3"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    # ref 200 isn't in the fixture pool -> fetch_refs_by_ids misses it,
    # so the row renders non-held (still a well-formed swap fragment).
    assert 'class="refs-row' in resp.text


def test_fetch_ref_htmx_row_renders_held_when_stub_resolves_to_known_ref(
    client, runtime
) -> None:
    runtime.store.identifier_lookup[("doi", "10.1/ext")] = 11  # jones2025
    resp = client.post(
        "/papers/10/fetch-ref",
        data={"doi": "10.1/ext"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "/papers/jones2025" in resp.text
    assert "held" in resp.text


def test_fetch_ref_non_htmx_redirects_to_tab(client, runtime) -> None:
    runtime.store.identifier_lookup[("doi", "10.1/ext")] = 200
    resp = client.post(
        "/papers/10/fetch-ref",
        data={"doi": "10.1/ext", "direction": "cited"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers/10?tab=Cited"


def test_fetch_ref_dispatch_error_surfaces(client, runtime) -> None:
    runtime.error_verbs.add("put")
    resp = client.post("/papers/10/fetch-ref", data={"doi": "10.1/ext"})
    assert resp.status_code == 400
    assert "Fetch error" in resp.text
    assert "invalid put: rejected by handler" in resp.text
    # A rejected mint never reaches the queue-jump / held stamp.
    assert runtime.store.requeue_ref_id_calls == []
    assert runtime.store.s2_neighbor_held_updates == []


# ── reader tab strip ─────────────────────────────────────────────────


def _tab_xfor_attr(html: str) -> str | None:
    """The tab strip's ``x-for`` expression as a *browser* would parse it.

    A substring check on the raw HTML is not enough: ``tojson`` emits raw
    double quotes, so inside a double-quoted attribute the array text is
    still present in the page while the parsed attribute value is the
    truncated ``t in [`` (the bug that killed the tab strip fleet-wide).
    """
    from html.parser import HTMLParser

    found: list[str] = []

    class _P(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag != "template":
                return
            xfor = dict(attrs).get("x-for") or ""
            if xfor.startswith("t in "):
                found.append(xfor)

    _P().feed(html)
    return found[0] if found else None


def test_reader_tab_strip_contains_sources_and_cited(client) -> None:
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert _tab_xfor_attr(resp.text) == (
        't in ["Navigate", "Jump", "Sources", "Cited", "Meta"]'
    )


def test_reader_tab_strip_excludes_sources_and_cited_for_pres(client) -> None:
    """The Sources/Cited tabs are paper-only (``doc.show_refs_tabs``) —
    the pres slide-deck reader shares this reader shell but has no S2
    bibliography, so its tab strip must not carry them (the ``x-show``
    Sources/Cited panel divs stay in the DOM as inert dead code — they're
    only reachable via a ``?tab=Sources`` deep link, which the server-side
    ``refs_panel`` guard now 404s regardless of kind)."""
    resp = client.get("/pres/2001-lecture01")
    assert resp.status_code == 200
    assert _tab_xfor_attr(resp.text) == 't in ["Navigate", "Jump", "Meta"]'
