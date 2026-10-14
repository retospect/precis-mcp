"""``precis mesh`` — local-mesh-upkeep slice 0 surfaces.

    precis mesh eval --set <jsonl> --endpoint-url <url> --model <id>
        [--judge-endpoint-url <url> --judge-model <id>] [--pack N]
    precis mesh usefulness [--database-url DSN | --prod]
    precis mesh retest <dumpA> <dumpB> [--campaign norr-her-meta]

``eval`` runs a frozen mesh-eval set against any OpenAI-compatible
endpoint (a local vLLM/llama-swap bench or a hosted arm) and prints the
per-lane accuracy + Wilson CI, judged acceptance, reviewer cost, and the
two bars. ``usefulness`` prints the mesh-health metrics read-only.
``retest`` recomputes the frontier's folded-key test-retest between two
taxonomy discovery dumps — the auto-apply bar's reference leg.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from precis.errors import BadInput


def add_parser(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "mesh", help="Mesh upkeep evals + usefulness metrics (local-mesh-upkeep)."
    )
    sub = p.add_subparsers(dest="mesh_cmd", required=True)

    ev = sub.add_parser(
        "eval",
        help="Score a model on a frozen mesh-eval set — per-lane accuracy + "
        "CI, judged acceptance, the two bars. EXPENSIVE: real model calls.",
    )
    ev.add_argument(
        "--set",
        dest="sets",
        action="append",
        required=True,
        metavar="JSONL",
        help="Frozen eval set (repeatable — one per lane).",
    )
    ev.add_argument("--endpoint-url", required=True, help="OpenAI-compatible base url.")
    ev.add_argument("--model", required=True, help="Served model id.")
    ev.add_argument(
        "--api-key-env",
        default=None,
        help="Env var holding the endpoint key (default: none/dummy).",
    )
    ev.add_argument("--pack", type=int, default=1, help="categorise hubs per call.")
    ev.add_argument("--limit", type=int, default=None, help="Items per set (smoke).")
    ev.add_argument(
        "--judge-endpoint-url",
        default=None,
        help="Judge endpoint (the frontier arm); enables the acceptance read.",
    )
    ev.add_argument("--judge-model", default=None, help="Judge model id.")
    ev.add_argument(
        "--judge-api-key-env", default=None, help="Env var holding the judge key."
    )
    ev.add_argument("--out", default=None, help="Write the per-item results JSON here.")

    u = sub.add_parser(
        "usefulness",
        help="Mesh-usefulness metrics: orphans, dup candidates, "
        "stale nodes, next-hop follow-through. Read-only.",
    )
    u.add_argument("--database-url", default=None, help="Postgres DSN override.")
    u.add_argument(
        "--prod",
        action="store_true",
        help="Read PROD via the agent_ro role (the secrets-file DSN, rewritten "
        "and verified read-only).",
    )
    u.add_argument("--stale-days", type=int, default=90)
    u.add_argument("--hop-days", type=int, default=30)

    rt = sub.add_parser(
        "retest",
        help="Folded-key test-retest between two discovery dumps "
        "(the frontier self-agreement reference).",
    )
    rt.add_argument(
        "dump_a", metavar="DUMP_A", help="Run dir holding discovered.jsonl."
    )
    rt.add_argument("dump_b", metavar="DUMP_B")
    rt.add_argument("--campaign", default="norr-her-meta")
    rt.add_argument("--top", type=int, default=12)


def _judge_fn(cfg: Any) -> Any:
    """Build a frontier judge: ask the judge endpoint to accept/reject a reply."""

    from precis.mesh_eval.runner import (
        CallResult,
        JudgeVerdict,
        endpoint_caller,
    )
    from precis.mesh_eval.sets import MeshItem

    caller = endpoint_caller(cfg)

    def _judge(item: MeshItem, response: str) -> JudgeVerdict:
        prompt = (
            "You are the reviewer for an automated graph-upkeep worker.\n\n"
            "TASK GIVEN TO THE WORKER:\n"
            f"{(item.prompt or '')[:6000]}\n\n"
            "WORKER'S REPLY:\n"
            f"{response[:4000]}\n\n"
            "The correct answer (gold):\n"
            f"{json.dumps(item.gold, ensure_ascii=False)[:4000]}\n\n"
            "Judge the worker's reply against the gold: does it get the "
            "materially same answer (same entities, relations, verdict — "
            "allowing different wording)? Reply with a JSON object and "
            'nothing else: {"accept": true} or {"accept": false, '
            '"reason": "<short>"}.'
        )
        res: CallResult = caller([{"role": "user", "content": prompt}], 512)
        if res.error:
            raise RuntimeError(f"judge call failed: {res.error}")
        from precis.mesh_eval.scorers import _last_json

        data = _last_json(res.text, (dict,))
        if not isinstance(data, dict) or not isinstance(data.get("accept"), bool):
            raise RuntimeError(f"judge reply unparseable: {res.text[:200]!r}")
        return JudgeVerdict(
            accepted=data["accept"],
            input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
            cost_usd=res.cost_usd,
        )

    return _judge


def _cmd_eval(args: argparse.Namespace) -> None:
    import os

    from precis.mesh_eval.runner import (
        EndpointConfig,
        endpoint_caller,
        render_report,
        run_set,
    )
    from precis.mesh_eval.sets import load_set

    api_key = os.environ.get(args.api_key_env, "dummy") if args.api_key_env else "dummy"
    cand = endpoint_caller(
        EndpointConfig(
            base_url=args.endpoint_url,
            model=args.model,
            api_key=api_key,
        )
    )
    judge = None
    if args.judge_endpoint_url:
        if not args.judge_model:
            raise BadInput("--judge-endpoint-url needs --judge-model")
        jkey = (
            os.environ.get(args.judge_api_key_env, "dummy")
            if args.judge_api_key_env
            else "dummy"
        )
        judge = _judge_fn(
            EndpointConfig(
                base_url=args.judge_endpoint_url,
                model=args.judge_model,
                api_key=jkey,
            )
        )

    reports = []
    references: dict[str, dict[str, Any]] = {}
    for path in args.sets:
        eset = load_set(path)
        references[eset.lane] = eset.reference
        reports.append(
            run_set(
                eset,
                cand,
                model=args.model,
                judge_fn=judge,
                limit=args.limit,
                pack=args.pack,
            )
        )
    print(render_report(reports, references=references))
    if args.out:
        out = Path(args.out)
        out.write_text(
            json.dumps(
                [
                    {
                        "lane": r.lane,
                        "model": r.model,
                        "n": r.n,
                        "errors": r.errors,
                        "accuracy": r.accuracy,
                        "ci": [r.ci_lo, r.ci_hi],
                        "n_judged": r.n_judged,
                        "n_accepted": r.n_accepted,
                        "per_item": [
                            {
                                "item_id": i.item_id,
                                "score": i.score,
                                "error": i.error,
                                "detail": i.detail,
                                "judge_accepted": i.judge_accepted,
                                "judge_error": i.judge_error,
                            }
                            for i in r.per_item
                        ],
                    }
                    for r in reports
                ],
                indent=1,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"per-item results -> {out}", file=sys.stderr)


def _cmd_usefulness(args: argparse.Namespace) -> None:
    from precis.mesh_eval.usefulness import collect_usefulness, format_usefulness

    if args.prod:
        # The secrets-file DSN, rewritten to agent_ro and verified on a real
        # connection (same path scripts/guide-web uses). Loaded by file path:
        # ``import precis_web`` pulls fastapi, absent in a non-web env.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "readonly_dsn",
            Path(__file__).resolve().parents[3]
            / "src"
            / "precis_web"
            / "readonly_dsn.py",
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        dsn_file = Path.home() / ".secrets" / "pw" / "PRECIS_DATABASE_URL"
        ro = mod.rewrite_dsn(dsn_file.read_text(encoding="utf-8"))
        mod.verify_read_only(ro)
        dsn = ro
    else:
        from precis.cli._common import resolve_dsn

        dsn = resolve_dsn(args.database_url)

    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rep = collect_usefulness(
            conn, stale_days=args.stale_days, hop_days=args.hop_days
        )
        conn.rollback()
    print(format_usefulness(rep))


def _cmd_retest(args: argparse.Namespace) -> None:
    from precis.mesh_eval.retest import folded_agreement, format_retest
    from precis.taxonomy.config import load_campaign

    config = load_campaign(args.campaign)
    r = folded_agreement(args.dump_a, args.dump_b, config.measurand_aliases)
    print(format_retest(r))


def run(args: argparse.Namespace) -> None:
    if args.mesh_cmd == "eval":
        _cmd_eval(args)
    elif args.mesh_cmd == "usefulness":
        _cmd_usefulness(args)
    elif args.mesh_cmd == "retest":
        _cmd_retest(args)
    else:  # pragma: no cover - argparse enforces required=True
        raise BadInput(f"unknown mesh subcommand {args.mesh_cmd!r}")
