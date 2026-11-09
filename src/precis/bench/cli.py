"""``python -m precis.bench`` — the subcommands behind ``scripts/bench``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from precis.bench import corpus

NOT_BUILT = "not built yet, see docs/backlog/memory-ablation-bench.md"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scripts/bench", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="ingest the fixture corpus into the dev DB")
    ing.add_argument(
        "--set", dest="set_name", choices=sorted(corpus.SET_SIZES), required=True
    )
    ing.add_argument("--manifest", type=Path, default=None)
    ing.add_argument("--cache", type=Path, default=None)

    man = sub.add_parser(
        "manifest", help="rebuild bench/corpus/manifest.json (maintainers)"
    )
    man.add_argument("--check", action="store_true", help="verify instead of writing")
    man.add_argument("--manifest", type=Path, default=None)
    man.add_argument("--cache", type=Path, default=None)

    sub.add_parser("run", help="not built yet")
    sub.add_parser("report", help="not built yet")
    return ap


def _ingest(args: argparse.Namespace) -> int:
    import psycopg

    dsn = os.environ.get("PRECIS_DATABASE_URL", "")
    if not dsn:
        print("PRECIS_DATABASE_URL is unset; run via scripts/bench", file=sys.stderr)
        return 2
    corpus.assert_throwaway_dsn(dsn)
    manifest = corpus.load_manifest(args.manifest)
    with psycopg.connect(dsn, autocommit=True) as conn:
        report = corpus.ingest_set(
            conn, args.set_name, manifest=manifest, cache=args.cache
        )
    print(report.line())
    print(
        "embeddings and derived passes are the worker's job (precis worker --only embed)"
    )
    return 0


def _manifest(args: argparse.Namespace) -> int:
    path = args.manifest or corpus.default_manifest_path()
    archive = corpus.fetch_archive(args.cache, sha256=corpus.ARCHIVE_SHA256)
    built = corpus.build_manifest(
        corpus.load_archive(archive),
        archive_bytes=archive.stat().st_size,
    )
    text = corpus.manifest_text(built)
    if args.check:
        have = path.read_text(encoding="utf-8") if path.is_file() else ""
        if have != text:
            print(
                f"{path} differs from the manifest rebuilt from the archive",
                file=sys.stderr,
            )
            return 1
        print(f"{path} matches (quick={len(built['quick'])} full={len(built['full'])})")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path} (quick={len(built['quick'])} full={len(built['full'])})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd in ("run", "report"):
        print(NOT_BUILT)
        return 2
    try:
        return _ingest(args) if args.cmd == "ingest" else _manifest(args)
    except corpus.BenchCorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
