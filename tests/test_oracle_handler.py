"""Phase F — oracle handler tests.

Two layers, mirroring the wisdom test layout that came before:

1. **Unit-level**: helper functions, registry surface, kind metadata.
   No DB needed.

2. **Read/write surface**: tested via the in-memory ``_FakeStore``
   pattern reused from the random_handler tests.  Covers tradition
   listing, per-tradition overview, chunk-append put.

Live-store integration (real PG + pgvector) is deferred to the
acatome-store integration suite.  This file confirms the handler
builds, registers, and round-trips through fakes cleanly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from precis.handlers.oracle import (
    OracleHandler,
    _now_iso,
    _parse_meta,
)
from precis.protocol import ErrorCode, PrecisError


# ---------------------------------------------------------------------------
# Helper-fn unit tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_now_iso_format(self):
        s = _now_iso()
        assert "T" in s
        assert s.endswith("Z")
        assert len(s) == 20  # YYYY-MM-DDTHH:MM:SSZ

    def test_parse_meta_dict(self):
        ref = {"meta": {"a": 1}}
        assert _parse_meta(ref) == {"a": 1}

    def test_parse_meta_json_string(self):
        ref = {"meta": '{"a": 1}'}
        assert _parse_meta(ref) == {"a": 1}

    def test_parse_meta_legacy_metadata_field(self):
        ref = {"metadata": {"b": 2}}
        assert _parse_meta(ref) == {"b": 2}

    def test_parse_meta_invalid_json_returns_empty(self):
        ref = {"meta": "not-json"}
        assert _parse_meta(ref) == {}


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_oracle_kind_registered(self):
        from precis.registry import KINDS, _discover

        _discover()
        assert "oracle" in KINDS, (
            f"oracle not in KINDS — got: {sorted(KINDS.keys())}"
        )

    def test_oracle_handler_class(self):
        from precis.registry import KINDS, _discover

        _discover()
        oracle_kind = KINDS["oracle"]
        assert oracle_kind.handler_cls is OracleHandler
        assert OracleHandler.scheme == "oracle"
        assert OracleHandler.corpus_id == "oracle"
        assert OracleHandler.writable is True

    def test_oracle_corpus_seed_present(self):
        from acatome_store.models import CORPUS_SEEDS

        ids = {row[0] for row in CORPUS_SEEDS}
        assert "oracle" in ids

    def test_oracle_corpus_sample_unit_chunk(self):
        from acatome_store.models import CORPUS_SEEDS

        oracle_row = next(r for r in CORPUS_SEEDS if r[0] == "oracle")
        assert len(oracle_row) == 8, (
            "oracle seed must include sample_unit (8-tuple)"
        )
        assert oracle_row[7] == "chunk"

    def test_wisdom_kind_retired(self):
        """The old wisdom kind should be gone."""
        from precis.registry import KINDS, _discover

        _discover()
        assert "wisdom" not in KINDS

    def test_oracle_examples_present(self):
        from precis.registry import KINDS, _discover

        _discover()
        spec = KINDS["oracle"].spec
        assert spec.examples
        assert any("oracle:iching" in e for e in spec.examples)
        assert any("random:?corpus=oracle" in e for e in spec.examples)
        assert any("not-tag" in e for e in spec.examples)


# ---------------------------------------------------------------------------
# Fake store wired for the oracle handler
# ---------------------------------------------------------------------------


class _FakeRef:
    def __init__(
        self,
        slug: str,
        title: str,
        *,
        ref_id: int = 1,
        tags: list[str] | None = None,
        meta: dict | None = None,
    ):
        self.id = ref_id
        self.slug = slug
        self.corpus_id = "oracle"
        self.title = title
        self.tags = json.dumps(tags) if tags else None
        self.meta = json.dumps(meta) if meta else None

    def to_dict(self):
        return {
            "id": self.id,
            "slug": self.slug,
            "corpus_id": self.corpus_id,
            "title": self.title,
            "tags": self.tags,
            "meta": self.meta,
        }


class _FakeStore:
    """Tiny stub implementing only the methods OracleHandler calls."""

    def __init__(self, refs: list[_FakeRef], blocks: dict[str, list[dict]]):
        self._refs = {r.slug: r for r in refs}
        self._blocks = blocks
        self._created_blocks: list[dict] = []

    def list_refs_by_corpus(self, corpus_id: str, limit: int = 100):
        return [r.to_dict() for r in self._refs.values()
                if r.corpus_id == corpus_id]

    def get(self, slug):
        ref = self._refs.get(slug)
        return ref.to_dict() if ref else None

    def get_blocks(self, slug):
        return self._blocks.get(slug, [])

    def create_ref(self, *, slug, corpus_id, title, metadata, tags, blocks):
        new_ref = _FakeRef(
            slug=slug, title=title, ref_id=len(self._refs) + 1,
            tags=tags, meta=metadata,
        )
        self._refs[slug] = new_ref
        self._blocks.setdefault(slug, [])

    def update_ref_metadata(self, slug, meta, merge=True):
        ref = self._refs.get(slug)
        if ref is None:
            return
        existing = json.loads(ref.meta) if ref.meta else {}
        if merge:
            existing.update(meta)
            ref.meta = json.dumps(existing)
        else:
            ref.meta = json.dumps(meta)

    def get_toc(self, slug):
        return self._blocks.get(slug, [])

    def get_links(self, slug):
        return []

    # The OracleHandler's _append_entry uses ``store._Session()``
    # via SQLAlchemy directly to upsert blocks.  We don't fake that
    # full path here — chunk-append integration is deferred to the
    # live-store suite.  The test `test_append_creates_ref_when_missing`
    # exercises the create_ref path before the SQL hop, which is the
    # part this fake covers cleanly.


# ---------------------------------------------------------------------------
# Read-side tests
# ---------------------------------------------------------------------------


def _read(handler, path: str) -> str:
    return handler.read(
        path=path, selector=None, view=None, subview=None,
        query="", summarize=False, depth=0, page=1,
    )


def _patch_store(store):
    """Patch every get_store entry point used by oracle + ref_base.

    The OracleHandler imports _get_store from precis.handlers.oracle
    (its own helper), but super().read() routes through RefHandler
    which has its own _get_store in precis.handlers._ref_base.  Both
    ultimately delegate to ``precis._store.get_store`` — patching
    that root function is the cleanest hook.
    """
    return patch("precis._store.get_store", return_value=store)


class TestListTraditions:
    def test_empty_corpus_lists_setup_hint(self):
        store = _FakeStore([], {})
        with patch("precis.handlers.oracle._get_store", return_value=store):
            h = OracleHandler()
            out = _read(h, "")
        assert "empty" in out.lower()
        assert "precis-ingest-oracle" in out
        assert "put(type='oracle'" in out

    def test_lists_built_in_and_personal(self):
        refs = [
            _FakeRef("oracle:iching", "I-Ching",
                     tags=["oracle", "i-ching", "built-in"]),
            _FakeRef("oracle:stoic", "Stoic",
                     tags=["oracle", "stoic", "built-in"]),
            _FakeRef("oracle:personal", "Personal",
                     tags=["oracle"]),
        ]
        blocks = {
            "oracle:iching": [
                {"block_index": i, "text": f"hex-{i}"} for i in range(64)
            ],
            "oracle:stoic": [
                {"block_index": i, "text": f"aph-{i}"} for i in range(4)
            ],
            "oracle:personal": [
                {"block_index": 0, "text": "my note"},
            ],
        }
        store = _FakeStore(refs, blocks)
        with patch("precis.handlers.oracle._get_store", return_value=store):
            h = OracleHandler()
            out = _read(h, "")
        assert "## Built-in" in out
        assert "## Personal" in out
        assert "iching" in out
        assert "personal" in out
        # Entry counts visible.
        assert "64 entries" in out
        assert "4 entries" in out
        assert "1 entry" in out

    def test_by_tradition_alias(self):
        refs = [
            _FakeRef("oracle:iching", "I-Ching", tags=["oracle", "built-in"]),
        ]
        store = _FakeStore(refs, {"oracle:iching": []})
        with patch("precis.handlers.oracle._get_store", return_value=store):
            h = OracleHandler()
            a = _read(h, "")
            b = _read(h, "/by-tradition")
        assert a == b


class TestTraditionOverview:
    def _store_with_iching(self):
        ref = _FakeRef(
            "oracle:iching", "I-Ching",
            tags=["oracle", "i-ching", "built-in"],
            meta={
                "tradition": "iching",
                "description": (
                    "The Book of Changes. 64 archetypes for re-framing."
                ),
            },
        )
        blocks = [
            {"block_index": i, "text": f"hex {i}", "section_path":
             json.dumps([f"Hexagram {i+1}"])}
            for i in range(64)
        ]
        return _FakeStore([ref], {"oracle:iching": blocks})

    def test_overview_renders_with_count(self):
        store = self._store_with_iching()
        with _patch_store(store):
            h = OracleHandler()
            out = _read(h, "iching")
        assert "I-Ching" in out
        assert "64 entries" in out
        assert "The Book of Changes" in out

    def test_overview_shows_tradition_tag_in_random_hint(self):
        store = self._store_with_iching()
        with _patch_store(store):
            h = OracleHandler()
            out = _read(h, "iching")
        assert "random:?corpus=oracle&tag=i-ching" in out

    def test_overview_excludes_built_in_from_tag_hint(self):
        store = self._store_with_iching()
        with _patch_store(store):
            h = OracleHandler()
            out = _read(h, "iching")
        # The tag-hint should not point at "built-in".
        assert "tag=built-in" not in out

    def test_small_tradition_lists_entries(self):
        ref = _FakeRef("oracle:stoic", "Stoic", tags=["oracle", "stoic"])
        blocks = [
            {"block_index": i, "text": f"aphorism {i}",
             "section_path": json.dumps([f"Aphorism {i+1}"])}
            for i in range(3)
        ]
        store = _FakeStore([ref], {"oracle:stoic": blocks})
        with _patch_store(store):
            h = OracleHandler()
            out = _read(h, "stoic")
        assert "Aphorism 1" in out
        assert "Aphorism 2" in out
        assert "Aphorism 3" in out

    def test_large_tradition_shows_sample_only(self):
        store = self._store_with_iching()
        with _patch_store(store):
            h = OracleHandler()
            out = _read(h, "iching")
        # 64 entries → "Sample entries" + "and N more" footer.
        assert "Sample entries" in out
        assert "and 61 more" in out

    def test_browse_block_uses_tilde_selector_for_chunks(self):
        """Chunks/ranges advertise ~N (canonical), not /N (view).

        The wider URI grammar reserves ``/V`` for named views (toc,
        abstract, fig/3, …) and ``~N`` for selectors (chunk index,
        range, slug).  Oracle was the only kind advertising ``/N``
        for chunks, which actually returned ``view_unknown`` because
        the routing correctly treated numeric path segments as view
        names.  Lock the canonical advertisement in.
        """
        store = self._store_with_iching()
        with _patch_store(store):
            h = OracleHandler()
            out = _read(h, "iching")
        # Selector syntax for chunk + range.
        assert "get(id='oracle:iching~0')" in out
        assert "oracle:iching~0..9" in out
        # /toc remains a view, so still /-prefixed.
        assert "get(id='oracle:iching/toc')" in out
        # Old broken syntax must not be advertised.
        assert "get(id='oracle:iching/0')" not in out
        assert "oracle:iching/0.." not in out
