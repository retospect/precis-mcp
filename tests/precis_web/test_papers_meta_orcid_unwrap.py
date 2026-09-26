"""Paper Meta tab: ORCID iD icons in the byline, Meta-by-default, and the
abstract "unwrap" button (gr351776, gr349107).

Covers the rendered markup only (template-level assertions against the
real ``_reader``/``papers/_meta_panel``/``papers/_meta_forms`` templates,
served through the FakeStore-backed app — see ``conftest.py``'s
docstring). The unwrap button's own line-joining logic is inline JS with
no server round-trip, so it isn't exercised by a browser here; the
markup + handler wiring is what a render test can check.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from tests.precis_web.conftest import make_ref


def _paper_with_authors(*, authors: list[dict[str, str]]) -> SimpleNamespace:
    """Swap paper #10 (slug ``smith2024``) for one carrying *authors* —
    same shape ``conftest.FakeStore`` seeds by default, just with the
    author list a test wants."""
    return make_ref(
        id=10,
        kind="paper",
        slug="smith2024",
        title="A paper",
        year=2024,
        pdf_sha256="abc",
        authors=authors,
        meta={"abstract": "<jats:p>We study <b>X</b> in depth.</jats:p>"},
    )


# ── ORCID icon in the byline (gr351776a) ───────────────────────────────


def test_orcid_icon_renders_for_author_with_orcid(client, runtime) -> None:
    runtime.store.papers[0] = _paper_with_authors(
        authors=[
            {
                "family": "Smith",
                "given": "Jane",
                "orcid": "0000-0002-1825-0097",
            }
        ]
    )
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert "orcid-icon" in resp.text
    assert 'href="https://orcid.org/0000-0002-1825-0097"' in resp.text


def test_orcid_icon_absent_for_author_without_orcid(client, runtime) -> None:
    # Default fixture author (Jane Smith) carries no orcid key.
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert "orcid-icon" not in resp.text
    assert "orcid.org/" not in resp.text


def test_orcid_icon_only_on_the_author_that_has_one(client, runtime) -> None:
    """A byline with a mix of ORCID / no-ORCID authors gets exactly one
    icon — no placeholder for the author lacking an iD."""
    runtime.store.papers[0] = _paper_with_authors(
        authors=[
            {"family": "Smith", "given": "Jane", "orcid": "0000-0002-1825-0097"},
            {"family": "Doe", "given": "Alex"},
        ]
    )
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert resp.text.count("orcid-icon") == 1


# ── Meta tab is the default (gr351776b) ─────────────────────────────────


def test_meta_tab_is_the_default_on_open(client) -> None:
    """No ``?tab=`` query — the reader shell is handed ``initial_tab``
    'Meta', not 'Navigate' (the old default)."""
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert "'Meta', false)" in resp.text


def test_explicit_tab_query_still_wins_over_the_default(client) -> None:
    resp = client.get("/papers/smith2024?tab=Navigate")
    assert resp.status_code == 200
    assert "'Navigate', false)" in resp.text


# ── Abstract "unwrap" button (gr349107) ────────────────────────────────


def test_unwrap_button_markup_present_on_meta_tab(client) -> None:
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert "unwrap-abstract-btn" in resp.text
    assert "__paperUnwrapAbstract(this)" in resp.text
    # The handler itself ships once per page (guarded singleton), wired to
    # the abstract textarea via its `name="abstract"` selector — not a new
    # dependency, just inline JS.
    assert "window.__paperUnwrapAbstract" in resp.text
    assert "querySelector('[name=\"abstract\"]')" in resp.text
