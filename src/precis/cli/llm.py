"""``precis llm`` — drive the LLM catalog from the CLI (docs/proposals/llm-catalog.md).

    precis llm seed          # mint/refresh a card per model precis runs
    precis llm reconcile      # run one reconcile pass now (facts + drift), forced
    precis llm list           # list the catalog cards

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


def _cmd_seed(store: Store) -> None:
    from precis.llm_catalog import seed_default_cards

    for model_id, ref_id, created in seed_default_cards(store):
        print(f"{'created' if created else 'refreshed'} llm lm{ref_id} — {model_id}")


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
    if sel.next_better:
        print(f"next better: {sel.next_better}")


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.database_url))
    if args.llm_cmd == "seed":
        _cmd_seed(store)
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
