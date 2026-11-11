"""``python -m precis.bench`` — the subcommands behind ``scripts/bench``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from precis.bench import corpus, raw, runner
from precis.config import load_config


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

    tsk = sub.add_parser(
        "tasks", help="extract the QASPER span-gold task set (bench/tasks/)"
    )
    tsk.add_argument(
        "--set", dest="set_name", choices=sorted(corpus.SET_SIZES), required=True
    )
    tsk.add_argument("--manifest", type=Path, default=None)
    tsk.add_argument("--cache", type=Path, default=None)
    tsk.add_argument(
        "--out", type=Path, default=None, help="default bench/tasks/qasper-<set>.jsonl"
    )
    tsk.add_argument(
        "--check",
        action="store_true",
        help="verify the file on disk equals a fresh extraction instead of writing",
    )

    fil = sub.add_parser(
        "fill",
        help="run the free local derived passes (embeddings, keywords) on the dev DB",
    )
    fil.add_argument(
        "--embedder",
        default="bge-m3",
        help="embedder name: bge-m3 (real, downloads once) or mock (hash)",
    )
    fil.add_argument("--no-keywords", action="store_true")

    run = sub.add_parser(
        "run", help="run the scripted ablation matrix on the ingested set"
    )
    run.add_argument(
        "--set", dest="set_name", choices=sorted(corpus.SET_SIZES), default="quick"
    )
    run.add_argument(
        "--arms",
        default=None,
        help="comma list of arms (default: all); see precis.bench.runner.ALL_ARMS",
    )
    run.add_argument("--seed", type=int, default=runner.PLANTED_SEED)
    run.add_argument("--out", type=Path, default=None)
    run.add_argument("--embedder", default="bge-m3")
    run.add_argument("--manifest", type=Path, default=None)
    run.add_argument("--tasks", type=Path, default=None)
    run.add_argument(
        "--planted",
        type=int,
        default=runner.PLANTED_N,
        help="planted-fact tasks to generate and run (0 disables)",
    )
    run.add_argument("--budget", type=int, default=runner.TOKEN_BUDGET)
    run.add_argument(
        "--no-fill",
        action="store_true",
        help="skip the embeddings/keywords pass over chunks that lack them",
    )
    run.add_argument("--limit", type=int, default=None, help="first N QASPER tasks")
    run.add_argument(
        "--caller",
        choices=runner.CALLERS,
        default=runner.DEFAULT_CALLER,
        help="scripted caller: triage (default) or ladder (the whole-ladder climb)",
    )

    swp = sub.add_parser(
        "sweep",
        help="pick the triage threshold on planted dev seeds (use a throwaway DB of its own)",
    )
    swp.add_argument(
        "--set", dest="set_name", choices=sorted(corpus.SET_SIZES), default="quick"
    )
    swp.add_argument(
        "--seeds",
        default=",".join(str(s) for s in runner.SWEEP_SEEDS),
        help="comma list of planted dev seeds (not the reported seed)",
    )
    swp.add_argument(
        "--thresholds",
        default=",".join(f"{t:g}" for t in runner.SWEEP_THRESHOLDS),
        help="comma list of triage thresholds",
    )
    swp.add_argument(
        "--caller",
        choices=("triage", "rowkw"),
        default="triage",
        help="caller to sweep",
    )
    swp.add_argument("--embedder", default="bge-m3")
    swp.add_argument("--manifest", type=Path, default=None)
    swp.add_argument("--tasks", type=Path, default=None)
    swp.add_argument("--planted", type=int, default=runner.PLANTED_N)
    swp.add_argument("--budget", type=int, default=runner.TOKEN_BUDGET)

    rep = sub.add_parser(
        "report",
        help="print the markdown table for a results file; several files "
        "(one per caller) are also compared side by side",
    )
    rep.add_argument("results", type=Path, nargs="+")

    raw_p = sub.add_parser(
        "raw",
        help="read a results file's raw log in bounded slices: one block per "
        "task x arm row with its per-call token log",
    )
    raw_p.add_argument("results", type=Path)
    raw_p.add_argument(
        "--arm",
        action="append",
        choices=list(runner.ALL_ARMS),
        metavar="ARM",
        help="only this arm (repeatable); default: every arm",
    )
    raw_p.add_argument("--task", default=None, help="only this task id")
    raw_p.add_argument("--source", choices=("qasper", "planted"), default=None)
    raw_p.add_argument("--abstention", action="store_true", help="abstention rows only")
    raw_p.add_argument("--offset", type=int, default=0)
    raw_p.add_argument("--limit", type=int, default=raw.DEFAULT_LIMIT)
    return ap


def _ingest(args: argparse.Namespace) -> int:
    import psycopg

    dsn = load_config().database_url or ""
    if not dsn:
        print("PRECIS_DATABASE_URL is unset; run via scripts/bench", file=sys.stderr)
        return 2
    corpus.assert_throwaway_dsn(dsn)
    manifest = corpus.load_manifest(args.manifest)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
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


def _tasks(args: argparse.Namespace) -> int:
    from precis.bench import qasper_tasks

    man = corpus.load_manifest(args.manifest)
    records = man[args.set_name]
    if corpus.corpus_sha256(records) != man["corpus_sha256"][args.set_name]:
        raise corpus.BenchCorpusError(
            f"manifest {args.set_name!r} does not match its corpus_sha256"
        )
    archive = corpus.fetch_archive(
        args.cache, sha256=man["dataset"]["archive"]["sha256"]
    )
    rows, report = qasper_tasks.build_tasks(records, corpus.load_archive(archive))
    text = qasper_tasks.tasks_text(rows)
    path = args.out or qasper_tasks.default_tasks_path(args.set_name)
    print(report.table())
    if args.check:
        have = path.read_text(encoding="utf-8") if path.is_file() else ""
        if have != text:
            print(f"{path} differs from a fresh extraction", file=sys.stderr)
            return 1
        print(f"{path} matches ({len(rows)} rows)")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path} ({len(rows)} rows, {len(text.encode('utf-8'))} bytes)")
    return 0


def _dsn() -> str:
    dsn = load_config().database_url or ""
    if not dsn:
        raise corpus.BenchCorpusError(
            "PRECIS_DATABASE_URL is unset; run via scripts/bench"
        )
    corpus.assert_throwaway_dsn(dsn)
    return dsn


def _fill(args: argparse.Namespace) -> int:
    from precis.bench import layers
    from precis.embedder import make_embedder
    from precis.store import Store

    store = Store.connect(_dsn())
    try:
        embedder = make_embedder(args.embedder, dim=store.embedding_dim())
        totals = layers.fill(store, embedder, keywords=not args.no_keywords)
    finally:
        store.close()
    print(json.dumps({"embedder": args.embedder, **totals}))
    return 0


def _run(args: argparse.Namespace) -> int:
    # A scripted run is thousands of searches: don't book a search trace row
    # per call into the dev DB.
    os.environ.setdefault("PRECIS_SEARCH_TRACES", "0")
    path = runner.run_cli(
        set_name=args.set_name,
        arms=None if args.arms is None else [a.strip() for a in args.arms.split(",")],
        seed=args.seed,
        out=args.out,
        embedder_name=args.embedder,
        manifest_path=args.manifest,
        tasks_path=args.tasks,
        planted_n=args.planted,
        budget=args.budget,
        limit=args.limit,
        dsn=_dsn(),
        fill=not args.no_fill,
        caller=args.caller,
    )
    print(f"wrote {path}")
    return 0


def _sweep(args: argparse.Namespace) -> int:
    os.environ.setdefault("PRECIS_SEARCH_TRACES", "0")
    print(
        runner.sweep_cli(
            set_name=args.set_name,
            seeds=[int(s) for s in args.seeds.split(",") if s.strip()],
            thresholds=[float(t) for t in args.thresholds.split(",") if t.strip()],
            embedder_name=args.embedder,
            manifest_path=args.manifest,
            tasks_path=args.tasks,
            planted_n=args.planted,
            budget=args.budget,
            dsn=_dsn(),
            caller=args.caller,
        )
    )
    return 0


def _report(args: argparse.Namespace) -> int:
    runs = [runner.load_results(p) for p in args.results]
    for run in runs:
        print(runner.report_markdown(run))
    if len(runs) > 1:
        print(runner.compare_markdown(runs))
    return 0


def _raw(args: argparse.Namespace) -> int:
    results = runner.load_results(args.results)
    try:
        print(
            raw.raw_markdown(
                results,
                arms=args.arm or None,
                task=args.task,
                source=args.source,
                abstention=args.abstention,
                offset=args.offset,
                limit=args.limit,
                results_path=args.results,
            )
        )
    except ValueError as exc:
        raise runner.BenchRunError(str(exc)) from exc
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "tasks":
            return _tasks(args)
        if args.cmd == "fill":
            return _fill(args)
        if args.cmd == "run":
            return _run(args)
        if args.cmd == "sweep":
            return _sweep(args)
        if args.cmd == "report":
            return _report(args)
        if args.cmd == "raw":
            return _raw(args)
        return _ingest(args) if args.cmd == "ingest" else _manifest(args)
    except (corpus.BenchCorpusError, runner.BenchRunError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
