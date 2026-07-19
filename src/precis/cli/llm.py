"""``precis llm`` — drive the LLM catalog from the CLI (docs/proposals/llm-catalog.md).

    precis llm seed             # mint/refresh a card per model precis runs
    precis llm seed --frontier  # + the curated frontier open-weight ladder (OSS)
    precis llm seed --all       # both default + frontier
    precis llm reconcile        # run one reconcile pass now (facts + drift), forced
    precis llm list             # list the catalog cards
    precis llm cost --days 7    # mine llm_call_log: per lane/pass/ref spend + wall-clock

Slice 1 is read-only: the catalog is machine-maintained (seed + reconcile), and
agents *read* it via search/get. The whole thing ships dark — an empty catalog is
byte-identical to today's behaviour.
"""

from __future__ import annotations

import argparse
from typing import Any

from precis.cli._common import resolve_dsn
from precis.store import Store


def add_parser(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "llm", help="LLM catalog — model choice as a queryable resource."
    )
    lsub = p.add_subparsers(dest="llm_cmd", required=True)

    s = lsub.add_parser("seed", help="Mint/refresh a card per model precis runs.")
    s.add_argument("--database-url", default=None, help="Postgres DSN override.")
    s.add_argument(
        "--frontier",
        action="store_true",
        help="Seed the curated frontier open-weight ladder (Opus→Haiku) instead "
        "of the models precis already runs.",
    )
    s.add_argument(
        "--all",
        action="store_true",
        help="Seed both the default (models precis runs) and frontier ladders.",
    )

    r = lsub.add_parser(
        "reconcile", help="Run one reconcile pass now (refresh facts + flag drift)."
    )
    r.add_argument("--database-url", default=None, help="Postgres DSN override.")

    ls = lsub.add_parser("list", help="List the catalog cards.")
    ls.add_argument("--database-url", default=None, help="Postgres DSN override.")

    t = lsub.add_parser("tote", help="Show a model's realized-telemetry rollup.")
    t.add_argument("model", help="Model id (e.g. claude-opus-4-8).")
    t.add_argument("--database-url", default=None, help="Postgres DSN override.")

    o = lsub.add_parser(
        "observe", help="Derive + record observed axes from llm_call_log telemetry."
    )
    o.add_argument("model", help="Model id (e.g. claude-opus-4-8).")
    o.add_argument("--database-url", default=None, help="Postgres DSN override.")

    s = lsub.add_parser(
        "select", help="Deterministic requirement→model pick (the slice-4 policy)."
    )
    s.add_argument(
        "--tier",
        default="cloud-super",
        help="Tier floor / budget band + degrade target.",
    )
    s.add_argument("--axis", default=None, help="Dominant capability axis (e.g. code).")
    s.add_argument(
        "--min", type=int, default=1, help="Minimum ordinal on the axis (1..5)."
    )
    s.add_argument(
        "--window", type=int, default=None, help="Required input window (tokens)."
    )
    s.add_argument("--tools", action="store_true", help="Require tool support.")
    s.add_argument(
        "--structured", action="store_true", help="Require structured output."
    )
    s.add_argument("--database-url", default=None, help="Postgres DSN override.")

    ch = lsub.add_parser(
        "choose",
        help="Task→requirement (LLM judge) → model (policy) — the slice-5 loop.",
    )
    ch.add_argument("task", help="A description of the task to route.")
    ch.add_argument(
        "--tier", default="cloud-super", help="Tier floor / degrade target."
    )
    ch.add_argument("--database-url", default=None, help="Postgres DSN override.")

    co = lsub.add_parser(
        "cost",
        help="Mine llm_call_log — per lane / pass / ref / model: calls, real-$, "
        "wall-clock. Read-only. (All dispatch LLM lanes; non-LLM compute excluded.)",
    )
    co.add_argument(
        "--days", type=int, default=7, help="Trailing window in days (default 7)."
    )
    co.add_argument(
        "--by",
        default="transport",
        choices=["transport", "source", "ref", "model"],
        help="Group by lane (transport), pass (source), entity (ref), or model.",
    )
    co.add_argument(
        "--source", default=None, help="Restrict to one pass label (e.g. quest_tick)."
    )
    co.add_argument("--limit", type=int, default=40, help="Max rows (default 40).")
    co.add_argument("--database-url", default=None, help="Postgres DSN override.")

    ev = lsub.add_parser(
        "eval",
        help="Golden-task eval: run a model on precis's own tasks → measured-eval "
        "ordinals (slice 11). EXPENSIVE — runs real model calls.",
    )
    ev.add_argument("model", help="Candidate model id (e.g. qwen3.6-27b).")
    ev.add_argument(
        "--compare",
        default=None,
        metavar="MODEL_B",
        help="Second model — run both over the gold set and print an A/B table "
        "(implies --no-record).",
    )
    ev.add_argument(
        "--tier",
        default="cloud-small",
        help="Tier the candidate runs under (transport selection).",
    )
    ev.add_argument(
        "--gold", default=None, help="Gold-set JSON path (default: the seed set)."
    )
    ev.add_argument(
        "--no-record",
        action="store_true",
        help="Score + print only; do not write measured-eval ordinals to the card.",
    )
    ev.add_argument("--database-url", default=None, help="Postgres DSN override.")


def _cmd_seed(store: Store, *, frontier: bool = False, seed_all: bool = False) -> None:
    from precis.llm_catalog import seed_default_cards, seed_frontier_cards

    seeders = []
    if seed_all or not frontier:
        seeders.append(seed_default_cards)
    if seed_all or frontier:
        seeders.append(seed_frontier_cards)
    for seeder in seeders:
        for model_id, ref_id, created in seeder(store):
            verb = "created" if created else "refreshed"
            print(f"{verb} llm lm{ref_id} — {model_id}")


def _cmd_reconcile(store: Store) -> None:
    from precis.workers.llm_reconcile import run_llm_reconcile_pass

    res = run_llm_reconcile_pass(store, force=True)
    print(
        f"llm_reconcile: refreshed/flagged {res.ok} "
        f"(claimed={res.claimed}, failed={res.failed})"
    )


def _cmd_list(store: Store) -> None:
    cards = store.list_refs(kind="llm", limit=200)
    if not cards:
        print("no llm cards yet — run `precis llm seed`")
        return
    for c in cards:
        meta = c.meta or {}
        print(
            f"lm{c.id}  {meta.get('model_id', '?'):32}  "
            f"floor={meta.get('tier_floor', '?')}  "
            f"offerings={len(meta.get('offerings') or [])}"
        )


def _cmd_tote(store: Store, model: str) -> None:
    from precis.llm_catalog import llm_tote, llm_tote_by_source

    tote = llm_tote(store, model)
    print(f"tote — {model} (last 30d): {tote.calls} calls, ${tote.cost_usd:.4f}")
    if tote.error_rate is not None:
        print(f"  error rate: {tote.error_rate:.1%}")
    if tote.p50_duration_ms is not None:
        print(f"  p50 duration: {tote.p50_duration_ms:.0f} ms")
    for src, r in llm_tote_by_source(store, model):
        print(f"  {src}: {r.calls} calls, ${r.cost_usd:.4f}")


def _cmd_observe(store: Store, model: str) -> None:
    from precis.llm_catalog import record_observed_axes

    axes = record_observed_axes(store, model)
    if not axes:
        print(f"{model}: not enough telemetry to set observed axes (or no card)")
        return
    print(f"{model}: recorded observed axes {axes} (observed-telemetry)")


def _cmd_select(store: Store, args: argparse.Namespace) -> None:
    from precis.utils.llm.policy import Requirement, select_offering
    from precis.utils.llm.router import Tier

    req = Requirement(
        tier_floor=Tier(args.tier),
        axis=args.axis,
        min_ordinal=args.min,
        max_input=args.window,
        needs_tools=args.tools,
        needs_structured=args.structured,
    )
    sel = select_offering(store, req)
    src = "catalog" if sel.from_catalog else "tier-floor"
    print(f"model: {sel.model}  [{src}] — {sel.reason}")
    if sel.endpoint:
        e = sel.endpoint
        print(
            f"booked endpoint: {e.get('provider')} quant={e.get('quant')} "
            f"window={e.get('max_input')} ${e.get('price_in')}/1M in"
        )
    if sel.next_better:
        print(f"next better: {sel.next_better}")


def _cmd_choose(store: Store, args: argparse.Namespace) -> None:
    from precis.utils.llm.requirement import choose_model
    from precis.utils.llm.router import Tier

    req, sel = choose_model(store, args.task, tier_floor=Tier(args.tier))
    axis = f"{req.axis}≥{req.min_ordinal}" if req.axis else "no axis"
    print(f"requirement: {axis}, tools={req.needs_tools}, window={req.max_input}")
    src = "catalog" if sel.from_catalog else "tier-floor"
    print(f"model: {sel.model}  [{src}] — {sel.reason}")
    if sel.endpoint:
        e = sel.endpoint
        print(
            f"booked endpoint: {e.get('provider')} quant={e.get('quant')} "
            f"window={e.get('max_input')} ${e.get('price_in')}/1M in"
        )
    if sel.next_better:
        print(f"next better: {sel.next_better}")


def _fmt_int(n: int) -> str:
    """Human-terse thousands (1234567 → '1.2M', 12345 → '12.3k')."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _cmd_cost(store: Store, args: argparse.Namespace) -> None:
    from precis import route_log

    rows = route_log.spend_rollup(
        store,
        days=args.days,
        group_by=args.by,
        source=args.source,
        limit=args.limit,
    )
    scope = f" source={args.source}" if args.source else ""
    print(f"llm_call_log — last {args.days}d, by {args.by}{scope}")
    if not rows:
        print("  (no rows — the route-log started ~2026-07-14; widen --days or wait)")
        return
    # Natural units kept separate — real-$ is only the lanes that bill; chars are a
    # ~token volume proxy (÷4); wall is compute time (the local lane's real cost).
    print(
        f"  {'key':<22} {'calls':>7} {'real $':>9} {'chars(in/out)':>15} "
        f"{'wall':>8} {'err':>5}"
    )
    tot_calls = tot_usd = tot_wall = 0
    for r in rows:
        wall_h = r.wall_ms / 3_600_000
        chars = f"{_fmt_int(r.req_chars)}/{_fmt_int(r.resp_chars)}"
        print(
            f"  {r.key[:22]:<22} {r.calls:>7} {r.real_usd:>9.4f} {chars:>15} "
            f"{wall_h:>7.1f}h {r.errors:>5}"
        )
        tot_calls += r.calls
        tot_usd += r.real_usd
        tot_wall += r.wall_ms
    print(
        f"  {'TOTAL':<22} {tot_calls:>7} {tot_usd:>9.4f} {'':>15} "
        f"{tot_wall / 3_600_000:>7.1f}h"
    )
    print(
        "  note: covers all LLM lanes through dispatch — agentic/judge (full "
        "rows) +\n        corpus batch passes (llm_summarize/classify/glossary, "
        "lite rows). Non-LLM\n        compute (DFT/relax/fold, containers) is not "
        "here (never hits dispatch)."
    )


def _cmd_eval(store: Store, args: argparse.Namespace) -> None:
    from precis.llm_eval import compare as _compare
    from precis.llm_eval import run_eval
    from precis.utils.llm.router import Tier

    tier = Tier(args.tier)
    if args.compare:
        reports = _compare(
            store,
            model_a=args.model,
            model_b=args.compare,
            tier=tier,
            gold_path=args.gold,
            record=False,
        )
        axes = sorted({a for r in reports.values() for a in r.ordinals})
        print(f"{'axis':<24} {args.model:>16} {args.compare:>16}")
        for axis in axes:
            oa = reports[args.model].ordinals.get(axis, "—")
            ob = reports[args.compare].ordinals.get(axis, "—")
            print(f"{axis:<24} {oa!s:>16} {ob!s:>16}")
        skipped = reports[args.model].skipped
        if skipped:
            print(f"\nskipped ({len(skipped)}): " + "; ".join(skipped))
        return

    report = run_eval(
        store,
        model=args.model,
        tier=tier,
        gold_path=args.gold,
        record=not args.no_record,
    )
    print(f"golden eval — {args.model} (tier {args.tier})")
    for res in report.results:
        mark = "recorded" if res.recorded else "not recorded"
        print(
            f"  {res.axis:<24} ordinal {res.ordinal}  "
            f"(mean {res.mean_score:.2f} / {res.n} tasks) [{mark}]"
        )
    if report.skipped:
        print(f"  skipped ({len(report.skipped)}): " + "; ".join(report.skipped))


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.database_url))
    if args.llm_cmd == "seed":
        _cmd_seed(store, frontier=args.frontier, seed_all=args.all)
    elif args.llm_cmd == "reconcile":
        _cmd_reconcile(store)
    elif args.llm_cmd == "list":
        _cmd_list(store)
    elif args.llm_cmd == "tote":
        _cmd_tote(store, args.model)
    elif args.llm_cmd == "observe":
        _cmd_observe(store, args.model)
    elif args.llm_cmd == "select":
        _cmd_select(store, args)
    elif args.llm_cmd == "choose":
        _cmd_choose(store, args)
    elif args.llm_cmd == "cost":
        _cmd_cost(store, args)
    elif args.llm_cmd == "eval":
        _cmd_eval(store, args)
