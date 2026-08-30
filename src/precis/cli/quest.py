"""``precis quest`` — drive the quest layer from the CLI.

    precis quest tick 7              # run one research tick against quest 7
    precis quest tick 7 --dry-run    # assemble + print the tick context, no LLM
    precis quest dossier 7           # print quest 7's living dossier
    precis quest dossier-dedup 7 --dry-run  # preview near-dup ledger merges
    precis quest gaps 7              # print quest 7's gaps + health
    precis quest status 7            # ops roll-up: logbook, candidates, sim
                                      # jobs, coordinator trail, LLM spend
    precis quest review-all <draft>  # rung 3a: mint a review-todo for every
                                      # (reviewable chunk x persona) of a draft
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
        help="LLM tier (e.g. medium, small, frontier).",
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
        default="big",
        help="LLM tier for the weave/title-judgment calls (default big — "
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
        "review-todo for every (reviewable chunk x persona) of a draft.",
    )
    ra.add_argument("draft", help="Draft slug or numeric id.")
    ra.add_argument(
        "--personas",
        default=None,
        help="Comma-separated persona list (default: flow,cites,structure,"
        "adversarial — all four).",
    )
    ra.add_argument(
        "--author",
        action="store_true",
        help="Stamp meta.author=True on minted cites/structure todos "
        "(plumbing only — no authoring behavior yet; flow/adversarial "
        "never author).",
    )
    ra.add_argument(
        "--only-dirty",
        action="store_true",
        help="Incremental re-check: "
        "skip a (chunk, persona) pair already approved at the chunk's "
        "current sha, and skip any chunk carrying an open anchored "
        "change-request. Cheap re-run after edits instead of a full "
        "re-review.",
    )
    ra.add_argument(
        "--scope",
        default=None,
        help="Narrow the fanout to one draft chunk (dc<id> or ¶<handle>): "
        "a heading's whole subtree, or a single prose chunk. Default: the "
        "whole draft.",
    )
    ra.add_argument("--database-url", default=None, help="Postgres DSN override.")

    d = qsub.add_parser("dossier", help="Print a quest's dossier.")
    d.add_argument("id", type=int, help="Quest ref id.")
    d.add_argument("--database-url", default=None, help="Postgres DSN override.")

    dd = qsub.add_parser(
        "dossier-dedup",
        help="One-off cleanup: merge near-duplicate pinned-ledger attempt "
        "nodes a quest accumulated before the add_attempt upsert discipline "
        "landed. Keeps the oldest node per cluster at its most-advanced "
        "status, re-parents absorbed nodes' children onto it.",
    )
    dd.add_argument("id", type=int, help="Quest ref id.")
    dd.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned merges without changing anything.",
    )
    dd.add_argument("--database-url", default=None, help="Postgres DSN override.")

    g = qsub.add_parser("gaps", help="Print a quest's gaps + health.")
    g.add_argument("id", type=int, help="Quest ref id.")
    g.add_argument("--database-url", default=None, help="Postgres DSN override.")

    f = qsub.add_parser("frontier", help="Print a quest's Pareto frontier.")
    f.add_argument("id", type=int, help="Quest ref id.")
    f.add_argument("--database-url", default=None, help="Postgres DSN override.")

    fg = qsub.add_parser(
        "figure",
        help="Render a quest Pareto frontier or pathway energy-profile "
        "figure (matplotlib) and attach it to a draft, with a snapshot "
        "data-package frozen alongside the pixels.",
    )
    fg.add_argument("target", help="Quest or pathway id / handle / slug.")
    fg.add_argument(
        "--draft",
        required=True,
        help="Draft id / handle / slug to attach the figure to.",
    )
    fg.add_argument(
        "--caption",
        default=None,
        help="Figure caption (default: auto-generated from the target's title).",
    )
    fg.add_argument(
        "--pos",
        default=None,
        help="Draft chunk handle to insert the figure after (default: append "
        "at the end of the draft).",
    )
    fg.add_argument("--database-url", default=None, help="Postgres DSN override.")

    rd = qsub.add_parser(
        "redispatch",
        help="Re-dispatch a barrier eval for every candidate on the deployed "
        "engine — use after an autocatpath deploy to re-score stale results "
        "(the idem key folds the engine version, so this only re-runs work an "
        "engine change invalidated).",
    )
    rd.add_argument("id", type=int, help="Quest ref id.")
    rd.add_argument(
        "--include-ruled-out",
        action="store_true",
        help="Also re-evaluate candidates ruled out on now-suspect stale barriers.",
    )
    rd.add_argument("--database-url", default=None, help="Postgres DSN override.")

    rc = qsub.add_parser(
        "reset-compute",
        help="Surgically wipe the barrier-lane compute history (stale measures, "
        "ruled-out + graduation tags, dossier) for a clean re-run — keeps the "
        "candidate designs + papers. Follow with `redispatch`.",
    )
    rc.add_argument("id", type=int, help="Quest ref id.")
    rc.add_argument(
        "--keep-dossier",
        action="store_true",
        help="Leave the dossier intact (default resets it so the tick regenerates it).",
    )
    rc.add_argument("--database-url", default=None, help="Postgres DSN override.")

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
    from precis.quest.review_fanout import (
        ALL_PERSONAS,
        DOC_PERSONAS,
        mint_review_fanout,
    )

    key: int | str = int(args.draft) if str(args.draft).isdigit() else args.draft
    ref = store.get_ref(kind="draft", id=key)
    if ref is None:
        print(f"quest review-all: no draft {args.draft!r}", file=sys.stderr)
        sys.exit(2)

    personas = (
        tuple(x.strip() for x in args.personas.split(",") if x.strip())
        if args.personas
        else ALL_PERSONAS
    )

    scope_chunk_id: int | None = None
    if args.scope:
        scope_chunk = store.drafts.get_draft_chunk(args.scope)
        if scope_chunk is None:
            print(f"quest review-all: no draft chunk {args.scope!r}", file=sys.stderr)
            sys.exit(2)
        scope_chunk_id = scope_chunk.chunk_id

    try:
        result = mint_review_fanout(
            store,
            ref.id,
            personas=personas,
            doc_personas=DOC_PERSONAS if scope_chunk_id is None else (),
            author=bool(args.author),
            only_dirty=bool(args.only_dirty),
            scope=scope_chunk_id,
        )
    except (BadInput, NotFound) as exc:
        print(f"quest review-all: {exc}", file=sys.stderr)
        sys.exit(2)

    msg = (
        f"draft {args.draft!r}: review-all fanout — {result['chunks_seen']} "
        f"chunk(s) x {len(personas)} persona(s), {len(result['minted'])} minted, "
        f"{result['skipped']} already live, {result['unsettled_skipped']} "
        f"unsettled-skipped, parented on todo {result['parent_id']}"
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


def _cmd_dossier_dedup(store: Store, args: argparse.Namespace) -> None:
    from precis.quest.dossier import dedup_ledger

    merges = dedup_ledger(store, args.id, dry_run=args.dry_run)
    if not merges:
        print(f"quest {args.id}: no near-duplicate ledger nodes found")
        return
    removed = 0
    for m in merges:
        status_note = (
            f" [{m.prior_status} -> {m.new_status}]"
            if m.new_status != m.prior_status
            else f" [{m.prior_status}]"
        )
        print(f"survivor: {m.survivor_text}{status_note}")
        for text, status in m.absorbed:
            print(f"  <- absorbed [{status}]: {text}")
        removed += len(m.absorbed)
    verb = "would merge" if args.dry_run else "merged"
    print(f"\n{verb} {len(merges)} cluster(s), {removed} node(s) removed")


def _cmd_gaps(store: Store, args: argparse.Namespace) -> None:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler

    h = QuestHandler(hub=Hub(store=store))
    print(h.get(id=args.id, view="gaps").body)


def _resolve_target_ref(store: Store, token: str) -> Any:
    """A quest or pathway ref for ``token`` — a decimal ``qu<id>`` handle,
    a bare numeric id (tried as a quest ref_id, then a pathway ref_id), or
    a pathway slug (pathway is a slug-addressed kind — see
    ``precis_pathway.handler``). ``None`` when nothing resolves."""
    from precis.utils import handle_registry

    parsed = handle_registry.parse(token)
    if parsed is not None:
        kind, is_chunk, pk = parsed
        if is_chunk or kind not in ("quest", "pathway"):
            return None
        return store.get_ref(kind=kind, id=pk)
    if token.strip().lstrip("-").isdigit():
        pk = int(token)
        return store.get_ref(kind="quest", id=pk) or store.get_ref(
            kind="pathway", id=pk
        )
    return store.get_ref(kind="pathway", id=token)


def _resolve_draft_ref(store: Store, token: str) -> Any:
    """A draft ref for ``token`` — a decimal ``dr<id>`` handle, a bare
    numeric id, or a slug."""
    from precis.utils import handle_registry

    parsed = handle_registry.parse(token)
    if parsed is not None:
        kind, is_chunk, pk = parsed
        if is_chunk or kind != "draft":
            return None
        return store.get_ref(kind="draft", id=pk)
    key: int | str = int(token) if token.isdigit() else token
    return store.get_ref(kind="draft", id=key)


def _cmd_figure(store: Store, args: argparse.Namespace) -> None:
    import sys

    from precis.quest import figures as figures_mod
    from precis.utils import handle_registry

    target = _resolve_target_ref(store, args.target)
    if target is None:
        print(f"quest figure: no quest or pathway {args.target!r}", file=sys.stderr)
        sys.exit(2)
    draft = _resolve_draft_ref(store, args.draft)
    if draft is None:
        print(f"quest figure: no draft {args.draft!r}", file=sys.stderr)
        sys.exit(2)

    target_handle = handle_registry.try_format(target.kind, target.id) or (
        f"{target.kind}:{target.id}"
    )
    try:
        if target.kind == "quest":
            png, snapshot = figures_mod.quest_pareto_figure(store, target)
            default_caption = f"Pareto frontier for {target.title} ({target_handle})"
        elif target.kind == "pathway":
            png, snapshot = figures_mod.pathway_profile_figure(store, target)
            default_caption = f"Energy profile for {target.title} ({target_handle})"
        else:
            print(
                f"quest figure: target must be a quest or pathway "
                f"(got {target.kind!r})",
                file=sys.stderr,
            )
            sys.exit(2)
    except ValueError as exc:
        # Renderers raise for underpopulated data (a <2-point frontier, a
        # pathway with no plottable states) — a user error, not a crash.
        print(f"quest figure: {exc}", file=sys.stderr)
        sys.exit(2)

    at = {"after": args.pos} if args.pos else None
    chunk = store.drafts.add_figure(
        ref_id=draft.id,
        caption=args.caption or default_caption,
        origin="own_graph",
        image=png,
        mime="image/png",
        at=at,
        figure_meta={"data_package": snapshot},
    )
    ord_map = store.drafts.chunk_ord_map(chunk.ref_id)
    store.add_link(
        src_ref_id=chunk.ref_id,
        src_pos=ord_map.get(chunk.chunk_id),
        dst_ref_id=target.id,
        relation="derived-from",
    )
    print(
        f"quest figure: added {chunk.dc} to draft {draft.id} (derived-from {target_handle})"
    )


def _cmd_redispatch(store: Store, args: argparse.Namespace) -> None:
    from precis.dispatch import Hub
    from precis.quest.compute import redispatch_candidates

    note = redispatch_candidates(
        store,
        args.id,
        hub=Hub(store=store),
        include_ruled_out=args.include_ruled_out,
    )
    print(f"quest {args.id}: {note}")


def _cmd_reset_compute(store: Store, args: argparse.Namespace) -> None:
    from precis.quest.compute import reset_compute

    note = reset_compute(store, args.id, keep_dossier=args.keep_dossier)
    print(f"quest {args.id}: {note}")


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
    elif args.quest_cmd == "dossier-dedup":
        _cmd_dossier_dedup(store, args)
    elif args.quest_cmd == "gaps":
        _cmd_gaps(store, args)
    elif args.quest_cmd == "frontier":
        _cmd_frontier(store, args)
    elif args.quest_cmd == "figure":
        _cmd_figure(store, args)
    elif args.quest_cmd == "redispatch":
        _cmd_redispatch(store, args)
    elif args.quest_cmd == "reset-compute":
        _cmd_reset_compute(store, args)
    elif args.quest_cmd == "status":
        _cmd_status(store, args)
    elif args.quest_cmd == "seed-catalyst":
        _cmd_seed_catalyst(store, args)
    elif args.quest_cmd == "tag-papers":
        _cmd_tag_papers(store, args)
    elif args.quest_cmd == "run":
        _cmd_run(store, args)
