"""Chunked-transcript reads for `YouTubeHandler` (gr540142, gr533135).

Before chunk selectors, a transcript landed as ONE unaddressable blob:
any video over the ~4 kB context cap was cached but unreadable through
the tool. These tests cover the fix surface:

- ingest emits time-windowed chunks (``start_s``/``end_s`` meta)
- ``id='<video>~N'`` / ``id='<video>~A..B'`` selector reads
- ``view='toc'`` — the chunk map with sizes and timestamps
- lazy re-chunk of legacy one-blob transcripts on first read
- the contract shapes: toc fits, each range fits, ``rest:`` names the
  next range
"""

from __future__ import annotations

import importlib.util
import itertools
import re
import sys
from typing import Any

import pytest

from precis._result_budget import (
    DEFAULT_CONTEXT_BODY_BYTES,
    fit_result_body,
    read_shape,
)
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.youtube import YouTubeHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ChunkInsert

VID = "vidCHUNKED01"[:11]

# Dispatch-level tests boot the full hub (``runtime_with_store``), which
# needs the [paper] extra. Absent on host; present in the container gate.
_NEEDS_PAPER_EXTRA = pytest.mark.skipif(
    importlib.util.find_spec("pysbd") is None,
    reason="full-hub boot needs the [paper] extra",
)


class _Snippet:
    """youtube-transcript-api snippet: text + caption timing."""

    def __init__(self, text: str, start: float = 0.0, duration: float = 2.0) -> None:
        self.text = text
        self.start = start
        self.duration = duration


class _LongApi:
    """Stub API returning a long caption track (~50 kB of snippets)."""

    def fetch(self, video_id: str, languages: list[str]) -> Any:
        return [
            _Snippet(f"caption line {i} " + "word " * 12, start=i * 30.0)
            for i in range(700)
        ]

    def list(self, video_id: str) -> Any:  # pragma: no cover - unused
        return []


@pytest.fixture(autouse=True)
def _patch_yt(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_yt = type(sys)("youtube_transcript_api")
    monkeypatch.setattr(fake_yt, "YouTubeTranscriptApi", _LongApi, raising=False)
    fake_errors = type(sys)("youtube_transcript_api._errors")

    class _Boom(Exception):
        pass

    for name in ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable"):
        monkeypatch.setattr(fake_errors, name, _Boom, raising=False)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", fake_yt)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", fake_errors)


@pytest.fixture
def handler(hub: Hub) -> YouTubeHandler:
    return YouTubeHandler(hub=hub)


@pytest.fixture
def cached(handler: YouTubeHandler, store: Store):
    """Fetch + cache the ~50 kB stub transcript; return (ref, blocks)."""
    handler.get(id=VID)
    found = store.get_cache_entry_by_slug(kind="youtube", slug=VID)
    assert found is not None
    return found[0], store.chunks.list_chunks_for_ref(found[0].id)


def _legacy_blob(store: Store, text: str, slug: str = "vidLEGACY001"[:11]):
    """Insert a pre-chunking one-blob transcript, the prod shape."""
    return store.put_cache_entry(
        kind="youtube",
        slug=slug,
        title="legacy video",
        body_blocks=[ChunkInsert(ord=0, text=text)],
        provider="youtube",
        request_hash=f"hash-{slug}",
        ttl_seconds=30 * 24 * 3600,
        cache_meta={"video_id": slug, "languages": ["en"]},
    )


def _blob_text(total_bytes: int = 50000) -> str:
    line = "the quick brown fox jumps over the lazy dog " * 2  # ~90 chars
    lines: list[str] = []
    n = 0
    while n < total_bytes:
        lines.append(f"{len(lines):05d} {line}")
        n += len(lines[-1]) + 1
    return "\n".join(lines)


# ── ingest chunking ──────────────────────────────────────────────────


def test_fetch_stores_time_windowed_chunks(cached) -> None:
    _ref, blocks = cached
    assert len(blocks) > 10
    for b in blocks:
        assert len(b.text.encode("utf-8")) <= 2100
        assert b.meta.get("start_s") is not None
        assert b.meta.get("end_s") is not None
    # Windows are contiguous and ascending.
    assert blocks[0].meta["start_s"] == 0.0
    for prev, nxt in itertools.pairwise(blocks):
        assert prev.meta["start_s"] <= nxt.meta["start_s"]


def test_chunk_text_round_trips(cached) -> None:
    """The windowed chunks still contain the full caption text."""
    _ref, blocks = cached
    joined = "\n".join(b.text for b in blocks)
    assert "caption line 0" in joined and "caption line 699" in joined


# ── ~N and ~A..B selectors ──────────────────────────────────────────


def test_single_chunk_selector(handler: YouTubeHandler, cached) -> None:
    _ref, blocks = cached
    resp = handler.get(id=f"{VID}~1")
    assert blocks[1].text in resp.body
    assert blocks[0].text not in resp.body
    assert len(resp.body.encode("utf-8")) < DEFAULT_CONTEXT_BODY_BYTES


def test_range_selector(handler: YouTubeHandler, cached) -> None:
    _ref, blocks = cached
    resp = handler.get(id=f"{VID}~1..2")
    assert blocks[1].text in resp.body and blocks[2].text in resp.body
    assert blocks[0].text not in resp.body and blocks[3].text not in resp.body
    # The range grammar and rendering are the base's (gr533135).
    assert f"_chunks ~1..2 of `{VID}`" in resp.body
    # Each chunk row carries its timestamp label.
    assert "`" + f"{VID}~1`" in resp.body
    assert "`" + f"{VID}~2`" in resp.body
    assert "–" in resp.body  # time window on the row


def test_range_selector_via_url(handler: YouTubeHandler, cached) -> None:
    _ref, blocks = cached
    resp = handler.get(id=f"https://youtu.be/{VID}~0..0")
    assert blocks[0].text in resp.body


def test_descending_range_refused(handler: YouTubeHandler, cached) -> None:
    with pytest.raises(BadInput, match="empty chunk range"):
        handler.get(id=f"{VID}~3..1")


def test_out_of_range_ord_notfound(handler: YouTubeHandler, cached) -> None:
    with pytest.raises(NotFound, match="no chunks in") as exc:
        handler.get(id=f"{VID}~900..901")
    assert "~" in str(exc.value.next)


def test_range_clamps_past_last_ord(handler: YouTubeHandler, cached) -> None:
    _ref, blocks = cached
    last = blocks[-1].ord
    resp = handler.get(id=f"{VID}~{last}..{last + 5}")
    assert blocks[-1].text in resp.body


def test_wide_range_hints_a_smaller_call(handler: YouTubeHandler, cached) -> None:
    """A range the explicit-read cap will cut carries a hint naming the
    continuation after the cut (``~<first chunk not shown>..hi``)."""
    _ref, blocks = cached
    last = blocks[-1].ord
    resp = handler.get(id=f"{VID}~0..{last}")
    assert resp.result_budget_hint is not None
    m = re.search(rf"id='{VID}~(\d+)\.\.{last}'", resp.result_budget_hint)
    assert m, resp.result_budget_hint
    assert 0 < int(m.group(1)) <= last


def test_read_shape_youtube_selector_is_explicit() -> None:
    """``read_shape``/``_tilde_is_selector`` recognise youtube ``~``
    selectors as explicit part-reads (gr533135)."""
    assert read_shape("get", {"kind": "youtube", "id": f"{VID}~0..2"}) == "explicit"
    assert (
        read_shape("get", {"kind": "youtube", "id": f"https://youtu.be/{VID}~1"})
        == "explicit"
    )


# ── view='toc' ───────────────────────────────────────────────────────


def test_toc_fits_with_sizes_and_times(handler: YouTubeHandler, cached) -> None:
    _ref, blocks = cached
    resp = handler.get(id=VID, view="toc")
    body = resp.body.encode("utf-8")
    assert len(body) < DEFAULT_CONTEXT_BODY_BYTES
    for b in blocks[:10]:
        assert f"{VID}~{b.ord}`".encode() in body or f"{VID}~{b.ord}".encode() in body
    assert b"kB" in body  # sizes via size_cell
    assert "0:00" in resp.body  # timestamps
    assert f"{len(blocks)} chunks" in resp.body  # aggregate line


def test_toc_never_fetches(handler: YouTubeHandler, store: Store) -> None:
    with pytest.raises(NotFound, match="not cached"):
        handler.get(id="vidNOCACHE1"[:11], view="toc")


# ── legacy one-blob transcripts ──────────────────────────────────────


def test_legacy_blob_rechunked_on_read(handler: YouTubeHandler, store: Store) -> None:
    ref, _cache = _legacy_blob(store, _blob_text(50000))
    handler.get(id="vidLEGACY001"[:11], no_fetch=True)
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    assert len(blocks) > 10
    assert all(len(b.text.encode("utf-8")) <= 2100 for b in blocks)
    assert all(b.meta.get("legacy_rechunk") for b in blocks)


def test_legacy_blob_rechunked_on_selector(
    handler: YouTubeHandler, store: Store
) -> None:
    ref, _cache = _legacy_blob(store, _blob_text(20000), slug="vidLEGACY02"[:11])
    resp = handler.get(id="vidLEGACY02~0")
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    assert len(blocks) > 1
    # The base's range render strips chunk-edge whitespace.
    assert blocks[0].text.strip() in resp.body
    assert len(resp.body.encode("utf-8")) < DEFAULT_CONTEXT_BODY_BYTES


def test_legacy_small_blob_left_alone(handler: YouTubeHandler, store: Store) -> None:
    ref, _cache = _legacy_blob(store, "short transcript body", slug="vidSMALL001"[:11])
    handler.get(id="vidSMALL001", no_fetch=True)
    assert len(store.chunks.list_chunks_for_ref(ref.id)) == 1


def test_rechunked_toc_and_ranges(handler: YouTubeHandler, store: Store) -> None:
    ref, _cache = _legacy_blob(store, _blob_text(50000), slug="vidLEGACY03"[:11])
    resp = handler.get(id="vidLEGACY03", view="toc")
    assert len(resp.body.encode("utf-8")) < DEFAULT_CONTEXT_BODY_BYTES
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    assert f"{len(blocks)} chunks" in resp.body
    # A range read on the re-chunked ref works and fits.
    r = handler.get(id="vidLEGACY03~0..1")
    assert blocks[0].text.strip() in r.body and blocks[1].text.strip() in r.body


# ── the contract: a 50 kB transcript fits at every step ─────────────


def test_oversized_transcript_fits_the_contract(
    handler: YouTubeHandler, cached, store: Store, monkeypatch
) -> None:
    """50 kB transcript: whole-body read → fitted head + selector
    outline + a next: call that itself fits (R3/R5)."""
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", "4096")
    _ref, blocks = cached
    resp = handler.get(id=VID, no_fetch=True)
    fitted = fit_result_body(
        resp.body,
        verb="get",
        kind="youtube",
        context_limited=True,
        targeted_hint=resp.result_budget_hint,
        read=read_shape("get", {"kind": "youtube", "id": VID}),
        outline=resp.budget_outline,
    )
    assert len(fitted.encode("utf-8")) <= DEFAULT_CONTEXT_BODY_BYTES
    assert "outline:" in fitted
    assert f"{VID}~" in fitted  # selector-bearing outline rows
    # The base's first-range hint (gr533135) is the targeted read.
    assert "next:" in fitted and f"id='{VID}~0..3'" in fitted


# ── dispatch level: Hub/runtime, not the handler directly ────────────


@_NEEDS_PAPER_EXTRA
def test_dispatch_toc_of_legacy_blob_fits_and_rechunks(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A 50 kB legacy one-blob transcript: ``view='toc'`` through the
    runtime re-windows it on first read and the map fits the 4 kB
    context cap."""
    slug = "vidDSPLEG01"[:11]
    ref, _cache = _legacy_blob(store, _blob_text(50000), slug=slug)
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "youtube", "id": slug, "view": "toc"}, fold=True
    )
    assert not is_error, body
    assert len(body.encode("utf-8")) <= 4096
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    assert len(blocks) > 10
    assert f"{len(blocks)} chunks" in body


@_NEEDS_PAPER_EXTRA
def test_dispatch_range_read_names_rest(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``~A..B`` through dispatch (over-wide, legacy blob re-windowed on
    the way) is fitted under the explicit-read cap, with a ``rest:`` line
    naming the continuation after the cut (not the first page)."""
    slug = "vidDSPLEG02"[:11]
    ref, _cache = _legacy_blob(store, _blob_text(50000), slug=slug)
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "youtube", "id": f"{slug}~0..999"}, fold=True
    )
    assert not is_error, body
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    assert len(blocks) > 10
    rest = next(ln for ln in body.split("\n") if ln.startswith("rest:"))
    m = re.search(rf"id='{slug}~(\d+)\.\.999'", rest)
    assert m, rest
    cut = int(m.group(1))
    # Everything before the named chunk is shown whole.
    assert 0 < cut < len(blocks) - 1
    assert blocks[cut - 1].text.strip() in body
    # No gap: the hint never skips past an unshown chunk (it may re-name
    # one chunk that squeezed in under the trailer reserve).
    assert blocks[cut + 1].text.strip() not in body
    assert blocks[0].text.strip() in body
    assert len(body.encode("utf-8")) <= 24576 + 1024


@_NEEDS_PAPER_EXTRA
def test_dispatch_chunk_handle_rewrite_and_nc_guard(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The dispatcher's chunk-handle → ``slug~ord`` rewrite, youtube
    flavour: youtube is a codeless kind — its chunks mint no universal
    handle, so ``<video>~N`` IS the address and a bare one
    self-identifies through dispatch to that chunk. ``nc<id>`` is the
    *news* chunk code; on a youtube chunk the kind check refuses it
    rather than misrouting."""
    slug = "vidDSPLEG03"[:11]
    ref, _cache = _legacy_blob(store, _blob_text(20000), slug=slug)
    # First read re-windows the blob.
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"id": f"{slug}~1"}, fold=True
    )
    assert not is_error, body
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    assert len(blocks) > 1
    assert blocks[1].text.strip() in body
    assert blocks[0].text.strip() not in body
    assert f"_chunks ~1 of `{slug}`" in body
    # The news chunk code never resolves a youtube chunk — kind guard.
    out, is_error = runtime_with_store.dispatch_with_status(
        "get", {"id": f"nc{blocks[0].id}"}
    )
    assert is_error
    assert blocks[0].text not in out


@_NEEDS_PAPER_EXTRA
def test_dispatch_sequential_rechunk_attempts_stay_consistent(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The concurrent lazy re-chunk race, simulated sequentially: two
    readers both see the legacy blob; the first dispatch read re-windows,
    and a second attempt on the same legacy state — the lock loser's
    path — hits the in-transaction re-check and is a no-op. The raw
    replace path itself (DELETE+INSERT) tolerates a repeated call."""
    from precis.handlers.youtube import _split_blob_window

    slug = "vidDSPLEG04"[:11]
    text = _blob_text(20000)
    ref, _cache = _legacy_blob(store, text, slug=slug)
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "youtube", "id": slug, "no_fetch": True}, fold=True
    )
    assert not is_error, body
    first = store.chunks.list_chunks_for_ref(ref.id)
    assert len(first) > 1
    assert all(b.meta.get("legacy_rechunk") for b in first)

    # Reader 2's check-and-replace on the same legacy state: the atomic
    # re-check under the refs lock sees the committed re-window and does
    # nothing.
    YouTubeHandler(hub=runtime_with_store.hub)._maybe_rechunk(ref)
    second = store.chunks.list_chunks_for_ref(ref.id)
    assert [b.text for b in second] == [b.text for b in first]

    # The replace path itself is idempotent on the same input.
    store.chunks.insert_chunks(ref.id, _split_blob_window(text), replace=True)
    third = store.chunks.list_chunks_for_ref(ref.id)
    assert [b.text for b in third] == [b.text for b in first]

    # A follow-up read is still clean and consistent.
    body2, is_error2 = runtime_with_store.dispatch_with_status(
        "get", {"kind": "youtube", "id": slug, "view": "toc"}, fold=True
    )
    assert not is_error2, body2
    assert store.chunks.count_chunks(ref.id) == len(third)


@_NEEDS_PAPER_EXTRA
def test_dispatch_rechunk_never_raises_on_store_fault(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch
) -> None:
    """A store fault inside the locked re-chunk (e.g. lock timeout or a
    unique violation that slipped the re-check) must not fail the read —
    the caller serves whatever chunks are stored."""
    import psycopg

    slug = "vidDSPLEG05"[:11]
    ref, _cache = _legacy_blob(store, _blob_text(20000), slug=slug)
    orig = store.chunks.insert_chunks

    def _boom(*a: Any, **kw: Any) -> Any:
        raise psycopg.errors.LockNotAvailable("lock timeout")

    monkeypatch.setattr(store.chunks, "insert_chunks", _boom)
    body, is_error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "youtube", "id": slug, "no_fetch": True}, fold=True
    )
    monkeypatch.setattr(store.chunks, "insert_chunks", orig)
    assert not is_error, body
    # The legacy blob is still there, untouched — and still served.
    assert store.chunks.count_chunks(ref.id) == 1
    assert "lazy dog" in body
