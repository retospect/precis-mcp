"""``precis retire-draft-equations`` — migrate legacy draft ``equation`` chunks
to ``$$…$$`` ``paragraph`` chunks.

The draft model has no dedicated ``equation`` chunk kind any more (the importer
emits normalised ``$$…$$`` paragraphs; the reader/editor/export treat math as
KaTeX in prose). This one-off, re-runnable backfill converts the *existing*
draft equation chunks:

* normalise the raw LaTeX body to a single-line ``$$ … $$`` via the shared,
  tested :func:`precis.draftimport.mathnorm.normalize_math` (strips ``\\label``,
  unwraps outer ``equation``/``align``/``gather`` envs, wraps bare aligned rows);
* ``store.edit_text`` the new body — this bumps ``content_sha`` (so the
  embed/keyword/summary cascade re-derives) and logs a ``chunk_events`` row with
  the original text as ``prev_text``, so the change is **reversible**;
* flip ``chunk_kind`` to ``paragraph``.

Cross-refs survive: ``\\label{…}`` in each raw body is mapped to the chunk's
handle *before* the label is stripped, and any ``\\ref``/``\\eqref`` to it (in
any draft chunk) is rewritten to the draft ``[¶<handle>]`` handle-reference form.

**Scope: drafts only.** Paper-ingest ``equation`` chunks (~99.5% of the corpus)
are a different pipeline (Marker PDF ingest, deliberately un-embedded) and are
left untouched — see the deferred backlog item in ``OPEN-ITEMS.md``. The
``equation`` slug therefore stays FK-alive in ``chunk_kinds`` for the paper
path; this command does not stamp ``deprecated_at``.

Dry-run by default: prints the planned per-chunk conversion and writes nothing.
Pass ``--commit`` to write. Re-running is safe: already-converted chunks are no
longer ``chunk_kind='equation'`` so they don't match.
"""

from __future__ import annotations

import argparse
import re
import sys

from precis.cli._common import resolve_dsn

#: ``\ref{L}`` / ``\eqref{L}`` — a cross-reference to a labelled equation.
_REF_RE = re.compile(r"\\(?:eq)?ref\s*\{([^}]+)\}")


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``retire-draft-equations`` subparser on ``sub``."""
    p = sub.add_parser(
        "retire-draft-equations",
        help="Convert legacy draft `equation` chunks to `$$…$$` paragraphs.",
        description=(
            "Migrate existing draft equation chunks to normalised `$$…$$` "
            "paragraph chunks (drafts only; paper chunks untouched). Dry-run "
            "by default; pass --commit to write."
        ),
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="Commit changes. Without this flag the command is a dry-run.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Convert at most N equation chunks (default: all).",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis retire-draft-equations``."""
    from precis.config import load_config
    from precis.draftimport.demacro import labels_in
    from precis.draftimport.mathnorm import normalize_math
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    store = build_runtime(cfg).store
    if store is None:
        print(
            "retire-draft-equations: no database configured — set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    dry = not args.commit
    mode = "DRY-RUN" if dry else "COMMIT"
    print(f"retire-draft-equations [{mode}]: limit={args.limit}", file=sys.stderr)

    with store.pool.connection() as conn:
        eq_rows = conn.execute(
            "SELECT c.chunk_id, c.handle, c.text "
            "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
            "WHERE c.chunk_kind = 'equation' AND r.kind = 'draft' "
            "AND c.retired_at IS NULL "
            "ORDER BY c.chunk_id"
        ).fetchall()
    if args.limit is not None:
        eq_rows = eq_rows[: args.limit]

    label_to_handle: dict[str, str] = {}
    converted = 0
    skipped_empty = 0
    for chunk_id, handle, text in eq_rows:
        raw = text or ""
        for lab in labels_in(raw):
            label_to_handle[lab] = handle
        norm = normalize_math(raw)
        if not norm:
            skipped_empty += 1
            print(f"  SKIP dc-chunk {chunk_id} (label-only, no math): {raw[:60]!r}")
            continue
        converted += 1
        print(f"  chunk {chunk_id}: {raw[:48]!r} -> {norm[:64]!r}")
        if not dry:
            store.edit_text(handle, norm)
            with store.pool.connection() as conn:
                conn.execute(
                    "UPDATE chunks SET chunk_kind = 'paragraph' WHERE chunk_id = %s",
                    (chunk_id,),
                )

    # Cross-ref rewrite: any draft chunk with \ref{L}/\eqref{L} where L labels a
    # converted equation → the draft [¶<handle>] handle-reference form.
    rewrites = 0
    unresolved: set[str] = set()
    if label_to_handle:
        with store.pool.connection() as conn:
            ref_rows = conn.execute(
                "SELECT c.chunk_id, c.handle, c.text "
                "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
                "WHERE r.kind = 'draft' AND c.retired_at IS NULL "
                "AND c.text LIKE '%ref{%'"
            ).fetchall()
        for chunk_id, handle, text in ref_rows:
            raw = text or ""

            def _sub(m: re.Match[str]) -> str:
                lab = m.group(1).strip()
                h = label_to_handle.get(lab)
                if h is None:
                    return m.group(0)
                return f"[¶{h}]"

            new = _REF_RE.sub(_sub, raw)
            # Record any eq-ref we could not resolve (label not among converted).
            for m in _REF_RE.finditer(raw):
                lab = m.group(1).strip()
                if lab.startswith("eq") and lab not in label_to_handle:
                    unresolved.add(lab)
            if new != raw:
                rewrites += 1
                print(f"  ref-rewrite chunk {chunk_id}")
                if not dry:
                    store.edit_text(handle, new)

    print(
        f"\nretire-draft-equations [{mode}] done: "
        f"{converted} converted, {skipped_empty} skipped (label-only), "
        f"{rewrites} cross-refs rewritten, {len(label_to_handle)} labels mapped.",
        file=sys.stderr,
    )
    if unresolved:
        print(
            f"  unresolved eq-refs (label not among converted): {sorted(unresolved)}",
            file=sys.stderr,
        )
    if dry and (converted or rewrites):
        print("Re-run with --commit to write.", file=sys.stderr)


__all__ = ["add_parser", "run"]
