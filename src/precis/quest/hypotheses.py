"""Hypotheses as living findings that serve the quest (gr674774, me674763).

A quest's hypothesis is a ``finding(hypothesis=True)`` with a ``serves``
edge to the quest. Its verdict and its supports/disputes edges live on that
finding and are updated in place; the logbook carries only a dated pointer
(``raised fi<n>``). This module is the one seam the tick, the gap scan and
the dialectic ops share:

* :func:`mint_tick_hypotheses` — the tick payload's ``hypotheses`` list →
  findings (converging on an existing sentence), ``serves`` edges, pointer.
* :func:`serving_hypotheses` — the quest's hypothesis findings with their
  edge counts and settled state (gap scan + tick context).
* :func:`settle_hypothesis` / :func:`reopen_hypothesis` — the verdict write.

Settled means ``STATUS:refuted`` or the open tag :data:`SETTLED_TAG`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput
from precis.store import Store
from precis.store.types import Tag

log = logging.getLogger(__name__)

SETTLED_TAG = "settled"
VERDICTS = ("refuted", "supported", "inconclusive")
#: Hypotheses minted per tick — each costs a mint gate pass and corpus edges.
MAX_HYPOTHESES_PER_TICK = 3

_FI_RE = re.compile(r"\bfi(\d+)\b")


@dataclass(frozen=True)
class LiveHypothesis:
    ref_id: int
    sentence: str
    supports: int
    disputes: int
    settled: bool
    refuted: bool


@dataclass
class HypothesisReceipt:
    minted: list[int]
    skipped: int = 0


def _is_hypothesis(ref: Any) -> bool:
    from precis.handlers._finding_hypothesis import (
        ARTIFACT_HYPOTHESIS,
        META_ARTIFACT_TYPE,
    )

    return (
        ref is not None
        and ref.kind == "finding"
        and ref.retired_at is None
        and (ref.meta or {}).get(META_ARTIFACT_TYPE) == ARTIFACT_HYPOTHESIS
    )


def _tag_state(store: Store, ref_id: int) -> tuple[bool, bool]:
    """``(refuted, settled_tag)`` for a finding."""
    try:
        tags = store.tags_for(ref_id)
    except Exception:
        return False, False
    refuted = any((t.prefix or "") == "STATUS" and t.value == "refuted" for t in tags)
    tagged = any(t.namespace == "open" and t.value == SETTLED_TAG for t in tags)
    return refuted, tagged


def serving_hypotheses(store: Store, quest_id: int) -> list[LiveHypothesis]:
    """Hypothesis findings that ``serves`` the quest (settled ones included)."""
    ids = list(
        dict.fromkeys(
            ln.src_ref_id
            for ln in store.links_for(quest_id, direction="in", relation="serves")
        )
    )
    if not ids:
        return []
    refs = store.fetch_refs_by_ids(set(ids))
    out: list[LiveHypothesis] = []
    for i in ids:
        ref = refs.get(i)
        if not _is_hypothesis(ref):
            continue
        assert ref is not None
        refuted, tagged = _tag_state(store, i)
        out.append(
            LiveHypothesis(
                ref_id=i,
                sentence=str(ref.title or "").strip(),
                supports=len(store.links_for(i, direction="in", relation="supports")),
                disputes=len(store.links_for(i, direction="in", relation="disputes")),
                settled=refuted or tagged,
                refuted=refuted,
            )
        )
    return out


def render_live_hypotheses(store: Store, quest_id: int) -> str:
    """The tick context's "Live hypotheses" body: unrefuted serving
    hypotheses with edge counts; refuted ones are omitted (never re-propose)."""
    lines = []
    for h in serving_hypotheses(store, quest_id):
        if h.refuted:
            continue
        mark = " — SETTLED" if h.settled else ""
        lines.append(
            f"- [fi{h.ref_id}] {h.sentence} "
            f"(supports {h.supports}, disputes {h.disputes}){mark}"
        )
    return "\n".join(lines) if lines else "(no live hypotheses yet)"


def settle_hypothesis(
    store: Store, hypothesis_id: int, verdict: str, *, ruling_id: int | None = None
) -> bool:
    """Record the verdict on the finding. ``refuted`` → ``STATUS:refuted``
    (+ ``superseded-by`` the ruling, me474371); the others → the ``settled``
    tag. Returns ``True`` iff something changed."""
    refuted, tagged = _tag_state(store, hypothesis_id)
    changed = False
    if verdict == "refuted":
        if not refuted:
            store.add_tag(
                hypothesis_id, Tag.closed("STATUS", "refuted"), replace_prefix=True
            )
            changed = True
        if ruling_id is not None and ruling_id != hypothesis_id:
            before = len(
                store.links_for(
                    hypothesis_id, direction="out", relation="superseded-by"
                )
            )
            store.add_link(
                src_ref_id=hypothesis_id,
                dst_ref_id=ruling_id,
                relation="superseded-by",
            )
            changed |= (
                len(
                    store.links_for(
                        hypothesis_id, direction="out", relation="superseded-by"
                    )
                )
                > before
            )
    elif not tagged:
        store.add_tag(hypothesis_id, Tag.open(SETTLED_TAG))
        changed = True
    return changed


def reopen_hypothesis(store: Store, hypothesis_id: int) -> bool:
    """Clear the ``settled`` tag (a refutation stays — it is a ruling)."""
    _refuted, tagged = _tag_state(store, hypothesis_id)
    if not tagged:
        return False
    store.remove_tag(hypothesis_id, Tag.open(SETTLED_TAG))
    return True


def _resolve_ref_id(store: Store, token: object, kind: str) -> int | None:
    text = str(token or "").strip().strip("[]")
    if not text:
        return None
    try:
        resolved = store.resolve_handle(text)
    except Exception:
        return None
    if resolved is None or resolved.chunk_id is not None:
        return None
    ref = store.fetch_refs_by_ids([int(resolved.ref_id)]).get(int(resolved.ref_id))
    return int(ref.id) if ref is not None and ref.kind == kind else None


def mint_tick_hypotheses(
    store: Store,
    quest_id: int,
    items: object,
    *,
    by: str,
) -> HypothesisReceipt:
    """Mint each well-formed ``hypotheses`` payload item as a hypothesis
    finding serving the quest (and its ``direction`` todo when named), then
    log a ``raised fi<n>`` pointer. A gate-failing item is skipped with a
    logged note; the tick never fails here. Re-raising a sentence converges
    on the same finding and writes no second pointer."""
    from precis.handlers._finding_hypothesis import put_hypothesis
    from precis.quest.logbook import append_entry

    receipt = HypothesisReceipt(minted=[])
    if not isinstance(items, list):
        return receipt
    for item in items[:MAX_HYPOTHESES_PER_TICK]:
        if not isinstance(item, dict):
            continue
        sentence = str(item.get("sentence") or item.get("text") or "").strip()
        motivated = item.get("motivated_by")
        try:
            resp = put_hypothesis(
                store,
                sentence=sentence,
                scope=None,
                motivation=str(item.get("motivation") or ""),
                testable_by=str(item.get("testable_by") or ""),
                motivated_by=[str(m) for m in motivated]
                if isinstance(motivated, list)
                else None,
                llm_models=[by or "quest-tick"],
            )
            m = _FI_RE.search(resp.body)
            if m is None:
                raise BadInput("hypothesis mint returned no handle")
            fid = int(m.group(1))
        except BadInput as exc:
            receipt.skipped += 1
            first = str(exc).splitlines()[0] if str(exc) else "refused"
            append_entry(
                store,
                quest_id,
                text=f"hypothesis not minted — {first[:200]}: {sentence[:120]}",
                entry_type="note",
                by=by,
            )
            continue
        except Exception:
            receipt.skipped += 1
            log.exception("mint_tick_hypotheses: mint raised for quest %s", quest_id)
            continue
        already = any(
            ln.dst_ref_id == quest_id
            for ln in store.links_for(fid, direction="out", relation="serves")
        )
        store.add_link(src_ref_id=fid, dst_ref_id=quest_id, relation="serves")
        direction = _resolve_ref_id(store, item.get("direction"), "todo")
        if direction is not None:
            store.add_link(src_ref_id=fid, dst_ref_id=direction, relation="serves")
        if already:
            continue
        receipt.minted.append(fid)
        append_entry(
            store,
            quest_id,
            text=f"raised fi{fid} — {sentence[:200]}",
            entry_type="note",
            by=by,
        )
    return receipt
