"""Tests for FileCorpusIndex + chunker + cache.

The index is exercised against the deterministic ``MockEmbedder`` so
the test suite stays hermetic — no sentence-transformers, no torch,
no model downloads. Skill-handler-level tests (``test_skill.py``)
cover the integration with the live skill corpus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.skill_index import FileCorpusIndex, chunk_by_h2
from precis.skill_index.cache import EmbeddingCache, default_cache_dir
from precis.skill_index.chunker import CHUNKER_VERSION


def json_load(path: Path) -> dict[str, Any]:
    """Tiny helper used by cache-introspection tests."""
    return json.loads(path.read_text(encoding="utf-8"))


# ── chunker ──────────────────────────────────────────────────────────


def test_chunker_drops_front_matter() -> None:
    text = (
        "---\n"
        "title: foo\n"
        "status: active\n"
        "---\n\n"
        "# Heading\n\nIntro text.\n\n"
        "## Section A\n\nA body.\n"
    )
    chunks = chunk_by_h2(text)
    # Front matter not included anywhere.
    for c in chunks:
        assert "title: foo" not in c.text


def test_chunker_yields_head_and_sections() -> None:
    text = (
        "# Top\n\n"
        "Intro paragraph.\n\n"
        "## First\n\n"
        "First body.\n\n"
        "## Second\n\n"
        "Second body.\n"
    )
    chunks = chunk_by_h2(text)
    headings = [c.heading for c in chunks]
    assert headings == ["", "First", "Second"]
    # Head chunk holds the H1 + intro.
    assert "Intro paragraph" in chunks[0].text
    # Each section keeps its H2 line.
    assert "## First" in chunks[1].text
    assert "First body" in chunks[1].text


def test_chunker_no_h2_returns_single_chunk() -> None:
    text = "# Only H1\n\nBody only.\n"
    chunks = chunk_by_h2(text)
    assert len(chunks) == 1
    assert chunks[0].heading == ""
    assert "Body only" in chunks[0].text


def test_chunker_aliases_consecutive_h2s_sharing_body() -> None:
    # Consecutive H2s with only whitespace between them form an
    # alias group: each heading emits a chunk sharing the body
    # that follows. CHUNKER_VERSION 2 behaviour (was: drop the
    # bodyless heading).
    text = "## Empty\n\n## Has body\n\nbody.\n"
    chunks = chunk_by_h2(text)
    headings = [c.heading for c in chunks]
    assert headings == ["Empty", "Has body"]
    # Both chunks carry the shared body.
    assert "body." in chunks[0].text
    assert "body." in chunks[1].text
    # Each chunk's text leads with its own alias heading, not the
    # group's other headings.
    assert chunks[0].text.startswith("## Empty\n")
    assert chunks[1].text.startswith("## Has body\n")


def test_chunker_drops_alias_group_at_eof_with_no_body() -> None:
    # Three back-to-back H2s with nothing after — no shared body
    # to attach, so the entire group is dropped.
    text = "## A\n## B\n## C\n"
    chunks = chunk_by_h2(text)
    assert chunks == []


def test_chunker_handles_blank_input() -> None:
    assert chunk_by_h2("") == []
    assert chunk_by_h2("   \n\n  ") == []


def test_chunker_alias_group_of_three_emits_three_chunks() -> None:
    text = (
        "## Check that citations are valid\n"
        "## Run an in-depth check of citations\n"
        "## Verify sources are real\n"
        "\n"
        "shared body content.\n"
    )
    chunks = chunk_by_h2(text)
    headings = [c.heading for c in chunks]
    assert headings == [
        "Check that citations are valid",
        "Run an in-depth check of citations",
        "Verify sources are real",
    ]
    for c in chunks:
        assert "shared body content." in c.text


def test_chunker_mixes_standalone_and_alias_group() -> None:
    text = (
        "## Standalone first\nfirst body.\n"
        "## Alias A\n"
        "## Alias B\n"
        "shared body.\n"
        "## Standalone last\nlast body.\n"
    )
    chunks = chunk_by_h2(text)
    headings = [c.heading for c in chunks]
    assert headings == [
        "Standalone first",
        "Alias A",
        "Alias B",
        "Standalone last",
    ]
    assert "first body." in chunks[0].text
    assert "shared body." in chunks[1].text
    assert "shared body." in chunks[2].text
    assert "last body." in chunks[3].text


def test_chunker_default_emits_no_body_only_twins() -> None:
    # The structural default (used by ``slug~N`` and the TOC adapter)
    # never produces body-only twins.
    text = "## A\n## B\n\nshared body.\n## C\nc body.\n"
    chunks = chunk_by_h2(text)
    assert all(not c.body_only for c in chunks)


def test_chunker_with_body_aliases_appends_one_twin_per_group() -> None:
    text = (
        "## Standalone first\nfirst body.\n"
        "## Alias A\n"
        "## Alias B\n"
        "shared body.\n"
        "## Standalone last\nlast body.\n"
    )
    chunks = chunk_by_h2(text, with_body_aliases=True)
    structural = [c for c in chunks if not c.body_only]
    twins = [c for c in chunks if c.body_only]

    # Structural chunks are identical to the default chunking and
    # come first; twins are appended after.
    assert [c.heading for c in structural] == [
        "Standalone first",
        "Alias A",
        "Alias B",
        "Standalone last",
    ]
    assert chunks[: len(structural)] == structural

    # One twin per group (the two aliases share a single twin), so
    # three groups → three twins.
    assert [c.heading for c in twins] == [
        "Standalone first",
        "Alias A",
        "Standalone last",
    ]
    # Twin text is heading-stripped — the section body alone.
    assert twins[0].text == "first body."
    assert twins[1].text == "shared body."
    assert twins[2].text == "last body."
    for c in twins:
        assert not c.text.startswith("#")


def test_chunker_body_only_twin_for_single_section() -> None:
    text = "## Gotchas\n\nrevalidate every SSRF redirect.\n"
    chunks = chunk_by_h2(text, with_body_aliases=True)
    fused = [c for c in chunks if not c.body_only]
    twins = [c for c in chunks if c.body_only]
    assert len(fused) == 1 and len(twins) == 1
    assert fused[0].text == "## Gotchas\nrevalidate every SSRF redirect."
    assert twins[0].text == "revalidate every SSRF redirect."
    assert twins[0].heading == "Gotchas"


# ── cache ────────────────────────────────────────────────────────────


def test_cache_round_trip(tmp_path: Path) -> None:
    from precis.skill_index.cache import CachedChunk, CacheEntry

    cache = EmbeddingCache(
        cache_dir=tmp_path,
        namespace="test",
        embedder_model="mock",
        chunker_version=1,
    )
    entry = CacheEntry(
        slug="hello",
        file_sha256="abc",
        embedder_model="mock",
        chunker_version=1,
        chunks=[CachedChunk(heading="H", text="t", embedding=[0.1, 0.2, 0.3])],
    )
    cache.save(entry)
    out = cache.load("hello", "abc")
    assert out is not None
    assert out.slug == "hello"
    assert out.chunks[0].embedding == [0.1, 0.2, 0.3]


def test_cache_round_trip_preserves_body_only(tmp_path: Path) -> None:
    from precis.skill_index.cache import CachedChunk, CacheEntry

    cache = EmbeddingCache(
        cache_dir=tmp_path,
        namespace="test",
        embedder_model="mock",
        chunker_version=CHUNKER_VERSION,
    )
    cache.save(
        CacheEntry(
            slug="hello",
            file_sha256="abc",
            embedder_model="mock",
            chunker_version=CHUNKER_VERSION,
            chunks=[
                CachedChunk(heading="H", text="## H\nbody", embedding=[0.1]),
                CachedChunk(heading="H", text="body", embedding=[0.2], body_only=True),
            ],
        )
    )
    out = cache.load("hello", "abc")
    assert out is not None
    assert [c.body_only for c in out.chunks] == [False, True]


def test_cache_loads_legacy_file_without_body_only_key(tmp_path: Path) -> None:
    # Files written before v3 have no ``body_only`` key; they must
    # still load (defaulting to False) rather than failing the shape
    # check. (The chunker_version bump invalidates them on read for
    # the embedding path, but the loader itself must be tolerant.)
    cache = EmbeddingCache(
        cache_dir=tmp_path,
        namespace="test",
        embedder_model="mock",
        chunker_version=7,
    )
    path = cache.path_for("legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "slug": "legacy",
                "file_sha256": "s",
                "embedder_model": "mock",
                "chunker_version": 7,
                "chunks": [{"heading": "H", "text": "t", "embedding": [0.5]}],
            }
        ),
        encoding="utf-8",
    )
    out = cache.load("legacy", "s")
    assert out is not None
    assert out.chunks[0].body_only is False


def test_cache_miss_on_sha_mismatch(tmp_path: Path) -> None:
    from precis.skill_index.cache import CachedChunk, CacheEntry

    cache = EmbeddingCache(
        cache_dir=tmp_path,
        namespace="test",
        embedder_model="mock",
        chunker_version=1,
    )
    entry = CacheEntry(
        slug="hello",
        file_sha256="abc",
        embedder_model="mock",
        chunker_version=1,
        chunks=[CachedChunk(heading="", text="", embedding=[])],
    )
    cache.save(entry)
    assert cache.load("hello", "different-sha") is None


def test_cache_miss_on_chunker_version_change(tmp_path: Path) -> None:
    from precis.skill_index.cache import CachedChunk, CacheEntry

    cache = EmbeddingCache(
        cache_dir=tmp_path,
        namespace="test",
        embedder_model="mock",
        chunker_version=1,
    )
    cache.save(
        CacheEntry(
            slug="x",
            file_sha256="s",
            embedder_model="mock",
            chunker_version=1,
            chunks=[CachedChunk(heading="", text="", embedding=[])],
        )
    )
    # New cache instance with a bumped chunker version should not
    # see the stale entry, even at the same path.
    cache2 = EmbeddingCache(
        cache_dir=tmp_path,
        namespace="test",
        embedder_model="mock",
        chunker_version=2,
    )
    assert cache2.load("x", "s") is None


def test_cache_corrupt_file_returns_none(tmp_path: Path) -> None:
    cache = EmbeddingCache(
        cache_dir=tmp_path,
        namespace="test",
        embedder_model="mock",
        chunker_version=1,
    )
    # Manually plant a junk file at the expected path.
    path = cache.path_for("broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{{")
    assert cache.load("broken", "any-sha") is None


def test_default_cache_dir_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_CACHE_DIR", "/tmp/custom-cache")
    assert default_cache_dir() == Path("/tmp/custom-cache")


# ── index ────────────────────────────────────────────────────────────


@pytest.fixture
def files() -> dict[str, str]:
    """A toy 3-skill corpus for index tests.

    Each ``slug`` has a unique vocabulary so MockEmbedder's hash-
    derived vectors stay distinguishable. The mock isn't semantic,
    but it IS deterministic — an embedded query that's a substring
    of the chunk text will hash to a vector closer to the chunk
    than to unrelated chunks (small probability of false ties).
    """
    return {
        "alpha": "# Alpha\n\nFirst content about apples and oranges.\n",
        "beta": "# Beta\n\n## Section\n\nSecond content about programming.\n",
        "gamma": "# Gamma\n\nThird content about gardening.\n",
    }


def test_index_unavailable_without_embedder(files: dict[str, str]) -> None:
    idx = FileCorpusIndex(files=files, embedder=None)
    assert not idx.is_available()
    assert idx.search("anything") == []


def test_index_returns_hits_with_mock_embedder(
    files: dict[str, str], tmp_path: Path
) -> None:
    idx = FileCorpusIndex(
        files=files,
        embedder=MockEmbedder(dim=64),
        cache_dir=tmp_path,
        cache_namespace="test",
    )
    assert idx.is_available()
    hits = idx.search("First content about apples and oranges.", page_size=10)
    assert hits, "expected at least one hit"
    # MockEmbedder is hash-based, not semantic — ranks aren't tied
    # to content overlap with the query. What we can assert is that
    # the chunk whose text contains the query DOES surface in the
    # returned set with a positive score.
    slugs = {h.slug for h in hits}
    assert "alpha" in slugs
    assert all(h.score > 0 for h in hits if h.slug == "alpha")


def test_index_query_embed_hang_fails_fast(
    files: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged embedder call must not block ``search`` indefinitely.

    Regression for an observed incident: ``search(kind='skill', ...)``
    idled for the full 1800s MCP client timeout before being force-
    aborted, shaped like the ``embedder_wedged_warming`` bug (a stuck
    in-process embedder call with no bound). ``_bounded`` in
    ``skill_index.index`` caps any single embedder call at
    ``_EMBED_CALL_TIMEOUT_S``; shrink that cap here so the test itself
    doesn't take 30s to prove the guard fires.
    """
    import time

    from precis.skill_index import index as index_mod

    monkeypatch.setattr(index_mod, "_EMBED_CALL_TIMEOUT_S", 0.2)

    class _HangingEmbedder:
        @property
        def dim(self) -> int:  # pragma: no cover — unused
            return 32

        @property
        def model(self) -> str:
            return "hanging"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return MockEmbedder(dim=32).embed(texts)

        def embed_one(self, text: str) -> list[float]:
            # Simulate a wedged in-process call (e.g. a deadlocked
            # encode) — sleeps far longer than the shrunk cap above.
            time.sleep(5)
            return MockEmbedder(dim=32).embed_one(text)  # pragma: no cover

    idx = FileCorpusIndex(
        files=files,
        embedder=_HangingEmbedder(),
        cache_dir=tmp_path,
        cache_namespace="test",
    )
    start = time.monotonic()
    hits = idx.search("apples")
    elapsed = time.monotonic() - start
    # Query embed hangs, but the per-slug build embeds happily (build
    # doesn't call embed_one) — so we only exercise the query-side cap
    # here. It must return promptly (well under the 5s sleep) with no
    # hits, rather than blocking until the embedder call returns.
    assert hits == []
    assert elapsed < 2.0, f"search() blocked for {elapsed:.2f}s past its cap"


def test_index_caches_to_disk(files: dict[str, str], tmp_path: Path) -> None:
    embedder = MockEmbedder(dim=32)
    idx = FileCorpusIndex(
        files=files,
        embedder=embedder,
        cache_dir=tmp_path,
        cache_namespace="test",
    )
    # First search builds + writes.
    hits1 = idx.search("apples")
    assert hits1
    # Cache files exist on disk under the namespaced layout.
    cache_root = tmp_path / "test" / "mock" / f"v{CHUNKER_VERSION}"
    assert cache_root.is_dir()
    cache_files = list(cache_root.glob("*.json"))
    assert len(cache_files) == len(files)

    # Fresh index over the same files — should populate from cache,
    # not re-embed. We can't observe re-embed cost directly, but we
    # CAN swap the embedder for one that always raises and verify
    # search still works (because the cache is hit before the call).
    class _BoomEmbedder:
        @property
        def dim(self) -> int:  # pragma: no cover — unused
            return 32

        @property
        def model(self) -> str:
            return "mock"  # same model name → cache hit

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("should not be called for cache-hit chunks")

        def embed_one(self, text: str) -> list[float]:
            # Query embedding still goes through this — keep it
            # deterministic so cosine ranks something.
            return MockEmbedder(dim=32).embed_one(text)

    idx2 = FileCorpusIndex(
        files=files,
        embedder=_BoomEmbedder(),
        cache_dir=tmp_path,
        cache_namespace="test",
    )
    hits2 = idx2.search("apples")
    assert hits2  # cache hit served us, no _BoomEmbedder.embed() call


def test_index_embeds_body_only_twins(tmp_path: Path) -> None:
    # The index's default chunker emits body-only twins, so a skill
    # with a fused section also caches a heading-stripped twin. The
    # structural-only ``chunk_by_h2`` count is the lower bound.
    files = {"beta": "# Beta\n\n## Section\n\nSecond content.\n"}
    idx = FileCorpusIndex(
        files=files,
        embedder=MockEmbedder(dim=32),
        cache_dir=tmp_path,
        cache_namespace="test",
    )
    idx.search("anything")  # forces build
    entry = (idx._entries or {})["beta"]
    structural = [c for c in entry.chunks if not c.body_only]
    twins = [c for c in entry.chunks if c.body_only]
    # head chunk + one section = 2 structural; one section body = 1 twin.
    assert len(structural) == len(chunk_by_h2(files["beta"]))
    assert len(twins) == 1
    assert twins[0].text == "Second content."


def test_index_invalidates_on_file_change(
    files: dict[str, str], tmp_path: Path
) -> None:
    """A changed file → different sha256 → cache miss → re-embed.

    We assert the invariant directly via the cache layer instead of
    via ranking: MockEmbedder is hash-based and has no semantic
    notion of topic, so we can't legitimately ask for "networking"
    to rank ``alpha`` first. What we CAN check is that the new
    content's chunk vectors land in the cache (so subsequent boots
    don't re-embed) and that the entry's sha256 reflects the edit.
    """
    embedder = MockEmbedder(dim=32)
    idx = FileCorpusIndex(
        files=files,
        embedder=embedder,
        cache_dir=tmp_path,
        cache_namespace="test",
    )
    idx.search("anything")  # warm cache

    # Snapshot alpha's pre-edit cached entry.
    from precis.skill_index.cache import EmbeddingCache

    cache = EmbeddingCache(
        cache_dir=tmp_path,
        namespace="test",
        embedder_model="mock",
        chunker_version=CHUNKER_VERSION,
    )
    pre = json_load(cache.path_for("alpha"))
    pre_sha = pre["file_sha256"]

    # Edit alpha and rebuild a fresh index.
    files2 = dict(files)
    files2["alpha"] = "# Alpha\n\nCompletely different content about networking.\n"
    idx2 = FileCorpusIndex(
        files=files2,
        embedder=embedder,
        cache_dir=tmp_path,
        cache_namespace="test",
    )
    idx2.search("anything")  # forces rebuild

    post = json_load(cache.path_for("alpha"))
    assert post["file_sha256"] != pre_sha
    # Body of post-edit chunk reflects the new content.
    assert any("networking" in c["text"] for c in post["chunks"]), (
        "edited content should appear in the rebuilt cache entry"
    )
