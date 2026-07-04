"""Unit tests for the shared ref-mention grammar + resolver.

DB-free: ``resolve_link_targets`` is exercised against a hand-rolled
fake store (duck-typed ``fetch_refs_by_ids`` + ``pool.connection``) so
the write-time autolinker's resolution logic is covered without a live
postgres. The end-to-end "memory create writes links" path is covered
by the DB-backed tests in ``test_memory.py``.
"""

from __future__ import annotations

from precis.utils import mentions

# ---------------------------------------------------------------------------
# extract_handles
# ---------------------------------------------------------------------------


def test_extract_prefixed_bare_paper_and_conv() -> None:
    body = (
        "see memory:6134 and paper:acheson26~12 plus the thread "
        "discord/1490/151/999 — also futrell25 bare."
    )
    handles = mentions.extract_handles(body)
    assert ("memory", "6134", None) in handles
    assert ("paper", "acheson26", "~12") in handles
    assert ("conv", "discord/1490/151/999", None) in handles
    assert ("paper", "futrell25", None) in handles


def test_extract_dedups_and_strips_hash() -> None:
    # ``memory:#6134`` and ``memory:6134`` collapse; repeats dropped.
    handles = mentions.extract_handles("memory:#6134 memory:6134 memory:6134")
    assert handles == [("memory", "6134", None)]


def test_extract_gates_on_allowlist_and_low_signal() -> None:
    # ``user:`` is not a precis kind; ``tag:`` is low-signal. Neither
    # should surface, even though both match the ``noun:value`` shape.
    handles = mentions.extract_handles("user:asa tag:open memory:1")
    assert handles == [("memory", "1", None)]


def test_chunk_to_pos() -> None:
    assert mentions.chunk_to_pos("~12") == 12
    assert mentions.chunk_to_pos("~1..5") is None  # range, not one chunk
    assert mentions.chunk_to_pos("~p3") is None  # pdf page, not a chunk
    assert mentions.chunk_to_pos(None) is None


# ---------------------------------------------------------------------------
# resolve_link_targets — fake store
# ---------------------------------------------------------------------------


class _FakeRef:
    def __init__(
        self, ref_id: int, deleted_at: object = None, kind: str = "memory"
    ) -> None:
        self.id = ref_id
        self.deleted_at = deleted_at
        self.kind = kind


class _FakeStore:
    """Minimal store double: numeric id lookup + cite_key / pub_id → ref_id."""

    def __init__(
        self,
        refs: dict[int, _FakeRef],
        cite_keys: dict[str, int],
        pub_ids: dict[str, int] | None = None,
    ) -> None:
        self._refs = refs
        self._cite = cite_keys
        # Keyed upper-case: the pub_id resolver matches case-insensitively.
        self._pub = {k.upper(): v for k, v in (pub_ids or {}).items()}
        self.pool = self  # resolve_handle_ref does `store.pool.connection()`

    # -- cite_key lookup path ------------------------------------------
    def connection(self):
        store = self

        class _Ctx:
            def __enter__(self_):
                return store

            def __exit__(self_, *_a):
                return False

        return _Ctx()

    def execute(self, _sql: str, params: tuple):
        ident = params[0]
        # Route on the SQL: the patent path filters id_kind='pub_id' with a
        # case-insensitive upper() compare; the legacy path is cite_key/pub_id.
        if "upper(id_value)" in _sql:
            store_rid = self._pub.get(str(ident).upper())
        else:
            store_rid = self._cite.get(ident)

        class _Cur:
            def fetchone(self_):
                return (store_rid,) if store_rid is not None else None

        return _Cur()

    # -- numeric lookup path -------------------------------------------
    def fetch_refs_by_ids(
        self, ids: list[int], include_deleted: bool = False
    ) -> dict[int, _FakeRef]:
        out: dict[int, _FakeRef] = {}
        for i in ids:
            ref = self._refs.get(i)
            if ref is None:
                continue
            if ref.deleted_at is not None and not include_deleted:
                continue
            out[i] = ref
        return out


def test_resolve_targets_numeric_slug_and_chunk() -> None:
    store = _FakeStore(
        refs={6134: _FakeRef(6134), 7: _FakeRef(7)},
        cite_keys={"acheson26": 7},
    )
    targets = mentions.resolve_link_targets(store, "memory:6134 and paper:acheson26~3")
    pairs = {(t.dst_ref_id, t.dst_pos) for t in targets}
    assert pairs == {(6134, None), (7, 3)}


def test_resolve_skips_missing_deleted_and_self() -> None:
    store = _FakeStore(
        refs={
            6134: _FakeRef(6134),
            50: _FakeRef(50, deleted_at="2026-01-01"),  # soft-deleted
        },
        cite_keys={},
    )
    # 9999 missing, 50 deleted, 6134 == exclude → all dropped.
    targets = mentions.resolve_link_targets(
        store,
        "memory:6134 memory:9999 memory:50",
        exclude_ref_id=6134,
    )
    assert targets == []


def test_resolve_dedups_repeated_target() -> None:
    store = _FakeStore(refs={6134: _FakeRef(6134)}, cite_keys={})
    targets = mentions.resolve_link_targets(store, "memory:6134 memory:6134")
    assert [(t.dst_ref_id, t.dst_pos) for t in targets] == [(6134, None)]


# ---------------------------------------------------------------------------
# Patent public-number autolinking (gripe #48807)
# ---------------------------------------------------------------------------


def test_bracketed_patent_pubnum_links_case_insensitively() -> None:
    # Memory cites the public number lower-cased; the pub_id row is upper.
    store = _FakeStore(
        refs={70: _FakeRef(70, kind="patent")},
        cite_keys={},
        pub_ids={"US9927397B1": 70},
    )
    targets = mentions.resolve_link_targets(store, "see [us9927397b1] for the method")
    assert [(t.dst_ref_id, t.dst_pos) for t in targets] == [(70, None)]


def test_bracketed_pubnum_dedups_across_case() -> None:
    store = _FakeStore(
        refs={70: _FakeRef(70, kind="patent")},
        cite_keys={},
        pub_ids={"US9927397B1": 70},
    )
    # Two mentions in different case resolve to the same patent → one edge.
    targets = mentions.resolve_link_targets(
        store, "[us9927397b1] and again [US9927397B1]"
    )
    assert [(t.dst_ref_id, t.dst_pos) for t in targets] == [(70, None)]


def test_unknown_bracketed_pubnum_stays_literal() -> None:
    # No pub_id row → no link (the over-fire gate). Prose like [US0000]
    # must not spuriously link.
    store = _FakeStore(refs={}, cite_keys={}, pub_ids={})
    assert mentions.resolve_link_targets(store, "the [US0000] filing") == []


def test_bracketed_pubnum_on_non_patent_kind_is_dropped() -> None:
    # A same-shaped pub_id belonging to another kind must not masquerade
    # as a patent link.
    store = _FakeStore(
        refs={70: _FakeRef(70, kind="finding")},
        cite_keys={},
        pub_ids={"US9927397B1": 70},
    )
    assert mentions.resolve_link_targets(store, "[us9927397b1]") == []


def test_unbracketed_pubnum_never_links() -> None:
    # Only the bracketed form is a link intent; bare prose stays literal
    # even when the pub_id exists (avoids over-firing on `US1234` text).
    store = _FakeStore(
        refs={70: _FakeRef(70, kind="patent")},
        cite_keys={},
        pub_ids={"US9927397B1": 70},
    )
    assert mentions.resolve_link_targets(store, "US9927397B1 was granted") == []
