"""The skill index build is single-flight, bounded and retried (gr459844).

A cold 12-session burst held one ``search(kind='skill')`` for 592 s: every
concurrent first search ran its own full build inline, one 30 s-capped
embed per skill in turn, and a build with failures was kept as final, so
the skills it missed stayed out of semantic search until a restart.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from precis.embedder import MockEmbedder
from precis.skill_index import FileCorpusIndex
from precis.skill_index import index as index_mod

FILES = {
    "alpha": "# Alpha\n\nFirst content about apples and oranges.\n",
    "beta": "# Beta\n\n## Section\n\nSecond content about programming.\n",
    "gamma": "# Gamma\n\nThird content about gardening.\n",
}


class _CountingEmbedder(MockEmbedder):
    """Counts build embeds; can be slow, or fail for chosen texts."""

    def __init__(self, *, delay_s: float = 0.0, fail_on: str | None = None) -> None:
        super().__init__(dim=32)
        self.build_calls = 0
        self.delay_s = delay_s
        self.fail_on = fail_on
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.build_calls += 1
        time.sleep(self.delay_s)
        if self.fail_on is not None and any(self.fail_on in t for t in texts):
            raise RuntimeError("embedder busy")
        return super().embed(texts)


def _index(embedder: MockEmbedder, tmp_path: Path) -> FileCorpusIndex:
    return FileCorpusIndex(
        files=dict(FILES), embedder=embedder, cache_dir=tmp_path, cache_namespace="t"
    )


def test_concurrent_first_searches_share_one_build(tmp_path: Path) -> None:
    embedder = _CountingEmbedder(delay_s=0.05)
    idx = _index(embedder, tmp_path)
    threads = [threading.Thread(target=idx.search, args=("apples",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert embedder.build_calls == len(FILES)  # one embed per slug, not 8x


def test_a_slow_build_does_not_hold_the_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_mod, "_BUILD_WAIT_S", 0.2)
    embedder = _CountingEmbedder(delay_s=0.5)
    idx = _index(embedder, tmp_path)

    started = time.monotonic()
    assert idx.search("apples") == []  # semantic unavailable, lexical answers
    assert time.monotonic() - started < 1.0

    assert idx._build_thread is not None
    idx._build_thread.join(timeout=10.0)
    assert any(h.slug == "alpha" for h in idx.search("apples"))


def test_failed_slugs_are_retried_not_dropped_for_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_mod, "_BUILD_RETRY_S", 0.0)
    embedder = _CountingEmbedder(fail_on="programming")
    idx = _index(embedder, tmp_path)

    idx.search("apples")
    assert set(idx._entries or {}) == {"alpha", "gamma"}
    assert not idx._complete

    embedder.fail_on = None
    idx.search("programming")
    assert set(idx._entries or {}) == {"alpha", "beta", "gamma"}
    assert idx._complete


def test_retry_waits_for_the_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_mod, "_BUILD_RETRY_S", 3600.0)
    embedder = _CountingEmbedder(fail_on="programming")
    idx = _index(embedder, tmp_path)
    idx.search("apples")
    calls = embedder.build_calls
    idx.search("apples")
    assert embedder.build_calls == calls  # no rebuild inside the backoff


def test_an_empty_skill_is_not_a_failure(tmp_path: Path) -> None:
    embedder = _CountingEmbedder()
    idx = FileCorpusIndex(
        files={**FILES, "blank": ""},
        embedder=embedder,
        cache_dir=tmp_path,
        cache_namespace="t",
    )
    idx.search("apples")
    assert idx._complete
