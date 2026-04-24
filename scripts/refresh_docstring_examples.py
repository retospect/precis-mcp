#!/usr/bin/env python3
"""Refresh the canonical example slugs used in tool docstrings.

WHY:
  The MCP tool docstrings are themselves a teaching surface — every
  ``id='…'`` example becomes the LLM's mental model of valid input.
  When an example references a fictional slug (or one that has been
  removed from the store), the LLM copies it verbatim and the user sees
  ``ERROR [id_not_found]``.  This script picks real slugs from the live
  store and rewrites ``server.py`` so every example actually resolves.

WHEN TO RUN:
  - After a major reingest that may have renamed slugs.
  - After ``test_llm_tool_use.py::test_example_resolves_against_store``
    starts failing because a canonical example was removed from the
    corpus.
  - One-off, by hand — this is not a runtime concern.  The committed
    docstring output is what ships in the wheel.

DESIGN:
  - Deterministic given ``--seed`` (default 42).  Re-running with the
    same seed produces the same picks; bumping the seed picks fresh
    examples for variety.
  - Filters candidates to **real, dense, well-formed papers**:
      * has DOI                         (so doi: example resolves)
      * block_count >= 100              (so ›38..42 always works)
      * figure_count >= 3               (so /fig/3 resolves)
      * has abstract block              (so /abstract works)
  - Picks a SECOND paper (different from the first) that has
    ``arxiv_id``, for the arxiv: example.  Falls back to keeping the
    existing example if no arxiv-bearing paper passes the density
    filter.

USAGE:
    # Print the picks without writing anything.
    python scripts/refresh_docstring_examples.py --dry-run

    # Pick from a different seed and rewrite docstrings.
    python scripts/refresh_docstring_examples.py --seed 7 --apply

    # Default: dry-run, seed=42.

ENV:
  Reads the same ``~/.acatome/config.toml`` (or ``ACATOME_CONFIG`` env)
  that the precis server uses, via ``precis._store.get_store``.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path

# The two slugs we ship with today, treated as placeholders to substitute.
# When a maintainer reruns this script with --apply, every occurrence in
# ``server.py`` is rewritten to the freshly-sampled real slugs.
PLACEHOLDER_PAPER = "wang2020state"
PLACEHOLDER_DOI = "10.1021/jacs.2c01234"
PLACEHOLDER_ARXIV = "2301.12345"

# The file containing the docstrings we rewrite.  Single source — by
# convention, every user-facing ``@mcp.tool()`` lives in server.py.
SERVER_PY = Path(__file__).resolve().parent.parent / "src" / "precis" / "server.py"


def _load_candidates() -> list[dict]:
    """Pull every paper with full metadata and a non-trivial block count.

    Done in one query rather than per-paper to keep the script fast on
    a multi-thousand-paper corpus.  ``list_papers`` already JOINs the
    block table for the count.
    """
    from precis._store import get_store

    store = get_store()
    papers = store.list_papers(limit=10000)
    out = []
    for p in papers:
        slug = p.get("slug") or ""
        doi = p.get("doi") or ""
        block_count = p.get("block_count") or p.get("n_blocks") or 0
        # ``list_papers`` doesn't always carry figure_count — fall back
        # to a per-slug query only for the ~50 papers that survive the
        # cheap pre-filter so we don't hammer the DB.
        if not slug or not doi or block_count < 100:
            continue
        out.append(
            {
                "slug": slug,
                "doi": doi,
                "arxiv_id": p.get("arxiv_id") or "",
                "block_count": block_count,
            }
        )
    return out


def _enrich(candidate: dict, min_figs: int = 3) -> dict | None:
    """Enrich a candidate with figure_count, abstract presence, arxiv_id.

    ``list_papers()`` returns only the columns shown by the index view
    (slug, doi, title, authors, year, block_count, …) — notably **not**
    ``arxiv_id``.  We pull the full record via ``store.get(slug)`` and
    add the figure/abstract counts so the qualification filter has
    everything it needs in one place.

    Returns the enriched dict on success, or ``None`` when the
    candidate fails the structural checks (figures < min_figs or no
    abstract block) — these are the requirements that make ``/fig/3``
    and ``/abstract`` examples actually resolve.
    """
    from precis._store import get_store

    store = get_store()
    figs = store.get_figures(candidate["slug"]) or []
    if len(figs) < min_figs:
        return None
    abstract_blocks = (
        store.get_blocks(candidate["slug"], block_type="abstract") or []
    )
    if not abstract_blocks:
        return None
    full = store.get(candidate["slug"]) or {}
    return {
        **candidate,
        "arxiv_id": full.get("arxiv_id") or "",
        "figure_count": len(figs),
    }


def _pick(candidates: list[dict], seed: int) -> tuple[dict, dict | None]:
    """Pick (canonical_paper, canonical_arxiv_paper) deterministically.

    Same seed + same store contents → same picks.  ``canonical_arxiv``
    is None when no candidate has an arxiv_id; the caller leaves the
    existing arxiv example in place.

    The qualified set is built lazily — we shuffle the cheap pre-filter
    pool first, then enrich (one DB call per slug) until we have enough
    qualified candidates.  This avoids enriching all ~2700 candidates
    just to pick two.
    """
    rng = random.Random(seed)
    pool = list(candidates)
    rng.shuffle(pool)

    # Bias toward short slugs — the docstring token budget (≈600 per
    # tool) is tight and every char in a slug shows up ~13× across the
    # examples.  We keep slugs ≤ 14 chars first; if that's empty (small
    # corpus) we fall back to the full pool.  Within each bucket the
    # rng-shuffled order still gives variety across seeds.
    short, rest = [], []
    for c in pool:
        (short if len(c["slug"]) <= 14 else rest).append(c)
    pool = short + rest

    qualified: list[dict] = []
    arxiv_qualified: list[dict] = []
    # Stop once we have a paper for the canonical pick AND at least one
    # arxiv-bearing qualified candidate.  Cap the scan at 200 to bound
    # wall-clock on small stores while still giving variety.
    SCAN_BUDGET = 200
    for cand in pool[:SCAN_BUDGET]:
        enriched = _enrich(cand)
        if enriched is None:
            continue
        qualified.append(enriched)
        if enriched["arxiv_id"]:
            arxiv_qualified.append(enriched)
        if qualified and arxiv_qualified:
            break

    if not qualified:
        sys.exit(
            "no qualified candidates: need block_count>=100, doi, figs>=3, "
            "abstract present.  Is the store empty?"
        )
    canonical = qualified[0]

    # Pick the first arxiv qualified that isn't the canonical paper.
    canonical_arxiv = next(
        (c for c in arxiv_qualified if c["slug"] != canonical["slug"]),
        None,
    )

    return canonical, canonical_arxiv


def _build_substitutions(
    canonical: dict, canonical_arxiv: dict | None
) -> list[tuple[str, str]]:
    """Map placeholder → real for the substitutions we apply.

    Order matters: longer patterns first to avoid prefix shadowing
    (e.g. don't rewrite ``wang2020state›38`` as a substring of a
    larger match).  Python's ``re.sub`` with a literal pattern handles
    this naturally because we do exact substring replacement, but the
    list ordering documents the intent for human readers.
    """
    subs = [
        (PLACEHOLDER_PAPER, canonical["slug"]),
        (PLACEHOLDER_DOI, canonical["doi"]),
    ]
    if canonical_arxiv is not None:
        subs.append((PLACEHOLDER_ARXIV, canonical_arxiv["arxiv_id"]))
    return subs


def _apply_to_file(path: Path, subs: list[tuple[str, str]]) -> int:
    """Rewrite ``path`` in place, returning the count of replacements.

    Idempotent: rerunning with the same picks is a no-op.  We escape
    the placeholder for ``re`` even though they're plain ASCII so the
    regex is robust if a future placeholder contains a ``.`` etc.
    """
    text = path.read_text(encoding="utf-8")
    n = 0
    for placeholder, real in subs:
        if placeholder == real:
            continue  # no-op
        new_text, count = re.subn(re.escape(placeholder), real, text)
        n += count
        text = new_text
    path.write_text(text, encoding="utf-8")
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually rewrite server.py (default: dry-run, print only)",
    )
    args = parser.parse_args()

    candidates = _load_candidates()
    if not candidates:
        print("no candidates: store appears empty or no paper has DOI + 100+ blocks")
        return 1

    canonical, canonical_arxiv = _pick(candidates, args.seed)

    print(f"seed={args.seed}  candidates={len(candidates)}")
    print()
    print("canonical paper:")
    print(f"  slug         {canonical['slug']!r}")
    print(f"  doi          {canonical['doi']!r}")
    print(f"  block_count  {canonical['block_count']}")
    if canonical_arxiv:
        print()
        print("canonical arxiv paper:")
        print(f"  slug         {canonical_arxiv['slug']!r}")
        print(f"  arxiv_id     {canonical_arxiv['arxiv_id']!r}")
    else:
        print()
        print("(no arxiv-bearing candidate; arxiv example kept as-is)")

    subs = _build_substitutions(canonical, canonical_arxiv)
    print()
    print("substitutions:")
    for placeholder, real in subs:
        if placeholder == real:
            print(f"  {placeholder!r:<40} (already real, no-op)")
        else:
            print(f"  {placeholder!r:<40} → {real!r}")

    if args.apply:
        n = _apply_to_file(SERVER_PY, subs)
        print()
        print(f"applied: {n} replacements in {SERVER_PY}")
    else:
        print()
        print("(dry-run: rerun with --apply to rewrite server.py)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
