"""Tests for `precis.md_index.vectors.MdVectorCache` and the semantic
search leg (`cosine_search` / `fuse_blocks`) in `precis.md_index.search`.

Pure logic, no real embedder — a small deterministic stub stands in
for `precis.embedder.Embedder` (matching `MdEmbedder`'s protocol
shape: `model`, `dim`, `embed`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

import numpy as np
import pytest

from precis.md_index import MdVectorCache, cosine_search, fuse_blocks
from precis.md_index.types import MdBlockEntry, MdFileEntry, MdRepoIndex


def _entry(
    *, file: str, text: str, slug: str = "block-0", pos: int = 0
) -> MdBlockEntry:
    """A minimal `MdBlockEntry`, sha256'd the same way the real indexer
    does — so content-addressing tests are exercising the real key
    derivation, not a test-only shortcut."""
    return MdBlockEntry(
        file=file,
        pos=pos,
        slug=slug,
        kind="paragraph",
        heading_level=None,
        title=None,
        heading_path=(),
        nearest_heading_slug=None,
        text=text,
        line_start=1,
        line_end=1,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


class _StubEmbedder:
    """Deterministic embedder stub: records every `embed()` call so
    tests can assert on batching / dedup, returns a fixed vector per
    call so ordering is predictable."""

    model = "stub-embedder"
    dim = 3

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector or [1.0, 0.0, 0.0]
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self.vector) for _ in texts]


# ---------------------------------------------------------------------------
# MdVectorCache: round-trip, atomicity, corruption, dedupe.
# ---------------------------------------------------------------------------


def test_add_get_roundtrip_in_memory(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=3, cache_dir=tmp_path)
    cache.add("sha-a", [1.0, 2.0, 3.0])

    vec = cache.get("sha-a")
    assert vec is not None
    np.testing.assert_allclose(vec, [1.0, 2.0, 3.0])
    assert "sha-a" in cache
    assert len(cache) == 1


def test_get_missing_returns_none(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=3, cache_dir=tmp_path)
    assert cache.get("nope") is None
    assert "nope" not in cache


def test_add_wrong_shape_raises(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=3, cache_dir=tmp_path)
    with pytest.raises(ValueError, match="shape"):
        cache.add("sha-a", [1.0, 2.0])


def test_add_duplicate_sha_is_noop(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    cache.add("sha-a", [1.0, 0.0])
    cache.add("sha-a", [0.0, 1.0])  # ignored: sha already present

    vec = cache.get("sha-a")
    assert vec is not None
    np.testing.assert_allclose(vec, [1.0, 0.0])
    assert len(cache) == 1


def test_missing_reports_uncached_and_dedupes(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    cache.add("sha-a", [1.0, 0.0])

    assert cache.missing(["sha-a", "sha-b", "sha-b", "sha-c"]) == ["sha-b", "sha-c"]


def test_no_pair_on_disk_is_cold_start_not_error(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    assert len(cache) == 0
    assert not cache.npz_path.exists()
    assert not cache.manifest_path.exists()


def test_flush_then_reload_roundtrips(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=3, cache_dir=tmp_path, flush_every=1000)
    cache.add("sha-1", [1.0, 2.0, 3.0])
    cache.add("sha-2", [4.0, 5.0, 6.0])
    cache.flush()

    assert cache.npz_path.exists()
    assert cache.manifest_path.exists()

    reloaded = MdVectorCache(model="m", dim=3, cache_dir=tmp_path)
    assert len(reloaded) == 2
    reloaded_1 = reloaded.get("sha-1")
    reloaded_2 = reloaded.get("sha-2")
    assert reloaded_1 is not None
    assert reloaded_2 is not None
    np.testing.assert_allclose(reloaded_1, [1.0, 2.0, 3.0], atol=1e-5)
    np.testing.assert_allclose(reloaded_2, [4.0, 5.0, 6.0], atol=1e-5)
    assert reloaded.missing(["sha-1", "sha-2", "sha-3"]) == ["sha-3"]


def test_flush_is_noop_when_not_dirty(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    cache.flush()  # nothing added yet
    assert not cache.npz_path.exists()


def test_flush_writes_atomically_no_tmp_files_left(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    cache.add("sha-a", [1.0, 0.0])
    cache.flush()

    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
    assert cache.npz_path.exists()
    assert cache.manifest_path.exists()


def test_auto_flush_every_n(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=2, cache_dir=tmp_path, flush_every=2)
    cache.add("sha-a", [1.0, 0.0])
    assert not cache.npz_path.exists()  # 1 dirty, threshold not hit

    cache.add("sha-b", [0.0, 1.0])
    assert cache.npz_path.exists()  # 2 dirty -> auto-flushed
    assert cache.manifest_path.exists()


def test_corrupt_manifest_warns_and_rebuilds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    cache.add("sha-a", [1.0, 0.0])
    cache.flush()

    cache.manifest_path.write_text("not json at all", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        reloaded = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)

    assert len(reloaded) == 0
    assert reloaded.missing(["sha-a"]) == ["sha-a"]
    assert any("corrupt" in r.message for r in caplog.records)


def test_missing_npz_with_manifest_present_warns_and_rebuilds(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    cache.add("sha-a", [1.0, 0.0])
    cache.flush()

    cache.npz_path.unlink()

    reloaded = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    assert len(reloaded) == 0


def test_dim_mismatch_in_manifest_rebuilds(tmp_path: Path) -> None:
    cache = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    cache.add("sha-a", [1.0, 0.0])
    cache.flush()

    manifest = json.loads(cache.manifest_path.read_text(encoding="utf-8"))
    manifest["dim"] = 999
    cache.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    reloaded = MdVectorCache(model="m", dim=2, cache_dir=tmp_path)
    assert len(reloaded) == 0


def test_default_cache_dir_uses_config_cache_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cache = MdVectorCache(model="BAAI/bge-m3", dim=1024)
    assert cache.cache_dir == tmp_path / "precis" / "md-vectors"
    assert cache.npz_path.name == "BAAI-bge-m3-1024.npz"


# ---------------------------------------------------------------------------
# embed_missing: batching + content-addressed dedupe across "roots".
# ---------------------------------------------------------------------------


def test_embed_missing_embeds_only_uncached_blocks(tmp_path: Path) -> None:
    cache = MdVectorCache(model="stub", dim=3, cache_dir=tmp_path)
    embedder = _StubEmbedder()

    a = _entry(file="a.md", text="alpha content", slug="s0")
    b = _entry(file="b.md", text="beta content", slug="s0")

    n = cache.embed_missing([a, b], embedder)
    assert n == 2
    assert len(embedder.calls) == 1  # one batched call
    assert set(embedder.calls[0]) == {"alpha content", "beta content"}
    assert a.sha256 in cache
    assert b.sha256 in cache

    # Second pass over the same blocks: nothing left to embed.
    n2 = cache.embed_missing([a, b], embedder)
    assert n2 == 0
    assert len(embedder.calls) == 1  # embedder untouched


def test_embed_missing_dedupes_identical_content_across_roots(tmp_path: Path) -> None:
    """Two blocks with identical text under different roots/files share
    one embedding — content-addressing by sha256, not by (file, pos)."""
    cache = MdVectorCache(model="stub", dim=3, cache_dir=tmp_path)
    embedder = _StubEmbedder()

    root1_block = _entry(file="root1/notes.md", text="shared prose", slug="s0")
    root2_block = _entry(file="root2/notes.md", text="shared prose", slug="s0")
    assert root1_block.sha256 == root2_block.sha256

    n1 = cache.embed_missing([root1_block], embedder)
    n2 = cache.embed_missing([root2_block], embedder)

    assert n1 == 1
    assert n2 == 0
    assert len(embedder.calls) == 1
    assert len(cache) == 1


def test_embed_missing_returns_zero_without_calling_embedder_when_fully_cached(
    tmp_path: Path,
) -> None:
    cache = MdVectorCache(model="stub", dim=3, cache_dir=tmp_path)
    embedder = _StubEmbedder()
    a = _entry(file="a.md", text="already have it", slug="s0")
    cache.add(a.sha256, [1.0, 0.0, 0.0])

    n = cache.embed_missing([a], embedder)
    assert n == 0
    assert embedder.calls == []


def test_embed_missing_mismatched_vector_count_raises(tmp_path: Path) -> None:
    cache = MdVectorCache(model="stub", dim=3, cache_dir=tmp_path)

    class _BadEmbedder:
        model = "bad"
        dim = 3

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0]]  # always returns 1, regardless of input

    a = _entry(file="a.md", text="one", slug="s0")
    b = _entry(file="b.md", text="two", slug="s0")
    with pytest.raises(ValueError, match="vectors"):
        cache.embed_missing([a, b], _BadEmbedder())


# ---------------------------------------------------------------------------
# Concurrency: background warm thread vs. concurrent request reads.
# ---------------------------------------------------------------------------


def test_concurrent_embed_missing_and_search_no_crash(tmp_path: Path) -> None:
    """Bounded concurrency smoke test for the race `MdVectorCache`'s
    docstring documents being locked against: one thread runs
    `embed_missing` (which calls `add()` under the lock, like the
    background md-index warm thread) while another concurrently calls
    `get`/`__contains__`/`cosine_search` (like a request thread
    serving `MdHandler.search`) on the *same* cache. Before locking,
    `add()`'s three-statement update of `_index`/`_shas`/`_vectors`
    could hand a concurrent reader an index one past the end of
    `_vectors` -> `IndexError`.

    Only asserts "no exception" and "converged final state" — no
    timing assumptions, so this stays deterministic and fast.
    """
    n_blocks = 300
    blocks = [
        _entry(file=f"f{i}.md", text=f"block number {i}", slug=f"s{i}")
        for i in range(n_blocks)
    ]
    index = _index_of(*blocks)
    cache = MdVectorCache(model="stub", dim=3, cache_dir=tmp_path, flush_every=25)
    embedder = _StubEmbedder([1.0, 0.0, 0.0])

    errors: list[BaseException] = []
    stop = threading.Event()

    def _writer() -> None:
        try:
            # Several small batches, not one big embed_missing() call,
            # so add() interleaves with the reader many times over
            # instead of racing it exactly once.
            batch = 20
            for i in range(0, n_blocks, batch):
                cache.embed_missing(blocks[i : i + batch], embedder)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)
        finally:
            stop.set()

    def _reader() -> None:
        try:
            probe = blocks[:20]
            while not stop.is_set():
                for b in probe:
                    cache.get(b.sha256)
                    _ = b.sha256 in cache
                cosine_search(index, [1.0, 0.0, 0.0], cache, limit=5)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    reader = threading.Thread(target=_reader)
    writer = threading.Thread(target=_writer)
    reader.start()
    writer.start()
    writer.join(timeout=5)
    reader.join(timeout=5)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert len(cache) == n_blocks
    assert cache.missing([b.sha256 for b in blocks]) == []


# ---------------------------------------------------------------------------
# cosine_search
# ---------------------------------------------------------------------------


def _index_of(*entries: MdBlockEntry) -> MdRepoIndex:
    files: dict[str, list[MdBlockEntry]] = {}
    for e in entries:
        files.setdefault(e.file, []).append(e)
    return MdRepoIndex.build(
        root=Path("/repo"),
        files=[MdFileEntry(file=f, blocks=tuple(bs)) for f, bs in files.items()],
    )


def test_cosine_search_ranks_closest_first(tmp_path: Path) -> None:
    a = _entry(file="a.md", text="alpha", slug="s0")
    b = _entry(file="b.md", text="beta", slug="s0")
    index = _index_of(a, b)

    cache = MdVectorCache(model="stub", dim=3, cache_dir=tmp_path)
    cache.add(a.sha256, [1.0, 0.0, 0.0])
    cache.add(b.sha256, [0.0, 1.0, 0.0])

    hits = cosine_search(index, [0.9, 0.1, 0.0], cache)
    assert [f for _, f, _ in hits] == ["a.md", "b.md"]
    assert hits[0][0] > hits[1][0]


def test_cosine_search_skips_blocks_without_cached_vectors(tmp_path: Path) -> None:
    a = _entry(file="a.md", text="alpha", slug="s0")
    b = _entry(file="b.md", text="beta", slug="s0")  # never embedded
    index = _index_of(a, b)

    cache = MdVectorCache(model="stub", dim=3, cache_dir=tmp_path)
    cache.add(a.sha256, [1.0, 0.0, 0.0])

    hits = cosine_search(index, [1.0, 0.0, 0.0], cache)
    assert [f for _, f, _ in hits] == ["a.md"]


def test_cosine_search_zero_query_vector_returns_no_hits(tmp_path: Path) -> None:
    a = _entry(file="a.md", text="alpha", slug="s0")
    index = _index_of(a)
    cache = MdVectorCache(model="stub", dim=3, cache_dir=tmp_path)
    cache.add(a.sha256, [1.0, 0.0, 0.0])

    assert cosine_search(index, [0.0, 0.0, 0.0], cache) == []


def test_cosine_search_respects_limit(tmp_path: Path) -> None:
    a = _entry(file="a.md", text="alpha", slug="s0")
    b = _entry(file="b.md", text="beta", slug="s0")
    index = _index_of(a, b)
    cache = MdVectorCache(model="stub", dim=3, cache_dir=tmp_path)
    cache.add(a.sha256, [1.0, 0.0, 0.0])
    cache.add(b.sha256, [0.9, 0.1, 0.0])

    hits = cosine_search(index, [1.0, 0.0, 0.0], cache, limit=1)
    assert len(hits) == 1
    assert hits[0][1] == "a.md"


# ---------------------------------------------------------------------------
# fuse_blocks
# ---------------------------------------------------------------------------


def test_fuse_blocks_prefers_double_leg_hits() -> None:
    a = _entry(file="a.md", text="a", slug="s0")
    b = _entry(file="b.md", text="b", slug="s0")
    c = _entry(file="c.md", text="c", slug="s0")

    lexical = [(10.0, "a.md", a), (5.0, "b.md", b)]
    semantic = [(0.9, "a.md", a), (0.1, "c.md", c)]

    fused = fuse_blocks(lexical, semantic)
    fused_files = [f for _, f, _ in fused]

    assert fused_files[0] == "a.md"  # rank 1 in both legs -> highest fused score
    assert set(fused_files) == {"a.md", "b.md", "c.md"}
    # b.md and c.md are both rank-2-in-one-leg-only -> tie, broken by file.
    assert fused_files[1:] == ["b.md", "c.md"]


def test_fuse_blocks_matches_store_rrf_formula() -> None:
    a = _entry(file="a.md", text="a", slug="s0")
    lexical = [(1.0, "a.md", a)]
    semantic = [(1.0, "a.md", a)]

    fused = fuse_blocks(lexical, semantic, k=60)
    assert fused[0][0] == pytest.approx(1.0 / 61 + 1.0 / 61)


def test_fuse_blocks_handles_empty_semantic_leg() -> None:
    a = _entry(file="a.md", text="a", slug="s0")
    b = _entry(file="b.md", text="b", slug="s0")
    lexical = [(5.0, "a.md", a), (3.0, "b.md", b)]

    fused = fuse_blocks(lexical, [])
    assert [f for _, f, _ in fused] == ["a.md", "b.md"]


def test_fuse_blocks_handles_empty_lexical_leg() -> None:
    a = _entry(file="a.md", text="a", slug="s0")
    b = _entry(file="b.md", text="b", slug="s0")
    semantic = [(0.8, "a.md", a), (0.5, "b.md", b)]

    fused = fuse_blocks([], semantic)
    assert [f for _, f, _ in fused] == ["a.md", "b.md"]


def test_fuse_blocks_respects_limit() -> None:
    a = _entry(file="a.md", text="a", slug="s0")
    b = _entry(file="b.md", text="b", slug="s0")
    lexical = [(5.0, "a.md", a), (3.0, "b.md", b)]

    fused = fuse_blocks(lexical, [], limit=1)
    assert len(fused) == 1
    assert fused[0][1] == "a.md"
