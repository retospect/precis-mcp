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

from precis.errors import BadInput

# ---------------------------------------------------------------------------
# Type aliases — closed vocabularies mirrored from the schema
# ---------------------------------------------------------------------------

Density = Literal["sparse", "medium", "dense"]
CacheFreshness = Literal["pinned", "fresh", "stale", "expired"]
Namespace = Literal["closed", "flag", "open"]
Relation = Literal[
    # Initial migration (0001).
    "related-to",
    "blocks",
    "blocked-by",
    "contradicts",
    "contradicted-by",
    # Phase 7 link CRUD migration (0005). Keep this list in sync
    # with `migrations/0005_link_relations.sql` so type-checkers
    # catch typos in `rel=` kwargs ahead of the FK violation.
    "cites",
    "cited-by",
    "derived-from",
    "derived-into",
    "supports",
    "supported-by",
    "generalises",
    "specialises",
    "see-also",
]
ActorSlug = Literal["agent", "user", "system"]


# Inverse relations for auto-mirroring at link write time. Mirrored
# from ``migrations/0001_initial.sql`` and ``0005_link_relations.sql``
# (the ``relations.inverse_slug`` column). Symmetric relations
# (``related-to``) are NOT in this map — the bidirectional query in
# :meth:`Store.links_for` (direction='both') already surfaces them
# from either side, and inserting both directions would just produce
# duplicate rows the renderer would have to dedupe.
#
# Asymmetric relations *with* a documented inverse get auto-mirrored
# at write time so that ``links_for(B, relation='cited-by',
# direction='out')`` returns the right rows without the agent having
# to remember to also pass ``direction='in'``. The MCP critic
# flagged the missing inverse rows as the cause of "who cites me?"
# filters returning empty.
#
# ``see-also`` is asymmetric *and* has no inverse (NULL in the
# schema): it's a one-way pointer "for context" with no reverse
# semantic. Not in this map.
_INVERSE_RELATIONS: dict[str, str] = {
    "blocks": "blocked-by",
    "blocked-by": "blocks",
    "contradicts": "contradicted-by",
    "contradicted-by": "contradicts",
    "cites": "cited-by",
    "cited-by": "cites",
    "derived-from": "derived-into",
    "derived-into": "derived-from",
    "supports": "supported-by",
    "supported-by": "supports",
    "generalises": "specialises",
    "specialises": "generalises",
}


# ---------------------------------------------------------------------------
# Row types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ref:
    """A ref row from the v2 ``refs`` table.

    ``id`` maps to the v2 ``ref_id`` column (the rename happened in
    ``migrations/0001_initial.sql``). ``slug`` is populated by a
    correlated subquery against ``ref_identifiers`` with
    ``id_kind='cite_key'`` — the convention every slug-addressed kind
    uses in v2 per ADR 0008. Numeric kinds (memory/todo/gripe/fc)
    have no ``ref_identifiers`` row so ``slug`` is ``None``.
    """

    id: int
    kind: str  # FK to kinds.slug
    slug: str | None  # populated from ref_identifiers id_kind='cite_key'
    title: str
    provider: str | None  # FK to providers.slug
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    # v2-new fields. All optional with sensible defaults so existing
    # call sites that don't know about them (everything but the v2
    # ingest path) continue to work unchanged.
    set_by: str | None = None  # FK to actors.slug
    authors: list[dict[str, Any]] | None = None
    year: int | None = None
    human_verified_at: datetime | None = None
    human_verified_by: str | None = None
    human_verified_note: str | None = None
    retraction_status: str | None = None
    retracted_at: datetime | None = None
    retraction_reason: str | None = None
    retraction_url: str | None = None
    retraction_checked_at: datetime | None = None
    pdf_sha256: str | None = None
    pdf_pages: str | None = None  # PG int4range as text
    pdf_role: str | None = None

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

        Permissive: accepts unknown closed values and any bare flag.
        Use :meth:`parse_strict` to reject inputs that violate the
        documented vocabulary discipline.
        """
        if ":" in s:
            prefix, _, value = s.partition(":")
            if prefix and prefix.isupper():
                return cls.closed(prefix, value)
        if known_flags and s in known_flags:
            return cls.flag(s)
        return cls.open(s)

    @classmethod
    def parse_strict(cls, s: str, *, kind: str | None = None) -> Tag:
        """Parse + validate against the documented tag vocabulary.

        Raises ``BadInput`` for:
          * unknown values inside a registered closed prefix
            (e.g. ``STATUS:bogus`` when STATUS is restricted to
            ``open|doing|blocked|done|won't-do``)
          * bare flags that collide with a closed-vocab value
            (e.g. ``'urgent'`` must be written as ``'PRIO:urgent'``)
          * closed-axis tags on kinds that don't use that axis
            (e.g. ``STATUS:`` on a ``memory`` — memories have no
            workflow state; see ``_KIND_ALLOWED_AXES``)

        If ``kind`` is provided and the kind has a restricted axis
        set, closed-prefix tags outside that set are rejected with
        an error that names the kind's allowed axes. Passing
        ``kind=None`` keeps the previous global-vocabulary behaviour
        for callers that don't know their kind at validation time
        (filter queries, migrations).

        The MCP critic flagged that the runtime previously accepted
        both shapes silently, leaving non-canonical tags that no
        filter query could find.
        """
        if not isinstance(s, str) or not s.strip():
            raise BadInput(
                f"invalid tag: {s!r}",
                next="tags must be non-empty strings (e.g. 'STATUS:done')",
            )

        if ":" in s:
            prefix, _, value = s.partition(":")
            if prefix and prefix.isupper():
                allowed = _CLOSED_VOCAB.get(prefix)
                # The MCP critic (Apr 2026) flagged that ``DENSITY:sparse``
                # and ``CONFIDENCE:moderate`` were accepted at runtime
                # despite ``precis-tags`` documenting them as rejected.
                # Cause: this branch only validated values inside
                # *registered* prefixes; an unregistered uppercase
                # prefix passed straight through. Tighten: any
                # uppercase prefix that isn't in ``_CLOSED_VOCAB`` is
                # an unknown axis, not an open tag — reject with the
                # full registered list as the recovery hint. This
                # also catches typos like ``STATSU:open`` that would
                # otherwise survive into queries silently.
                if allowed is None:
                    # Canonical-form hint matters: ``precis-tags``
                    # documents lowercase open tags as
                    # ``prefix:value`` (e.g. ``topic:co2-capture``),
                    # not ``prefix-value``. The MCP critic flagged
                    # the previous hint (``density-'bogus'``) as
                    # contradicting the docs and producing parse
                    # errors when copy-pasted. Spell the canonical
                    # ``lowercase:value`` form. (Critic MINOR #8.)
                    raise BadInput(
                        f"unknown closed-prefix axis: {prefix!r}:",
                        options=sorted(_CLOSED_VOCAB.keys()),
                        next=(
                            f"either use a registered axis "
                            f"({sorted(_CLOSED_VOCAB.keys())}) or write "
                            f"this as a lowercase open tag "
                            f"(e.g. tags=['{prefix.lower()}:{value}'])"
                        ),
                    )
                if value not in allowed:
                    raise BadInput(
                        f"invalid {prefix} value: {value!r}",
                        options=sorted(allowed),
                        next=(f"{prefix}: must be one of {sorted(allowed)}"),
                    )
                # Per-kind axis enforcement. The MCP critic noted
                # that ``STATUS:open`` on a ``memory`` is a smell —
                # memories have no workflow state, so the tag is
                # decorative at best and misleading at worst (a
                # filter query for open todos shouldn't return
                # memory rows). When ``kind=`` is provided and the
                # kind is in ``_KIND_ALLOWED_AXES``, we require the
                # closed prefix to be in that kind's allowed axis
                # set. Kinds not in the map are unrestricted
                # (backwards-compatible).
                if kind is not None:
                    kind_allowed = _KIND_ALLOWED_AXES.get(kind)
                    if kind_allowed is not None and prefix not in kind_allowed:
                        raise BadInput(
                            f"{prefix!r}: axis not allowed on kind {kind!r}",
                            options=sorted(kind_allowed),
                            next=(
                                f"kind={kind!r} accepts closed axes "
                                f"{sorted(kind_allowed)} - for {prefix.lower()} "
                                f"semantics write this as a lowercase open tag "
                                f"(e.g. tags=['{prefix.lower()}:{value}'])"
                            ),
                        )
                return cls.closed(prefix, value)

        # Bare-flag form. Reject if it collides with a registered
        # closed-vocab value — agents that wrote `'urgent'` instead of
        # `'PRIO:urgent'` would otherwise produce an open-tag row that
        # never matches `tags=['PRIO:urgent']` filter queries.
        canonical = _RESERVED_FLAGS.get(s)
        if canonical is not None:
            raise BadInput(
                f"bare flag {s!r} collides with closed value {canonical!r}",
                next=f"use tags=[{canonical!r}] instead of tags=[{s!r}]",
            )
        return cls.parse(s)

    @classmethod
    def normalize_filter(
        cls, tags: list[str] | None, *, kind: str | None = None
    ) -> list[str] | None:
        """Validate and canonicalise a tag-filter list at the agent boundary.

        Used by handler ``search`` (and any future ``list``) methods
        that accept a ``tags=`` kwarg. Each tag is run through
        :meth:`parse_strict` (so bad input raises the same
        ``BadInput`` shape as ``put``), then converted back to its
        canonical string form via ``__str__`` for the SQL layer.

        ``kind=`` is forwarded to ``parse_strict`` for per-kind axis
        enforcement. Callers that filter across kinds should pass
        ``kind=None`` to keep the global vocabulary.

        Returns ``None`` for ``None`` or empty input so callers can
        forward to the store's ``tags=`` kwarg unchanged — the
        downstream :func:`build_tag_filter` treats ``None``/``[]`` as
        a no-op.
        """
        if not tags:
            return None
        return [str(cls.parse_strict(t, kind=kind)) for t in tags]

    def __str__(self) -> str:
        if self.namespace == "closed":
            return f"{self.prefix}:{self.value}"
        return self.value


# ---------------------------------------------------------------------------
# Closed-prefix tag vocabularies
# ---------------------------------------------------------------------------
# Each registered prefix carries a closed set of allowed values. The
# runtime rejects unknown values via :meth:`Tag.parse_strict`. To add a
# new prefix, append here AND update the docs (`precis-tags`,
# `precis-todo-help`, etc.).
#
# STATUS values match TodoHandler's documented lifecycle. Everything
# else is provisional — best-effort coverage of vocabulary actually used
# in the codebase + skill docs.

_CLOSED_VOCAB: dict[str, frozenset[str]] = {
    "STATUS": frozenset({"open", "doing", "blocked", "done", "won't-do"}),
    "PRIO": frozenset({"low", "normal", "high", "urgent"}),
    "SRC": frozenset({"primary", "secondary"}),
    "CACHE": frozenset({"fresh", "stale", "pinned"}),
    # ``WATCH:<interval>`` marks cache-backed refs that should be
    # auto-refreshed by the nightly maintenance driver. The cron
    # scans ``search(tags=['WATCH:daily'])`` and re-fetches each
    # match via ``get(..., mode='refresh')``. Closed vocabulary so
    # a typo (``WATCH:dialy``) fails loud at write time instead of
    # silently dropping the row from the sweep. (gripe:3681 phase 4.)
    "WATCH": frozenset({"hourly", "daily", "weekly", "monthly"}),
}

# Bare flag values that collide with a closed-vocab value. Maintained as
# a derived map so adding to ``_CLOSED_VOCAB`` automatically updates the
# rejection set.
_RESERVED_FLAGS: dict[str, str] = {
    v: f"{prefix}:{v}" for prefix, vals in _CLOSED_VOCAB.items() for v in vals
}

# Per-kind closed-axis whitelist. Kinds absent from this map accept
# every registered axis (backwards-compatible with existing corpora).
# Kinds present here are restricted to the listed axes — anything
# outside raises ``BadInput`` via :meth:`Tag.parse_strict`.
#
# The MCP critic flagged ``STATUS:open`` on a memory as a smell: the
# memory kind has no workflow state, so the tag is decorative and a
# filter query (``search(kind='todo', tags=['STATUS:open'])``) will
# never see it. Restricting axes per-kind catches the smell at the
# write boundary.
#
# The vocabulary here is conservative: every kind that genuinely uses
# a closed axis lists it; every kind that doesn't use any is omitted
# from the map (no restriction). See docs/precis-v2-skills/ for the
# narrative discipline each kind follows.
_KIND_ALLOWED_AXES: dict[str, frozenset[str]] = {
    # Workflow kinds — STATUS + priority.
    "todo": frozenset({"STATUS", "PRIO"}),
    "gripe": frozenset({"STATUS", "PRIO"}),
    "quest": frozenset({"STATUS", "PRIO"}),
    # Free-form notes: no closed axes. Confidence, topic, project,
    # etc. are open tags (``confidence-strong``, ``topic-noxrr``).
    "memory": frozenset(),
    # Flashcard doesn't use STATUS (review state lives elsewhere —
    # EASE/DUE on blocks in a future phase), nor PRIO.
    "fc": frozenset(),
    # Conversation refs don't carry closed axes — conversations
    # aren't workflow objects. Any status belongs on the associated
    # todo/quest.
    "conv": frozenset(),
    # Paper refs use SRC (primary vs secondary lit) and CACHE (pinned
    # vs re-ingested). STATUS doesn't apply — papers don't have a
    # workflow state.
    "paper": frozenset({"SRC", "CACHE"}),
    # Cache-backed kinds use CACHE (freshness state) and WATCH
    # (refresh interval for the nightly maintenance driver). The
    # MCP critic gripe:3681 phase 4 motivated WATCH — a closed-vocab
    # interval tag lets the cron sweep enumerate
    # ``search(tags=['WATCH:daily'])`` and refresh each match
    # without per-kind plumbing. ``math`` is intentionally left off
    # the WATCH axis: Wolfram results are deterministic and don't
    # drift, so refreshing them wastes API budget.
    "research": frozenset({"CACHE", "WATCH"}),
    "think": frozenset({"CACHE", "WATCH"}),
    "websearch": frozenset({"CACHE", "WATCH"}),
    "web": frozenset({"CACHE", "WATCH"}),
    "youtube": frozenset({"CACHE", "WATCH"}),
    # Oracle refs (curated prompts/rubrics) are read-only references
    # with no workflow state.
    "oracle": frozenset(),
    # Skill refs ditto.
    "skill": frozenset(),
    # Patent refs use SRC (e.g. SRC:primary for the patent we ingested
    # direct, SRC:secondary for refs found via family-walk) and CACHE
    # (cluster-wide cache discipline). STATUS doesn't apply — patents
    # don't have a workflow lifecycle; ingestion-pending bookkeeping
    # would live on an associated `quest` row.
    "patent": frozenset({"SRC", "CACHE"}),
}


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
