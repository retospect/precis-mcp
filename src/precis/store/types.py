"""Frozen row types and aliases for the store layer.

Mapping from `asyncpg.Record` to these types lives in the per-domain
modules (`refs.py`, `blocks.py`, ...). The types are deliberately
immutable so they can flow through the runtime without anyone mutating
them in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Type aliases — closed vocabularies mirrored from the schema
# ---------------------------------------------------------------------------

Density = Literal["sparse", "medium", "dense"]
CacheFreshness = Literal["pinned", "fresh", "stale", "expired"]
Namespace = Literal["closed", "flag", "open"]
Relation = Literal[
    "related-to",
    "blocks",
    "blocked-by",
    "contradicts",
    "contradicted-by",
]
ActorSlug = Literal["agent", "user", "system"]


# ---------------------------------------------------------------------------
# Row types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ref:
    """A ref row from the `refs` table."""

    id: int
    corpus_id: int
    kind: str  # FK to kinds.slug
    slug: str | None  # NULL for numeric kinds
    title: str
    provider: str | None  # FK to providers.slug
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @property
    def public_id(self) -> str:
        """Agent-facing identifier: slug for slug kinds, str(id) for numeric."""
        return self.slug if self.slug is not None else str(self.id)


@dataclass(frozen=True, slots=True)
class Block:
    """A block (chunk) row from the `blocks` table."""

    id: int
    ref_id: int
    pos: int  # 0-based, renumberable
    slug: str | None  # stable citation handle
    text: str
    token_count: int | None
    embedding: list[float] | None  # populated only when fetched explicitly
    density: Density | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Link:
    """A link row from the `links` table."""

    id: int
    src_ref_id: int
    src_pos: int | None
    dst_ref_id: int
    dst_pos: int | None
    relation: Relation
    set_by: ActorSlug
    meta: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Tag:
    """Unified tag representation across the three namespace tables."""

    namespace: Namespace
    prefix: str | None  # closed only; None for flag/open
    value: str  # closed value, flag name, or open value

    @classmethod
    def closed(cls, prefix: str, value: str) -> Tag:
        return cls(namespace="closed", prefix=prefix, value=value)

    @classmethod
    def flag(cls, name: str) -> Tag:
        return cls(namespace="flag", prefix=None, value=name)

    @classmethod
    def open(cls, value: str) -> Tag:
        return cls(namespace="open", prefix=None, value=value.lower())

    @classmethod
    def parse(cls, s: str, *, known_flags: frozenset[str] | None = None) -> Tag:
        """Parse 'STATUS:done' / 'pinned' / 'nitrate-reduction' into a Tag.

        Disambiguation:
          * contains ':' AND prefix is all-uppercase  -> closed
          * matches a known flag name                 -> flag
          * else                                      -> open (lowercased)
        """
        if ":" in s:
            prefix, _, value = s.partition(":")
            if prefix and prefix.isupper():
                return cls.closed(prefix, value)
        if known_flags and s in known_flags:
            return cls.flag(s)
        return cls.open(s)

    def __str__(self) -> str:
        if self.namespace == "closed":
            return f"{self.prefix}:{self.value}"
        return self.value


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A cache_state row."""

    ref_id: int
    provider: str
    request_hash: str
    model: str | None
    fetched_at: datetime
    fresh_until: datetime | None  # NULL = pinned
    cost_usd: float | None
    meta: dict[str, Any]


# ---------------------------------------------------------------------------
# Insert payload types (mutable; not stored)
# ---------------------------------------------------------------------------


@dataclass
class BlockInsert:
    """Payload for inserting a block. Mutable on purpose — callers build
    these incrementally during ingestion."""

    pos: int
    text: str
    slug: str | None = None
    token_count: int | None = None
    embedding: list[float] | None = None
    density: Density | None = None
    meta: dict[str, Any] = field(default_factory=dict)
