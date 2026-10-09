"""memory-attribution-audit — the attribution gate retro-applied.

Usage (via the ``memory-attribution-audit`` shell wrapper):

    memory-attribution-audit            # dry run: counts + 30-row sample
    memory-attribution-audit --apply    # add AUDIT:ungrounded-number to flagged rows
    memory-attribution-audit --sample 60

Reads ``PRECIS_DATABASE_URL`` (dev DB by default; point it at prod through
the gitignored cluster overlay, never a literal in the repo). Never edits a
body chunk: ``--apply`` only adds the closed tag (``set_by='system'``) and
never clears one. A memory whose evaluation raises is counted in ``errors``
and skipped.
See docs/backlog/memory-attribution-gate.md §4.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Make `_common` importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import open_store

#: A body worth running through the gate: it names *something* citation-shaped.
#: Python decides the rest (a number near it); this only skips the plainly
#: citation-free bulk. ``kind:id``, ``[xx123]`` and bare ``pc123456`` forms.
_CANDIDATE_SQL_RE = (
    r"([a-z][a-z0-9-]*:#?[0-9]|\[[a-z]{2}[0-9]+|"
    r"\m(pa|pt|me|fi|pc|pk|fb)[0-9]{3,}|\m[a-z]{3,}[0-9]{2}[a-z]?\M)"
)

_BATCH = 500


def candidate_rows(store: Any, after_id: int, limit: int) -> list[tuple[int, str]]:
    """Live memories (``ref_id > after_id``) whose body might carry a cited number."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, c.text FROM refs r "
            "JOIN chunks c ON c.ref_id = r.ref_id "
            "AND c.chunk_kind = 'memory_body' AND c.retired_at IS NULL "
            "WHERE r.kind = 'memory' AND r.retired_at IS NULL "
            "AND r.ref_id > %s AND c.text ~ %s "
            "ORDER BY r.ref_id LIMIT %s",
            (after_id, _CANDIDATE_SQL_RE, limit),
        ).fetchall()
    return [(int(r), str(t)) for r, t in rows]


def cite_kind(cite: str) -> str:
    """Per-cite-kind bucket: ``websearch`` for ``websearch:1``, ``pc`` for ``pc123``."""
    if ":" in cite:
        return cite.split(":", 1)[0]
    letters = "".join(ch for ch in cite.strip("[]") if ch.isalpha())
    return letters if len(letters) <= 2 else "paper-key"


def audit(store: Any, *, apply: bool = False, sample: int = 30) -> dict[str, Any]:
    """Run the gate over every candidate memory.

    Returns ``{candidates, flagged, flagged_ids, by_cite_kind, sample,
    applied, errors}``. One :class:`AttributionCache` spans the run, so each cited
    paper/websearch is read once however many memories cite it.
    """
    from precis.handlers._attribution import (
        AUDIT_VALUE,
        AttributionCache,
        grounding_failures,
        ungrounded_cited_numbers,
    )
    from precis.store import Tag
    from precis.utils.numerics import numeric_spans

    tag = Tag.closed("AUDIT", AUDIT_VALUE)
    cache = AttributionCache()
    candidates = 0
    errors = 0
    flagged: list[int] = []
    by_kind: Counter[str] = Counter()
    samples: list[str] = []
    after = 0
    while True:
        rows = candidate_rows(store, after, _BATCH)
        if not rows:
            break
        after = rows[-1][0]
        for ref_id, body in rows:
            if not numeric_spans(body):
                continue
            candidates += 1
            try:
                misses = grounding_failures(
                    ungrounded_cited_numbers(store, body, cache=cache)
                )
            except Exception:
                errors += 1
                continue
            if not misses:
                continue
            flagged.append(ref_id)
            for m in misses:
                by_kind[cite_kind(m.cite)] += 1
                if len(samples) < sample:
                    samples.append(f'me{ref_id}  "{m.token}"  {m.cite}')
    applied = 0
    if apply:
        for ref_id in flagged:
            store.add_tag(ref_id, tag, set_by="system")
            applied += 1
    return {
        "candidates": candidates,
        "flagged": len(flagged),
        "flagged_ids": flagged,
        "by_cite_kind": dict(by_kind.most_common()),
        "sample": samples,
        "applied": applied,
        "errors": errors,
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        epilog="--apply only ADDS AUDIT:ungrounded-number; it never clears a tag.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Add AUDIT:ungrounded-number to every flagged memory (default: dry "
            "run). Only adds tags; never clears them."
        ),
    )
    p.add_argument("--sample", type=int, default=30, help="Sample rows to print.")
    args = p.parse_args()
    store, _cfg = open_store()
    try:
        out = audit(store, apply=args.apply, sample=args.sample)
    finally:
        store.close()
    print(f"candidates (citation + unit-bearing number): {out['candidates']}")
    print(f"flagged (number not in cited text):          {out['flagged']}")
    print(f"errors (memories skipped on exception):      {out['errors']}")
    for kind, n in out["by_cite_kind"].items():
        print(f"  {kind}: {n}")
    print(f"sample ({len(out['sample'])}):")
    for line in out["sample"]:
        print(f"  {line}")
    if args.apply:
        print(f"applied AUDIT:ungrounded-number to {out['applied']} memories")
    else:
        print("dry run: nothing changed (--apply to tag)")


if __name__ == "__main__":
    main()
