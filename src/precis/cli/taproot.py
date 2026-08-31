"""``precis taproot mint`` — mint claim hubs from a cite-seeded JSON spec.

    precis taproot mint --spec spec.json
    precis taproot mint --json '[{"sentence": "...", "scope": {}, \
"supporters": [{"paper": "pa5", "role": "corroborates"}]}]'
    precis taproot mint --spec spec.json --dry-run
    precis taproot mint --spec spec.json --format json

A thin CLI skin over :func:`precis.taproot.authoring.seed_claim_hub`, itself
a helper over the existing taproot write door (:mod:`precis.taproot.hub`) —
for a human/backfill to mint a claim hub from a draft's already-existing
paper-chunk citations, not a second write path.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any

from precis.cli._common import resolve_dsn

if TYPE_CHECKING:
    from precis.store.store import Store


def add_parser(subparsers: Any) -> None:
    """Register the ``taproot`` subcommand group (``mint`` / ``refine`` /
    ``merge`` / ``backfill`` / ``backfill-grounding`` / ``repair-evidence`` /
    ``verify-edges`` / ``reword-sweep`` / ``direct-mint`` / ``lint``)."""
    p = subparsers.add_parser(
        "taproot", help="Taproot claim-hub authoring (mint hubs from citations)."
    )
    tsub = p.add_subparsers(dest="taproot_cmd", required=True)

    m = tsub.add_parser(
        "mint",
        help="Mint/attach claim hubs from a JSON spec of cited claims.",
    )
    spec_group = m.add_mutually_exclusive_group(required=True)
    spec_group.add_argument("--spec", help="Path to a JSON spec file.")
    spec_group.add_argument(
        "--json", dest="json_spec", help="Inline JSON spec (same shape as --spec)."
    )
    m.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve papers + print what WOULD be minted/attached; write nothing.",
    )
    m.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    m.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for the writes (default: agent).",
    )
    m.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    r = tsub.add_parser(
        "refine",
        help="Link one claim hub as a sharper/reworded version of another "
        "(mint the sharper claim first, then refine).",
    )
    r.add_argument(
        "--from",
        dest="from_hub",
        required=True,
        help="Sharper/newer claim hub (fi<id> handle, pub_id, or ref_id).",
    )
    r.add_argument(
        "--to",
        dest="to_hub",
        required=True,
        help="Coarser/original claim hub this one refines (same forms).",
    )
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve both hubs + print what WOULD be linked; write nothing.",
    )
    r.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for the write (default: agent).",
    )
    r.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    mg = tsub.add_parser(
        "merge",
        help="Collapse one claim hub (--loser) into another (--winner): "
        "repoint every evidence/claim-link edge, dedup against edges the "
        "winner already holds, drop self-loops, retire the loser. "
        "Irreversible (links has no undo) -- dry-run first.",
    )
    mg.add_argument(
        "--loser",
        required=True,
        help="Claim hub to retire and absorb into --winner (fi<id> handle, "
        "pub_id, cite_key, or bare ref_id).",
    )
    mg.add_argument(
        "--winner",
        required=True,
        help="Claim hub that survives, unchanged (same forms as --loser).",
    )
    mg.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full plan (every edge repointed/dropped, the "
        "publish-state check) and write nothing.",
    )
    mg.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for the writes (default: agent).",
    )
    mg.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )

    b = tsub.add_parser(
        "backfill",
        help="Convert a draft chunk's legacy [pc<id>]/[pa<id>] cites into "
        "claim-hub [fi<id>] cites, converging onto existing hubs (dry-run by "
        "default). A fetched whole-paper [pa] is re-grounded to a passage "
        "[pc] (then promotable); stub [pa] cites are skipped (fetch first).",
    )
    b_target = b.add_mutually_exclusive_group(required=True)
    b_target.add_argument("--chunk", help="One draft chunk handle, e.g. dc1652005.")
    b_target.add_argument(
        "--draft", help="A draft slug — backfill every body chunk in it."
    )
    b.add_argument(
        "--apply",
        action="store_true",
        help="Mint/converge hubs + rewrite prose. Default (omitted) is a "
        "read-only dry-run that writes nothing.",
    )
    b.add_argument(
        "--ref-level",
        action="store_true",
        help="[pa] arm: promote a FETCHED whole-paper [pa<id>] cite ref-level "
        "(ungrounded, no passage) and rewrite it to [fi<hub>]. Default (omitted) "
        "re-grounds a fetched [pa]->[pc] at the located passage (no hub yet); "
        "stub [pa] cites are always skipped either way.",
    )
    b.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    b.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for the writes (default: agent; edges are "
        "fingerprinted by meta.origin='draft-backfill', distinct from the "
        "chase pilot's set_by='chase').",
    )
    b.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")

    bg = tsub.add_parser(
        "backfill-grounding",
        help="Upgrade ref-level taproot/draft citation edges to chunk-grounded "
        "(pc/dc).",
    )
    bg.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what WOULD be grounded/resynced; write nothing.",
    )
    bg.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    bg.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )

    re_ = tsub.add_parser(
        "repair-evidence",
        help="Re-ground evidence edges that assert support while anchoring "
        "no passage (meta.source_handle = jsonb null, src_chunk_id NULL) -- "
        "docs/backlog/evidence-edges-assert-support-with-no-passage.md. "
        "DRY-RUN BY DEFAULT: writes a JSONL proposal and makes zero DB "
        "writes unless --apply is given. A source with no supporting "
        "passage is recorded as verify-rejected; the claim is NEVER edited.",
    )
    re_mode = re_.add_mutually_exclusive_group()
    re_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly request the default: propose repairs, write nothing.",
    )
    re_mode.add_argument(
        "--apply",
        action="store_true",
        help="Write the grounding in place (UPDATE links SET src_chunk_id + "
        "meta.source_handle on the ORIGINAL row -- never a second edge). "
        "Default (omitted) is a read-only dry-run.",
    )
    re_.add_argument(
        "--draft",
        default=None,
        help="Restrict to hubs this draft cites (dr<id> handle, draft slug, "
        "or bare ref_id) -- the broken batch is concentrated in one draft's "
        "cohort. Default: every repairable edge in the corpus.",
    )
    re_.add_argument(
        "--cohort",
        choices=("no-passage", "prose-less", "both"),
        default="no-passage",
        help="Which broken shape to repair. 'no-passage' (default): the edge "
        "anchors no passage at all (src_chunk_id NULL). 'prose-less': the edge "
        "anchors a passage that cannot be evidence -- a title/author "
        "front-matter block (gripe 245842). 'both' runs the union, link_id "
        "order. Same repair either way.",
    )
    re_.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Repair at most this many edges (link_id order, so a limited "
        "run is a stable prefix). Default: the whole cohort.",
    )
    re_.add_argument(
        "--tier",
        choices=("small", "medium", "big", "frontier"),
        default="medium",
        help="LLM tier for the support re-verification (default: medium, "
        "re-grounding's own tier). These verdicts were written by something "
        "that read no passage -- 'big' is a defensible re-audit.",
    )
    re_.add_argument(
        "--top-k",
        type=int,
        default=6,
        help="Candidate passages ranked per edge (default: 6).",
    )
    re_.add_argument(
        "--out",
        default=None,
        help="Write the JSONL rows (link_id, hub, source_ref, chunk_id, "
        "quote, reason) here. Default: stdout (the run summary goes to "
        "stderr either way, so stdout stays pipeable).",
    )
    re_.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )

    ve = tsub.add_parser(
        "verify-edges",
        help="Verify withheld/unverified evidence edges against their pinned "
        "passage and stamp the meta.support verdict the publish preflight "
        "reads (2026-08-27 audit: 264 withheld edges across 248 hubs). "
        "DRY-RUN BY DEFAULT: zero DB writes unless --apply. A "
        "non-corroborating verdict is NEVER stamped -- reported (default "
        "cohort) or stripped back to withheld (--unverified-stamped); "
        "pruning stays reground's door. Passage-less edges are skipped + "
        "counted (repair-evidence territory).",
    )
    ve_mode = ve.add_mutually_exclusive_group()
    ve_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly request the default: verify + report, write nothing.",
    )
    ve_mode.add_argument(
        "--apply",
        action="store_true",
        help="Write the verdicts (jsonb-merge onto the edge's meta -- "
        "support/support_reason/caveats/verified_by/verified_at/"
        "verified_claim_sha -- one short transaction per edge). Default "
        "(omitted) is a read-only dry-run (the LLM verify calls still run, "
        "budget-metered).",
    )
    ve.add_argument(
        "--unverified-stamped",
        action="store_true",
        help="Select the untrustworthy-stamp cohort instead: edges carrying "
        "a support value this sweep cannot stand behind -- written at mint "
        "time and never verified (no verified_by), or verified before "
        "verified_claim_sha existed (no sha, so the sentence judged is "
        "unknown). On --apply a corroborating verdict OVERWRITES the stamp "
        "with the real one; a non-corroborating verdict STRIPS "
        "meta.support, returning the edge to withheld behind the publish "
        "gate.",
    )
    ve.add_argument(
        "--hub",
        default=None,
        help="Restrict to one claim hub (fi<id> handle, pub_id, cite_key, "
        "or bare ref_id). Default: every live strict claim hub.",
    )
    ve.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Verify at most this many edges (link_id order, so a limited "
        "run is a stable prefix). Default: the whole cohort.",
    )
    ve.add_argument(
        "--out",
        default=None,
        help="Write the JSONL rows (link_id, hub, source_ref, chunk_id, "
        "verdict, action) here. Default: stdout (the run summary goes to "
        "stderr either way, so stdout stays pipeable).",
    )
    ve.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )

    rw = tsub.add_parser(
        "reword-sweep",
        help="LLM batch reword of claim hubs blocked by the sentence lints "
        "(2026-08-27 audit: 356 of 1,490 live hubs fail ONLY the blocking "
        "lint codes) -- the LLM half of the lint-debt split; `taproot lint "
        "--fix` owns the mechanical notation codes. DRY-RUN BY DEFAULT: "
        "zero DB writes unless --apply (the MEDIUM reword calls still run, "
        "budget-metered). Every proposal is re-validated in code before "
        "any write (blocking lints, numeric/unit survival, citation ban, "
        "length budget); NO-REWORD is an expected verdict, not a failure.",
    )
    rw_mode = rw.add_mutually_exclusive_group()
    rw_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly request the default: propose + report, write nothing.",
    )
    rw_mode.add_argument(
        "--apply",
        action="store_true",
        help="Write accepted rewords through taproot.hub.refine_claim_sentence "
        "-- the single retitle door, so refs.title, the finding_body chunk, "
        "and the pub_id alias stay in sync. Default (omitted) is a "
        "read-only dry-run.",
    )
    rw.add_argument(
        "--hub",
        default=None,
        help="Restrict to one claim hub (fi<id> handle, pub_id, cite_key, "
        "or bare ref_id -- it still has to qualify for the cohort). "
        "Default: every live strict claim hub failing a blocking lint.",
    )
    rw.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Reword at most this many hubs (ref_id order, applied after "
        "the cohort filters, so a limited run is a stable prefix). "
        "Default: the whole cohort.",
    )
    rw.add_argument(
        "--out",
        default=None,
        help="Write the JSONL rows (hub, old, new, status, lint_codes, "
        "checks_failed, reason, applied) here. Default: stdout (the run "
        "summary goes to stderr either way, so stdout stays pipeable).",
    )
    rw.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )

    dm = tsub.add_parser(
        "direct-mint",
        help="Argue a PROPOSED claim against one passage (chunk), then fit it "
        "into the claim tree — the directed-mint front door (docs/backlog/"
        "taproot-directed-claim-minting.md). Dry-run by default; zero "
        "claim-data writes unless --apply is given.",
    )
    dm.add_argument(
        "--claim", required=True, help="The proposed claim sentence to argue."
    )
    dm.add_argument(
        "--chunk",
        required=True,
        type=int,
        help="chunk_id of the passage to argue the claim against (a live "
        "paper/patent/edgar body chunk).",
    )
    dm.add_argument(
        "--demand",
        default=None,
        help="The demanding quest/draft/todo (provenance) — stamped onto the "
        "minted/attached hub's meta.demanded_by so the mint is never unowned.",
    )
    dm.add_argument(
        "--apply",
        action="store_true",
        help="Mint/attach through the write door. Default (omitted) is a "
        "read-only dry-run that makes ZERO claim-data writes (the qualify "
        "LLM call still runs, budget-metered).",
    )
    dm.add_argument(
        "--out",
        default=None,
        help="Write the markdown report to this file instead of stdout.",
    )
    dm.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for the writes (default: agent).",
    )
    dm.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )

    lint = tsub.add_parser(
        "lint",
        help="Lint claim-hub sentences/scope (notation + admissibility); "
        "the measurement instrument for the corpus remediation effort -- "
        "aggregate per-code counts over a cohort of live claim hubs.",
    )
    lint.add_argument(
        "--hub",
        action="append",
        default=None,
        help="Lint one hub (fi<id> handle, pub_id, or cite_key; repeatable). "
        "Default: every live TAPROOT:claim hub.",
    )
    lint.add_argument(
        "--codes",
        action="append",
        default=None,
        help="Filter to named lint codes (the text before the first ':' in "
        "a warning, e.g. 'ascii-ohm'); repeatable.",
    )
    lint.add_argument(
        "--detail",
        action="store_true",
        help="List offending hub ref_ids per code (capped, with an "
        "'and N more' tail) instead of only counts.",
    )
    lint.add_argument(
        "--fix",
        action="store_true",
        help="Propose mechanically-safe notation fixes via "
        "precis.taproot.notation.normalize_notation. Dry-run unless --apply "
        "is also given; a no-op (reported, not an error) if "
        "normalize_notation hasn't landed yet.",
    )
    lint.add_argument(
        "--apply",
        action="store_true",
        help="With --fix, write the proposed fixes (refs.title + the ord=0 "
        "body chunk, atomically). Ignored without --fix; default is dry-run.",
    )
    lint.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    lint.add_argument(
        "--set-by",
        default="agent",
        help="set_by actor slug for --fix --apply writes (default: agent).",
    )
    lint.add_argument(
        "--database-url", default=None, help="Override PRECIS_DATABASE_URL."
    )


def _load_spec(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.spec:
        with open(args.spec, encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = json.loads(args.json_spec)
    if not isinstance(raw, list):
        print("taproot mint: spec must be a JSON array of claims", file=sys.stderr)
        sys.exit(2)
    return raw


def _dry_run_result(store: Store, entry: dict[str, Any]) -> dict[str, Any]:
    from precis.identity import make_pub_id, make_taproot_hub_paper_id
    from precis.taproot.authoring import (
        _evidence_edge_exists,
        find_hub_by_pub_id,
        resolve_paper_ref_id,
    )
    from precis.taproot.hub import _DEFAULT_ROLE, _grounding_chunk_ord

    sentence = entry.get("sentence", "")
    scope = entry.get("scope") or {}
    supporters = entry.get("supporters", [])

    pub_id = make_pub_id(make_taproot_hub_paper_id(sentence, scope))
    hub_ref_id = find_hub_by_pub_id(store, pub_id)

    attached = 0
    already = 0
    ungrounded = 0
    for supporter in supporters:
        # Resolution only -- raises BadInput on an unresolvable (or
        # non-paper/patent) paper, same as the real (write) path would, so
        # a dry-run catches a bad spec before anything is minted.
        paper_ref_id = resolve_paper_ref_id(store, supporter.get("paper"))
        role = supporter.get("role") or _DEFAULT_ROLE
        src_ord = _grounding_chunk_ord(
            store,
            paper_ref_id=paper_ref_id,
            meta={"source_handle": supporter.get("source_handle")},
        )
        # A brand-new hub has no existing evidence at all -- every
        # supporter would be a fresh attach. For a pre-existing hub, check
        # the actual (paper, hub, role, chunk) edge -- reporting every
        # supporter as "already" just because the hub pre-exists was
        # inaccurate for a genuinely new supporter/passage on an old hub.
        if hub_ref_id is not None and _evidence_edge_exists(
            store,
            paper_ref_id=paper_ref_id,
            hub_ref_id=hub_ref_id,
            role=role,
            src_ord=src_ord,
        ):
            already += 1
        else:
            attached += 1
            if src_ord is None:
                ungrounded += 1

    return {
        "pub_id": pub_id,
        "hub_ref_id": hub_ref_id,
        "attached": attached,
        "already": already,
        "ungrounded": ungrounded,
        "sentence": sentence,
        "dry_run": True,
    }


def _preflight_resolve_supporters(store: Store, claims: list[dict[str, Any]]) -> None:
    """Resolve every supporter's ``paper`` across every claim, read-only.

    Runs BEFORE any write so a bad spec anywhere in the batch (an
    unresolvable handle, or one that resolves to a non-paper/patent ref —
    :func:`~precis.taproot.authoring.resolve_paper_ref_id` now rejects
    those) fails closed: nothing gets partially minted to prod for the
    claims that precede the bad one.

    Raises:
        BadInput: same as :func:`~precis.taproot.authoring.resolve_paper_ref_id`
            (or a missing ``'paper'`` field, mirroring
            :func:`~precis.taproot.authoring.seed_claim_hub`'s own check).
    """
    from precis.errors import BadInput
    from precis.taproot.authoring import resolve_paper_ref_id

    for entry in claims:
        for supporter in entry.get("supporters", []):
            paper = supporter.get("paper")
            if paper is None:
                raise BadInput("supporter missing required 'paper' field")
            resolve_paper_ref_id(store, paper)


def _print_results(results: list[dict[str, Any]], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(results, indent=2))
        return

    total_ungrounded = 0
    lint_warnings: list[str] = []
    for r in results:
        snippet = (r.get("sentence") or "").strip()
        if len(snippet) > 60:
            snippet = snippet[:57] + "..."
        suffix = "  [DRY-RUN]" if r.get("dry_run") else ""
        collapsed = r.get("collapsed") or []
        collapsed_note = f", {len(collapsed)} collapsed" if collapsed else ""
        ungrounded = int(r.get("ungrounded") or 0)
        total_ungrounded += ungrounded
        ungrounded_note = f", {ungrounded} ungrounded" if ungrounded else ""
        print(
            f"{r['pub_id']}  {snippet}  "
            f"(+{r['attached']} evidence, {r['already']} already"
            f"{collapsed_note}{ungrounded_note}){suffix}"
        )
        lint_warnings += [
            f"{r['pub_id']}  {w}"
            for w in [*(r.get("notation") or []), *(r.get("scope_lint") or [])]
        ]

    # Advisory lints were previously carried in the result dict and printed
    # only under --format json, i.e. surfaced to nobody on the default path.
    # A scope warning matters most here, at mint: `scope` is in the identity
    # hash, so prose in a scope value forks a hub that should have converged
    # (156/1525 hubs corpus-wide, 2026-08-20). Never blocks the mint.
    for w in lint_warnings:
        print(f"lint: {w}", file=sys.stderr)

    # Nudge: an ungrounded edge cites the whole paper (pa<id>), not the
    # passage (pc<id>) — supplying source_handle makes the citation tree
    # resolve to the supporting chunk.
    if total_ungrounded:
        print(
            f"note: {total_ungrounded} evidence edge(s) attached ref-level "
            "(no grounding chunk) — add source_handle=<pc<id>> per supporter "
            "so the edge cites the passage, not just the paper.",
            file=sys.stderr,
        )


def _run_mint(args: argparse.Namespace) -> None:
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.authoring import seed_claim_hub

    claims = _load_spec(args)
    store = Store.connect(resolve_dsn(args.database_url))
    results: list[dict[str, Any]] = []

    try:
        if not args.dry_run:
            # Pre-flight: read-only resolution of every supporter, before
            # any claim's hub gets minted -- a bad claim N never leaves
            # claims 0..N-1 written while hiding that partial write.
            _preflight_resolve_supporters(store, claims)

        for entry in claims:
            sentence = entry.get("sentence", "")
            if args.dry_run:
                results.append(_dry_run_result(store, entry))
                continue
            scope = entry.get("scope") or {}
            supporters = entry.get("supporters", [])
            out = seed_claim_hub(
                store,
                sentence=sentence,
                scope=scope,
                supporters=supporters,
                set_by=args.set_by,
            )
            out["sentence"] = sentence
            results.append(out)
    except BadInput as exc:
        print(f"taproot mint: error: {exc.cause}", file=sys.stderr)
        if results:
            # A write can still fail mid-batch for some other reason after
            # pre-flight passed (e.g. a concurrent delete) -- never hide a
            # partial run; show what already committed before exiting.
            print(
                "taproot mint: already-committed before the failure:",
                file=sys.stderr,
            )
            _print_results(results, args.format)
        sys.exit(1)
    finally:
        store.close()

    _print_results(results, args.format)


def _run_refine(args: argparse.Namespace) -> None:
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.authoring import resolve_hub_ref_id
    from precis.taproot.hub import link_claims

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        from_hub = resolve_hub_ref_id(store, args.from_hub)
        to_hub = resolve_hub_ref_id(store, args.to_hub)
        if from_hub == to_hub:
            print(
                "taproot refine: error: --from and --to resolve to the same "
                f"hub (ref_id={from_hub}); a claim can't refine itself",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.dry_run:
            print(
                f"[DRY-RUN] would link fi{from_hub} --refines--> fi{to_hub}",
            )
            return
        wrote = link_claims(
            store,
            from_hub_ref_id=from_hub,
            to_hub_ref_id=to_hub,
            set_by=args.set_by,
        )
        verb = "linked" if wrote else "already linked"
        print(f"{verb}: fi{from_hub} --refines--> fi{to_hub}")
    except BadInput as exc:
        print(f"taproot refine: error: {exc.cause}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()


def _print_merge_plan(plan: Any, *, dry_run: bool) -> None:
    from precis.taproot.hub import MERGE_COLLAPSE_RELATION

    tag = "[DRY-RUN] " if dry_run else ""

    if plan.already_merged:
        print(
            f"{tag}no-op: fi{plan.loser_ref_id} was already merged into "
            f"fi{plan.winner_ref_id}"
        )
        return

    if not plan.edges:
        print(f"{tag}no edges on fi{plan.loser_ref_id} to repoint")

    for edge in plan.edges:
        peer = f"fi{edge.other_ref_id}"
        if edge.direction == "outbound":
            old = f"fi{plan.loser_ref_id} --{edge.relation}--> {peer}"
            new = f"fi{plan.winner_ref_id} --{edge.relation}--> {peer}"
        else:
            old = f"{peer} --{edge.relation}--> fi{plan.loser_ref_id}"
            new = f"{peer} --{edge.relation}--> fi{plan.winner_ref_id}"

        if edge.action == "repoint":
            print(f"{tag}repoint (link_id={edge.link_id}): {old}  =>  {new}")
        elif edge.action == "drop_redundant":
            print(
                f"{tag}drop redundant (link_id={edge.link_id}): {old}  -- "
                f"winner already holds this edge as link_id="
                f"{edge.duplicate_of_link_id}"
                + (f"; meta={edge.meta}" if edge.meta else "")
            )
        else:  # drop_self_loop
            print(
                f"{tag}drop self-loop (link_id={edge.link_id}): {old}  -- "
                f"repointing would yield fi{plan.winner_ref_id} --"
                f"{edge.relation}--> fi{plan.winner_ref_id}"
                + (f"; meta={edge.meta}" if edge.meta else "")
            )

    if plan.can_merge:
        print(f"{tag}publish-state check: OK (neither side past 'candidate')")
        print(
            f"{tag}retire fi{plan.loser_ref_id}: refs.retired_at set; "
            f"recorded fi{plan.loser_ref_id} --{MERGE_COLLAPSE_RELATION}--> "
            f"fi{plan.winner_ref_id}"
        )
    else:
        print(f"{tag}publish-state check: BLOCKED -- {plan.block_reason}")
        print(f"{tag}fi{plan.loser_ref_id} stays live -- merge refused")


def _run_merge(args: argparse.Namespace) -> None:
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.authoring import resolve_hub_ref_id, resolve_merge_loser_ref_id
    from precis.taproot.hub import merge_hubs

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        winner = resolve_hub_ref_id(store, args.winner)
        loser = resolve_merge_loser_ref_id(store, args.loser)
        if loser == winner:
            print(
                "taproot merge: error: --loser and --winner resolve to the "
                f"same hub (ref_id={loser}); a hub can't merge into itself",
                file=sys.stderr,
            )
            sys.exit(1)
        plan = merge_hubs(
            store,
            loser_ref_id=loser,
            winner_ref_id=winner,
            set_by=args.set_by,
            dry_run=args.dry_run,
        )
        _print_merge_plan(plan, dry_run=args.dry_run)
    except BadInput as exc:
        print(f"taproot merge: error: {exc.cause}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()


def _resolve_backfill_chunks(store: Store, args: argparse.Namespace) -> list[int]:
    """The draft body chunk_ids to backfill: one for ``--chunk``, all of a
    draft's body chunks (in reading order) for ``--draft``."""
    from precis.errors import BadInput

    if args.chunk:
        token = args.chunk.strip().removeprefix("dc")
        if not token.isdigit():
            raise BadInput(f"--chunk must be a dc<id> handle, got {args.chunk!r}")
        return [int(token)]

    # A draft slug lives in ref_identifiers (id_kind='cite_key'), not a
    # refs.slug column — resolve through the canonical store lookup, not
    # hand-rolled SQL (which drifted against the schema, gr schema-drift test).
    ref = store.get_ref(kind="draft", id=args.draft.strip())
    if ref is None:
        raise BadInput(f"no live draft with slug {args.draft.strip()!r}")
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (int(ref.id),),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _print_backfill(results: list[Any], fmt: str, *, applied: bool) -> None:
    if fmt == "json":
        payload = [
            {
                "chunk_id": r.chunk_id,
                "draft_ref_id": r.draft_ref_id,
                "applied": applied,
                "rewritten": r.rewritten_text is not None,
                "n_ungrounded": r.n_ungrounded,
                "groups": [
                    {
                        "action": p.action,
                        "kind": p.group.kind,
                        "handles": p.group.handles,
                        "hub_ref_id": p.hub_ref_id,
                        "ungrounded": p.ungrounded,
                        "reground_targets": p.reground_targets,
                        "claim": p.claim.sentence if p.claim else None,
                        "note": p.note,
                    }
                    for p in r.plans
                ],
            }
            for r in results
        ]
        print(json.dumps(payload, indent=2))
        return

    tag = "" if applied else "  [DRY-RUN]"
    for r in results:
        if not r.plans:
            print(
                f"dc{r.chunk_id}: no [pc<id>]/[pa<id>] cites (already converted){tag}"
            )
            continue
        counts: dict[str, int] = {}
        for p in r.plans:
            counts[p.action] = counts.get(p.action, 0) + 1
        summary = ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
        ung = r.n_ungrounded
        ung_note = f"  ({ung} ref-level/ungrounded)" if ung else ""
        print(
            f"dc{r.chunk_id}: {len(r.plans)} cite-group(s) — {summary}{ung_note}{tag}"
        )
        for p in r.plans:
            handles = "+".join(p.group.handles)
            if p.action == "attach":
                arrow = f"→ fi{p.hub_ref_id} (converge)"
            elif p.action in ("new", "new_contradicts"):
                arrow = f"→ fi{p.hub_ref_id}" if p.hub_ref_id else "→ new hub"
            elif p.action == "reground":
                pcs = "".join(f"[pc{c}]" for c in p.reground_targets)
                arrow = f"→ {pcs} (re-ground)"
            else:
                arrow = f"({p.action})"
            if p.ungrounded and p.hub_ref_id is not None:
                arrow += " [ref-level]"  # whole-paper [pa] promote, any action
            claim = (p.claim.sentence[:70] + "…") if p.claim else p.note
            print(f"    [{handles}] {arrow}  {claim}")


def _run_backfill(args: argparse.Namespace) -> None:
    from precis.config import load_config
    from precis.errors import BadInput
    from precis.runtime import build_runtime
    from precis.taproot.backfill import apply_chunk, plan_chunk

    cfg = load_config()
    dsn = resolve_dsn(args.database_url)
    if dsn:
        cfg = cfg.model_copy(update={"database_url": dsn})
    # Bulk unattended pass: ``canon.block()`` embeds unguarded per chunk,
    # so take the patient batch budget + no shed (gripe 244419).
    runtime = build_runtime(cfg, interactive=False)
    store = runtime.store
    embedder = getattr(runtime.hub, "embedder", None)
    if store is None:
        print("taproot backfill: no database configured", file=sys.stderr)
        sys.exit(2)
    if embedder is None:
        print(
            "taproot backfill: no embedder configured — the hub ANN "
            "convergence step needs one (set config.embedder / PRECIS_EMBEDDER_URL)",
            file=sys.stderr,
        )
        sys.exit(2)

    results: list[Any] = []
    try:
        chunk_ids = _resolve_backfill_chunks(store, args)
        draft_handler = runtime.hub.handler_for("draft") if args.apply else None
        ref_level = getattr(args, "ref_level", False)
        for cid in chunk_ids:
            if args.apply:
                results.append(
                    apply_chunk(
                        store,
                        embedder,
                        draft_handler,
                        cid,
                        set_by=args.set_by,
                        ref_level=ref_level,
                    )
                )
            else:
                results.append(plan_chunk(store, embedder, cid, ref_level=ref_level))
    except BadInput as exc:
        print(f"taproot backfill: error: {exc.cause}", file=sys.stderr)
        if results:
            _print_backfill(results, args.format, applied=args.apply)
        sys.exit(1)
    finally:
        store.close()

    _print_backfill(results, args.format, applied=args.apply)


_DRAFT_REF_LEVEL_MENTION_SQL = """
    SELECT {select}
    FROM links l
    JOIN refs r ON r.ref_id = l.src_ref_id
    WHERE r.kind = 'draft'
      AND l.relation IN ('cites', 'related-to')
      AND l.meta->>'auto' = 'mention'
      AND l.src_chunk_id IS NULL
"""

_PAPER_EVIDENCE_CANDIDATE_SQL = """
    SELECT l.link_id, l.src_ref_id, l.dst_ref_id, l.relation, l.meta
    FROM links l
    JOIN refs s ON s.ref_id = l.src_ref_id
    WHERE s.kind IN ('paper', 'patent')
      AND l.relation IN ('corroborates', 'establishes', 'contradicts')
      AND l.src_chunk_id IS NULL
      AND l.meta->>'source_handle' IS NOT NULL
      AND l.meta->>'source_handle' <> 'null'
"""


def _backfill_grounding(store: Store, *, dry_run: bool) -> dict[str, Any]:
    """Upgrade ref-level taproot/draft citation edges to chunk-grounded.

    Two independent passes, both idempotent (an already-grounded or
    still-unresolvable edge is a no-op, never an error) and re-runnable:

    PART A — draft ``cites``/``related-to`` auto-mention edges.
    Re-running the draft autolinker
    (:meth:`~precis.handlers.draft.DraftHandler.sync_draft_links`) over
    every draft still carrying a ref-level auto-mention edge migrates it
    to chunk-grounded (drops the stale ref-level rows, re-adds at the
    citing chunk's ord).

    PART B — paper/patent evidence edges (``corroborates``/
    ``establishes``/``contradicts``) with a ``meta.source_handle`` but
    never grounded (pre-fix, or a path that didn't thread ``src_pos``).
    Resolves the handle to its chunk via
    :func:`precis.taproot.hub._grounding_chunk_ord` and sets
    ``src_chunk_id`` via a bare ``UPDATE`` (no handler write path exists
    for "re-ground in place"). A ``UniqueViolation`` (already-grounded
    for that tuple) is caught per-row and counted, never aborting.

    ``dry_run=True`` writes nothing — counts report what WOULD happen.
    """
    from psycopg.errors import UniqueViolation

    from precis.dispatch import Hub
    from precis.handlers.draft import DraftHandler
    from precis.taproot.hub import _grounding_chunk_ord

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "drafts_found": 0,
        "drafts_resynced": 0,
        "draft_edges_before": 0,
        "draft_edges_after": 0,
        "paper_candidates": 0,
        "paper_edges_grounded": 0,
        "unresolved": 0,
        "skipped_collision": 0,
    }

    # ── Part A: draft cites/related-to resync ───────────────────────────
    with store.pool.connection() as conn:
        draft_ref_ids = [
            int(row[0])
            for row in conn.execute(
                _DRAFT_REF_LEVEL_MENTION_SQL.format(select="DISTINCT l.src_ref_id")
            ).fetchall()
        ]
        count_row = conn.execute(
            _DRAFT_REF_LEVEL_MENTION_SQL.format(select="count(*)")
        ).fetchone()
        assert count_row is not None  # count(*) always returns exactly one row
        draft_edges_before = int(count_row[0])
    result["drafts_found"] = len(draft_ref_ids)
    result["draft_edges_before"] = draft_edges_before

    if dry_run:
        # Nothing is written; every candidate draft WOULD be resynced.
        result["drafts_resynced"] = len(draft_ref_ids)
        result["draft_edges_after"] = draft_edges_before
    else:
        handler = DraftHandler(hub=Hub(store=store))
        for ref_id in draft_ref_ids:
            handler.sync_draft_links(ref_id)
        result["drafts_resynced"] = len(draft_ref_ids)
        with store.pool.connection() as conn:
            after_row = conn.execute(
                _DRAFT_REF_LEVEL_MENTION_SQL.format(select="count(*)")
            ).fetchone()
            assert after_row is not None  # count(*) always returns exactly one row
            result["draft_edges_after"] = int(after_row[0])

    # ── Part B: paper/patent evidence edges with a stored source_handle ─
    with store.pool.connection() as conn:
        candidates = conn.execute(_PAPER_EVIDENCE_CANDIDATE_SQL).fetchall()
    result["paper_candidates"] = len(candidates)

    for link_id, src_ref_id, _dst_ref_id, _relation, meta in candidates:
        ord_ = _grounding_chunk_ord(store, paper_ref_id=src_ref_id, meta=meta or {})
        if ord_ is None:
            result["unresolved"] += 1
            continue
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = %s",
                (src_ref_id, ord_),
            ).fetchone()
        if row is None:
            result["unresolved"] += 1
            continue
        if dry_run:
            result["paper_edges_grounded"] += 1
            continue
        chunk_id = int(row[0])
        try:
            with store.pool.connection() as conn:
                conn.execute(
                    "UPDATE links SET src_chunk_id = %s WHERE link_id = %s",
                    (chunk_id, int(link_id)),
                )
        except UniqueViolation:
            result["skipped_collision"] += 1
            continue
        result["paper_edges_grounded"] += 1

    return result


def _print_backfill_grounding(result: dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(result, indent=2))
        return

    suffix = "  [DRY-RUN]" if result["dry_run"] else ""
    print(
        f"drafts: {result['drafts_found']} candidate(s), "
        f"{result['draft_edges_before']} ref-level auto-mention edge(s) -- "
        f"resynced {result['drafts_resynced']} draft(s){suffix}"
    )
    if not result["dry_run"]:
        print(
            "  ref-level auto-mention edges remaining after resync: "
            f"{result['draft_edges_after']}"
        )
    print(
        f"paper/patent evidence: {result['paper_candidates']} candidate(s) -- "
        f"grounded {result['paper_edges_grounded']}, "
        f"unresolved {result['unresolved']}, "
        f"skipped_collision {result['skipped_collision']}{suffix}"
    )


def _run_backfill_grounding(args: argparse.Namespace) -> None:
    from precis.store import Store

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        result = _backfill_grounding(store, dry_run=args.dry_run)
    finally:
        store.close()

    _print_backfill_grounding(result, args.format)


def _resolve_draft_ref_id(store: Store, token: str) -> int:
    """``--draft`` -> a live ``draft`` ref_id. Accepts a ``dr<id>``
    universal handle, a draft slug (cite_key/pub_id), or a bare ref_id --
    the same two-step every other CLI resolver uses
    (:func:`~precis.taproot.authoring.resolve_hub_ref_id`), gated on the
    resolved ref actually being a draft so a typo'd handle fails here
    rather than silently selecting an empty cohort.
    """
    from precis.errors import BadInput
    from precis.utils.mentions import resolve_handle_ref, resolve_handle_target

    ident = token.strip()
    target = resolve_handle_target(store, ident)
    ref_id: int | None = target.dst_ref_id if target is not None else None
    if ref_id is None:
        ref = resolve_handle_ref(store, ident, include_deleted=False)
        ref_id = int(ref.id) if ref is not None else None
    kind: str | None = None
    if ref_id is not None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT kind FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
        kind = str(row[0]) if row is not None else None
    if ref_id is None or kind != "draft":
        raise BadInput(
            f"cannot resolve draft: {token!r}",
            next=(
                "pass a live draft's 'dr<id>' handle, its slug, or its bare "
                "ref_id -- --draft scopes the cohort to hubs that draft cites"
            ),
        )
    return ref_id


def _write_repair_rows(rows: list[dict[str, Any]], out_path: str | None) -> None:
    """The run artifact: one JSON object per line, to ``--out`` or stdout.
    Written in BOTH modes -- a dry-run's rows are the proposal to review,
    an ``--apply`` run's rows are the record of what changed."""
    payload = "\n".join(json.dumps(r) for r in rows)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"{payload}\n" if payload else "")
        return
    if payload:
        print(payload)


def _run_repair_evidence(args: argparse.Namespace) -> None:
    """``precis taproot repair-evidence`` -- re-ground the passage-less
    evidence edges (docs/backlog/evidence-edges-assert-support-with-no-
    passage.md).

    Dry-run is the default and the ONLY mode that needs no flag: without
    ``--apply`` this makes zero DB writes (the LLM verify calls still run,
    budget-metered). A per-edge failure -- most importantly
    :class:`~precis.taproot.reground.RegroundingUnavailable`, "the model
    never ran" -- is caught, written to the artifact as an ``error`` row
    (never as a grounding reason: conflating a dead dispatch with "no
    passage supports this" is exactly the silent failure the strict
    posture exists to prevent), and exits nonzero at the end.
    """
    from precis.budget import meter
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.repair_evidence import (
        repair_edge,
        select_broken_evidence_edges,
        select_prose_less_evidence_edges,
    )
    from precis.utils.llm.router import Tier

    apply = bool(args.apply)
    tier = Tier(args.tier)
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    n_errors = 0

    store = Store.connect(resolve_dsn(args.database_url))
    # The verify dispatch resolves its endpoint through the budget meter
    # and this run's spend is gated by the breaker -- same bind as
    # `taproot-migrate reground`. Telemetry only; the claim-data writes
    # here are the two link columns, and only under --apply.
    meter.bind_store(store)
    try:
        draft_ref_id = _resolve_draft_ref_id(store, args.draft) if args.draft else None
        cohort = args.cohort
        edges = []
        if cohort in ("no-passage", "both"):
            edges += select_broken_evidence_edges(
                store, draft_ref_id=draft_ref_id, limit=args.limit
            )
        if cohort in ("prose-less", "both"):
            edges += select_prose_less_evidence_edges(
                store, draft_ref_id=draft_ref_id, limit=args.limit
            )
        if cohort == "both":
            # Two disjoint SQL predicates (src_chunk_id NULL vs NOT NULL), so
            # the union needs no dedup -- only a stable order, and the limit
            # re-applied across the union rather than per cohort.
            edges.sort(key=lambda e: e.link_id)
            if args.limit is not None:
                edges = edges[: args.limit]
        for edge in edges:
            try:
                result = repair_edge(
                    store,
                    edge.hub_ref_id,
                    edge.source_ref_id,
                    edge.link_id,
                    source_kind=edge.source_kind,
                    apply=apply,
                    tier=tier,
                    top_k=args.top_k,
                )
            except Exception as exc:
                n_errors += 1
                print(
                    f"taproot repair-evidence: link {edge.link_id} "
                    f"(hub fi{edge.hub_ref_id}) failed: {exc}",
                    file=sys.stderr,
                )
                rows.append(
                    {
                        "link_id": edge.link_id,
                        "hub": edge.hub_ref_id,
                        "source_ref": edge.source_ref_id,
                        "chunk_id": None,
                        "source_handle": None,
                        "quote": None,
                        "reason": None,
                        "applied": False,
                        "error": str(exc),
                    }
                )
                continue
            counts[result.status] = counts.get(result.status, 0) + 1
            rows.append(result.to_row())
    except BadInput as exc:
        print(f"taproot repair-evidence: error: {exc.cause}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()

    _write_repair_rows(rows, args.out)
    suffix = "" if apply else "  [DRY-RUN -- nothing written]"
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    print(
        f"taproot repair-evidence: {len(rows)} edge(s) processed -- "
        f"{breakdown}, errors={n_errors}{suffix}",
        file=sys.stderr,
    )
    if n_errors > 0:
        sys.exit(1)


def _run_verify_edges(args: argparse.Namespace) -> None:
    """``precis taproot verify-edges`` -- certify withheld/unverified
    evidence edges for the publish preflight (module docstring:
    :mod:`precis.taproot.verify_edges`).

    Dry-run is the default and the ONLY mode that needs no flag: without
    ``--apply`` this makes zero DB writes (the LLM verify calls still run,
    budget-metered). A ``None`` verdict (LLM failure) is a counted skip; a
    per-edge raise is caught, written to the artifact as an ``error`` row,
    and exits nonzero at the end -- same discipline as repair-evidence.
    """
    from precis.budget import meter
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.verify_edges import (
        count_passageless_edges,
        select_unverified_stamped_edges,
        select_withheld_edges,
        verify_edge,
    )

    apply = bool(args.apply)
    unverified = bool(args.unverified_stamped)
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    n_errors = 0
    n_passageless = 0

    store = Store.connect(resolve_dsn(args.database_url))
    # Same bind as repair-evidence: the verify dispatch resolves its
    # endpoint through the budget meter and this run's spend is gated by
    # the breaker. The only claim-data writes are the per-edge meta
    # patches, and only under --apply.
    meter.bind_store(store)
    try:
        hub_ref_id: int | None = None
        if args.hub:
            from precis.taproot.authoring import resolve_hub_ref_id

            token = args.hub.strip()
            hub_ref_id = resolve_hub_ref_id(
                store, int(token) if token.isdigit() else token
            )
        if unverified:
            edges = select_unverified_stamped_edges(
                store, hub_ref_id=hub_ref_id, limit=args.limit
            )
        else:
            edges = select_withheld_edges(
                store, hub_ref_id=hub_ref_id, limit=args.limit
            )
        n_passageless = count_passageless_edges(
            store, unverified_stamped=unverified, hub_ref_id=hub_ref_id
        )
        for edge in edges:
            try:
                result = verify_edge(
                    store, edge, apply=apply, unverified_stamped=unverified
                )
            except Exception as exc:
                n_errors += 1
                print(
                    f"taproot verify-edges: link {edge.link_id} "
                    f"(hub fi{edge.hub_ref_id}) failed: {exc}",
                    file=sys.stderr,
                )
                rows.append(
                    {
                        "link_id": edge.link_id,
                        "hub": edge.hub_ref_id,
                        "source_ref": edge.source_ref_id,
                        "chunk_id": edge.chunk_id,
                        "supports": None,
                        "support_reason": None,
                        "contradicts": False,
                        "status": None,
                        "action": None,
                        "applied": False,
                        "error": str(exc),
                    }
                )
                continue
            counts[result.status] = counts.get(result.status, 0) + 1
            rows.append(result.to_row())
    except BadInput as exc:
        print(f"taproot verify-edges: error: {exc.cause}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()

    _write_repair_rows(rows, args.out)
    if apply:
        suffix = ""
    else:
        would = f"would stamp {counts.get('verified', 0)}"
        if unverified:
            would += f", strip {counts.get('stripped', 0)}"
        suffix = f"  [DRY-RUN -- nothing written; {would}]"
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    print(
        f"taproot verify-edges: {len(rows)} edge(s) processed -- {breakdown}, "
        f"skipped_passageless={n_passageless}, errors={n_errors}{suffix}",
        file=sys.stderr,
    )
    if n_errors > 0:
        sys.exit(1)


def _run_reword_sweep(args: argparse.Namespace) -> None:
    """``precis taproot reword-sweep`` -- LLM batch reword of lint-blocked
    claim hub sentences through the retitle door (module docstring:
    :mod:`precis.taproot.reword`).

    Dry-run is the default and the ONLY mode that needs no flag: without
    ``--apply`` this makes zero DB writes (the MEDIUM reword calls still
    run, budget-metered). Per-hub failures are named statuses inside the
    sweep (``llm-failed`` / ``apply-failed`` -- the module never raises on
    a dead model or a refused write), counted and reported; only a hard
    error (BadInput) exits nonzero -- same convention as verify-edges,
    whose per-edge failures are counted rows, not a crash.
    """
    from precis.budget import meter
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.reword import run_reword_sweep

    apply = bool(args.apply)

    store = Store.connect(resolve_dsn(args.database_url))
    # Same bind as repair-evidence/verify-edges: the reword dispatch
    # resolves its endpoint through the budget meter and this run's spend
    # is gated by the breaker. The only claim-data writes are the
    # refine_claim_sentence retitles, and only under --apply.
    meter.bind_store(store)
    try:
        hub_ref_id: int | None = None
        if args.hub:
            from precis.taproot.authoring import resolve_hub_ref_id

            token = args.hub.strip()
            hub_ref_id = resolve_hub_ref_id(
                store, int(token) if token.isdigit() else token
            )
        summary = run_reword_sweep(
            store,
            apply=apply,
            limit=args.limit,
            hub=hub_ref_id,
            out=args.out if args.out else sys.stdout,
        )
    except BadInput as exc:
        print(f"taproot reword-sweep: error: {exc.cause}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()

    counts: dict[str, int] = summary["counts"]
    if apply:
        suffix = ""
    else:
        would = f"would reword {counts.get('reworded', 0)}"
        suffix = f"  [DRY-RUN -- nothing written; {would}]"
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    print(
        f"taproot reword-sweep: {summary['processed']} hub(s) processed -- "
        f"{breakdown}, applied={summary['applied']}{suffix}",
        file=sys.stderr,
    )


def _run_direct_mint(args: argparse.Namespace) -> None:
    from precis.budget import meter
    from precis.config import load_config
    from precis.errors import BadInput
    from precis.runtime import build_runtime
    from precis.taproot.directed import directed_mint, render_report

    cfg = load_config()
    dsn = resolve_dsn(args.database_url)
    if dsn:
        cfg = cfg.model_copy(update={"database_url": dsn})
    # Bulk unattended pass: ``canon.block()`` embeds unguarded per chunk,
    # so take the patient batch budget + no shed (gripe 244419).
    runtime = build_runtime(cfg, interactive=False)
    store = runtime.store
    embedder = getattr(runtime.hub, "embedder", None)
    if store is None:
        print("taproot direct-mint: no database configured", file=sys.stderr)
        sys.exit(2)
    if embedder is None:
        print(
            "taproot direct-mint: no embedder configured — the block() ANN "
            "convergence step needs one (set config.embedder / "
            "PRECIS_EMBEDDER_URL)",
            file=sys.stderr,
        )
        sys.exit(2)

    # Bind the store to the budget meter so the qualify LLM dispatch (BIG
    # tier) resolves the host's real served_by endpoint from the DB and is
    # budget-metered — same rationale as taproot-migrate's dry-run: writes
    # llm_call_log telemetry only, never claim data.
    meter.bind_store(store)

    try:
        report = directed_mint(
            store,
            embedder,
            proposed=args.claim,
            chunk_id=args.chunk,
            demand=args.demand,
            apply=args.apply,
            set_by=args.set_by,
        )
    except BadInput as exc:
        print(f"taproot direct-mint: error: {exc.cause}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()

    rendered = render_report(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"wrote report to {args.out}")
    else:
        print(rendered)


#: Cohort query for ``taproot lint``'s default (no ``--hub``) run: every
#: live claim hub. ``DISTINCT`` + explicit column selection is a belt-and-
#: braces guard against row multiplication (``ref_tags``/``tags`` are each
#: unique on their join key here, so this shouldn't duplicate rows -- but a
#: naive version of this join inflated a prior corpus count from 1,524 to
#: 1,710, so the guard stays even though the schema no longer requires it).
#: ``rt.expires_at IS NULL OR rt.expires_at > now()`` excludes an expired
#: tag row -- a bare join with no expiry filter over-counts stale tags.
#:
#: ``LEFT JOIN``s in the ``ord=0`` ``finding_body`` chunk (never retired,
#: the DELETE+INSERT convention keeps at most one live row there) so the
#: ``title-body-divergence``/``missing-body-chunk`` codes (:func:`_lint_hub`)
#: have both sides of the comparison -- a live claim hub for which that
#: chunk is somehow absent still surfaces (as ``chunk_text IS NULL``) rather
#: than silently dropping out of the cohort.
_LINT_COHORT_SQL = """
    SELECT DISTINCT r.ref_id, r.title, r.meta, ch.text
    FROM refs r
    JOIN ref_tags rt ON rt.ref_id = r.ref_id
    JOIN tags t ON t.tag_id = rt.tag_id
               AND t.namespace = 'TAPROOT' AND t.value = 'claim'
    LEFT JOIN chunks ch ON ch.ref_id = r.ref_id AND ch.ord = 0
                        AND ch.chunk_kind = 'finding_body'
                        AND ch.retired_at IS NULL
    WHERE r.kind = 'finding'
      AND r.retired_at IS NULL
      AND (rt.expires_at IS NULL OR rt.expires_at > now())
    ORDER BY r.ref_id
"""

#: Per-code offending-ref_id cap for ``--detail`` output (text and json
#: alike) -- a corpus-wide code can hit hundreds of hubs; the aggregate
#: count already carries that signal, --detail is for "show me a sample."
_LINT_DETAIL_CAP = 20


def _select_lint_cohort(
    store: Store, hubs: list[str] | None
) -> list[tuple[int, str, dict[str, Any], str | None]]:
    """Resolve the lint cohort to ``(ref_id, title, meta, body_text)`` rows.

    ``hubs`` (from repeatable ``--hub``) resolves each handle through the
    same ``resolve_hub_ref_id`` the ``refine`` subcommand uses (fi<id> /
    pub_id / cite_key, gated on being a live claim hub) and preserves the
    caller's order, deduped. ``None``/empty falls back to
    :data:`_LINT_COHORT_SQL` -- every live claim hub. ``body_text`` is the
    hub's live ``ord=0`` ``finding_body`` chunk text, or ``None`` when it's
    missing (feeds :func:`_lint_hub`'s ``title-body-divergence`` /
    ``missing-body-chunk`` codes).
    """
    if hubs:
        from precis.taproot.authoring import resolve_hub_ref_id

        ref_ids: list[int] = []
        seen: set[int] = set()
        for h in hubs:
            rid = resolve_hub_ref_id(store, h)
            if rid not in seen:
                seen.add(rid)
                ref_ids.append(rid)
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.ref_id, r.title, r.meta, ch.text "
                "FROM refs r "
                "LEFT JOIN chunks ch ON ch.ref_id = r.ref_id AND ch.ord = 0 "
                "                    AND ch.chunk_kind = 'finding_body' "
                "                    AND ch.retired_at IS NULL "
                "WHERE r.ref_id = ANY(%s) AND r.retired_at IS NULL",
                (ref_ids,),
            ).fetchall()
        by_id = {int(r[0]): (str(r[1] or ""), dict(r[2] or {}), r[3]) for r in rows}
        return [(rid, *by_id[rid]) for rid in ref_ids if rid in by_id]

    with store.pool.connection() as conn:
        rows = conn.execute(_LINT_COHORT_SQL).fetchall()
    return [(int(r[0]), str(r[1] or ""), dict(r[2] or {}), r[3]) for r in rows]


#: ``title-body-divergence``'s warning text -- kept as one constant so the
#: comment about REPORTING-only stays attached to the string it applies to
#: no matter where it's emitted from.
#:
#: **Never wire this into ``--fix``.** Picking which of ``refs.title`` /
#: the ``finding_body`` chunk is authoritative is a human judgment call,
#: not a mechanical rule -- the 200-char truncation repair
#: (docs/backlog/hub-title-200-truncation-via-stale-mcp.md) needed the
#: chunk for the 306 non-frozen hubs but ``nanopub_publish.approved_title``
#: for the 26 frozen ones, because 21 of those had a reviewer-authored
#: reword at approval that the chunk never saw. Automating "always prefer
#: the chunk" (or the title) here would have destroyed those 21 roundtrip.
_TITLE_BODY_DIVERGENCE_MSG = (
    "title-body-divergence: refs.title and the ord=0 finding_body chunk "
    "disagree after stripping -- reporting only, this is a human call "
    "(see docs/backlog/hub-title-200-truncation-via-stale-mcp.md)"
)

#: ``missing-body-chunk``'s warning text -- a live claim hub with no live
#: ``ord=0`` ``finding_body`` chunk is itself a finding (every hub should
#: have one per :func:`~precis.taproot.hub.mint_hub`), independent of
#: whatever ``refs.title`` says.
_MISSING_BODY_CHUNK_MSG = (
    "missing-body-chunk: no live ord=0 finding_body chunk for this hub"
)


def _lint_hub(title: str, meta: dict[str, Any], body: str | None = None) -> list[str]:
    """Run both string linters over one hub's sentence (``refs.title``) +
    scope (``refs.meta->'scope'``), plus the DB-derived title/body
    comparison, and return the concatenated warning list.

    ``body`` is the hub's ``ord=0`` ``finding_body`` chunk text (from
    :func:`_select_lint_cohort`'s ``LEFT JOIN``); ``None`` means the join
    found no live row for it. ``title-body-divergence`` and
    ``missing-body-chunk`` are mutually exclusive per hub -- there is
    nothing to "diverge" against when the chunk itself is absent.
    """
    from precis.taproot.notation import lint_notation
    from precis.taproot.sentence_lint import lint_claim_sentence, lint_scope

    scope = (meta or {}).get("scope") or {}
    warnings = [*lint_notation(title), *lint_claim_sentence(title), *lint_scope(scope)]
    if body is None:
        warnings.append(_MISSING_BODY_CHUNK_MSG)
    elif body.strip() != title.strip():
        warnings.append(_TITLE_BODY_DIVERGENCE_MSG)
    return warnings


def _lint_code(warning: str) -> str:
    """The lint code is the text before the first colon, e.g.
    ``"ascii-ohm: 'kOhm' found -- use 'kΩ'"`` -> ``"ascii-ohm"``."""
    return warning.split(":", 1)[0]


def _lint_cohort(
    cohort: list[tuple[int, str, dict[str, Any], str | None]], codes: list[str] | None
) -> dict[str, Any]:
    """Aggregate lint warnings across ``cohort`` by code.

    ``codes`` (from repeatable ``--codes``), when given, filters to only
    those named codes -- both in the per-code breakdown and in whether a
    hub counts toward ``hubs_with_warnings``.
    """
    code_filter = set(codes) if codes else None
    per_code: dict[str, list[int]] = {}
    hubs_with_warnings = 0
    for ref_id, title, meta, body in cohort:
        hit = False
        for warning in _lint_hub(title, meta, body):
            code = _lint_code(warning)
            if code_filter is not None and code not in code_filter:
                continue
            per_code.setdefault(code, []).append(ref_id)
            hit = True
        if hit:
            hubs_with_warnings += 1
    return {
        "cohort_size": len(cohort),
        "hubs_with_warnings": hubs_with_warnings,
        "hubs_clean": len(cohort) - hubs_with_warnings,
        "codes": per_code,
    }


def _print_lint(result: dict[str, Any], fmt: str, *, detail: bool) -> None:
    codes: dict[str, list[int]] = result["codes"]

    if fmt == "json":
        codes_payload: dict[str, Any] = {}
        for code, ref_ids in sorted(codes.items()):
            entry: dict[str, Any] = {"count": len(ref_ids)}
            if detail:
                shown = ref_ids[:_LINT_DETAIL_CAP]
                entry["hub_ids"] = [f"fi{r}" for r in shown]
                if len(ref_ids) > _LINT_DETAIL_CAP:
                    entry["more"] = len(ref_ids) - _LINT_DETAIL_CAP
            codes_payload[code] = entry
        print(
            json.dumps(
                {
                    "cohort_size": result["cohort_size"],
                    "hubs_with_warnings": result["hubs_with_warnings"],
                    "hubs_clean": result["hubs_clean"],
                    "codes": codes_payload,
                },
                indent=2,
            )
        )
        return

    print(
        f"cohort: {result['cohort_size']} hub(s) -- "
        f"{result['hubs_with_warnings']} with warning(s), "
        f"{result['hubs_clean']} clean"
    )
    if not codes:
        print("no lint warnings.")
        return
    for code, ref_ids in sorted(codes.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        print(f"  {code}: {len(ref_ids)}")
        if detail:
            shown = ref_ids[:_LINT_DETAIL_CAP]
            print("    " + ", ".join(f"fi{r}" for r in shown))
            if len(ref_ids) > _LINT_DETAIL_CAP:
                print(f"    ...and {len(ref_ids) - _LINT_DETAIL_CAP} more")


def _run_lint_fix(
    store: Store,
    cohort: list[tuple[int, str, dict[str, Any], str | None]],
    *,
    apply: bool,
    set_by: str,
    results: list[dict[str, Any]],
) -> bool:
    """Propose (``apply=False``) or write (``apply=True``)
    mechanically-safe notation fixes. Appends one entry per hub to the
    caller-owned ``results`` and returns ``notation_available``.

    ``results`` is a parameter, not a return value, **so a mid-batch
    failure still reports what already committed** — each
    ``refine_claim_sentence`` call commits its own transaction, so a
    raise on hub N leaves hubs 1..N-1 live-rewritten; returning the list
    would discard that record (same discipline as :func:`_run_mint`).

    ``normalize_notation`` is imported defensively (a sibling agent may
    still be landing it), degrading to ``([], False)`` rather than
    Import/AttributeError if unshipped.

    An ``apply=True`` write goes through
    :func:`~precis.taproot.hub.refine_claim_sentence` — the write door
    that updates ``refs.title`` AND the ``ord=0`` body chunk in one
    transaction so they can't diverge, and itself reads ``refs.title``
    back to assert an exact match, raising
    :class:`~precis.taproot.hub.TitleRoundTripError` otherwise. No
    separate check needed here — the write door owns the guarantee for
    every caller.
    """
    import importlib

    notation_mod = importlib.import_module("precis.taproot.notation")
    normalize = getattr(notation_mod, "normalize_notation", None)
    if normalize is None:
        return False

    from precis.taproot.hub import refine_claim_sentence

    for ref_id, title, _meta, _body in cohort:
        new_title, notes = normalize(title)
        changed = new_title.strip() != title.strip()
        entry: dict[str, Any] = {
            "hub_ref_id": ref_id,
            "old_title": title,
            "new_title": new_title,
            "changed": changed,
            "notes": list(notes),
            "applied": False,
        }
        # Append BEFORE the write, so a raise still leaves this hub visible
        # in the partial report as `applied: False`.
        results.append(entry)
        if changed and apply:
            refine_claim_sentence(store, ref_id, new_title, set_by=set_by)
            entry["applied"] = True
    return True


def _print_lint_fix(
    results: list[dict[str, Any]], fmt: str, *, applied: bool, notation_available: bool
) -> None:
    if fmt == "json":
        print(
            json.dumps(
                {"notation_available": notation_available, "results": results}, indent=2
            )
        )
        return

    if not notation_available:
        print(
            "taproot lint --fix: normalize_notation not available in "
            "precis.taproot.notation yet -- no-op.",
            file=sys.stderr,
        )
        return

    changed = [r for r in results if r["changed"]]
    if not changed:
        print(
            f"no mechanically-safe notation fixes proposed ({len(results)} hub(s) checked)."
        )
        return

    tag = "" if applied else "  [DRY-RUN]"
    for r in changed:
        verb = "applied" if r["applied"] else "would apply"
        print(f"fi{r['hub_ref_id']}: {verb}{tag}")
        print(f"  - {r['old_title']}")
        print(f"  + {r['new_title']}")
        if r["notes"]:
            print(f"    notes: {', '.join(r['notes'])}")


def _run_lint(args: argparse.Namespace) -> None:
    from precis.errors import BadInput
    from precis.store import Store
    from precis.taproot.hub import TitleRoundTripError

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        cohort = _select_lint_cohort(store, args.hub)

        if args.fix:
            results: list[dict[str, Any]] = []
            try:
                notation_available = _run_lint_fix(
                    store,
                    cohort,
                    apply=args.apply,
                    set_by=args.set_by,
                    results=results,
                )
            except TitleRoundTripError as exc:
                # Never hide a partial run: hubs before the failure are
                # already committed, so show them before exiting.
                _print_lint_fix(
                    results,
                    args.format,
                    applied=args.apply,
                    notation_available=True,
                )
                done = sum(1 for r in results if r["applied"])
                print(
                    f"taproot lint: error: {exc}\n"
                    f"  PARTIAL RUN -- {done} hub(s) already written before "
                    f"the failure (listed above as 'applied').",
                    file=sys.stderr,
                )
                sys.exit(1)
            _print_lint_fix(
                results,
                args.format,
                applied=args.apply,
                notation_available=notation_available,
            )
            return

        result = _lint_cohort(cohort, args.codes)
        _print_lint(result, args.format, detail=args.detail)
    except BadInput as exc:
        print(f"taproot lint: error: {exc.cause}", file=sys.stderr)
        sys.exit(1)
    finally:
        store.close()


def run(args: argparse.Namespace) -> None:
    """Execute ``precis taproot <taproot_cmd>``."""
    if args.taproot_cmd == "mint":
        _run_mint(args)
    elif args.taproot_cmd == "refine":
        _run_refine(args)
    elif args.taproot_cmd == "merge":
        _run_merge(args)
    elif args.taproot_cmd == "backfill":
        _run_backfill(args)
    elif args.taproot_cmd == "backfill-grounding":
        _run_backfill_grounding(args)
    elif args.taproot_cmd == "repair-evidence":
        _run_repair_evidence(args)
    elif args.taproot_cmd == "verify-edges":
        _run_verify_edges(args)
    elif args.taproot_cmd == "reword-sweep":
        _run_reword_sweep(args)
    elif args.taproot_cmd == "direct-mint":
        _run_direct_mint(args)
    elif args.taproot_cmd == "lint":
        _run_lint(args)
    else:
        print(f"taproot: unknown subcommand {args.taproot_cmd!r}", file=sys.stderr)
        sys.exit(2)


__all__ = ["add_parser", "run"]
