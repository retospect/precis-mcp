"""``precis taproot-migrate {score,dry-run,canary,reground,apply}`` — the
migration runner for existing (pre-decomposition) claim hubs,
``docs/backlog/taproot-atomic-claims.md``'s Strategy end to end.
``score``/``dry-run``/``canary``/``reground`` make zero claim-data writes
(no refs/links/meta/chunks) — thin CLI skin over
:mod:`precis.taproot.migrate` / :mod:`precis.taproot.eval_canon` /
:mod:`precis.taproot.reground`. ``apply`` is Phase 2 — the one subcommand
here that writes, over :mod:`precis.taproot.apply_migrate`. ``dry-run``,
``canary``, ``reground``, and ``apply`` all bind their store to
:mod:`precis.budget.meter` so real LLM dispatch resolves the host's
serving endpoint and is budget-metered (``llm_call_log`` telemetry, never
claim data); ``score`` makes no LLM calls at all.

``reground`` sits between ``dry-run`` and ``apply`` (``docs/backlog/
taproot-atom-regrounding.md``, "no source, no atom"): it re-verifies each
``split``-verdict hub's atoms against its candidate source papers' actual
text and writes a *regrounded* JSONL artifact — the original dry-run row
plus a ``"grounding"`` key ``apply`` reads to withhold any atom that
turned out to be an extractor invention, never silently placing it. A hub
``verify_atoms`` couldn't check at all (a dead dispatch, a malformed row)
gets an error-sentinel ``"grounding": {"error": "..."}`` instead of being
passed through with no ``"grounding"`` key — the latter reads as "never
regrounded, place as before" to ``apply``, which would silently place
exactly the atoms this check failed on.

The extractor tier defaults to **haiku** (round 2 of ``docs/backlog/
taproot-migration-extraction-quality-gates.md``): the labelled-25 A/B
re-run showed the SMALL tier collapsing multi-clause sentences to single
truncated atoms, and the BIG chain's OSS models intermittently breaking
the JSON contract (prose / invented schemas / empty responses — silent
NO-CLAIMs), while claude-haiku held the contract 12/12 on the same
prompts (:func:`precis.taproot.canon.extract_claim_strict_medium`, MEDIUM
tier + a format-flake guard — routed through :mod:`precis.utils.llm.router`
since the 2026-08-15 ``llm.chain.medium`` cutover made MEDIUM haiku).
``--tier small`` / ``--tier big`` remain as explicit opt-ins for A/B work.

Run ``canary`` (11 hand-authored passages through the chosen tier + the
migration gates, exit 1 on failure) before any bulk ``dry-run`` — it
catches a collapsed/degraded extractor for the cost of 11 calls instead
of a burned bulk run.

    precis taproot-migrate score
    precis taproot-migrate score --format json
    precis taproot-migrate canary
    precis taproot-migrate dry-run --limit 50
    precis taproot-migrate dry-run --limit 50 --cohort likely-compound --controls 10
    precis taproot-migrate dry-run --limit 50 --out /tmp/report.md
    precis taproot-migrate dry-run --limit 50 --json /tmp/report.jsonl \\
        --tier small --escalate
    precis taproot-migrate reground --json /tmp/report.jsonl --out /tmp/regrounded.jsonl
    precis taproot-migrate apply --json /tmp/report.jsonl
    precis taproot-migrate apply --json /tmp/regrounded.jsonl --only-verdict split --limit 20

**``apply`` is a quiet-window operation (an operator step, not a code
gate).** Before running it, pause the derived-queue workers that touch
hubs (``hub_refine``, ``chase_trigger``) so nothing refines/re-embeds a
hub mid-repoint, and avoid 02:00-03:30 UTC (nightly backup + caspar's
daily reboot) — see ``docs/backlog/taproot-atomic-claims.md``'s "Quiet
window definition". Phase 3 (human review) is not built here.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from precis.cli._common import resolve_dsn


def add_parser(subparsers: Any) -> None:
    """Register the ``taproot-migrate`` subcommand group (``score`` /
    ``dry-run``)."""
    p = subparsers.add_parser(
        "taproot-migrate",
        help="Taproot atomic-claims migration runner — phase 0 (score/cohort) "
        "+ phase 1 (dry-run decomposition) over EXISTING claim hubs. Both "
        "subcommands make zero claim-data writes.",
    )
    tsub = p.add_subparsers(dest="taproot_migrate_cmd", required=True)

    s = tsub.add_parser(
        "score",
        help="Phase 0: score+cohort every live, not-yet-migrated claim hub "
        "by title compoundness heuristics (conjunctions/length/punctuation). "
        "No model call, read-only.",
    )
    s.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    s.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    c = tsub.add_parser(
        "canary",
        help="Pre-run extractor smoke test: 11 hand-authored passages "
        "through the chosen tier + the migration gates; exit 1 on any "
        "lossy/nested verdict or no-claim mismatch. Run before any bulk "
        "dry-run.",
    )
    c.add_argument(
        "--tier",
        choices=("small", "big", "haiku"),
        default="haiku",
        help="Extractor tier to smoke-test (default: haiku — the dry-run default).",
    )
    c.add_argument(
        "--fixture",
        default=None,
        help="Override the packaged extraction_passages.jsonl fixture path.",
    )
    c.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    d = tsub.add_parser(
        "dry-run",
        help="Phase 1: run the strict extractor (BIG tier by default) over "
        "the top-scored hubs' claim sentences and report proposed splits "
        "as markdown. Zero claim-data writes (no refs/links/meta/chunks) "
        "-- LLM spend, budget-metered, only.",
    )
    d.add_argument(
        "--limit",
        type=int,
        required=True,
        help="Number of top-scored hubs to evaluate.",
    )
    d.add_argument(
        "--cohort",
        choices=("likely-compound", "uncertain", "likely-atomic"),
        default=None,
        help="Restrict the top-scored set to one cohort (default: all cohorts).",
    )
    d.add_argument(
        "--controls",
        type=int,
        default=0,
        help="Also sample this many hubs from the bottom of the likely-atomic "
        "cohort, as a pass-through sanity control (default: 0).",
    )
    d.add_argument(
        "--out",
        default=None,
        help="Write the markdown report to this file instead of stdout.",
    )
    d.add_argument(
        "--json",
        default=None,
        help="Also write one JSON object per outcome (hub, score, verdict, "
        "gate metadata, full extraction, escalation results) to this file.",
    )
    d.add_argument(
        "--tier",
        choices=("small", "big", "haiku"),
        default="haiku",
        help="Primary extractor tier (default: haiku — held the JSON contract "
        "12/12 on the sentences the BIG chain flaked to empty on; SMALL "
        "collapses multi-clause sentences to single truncated atoms).",
    )
    d.add_argument(
        "--escalate",
        action="store_true",
        help="With --tier small only: re-extract lossy/nested/junk-candidate "
        "outcomes with the BIG-tier extractor and record both results (P2-10).",
    )
    d.add_argument(
        "--control-seed",
        type=int,
        default=0,
        help="Seed for the uniform random control sample (default: 0, deterministic).",
    )
    d.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    rg = tsub.add_parser(
        "reground",
        help="Between dry-run and apply: re-verify each split-verdict hub's "
        "atoms against its candidate source papers' actual text -- 'no "
        "source, no atom' (docs/backlog/taproot-atom-regrounding.md). Zero "
        "claim-data writes (read-only DB + budget-metered LLM verify "
        "calls); writes a regrounded JSONL artifact for `apply` to consume.",
    )
    rg.add_argument(
        "--json",
        dest="json_path",
        required=True,
        help="The dry-run JSONL file (dump_outcomes_jsonl's output) to reground.",
    )
    rg.add_argument(
        "--out",
        required=True,
        help="Write the regrounded JSONL artifact here (original rows, plus a "
        "'grounding' key on every split-verdict row).",
    )
    rg.add_argument(
        "--top-k",
        type=int,
        default=6,
        help="Candidate passages per atom x paper (default: 6).",
    )
    rg.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )

    a = tsub.add_parser(
        "apply",
        help="Phase 2 (WRITES): apply a dry-run report's outcomes to "
        "production hubs -- one transaction per hub, resumable "
        "(re-running skips already-stamped hubs). Mint/converge atom "
        "hubs, link conjunct-of, re-point evidence edges via the "
        "add-first invariant, stamp meta.taproot_decomposed_at. "
        "QUIET-WINDOW OP: pause hub_refine/chase_trigger first and avoid "
        "02:00-03:30 UTC -- see docs/backlog/taproot-atomic-claims.md.",
    )
    a.add_argument(
        "--json",
        dest="json_path",
        required=True,
        help="The dry-run JSONL file (dump_outcomes_jsonl's output) to apply.",
    )
    a.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Apply at most this many outcomes (after --only-verdict filtering, "
        "in file order). Default: all.",
    )
    a.add_argument(
        "--only-verdict",
        choices=("pass-through", "split", "no-claim", "lossy", "nested", "error"),
        default=None,
        help="Restrict to outcomes with this verdict (default: every verdict "
        "present in the file).",
    )
    a.add_argument(
        "--embedder",
        default="bge-m3",
        help="Embedder for the atom-placement block() ANN lookup (default: bge-m3).",
    )
    a.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")


def _run_score(args: argparse.Namespace) -> None:
    from precis.store import Store
    from precis.taproot.migrate import COHORTS, score_hubs

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        scores = score_hubs(store)
    finally:
        store.close()

    counts: dict[str, int] = {}
    for s in scores:
        counts[s.cohort] = counts.get(s.cohort, 0) + 1

    if args.format == "json":
        print(
            json.dumps(
                {
                    "total": len(scores),
                    "cohorts": {c: counts.get(c, 0) for c in COHORTS},
                    "top": [
                        {
                            "ref_id": s.ref_id,
                            "title": s.title,
                            "score": s.score,
                            "cohort": s.cohort,
                            "signals": list(s.signals),
                        }
                        for s in scores[:30]
                    ],
                },
                indent=2,
            )
        )
        return

    print(f"taproot-migrate score: {len(scores)} live, not-yet-migrated claim hub(s)")
    for cohort in COHORTS:
        print(f"  {cohort}: {counts.get(cohort, 0)}")
    if not scores:
        return
    print()
    print("Top 30 by score:")
    for s in scores[:30]:
        title = s.title if len(s.title) <= 70 else s.title[:67] + "..."
        signals = ",".join(s.signals) or "-"
        print(f"  fi{s.ref_id:<8} score={s.score}  {s.cohort:<15} [{signals}]  {title}")


def _resolve_extract_fn(tier: str) -> Any:
    """The strict extractor for a ``--tier`` value. Strict on purpose —
    both callers (dry-run, canary) must tell a dead dispatch apart from a
    semantic NO-CLAIM."""
    from precis.taproot.canon import (
        extract_claim_strict,
        extract_claim_strict_big,
        extract_claim_strict_medium,
    )

    if tier == "haiku":
        return extract_claim_strict_medium
    return extract_claim_strict_big if tier == "big" else extract_claim_strict


def _run_canary(args: argparse.Namespace) -> None:
    from precis.budget import meter
    from precis.store import Store
    from precis.taproot.eval_canon import (
        EXTRACTION_PASSAGES_FIXTURE,
        canary_extraction,
    )

    store = Store.connect(resolve_dsn(args.database_url))
    meter.bind_store(store)
    try:
        report = canary_extraction(
            args.fixture or EXTRACTION_PASSAGES_FIXTURE,
            extract_fn=_resolve_extract_fn(args.tier),
        )
    finally:
        store.close()
    if not report.ok:
        sys.exit(1)


def _run_dry_run(args: argparse.Namespace) -> None:
    from precis.store import Store
    from precis.taproot.migrate import dry_run, dump_outcomes_jsonl, render_report

    escalate_fn = None
    if args.escalate:
        if args.tier != "small":
            print(
                "taproot-migrate: --escalate is the SMALL→BIG retry; it only "
                f"applies with --tier small (got --tier {args.tier})",
                file=sys.stderr,
            )
            sys.exit(2)
        from precis.taproot.canon import extract_claim_strict_big

        escalate_fn = extract_claim_strict_big

    store = Store.connect(resolve_dsn(args.database_url))
    # Bind the store to the budget meter so LOCAL dispatch can resolve the
    # host's real served_by llama-swap endpoint from the DB (instead of
    # falling to the dead 127.0.0.1:4000 default) and this run's LLM spend
    # is gated by the budget breaker. Writes telemetry only (llm_call_log +
    # transient serving-slot rows), never claim data.
    from precis.budget import meter

    meter.bind_store(store)
    try:
        report = dry_run(
            store,
            limit=args.limit,
            cohort=args.cohort,
            controls=args.controls,
            control_seed=args.control_seed,
            extract_fn=_resolve_extract_fn(args.tier),
            escalate_fn=escalate_fn,
        )
    finally:
        store.close()

    rendered = render_report(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"wrote {len(report.outcomes)} hub outcome(s) to {args.out}")
    else:
        print(rendered)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(dump_outcomes_jsonl(report))
        print(f"wrote {len(report.outcomes)} outcome(s) as JSONL to {args.json}")


def _grounded_record_to_dict(rec: Any) -> dict[str, Any]:
    return {
        "paper_ref_id": rec.paper_ref_id,
        "chunk_id": rec.chunk_id,
        "chunk_ord": rec.chunk_ord,
        "quote": rec.quote,
        "bound": rec.bound,
    }


def _atom_grounding_to_dict(ag: Any) -> dict[str, Any]:
    return {
        "sentence": ag.atom.sentence,
        "grounded": ag.grounded,
        "records": [_grounded_record_to_dict(r) for r in ag.records],
        "reason": ag.reason,
    }


def _hub_grounding_to_dict(result: Any) -> dict[str, Any]:
    """:class:`~precis.taproot.reground.HubGroundingResult` -> the plain
    dict a regrounded JSONL row's ``"grounding"`` key carries -- original
    row + per-atom grounding records/reasons + a per-hub summary
    (``docs/backlog/taproot-atom-regrounding.md``'s CLI-stage contract)."""
    atoms = result.atoms
    reasons: dict[str, int] = {}
    grounded_n = 0
    for a in atoms:
        if a.grounded:
            grounded_n += 1
        elif a.reason:
            reasons[a.reason] = reasons.get(a.reason, 0) + 1
    return {
        "paper_ref_ids": list(result.paper_ref_ids),
        "atoms": [_atom_grounding_to_dict(a) for a in atoms],
        "summary": {
            "total_atoms": len(atoms),
            "grounded": grounded_n,
            "withheld_reasons": reasons,
        },
    }


#: ``reground_row(store, hub_ref_id, atoms, *, top_k=...) -> HubGroundingResult``.
#: Injected so :func:`_reground_row` is unit-testable without a live DB/LLM
#: (default :func:`precis.taproot.reground.verify_atoms`).
RegroundRowFn = Any


def _reground_row(
    store: Any,
    row: dict[str, Any],
    *,
    top_k: int,
    verify_atoms_fn: RegroundRowFn | None = None,
) -> dict[str, Any]:
    """Re-ground ONE dry-run row — the per-row unit :func:`_run_reground`'s
    loop calls, pulled out specifically so a malformed row (bad ``"hub"``,
    bad ``"extraction"``) or a raising ``verify_atoms`` call degrades to
    *this row's own* error sentinel and lets the caller's loop continue,
    rather than aborting the whole run — the regrounded JSONL is only
    written after every row has been processed, so an unhandled exception
    here would silently discard every prior hub's already-computed
    grounding result too.

    A non-``"split"`` row passes through completely unchanged (nothing to
    re-ground). A ``"split"`` row that re-grounds cleanly gets a
    ``"grounding"`` key with the real per-atom result
    (:func:`_hub_grounding_to_dict`); one that raises (from either the row
    parsing or the ``verify_atoms_fn`` call itself) gets the error-sentinel
    shape ``{"grounding": {"error": "<message>"}}`` instead — deliberately
    NOT the same as no ``"grounding"`` key at all: a missing key reads (to
    :mod:`precis.taproot.apply_migrate`) as "never regrounded, safe to
    place as before"; silently reusing that shape here would place exactly
    the atoms this check failed on, fully ungated.
    """
    if row.get("verdict") != "split":
        return row

    from precis.taproot.apply_migrate import _parse_atoms
    from precis.taproot.reground import verify_atoms as _default_verify_atoms

    verify_fn = (
        verify_atoms_fn if verify_atoms_fn is not None else _default_verify_atoms
    )
    try:
        hub_ref_id = int(row["hub"])
        atoms = _parse_atoms(row.get("extraction"))
        result = verify_fn(store, hub_ref_id, atoms, top_k=top_k)
    except Exception as exc:
        out_row = dict(row)
        out_row["grounding"] = {"error": str(exc)}
        return out_row

    out_row = dict(row)
    out_row["grounding"] = _hub_grounding_to_dict(result)
    return out_row


def _row_regrounding_failed(row: dict[str, Any]) -> str | None:
    """The error message if ``row``'s ``"grounding"`` is the error-sentinel
    shape :func:`_reground_row` writes, else ``None``."""
    grounding = row.get("grounding")
    if isinstance(grounding, dict):
        error = grounding.get("error")
        if isinstance(error, str) and error:
            return error
    return None


def _run_reground(args: argparse.Namespace) -> None:
    from precis.budget import meter
    from precis.store import Store

    with open(args.json_path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    store = Store.connect(resolve_dsn(args.database_url))
    # Same reasoning as dry-run's/apply's bind -- verify_atoms_batch's real
    # dispatch (MEDIUM tier) resolves the host's serving endpoint through
    # the budget meter and this run's spend is gated by the breaker.
    # Writes telemetry only (llm_call_log), never claim data -- this
    # subcommand makes zero refs/links/meta/chunk writes.
    meter.bind_store(store)
    n_split = 0
    n_errors = 0
    out_rows: list[dict[str, Any]] = []
    try:
        for row in rows:
            if row.get("verdict") != "split":
                out_rows.append(row)
                continue
            n_split += 1
            out_row = _reground_row(store, row, top_k=args.top_k)
            error = _row_regrounding_failed(out_row)
            if error is not None:
                n_errors += 1
                print(
                    f"taproot-migrate reground: hub {row.get('hub')!r} "
                    f"failed: {error} -- wrote an error sentinel "
                    "('grounding': {'error': ...}) rather than passing the "
                    "row through unchanged: a plain missing 'grounding' key "
                    "reads as 'never regrounded, safe to place as before' -- "
                    "silently reusing that shape here would place exactly "
                    "the atoms this check failed on, fully ungated",
                    file=sys.stderr,
                )
            out_rows.append(out_row)
    finally:
        store.close()

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in out_rows))
    print(
        f"taproot-migrate reground: {n_split} split hub(s) processed, "
        f"{n_errors} error(s), wrote {len(out_rows)} row(s) to {args.out}"
    )
    if n_errors > 0:
        # Mirrors _run_canary's/apply's exit(1) on failure -- an operator
        # watching exit codes must see an errored batch, not a
        # clean-looking summary print with silently pass-through rows.
        sys.exit(1)


def _file_review_todo(
    store: Any, hub_ref_id: int, reason: str, detail: dict[str, Any]
) -> None:
    """``apply``'s production ``todo_fn``: a minimal ``kind='todo'`` for a
    hub :func:`~precis.taproot.apply_migrate.apply_dry_run` couldn't
    auto-apply (an unverified evidence edge, a no-claim hub carrying
    evidence, a would-strand-to-zero abort, ...). Mirrors
    ``workers/chase.py``'s ``_file_taproot_review_todo`` shape (its own
    ``store.tx()``, not any transaction the caller may still hold open —
    filing a review is a side-effect for a human, never part of an atomic
    hub write)."""
    from precis.store.types import Tag

    title = f"taproot-migrate apply: review hub_ref_id={hub_ref_id}"
    with store.tx() as c:
        todo = store.insert_ref(
            kind="todo",
            slug=None,
            title=title[:200],
            # `detail` first so the fixed keys always win -- a caller-
            # supplied detail dict (from apply_dry_run's needs_review call
            # sites) must never be able to shadow `source`/`hub_ref_id`/
            # `reason`, even if a future detail happens to carry one of
            # those names.
            meta={
                **detail,
                "source": "taproot-migrate:apply",
                "hub_ref_id": hub_ref_id,
                "reason": reason,
            },
            conn=c,
        )
        store.add_tag(
            todo.id,
            Tag.closed("STATUS", "open"),
            set_by="agent",
            replace_prefix=True,
            conn=c,
        )


def _run_apply(args: argparse.Namespace) -> None:
    from precis.embedder import make_embedder
    from precis.store import Store
    from precis.taproot.apply_migrate import apply_dry_run

    with open(args.json_path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    if args.only_verdict is not None:
        rows = [r for r in rows if r.get("verdict") == args.only_verdict]
    if args.limit is not None:
        rows = rows[: args.limit]

    store = Store.connect(resolve_dsn(args.database_url))
    # Same reasoning as dry-run's bind: extract_verify_fn's real dispatch
    # (BIG-tier qualify_claim) and the placement cascade's dedup_judge/
    # merge_confirm all resolve the host's serving endpoint through the
    # budget meter, and this run's spend is gated by the breaker. Writes
    # telemetry only -- claim writes go through apply_dry_run's own doors.
    from precis.budget import meter

    meter.bind_store(store)
    embedder = make_embedder(args.embedder, dim=store.embedding_dim())
    try:
        report = apply_dry_run(
            store,
            rows,
            embedder=embedder,
            todo_fn=lambda hub_ref_id, reason, detail: _file_review_todo(
                store, hub_ref_id, reason, detail
            ),
        )
    finally:
        store.close()

    print(f"taproot-migrate apply: {len(rows)} outcome(s) processed")
    for field_name in (
        "stamped_passthrough",
        "split_applied",
        "atoms_placed",
        "atoms_needs_review",
        "edges_repointed",
        "edges_kept_needs_review",
        "atoms_withheld_ungrounded",
        "skipped_already_stamped",
        "skipped_verdict",
        "no_claim_needs_review",
        "no_claim_unevidenced",
        "partial_failures",
    ):
        print(f"  {field_name}: {getattr(report, field_name)}")
    if report.atoms_withheld_reasons:
        print(f"  atoms_withheld_reasons: {report.atoms_withheld_reasons}")

    if report.partial_failures > 0:
        # Mirrors _run_canary's exit(1) on failure -- an operator watching
        # exit codes (a cron/runbook wrapper) must see an errored batch,
        # not just a clean-looking summary print.
        sys.exit(1)


def run(args: argparse.Namespace) -> None:
    """Execute ``precis taproot-migrate <taproot_migrate_cmd>``."""
    if args.taproot_migrate_cmd == "score":
        _run_score(args)
    elif args.taproot_migrate_cmd == "canary":
        _run_canary(args)
    elif args.taproot_migrate_cmd == "dry-run":
        _run_dry_run(args)
    elif args.taproot_migrate_cmd == "reground":
        _run_reground(args)
    elif args.taproot_migrate_cmd == "apply":
        _run_apply(args)
    else:
        print(
            f"taproot-migrate: unknown subcommand {args.taproot_migrate_cmd!r}",
            file=sys.stderr,
        )
        sys.exit(2)


__all__ = ["add_parser", "run"]
