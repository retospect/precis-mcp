"""gr548801 — perplexity ``[N]`` citations become linked ``web`` refs.

A fetched or imported body's ``Sources:`` list mints one born-stale
``web`` cache stub per cited URL — no network at mint — and the render
rewrites each ``[N]`` marker to ``[web:<ref_id>]``. The stub fetches on
access through the SSRF-guarded path, preserving the ref id, and
``precis jobs backfill-perplexity-cites`` materialises the same refs
eagerly for rows cached before the feature landed.
"""

from __future__ import annotations

import argparse
import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers._cache_base import FetchResult
from precis.handlers.perplexity import ResearchHandler
from precis.handlers.web import WebHandler
from precis.store import Store
from precis.store.types import ChunkInsert
from precis.utils.url import canonical_url
from precis.utils.web_refs import (
    parse_numbered_sources,
    rewrite_citation_markers,
)

# ── stub httpx.Client (same approach as tests/test_perplexity.py) ──────


class _StubResp:
    def __init__(
        self,
        *,
        json_data: dict[str, Any] | None = None,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._json_data = json_data or {}
        self.status_code = status_code
        self.text = text or "stub-text"
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._json_data


class _StubClient:
    """Drop-in for ``httpx.Client``; ``get`` must never fire at mint."""

    last_payload: dict[str, Any] | None = None
    response: _StubResp | None = None
    call_count: int = 0

    def __init__(self, **_kw: Any) -> None:
        pass

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *_e: Any) -> None:
        pass

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _StubResp:
        type(self).last_payload = json
        type(self).call_count += 1
        resp = type(self).response
        assert resp is not None
        return resp

    def get(self, *_a: Any, **_kw: Any) -> None:
        raise AssertionError(
            "citation-ref minting must not fetch — stubs are born-stale"
        )


_RESEARCH_RESPONSE = {
    "choices": [
        {
            "message": {
                "content": (
                    "Alpha settled it [1]. Beta concurred [2]. Gamma confirmed [7]."
                )
            }
        }
    ],
    "search_results": [
        {"id": 1, "url": "https://alpha.example/report?utm_source=x"},
        {"id": 2, "url": "https://beta.example/beta-page"},
        # Non-sequential marker id — the id-spaced numbering a
        # deep-research report produces (gr542060).
        {"id": 7, "url": "https://gamma.example/"},
    ],
}


@pytest.fixture(autouse=True)
def _patch_httpx_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setenv("PERPLEXITY_API_KEY", "TEST-KEY")
    _StubClient.last_payload = None
    _StubClient.response = _StubResp(json_data=_RESEARCH_RESPONSE)
    _StubClient.call_count = 0
    monkeypatch.setattr(httpx, "Client", _StubClient)
    from precis.utils.http import reset_circuit

    reset_circuit()
    monkeypatch.setattr("precis.utils.http._default_sleep", lambda _s: None)


@pytest.fixture
def research(hub_no_embedder: Hub) -> ResearchHandler:
    return ResearchHandler(hub=hub_no_embedder)


@pytest.fixture
def web(hub_no_embedder: Hub) -> WebHandler:
    return WebHandler(hub=hub_no_embedder)


def _research_embedded(store: Store) -> ResearchHandler:
    return ResearchHandler(
        hub=Hub(store=store, embedder=MockEmbedder(dim=store.embedding_dim()))
    )


def _web_entry(store: Store, url: str) -> Any:
    return store.get_cache_entry(
        provider="web",
        request_hash=WebHandler._hash(canonical_url(url)),
    )


# ── parse + rewrite units ─────────────────────────────────────────────


def test_parse_numbered_sources_blank_separated() -> None:
    body = (
        "prose [1]\n\nSources:\n[1] https://a.example/\n\n"
        "[2] https://b.example/\n\nNext: trailer"
    )
    assert parse_numbered_sources(body) == [
        (1, "https://a.example/"),
        (2, "https://b.example/"),
    ]


def test_parse_numbered_sources_ignores_prose_list() -> None:
    """A ``Sources:`` mid-body followed by real prose yields just its rows."""
    body = "Sources:\n[3] https://c.example/\n\nmore prose [9]"
    assert parse_numbered_sources(body) == [(3, "https://c.example/")]


def test_rewrite_leaves_unmapped_and_markdown_alone() -> None:
    text = "see [1] and [9] and [2](https://x) and [[3]] and [4]:"
    out = rewrite_citation_markers(text, {1: 11, 9: 12})
    assert "[web:11]" in out
    assert "[web:12]" in out
    assert "[2](https://x)" in out
    assert "[[3]]" in out
    assert "[4]:" in out


# ── render: markers become web ref handles ────────────────────────────


def test_citations_mint_web_refs_and_render_links(
    research: ResearchHandler, store: Store
) -> None:
    resp = research.get(id="who settled the thing")
    # Prose markers AND Sources rows are rewritten.
    assert "settled it [web:" in resp.body
    assert "concurred [web:" in resp.body
    assert "confirmed [web:" in resp.body
    assert re.search(r"\[web:\d+\] https://beta\.example/beta-page", resp.body)
    # Three cited URLs → three web refs (the utm param canonicalises away).
    web_refs = store.list_refs(kind="web")
    assert len(web_refs) == 3
    # Only the Sonar call hit the network — minting stubs never fetches.
    assert _StubClient.call_count == 1
    # The rewritten handle is the ref's real id, citable as web:<id>.
    for ref in web_refs:
        assert f"[web:{ref.id}]" in resp.body
    # Idempotent: a second render reuses the rows, mints nothing.
    resp2 = research.get(id="who settled the thing")
    assert [r.id for r in store.list_refs(kind="web")] == [r.id for r in web_refs]
    assert resp2.body.count("[web:") == resp.body.count("[web:")


def test_citation_map_meta_stamps_the_marker_numbering(
    research: ResearchHandler, store: Store
) -> None:
    research.get(id="who settled the thing")
    entry = store.list_cache_entries(kind="perplexity-research", limit=1)
    meta = entry[0][1].meta or {}
    assert meta["citation_map"]["7"] == "https://gamma.example/"
    assert meta["citation_map"]["1"] == "https://alpha.example/report?utm_source=x"


def test_no_sources_drops_url_list_but_still_links_markers(
    research: ResearchHandler,
) -> None:
    resp = research.get(id="who settled the thing", no_sources=True)
    assert "Sources:" not in resp.body
    assert "beta.example/beta-page" not in resp.body
    assert "settled it [web:" in resp.body


def test_imported_report_links_markers_without_citation_meta(
    store: Store,
) -> None:
    """``put(mode='import')`` rows carry no ``citation_map`` — the render
    falls back to parsing the stored ``Sources:`` block."""
    h = _research_embedded(store)
    h.put(
        id="an imported deep research report",
        text=(
            "finding one [1] and two [2].\n\n"
            "Sources:\n"
            "[1] https://one.example/alpha\n"
            "[2] https://two.example/beta"
        ),
        mode="import",
    )
    resp = h.get(id="an imported deep research report")
    assert resp.body.count("[web:") == 4  # 2 markers + 2 Sources rows
    assert len(store.list_refs(kind="web")) == 2


# ── stub behaviour: born-stale, fetch-on-access ────────────────────────


def test_stub_is_born_stale_and_resolves_by_ref_id(
    research: ResearchHandler, web: WebHandler, store: Store
) -> None:
    research.get(id="who settled the thing")
    entry = _web_entry(store, "https://alpha.example/report")
    assert entry is not None
    stub_ref, stub_cache = entry
    assert not web._is_fresh(stub_cache)
    assert (stub_cache.meta or {}).get("stub") is True
    # The stub body explains the fetch-on-access path.
    stub_text = store.chunks.list_chunks_for_ref(stub_ref.id)[0].text
    assert "not fetched yet" in stub_text
    # ``web:<id>`` resolves by numeric ref id — the form the rewritten
    # marker emits — serving the stub rather than erroring.
    resp = web.get(id=str(stub_ref.id))
    assert "not fetched yet" in resp.body
    resp = web.get(id=stub_ref.id)
    assert "not fetched yet" in resp.body
    # And the minted URL itself fetches rather than serving the stub.
    assert _StubClient.call_count == 1


def test_stub_fetch_on_access_preserves_ref_id(
    research: ResearchHandler,
    web: WebHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    research.get(id="who settled the thing")
    entry = _web_entry(store, "https://alpha.example/report")
    assert entry is not None
    stub_ref, _ = entry

    def fake_fetch(self: WebHandler, key: str) -> FetchResult:
        assert key == "https://alpha.example/report"
        return FetchResult(
            title="Alpha Report",
            body_blocks=[ChunkInsert(ord=0, text="the real alpha page")],
            meta={"url": key},
        )

    monkeypatch.setattr(WebHandler, "_fetch", fake_fetch)
    # A different spelling of the same URL lands on the stub row.
    resp = web.get(id="https://alpha.example/report?utm_source=x")
    assert "the real alpha page" in resp.body
    after = _web_entry(store, "https://alpha.example/report")
    assert after is not None
    assert after[0].id == stub_ref.id
    assert (after[1].meta or {}).get("stub") is None


def test_same_citation_shared_across_reports_mints_once(
    research: ResearchHandler, store: Store
) -> None:
    research.get(id="who settled the thing")
    _StubClient.response = _StubResp(
        json_data={
            "choices": [{"message": {"content": "unrelated answer [1]"}}],
            "search_results": [{"id": 1, "url": "https://alpha.example/report"}],
        }
    )
    research.get(id="a different question entirely")
    # alpha.example/report was already a web ref — still one row.
    alpha_entries = [
        e
        for e in store.list_cache_entries(kind="web", limit=50)
        if "alpha.example" in ((e[1].meta or {}).get("url") or "")
    ]
    assert len(alpha_entries) == 1


# ── backfill CLI ──────────────────────────────────────────────────────


def test_backfill_cli_mints_stubs_for_existing_rows(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    from precis.cli.perplexity import run_cites
    from tests.conftest import _active_dsn

    h = _research_embedded(store)
    h.put(
        id="old cached report",
        text="finding [1].\n\nSources:\n[1] https://cited.example/x",
        mode="import",
    )
    assert store.list_refs(kind="web") == []

    dsn = _active_dsn()
    run_cites(argparse.Namespace(kind=None, limit=None, dry_run=True, database_url=dsn))
    assert "would mint=1" in capsys.readouterr().out
    assert store.list_refs(kind="web") == []

    run_cites(
        argparse.Namespace(kind=None, limit=None, dry_run=False, database_url=dsn)
    )
    out = capsys.readouterr().out
    assert "minted=1" in out
    refs = store.list_refs(kind="web")
    assert len(refs) == 1
    # Render now links against the backfilled ref — nothing new minted.
    resp = h.get(id="old cached report")
    assert f"[web:{refs[0].id}]" in resp.body
    assert len(store.list_refs(kind="web")) == 1


def test_marker_token_parses_as_linkable_ref() -> None:
    """``web:<id>`` is the ``kind:id`` form mentions + linkify resolve."""
    from precis.utils.mentions import LINKIFY_KINDS, REF_PATTERN

    m = REF_PATTERN.search("[web:123]")
    assert m is not None
    assert m.group("kind") == "web"
    assert m.group("id") == "123"
    assert "web" in LINKIFY_KINDS
