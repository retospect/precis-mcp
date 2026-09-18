"""In-memory mock of the precis-mcp store surface our handlers use.

The real handlers run against precis-mcp's PostgreSQL-backed
:class:`precis.store.Store`. For end-to-end handler tests we don't
want to stand that up — the structural plumbing (ref insert, meta
update, links, chunks, soft-delete) is what we want to exercise,
not the SQL.

This mock implements just the surface our DFT handlers reach for:

- ``insert_ref(kind, slug, title, meta)`` → returns a Ref with a
  generated id
- ``fetch_ref(ref_id)`` → returns the stored ref or None
- ``set_meta(ref_id, **fields)`` → merges fields into ref.meta
- ``soft_delete(ref_id)`` → marks ref deleted; queries hide it
- ``insert_blocks(ref_id, blocks)`` → appends chunks
- ``list_blocks_for_ref(ref_id)`` → returns chunks
- ``add_link(src, dst, relation, meta)`` → records the edge
- ``links_for(ref_id, direction='out'|'in', relation=...)`` →
  returns the edges
- ``find_by_meta(kind, key, value)`` → linear scan for dedup

Numeric ids are not used — DFT structures have string ids
(``structure:<sha>``, ``draft:<uuid>``), which is what
``slug`` carries on the real store too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _MockRef:
    """Minimal Ref stand-in matching the precis-mcp shape we use."""

    ref_id: int
    kind: str
    slug: str | None
    title: str
    meta: dict[str, Any]
    deleted: bool = False


@dataclass
class _MockBlock:
    """Minimal chunk stand-in."""

    ref_id: int
    pos: int
    text: str
    chunk_kind: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class _MockLink:
    src_ref_id: int
    dst_ref_id: int
    relation: str
    meta: dict[str, Any] = field(default_factory=dict)


class TestStore:
    """In-memory mock store. Pytest-importable but isn't actually a
    pytest test — the name prefix is to keep IDE auto-imports
    grouped under ``precis_dft._test_store``."""

    # Signal to pytest that this class is not a test collection.
    __test__ = False

    def __init__(self) -> None:
        self._refs: dict[int, _MockRef] = {}
        self._next_ref_id = 1
        self._blocks: list[_MockBlock] = []
        self._links: list[_MockLink] = []
        # Quick (kind, slug) lookup for dedup on content-addressed
        # kinds.
        self._by_slug: dict[tuple[str, str], int] = {}

    # ── Refs ─────────────────────────────────────────────────────

    def insert_ref(
        self,
        *,
        kind: str,
        slug: str | None,
        title: str,
        meta: dict[str, Any],
    ) -> _MockRef:
        if slug is not None and (kind, slug) in self._by_slug:
            return self._refs[self._by_slug[(kind, slug)]]
        ref_id = self._next_ref_id
        self._next_ref_id += 1
        ref = _MockRef(
            ref_id=ref_id,
            kind=kind,
            slug=slug,
            title=title,
            meta=dict(meta),
        )
        self._refs[ref_id] = ref
        if slug is not None:
            self._by_slug[(kind, slug)] = ref_id
        return ref

    def fetch_ref(self, ref_id: int) -> _MockRef | None:
        ref = self._refs.get(ref_id)
        if ref is None or ref.deleted:
            return None
        return ref

    def fetch_ref_by_slug(self, kind: str, slug: str) -> _MockRef | None:
        ref_id = self._by_slug.get((kind, slug))
        if ref_id is None:
            return None
        return self.fetch_ref(ref_id)

    def set_meta(self, ref_id: int, **fields: Any) -> None:
        ref = self._refs.get(ref_id)
        if ref is None or ref.deleted:
            raise KeyError(f"ref {ref_id} not found")
        ref.meta.update(fields)

    def soft_delete(self, ref_id: int) -> None:
        ref = self._refs.get(ref_id)
        if ref is None:
            return
        ref.deleted = True

    def find_refs_by_meta(
        self,
        kind: str,
        key: str,
        value: Any,
        *,
        limit: int | None = None,
    ) -> list[_MockRef]:
        """Linear scan over refs. The real precis-mcp store has an
        SQL implementation; the mock matches its behaviour."""
        out = [
            ref
            for ref in self._refs.values()
            if not ref.deleted and ref.kind == kind and ref.meta.get(key) == value
        ]
        if limit is not None:
            out = out[:limit]
        return out

    # ── Chunks ───────────────────────────────────────────────────

    def insert_blocks(
        self,
        ref_id: int,
        blocks: list[dict[str, Any]],
    ) -> None:
        existing = self.list_blocks_for_ref(ref_id)
        next_pos = max((b.pos for b in existing), default=-1) + 1
        for entry in blocks:
            self._blocks.append(
                _MockBlock(
                    ref_id=ref_id,
                    pos=next_pos,
                    text=entry.get("text", ""),
                    chunk_kind=entry.get("chunk_kind", "body"),
                    meta=dict(entry.get("meta", {})),
                )
            )
            next_pos += 1

    def list_blocks_for_ref(self, ref_id: int) -> list[_MockBlock]:
        return [b for b in self._blocks if b.ref_id == ref_id]

    def delete_blocks_for_ref(
        self, ref_id: int, *, chunk_kind: str | None = None
    ) -> None:
        if chunk_kind is None:
            self._blocks = [b for b in self._blocks if b.ref_id != ref_id]
        else:
            self._blocks = [
                b
                for b in self._blocks
                if not (b.ref_id == ref_id and b.chunk_kind == chunk_kind)
            ]

    # ── Links ────────────────────────────────────────────────────

    def add_link(
        self,
        *,
        src_ref_id: int,
        dst_ref_id: int,
        relation: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._links.append(
            _MockLink(
                src_ref_id=src_ref_id,
                dst_ref_id=dst_ref_id,
                relation=relation,
                meta=dict(meta or {}),
            )
        )

    def links_for(
        self,
        ref_id: int,
        *,
        direction: str = "out",
        relation: str | None = None,
    ) -> list[_MockLink]:
        if direction == "out":
            out = [link for link in self._links if link.src_ref_id == ref_id]
        elif direction == "in":
            out = [link for link in self._links if link.dst_ref_id == ref_id]
        else:
            out = [
                link
                for link in self._links
                if link.src_ref_id == ref_id or link.dst_ref_id == ref_id
            ]
        if relation is not None:
            out = [link for link in out if link.relation == relation]
        return out


__all__ = ["TestStore"]
