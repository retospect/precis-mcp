"""``precis convert-draft-lists`` — markdown bullet paragraphs → structured lists.

Backfill for the trap closed in :mod:`precis.draft.mdlist`: drafts written
before that parser stored their lists as markdown ``- `` text inside a
``paragraph`` chunk. Only the web reader renders that; the LaTeX/PDF and
docx exports build lists from ``ulist``/``olist`` containers with ``item``
children (migration 0037), so bullet text collapsed into one run-on line
with literal hyphens.

The conversion reuses the write door rather than a second parser: each
paragraph's text goes back through ``store.drafts.add_chunks``, which now
parses bullets at insert, and the old paragraph is retired. In-prose
cross-refs (``[dc<id>]``) to a converted paragraph are rewritten onto its
new container so they keep resolving.

Dry-run by default; ``--commit`` writes. Re-running is safe — a converted
list is no longer a bullet-led paragraph, so it no longer matches.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from precis.cli._common import resolve_dsn
from precis.draft.mdlist import count_items, parse_list_block


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``convert-draft-lists`` subparser on ``sub``."""
    p = sub.add_parser(
        "convert-draft-lists",
        help="Convert markdown bullet paragraphs to ulist/item chunk trees.",
        description=(
            "Restructure draft paragraphs that are wholly a markdown list "
            "into ulist/olist containers with item children, so the PDF and "
            "docx exports render them as real nested lists. Dry-run by "
            "default; pass --commit to write."
        ),
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="Commit changes. Without this flag the command is a dry-run.",
    )
    p.add_argument(
        "--draft",
        default=None,
        help="Restrict to one draft slug (default: every draft).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Convert at most N paragraphs (default: all).",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def _candidates(store: Any, slug: str | None) -> list[tuple[int, int, str, str]]:
    """Live draft paragraphs whose text parses as a list:
    ``(chunk_id, ref_id, handle, text)`` in document order."""
    sql = (
        "SELECT c.chunk_id, c.ref_id, c.handle, c.text "
        "  FROM chunks c "
        "  JOIN refs r ON r.ref_id = c.ref_id "
        " WHERE r.kind = 'draft' AND c.chunk_kind = 'paragraph' "
        "   AND c.retired_at IS NULL AND c.pos IS NOT NULL "
    )
    params: list[Any] = []
    if slug is not None:
        sql += (
            "   AND c.ref_id = (SELECT ref_id FROM ref_identifiers "
            "                    WHERE id_value = %s LIMIT 1) "
        )
        params.append(slug)
    sql += " ORDER BY c.chunk_id"
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [r for r in rows if parse_list_block(r[3] or "") is not None]


def run(args: argparse.Namespace) -> None:
    """Execute ``precis convert-draft-lists``."""
    from precis.config import load_config
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    store = build_runtime(cfg).store
    if store is None:
        print(
            "convert-draft-lists: no database configured — set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    dry = not args.commit
    mode = "DRY-RUN" if dry else "COMMIT"
    rows = _candidates(store, args.draft)
    if args.limit is not None:
        rows = rows[: args.limit]
    print(
        f"convert-draft-lists [{mode}]: {len(rows)} paragraph(s) parse as a list",
        file=sys.stderr,
    )

    # old paragraph chunk_id → new container chunk_id, for the cross-ref
    # rewrite once every conversion has landed.
    remap: dict[int, int] = {}
    items_total = 0
    for chunk_id, ref_id, handle, text in rows:
        tree = parse_list_block(text or "")
        assert tree is not None  # _candidates filtered on exactly this
        items = count_items(tree)
        items_total += items
        head = " ".join((text or "").split())[:70]
        print(f"  dc{chunk_id}: {items} item(s)  {head!r}")
        if dry:
            continue
        chunk = store.drafts.get_draft_chunk(f"dc{chunk_id}")
        assert chunk is not None
        made = store.drafts.add_chunks(
            ref_id=ref_id,
            chunk_kind="paragraph",  # add_chunks parses the bullets
            text=text or "",
            at={"after": handle},
            meta=chunk.meta or None,
        )
        containers = [c for c in made if c.chunk_kind in ("ulist", "olist")]
        if not containers:  # parser and writer disagreed — leave it be
            print(f"    ! dc{chunk_id} did not convert; left as a paragraph")
            continue
        remap[chunk_id] = containers[0].chunk_id
        store.drafts.retire_chunk(handle, source={"reason": "markdown-list"})

    rewrites = _rewrite_xrefs(store, remap) if remap else 0
    print(
        f"convert-draft-lists [{mode}]: {len(rows)} paragraph(s), "
        f"{items_total} item(s), {rewrites} cross-ref(s) rewritten"
        + ("  (nothing written — pass --commit)" if dry else ""),
        file=sys.stderr,
    )


def _rewrite_xrefs(store: Any, remap: dict[int, int]) -> int:
    """Point in-prose ``[dc<id>]`` refs at the converted paragraph's new
    container. Reuses the store's own tested remapper rather than keeping a
    second copy of the cross-ref grammar."""
    from precis.store._draft_ops import _remap_intra_draft_xrefs

    hits = 0
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.handle, c.text FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
            " WHERE r.kind = 'draft' AND c.retired_at IS NULL AND c.text LIKE '%%dc%%'"
        ).fetchall()
    for handle, text in rows:
        new = _remap_intra_draft_xrefs(text or "", remap, {})
        if new != (text or ""):
            store.drafts.edit_text(handle, new)
            hits += 1
    return hits
