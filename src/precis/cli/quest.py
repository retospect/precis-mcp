"""``precis quest`` — drive the quest layer from the CLI.

    precis quest tick 7              # run one research tick against quest 7
    precis quest tick 7 --dry-run    # assemble + print the tick context, no LLM
    precis quest dossier 7           # print quest 7's living dossier
    precis quest gaps 7              # print quest 7's gaps + health
    precis quest status 7            # ops roll-up: logbook, candidates, sim
                                      # jobs, coordinator trail, LLM spend
    precis quest review-all <draft>  # rung 3a: mint a review-todo for every
                                      # (reviewable chunk x lens) of a draft
    precis quest tag-papers 7        # backfill quest:<id> tag onto serving
                                      # papers (Drive-scoped browse)

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

    w = qsub.add_parser(
        "weave",
        help="Run one weave tick (rung 6e-1): place + weave the dossier's "
        "unintegrated papers, scaffolding new sections for any residual.",
    )
    w.add_argument("id", type=int, help="Quest ref id.")
    w.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute placements/proposed titles + call the model, but write "
        "nothing (no sections, citations, links, or logbook entry).",
    )
    w.add_argument(
        "--tier",
        default="cloud-mid",
        help="LLM tier for the weave/title-judgment calls (default cloud-mid — "
        "the mid agentic rung; see --tier on `quest tick` for the full set).",
    )
    w.add_argument(
        "--max-sections",
        type=int,
        default=None,
        help="Cap how many section batches (Maintain + newly-scaffolded Make "
        "sections combined) this one tick weaves — a cost/latency valve for a "
        "bounded first run. Unset = weave every section this tick; papers left "
        "over stay unintegrated for the next tick (no state lost).",
    )
    w.add_argument("--database-url", default=None, help="Postgres DSN override.")

    ra = qsub.add_parser(
        "review-all",
        help="One-shot whole-draft review fanout (rung 3a): mint a "
        "review-todo for every (reviewable chunk x lens) of a draft.",
    )
    ra.add_argument("draft", help="Draft slug or numeric id.")
    ra.add_argument(
        "--lenses",
        default=None,
        help="Comma-separated lens list (default: flow,cites,structure,"
        "adversarial — all four).",
    )
    ra.add_argument(
        "--author",
        action="store_true",
        help="Stamp meta.author=True on minted cites/structure todos "
        "(plumbing only — no authoring behavior yet; flow/adversarial "
        "never author).",
    )
    ra.add_argument("--database-url", default=None, help="Postgres DSN override.")

    d = qsub.add_parser("dossier", help="Print a quest's dossier.")
    d.add_argument("id", type=int, help="Quest ref id.")
    d.add_argument("--database-url", default=None, help="Postgres DSN override.")

    g = qsub.add_parser("gaps", help="Print a quest's gaps + health.")
    g.add_argument("id", type=int, help="Quest ref id.")
    g.add_argument("--database-url", default=None, help="Postgres DSN override.")

    f = qsub.add_parser("frontier", help="Print a quest's Pareto frontier.")
    f.add_argument("id", type=int, help="Quest ref id.")
    f.add_argument("--database-url", default=None, help="Postgres DSN override.")

    st = qsub.add_parser(
        "status",
        help="Ops roll-up: logbook tail, candidates + measures, sim-job "
        "status, coordinator tick trail, LLM spend. Read-only.",
    )
    st.add_argument("id", type=int, help="Quest ref id.")
    st.add_argument(
        "--logbook", type=int, default=10, help="Logbook tail lines (default 10)."
    )
    st.add_argument(
        "--tick-events",
        type=int,
        default=10,
        help="Coordinator job_event tail lines (default 10).",
    )
    st.add_argument("--database-url", default=None, help="Postgres DSN override.")

    sc = qsub.add_parser(
        "seed-catalyst",
        help="Mint the NO→NH₃/Pd catalyst-discovery quest (idempotent, dark).",
    )
    sc.add_argument("--database-url", default=None, help="Postgres DSN override.")

    tp = qsub.add_parser(
        "tag-papers",
        help="Backfill: tag every serves-linked paper with quest:<id> "
        "(scopes the Drive browse surface to this quest — idempotent).",
    )
    tp.add_argument("id", type=int, help="Quest ref id.")
    tp.add_argument("--database-url", default=None, help="Postgres DSN override.")

    r = qsub.add_parser(
        "run", help="Allocator: pick the best active quest + tick it once."
    )
    r.add_argument(
        "--budget",
        type=int,
        default=None,
        help="Weekly compute budget in chars (overrides PRECIS_QUEST_WEEKLY_CHARS).",
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
            f"{outcome.results_harvested} result(s), {outcome.ruled_out} ruled-out, "
            f"{outcome.graduated} graduated"
        )
    if outcome.cost_usd:
        msg += f", ${outcome.cost_usd:.4f}"
    print(f"{msg} ({outcome.note})")


def _cmd_weave(store: Store, args: argparse.Namespace) -> None:
    from precis.quest.weave_tick import weave_tick
    from precis.utils.llm.router import DispatchClient, Tier

    client = DispatchClient(
        tier=Tier(args.tier), source="quest_weave", tools_needed=True
    )
    result = weave_tick(
        store, client, args.id, dry_run=args.dry_run, max_sections=args.max_sections
    )

    if not result.get("ok"):
        print(f"quest {args.id}: weave tick failed — {result.get('error')}")
        return
    if result.get("note"):
        print(f"quest {args.id}: weave tick — {result['note']}")
        return

    woven = result["woven"]
    ok_sections = sum(1 for w in woven if w.get("ok"))
    failed_sections = len(woven) - ok_sections
    dispositions: dict[str, int] = {}
    citations = 0
    for w in woven:
        if not w.get("ok"):
            continue
        for p in w.get("papers", []):
            disp = p.get("disposition")
            if disp:
                dispositions[disp] = dispositions.get(disp, 0) + 1
            citations += len(p.get("citation_ids") or [])

    mode = "dry-run" if args.dry_run else "applied"
    print(
        f"quest {args.id}: weave tick ({mode}) — batch {result['batch_size']}, "
        f"{ok_sections} section(s) woven, {failed_sections} failed, "
        f"{len(result['new_sections'])} new section(s), {citations} citation(s), "
        f"dispositions {dispositions}, {len(result['residual_unplaced'])} residual "
        "unplaced"
    )
    if result["new_sections"]:
        for ns in result["new_sections"]:
            print(f"  new section: {ns['title']!r} ({len(ns['paper_ref_ids'])} papers)")
    if not args.dry_run and result.get("log_entry"):
        print(f"  logbook entry #{result['log_entry']}")


def _cmd_review_all(store: Store, args: argparse.Namespace) -> None:
    import sys

    from precis.errors import BadInput, NotFound
    from precis.quest.review_fanout import ALL_LENSES, mint_review_fanout

    key: int | str = int(args.draft) if str(args.draft).isdigit() else args.draft
    ref = store.get_ref(kind="draft", id=key)
    if ref is None:
        print(f"quest review-all: no draft {args.draft!r}", file=sys.stderr)
        sys.exit(2)

    lenses = (
        tuple(x.strip() for x in args.lenses.split(",") if x.strip())
        if args.lenses
        else ALL_LENSES
    )

    try:
        result = mint_review_fanout(
            store, ref.id, lenses=lenses, author=bool(args.author)
        )
    except (BadInput, NotFound) as exc:
        print(f"quest review-all: {exc}", file=sys.stderr)
        sys.exit(2)

    msg = (
        f"draft {args.draft!r}: review-all fanout — {result['chunks_seen']} "
        f"chunk(s) x {len(lenses)} lens(es), {len(result['minted'])} minted, "
        f"{result['skipped']} already live, parented on todo "
        f"{result['parent_id']}"
    )
    if args.author:
        msg += f", {result['author_minted']} author-enabled"
    print(msg)


def _cmd_seed_catalyst(store: Store, args: argparse.Namespace) -> None:
    from precis.quest.catalyst_seed import seed_catalyst_quest

    qid, created = seed_catalyst_quest(store)
    verb = "minted" if created else "already exists"
    print(
        f"catalyst quest {verb}: qu{qid} (NO→NH₃ on Pd(111)). "
        f"First light:  precis quest tick {qid} --compute"
    )


def _cmd_tag_papers(store: Store, args: argparse.Namespace) -> None:
    import sys

    from precis.errors import NotFound
    from precis.quest.tagging import quest_tag_value, tag_serving_papers

    try:
        n = tag_serving_papers(store, args.id)
        tag_value = quest_tag_value(args.id, store)
    except NotFound as exc:
        print(f"quest tag-papers: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"quest {args.id}: tagged {n} serving paper(s) with tag={tag_value!r}")


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
        if summary.get("status") == "paused":
            print(
                f"quest run: cooled {summary['cooled']}, a quest was eligible but "
                "the budget breaker is paused (dollar cap / claude-OAuth quota) — "
                "skipped; retries when the window rolls off"
            )
            return
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


def _cmd_status(store: Store, args: argparse.Namespace) -> None:
    from precis.quest.status import gather_quest_status, render_quest_status

    status = gather_quest_status(
        store, args.id, logbook_n=args.logbook, tick_n=args.tick_events
    )
    if status is None:
        print(f"quest {args.id}: not found")
        return
    print(render_quest_status(status))


def run(args: argparse.Namespace) -> None:
    store = Store.connect(resolve_dsn(args.database_url))
    if args.quest_cmd == "tick":
        _cmd_tick(store, args)
    elif args.quest_cmd == "weave":
        _cmd_weave(store, args)
    elif args.quest_cmd == "review-all":
        _cmd_review_all(store, args)
    elif args.quest_cmd == "dossier":
        _cmd_dossier(store, args)
    elif args.quest_cmd == "gaps":
        _cmd_gaps(store, args)
    elif args.quest_cmd == "frontier":
        _cmd_frontier(store, args)
    elif args.quest_cmd == "status":
        _cmd_status(store, args)
    elif args.quest_cmd == "seed-catalyst":
        _cmd_seed_catalyst(store, args)
    elif args.quest_cmd == "tag-papers":
        _cmd_tag_papers(store, args)
    elif args.quest_cmd == "run":
        _cmd_run(store, args)
