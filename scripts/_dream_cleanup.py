"""dream-cleanup — scripted prod hygiene for the 2026-10 dream/digest pass.

Dry run by default (prints the selection + a sample, writes nothing);
``--apply`` writes, one transaction per action. Reads
``PRECIS_DATABASE_URL`` — run against prod via ``scripts/_prod-run
scripts/dream-cleanup --apply`` (a coordinator does this; the writer role
has no prod writes).

Actions (selection queries are the spec — verify the dry-run counts
before applying):

1. ``retire-fail-markers`` — soft-retire every live ``review-fail:*``
   cooldown marker (prod held 87 ``review-fail:structural``; the code now
   keeps at most one live marker per reviewer).
2. ``untag-digests`` — drop ``internal-thought`` and ``user:asa`` from
   live digest memories (``digest:*`` / ``tree-review:*`` tagged); the
   tags scoped reviewer output into asa's inner-thoughts fetch.
3. ``trim-transcript-tails`` — rewrite digest bodies whose tail is
   transcript narration: the body is cut at the first line matching the
   narration pattern (DELETE + INSERT on the ``memory_body`` chunk —
   body chunks are append-only, never UPDATE).
4. ``move-to-draft`` — tag me9209 / me23441 ``kind:draft``: draft prose
   filed as memories by mistake.
5. ``link-corrections`` — add ``contradicts`` edges from correction
   dreams (DREAM-tagged memories whose body names an ``me<id>`` handle
   next to a correction phrase) to the memory they correct — the edge
   the old "follow-up correction memory" flow never recorded.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import open_store

# ── selection SQL (the spec) ─────────────────────────────────────

_FAIL_MARKERS = """
SELECT r.ref_id FROM refs r
 WHERE r.kind = 'memory' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'OPEN' AND t.value LIKE 'review-fail:%')
 ORDER BY r.ref_id
"""

_TAGGED_DIGESTS = """
SELECT r.ref_id FROM refs r
 WHERE r.kind = 'memory' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                  AND (t.value LIKE 'digest:%' OR t.value LIKE 'tree-review:%'))
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                  AND t.value IN ('internal-thought', 'user:asa'))
 ORDER BY r.ref_id
"""

_DIGEST_BODIES = """
SELECT r.ref_id, c.chunk_id, c.text FROM refs r
 JOIN chunks c ON c.ref_id = r.ref_id AND c.chunk_kind = 'memory_body'
   AND c.retired_at IS NULL AND c.ord = 0
 WHERE r.kind = 'memory' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                  AND (t.value LIKE 'digest:%' OR t.value LIKE 'tree-review:%'))
 ORDER BY r.ref_id
"""

_CORRECTION_DREAMS = """
SELECT r.ref_id, c.text FROM refs r
 JOIN chunks c ON c.ref_id = r.ref_id AND c.chunk_kind = 'memory_body'
   AND c.retired_at IS NULL
 WHERE r.kind = 'memory' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id AND t.namespace = 'DREAM')
   AND c.text ~ 'me[0-9]+'
   AND c.text ~* %(pat)s
 ORDER BY r.ref_id
"""

_CORRECTION_PAT = (
    r"(did n.o?t check out|turned out (to be )?(wrong|false|incorrect)|"
    r"was wrong|does not hold|did not hold|failed verification|"
    r"could not verify|cannot verify|should be retired|"
    r"no longer (true|valid)|superseded by)"
)

_DRAFT_IDS = (9209, 23441)

#: A body line that starts the transcript tail — narration or tool chatter
#: pasted after the digest proper.
_TAIL_RE = re.compile(
    r"^\s*(i'll |i will |let me |now i|i need to |i have |i've |"
    r"the (structural|deep) review (will|is complete|is done)|"
    r"review complete|done reviewing|tool calls?[: ]|turns?[: ]|"
    r"cost[: ]|duration[: ]|```\s*$)",
    re.IGNORECASE,
)


def _trim_tail(text: str) -> str | None:
    """Body minus its transcript tail; ``None`` when no tail marker matches."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i > 0 and _TAIL_RE.match(line):
            trimmed = "\n".join(lines[:i]).rstrip()
            if len(trimmed) >= 200:
                return trimmed
            return trimmed or None
    return None


def _mem_handles(text: str, skip: set[int]) -> list[int]:
    return [int(m) for m in re.findall(r"\bme(\d+)\b", text) if int(m) not in skip]


def run(store: Any, *, apply: bool) -> list[str]:
    out: list[str] = []

    with store.pool.connection() as conn:
        markers = [int(r[0]) for r in conn.execute(_FAIL_MARKERS).fetchall()]
        digests = [int(r[0]) for r in conn.execute(_TAGGED_DIGESTS).fetchall()]
        bodies = [
            (int(r[0]), int(r[1]), str(r[2]))
            for r in conn.execute(_DIGEST_BODIES).fetchall()
        ]
        corrections = [
            (int(r[0]), str(r[1]))
            for r in conn.execute(
                _CORRECTION_DREAMS, {"pat": _CORRECTION_PAT}
            ).fetchall()
        ]
        # Tag only the named memories that are actually live — a ref that
        # is gone or already retired is reported, not written.
        draft_ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) "
                "AND kind = 'memory' AND retired_at IS NULL",
                (list(_DRAFT_IDS),),
            ).fetchall()
        ]

    trims: list[tuple[int, int, str]] = []
    for rid, cid, t in bodies:
        t2 = _trim_tail(t)
        if t2 is not None:
            trims.append((rid, cid, t2))
    links = [(rid, _mem_handles(t, {rid})) for rid, t in corrections]
    links = [(rid, tgts) for rid, tgts in links if tgts]

    out.append(f"retire-fail-markers: {len(markers)} live review-fail:* markers")
    out.append(f"untag-digests: {len(digests)} digests carrying inner-life tags")
    out.append(f"trim-transcript-tails: {len(trims)} digest bodies")
    out.append(
        f"move-to-draft: {len(draft_ids)} of {len(_DRAFT_IDS)} named "
        "memories live -> kind:draft"
    )
    out.append(f"link-corrections: {len(links)} correction dreams")
    if not apply:
        out.append("(dry run — pass --apply to write)")
        return out

    from precis.store.types import Tag

    for rid in markers:
        with store.tx() as conn:
            store.unlink_ref(rid, conn=conn)
            store.retire_ref(rid, conn=conn)
    out.append(f"retired {len(markers)} markers")

    for rid in digests:
        with store.tx() as conn:
            for value in ("internal-thought", "user:asa"):
                store.remove_tag(rid, Tag.open(value), conn=conn)
    out.append(f"untagged {len(digests)} digests")

    for rid, cid, new_text in trims:
        with store.tx() as conn:
            conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (cid,))
            from precis.store.types import ChunkInsert

            store.chunks.insert_chunks(
                rid,
                [ChunkInsert(ord=0, text=new_text, meta={"chunk_kind": "memory_body"})],
                conn=conn,
            )
    out.append(f"trimmed {len(trims)} digest bodies")

    for rid in draft_ids:
        with store.tx() as conn:
            store.add_tag(rid, Tag.open("kind:draft"), set_by="system", conn=conn)
    out.append(f"tagged {draft_ids} kind:draft")

    n_edges = 0
    for rid, targets in links:
        with store.tx() as conn:
            for tgt in targets:
                try:
                    store.add_link(
                        src_ref_id=rid,
                        dst_ref_id=tgt,
                        relation="contradicts",
                        set_by="system",
                        conn=conn,
                    )
                    n_edges += 1
                except Exception:
                    log_line = f"link-corrections: me{rid} -> me{tgt} skipped"
                    out.append(log_line)
    out.append(f"added {n_edges} contradicts edges")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="write the changes")
    args = p.parse_args()
    if not os.environ.get("PRECIS_DATABASE_URL"):
        sys.exit("PRECIS_DATABASE_URL is not set")
    store, _cfg = open_store()
    try:
        for line in run(store, apply=args.apply):
            print(line)
    finally:
        store.close()


if __name__ == "__main__":
    main()
