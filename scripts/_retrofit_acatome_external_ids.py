"""Retrofit `header.external_ids` on existing `.acatome` bundles.

Walks every `.acatome` bundle under ``~/.acatome/papers/`` (override
with ``--bundle-dir``), looks up the matching paper ref in precis via
any canonical identifier in the bundle header (``doi`` → ``arxiv_id``
→ ``s2_id`` → ``pdf_hash``), reads the full ``ref_identifiers``
cluster for that ref, and writes the S2-shaped ``externalIds`` dict
back into ``header.external_ids``.

Use after the live precis DB has been swept by
``enrich-paper-identifiers`` so the alias table is fully populated —
the sweep is the cache this script reads from. Bundles whose headers
already carry a non-empty ``external_ids`` are skipped (idempotent).

Atomic write: rewrite to a sibling tempfile under the bundle's
parent dir, ``fsync``, then ``os.replace`` over the original. A crash
mid-write leaves the original intact.

Dry-run by default (``--apply`` writes). ``--limit`` caps how many
bundles get touched. ``--re-retrofit`` ignores the "already populated"
guard and rewrites anyway.

Env: ``PRECIS_DATABASE_URL`` (default points at local cluster).
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_BUNDLE_DIR = Path.home() / ".acatome" / "papers"
DEFAULT_DB = "postgresql://acatome:acatome@127.0.0.1:5432/precis"

# Map from `ref_identifiers.scheme` → Semantic Scholar `externalIds`
# key. Mirrors `acatome_meta.semantic_scholar._normalize`'s reverse
# direction. ``s2`` and ``pdfsha256`` are intentionally excluded —
# they go into ``header.s2_id`` / ``header.pdf_hash`` which are
# separate fields, not part of the externalIds cluster.
_SCHEME_TO_S2_KEY: dict[str, str] = {
    "doi": "DOI",
    "arxiv": "ArXiv",
    "pubmed": "PubMed",
    "pmc": "PubMedCentralID",
    "mag": "MAG",
    "dblp": "DBLP",
    "corpusid": "CorpusId",
    "openalex": "OpenAlex",
    "acl": "ACL",
}

log = logging.getLogger("retrofit-acatome-external-ids")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE_DIR)
    ap.add_argument(
        "--database-url", default=os.environ.get("PRECIS_DATABASE_URL", DEFAULT_DB)
    )
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually write bundles (default: dry-run, just count).",
    )
    ap.add_argument(
        "--re-retrofit",
        action="store_true",
        help="Rewrite even bundles whose header already has external_ids.",
    )
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bundle_dir: Path = args.bundle_dir
    if not bundle_dir.is_dir():
        log.error("bundle dir does not exist: %s", bundle_dir)
        return 2

    bundles = sorted(bundle_dir.glob("*/*.acatome"))
    if args.limit:
        bundles = bundles[: args.limit]
    log.info("found %d bundles under %s", len(bundles), bundle_dir)

    import psycopg

    conn = psycopg.connect(args.database_url, autocommit=True)
    try:
        stats = {
            "scanned": 0,
            "skipped_already_populated": 0,
            "skipped_no_canonical_id": 0,
            "skipped_no_ref_match": 0,
            "skipped_no_aliases": 0,
            "would_update": 0,
            "updated": 0,
            "errors": 0,
        }
        for bundle_path in bundles:
            stats["scanned"] += 1
            try:
                _process_one(bundle_path, conn, args, stats)
            except Exception as exc:  # bundle-local; keep going
                log.warning("error on %s: %s", bundle_path.name, exc)
                stats["errors"] += 1
    finally:
        conn.close()

    log.info("=" * 60)
    for k, v in stats.items():
        log.info("  %-30s %d", k, v)
    if not args.apply and stats["would_update"]:
        log.info("dry-run — pass --apply to actually rewrite bundles")
    return 0


def _process_one(
    bundle_path: Path,
    conn: Any,  # psycopg connection
    args: argparse.Namespace,
    stats: dict[str, int],
) -> None:
    """Read one bundle, look up its alias cluster, optionally rewrite.

    Mutates *stats* with the outcome counter for this bundle.
    """
    with gzip.open(bundle_path, "rt", encoding="utf-8") as f:
        bundle = json.load(f)

    header = bundle.get("header", {})
    existing = header.get("external_ids") or {}

    if existing and not args.re_retrofit:
        stats["skipped_already_populated"] += 1
        return

    # Pick the most reliable canonical id present in the bundle.
    # DOI > arXiv > S2 paperId > pdf_hash (in practice the alias table
    # has every ref reachable from any of these — the order just biases
    # which lookup we try first).
    candidates: list[tuple[str, str]] = []
    if header.get("doi"):
        candidates.append(("doi", str(header["doi"]).strip().lower()))
    if header.get("arxiv_id"):
        candidates.append(("arxiv", str(header["arxiv_id"]).strip().lower()))
    if header.get("s2_id"):
        candidates.append(("s2", str(header["s2_id"]).strip().lower()))
    if header.get("pdf_hash"):
        candidates.append(("pdfsha256", str(header["pdf_hash"]).strip().lower()))

    if not candidates:
        stats["skipped_no_canonical_id"] += 1
        log.debug("no canonical id in %s — skipping", bundle_path.name)
        return

    ref_id: int | None = None
    for scheme, value in candidates:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ri.ref_id FROM ref_identifiers ri "
                "JOIN refs r ON r.id = ri.ref_id "
                "WHERE ri.scheme = %s AND ri.value = %s "
                "  AND r.kind = 'paper' AND r.deleted_at IS NULL",
                (scheme, value),
            )
            row = cur.fetchone()
        if row:
            ref_id = int(row[0])
            log.debug(
                "matched %s via %s=%s -> ref %d",
                bundle_path.name,
                scheme,
                value,
                ref_id,
            )
            break

    if ref_id is None:
        stats["skipped_no_ref_match"] += 1
        log.debug(
            "no ref match for %s (tried %s)",
            bundle_path.name,
            [c[0] for c in candidates],
        )
        return

    # Pull every alias row for this ref and convert to S2 externalIds shape.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scheme, value FROM ref_identifiers WHERE ref_id = %s",
            (ref_id,),
        )
        alias_rows = cur.fetchall()

    new_external_ids: dict[str, str] = {}
    s2_paper_id: str | None = None
    for scheme, value in alias_rows:
        s2_key = _SCHEME_TO_S2_KEY.get(scheme)
        if s2_key:
            new_external_ids[s2_key] = value
        elif scheme == "s2" and not s2_paper_id:
            # Adopt the first s2 paperId seen — refs occasionally have
            # multiple legitimate clusters (cross-listed preprints).
            s2_paper_id = value

    if not new_external_ids and not s2_paper_id:
        stats["skipped_no_aliases"] += 1
        return

    # Decide what would change.
    changed = False
    if new_external_ids and (existing or {}) != new_external_ids:
        changed = True
    # Only fix s2_id when the bundle's stored value is empty or doesn't
    # match any of the ref's known S2 paperIds (the bogus-cluster case
    # we hit during dedup).
    s2_alias_values = {v for s, v in alias_rows if s == "s2"}
    bundle_s2 = (header.get("s2_id") or "").strip().lower()
    if s2_paper_id and (
        not bundle_s2 or (s2_alias_values and bundle_s2 not in s2_alias_values)
    ):
        changed = True

    if not changed:
        stats["skipped_already_populated"] += 1
        return

    if not args.apply:
        stats["would_update"] += 1
        log.info(
            "[dry] %s: would set external_ids=%s%s",
            bundle_path.name,
            sorted(new_external_ids.keys()),
            f" + s2_id={s2_paper_id[:12]}…" if s2_paper_id and not bundle_s2 else "",
        )
        return

    # Apply.
    if new_external_ids:
        header["external_ids"] = new_external_ids
    if s2_paper_id and (
        not bundle_s2 or (s2_alias_values and bundle_s2 not in s2_alias_values)
    ):
        header["s2_id"] = s2_paper_id
    bundle["header"] = header

    _atomic_write_bundle(bundle_path, bundle)
    stats["updated"] += 1
    log.info(
        "updated %s (ref=%d, schemes=%s)",
        bundle_path.name,
        ref_id,
        sorted(new_external_ids.keys()),
    )


def _atomic_write_bundle(path: Path, data: dict[str, Any]) -> None:
    """Write a bundle dict back to *path* atomically (gzipped JSON).

    Tempfile sibling + ``fsync`` + ``os.replace``. A crash mid-write
    leaves the original intact; readers never see a partial bundle.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    sys.exit(main())
