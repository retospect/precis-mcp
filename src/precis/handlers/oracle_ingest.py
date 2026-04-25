"""``precis-ingest-oracle`` — load oracle papers into the wisdom corpus.

Reads YAML files from ``data/oracle/`` (or a user-supplied directory)
and ingests each one as a paper-shaped ref in the ``oracle`` corpus,
with one chunk per entry.

Schema (per YAML file, one tradition per file)::

    slug: iching            # → ref slug oracle:iching
    title: I-Ching          # → ref title
    description: ...        # → ref meta.description
    tags: [i-ching, ...]    # → ref tags ('oracle' and 'built-in' added)
    entries:
      - title: ...          # → section_path[0]
        body: |             # → chunk text
          ...
        extra_section_path: [...]    # → appended to section_path
        original: ...       # → tail line `_original_: ...`
        pinyin: ...
        lang: ...
        source: ...
        trigrams: ...       # i-ching only
        binary: ...

Modes:

- ``--dry-run``: report what would be ingested; no DB writes.
- ``--overwrite``: replace existing refs (full re-ingest of all
  chunks).  Default is to skip refs that already exist.
- ``--from <DIR>``: directory of YAML files; default is the bundled
  ``data/oracle/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def _bundled_oracle_dir() -> Path | None:
    """Return the path to the bundled ``data/oracle/`` directory.

    The canonical location is ``src/precis/data/oracle/`` (in-package),
    which means it ships inside the wheel.  Resolved relative to this
    module:

    - Editable install: ``<repo>/src/precis/handlers/oracle_ingest.py``
      → ``parents[1]`` is ``<repo>/src/precis/``
    - Built wheel: ``<site-packages>/precis/handlers/oracle_ingest.py``
      → ``parents[1]`` is ``<site-packages>/precis/``

    Both paths resolve to ``parents[1] / "data" / "oracle"``.
    """
    here = Path(__file__).resolve()
    candidates = [
        # Canonical: data/ inside the package tree (ships with wheel).
        here.parents[1] / "data" / "oracle",
        # Legacy: data/ at the repo root (pre-5.2.1 layout).
        here.parents[3] / "data" / "oracle",
        here.parents[2] / "data" / "oracle",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


# ---------------------------------------------------------------------------
# Body rendering — the inverse of OracleHandler._read_overview
# ---------------------------------------------------------------------------


def _render_chunk_body(entry: dict) -> str:
    """Compose the chunk text from an entry's body + structured tail.

    Body is taken verbatim (already markdown).  Each of ``original /
    pinyin / source / lang / trigrams / binary`` adds a ``_key_:
    value`` tail line so the data is searchable and visible without
    needing a Block.meta column.
    """
    body = (entry.get("body") or "").strip()
    tail_keys = (
        "original", "pinyin", "source", "lang", "trigrams", "binary",
    )
    tail_lines: list[str] = []
    for key in tail_keys:
        val = entry.get(key)
        if val is None or val == "":
            continue
        tail_lines.append(f"_{key}_: {val}")
    if tail_lines:
        return f"{body}\n\n" + "\n".join(tail_lines)
    return body


def _section_path(entry: dict) -> list[str]:
    """Build the section_path JSON for an entry.

    ``[title]`` plus any ``extra_section_path`` items, deduped while
    preserving order.
    """
    head = (entry.get("title") or "").strip()
    extras = list(entry.get("extra_section_path") or [])
    out = [head] if head else []
    for x in extras:
        s = str(x).strip()
        if s and s not in out and s != "—":
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest_paper(
    yaml_path: Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """Ingest one tradition's YAML into ``oracle:<slug>``.

    Returns stats: ``{created, replaced, chunks, skipped, errors}``.
    """
    if not yaml_path.exists():
        raise FileNotFoundError(yaml_path)

    with open(yaml_path) as f:
        doc = yaml.safe_load(f)

    if not isinstance(doc, dict):
        raise ValueError(f"{yaml_path}: top-level must be a mapping")
    for required in ("slug", "title", "entries"):
        if required not in doc:
            raise ValueError(f"{yaml_path}: missing required key {required!r}")

    slug_short = doc["slug"]
    full_slug = f"oracle:{slug_short}"
    title = doc["title"]
    description = doc.get("description", "")
    user_tags = list(doc.get("tags") or [])
    entries = doc.get("entries") or []
    if not isinstance(entries, list):
        raise ValueError(f"{yaml_path}: 'entries' must be a list")

    # Always tag with the oracle + built-in markers in addition to the
    # tradition-specific tags supplied by the YAML author.
    full_tags = list(user_tags)
    for required_tag in ("oracle", "built-in"):
        if required_tag not in full_tags:
            full_tags.insert(0, required_tag)

    stats = {
        "created": 0, "replaced": 0, "chunks": 0,
        "skipped": 0, "errors": 0,
    }

    if dry_run:
        print(f"  would ingest: {full_slug}  ({len(entries)} entries)")
        for i, entry in enumerate(entries):
            label = entry.get("title", "?")
            print(f"    ›{i:>3}  {label}")
        stats["created"] = 1
        stats["chunks"] = len(entries)
        return stats

    # Live ingest.
    from precis._store import get_store
    store = get_store()

    existing = store.get(full_slug)
    if existing is not None and not overwrite:
        stats["skipped"] = 1
        return stats

    metadata = {
        "tradition": slug_short,
        "description": description,
        "ingested_at": _now_iso(),
    }

    blocks_payload: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        text = _render_chunk_body(entry)
        section_path = _section_path(entry)
        blocks_payload.append({
            "text": text,
            "block_type": "text",
            "section_path": section_path,
            "block_index": i,
            "page": 0,
        })

    try:
        if existing is not None and overwrite:
            # Replace: drop existing blocks, recreate from scratch.
            from acatome_store.models import Block, Ref
            from sqlalchemy import delete as sa_delete
            from sqlalchemy import select

            with store._Session() as session:
                ref_row = session.execute(
                    select(Ref).where(Ref.slug == full_slug)
                ).scalar_one_or_none()
                if ref_row is None:
                    stats["errors"] += 1
                    return stats
                session.execute(
                    sa_delete(Block).where(Block.ref_id == ref_row.id)
                )
                # Re-insert blocks.
                for i, blk in enumerate(blocks_payload):
                    block = Block(
                        node_id=f"{full_slug}-b{i:04d}",
                        profile="default",
                        ref_id=ref_row.id,
                        page=0,
                        block_index=i,
                        block_type="text",
                        text=blk["text"],
                        section_path=json.dumps(blk["section_path"]),
                    )
                    session.add(block)
                ref_row.title = title
                session.commit()

            store.update_ref_metadata(full_slug, metadata, merge=True)
            # Update tags in-place — store doesn't have a single-call
            # tag-replace surface; use the metadata path via update +
            # refresh.
            try:
                store.set_tags(full_slug, full_tags)
            except AttributeError:
                # Older store — silently fall through; tags from the
                # original create stick.
                pass
            stats["replaced"] = 1
            stats["chunks"] = len(blocks_payload)
        else:
            store.create_ref(
                slug=full_slug,
                corpus_id="oracle",
                title=title,
                metadata=metadata,
                tags=full_tags,
                blocks=blocks_payload,
            )
            stats["created"] = 1
            stats["chunks"] = len(blocks_payload)
    except Exception as exc:
        log.error("ingest failed for %s: %s", full_slug, exc)
        stats["errors"] += 1

    return stats


def ingest_directory(
    src_dir: Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Ingest every ``*.yaml`` in ``src_dir``.

    Returns aggregate stats and a per-file breakdown.
    """
    yaml_files = sorted(src_dir.glob("*.yaml")) + sorted(src_dir.glob("*.yml"))
    if not yaml_files:
        raise FileNotFoundError(f"no YAML files in {src_dir}")

    aggregate = {
        "files": 0, "created": 0, "replaced": 0, "chunks": 0,
        "skipped": 0, "errors": 0, "per_file": {},
    }
    for yp in yaml_files:
        stats = ingest_paper(yp, overwrite=overwrite, dry_run=dry_run)
        aggregate["files"] += 1
        for k in ("created", "replaced", "chunks", "skipped", "errors"):
            aggregate[k] += stats[k]
        aggregate["per_file"][yp.name] = stats
    return aggregate


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="precis-ingest-oracle",
        description=(
            "Load oracle wisdom papers into the precis 'oracle' corpus. "
            "One paper per tradition (iching, chengyu, stoic, …); one "
            "chunk per entry."
        ),
    )
    parser.add_argument(
        "--from", dest="src",
        help=(
            "directory of oracle YAML files (default: bundled "
            "data/oracle/)"
        ),
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help=(
            "replace existing refs (drops & re-inserts all chunks); "
            "default is to skip already-ingested traditions"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="don't write — show what would be ingested",
    )
    args = parser.parse_args(argv)

    src = Path(args.src) if args.src else _bundled_oracle_dir()
    if src is None or not src.is_dir():
        print(
            "ERROR: oracle YAML directory not found.  Pass --from <dir> "
            "or ensure the package was installed with the data/oracle/ "
            "directory.",
            file=sys.stderr, flush=True,
        )
        return 2

    print(f"Ingesting oracle papers from {src} (dry_run={args.dry_run}) …")
    try:
        agg = ingest_directory(
            src, overwrite=args.overwrite, dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print()
    print(f"Files processed: {agg['files']}")
    print(
        f"  created={agg['created']}  replaced={agg['replaced']}  "
        f"skipped={agg['skipped']}  errors={agg['errors']}  "
        f"total chunks={agg['chunks']}"
    )
    print()
    for fname, stats in agg["per_file"].items():
        print(
            f"  {fname:<30}  "
            f"created={stats['created']} replaced={stats['replaced']} "
            f"chunks={stats['chunks']} skipped={stats['skipped']} "
            f"errors={stats['errors']}"
        )

    return 0 if agg["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(_cli_main(sys.argv[1:]))
