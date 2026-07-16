"""``precis quest`` — drive the quest layer from the CLI.

    precis quest tick 7              # run one research tick against quest 7
    precis quest tick 7 --dry-run    # assemble + print the tick context, no LLM
    precis quest dossier 7           # print quest 7's living dossier
    precis quest gaps 7              # print quest 7's gaps + health

The autonomous loop (rung 4d) is dark by default; ``tick`` is the manual, one-
shot driver — explicit human intent, so it runs regardless of
``PRECIS_QUEST_LOOP_ENABLED``.
"""

from __future__ import annotations

import argparse
from typing import Any

from precis.cli._common import resolve_dsn
from precis.store import Store


def add_parser(subparsers: Any) -> None:
    p = subparsers.add_parser("quest", help="Quest layer — strivings above the work.")
    qsub = p.add_subparsers(dest="quest_cmd", required=True)

    t = qsub.add_parser("tick", help="Run one quest research tick (slice 4).")
    t.add_argument("id", type=int, help="Quest ref id.")
    t.add_argument(
        "--dry-run",
        action="store_true",
        help="Assemble + print the tick context only; make no LLM call.",
    )
    t.add_argument(
        "--tier",
        default=None,
        help="LLM tier (e.g. cloud-small, local-small, cloud-super).",
    )
    t.add_argument(
        "--compute",
        action="store_true",
        help="Materialise proposals into structure candidates + dispatch relax "
        "sims (the GPU compute lane). Off by default.",
    )
    t.add_argument("--database-url", default=None, help="Postgres DSN override.")

    d = qsub.add_parser("dossier", help="Print a quest's dossier.")
    d.add_argument("id", type=int, help="Quest ref id.")
    d.add_argument("--database-url", default=None, help="Postgres DSN override.")

    g = qsub.add_parser("gaps", help="Print a quest's gaps + health.")
    g.add_argument("id", type=int, help="Quest ref id.")
    g.add_argument("--database-url", default=None, help="Postgres DSN override.")

    f = qsub.add_parser("frontier", help="Print a quest's Pareto frontier.")
    f.add_argument("id", type=int, help="Quest ref id.")
    f.add_argument("--database-url", default=None, help="Postgres DSN override.")

    r = qsub.add_parser(
        "run", help="Allocator: pick the best active quest + tick it once."
    )
    r.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Weekly compute budget (overrides PRECIS_QUEST_WEEKLY_BUDGET).",
    )
    r.add_argument(
        "--force",
        action="store_true",
        help="Run even if PRECIS_QUEST_LOOP_ENABLED is unset.",
    )
    r.add_argument(
        "--no-compute",
        action="store_true",
        help="Reason only; do not dispatch sims for the picked quest.",
    )
    r.add_argument("--database-url", default=None, help="Postgres DSN override.")


def _cmd_tick(store: Store, args: argparse.Namespace) -> None:
    from precis.quest.tick import build_tick_prompt, run_quest_tick

    if args.dry_run:
        qref = store.get_ref(kind="quest", id=args.id)
        if qref is None:
            print(f"quest {args.id}: not found")
            return
        print(build_tick_prompt(store, qref))
        return

    outcome = run_quest_tick(store, args.id, tier=args.tier, compute=args.compute)
    msg = (
        f"quest {outcome.quest_id}: tick {outcome.status} — "
        f"{outcome.logbook_added} logbook entr"
        f"{'y' if outcome.logbook_added == 1 else 'ies'}, "
        f"dossier {'rewritten' if outcome.dossier_rewritten else 'unchanged'}"
    )
    if args.compute:
        msg += (
            f", {outcome.candidates_created} candidate(s), "
            f"{outcome.sims_dispatched} sim(s), "
            f"{outcome.results_harvested} result(s), {outcome.ruled_out} ruled-out"
        )
    if outcome.cost_usd:
        msg += f", ${outcome.cost_usd:.4f}"
    print(f"{msg} ({outcome.note})")


def _cmd_run(store: Store, args: argparse.Namespace) -> None:
    from precis.quest.allocator import run_allocator_pass

    summary = run_allocator_pass(
        store,
        enabled=True if args.force else None,
        total_budget=args.budget,
        compute=not args.no_compute,
    )
    if not summary["enabled"]:
        print(
            "quest run: PRECIS_QUEST_LOOP_ENABLED is unset — the autonomous loop "
            "is dark. Pass --force to run one step anyway."
        )
        return
    if summary["picked"] is None:
        print(f"quest run: cooled {summary['cooled']}, no quest eligible to tick")
        return
    print(
        f"quest run: cooled {summary['cooled']}, picked quest {summary['picked']} "
        f"(score {summary['score']}) → tick {summary['status']}"
    )


def _cmd_frontier(store: Store, args: argparse.Namespace) -> None:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    h = QuestHandler(hub=Hub(store=store))
    print(h.get(id=args.id, view="frontier").body)


def _cmd_dossier(store: Store, args: argparse.Namespace) -> None:
    from precis.quest.dossier import read_dossier

    did, _handle, text = read_dossier(store, args.id)
    if did is None:
        print(f"quest {args.id}: no dossier yet — run `precis quest tick {args.id}`")
        return
    print(text or "(dossier is empty)")


def _cmd_gaps(store: Store, args: argparse.Namespace) -> None:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    h = QuestHandler(hub=Hub(store=store))
    print(h.get(id=args.id, view="gaps").body)


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.database_url))
    if args.quest_cmd == "tick":
        _cmd_tick(store, args)
    elif args.quest_cmd == "dossier":
        _cmd_dossier(store, args)
    elif args.quest_cmd == "gaps":
        _cmd_gaps(store, args)
    elif args.quest_cmd == "frontier":
        _cmd_frontier(store, args)
    elif args.quest_cmd == "run":
        _cmd_run(store, args)
