"""The turn's working set — eyes + cursor, snapshot per tick (ADR 0051 §6/§15).

A **thread** curates its own context by placing **eyes** on nodes. An eye is
a point on two orthogonal axes (§6):

- **extent** — *how much* to render: an ordinal ladder
  ``none < toc < summary < full < fidelity``.
- **persistence** — *how long* it survives the decay machinery:
  ``transient`` (dies at the next crunch) / ``normal`` (adaptive TTL) /
  ``pinned`` (never decays).

plus a **provenance** — ``requested`` (an explicit ``focus`` or its
structural neighborhood — you asked) vs ``inferred`` (a recency/salience
auto-lens — the system offered). Provenance drives the default persistence
and the ``◦`` render glyph (§6b).

The working set (the eye list + the model-owned ``▸`` cursor) is stored as a
**per-tick JSONB snapshot on the tick's ``job.meta``** (§15): each tick reads
the previous tick's snapshot, applies its curation deltas, and writes a fresh
one into its own ``meta`` — append-only (a new job row per tick), auditable,
replayable, and the store-first reconstruction path when a turn is killed.

**Scope note (phase-B foundation).** This module is the *data model +
serialization* only — the pure, tested substrate both the render-loop (B) and
the fisheye (C) build on. It deliberately does **not** yet implement the decay
ladder (``full —warn→ toc —warn→ gone`` + bunched eviction, §6b) or the
plan_tick loop wiring: those carry the eviction constants and the
``--max-turns 1`` render-loop semantics, and land with the fisheye slice.
Nothing in the live path imports this yet, so it ships dark.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from typing import Any

#: The meta key the per-tick working-set snapshot lives under on a
#: ``kind='job'`` row (§15).
META_KEY = "working_set"

#: Snapshot schema version — bump when the serialized shape changes so an old
#: tick's snapshot is recognized (and can be migrated or dropped) rather than
#: silently mis-read.
SCHEMA_VERSION = 1

#: Default life (in ticks) of a freshly-placed ``normal`` eye before it starts
#: to decay (§6b). A backstop, not a sculptor (§5): the driver right-sizes via
#: ``focus``; this only reclaims *neglected* eyes. Env-tunable; the adaptive
#: shortening-as-context-grows (§5) is a later refinement over this floor.
_DEFAULT_NORMAL_TTL = 4


def _normal_ttl() -> int:
    """The ``normal`` eye TTL, from ``PRECIS_EYE_TTL`` or the default."""
    raw = os.environ.get("PRECIS_EYE_TTL")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_NORMAL_TTL


class Extent(IntEnum):
    """How much of a node to render — the ordinal ``focus`` ladder (§6). An
    ``IntEnum`` so ``a < b`` compares fidelity directly (``NONE`` is least,
    ``HOP1`` most).

    The model-facing vocabulary (via :meth:`parse` / :attr:`label`) is
    ``kwd < summary < verbatim < fisheye < fisheye+1hop`` — each rung strictly
    contains the previous. The enum *identifiers* keep their original names
    (``TOC``/``FULL``/``FIDELITY``) for code stability; the labels are the
    user-facing names. ``fisheye`` is the spatial neighborhood; ``fisheye+1hop``
    adds the **reference ring** — everything the node points at, one edge out
    (cited papers, cross-refs, linked notes/memories — see ``utils.refeye``)."""

    NONE = 0  # a parent TOC line — collapsed
    TOC = 1  # "kwd": one-line bookmark, ancestor path un-collapsed
    SUMMARY = 2  # "summary": node gloss
    FULL = 3  # "verbatim": node full text
    FIDELITY = 4  # "fisheye": verbatim center + spatial neighborhood
    HOP1 = 5  # "fisheye+1hop": fisheye + the reference ring (one edge out)

    @property
    def label(self) -> str:
        """The user-facing name (``kwd``/``verbatim``/``fisheye+1hop``), falling
        back to the lower-cased identifier for ``NONE``."""
        return _EXTENT_LABELS.get(self, self.name.lower())

    @classmethod
    def parse(cls, value: str | int | Extent) -> Extent:
        """Coerce a ``focus`` level to an :class:`Extent`. Accepts the enum,
        an int, an identifier (``'full'``), or the user vocabulary
        (``'verbatim'`` / ``'fisheye'`` / ``'fisheye+1hop'`` / ``'1hop'``).
        Raises ``ValueError`` on an unknown label."""
        if isinstance(value, Extent):
            return value
        if isinstance(value, int):
            return cls(value)
        token = str(value).strip().lower()
        alias = _EXTENT_ALIASES.get(token)
        return cls[alias] if alias is not None else cls[token.upper()]


#: User-facing label per rung — the vocabulary the driver types at ``focus``
#: (kept out of the enum body so it isn't coerced into a member).
_EXTENT_LABELS: dict[Extent, str] = {
    Extent.TOC: "kwd",
    Extent.SUMMARY: "summary",
    Extent.FULL: "verbatim",
    Extent.FIDELITY: "fisheye",
    Extent.HOP1: "fisheye+1hop",
}

#: Accepted aliases (label vocabulary + shorthands) → enum identifier name.
_EXTENT_ALIASES: dict[str, str] = {
    "kwd": "TOC",
    "keywords": "TOC",
    "verbatim": "FULL",
    "fisheye": "FIDELITY",
    "fisheye+1hop": "HOP1",
    "fisheye+hop": "HOP1",
    "1hop": "HOP1",
    "hop1": "HOP1",
    "hop": "HOP1",
}


class Persistence(StrEnum):
    """How long an eye survives the decay machinery (§6)."""

    TRANSIENT = "transient"  # ttl=1, dies at the next crunch (auto-lens default)
    NORMAL = "normal"  # adaptive TTL (explicit-request default)
    PINNED = "pinned"  # never decays (the cursor, the last-5 ledger)


class Provenance(StrEnum):
    """Who placed the eye — drives the default persistence + the ``◦`` glyph
    (§6b). ``REQUESTED`` = an explicit ``focus`` or its structural
    neighborhood; ``INFERRED`` = a recency/salience auto-lens."""

    REQUESTED = "requested"
    INFERRED = "inferred"


#: Persistence a newly-placed eye derives from its provenance (§6): an
#: explicitly-requested eye is ``normal``; an inferred auto-lens is
#: ``transient`` (a fading suggestion). Re-``focus`` promotes an inferred eye
#: to ``requested``/``normal`` (adoption).
_DEFAULT_PERSISTENCE: dict[Provenance, Persistence] = {
    Provenance.REQUESTED: Persistence.NORMAL,
    Provenance.INFERRED: Persistence.TRANSIENT,
}


def default_persistence(provenance: Provenance) -> Persistence:
    """The persistence an eye of this provenance is born with (§6)."""
    return _DEFAULT_PERSISTENCE[provenance]


def ttl_for(persistence: Persistence) -> int | None:
    """The starting TTL (ticks) for a fresh eye of this persistence (§6b):
    ``transient`` dies at the next crunch (``1``); ``normal`` gets the
    adaptive floor; ``pinned`` never decays (``None`` = infinite)."""
    if persistence is Persistence.PINNED:
        return None
    if persistence is Persistence.TRANSIENT:
        return 1
    return _normal_ttl()


#: The decay ladder (§6b: ``fisheye+1hop —peel→ fisheye —warn→ kwd —warn→
#: gone``). The most expensive layer sheds first: a neglected ``fisheye+1hop``
#: eye drops its reference *ring* (→ ``fisheye``) before it drops its spatial
#: neighborhood, so decay peels cost in reverse of how it was added. Below the
#: ring, any eye richer than ``kwd`` collapses straight to ``kwd`` (one warn
#: already shown); an expiring ``kwd`` (or ``none``) is dropped (``None``).
def _decay_to(extent: Extent) -> Extent | None:
    if extent >= Extent.HOP1:
        return Extent.FIDELITY  # shed the reference ring, keep the neighborhood
    return Extent.TOC if extent > Extent.TOC else None


@dataclass(frozen=True, slots=True)
class Eye:
    """One eye — a node handle at an ``extent``, with a ``persistence`` and a
    ``provenance``. ``ttl`` is the remaining life in ticks for a ``normal``
    eye (``None`` for ``pinned`` = infinite, and unused-but-1 for
    ``transient``); the decay machinery (phase C) reads it — this module only
    carries it."""

    handle: str
    extent: Extent = Extent.FULL
    persistence: Persistence = Persistence.NORMAL
    provenance: Provenance = Provenance.REQUESTED
    ttl: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "extent": int(self.extent),
            "persistence": str(self.persistence),
            "provenance": str(self.provenance),
            "ttl": self.ttl,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Eye:
        return cls(
            handle=str(raw["handle"]),
            extent=Extent.parse(raw.get("extent", Extent.FULL)),
            persistence=Persistence(raw.get("persistence", Persistence.NORMAL)),
            provenance=Provenance(raw.get("provenance", Provenance.REQUESTED)),
            ttl=raw.get("ttl"),
        )


@dataclass
class WorkingSet:
    """The tick's curated context: the eye list + the model-owned ``▸``
    cursor. Serialized to / from a ``job.meta`` snapshot (§15).

    Eyes are keyed by ``handle`` (one eye per node); placing an eye on a
    handle that already has one **replaces** it (a re-``focus`` is the
    refresh/adopt action, §6). The cursor is a plan-node handle (``pe<id>``)
    or ``None``."""

    eyes: dict[str, Eye] = field(default_factory=dict)
    cursor: str | None = None

    # ── mutators (deltas the curate phase applies) ───────────────────

    def focus(
        self,
        handle: str,
        extent: Extent | str | int = Extent.FULL,
        *,
        provenance: Provenance = Provenance.REQUESTED,
        ttl: int | None = None,
    ) -> None:
        """Place (or replace) an eye on ``handle`` at ``extent``. ``NONE``
        clears the eye (a ``focus(h, none)`` receipt, §6c). Persistence is
        **derived** from provenance unless the eye is pinned elsewhere; ``ttl``
        overrides the default life."""
        ext = Extent.parse(extent)
        if ext is Extent.NONE:
            self.eyes.pop(handle, None)
            return
        persistence = default_persistence(provenance)
        self.eyes[handle] = Eye(
            handle=handle,
            extent=ext,
            persistence=persistence,
            provenance=provenance,
            ttl=ttl if ttl is not None else ttl_for(persistence),
        )

    def pin(self, handle: str, extent: Extent | str | int = Extent.FULL) -> None:
        """Place a ``pinned`` eye (never decays) — the cursor's fidelity eye
        and the ledger floor (§7)."""
        ext = Extent.parse(extent)
        self.eyes[handle] = Eye(
            handle=handle,
            extent=ext,
            persistence=Persistence.PINNED,
            provenance=Provenance.REQUESTED,
            ttl=None,
        )

    def close(self, handle: str) -> None:
        """Drop the eye on ``handle`` (``focus(h, none)``)."""
        self.eyes.pop(handle, None)

    def set_cursor(self, handle: str | None) -> None:
        """Move the model-owned ``▸`` cursor (§2b). ``None``/``''`` clears."""
        self.cursor = handle or None

    def get(self, handle: str) -> Eye | None:
        return self.eyes.get(handle)

    # ── decay ladder + bunched eviction (§6b) ────────────────────────

    def age(self, ticks: int = 1) -> None:
        """Decrement every non-pinned eye's TTL by ``ticks`` (floor 0). TTL is
        metadata, not a sort key (§6b): ageing does **not** reorder or drop —
        it only ripens eyes toward the next :meth:`crunch`. Pinned eyes (the
        cursor, the ledger floor) are exempt."""
        for handle, eye in list(self.eyes.items()):
            if eye.persistence is Persistence.PINNED or eye.ttl is None:
                continue
            self.eyes[handle] = replace(eye, ttl=max(0, eye.ttl - ticks))

    def expiring(self) -> list[str]:
        """Handles whose eye will demote/drop at the next crunch — the
        ``†`` tombstone / 'about to expire' list rendered in the tail (§6b) so
        the model can rescue by re-``focus``. A ripe eye has ``ttl <= 0``."""
        return [
            h
            for h, e in self.eyes.items()
            if e.persistence is not Persistence.PINNED
            and e.ttl is not None
            and e.ttl <= 0
        ]

    def crunch(self) -> tuple[list[str], list[str]]:
        """Apply the **bunched eviction** (§6b): in one batch, every ripe eye
        (``ttl <= 0``) demotes one rung on the decay ladder (rich → ``toc``,
        refreshing its TTL) or, if already ``toc``/``none``, is dropped.
        Pinned eyes never decay. Returns ``(demoted, dropped)`` handles for the
        render. Batching to a single call is what keeps eviction a *cache-break
        event*, not a per-item churn."""
        demoted: list[str] = []
        dropped: list[str] = []
        for handle in self.expiring():
            eye = self.eyes[handle]
            # A transient eye (an inferred auto-lens / search candidate) is a
            # fading suggestion — it *dies* at the crunch, it does not ride the
            # demote ladder (§6b). Only a neglected *normal* eye demotes.
            nxt = (
                None
                if eye.persistence is Persistence.TRANSIENT
                else _decay_to(eye.extent)
            )
            if nxt is None:
                del self.eyes[handle]
                dropped.append(handle)
            else:
                self.eyes[handle] = replace(
                    eye, extent=nxt, ttl=ttl_for(eye.persistence)
                )
                demoted.append(handle)
        return demoted, dropped

    # ── serialization (the §15 snapshot) ─────────────────────────────

    def to_meta_patch(self) -> dict[str, Any]:
        """A ``meta``-merge patch stamping this snapshot under
        :data:`META_KEY` — the value to shallow-merge into the tick's
        ``job.meta`` (§15)."""
        return {
            META_KEY: {
                "version": SCHEMA_VERSION,
                "eyes": [e.to_json() for e in self.eyes.values()],
                "cursor": self.cursor,
            }
        }

    @classmethod
    def from_meta(cls, meta: dict[str, Any] | None) -> WorkingSet:
        """Reconstruct the working set from a tick's ``job.meta`` (§15). A
        missing / unversioned / future-versioned snapshot yields an **empty**
        set rather than raising — a killed or pre-loop tick degrades to a cold
        start, never a crash."""
        snap = (meta or {}).get(META_KEY)
        if not isinstance(snap, dict) or snap.get("version") != SCHEMA_VERSION:
            return cls()
        eyes: dict[str, Eye] = {}
        for raw in snap.get("eyes") or []:
            try:
                eye = Eye.from_json(raw)
            except (KeyError, ValueError):
                continue  # skip a corrupt entry, keep the rest
            eyes[eye.handle] = eye
        cursor = snap.get("cursor")
        return cls(eyes=eyes, cursor=str(cursor) if cursor else None)

    def copy(self) -> WorkingSet:
        """A deep-enough copy for the fork-with-diff of a Call/Spawn child
        (§9): eyes are frozen, so a fresh dict suffices."""
        return WorkingSet(eyes=dict(self.eyes), cursor=self.cursor)


def adopt(eye: Eye) -> Eye:
    """Promote an inferred (auto-lens) eye to a requested one with a refreshed
    persistence — the re-``focus`` adoption action (§6). Extent is unchanged;
    only provenance/persistence lift."""
    return replace(
        eye,
        provenance=Provenance.REQUESTED,
        persistence=Persistence.NORMAL,
    )
