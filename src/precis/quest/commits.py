"""Quest ``commits`` — a tick's channel for non-materials progress (gr573805).

An operational quest (a compute scout hunting free tiers and credits, say)
has deliverables with no atomistic form: an ask queued for Reto, an account
row created disabled, a lead ruled out with its reason. Without a channel
the tick had to fabricate a slab/structure ``proposal`` to register motion,
and those kept being rejected as mismatched.

A tick payload may therefore carry an optional ``commits`` list of
``{"kind", "text", "ref"?}`` entries. ``kind`` is one of :data:`COMMIT_KINDS`:

* ``ask`` — a question or request queued for a human;
* ``account`` — an account/credential row created (disabled) for a lead;
* ``lead-ruled-out`` — a lead dropped, ``text`` carrying the reason.

The executor (:func:`apply_commits`) records each as a logbook entry stamped
``meta.commit_kind`` — an ``ask``/``account`` is a ``decision``, a
``lead-ruled-out`` a ``dead-end`` — so it is minable and counts as forward
motion. Malformed entries are dropped and counted in one observation, never
a tick failure. The model never gets ``milestone``/``result`` this way:
those stay system-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.quest import logbook

if TYPE_CHECKING:
    from precis.store.store import Store

#: Accepted ``commits`` entries per tick (WIP discipline, like ``needs``).
COMMITS_MAX = 3

TEXT_CHARS = 600
REF_CHARS = 80

#: commit kind -> logbook entry type it is recorded under.
COMMIT_KINDS: dict[str, str] = {
    "ask": "decision",
    "account": "decision",
    "lead-ruled-out": "dead-end",
}


@dataclass
class CommitsOutcome:
    logbook_entries: int = 0
    dropped: int = 0
    accepted: int = 0


def parse_commits(raw: Any) -> tuple[list[dict[str, str]], int]:
    """Valid ``commits`` entries (capped) and the count dropped."""
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return [], 1
    out: list[dict[str, str]] = []
    dropped = 0
    for item in raw:
        if not isinstance(item, dict):
            dropped += 1
            continue
        kind = str(item.get("kind") or "").strip().lower()
        text = " ".join(str(item.get("text") or "").split())
        if kind not in COMMIT_KINDS or not text:
            dropped += 1
            continue
        entry = {"kind": kind, "text": text[:TEXT_CHARS]}
        ref = " ".join(str(item.get("ref") or "").split())
        if ref:
            entry["ref"] = ref[:REF_CHARS]
        if len(out) >= COMMITS_MAX:
            dropped += 1
            continue
        out.append(entry)
    return out, dropped


def apply_commits(store: Store, quest_id: int, raw: Any, *, by: str) -> CommitsOutcome:
    """Log each valid commit on the quest's logbook; note any dropped."""
    entries, dropped = parse_commits(raw)
    outcome = CommitsOutcome(dropped=dropped)
    for e in entries:
        ref = f" ({e['ref']})" if "ref" in e else ""
        logbook.append_entry(
            store,
            quest_id,
            text=f"commit [{e['kind']}]{ref}: {e['text']}",
            entry_type=COMMIT_KINDS[e["kind"]],
            by=by,
            extra_meta={
                "commit_kind": e["kind"],
                **({"ref": e["ref"]} if "ref" in e else {}),
            },
        )
        outcome.logbook_entries += 1
        outcome.accepted += 1
    if dropped:
        logbook.append_entry(
            store,
            quest_id,
            text=(
                f"commits: dropped {dropped} malformed entr"
                f"{'y' if dropped == 1 else 'ies'} (need kind in "
                f"{', '.join(sorted(COMMIT_KINDS))} and non-empty text; "
                f"max {COMMITS_MAX} per tick)"
            ),
            entry_type="observation",
            by=by,
        )
        outcome.logbook_entries += 1
    return outcome
