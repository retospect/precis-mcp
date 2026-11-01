"""``put(dry_run=True)`` preview: what a create would ack, before it writes.

A memory ``put`` used to show its ``Next:`` block (nearest hubs, type tag,
"a thread with this title prefix exists", the round-trip check) only after
the row existed, so a duplicate was already written and had to be deleted
(gr548271). The dry run writes nothing: no ref, chunk, link, embedding row
or queue entry. It embeds the draft's title + body once, in memory, and
lists the nearest same-kind refs in the same ``SPACE:`` by cosine distance
(the ``+recall`` leg: body chunks plus ``card_combined`` cards, under the
:data:`RECALL_MAX_DISTANCE` floor).

:func:`kind_rule` proposes a kind by fixed rules, no model call. The
learned-router alternative (gr547900) waits for evidence: an override log
from this door is what would justify one. Memory puts get the rule;
finding and concept puts get the rest of the block.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from precis.response import Response
from precis.utils import handle_registry
from precis.utils.eye_render import RECALL_MAX_DISTANCE
from precis.utils.next_block import render_next_section

log = logging.getLogger(__name__)

#: Duplicates the preview lists.
DUP_K = 3
#: Cosine distance at or under which the preview asks for an edit instead.
NEAR_DUP_DISTANCE = 0.08
#: Longest text the definition rule still calls a definition.
DEFINITION_MAX_WORDS = 40

DRY_RUN_HEADER = "dry run — nothing written"

_CLOZE_RE = re.compile(r"\{\{c\d+::")
_PAPER_CITE_RE = re.compile(r"\bp[ca]\d+\b")
#: ``X is/are …`` with a subject of at most six words.
_DEFINITION_RE = re.compile(
    r"^[\w'()/-]+(?:\s+[\w'()/-]+){0,5}\s+(?:is|are)\s+\S", re.IGNORECASE
)
_LIST_OR_HEADING_RE = re.compile(r"^(?:#|[-*>]|\d+[.)]\s)")


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _claim_shaped(line: str) -> bool:
    """A declarative sentence: four or more words, not a heading, list item
    or quote, not ending in ``?`` or ``:``."""
    return (
        len(line.split()) >= 4
        and not _LIST_OR_HEADING_RE.match(line)
        and not line.endswith(("?", ":"))
    )


def kind_rule(text: str) -> tuple[str, str]:
    """``(kind, rule)`` the text's shape proposes, first match wins: cloze
    markup → anki; exactly one paper cite (``pcN``/``paN``) with a
    claim-shaped first line → finding; ``X is/are …`` in at most
    :data:`DEFINITION_MAX_WORDS` words → taxon; else memory."""
    if _CLOZE_RE.search(text):
        return "anki", "cloze markup {{cN::"
    first = _first_line(text)
    if len(set(_PAPER_CITE_RE.findall(text))) == 1 and _claim_shaped(first):
        return "finding", "one paper cite + claim-shaped first line"
    if len(text.split()) <= DEFINITION_MAX_WORDS and _DEFINITION_RE.match(first):
        return "taxon", f"definition-shaped, <= {DEFINITION_MAX_WORDS} words"
    return "memory", "default"


def nearest_duplicates(
    handler: Any, text: str, *, kind: str, space: str | None
) -> list[tuple[int, str, float]] | None:
    """Up to :data:`DUP_K` ``(ref_id, title, distance)`` for live ``kind``
    refs nearest ``text``, inside ``space`` when given. ``None`` when no
    embedder is wired or the embed fails (the preview says so)."""
    embedder = getattr(getattr(handler, "hub", None), "embedder", None)
    if embedder is None or not text.strip():
        return None
    try:
        vec = embedder.embed_one(text)
    except Exception:
        log.warning("put dry run: embed failed; duplicates not checked", exc_info=True)
        return None
    hits = handler.store.chunks.search_chunks_semantic(
        query_vec=vec,
        kinds=[kind],
        tags=[space] if space else None,
        limit=DUP_K * 3,
        max_distance=RECALL_MAX_DISTANCE,
        card_kinds=("card_combined",),
    )
    out: list[tuple[int, str, float]] = []
    for _block, ref, dist in hits:
        rid = int(ref.id)
        if any(rid == o[0] for o in out):
            continue
        out.append((rid, " ".join(str(ref.title or "").split())[:80], float(dist)))
        if len(out) == DUP_K:
            break
    return out


def render_preview(
    kind: str,
    *,
    dups: list[tuple[int, str, float]] | None,
    space: str | None,
    next_rows: list[tuple[str, str]],
    rule: tuple[str, str] | None = None,
) -> Response:
    """The dry-run ack: header, proposed kind (memory only), the
    near-duplicate retain line, the duplicate list, then ``Next:``."""

    def h(rid: int) -> str:
        return handle_registry.try_format(kind, rid) or f"{kind}:{rid}"

    lines = [DRY_RUN_HEADER]
    if rule is not None:
        lines.append(f"kind: {rule[0]} (rule: {rule[1]})")
    where = f" in {space}" if space else ""
    if dups is None:
        lines.append("nearest duplicates: not checked (no embedder)")
    else:
        near = next((d for d in dups if d[2] <= NEAR_DUP_DISTANCE), None)
        if near is not None:
            lines.append(
                f"near-duplicate {h(near[0])} ({near[2]:.2f}) — edit it instead?"
            )
        if dups:
            lines.append(f"nearest duplicates{where} (embedding distance):")
            lines += [f"  {h(rid)}  {dist:.3f}  {title}" for rid, title, dist in dups]
        else:
            lines.append(
                f"nearest duplicates{where}: none within {RECALL_MAX_DISTANCE}"
            )
    return Response(body="\n".join(lines) + render_next_section(next_rows))


def dup_read_rows(
    kind: str, dups: list[tuple[int, str, float]] | None
) -> list[tuple[str, str]]:
    """One ``get`` row for the nearest duplicate, when there is one."""
    if not dups:
        return []
    rid, _title, dist = dups[0]
    handle = handle_registry.try_format(kind, rid) or f"{kind}:{rid}"
    return [(f"get(kind='{kind}', id='{handle}')", f"read the nearest ({dist:.2f})")]
