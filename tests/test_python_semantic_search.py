"""Python-kind hybrid search: semantic fusion, degradation, warm fallback."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.handlers import _python_semantic as sem
from precis.handlers.python import PythonHandler

# Concept axes: a text lights up an axis when it contains any keyword.
_AXES = [
    ("stale", "expire", "invalidate", "evict", "ttl"),
    ("ssrf", "outbound", "redirect", "private"),
    ("parse", "tokenize"),
]


class _ConceptEmbedder:
    model = "concept-stub"
    dim = 4

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def embed_one(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("embedder down")
        low = text.lower()
        v = [1.0 if any(k in low for k in axis) else 0.0 for axis in _AXES]
        return [*v, 0.05]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self.embed_one(t) for t in texts]


def _write(repo: Path, rel: str, content: str) -> None:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    _write(r, "pkg/__init__.py", "")
    _write(
        r,
        "pkg/cache.py",
        '''
        def evict_expired(store):
            """Drop entries whose ttl has expired so readers never see them."""

        def render(x):
            """Format x for display."""
        ''',
    )
    _write(
        r,
        "pkg/net.py",
        '''
        def safe_get(url):
            """Fetch a url, refusing redirects into private ranges."""
        ''',
    )
    _write(
        r,
        "tests/test_cache.py",
        '''
        def _helper():
            """stale helper"""

        def test_evict():
            """stale"""
        ''',
    )
    return r


def _handler(repo: Path, embedder: object | None) -> PythonHandler:
    h = PythonHandler(hub=Hub(embedder=cast(Any, embedder)), roots={"r": repo})
    h._semantic._cache_dir = repo.parent / "vec"
    return h


def _handles(body: str) -> list[str]:
    return re.findall(r"^## (\S+)", body, flags=re.M)


def test_paraphrase_found_semantically_not_lexically(repo: Path) -> None:
    h = _handler(repo, _ConceptEmbedder())
    q = "where do we handle stale data"
    assert "pkg.cache.evict_expired" not in h.search(q=q, mode="lexical").body
    h._semantic.start_warmup(h._all_symbols, block=True)
    assert h._semantic.state == "ready"
    body = h.search(q=q).body
    assert _handles(body)[0] == "r::pkg.cache.evict_expired"
    assert "sim=" in body
    assert "lexical only" not in body


def test_exact_name_stays_first(repo: Path) -> None:
    h = _handler(repo, _ConceptEmbedder())
    h._semantic.start_warmup(h._all_symbols, block=True)
    assert _handles(h.search(q="render").body)[0] == "r::pkg.cache.render"
    assert _handles(h.search(q="pkg.net.safe_get").body)[0] == "r::pkg.net.safe_get"


def test_no_embedder_degrades_with_note(repo: Path) -> None:
    body = _handler(repo, None).search(q="render").body
    assert "(lexical only: no embedder wired)" in body
    assert _handles(body)[0] == "r::pkg.cache.render"


def test_embedder_error_degrades(repo: Path) -> None:
    h = _handler(repo, _ConceptEmbedder())
    h._semantic.start_warmup(h._all_symbols, block=True)
    h._semantic.embedder = _ConceptEmbedder(fail=True)  # query embed now fails
    body = h.search(q="render").body
    assert "(lexical only: embedder unavailable)" in body
    assert "r::pkg.cache.render" in body


def test_not_warm_falls_back_to_lexical_and_says_so(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _handler(repo, _ConceptEmbedder())
    # Suppress the lazy warm so the index stays cold.
    monkeypatch.setattr(h._semantic, "start_warmup", lambda *a, **k: None)
    body = h.search(q="render").body
    assert "semantic index warming (0% ready)" in body
    assert _handles(body)[0] == "r::pkg.cache.render"


def test_semantic_mode_and_unknown_mode(repo: Path) -> None:
    h = _handler(repo, _ConceptEmbedder())
    h._semantic.start_warmup(h._all_symbols, block=True)
    q = "how are outbound urls protected against ssrf"
    assert _handles(h.search(q=q, mode="semantic").body)[0] == "r::pkg.net.safe_get"
    with pytest.raises(Exception, match="unknown python search mode"):
        h.search(q="x", mode="bogus")


def test_reindex_embeds_only_changed_symbols(repo: Path) -> None:
    emb = _ConceptEmbedder()
    h = _handler(repo, emb)
    h._semantic.start_warmup(h._all_symbols, block=True)
    cache = h._semantic.cache()
    assert cache is not None
    n0 = len(cache)
    h._semantic.state = "idle"
    calls = emb.calls
    h._semantic.start_warmup(h._all_symbols, block=True)
    assert emb.calls == calls
    assert len(cache) == n0


def test_private_test_helpers_not_embedded(repo: Path) -> None:
    h = _handler(repo, _ConceptEmbedder())
    names = {s.name: sem.embeddable(s) for s in h._all_symbols()}
    assert names["_helper"] is False
    assert names["test_evict"] is True


def test_stale_vectors_pruned_only_when_they_dominate(repo: Path) -> None:
    h = _handler(repo, _ConceptEmbedder())
    h._semantic.start_warmup(h._all_symbols, block=True)
    cache = h._semantic.cache()
    assert cache is not None
    live = len(cache)
    # A minority of stale vectors survives a warm pass ...
    cache.add("stale-0", [0.0, 0.0, 0.0, 1.0])
    h._semantic.state = "idle"
    h._semantic.start_warmup(h._all_symbols, block=True)
    assert "stale-0" in cache
    # ... a majority is pruned, and the prune is persisted.
    for i in range(1, live + 2):
        cache.add(f"stale-{i}", [0.0, 0.0, 0.0, 1.0])
    h._semantic.state = "idle"
    h._semantic.start_warmup(h._all_symbols, block=True)
    assert len(cache) == live
    assert "stale-0" not in cache
    reloaded = sem.MdVectorCache(
        model=cache.model, dim=cache.dim, cache_dir=cache.cache_dir
    )
    assert len(reloaded) == live


def test_python_cache_flushes_in_large_batches(repo: Path) -> None:
    h = _handler(repo, _ConceptEmbedder())
    cache = h._semantic.cache()
    assert cache is not None
    assert cache.flush_every == sem._FLUSH_EVERY >= 1000


def test_keep_set_symbols_survive_prune(repo: Path) -> None:
    h = _handler(repo, _ConceptEmbedder())
    h._semantic.start_warmup(h._all_symbols, block=True)
    cache = h._semantic.cache()
    assert cache is not None
    live = len(cache)
    assert live > 0
    held = h._all_symbols()
    # Nothing is live, but every vector belongs to a held (worktree) symbol.
    h._semantic.state = "idle"
    h._semantic.start_warmup(lambda: [], keep=lambda: held, block=True)
    assert len(cache) == live
    # Without the keep-set the same vectors are all stale and get pruned.
    h._semantic.state = "idle"
    h._semantic.start_warmup(lambda: [], block=True)
    assert len(cache) == 0
