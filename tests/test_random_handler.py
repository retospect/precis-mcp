"""Phase C — random handler tests.

Split into two layers:

1. **Pure parsing** (no DB) — ``_split_query``, ``_parse_corpora``,
   ``_clamp_n``, ``_clamp_radius``.  Fast, no fixtures.
2. **Dispatch** — stubs out ``get_store`` / ``store.index.search_text``
   with lightweight fakes so the handler's dispatch plumbing is
   tested without needing pgvector / sentence-transformers /
   acatome-store on the test path.  Real integration is covered by
   the cross-corpus suite once a store is available.

The fake store returns known refs / blast hits so we can assert
rendering, bucket-by-corpus, seeded reproducibility, and the
footer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from precis.handlers import random_handler as rh
from precis.handlers.random_handler import (
    RandomHandler,
    _clamp_n,
    _clamp_radius,
    _parse_corpora,
    _parse_float,
    _parse_int,
    _split_query,
)
from precis.protocol import ErrorCode, PrecisError


def _read(h: RandomHandler, path: str) -> str:
    return h.read(
        path=path,
        selector=None,
        view=None,
        subview=None,
        query="",
        summarize=False,
        depth=0,
        page=1,
    )


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


class TestSplitQuery:
    def test_no_query(self):
        assert _split_query("my problem") == ("my problem", {})

    def test_leading_query(self):
        assert _split_query("?n=3&corpus=wisdom") == (
            "",
            {"n": "3", "corpus": "wisdom"},
        )

    def test_trailing_query(self):
        assert _split_query("my problem?n=3") == (
            "my problem",
            {"n": "3"},
        )

    def test_empty_pair_ignored(self):
        assert _split_query("?n=3&") == ("", {"n": "3"})


class TestParseInt:
    def test_default(self):
        assert _parse_int({}, "n", 1) == 1

    def test_present(self):
        assert _parse_int({"n": "5"}, "n", 1) == 5

    def test_invalid(self):
        with pytest.raises(PrecisError) as exc:
            _parse_int({"n": "abc"}, "n", 1)
        assert exc.value.code == ErrorCode.PARAM_INVALID


class TestParseFloat:
    def test_none(self):
        assert _parse_float({}, "radius") is None

    def test_present(self):
        assert _parse_float({"radius": "0.5"}, "radius") == 0.5

    def test_invalid(self):
        with pytest.raises(PrecisError):
            _parse_float({"radius": "tight"}, "radius")


class TestParseCorpora:
    def test_empty(self):
        assert _parse_corpora({}) == []

    def test_single(self):
        assert _parse_corpora({"corpus": "papers"}) == ["papers"]

    def test_list(self):
        assert _parse_corpora({"corpora": "papers,wisdom"}) == [
            "papers",
            "wisdom",
        ]

    def test_trim_whitespace(self):
        assert _parse_corpora({"corpora": " a , b ,c "}) == [
            "a",
            "b",
            "c",
        ]

    def test_both_keys_combine(self):
        # If someone sets both — we take singular first, then list.
        # Net effect: combined deduped order.
        out = _parse_corpora({"corpus": "papers", "corpora": "wisdom,notes"})
        assert out == ["papers", "wisdom", "notes"]


class TestClamps:
    def test_n_low(self):
        with pytest.raises(PrecisError):
            _clamp_n(0)

    def test_n_high(self):
        with pytest.raises(PrecisError):
            _clamp_n(21)

    def test_n_ok(self):
        assert _clamp_n(5) == 5

    def test_radius_neg(self):
        with pytest.raises(PrecisError):
            _clamp_radius(-0.1)

    def test_radius_over(self):
        with pytest.raises(PrecisError):
            _clamp_radius(1.5)

    def test_radius_ok(self):
        assert _clamp_radius(0.3) == 0.3


# ---------------------------------------------------------------------------
# Help view (no store needed)
# ---------------------------------------------------------------------------


class TestHelp:
    def test_help_shape(self):
        h = RandomHandler()
        out = _read(h, "/help")
        assert "random" in out.lower()
        assert "uniform" in out.lower()
        assert "blast" in out.lower()
        assert "corpus=oracle" in out.lower()

    def test_help_bare(self):
        h = RandomHandler()
        assert _read(h, "help") == _read(h, "/help")


# ---------------------------------------------------------------------------
# Uniform dispatch with fake store
# ---------------------------------------------------------------------------


class _FakeRef:
    def __init__(
        self, corpus_id: str, slug: str, title: str,
        tags: list[str] | None = None, ref_id: int = 0,
    ):
        import json as _json
        self.id = ref_id
        self.corpus_id = corpus_id
        self.slug = slug
        self.title = title
        # Mirrors the Ref.tags JSON-as-text shape.
        self.tags = _json.dumps(tags) if tags else None


class _FakeBlock:
    """Minimal stand-in for an acatome Block used by chunk-mode tests."""

    def __init__(
        self, ref_id: int, block_index: int, text: str,
        section_path: str = "[]",
    ):
        self.ref_id = ref_id
        self.block_index = block_index
        self.text = text
        self.profile = "default"
        self.section_path = section_path


class _FakeQuery:
    """Tolerant fake — accepts ``query(Ref)`` for ref mode and
    ``query(Block, Ref).join(...)`` for chunk mode.  Returns the
    appropriate row shape based on what was passed."""

    def __init__(
        self, rows: list, *, mode: str = "ref",
    ):
        self._rows = rows
        self._mode = mode

    def join(self, *args, **kwargs):  # noqa: ARG002
        return self

    def filter(self, *args, **kwargs):  # noqa: ARG002
        return self

    def limit(self, n):  # noqa: ARG002
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(
        self,
        rows: list,
        *,
        chunk_rows: list | None = None,
        corpus_overrides: dict[str, str] | None = None,
    ):
        self._rows = rows
        self._chunk_rows = chunk_rows or []
        self._corpus_overrides = corpus_overrides or {}

    def query(self, *models):
        # ``query(Ref)`` → ref mode; ``query(Block, Ref)`` → chunk mode.
        if len(models) >= 2:
            return _FakeQuery(self._chunk_rows, mode="chunk")
        return _FakeQuery(self._rows, mode="ref")

    def get(self, model, key):
        """Used by ``_resolve_sample_unit`` to look up Corpus.sample_unit.

        Returns a stand-in object with ``sample_unit`` from
        ``corpus_overrides``, or None when the corpus isn't seeded.
        """
        unit = self._corpus_overrides.get(key)
        if unit is None:
            return None
        return type("_FakeCorpus", (), {"sample_unit": unit})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ARG002
        return False


class _FakeStore:
    def __init__(
        self,
        refs: list[_FakeRef],
        hits: list[dict[str, Any]] | None = None,
        chunk_rows: list | None = None,
        corpus_overrides: dict[str, str] | None = None,
    ):
        self._refs = refs
        self._hits = hits or []
        self._chunk_rows = chunk_rows or []
        self._corpus_overrides = corpus_overrides or {}
        self.index = SimpleNamespace(
            search_text=lambda query, top_k=5, where=None, max_distance=None: self._hits,
        )

    def _Session(self):
        return _FakeSession(
            self._refs,
            chunk_rows=self._chunk_rows,
            corpus_overrides=self._corpus_overrides,
        )


@pytest.fixture
def fake_store_factory():
    """Produce a fake store with injectable refs + blast hits.

    Optional kwargs:
      - ``chunk_rows``: list of ``(_FakeBlock, _FakeRef)`` tuples for
        chunk-mode tests.
      - ``corpus_overrides``: dict mapping corpus_id → sample_unit
        (e.g. ``{'oracle': 'chunk'}``) so the handler resolves the
        right path.
    """
    def _make(refs, hits=None, chunk_rows=None, corpus_overrides=None):
        return _FakeStore(
            refs, hits=hits, chunk_rows=chunk_rows,
            corpus_overrides=corpus_overrides,
        )
    return _make


class TestUniform:
    def test_empty_store(self, fake_store_factory):
        store = fake_store_factory([])
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "")
        # New no-hits message format.
        assert "no refs match" in out.lower()

    def test_single_pick(self, fake_store_factory):
        refs = [_FakeRef("wisdom", "zhao-san-mu-si", "Chaos at morning")]
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "")
        assert "zhao-san-mu-si" in out
        assert "mode=uniform" in out
        assert "seed=os" in out

    def test_n_picks(self, fake_store_factory):
        refs = [_FakeRef("wisdom", f"slug-{i}", f"Title {i}") for i in range(10)]
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?n=3")
        # Exactly 3 refs rendered — count the "📚" marker.
        assert out.count("📚") == 3

    def test_seeded_reproducible(self, fake_store_factory):
        refs = [_FakeRef("wisdom", f"slug-{i}", f"Title {i}") for i in range(20)]
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            a = _read(h, "?n=3&seed=42")
            b = _read(h, "?n=3&seed=42")
        assert a == b

    def test_corpus_filter_echoed_in_footer(self, fake_store_factory):
        refs = [_FakeRef("wisdom", "x", "X")]
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?corpus=wisdom")
        assert "corpora=[wisdom]" in out

    def test_multi_corpus_filter_echoed(self, fake_store_factory):
        refs = [_FakeRef("wisdom", "x", "X")]
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?corpora=wisdom,memories")
        assert "corpora=[wisdom,memories]" in out

    def test_tag_filter_keeps_matching_refs(self, fake_store_factory):
        # Two refs in oracle, one tagged stoic, one tagged chengyu.
        # ?tag=stoic should return only the stoic one.
        refs = [
            _FakeRef("oracle", "stoic", "Stoic", tags=["oracle", "stoic"]),
            _FakeRef("oracle", "chengyu", "Chengyu", tags=["oracle", "chengyu"]),
        ]
        # Force ref-mode (override the chunk-mode default for oracle)
        # since the test isn't exercising chunk-rendering here.
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?corpus=oracle&tag=stoic&from=refs")
        assert "oracle: stoic" in out
        assert "oracle: chengyu" not in out
        assert "tag=stoic" in out  # filter summary shown

    def test_not_tag_filter_excludes(self, fake_store_factory):
        refs = [
            _FakeRef("oracle", "a", "A",
                     tags=["oracle", "stoic", "built-in"]),
            _FakeRef("oracle", "b", "B",
                     tags=["oracle", "stoic"]),  # personal
        ]
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?corpus=oracle&not-tag=built-in&from=refs")
        assert "oracle: a" not in out
        assert "oracle: b" in out
        assert "not-tag=built-in" in out

    def test_tag_union_any_match(self, fake_store_factory):
        refs = [
            _FakeRef("oracle", "stoic", "S", tags=["oracle", "stoic"]),
            _FakeRef("oracle", "koan", "K", tags=["oracle", "koan"]),
            _FakeRef("oracle", "chengyu", "C", tags=["oracle", "chengyu"]),
        ]
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?corpus=oracle&tag=stoic,koan&from=refs&n=5")
        # Tag union: both stoic and koan accepted, chengyu rejected.
        assert "oracle: stoic" in out
        assert "oracle: koan" in out
        assert "oracle: chengyu" not in out

    def test_tag_filter_no_matches_message(self, fake_store_factory):
        refs = [_FakeRef("oracle", "a", "A", tags=["oracle", "stoic"])]
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?corpus=oracle&tag=does-not-exist&from=refs")
        assert "no refs match" in out.lower()
        assert "tag=[does-not-exist]" in out

    def test_n_cap_rejected(self, fake_store_factory):
        refs = [_FakeRef("wisdom", "x", "X")]
        store = fake_store_factory(refs)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            with pytest.raises(PrecisError):
                _read(h, "?n=25")


# ---------------------------------------------------------------------------
# Chunk-sampling mode (oracle corpus)
# ---------------------------------------------------------------------------


class TestChunkMode:
    """Random sampling at chunk granularity, driven by Corpus.sample_unit
    (or explicit ``?from=chunks``)."""

    def _make_chunk_pool(self):
        """Build a small fake chunk pool: 3 refs, 2 chunks each."""
        refs = [
            _FakeRef("oracle", "iching", "I-Ching",
                     tags=["oracle", "i-ching", "built-in"], ref_id=1),
            _FakeRef("oracle", "stoic", "Stoic",
                     tags=["oracle", "stoic", "built-in"], ref_id=2),
            _FakeRef("oracle", "personal", "Personal",
                     tags=["oracle"], ref_id=3),
        ]
        chunks = [
            (_FakeBlock(1, 0, "Hexagram 1 — creative"), refs[0]),
            (_FakeBlock(1, 1, "Hexagram 2 — receptive"), refs[0]),
            (_FakeBlock(2, 0, "Amor fati — love what is"), refs[1]),
            (_FakeBlock(2, 1, "Memento mori"), refs[1]),
            (_FakeBlock(3, 0, "My note about a decision"), refs[2]),
            (_FakeBlock(3, 1, "Another note"), refs[2]),
        ]
        return refs, chunks

    def test_explicit_from_chunks(self, fake_store_factory):
        """``?from=chunks`` forces chunk mode regardless of corpus."""
        refs, chunks = self._make_chunk_pool()
        store = fake_store_factory(refs, chunk_rows=chunks)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?from=chunks&corpus=oracle&n=2&seed=1")
        # Chunk-mode output uses 📜 marker per chunk, not 📚.
        assert "📜" in out
        assert out.count("📜") == 2
        assert "mode=uniform-chunk" in out

    def test_corpus_sample_unit_drives_chunk_mode(self, fake_store_factory):
        """Without ?from=, the corpus's sample_unit decides."""
        refs, chunks = self._make_chunk_pool()
        store = fake_store_factory(
            refs, chunk_rows=chunks,
            corpus_overrides={"oracle": "chunk"},
        )
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?corpus=oracle&n=1&seed=1")
        assert "📜" in out
        assert "mode=uniform-chunk" in out

    def test_chunk_tag_filter_excludes_built_in(self, fake_store_factory):
        """``?not-tag=built-in&from=chunks`` returns only personal chunks."""
        refs, chunks = self._make_chunk_pool()
        store = fake_store_factory(refs, chunk_rows=chunks)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(
                h, "?from=chunks&corpus=oracle&not-tag=built-in&n=5",
            )
        # Only the 'personal' ref's chunks should appear.
        assert "personal›0" in out or "personal›1" in out
        # Built-in refs' chunks should not.
        assert "iching›" not in out
        assert "stoic›" not in out

    def test_chunk_tag_filter_iching_only(self, fake_store_factory):
        """``?tag=i-ching&from=chunks`` returns only iching chunks."""
        refs, chunks = self._make_chunk_pool()
        store = fake_store_factory(refs, chunk_rows=chunks)
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?from=chunks&corpus=oracle&tag=i-ching&n=5")
        assert "iching›" in out
        assert "stoic›" not in out
        assert "personal›" not in out

    def test_chunk_explicit_from_refs_overrides_corpus(
        self, fake_store_factory,
    ):
        """``?from=refs`` forces ref mode even on a chunk corpus."""
        refs, chunks = self._make_chunk_pool()
        store = fake_store_factory(
            refs, chunk_rows=chunks,
            corpus_overrides={"oracle": "chunk"},
        )
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?corpus=oracle&from=refs&n=1&seed=1")
        assert "📚" in out  # ref mode
        assert "mode=uniform-ref" in out

    def test_chunk_no_hits_message(self, fake_store_factory):
        """Chunk-mode no-hits surfaces a chunk-specific hint."""
        store = fake_store_factory(
            [], chunk_rows=[],
            corpus_overrides={"oracle": "chunk"},
        )
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "?corpus=oracle")
        assert "no chunks match" in out.lower()

    def test_invalid_from_rejected(self, fake_store_factory):
        store = fake_store_factory([])
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            with pytest.raises(PrecisError):
                _read(h, "?from=potato")


# ---------------------------------------------------------------------------
# Blast-radius dispatch with fake store
# ---------------------------------------------------------------------------


class TestBlastRadius:
    @staticmethod
    def _fake_hits() -> list[dict[str, Any]]:
        return [
            {
                "distance": 0.12,
                "metadata": {
                    "corpus_id": "wisdom",
                    "slug": "a",
                    "ref_title": "Aphorism A",
                },
            },
            {
                "distance": 0.18,
                "metadata": {
                    "corpus_id": "papers",
                    "slug": "p",
                    "ref_title": "Paper P",
                },
            },
            {
                "distance": 0.22,
                "metadata": {
                    "corpus_id": "wisdom",
                    "slug": "b",
                    "ref_title": "Aphorism B",
                },
            },
        ]

    def test_blast_renders_hits(self, fake_store_factory):
        store = fake_store_factory([], hits=self._fake_hits())
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "my problem?n=3")
        assert "💥 blast-radius near" in out
        assert "my problem" in out
        assert "Aphorism A" in out
        assert "Paper P" in out
        assert "0.120" in out

    def test_blast_groups_by_corpus(self, fake_store_factory):
        store = fake_store_factory([], hits=self._fake_hits())
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "my problem?n=3")
        # Both wisdom entries should appear under a single "wisdom:" header.
        assert out.count("wisdom:") == 1
        assert out.count("papers:") == 1

    def test_blast_no_hits_hint(self, fake_store_factory):
        store = fake_store_factory([], hits=[])
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "my problem?n=3&radius=0.2")
        assert "no hits" in out.lower()
        assert "radius 0.2" in out

    def test_blast_radius_passed_through(self, fake_store_factory, monkeypatch):
        """Verify ``radius=`` reaches ``search_text`` as ``max_distance``."""
        seen: dict[str, Any] = {}

        def _search(query, top_k=5, where=None, max_distance=None):  # noqa: ARG001
            seen["query"] = query
            seen["top_k"] = top_k
            seen["where"] = where
            seen["max_distance"] = max_distance
            return []

        store = fake_store_factory([])
        store.index.search_text = _search
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            _read(h, "cascading failure?n=5&radius=0.4&corpus=wisdom")
        assert seen["query"] == "cascading failure"
        assert seen["top_k"] == 5
        assert seen["max_distance"] == 0.4
        assert seen["where"] == {"corpus_id": "wisdom"}

    def test_blast_multi_corpus_uses_in_filter(self, fake_store_factory):
        seen: dict[str, Any] = {}

        def _search(query, top_k=5, where=None, max_distance=None):  # noqa: ARG001
            seen["where"] = where
            return []

        store = fake_store_factory([])
        store.index.search_text = _search
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            _read(h, "idea?corpora=wisdom,papers")
        assert seen["where"] == {"corpus_id": {"$in": ["wisdom", "papers"]}}

    def test_blast_footer_mode(self, fake_store_factory):
        store = fake_store_factory([], hits=self._fake_hits())
        with patch("precis._store.get_store", return_value=store):
            h = RandomHandler()
            out = _read(h, "my problem?n=3")
        assert "mode=blast-radius" in out
