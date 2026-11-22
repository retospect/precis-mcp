"""Handle-coded fetch caches: wikipedia / semanticscholar / chem (gr702974)
and youtube / web / websearch / perplexity-research / perplexity-reasoning
(gr702962).

Their reads persist a refs row + chunks, so they carry universal handles
(``wp`` / ``ss`` / ``ch`` / ``yt`` / ``wb`` / ``wx`` / ``rr`` / ``rz``): a
note can cite them, the handle routes by ref id (a wikipedia slug is
query-derived, so a slug round-trip would read as a fresh query), and
``view='links'`` plus the fisheye show the inbound edges.
"""

from __future__ import annotations

import ast
import re

import pytest

from precis.errors import BadInput, NotFound
from precis.handlers._cache_base import _cite_pointer
from precis.handlers._eye import eye_handle
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert, Ref
from precis.utils import handle_registry
from precis.utils.mentions import LINKIFY_KINDS, resolve_link_targets
from precis_web.routes.refs import _CACHE_BACKED_KINDS, _REFS_BROWSABLE_KINDS
from tests._refs import insert_gripe

_KINDS = {"wikipedia": "wp", "semanticscholar": "ss", "chem": "ch"}

#: gr702962 — the remaining stored fetch caches. ``ws``/``px`` were
#: deliberately avoided (they read as ws2812 / px4 in prose).
_GR702962_KINDS = {
    "youtube": "yt",
    "web": "wb",
    "websearch": "wx",
    "perplexity-research": "rr",
    "perplexity-reasoning": "rz",
}

#: provider string ``put_cache_entry`` stamps for each kind.
_PROVIDER = {
    "youtube": "youtube",
    "web": "web",
    "websearch": "perplexity",
    "perplexity-research": "perplexity",
    "perplexity-reasoning": "perplexity",
}


@pytest.mark.parametrize(("kind", "code"), sorted((_KINDS | _GR702962_KINDS).items()))
def test_kind_is_handle_coded_linkable_and_browsable(kind: str, code: str) -> None:
    assert handle_registry.format_handle(kind, 7) == f"{code}7"
    assert kind not in handle_registry.CODELESS_KINDS
    assert kind in LINKIFY_KINDS
    assert kind in _REFS_BROWSABLE_KINDS
    # The detail page must never fire a fetch for a browsable cache kind.
    assert kind in _CACHE_BACKED_KINDS


def test_cite_pointer_prints_the_handle() -> None:
    line = _cite_pointer("wikipedia", 999)
    assert line is not None
    assert "`[wp999]`" in line


def _seed_wikipedia(store: Store, *, query: str = "crispr gene editing") -> Ref:
    ref, _cache = store.put_cache_entry(
        kind="wikipedia",
        slug="crispr-gene-editing",
        title="CRISPR gene editing",
        body_blocks=[
            ChunkInsert(ord=0, text="CRISPR gene editing is a genetic method."),
            ChunkInsert(ord=1, text="It was adapted from a bacterial immune system."),
        ],
        provider="wikipedia",
        request_hash="h-" + query,
        ttl_seconds=None,
        cache_meta={"query": query},
    )
    return ref


@pytest.fixture
def no_fetch(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = runtime_with_store.hub.handler_for("wikipedia")
    assert handler is not None

    def _boom(key: str) -> None:
        raise AssertionError(f"a stored-row read fired a fetch for {key!r}")

    monkeypatch.setattr(handler, "_fetch", _boom)


@pytest.mark.usefixtures("no_fetch")
def test_handle_get_reads_the_stored_ref_without_fetching(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = _seed_wikipedia(store)
    out = runtime_with_store.dispatch("get", {"id": f"wp{ref.id}"})
    assert "bacterial immune system" in out
    assert f"cite as `[wp{ref.id}]`" in out
    # The handle's chunk selector routes by ref id too.
    out = runtime_with_store.dispatch("get", {"id": f"wp{ref.id}~1"})
    assert "bacterial immune system" in out
    assert "genetic method" not in out


@pytest.mark.usefixtures("no_fetch")
def test_bare_ref_id_reads_before_the_query_path(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = _seed_wikipedia(store)
    out = runtime_with_store.dispatch("get", {"kind": "wikipedia", "id": str(ref.id)})
    assert "genetic method" in out


@pytest.mark.usefixtures("no_fetch")
def test_links_view_shows_inbound_mentions(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = _seed_wikipedia(store)
    note = store.insert_ref(kind="memory", slug=None, title="gene editing note")
    # A note's ``[wp<id>]`` and ``wikipedia:<id>`` both resolve to this ref.
    for token in (f"[wp{ref.id}]", f"wikipedia:{ref.id}"):
        targets = resolve_link_targets(store, f"background in {token}")
        assert [t.dst_ref_id for t in targets] == [ref.id], token
    store.add_link(src_ref_id=note.id, dst_ref_id=ref.id)
    out = runtime_with_store.dispatch("get", {"id": f"wp{ref.id}", "view": "links"})
    assert f"me{note.id}" in out
    out = runtime_with_store.dispatch(
        "get", {"kind": "wikipedia", "id": ref.slug, "view": "links"}
    )
    assert f"me{note.id}" in out


def test_links_view_on_uncached_ref_never_fetches(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch(
        "get", {"kind": "chem", "id": "aspirin", "view": "links"}
    )
    assert "NotFound" in out
    assert "never fetches" in out


@pytest.mark.usefixtures("no_fetch")
def test_links_pagination_and_filters_through_dispatch(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """Catch dispatch rejecting the shared cache links continuation arguments."""
    ref = _seed_wikipedia(store)
    notes = [
        store.insert_ref(kind="memory", slug=None, title=f"source note {i}")
        for i in range(3)
    ]
    for note in notes:
        store.add_link(src_ref_id=note.id, dst_ref_id=ref.id, relation="cites")
    other = insert_gripe(store, "excluded kind")
    store.add_link(src_ref_id=other.id, dst_ref_id=ref.id, relation="cites")
    store.add_link(src_ref_id=notes[0].id, dst_ref_id=ref.id, relation="supports")

    args = {"limit": 1, "rel": "cites", "of_kind": "memory"}
    for i, note in enumerate(notes):
        # Direct dispatch spells the args= dict as ``__extras__`` (the MCP
        # wrapper is what renames the wire ``args`` key).
        out = runtime_with_store.dispatch(
            "get", {"id": f"wp{ref.id}", "view": "links", "__extras__": args}
        )
        assert f"me{note.id}" in out
        assert all(f"me{n.id}" not in out for n in notes if n.id != note.id)
        assert f"gr{other.id}" not in out
        assert "supported by" not in out
        assert f"shown {i + 1} of 3" in out
        if i < len(notes) - 1:
            continuation = re.search(r"args=(\{[^\n]+\})\)", out)
            assert continuation is not None, out
            args = ast.literal_eval(continuation.group(1))
        else:
            assert "complete" in out


# ── gr702962: youtube / web / websearch / perplexity-* ────────────────


def _seed_cache(
    store: Store,
    kind: str,
    *,
    slug: str,
    text: str,
    verdict: str = "clean",
    request_hash: str | None = None,
) -> Ref:
    ref, _cache = store.put_cache_entry(
        kind=kind,
        slug=slug,
        title=f"{kind} answer",
        body_blocks=[ChunkInsert(ord=0, slug="answer", text=text)],
        provider=_PROVIDER[kind],
        request_hash=request_hash or f"h-{slug}",
        ttl_seconds=None,
        cache_meta={"query": slug, "inject": {"verdict": verdict}},
    )
    return ref


@pytest.fixture
def no_provider_fetch(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every gr702962 handler's ``_fetch`` booby-trapped — a stored-row
    read must never reach upstream (a paid Sonar call least of all)."""

    def _boom(key: str) -> None:
        raise AssertionError(f"a stored-row read fired a fetch for {key!r}")

    for kind in _GR702962_KINDS:
        handler = runtime_with_store.hub.handler_for(kind)
        assert handler is not None, kind
        monkeypatch.setattr(handler, "_fetch", _boom)


@pytest.mark.usefixtures("no_provider_fetch")
@pytest.mark.parametrize("kind", sorted(_GR702962_KINDS))
def test_handle_read_serves_stored_ref_and_shows_inbound_links(
    kind: str, runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """Done-when: the handle read makes no upstream call, and a memory
    mention shows as an inbound row under ``view='links'`` and in the
    fisheye ring."""
    code = _GR702962_KINDS[kind]
    ref = _seed_cache(store, kind, slug=f"probe-{kind}", text="cached body text")
    receipt = runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "title": "Provider evidence",
            "text": f"Source for this evidence is {kind}:{ref.id}.",
        },
    )
    mention = re.search(r"\bme\d+\b", receipt)
    assert mention is not None, receipt
    note_handle = mention.group()

    out = runtime_with_store.dispatch("get", {"id": f"{code}{ref.id}"})
    assert "cached body text" in out
    assert f"cite as `[{code}{ref.id}]`" in out
    # The default read carries the Links section — the mention is visible.
    assert note_handle in out

    out = runtime_with_store.dispatch("get", {"id": f"{code}{ref.id}", "view": "links"})
    assert note_handle in out

    out = runtime_with_store.dispatch(
        "get", {"id": f"{code}{ref.id}", "extent": "fisheye+1hop"}
    )
    assert note_handle in out


@pytest.mark.usefixtures("no_provider_fetch")
@pytest.mark.parametrize("kind", sorted(_GR702962_KINDS))
def test_handle_token_autolinks(kind: str, store: Store) -> None:
    """``[<code><id>]`` and ``<kind>:<id>`` in a note body both resolve
    to the cached ref through the write-time autolinker."""
    code = _GR702962_KINDS[kind]
    ref = _seed_cache(store, kind, slug=f"autolink-{kind}", text="t")
    for token in (f"[{code}{ref.id}]", f"{kind}:{ref.id}"):
        targets = resolve_link_targets(store, f"background in {token}")
        assert [t.dst_ref_id for t in targets] == [ref.id], token


# ── review td707055: handle-read edge cases ──────────────────────────


@pytest.mark.usefixtures("no_provider_fetch")
@pytest.mark.parametrize("kind", sorted(_GR702962_KINDS))
def test_handle_plus_literal_is_refused_before_fetching(
    kind: str, runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """review td707055: ``get(id='<handle>', args={'literal': True})``
    rewrote the handle to bare digits, which skipped the stored-row
    guard and went upstream as a fresh (paid, for the Sonar kinds)
    query. The contradictory pair is refused instead."""
    code = _GR702962_KINDS[kind]
    ref = _seed_cache(store, kind, slug=f"lit-{kind}", text="t")
    out = runtime_with_store.dispatch(
        "get", {"id": f"{code}{ref.id}", "__extras__": {"literal": True}}
    )
    assert "[error:BadInput]" in out, out
    assert "literal" in out
    # The handle read itself still works once literal is dropped.
    out = runtime_with_store.dispatch("get", {"id": f"{code}{ref.id}"})
    assert f"{kind} answer" in out, out


@pytest.mark.usefixtures("no_provider_fetch")
def test_youtube_handle_serves_toc_and_languages(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review td707055: ``yt<N>`` routes id= as the ref id, but ``toc``
    and ``languages`` expect a video id/URL — the digits must resolve
    back to the stored row's slug before either view serves them."""
    from precis.response import Response

    ref = _seed_cache(
        store, "youtube", slug="dQw4w9WgXcQ", text="never gonna give you up"
    )
    out = runtime_with_store.dispatch("get", {"id": f"yt{ref.id}", "view": "toc"})
    assert "transcript map" in out, out
    assert "dQw4w9WgXcQ~0" in out, out

    seen: list[str] = []

    def _fake_languages(video_id: str) -> Response:
        seen.append(video_id)
        return Response(body=f"langs for {video_id}")

    monkeypatch.setattr("precis.handlers.youtube._list_languages", _fake_languages)
    out = runtime_with_store.dispatch("get", {"id": f"yt{ref.id}", "view": "languages"})
    assert seen == ["dQw4w9WgXcQ"]
    assert "langs for dQw4w9WgXcQ" in out


@pytest.mark.usefixtures("no_provider_fetch")
def test_handle_chunk_selector_reads_the_stored_chunk(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``yt<N>~<ord>`` routes the selector prefix as the ref id — the
    range read resolves it to the video-id slug (review td707055)."""
    ref = _seed_cache(
        store, "youtube", slug="dQw4w9WgXcQ", text="transcript words here"
    )
    out = runtime_with_store.dispatch("get", {"id": f"yt{ref.id}~0"})
    assert "transcript words here" in out, out


@pytest.mark.usefixtures("no_provider_fetch")
@pytest.mark.parametrize(("kind", "code"), (("web", "wb"), ("youtube", "yt")))
def test_cluster_map_advertises_the_working_plain_get(
    kind: str, code: str, runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """review td707055: web/youtube chunks mint no handle, so the
    cluster map's ``<ref>~<ord>`` rows are plain-get selectors — the
    header must name that call, not "focus a chunk handle"."""
    slug = "dQw4w9WgXcQ" if kind == "youtube" else f"slug-{kind}"
    ref = _seed_cache(store, kind, slug=slug, text=f"{kind} body text")
    out = runtime_with_store.dispatch(
        "get", {"id": f"{code}{ref.id}", "extent": "fisheye"}
    )
    call = f"get(kind={kind!r}, id='{code}{ref.id}~0')"
    assert call in out, out
    # The advertised call is the one that actually works: a plain get
    # reads the chunk; the same selector under extent= is refused.
    out = runtime_with_store.dispatch("get", {"id": f"{code}{ref.id}~0"})
    assert f"{kind} body text" in out, out
    out = runtime_with_store.dispatch(
        "get", {"id": f"{code}{ref.id}~0", "extent": "fisheye"}
    )
    assert "chunks carry no handle" in out, out


@pytest.mark.usefixtures("no_provider_fetch")
@pytest.mark.parametrize("kind", sorted(_GR702962_KINDS))
def test_high_injection_cache_eye_withholds_body_and_keeps_links(
    kind: str, runtime_with_store: PrecisRuntime, store: Store
) -> None:
    text = "Unique external body that should be withheld."
    ref = _seed_cache(store, kind, slug=f"flagged-{kind}", text=text, verdict="high")
    note = store.insert_ref(kind="memory", slug=None, title="Review note")
    store.add_link(src_ref_id=note.id, dst_ref_id=ref.id)
    handle = f"{_GR702962_KINDS[kind]}{ref.id}"

    for options in ({}, {"extent": "fisheye+1hop+recall"}):
        out = runtime_with_store.dispatch("get", {"id": handle, **options})
        assert "body withheld" in out, out
        assert text not in out
        assert f"me{note.id}" in out
    out = runtime_with_store.dispatch("get", {"id": f"{handle}~0"})
    assert "body withheld" in out, out
    assert text not in out


@pytest.mark.usefixtures("no_provider_fetch")
@pytest.mark.parametrize("kind", ["web", "websearch", "youtube"])
def test_handle_selectors_ignore_colliding_numeric_slug(
    kind: str, runtime_with_store: PrecisRuntime, store: Store
) -> None:
    slug = "dQw4w9WgXcQ" if kind == "youtube" else f"target-{kind}"
    ref = _seed_cache(store, kind, slug=slug, text="intended stored chunk")
    collision = _seed_cache(store, kind, slug=str(ref.id), text="legacy stored chunk")
    handle = f"{_GR702962_KINDS[kind]}{ref.id}"
    for address in (handle, f"{kind}:{ref.id}"):
        for selector in ("0", "0..0", "answer"):
            out = runtime_with_store.dispatch(
                "get",
                {"id": f"{address}~{selector}", "__extras__": {"tags": ["reviewed"]}},
            )
            assert "intended stored chunk" in out, out
            assert "legacy stored chunk" not in out
    out = runtime_with_store.dispatch("get", {"kind": kind, "id": f"{ref.id}~0"})
    assert "legacy stored chunk" in out, out
    assert "reviewed" in {str(tag) for tag in store.tags_for(ref.id)}
    assert "reviewed" not in {str(tag) for tag in store.tags_for(collision.id)}


def test_cache_eye_rejects_non_decimal_digits(store: Store) -> None:
    ref = _seed_cache(store, "websearch", slug="decimal-check", text="cached answer")
    assert handle_registry.parse("wx²") is None
    with pytest.raises(NotFound, match="no live"):
        eye_handle(store, kind="websearch", id="²")
    for ordinal in ("²", "--1"):
        with pytest.raises(BadInput, match="bad chunk selector"):
            eye_handle(store, kind="websearch", id=f"wx{ref.id}~{ordinal}")


@pytest.mark.usefixtures("no_provider_fetch")
def test_youtube_video_shaped_handle_reads_live_ref(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """gr702962: an emitted eleven-character handle must retain ref identity."""
    with store.tx() as conn:
        row = conn.execute(
            "SELECT nextval(pg_get_serial_sequence('refs', 'ref_id'))"
        ).fetchone()
        assert row is not None
        previous = row[0]
        conn.execute(
            "SELECT setval(pg_get_serial_sequence('refs', 'ref_id'), 123456788)"
        )
    try:
        ref = _seed_cache(
            store, "youtube", slug="dQw4w9WgXcQ", text="intended transcript"
        )
    finally:
        with store.tx() as conn:
            conn.execute(
                "SELECT setval(pg_get_serial_sequence('refs', 'ref_id'), %s)",
                (previous,),
            )
    assert ref.id == 123456789
    # A real write stores ``_hash(_canonical_key(video_id))`` — seed that
    # hash so the URL read below hits by request hash. A foreign hash
    # would simulate a different variant under the shared slug, where a
    # refetch is the correct behaviour (gr:3681). The foreign hash made the
    # URL read miss and hit the no-fetch trap (gr753177).
    handler = runtime_with_store.hub.handler_for("youtube")
    assert handler is not None
    _seed_cache(
        store,
        "youtube",
        slug="yt123456789",
        text="competing video",
        request_hash=handler._hash(handler._canonical_key("yt123456789")),
    )
    note = store.insert_ref(kind="memory", slug=None, title="handle evidence")
    store.add_link(src_ref_id=note.id, dst_ref_id=ref.id)
    for explicit in ({}, {"kind": "youtube"}):
        for options in ({}, {"view": "links"}, {"extent": "fisheye+1hop"}):
            out = runtime_with_store.dispatch(
                "get", {"id": "yt123456789", **explicit, **options}
            )
            assert f"me{note.id}" in out, out
            assert "competing video" not in out, out
        out = runtime_with_store.dispatch("get", {"id": "yt123456789", **explicit})
        assert "intended transcript" in out, out
    out = runtime_with_store.dispatch(
        "get", {"kind": "youtube", "id": "https://youtu.be/yt123456789"}
    )
    assert "competing video" in out, out
    assert "intended transcript" not in out, out
