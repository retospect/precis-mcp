"""``python -m precis.bench`` — the subcommands behind ``scripts/bench``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from precis.bench import corpus, figure, raw, runner
from precis.config import load_config


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scripts/bench", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="ingest the fixture corpus into the dev DB")
    ing.add_argument(
        "--set", dest="set_name", choices=sorted(corpus.ALL_SETS), required=True
    )
    ing.add_argument("--manifest", type=Path, default=None)
    ing.add_argument("--cache", type=Path, default=None)
    ing.add_argument(
        "--chunker",
        default=corpus.DEFAULT_CHUNKER,
        help="sentence-pysbd (default), section, or fixed-<tokens>",
    )

    var = sub.add_parser(
        "variant",
        help="provision a re-ingest arm's own throwaway database (``run`` "
        "does this itself; this command is for a separate pg_dump/warmup "
        "step before a long run)",
    )
    var.add_argument(
        "arm",
        metavar="ARM",
        help="re-ingest arm: "
        + ", ".join(v.arm for v in corpus.VARIANTS)
        + ", or distractor-<N>",
    )
    var.add_argument(
        "--set", dest="set_name", choices=sorted(corpus.SET_SIZES), default="quick"
    )
    var.add_argument("--manifest", type=Path, default=None)
    var.add_argument("--cache", type=Path, default=None)

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
        "--set", dest="set_name", choices=sorted(corpus.ALL_SETS), required=True
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
        help="comma list of arms (default: precis.bench.runner.ALL_ARMS); "
        "re-ingest arms chunk-sentence-pysbd, chunk-fixed-512, chunk-section, "
        "no-links and distractor-<N> each provision and run in their own "
        "throwaway database",
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
        help="scripted caller: triage (default), rowkw, rank-order (rowkw's "
        "unscored control), oracle (upper bound: opens the gold hit) or "
        "ladder (the whole-ladder climb)",
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
    rep.add_argument(
        "--figure",
        type=Path,
        default=None,
        metavar="OUT.svg|OUT.png",
        help="also write the recall@k vs median tokens-to-gold figure, one "
        "series per arm (and per file)",
    )

    idx = sub.add_parser(
        "index",
        help="list the held runs — stem, dates, set, caller, counts; "
        "--match filters by substring over the line or the file text "
        "(a date, a caller, a td<id> the file names)",
    )
    idx.add_argument(
        "--match",
        default=None,
        metavar="NEEDLE",
        help="only runs whose stem, index line or file text contains this",
    )
    idx.add_argument("--offset", type=int, default=0)
    idx.add_argument("--limit", type=int, default=raw.DEFAULT_LIMIT)

    raw_p = sub.add_parser(
        "raw",
        help="read a results file's raw log in bounded slices: one block per "
        "task x arm row with its per-call token log",
    )
    raw_p.add_argument("results", type=Path)
    raw_p.add_argument(
        "--arm",
        action="append",
        metavar="ARM",
        help="only this arm (repeatable); default: every arm — re-ingest "
        "arms (chunk-*, no-links, distractor-<N>) name themselves",
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
            conn,
            args.set_name,
            manifest=manifest,
            cache=args.cache,
            chunker=args.chunker,
        )
    print(report.line())
    print(
        "embeddings and derived passes are the worker's job (precis worker --only embed)"
    )
    return 0


def _variant(args: argparse.Namespace) -> int:
    variant = corpus.variant_for_arm(args.arm)
    if variant is None:
        raise corpus.BenchCorpusError(
            f"unknown re-ingest arm {args.arm!r}; known: "
            f"{', '.join(v.arm for v in corpus.VARIANTS)} and distractor-<N>"
        )
    manifest = corpus.load_manifest(args.manifest)
    dsn = corpus.variant_dsn(_dsn(), variant.arm, args.set_name, manifest)
    report = corpus.provision_variant(
        dsn,
        variant,
        set_name=args.set_name,
        manifest=manifest,
        cache=args.cache,
    )
    print(
        f"{args.arm}: {corpus.variant_dbname(dsn)} — "
        + ("already provisioned" if report["already"] else json.dumps(report))
    )
    return 0


def _manifest(args: argparse.Namespace) -> int:
    path = args.manifest or corpus.default_manifest_path()
    archive = corpus.fetch_archive(args.cache, sha256=corpus.ARCHIVE_SHA256)
    test_archive = corpus.fetch_archive(
        args.cache,
        url=corpus.TEST_ARCHIVE_URL,
        sha256=corpus.TEST_ARCHIVE_SHA256,
        name=corpus.TEST_ARCHIVE_NAME,
    )
    built = corpus.build_manifest(
        corpus.load_archive(archive),
        archive_bytes=archive.stat().st_size,
        heldout_papers=corpus.load_archive(test_archive, corpus.TEST_MEMBERS),
        heldout_archive_bytes=test_archive.stat().st_size,
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
        print(
            f"{path} matches (quick={len(built['quick'])} full={len(built['full'])} "
            f"heldout={len(built['heldout'])})"
        )
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(
        f"wrote {path} (quick={len(built['quick'])} full={len(built['full'])} "
        f"heldout={len(built['heldout'])})"
    )
    return 0


def _tasks(args: argparse.Namespace) -> int:
    from precis.bench import qasper_tasks

    man = corpus.load_manifest(args.manifest)
    records = man[args.set_name]
    if corpus.corpus_sha256(records) != man["corpus_sha256"][args.set_name]:
        raise corpus.BenchCorpusError(
            f"manifest {args.set_name!r} does not match its corpus_sha256"
        )
    papers = corpus.load_set_papers(man, args.set_name, args.cache)
    rows, report = qasper_tasks.build_tasks(records, papers)
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
    if args.figure is not None:
        if args.figure.suffix.lower() not in (".svg", ".png"):
            raise runner.BenchRunError("--figure takes a .svg or .png path")
        try:
            figure.write_figure(runs, args.figure)
        except ValueError as exc:
            raise runner.BenchRunError(str(exc)) from exc
        print(f"figure: {args.figure}")
    return 0


def _index(args: argparse.Namespace) -> int:
    try:
        print(
            raw.index_markdown(
                runner.default_results_dir(),
                match=args.match,
                offset=args.offset,
                limit=args.limit,
            )
        )
    except ValueError as exc:
        raise runner.BenchRunError(str(exc)) from exc
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
        if args.cmd == "variant":
            return _variant(args)
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
        if args.cmd == "index":
            return _index(args)
        return _ingest(args) if args.cmd == "ingest" else _manifest(args)
    except (corpus.BenchCorpusError, runner.BenchRunError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
