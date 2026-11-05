"""Quest logbook vocabulary + the shared append path.

The logbook is the quest's append-only, WORM, dated ledger — ``quest_log``
chunks off the quest ref (migration 0065, the ``gripe`` body+comment
pattern). Entries carry ``entry_type``+``by`` (+ optional ``cost``/
``chars``) in ``chunk.meta``. A ``milestone`` is a deed; a ``cost`` entry
feeds the tote, metered in ``chars`` (gr162594: ``cost_usd`` is null on the
free/quota-bound quest-tick lane).

:class:`~precis.handlers.quest.QuestHandler` and the autonomous
``quest_tick`` both write through :func:`append_entry` — one insert path,
one vocabulary.

New entries cite the live structures they name from their chunk endpoint.
``cites`` records a reference, not an evidential claim. Link failures are
isolated from the append and counted in the receipt; old entries are never
scanned or rewritten. The integer entry-number contract stays intact for
worker callers; the handler also requests a :class:`StructureLinkReceipt`.

Each citation pins the stored log chunk and quotes its literal handle. This
is evidence of the reference, not support for the entry's scientific claims.
The machine-edge annotation preserves the caller's model/run; its version
names this deterministic extraction rule, which has no separate LLM prompt.
Superseded handles follow the shared resolver to a live structure, with one
edge per resolved target even when an entry names both old and new handles.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.store.revision_context import current_revision_context
from precis.store.types import ChunkInsert

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)
_STRUCTURE_HANDLE = re.compile(r"\bst([0-9]+)\b")


@dataclass
class StructureLinkReceipt:
    """Counts of distinct named structures, including unresolved mentions."""

    linked: int = 0
    skipped: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"structure links: {self.linked} linked, {self.skipped} skipped "
            f"(unresolved), {self.failed} failed"
        )


def link_entry_structures(
    store: Store, quest_id: int, pos: int, text: str
) -> StructureLinkReceipt:
    """Idempotently cite live ``st<N>`` mentions from one existing log chunk.

    Each attempt owns a transaction/savepoint, including the lookup: even a
    database error must not abort an enclosing handler append transaction.
    """
    receipt = StructureLinkReceipt()
    mentions: dict[str, str] = {}
    for match in _STRUCTURE_HANDLE.finditer(text):
        mentions.setdefault(match[1].lstrip("0") or "0", match[0])
    linked_ids: set[int] = set()
    context = current_revision_context()
    for digits in sorted(mentions, key=lambda value: (len(value), value)):
        # A ref id is a positive signed bigint. Bound before int() as Python
        # also rejects extremely long decimal strings; neither failure may
        # cost the already-appended entry.
        if len(digits) > 19 or not 0 < int(digits) < 2**63:
            receipt.skipped += 1
            continue
        sid = int(digits)
        try:
            with store.tx() as conn:
                resolved = store.resolve_handle(f"st{sid}", conn=conn)
                if (
                    resolved is None
                    or resolved.kind != "structure"
                    or resolved.chunk_id is not None
                ):
                    receipt.skipped += 1
                    continue
                if resolved.ref_id in linked_ids:
                    continue
                chunks = store.chunks.list_chunks_for_ref(
                    quest_id, pos_range=(pos, pos), conn=conn
                )
                quote = mentions[digits]
                if not chunks or quote not in chunks[0].text:
                    raise ValueError("the log chunk must contain the cited handle")
                evidence = f"ql{chunks[0].id}"
                store.add_link(
                    src_ref_id=quest_id,
                    src_pos=pos,
                    dst_ref_id=resolved.ref_id,
                    relation="cites",
                    set_by="system",
                    meta={
                        "evidence": [evidence],
                        "quote": quote,
                        "model": (context.model if context else None)
                        or "quest-log-structure-extractor",
                        "prompt_version": "quest-log-structures-v1",
                        "run_id": (context.trust_run if context else None) or evidence,
                        "why": (
                            f"The log entry names {quote}, which resolves to "
                            f"st{resolved.ref_id}; this records its citation."
                        ),
                    },
                    conn=conn,
                )
            linked_ids.add(resolved.ref_id)
            receipt.linked += 1
        except Exception:
            receipt.failed += 1
            log.warning(
                "quest log qu%s~%s could not cite st%s",
                quest_id,
                pos,
                sid,
                exc_info=True,
            )
    return receipt


#: The append-only logbook chunk_kind (seeded by migration 0065).
LOG_KIND = "quest_log"

#: Lightly-typed logbook entry vocabulary (``quest-layer`` (git-only)). A
#: ``milestone`` is a deed; a ``cost`` entry (or any entry with ``meta.cost``)
#: feeds the tote; a ``dead-end`` records what failed so the system stops
#: re-treading it; an un-answered ``hypothesis`` is a gap (slice 3).
ENTRY_TYPES: frozenset[str] = frozenset(
    {
        "note",
        "observation",
        "hypothesis",
        "result",
        "decision",
        "dead-end",
        "milestone",
        "reflection",
        "cost",
    }
)
DEFAULT_ENTRY = "note"

#: Who authored a logbook entry.
BY_VALUES: frozenset[str] = frozenset({"human", "agent", "dream", "system"})
DEFAULT_BY = "human"

#: ``by`` stamp for a system-measured fact (a converged relax, a harvested
#: autocatpath barrier, a ruled-out verdict) — never the model's own "agent"
#: attribution, so a measured result can't read as indistinguishable from
#: model narration (gr171148/171149).
MEASURED_BY = "system"


def append_entry(
    store: Store,
    quest_id: int,
    *,
    text: str,
    entry_type: str,
    by: str,
    cost: float | None = None,
    chars: int | None = None,
    extra_meta: dict[str, Any] | None = None,
    link_receipt: StructureLinkReceipt | None = None,
) -> int:
    """Append one logbook entry; return its 1-based entry number.

    Validating ``entry_type``/``by`` against :data:`ENTRY_TYPES`/
    :data:`BY_VALUES` is the caller's job (the handler does, to surface a
    typo); this function is permissive — stamps whatever it's given, so the
    autonomous tick can clamp-and-proceed rather than raise. ``extra_meta``
    merges additional structured facts onto the entry's ``meta`` (e.g. the
    narrative growth-ratchet gate's word counts + reason) so they're
    minable later without parsing ``text`` — never overwrites the keys this
    function itself stamps (``chunk_kind``/``entry_type``/``by``/``cost``/
    ``chars``).

    The returned number is 1-based for human reading; the entry's linkable
    block handle is ``qu<quest_id>~<entry_no - 1>`` — ``~N`` selects a chunk
    by its 0-based ``ord`` (gr511468: an ack that printed the bare number
    produced off-by-one link targets).

    ``link_receipt`` receives distinct-structure counts for the caller's
    acknowledgement or tick outcome. Callers retain the integer return and
    get the same counts in the logger whenever the entry names a structure.
    """
    entry_meta: dict[str, Any] = {
        "chunk_kind": LOG_KIND,
        "entry_type": entry_type,
        "by": by,
    }
    if cost is not None:
        entry_meta["cost"] = float(cost)
    if chars is not None:
        entry_meta["chars"] = int(chars)
    if extra_meta:
        for k, v in extra_meta.items():
            entry_meta.setdefault(k, v)
    # Next pos = current chunk count. list_chunks_for_ref excludes the synthetic
    # card (ord=-1), so the first logbook entry lands at pos=0.
    next_pos = len(store.chunks.list_chunks_for_ref(quest_id))
    with store.tx() as conn:
        store.chunks.insert_chunks(
            quest_id,
            [ChunkInsert(ord=next_pos, text=text, meta=entry_meta)],
            conn=conn,
        )
    receipt = link_entry_structures(store, quest_id, next_pos, text)
    if link_receipt is not None:
        link_receipt.linked = receipt.linked
        link_receipt.skipped = receipt.skipped
        link_receipt.failed = receipt.failed
    if receipt.linked or receipt.skipped or receipt.failed:
        log.info("quest log qu%s~%s: %s", quest_id, next_pos, receipt.summary())
    return next_pos + 1


def clamp_entry_type(value: str | None) -> str:
    """Coerce an arbitrary (e.g. model-authored) entry type into the vocab."""
    v = (value or DEFAULT_ENTRY).strip().lower()
    return v if v in ENTRY_TYPES else DEFAULT_ENTRY


__all__ = [
    "BY_VALUES",
    "DEFAULT_BY",
    "DEFAULT_ENTRY",
    "ENTRY_TYPES",
    "LOG_KIND",
    "MEASURED_BY",
    "StructureLinkReceipt",
    "append_entry",
    "clamp_entry_type",
    "link_entry_structures",
]
