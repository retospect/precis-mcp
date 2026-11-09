"""alchem-diff — re-runnable agreement diff of precis measures vs the
ALCHEM knowledge graph (gr606461, Reto ruling 2026-11-09).

The ALCHEM Lab (Hanyang ERICA) published their extraction graph as CC BY
4.0 TriG — 11,314 ``alchem:PropertyValue`` nodes over 76 papers, each
keyed by DOI. It is one unreviewed gpt-6.1-sol pass, so the report is
AGREEMENT, not precision/recall: per paper it buckets every live output
measure and every ALCHEM value as match / value≠ / unit≠ / ours-only /
theirs-only, with the disagreements and the unmapped-ALCHEM-term list as
the fix queue for docs/backlog/table-numeric-extraction.md and
docs/backlog/measures-substrate.md. Re-run it as extraction improves.

Usage (via the ``alchem-diff`` shell wrapper):

    scripts/alchem-diff --fetch                     # download data.trig once
    scripts/alchem-diff                             # the 12 held papers, dev DB
    scripts/_prod-run scripts/alchem-diff           # against prod
    scripts/alchem-diff --papers pa57151,pa605992
    scripts/alchem-diff --dois 10.1002/aenm.202505964
    scripts/alchem-diff --all-alchem                # every ALCHEM DOI we hold
    scripts/alchem-diff --tol 0.05 --details 10 --json scratch/diff.json
    scripts/alchem-diff --extract --parent 609479   # mint the survey_extract jobs
    scripts/alchem-diff --run alchem-gr606461       # diff one extraction run's rows

The ours side is every live output measure on the paper by default;
``--run <token>`` narrows it to the rows a ``survey_extract`` pass stamped
with ``meta.extraction_run = <token>``, so the table is attributable
extraction-run evidence rather than whatever rows happen to exist.
``--extract`` is the "run precis extraction" half of the gripe: it mints
one queued ``survey_extract`` job per requested paper (idem-keyed on
``(run, paper)``, parented on the ``--parent`` todo) — the one write path;
everything else is SELECT-only. ``PRECIS_DATABASE_URL`` picks the DB (dev
by default; ``scripts/_prod-run`` supplies prod's).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Make `_common` importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import open_store

from precis.extract.alchem import (
    ALCHEM_TRIG_URL,
    AlchemPaper,
    PaperDiff,
    diff_paper,
    ours_from_measure_row,
    parse_alchem_trig,
    render_report,
)

#: The 12 held full-text papers named in gr606461.
DEFAULT_PAPERS = (
    "pa57151",
    "pa605992",
    "pa605993",
    "pa605997",
    "pa605998",
    "pa605999",
    "pa606003",
    "pa606004",
    "pa606007",
    "pa606008",
    "pa606155",
    "pa606157",
)

DEFAULT_TRIG = Path("scratch/alchem-data.trig")

#: The ``extraction_run`` token ``--extract`` stamps when ``--run`` is not
#: given — the gripe's first scored pass over the 12 held papers.
DEFAULT_RUN = "alchem-gr606461"


def fetch_trig(dest: Path) -> Path:
    """Download ``data.trig`` through the SSRF-pinned client."""
    from precis.utils.http import http_client, polite_user_agent
    from precis.utils.safe_fetch import safe_get

    with http_client(timeout=120.0, user_agent=polite_user_agent()) as client:
        resp = safe_get(client, ALCHEM_TRIG_URL)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
    print(f"fetched {ALCHEM_TRIG_URL} -> {dest} ({dest.stat().st_size} B)")
    return dest


def paper_doi(store: Any, ref_id: int) -> str | None:
    """The paper's canonical DOI, from ``ref_identifiers``."""
    for scheme, value, _src in store.list_ref_identifiers(ref_id):
        if scheme == "doi":
            return value.lower()
    return None


def resolve_paper(store: Any, *, handle: str | None, doi: str | None) -> int | None:
    """A request's precis paper ref_id — handle first, then DOI."""
    if handle is not None:
        resolved = store.resolve_handle(handle)
        return int(resolved.ref_id) if resolved is not None else None
    if doi is not None:
        return store.find_paper_ref_by_identifier(doi)
    return None


def diff_one(
    store: Any,
    alchem: dict[str, AlchemPaper],
    *,
    handle: str | None,
    doi: str | None,
    rel_tol: float,
    run: str | None = None,
) -> PaperDiff:
    """One paper: resolve it, pull its live output measures, diff."""
    label = handle or (doi or "?")
    ref_id = resolve_paper(store, handle=handle, doi=doi)
    if handle is not None and ref_id is None:
        return PaperDiff(label=f"{label} (unresolved handle)", doi=None, pairs=[])
    if ref_id is not None and handle is not None:
        doi = paper_doi(store, ref_id) or doi
    gold = alchem.get((doi or "").lower())
    rows = store.measures_for_paper(ref_id) if ref_id is not None else []
    if run is not None:
        rows = [r for r in rows if (r.get("meta") or {}).get("extraction_run") == run]
    ours = [ours_from_measure_row(r) for r in rows]
    if gold is None:
        label += " (no ALCHEM paper)" if doi else " (no DOI on ref)"
    if ref_id is None and handle is None:
        label += " (unresolved DOI)"
    elif ref_id is None:
        label += " (no precis paper)"
    diff = diff_paper(ours, gold.values if gold else [], rel_tol=rel_tol)
    diff.label = label
    diff.doi = doi
    return diff


def _requests(
    args: argparse.Namespace, *, alchem: dict[str, AlchemPaper] | None = None
) -> list[dict[str, Any]]:
    """The paper request list shared by the diff and ``--extract`` paths."""
    if alchem is not None:
        return [{"handle": None, "doi": doi} for doi, p in alchem.items() if p.doi]
    dois = [d.strip() for d in (args.dois or "").split(",") if d.strip()]
    handles = [h.strip() for h in (args.papers or "").split(",") if h.strip()]
    if not handles and not dois:
        handles = list(DEFAULT_PAPERS)
    return [{"handle": h, "doi": None} for h in handles] + [
        {"handle": None, "doi": d} for d in dois
    ]


def mint_extract_jobs(
    store: Any,
    requests: list[dict[str, Any]],
    *,
    run: str,
    parent: int,
    accept: bool,
) -> list[tuple[str, int | None, int | None, bool]]:
    """``--extract``: one queued ``survey_extract`` job per request, stamped
    ``run_token=run`` — idempotent on ``(run, paper)`` so a re-run reuses
    the in-flight job instead of double-minting. ``parent`` is the owning
    todo (``put``'s tree-position rule applies to hand mints too)."""
    from precis.workers._mint import mint_job_once

    out: list[tuple[str, int | None, int | None, bool]] = []
    for req in requests:
        label = req["handle"] or req["doi"] or "?"
        ref_id = resolve_paper(store, handle=req["handle"], doi=req["doi"])
        if ref_id is None:
            print(f"{label}: unresolvable — no job minted")
            out.append((label, None, None, False))
            continue
        with store.tx() as conn:
            job_id, reused = mint_job_once(
                store,
                conn=conn,
                job_type="survey_extract",
                executor="coordinator",
                title="survey_extract (unlinked)",
                params={
                    "paper": ref_id,
                    "run_token": run,
                    "skip_accept": not accept,
                },
                parent_id=parent,
                idem_key=f"alchem-diff:{run}:{ref_id}",
            )
        print(
            f"{label} (pa{ref_id}): job {job_id} "
            + ("already active" if reused else "queued")
        )
        out.append((label, ref_id, job_id, reused))
    return out


def diff_json(diffs: list[PaperDiff], *, run: str | None = None) -> str:
    """The machine-readable form for regression tracking across runs."""

    def pair_dict(p: Any) -> dict[str, Any]:
        return {
            "kind": p.kind,
            "note": p.note,
            "ours": (
                {
                    "id": p.ours.id,
                    "measurand": p.ours.measurand_key,
                    "literal": p.ours.literal,
                    "subject": p.ours.subject,
                }
                if p.ours
                else None
            ),
            "theirs": (
                {
                    "vid": p.theirs.vid,
                    "term": p.theirs.term,
                    "prop": p.theirs.prop,
                    "value_text": p.theirs.value_text,
                    "unit": p.theirs.unit,
                    "material": p.theirs.material,
                    "locator": p.theirs.locator,
                }
                if p.theirs
                else None
            ),
        }

    return json.dumps(
        [
            {
                "label": d.label,
                "doi": d.doi,
                "run": run,
                "counts": dict(d.counts()),
                "unmapped": sorted({t.term or t.prop for t in d.unmapped}),
                "pairs": [pair_dict(p) for p in d.pairs],
            }
            for d in diffs
        ],
        indent=2,
        ensure_ascii=False,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="alchem-diff",
        description="Agreement diff of precis measures vs the ALCHEM graph "
        "(gr606461): match / value / unit / ours-only / theirs-only per paper.",
    )
    ap.add_argument(
        "--trig",
        type=Path,
        default=DEFAULT_TRIG,
        help=f"ALCHEM data.trig path (default {DEFAULT_TRIG})",
    )
    ap.add_argument(
        "--fetch", action="store_true", help="download data.trig to --trig and exit"
    )
    ap.add_argument(
        "--papers",
        help="comma-separated paper handles (default: the 12 held papers)",
    )
    ap.add_argument("--dois", help="comma-separated DOIs — ALCHEM side resolved by DOI")
    ap.add_argument(
        "--all-alchem",
        action="store_true",
        help="every ALCHEM DOI that resolves to a precis paper",
    )
    ap.add_argument(
        "--run",
        help=(
            "extraction_run token — ours is only the measures that token's "
            f"survey_extract pass stamped (default for --extract: {DEFAULT_RUN})"
        ),
    )
    ap.add_argument(
        "--extract",
        action="store_true",
        help=(
            "mint one queued survey_extract job per requested paper "
            "(run_token = --run's value) and exit — the write path"
        ),
    )
    ap.add_argument(
        "--parent",
        type=int,
        help="owning todo ref_id for --extract's jobs (required there)",
    )
    ap.add_argument(
        "--accept",
        action="store_true",
        help="--extract: let each job spawn its measure_accept child "
        "(default skips it — the diff does not need a Wilson verdict)",
    )
    ap.add_argument(
        "--tol",
        type=float,
        default=0.01,
        help="relative interval-gap ceiling for a match (default 0.01)",
    )
    ap.add_argument(
        "--details",
        type=int,
        default=40,
        help="max disagreement/unmatched lines per paper (default 40)",
    )
    ap.add_argument("--json", type=Path, help="also write the JSON report here")
    args = ap.parse_args(argv)

    if args.fetch:
        fetch_trig(args.trig)
        return 0
    if args.extract and args.parent is None:
        ap.error("--extract requires --parent <todo ref_id> for the minted jobs")
    if args.extract and args.all_alchem:
        ap.error("--extract mints jobs for the named papers, not --all-alchem")

    if args.extract:
        # Extraction minting needs the store only — the TriG file may not
        # be fetched yet on this host.
        store, _cfg = open_store()
        try:
            requests = _requests(args)
            mint_extract_jobs(
                store,
                requests,
                run=args.run or DEFAULT_RUN,
                parent=args.parent,
                accept=args.accept,
            )
        finally:
            store.close()
        return 0

    if not args.trig.exists():
        ap.error(f"{args.trig} not found — run `scripts/alchem-diff --fetch` first")

    alchem = parse_alchem_trig(args.trig.read_text(encoding="utf-8"))
    store, _cfg = open_store()
    try:
        requests = _requests(args, alchem=alchem if args.all_alchem else None)
        diffs = [
            diff_one(store, alchem, rel_tol=args.tol, run=args.run, **req)
            for req in requests
        ]
    finally:
        store.close()

    print(render_report(diffs, details=args.details, run=args.run))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(diff_json(diffs, run=args.run) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
