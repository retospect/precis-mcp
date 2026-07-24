#!/usr/bin/env python3
"""Deterministic sampler for the context-quality audit.

For each row in the catalog documented at ``docs/design/context-quality-eval.md``
(that file is the source of truth for *which* contexts matter and *why* — this
script only pulls one real, representative sample of each so ``PROCEDURE.md``
has something concrete to judge against ``RUBRIC.md``), this script produces
``out/NN-<slug>.md`` (the rendered text, with a small header noting which call
or builder produced it) and a summary ``out/manifest.json`` — a
``{"kind_roster": {...}, "rows": [...]}`` document: ``rows`` is the per-catalog-row
outcome (as before), and ``kind_roster`` snapshots the connected server's live
``Hub.kinds``/``kinds_supporting('search')`` so a judge can tell "kind missing
from a cross-kind disclosure = a real gap" apart from "this build's roster
never had the kind registered" (see :func:`_kind_roster`).

Two sampler shapes:

* **Interactive** — invokes the real dispatch surface read-only
  (``PrecisRuntime.dispatch`` — :mod:`precis.runtime.dispatch`). Picks the
  most-recently-updated ref of a kind and renders its key view(s), or runs a
  representative cross-kind search. Never calls ``put``/``edit``/``delete``/
  ``tag`` — this harness only reads; filing findings as gripes is the human
  agent's job in ``PROCEDURE.md``, done through a *separate* MCP connection.
* **Agentic** — calls the profile's own prompt builder directly as a DRY RUN
  (assembles the text, spends no LLM tokens): the planner
  (:func:`precis.workers.planner_prompt.build_planner_prompts`) and the
  review-tier reviewers (:func:`precis.workers.review._build_prompt`, shared
  by ``structural`` and ``deep_review``).

Any single sampler that raises is caught, logged to the manifest with
``skipped: true`` + a reason, and the run continues — one broken context must
never sink the rest of the sample. A sampler for which no dry-run entry
exists yet is expected to raise ``NotImplementedError`` with a note; treat
that the same way (log + skip), don't add ad-hoc handling per-slug.

Usage::

    uv run scripts/context-audit/capture.py --list
    uv run scripts/context-audit/capture.py
    uv run scripts/context-audit/capture.py --only todo-tree
    uv run scripts/context-audit/capture.py --limit 3 --out /tmp/sample

Env — read-only Store connection (never write through this script):

    PRECIS_DATABASE_URL   dsn; e.g. a prod read hop (127.0.0.1:6432 as
                           agent_rw — see scripts/prod-psql) or a local
                           dev-DB precis (scripts/dev). ``--list`` needs no DB.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.runtime.core import PrecisRuntime
    from precis.store import Store

log = logging.getLogger("context_audit.capture")

DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "out"

#: The rendered handle an agent sees for a todo leaf (``td42994``) — used to
#: lift a representative ref_id out of a real ``search(..., view='doable')``
#: render rather than a second bespoke SQL query. Renders use kind-prefixed
#: handles like ``td123``/``pa123``, not a bare ``#123``.
_REF_ID_RE = re.compile(r"\btd(\d+)")


@dataclass(frozen=True, slots=True)
class Ctx:
    """What a sampler gets to work with."""

    store: Store
    runtime: PrecisRuntime


@dataclass(frozen=True, slots=True)
class SampleResult:
    """What a sampler returns on success."""

    text: str
    source_call: str
    ref_handle: str | None = None


@dataclass(frozen=True, slots=True)
class ContextRow:
    """One registry entry — one row of the catalog in
    ``docs/design/context-quality-eval.md``."""

    slug: str
    axis: str  # "interactive" | "agentic"
    sampler: Callable[[Ctx], SampleResult]
    note: str = ""


# ── shared helpers ────────────────────────────────────────────────────


def _recent_ref_id(store: Store, kind: str) -> int | None:
    """The most-recently-updated live ref_id of ``kind``, or None if there
    aren't any. Read-only — mirrors the ``ORDER BY updated_at DESC LIMIT 1``
    idiom used across ``src/precis/workers/`` for "pick a representative
    recent ref"."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = %s AND deleted_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
    return int(row[0]) if row else None


def _first_ref_id_in(rendered: str) -> int | None:
    """Lift the first ``#N`` ref id out of a rendered dispatch response."""
    m = _REF_ID_RE.search(rendered)
    return int(m.group(1)) if m else None


def _require(rid: int | None, what: str) -> int:
    if rid is None:
        raise LookupError(f"no {what} found — corpus may be empty for this kind")
    return rid


# ── interactive samplers (real dispatch, read-only) ──────────────────


def sample_todo_tree(ctx: Ctx) -> SampleResult:
    rid = _require(_recent_ref_id(ctx.store, "todo"), "todo refs")
    call = f"get(kind='todo', id={rid}, view='tree')"
    text = ctx.runtime.dispatch("get", {"kind": "todo", "id": rid, "view": "tree"})
    return SampleResult(text, call, ref_handle=f"todo:{rid}")


def sample_todo_doable(ctx: Ctx) -> SampleResult:
    call = "search(kind='todo', view='doable')"
    text = ctx.runtime.dispatch("search", {"kind": "todo", "view": "doable"})
    return SampleResult(text, call)


def sample_paper_read(ctx: Ctx) -> SampleResult:
    rid = _require(_recent_ref_id(ctx.store, "paper"), "paper refs")
    call = f"get(kind='paper', id={rid})"
    text = ctx.runtime.dispatch("get", {"kind": "paper", "id": rid})
    return SampleResult(text, call, ref_handle=f"paper:{rid}")


def sample_quest_tree(ctx: Ctx) -> SampleResult:
    rid = _require(_recent_ref_id(ctx.store, "quest"), "quest refs")
    call = f"get(kind='quest', id={rid}, view='tree')"
    text = ctx.runtime.dispatch("get", {"kind": "quest", "id": rid, "view": "tree"})
    return SampleResult(text, call, ref_handle=f"quest:{rid}")


def sample_draft_toc(ctx: Ctx) -> SampleResult:
    rid = _require(_recent_ref_id(ctx.store, "draft"), "draft refs")
    call = f"get(kind='draft', id={rid}, view='toc')"
    text = ctx.runtime.dispatch("get", {"kind": "draft", "id": rid, "view": "toc"})
    return SampleResult(text, call, ref_handle=f"draft:{rid}")


def sample_gripe_open(ctx: Ctx) -> SampleResult:
    # The open-gripe list is a get list-view (``id='/open'``), not a search;
    # ``search(kind='gripe', status='open')`` is rejected (needs q=/tags=).
    call = "get(kind='gripe', id='/open')"
    text = ctx.runtime.dispatch("get", {"kind": "gripe", "id": "/open"})
    return SampleResult(text, call)


def sample_cross_kind_search(ctx: Ctx) -> SampleResult:
    # A representative broad query — not scoped to one kind — to exercise the
    # cross-kind fan-out surface (`kind='*'`).
    q = "catalyst reaction pathway"
    call = f"search(kind='*', q={q!r})"
    text = ctx.runtime.dispatch("search", {"kind": "*", "q": q})
    return SampleResult(text, call)


def sample_skill_toc(ctx: Ctx) -> SampleResult:
    call = "get(kind='skill', id='toc')"
    text = ctx.runtime.dispatch("get", {"kind": "skill", "id": "toc"})
    return SampleResult(text, call, ref_handle="skill:toc")


def sample_skill_overview(ctx: Ctx) -> SampleResult:
    # A real skill body (not the corpus toc) — a stable id rather than a
    # recency pick, since which skill was last edited is meaningless here;
    # `precis-overview` is the master kinds table, always present.
    slug = "precis-overview"
    call = f"get(kind='skill', id={slug!r})"
    text = ctx.runtime.dispatch("get", {"kind": "skill", "id": slug})
    return SampleResult(text, call, ref_handle=f"skill:{slug}")


# ── todo triage-surface views (search(kind='todo', view=…)) ──────────


def _sample_todo_view(view: str) -> Callable[[Ctx], SampleResult]:
    """Build a sampler for one of the tree-aware ``search(kind='todo',
    view=…)`` triage surfaces (`_todo_views.py`'s ``TodoView`` enum) — each
    is a plain search with no id, so there's nothing per-view to vary
    beyond the ``view=`` string itself."""

    def _sampler(ctx: Ctx) -> SampleResult:
        call = f"search(kind='todo', view={view!r})"
        text = ctx.runtime.dispatch("search", {"kind": "todo", "view": view})
        return SampleResult(text, call)

    _sampler.__name__ = f"sample_todo_{view.replace('-', '_')}"
    return _sampler


sample_todo_attention = _sample_todo_view("attention")
sample_todo_strategic = _sample_todo_view("strategic")
sample_todo_blocked = _sample_todo_view("blocked")
sample_todo_ask_user = _sample_todo_view("ask-user")
sample_todo_waiting = _sample_todo_view("waiting")


# ── quest per-view get (get(kind='quest', id=N, view=…)) ─────────────


def _sample_quest_view(view: str) -> Callable[[Ctx], SampleResult]:
    """Build a sampler for one of quest's per-ref views (`quest.py`'s
    ``view='tree'|'gaps'|'dossier'|'frontier'|'leaderboard'`` branches) —
    each needs a concrete recent quest ref, then differs only in ``view=``."""

    def _sampler(ctx: Ctx) -> SampleResult:
        rid = _require(_recent_ref_id(ctx.store, "quest"), "quest refs")
        call = f"get(kind='quest', id={rid}, view={view!r})"
        text = ctx.runtime.dispatch("get", {"kind": "quest", "id": rid, "view": view})
        return SampleResult(text, call, ref_handle=f"quest:{rid}")

    _sampler.__name__ = f"sample_quest_{view}"
    return _sampler


sample_quest_gaps = _sample_quest_view("gaps")
sample_quest_frontier = _sample_quest_view("frontier")
sample_quest_dossier = _sample_quest_view("dossier")


def sample_draft_wordcount(ctx: Ctx) -> SampleResult:
    rid = _require(_recent_ref_id(ctx.store, "draft"), "draft refs")
    call = f"get(kind='draft', id={rid}, view='wordcount')"
    text = ctx.runtime.dispatch(
        "get", {"kind": "draft", "id": rid, "view": "wordcount"}
    )
    return SampleResult(text, call, ref_handle=f"draft:{rid}")


def sample_memory_sticky(ctx: Ctx) -> SampleResult:
    # The pinned set — a list view (`id='/sticky'`), not a search; mirrors
    # ``sample_gripe_open``'s `id='/<view>'` shape.
    call = "get(kind='memory', id='/sticky')"
    text = ctx.runtime.dispatch("get", {"kind": "memory", "id": "/sticky"})
    return SampleResult(text, call)


def sample_memory_recent(ctx: Ctx) -> SampleResult:
    rid = _require(_recent_ref_id(ctx.store, "memory"), "memory refs")
    call = f"get(kind='memory', id={rid})"
    text = ctx.runtime.dispatch("get", {"kind": "memory", "id": rid})
    return SampleResult(text, call, ref_handle=f"memory:{rid}")


def sample_gripe_read(ctx: Ctx) -> SampleResult:
    # The default per-ref render, complementing ``sample_gripe_open``'s
    # `id='/open'` list view.
    rid = _require(_recent_ref_id(ctx.store, "gripe"), "gripe refs")
    call = f"get(kind='gripe', id={rid})"
    text = ctx.runtime.dispatch("get", {"kind": "gripe", "id": rid})
    return SampleResult(text, call, ref_handle=f"gripe:{rid}")


def sample_search_keywords(ctx: Ctx) -> SampleResult:
    # Cross-kind keyword-only TOON (F20 discovery layer) — "what topics span
    # the corpus" for a query, no preview text.
    q = "catalyst reaction pathway"
    call = f"search(kind='*', view='keywords', q={q!r})"
    text = ctx.runtime.dispatch("search", {"kind": "*", "view": "keywords", "q": q})
    return SampleResult(text, call)


def sample_search_stubs(ctx: Ctx) -> SampleResult:
    # Paper acquire backlog — store-only (`Store.stub_backlog`), no
    # PaperHandler/marker/semanticscholar import, so this is host-safe
    # despite being paper-flavored.
    call = "search(view='stubs')"
    text = ctx.runtime.dispatch("search", {"view": "stubs"})
    return SampleResult(text, call)


def sample_search_source_recency(ctx: Ctx) -> SampleResult:
    # The unified-item-view Slice 2 source-search primitive
    # (`sort=`/`since=`/`until=`) — one best-chunk-per-ref cross-kind hit
    # list ordered by recency rather than relevance.
    q = "catalyst reaction pathway"
    call = f"search(kind='*', q={q!r}, sort='recency')"
    text = ctx.runtime.dispatch("search", {"kind": "*", "q": q, "sort": "recency"})
    return SampleResult(text, call)


# ── agentic samplers (dry-run prompt builders, no LLM spend) ─────────


def sample_planner_tick(ctx: Ctx) -> SampleResult:
    from precis.workers.planner_prompt import build_planner_prompts

    doable = ctx.runtime.dispatch("search", {"kind": "todo", "view": "doable"})
    rid = _first_ref_id_in(doable)
    if rid is None:
        raise LookupError("no doable todo found to build a planner tick for")
    prompts = build_planner_prompts(ctx.store, ref_id=rid, model="claude-sonnet-5")
    text = (
        "## SYSTEM (cached layer)\n\n"
        f"{prompts.system}\n\n"
        "## USER (variable layer)\n\n"
        f"{prompts.user}"
    )
    call = f"build_planner_prompts(store, ref_id={rid}, model='claude-sonnet-5')"
    return SampleResult(text, call, ref_handle=f"todo:{rid}")


def _sample_reviewer_dry_run(ctx: Ctx, reviewer_name: str) -> SampleResult:
    """Shared dry-run for a review-tier ``Reviewer`` (structural / deep_review):
    assemble its modules against a live ``AssemblyContext`` and render with
    :class:`~precis.utils.prompt.adapters.ClaudeAgentAdapter` — the exact path
    ``run_review_pass`` uses, minus the actual LLM dispatch."""
    from precis.workers.review import _build_prompt as _review_build_prompt

    if reviewer_name == "structural":
        from precis.workers.structural import STRUCTURAL as reviewer
    elif reviewer_name == "deep_review":
        from precis.workers.deep_review import DEEP_REVIEW as reviewer
    else:  # pragma: no cover - internal misuse only
        raise ValueError(reviewer_name)

    text = _review_build_prompt(reviewer, ctx.store)
    call = (
        f"review._build_prompt({reviewer_name.upper()}, store)  # dry-run, no LLM call"
    )
    return SampleResult(text, call)


def sample_structural_reviewer(ctx: Ctx) -> SampleResult:
    return _sample_reviewer_dry_run(ctx, "structural")


def sample_deep_reviewer(ctx: Ctx) -> SampleResult:
    return _sample_reviewer_dry_run(ctx, "deep_review")


# ── the registry ──────────────────────────────────────────────────────
#
# Small and easy to extend — add a row here as the catalog doc grows.
# Mirror its coverage rather than trying to enumerate every kind; a
# sampler that doesn't have a clean dry-run path yet should raise
# NotImplementedError with a one-line note (caught + logged below as a
# skip, not a crash) instead of being left out of the registry entirely.

CATALOG: list[ContextRow] = [
    ContextRow("todo-tree", "interactive", sample_todo_tree),
    ContextRow("todo-doable-queue", "interactive", sample_todo_doable),
    ContextRow("todo-attention", "interactive", sample_todo_attention),
    ContextRow("todo-strategic", "interactive", sample_todo_strategic),
    ContextRow("todo-blocked", "interactive", sample_todo_blocked),
    ContextRow("todo-ask-user", "interactive", sample_todo_ask_user),
    ContextRow("todo-waiting", "interactive", sample_todo_waiting),
    ContextRow("paper-read", "interactive", sample_paper_read),
    ContextRow("quest-tree", "interactive", sample_quest_tree),
    ContextRow("quest-gaps", "interactive", sample_quest_gaps),
    ContextRow("quest-frontier", "interactive", sample_quest_frontier),
    ContextRow("quest-dossier", "interactive", sample_quest_dossier),
    ContextRow("draft-toc", "interactive", sample_draft_toc),
    ContextRow("draft-wordcount", "interactive", sample_draft_wordcount),
    ContextRow("memory-sticky", "interactive", sample_memory_sticky),
    ContextRow("memory-recent", "interactive", sample_memory_recent),
    ContextRow("gripe-open-search", "interactive", sample_gripe_open),
    ContextRow("gripe-read", "interactive", sample_gripe_read),
    ContextRow("cross-kind-search", "interactive", sample_cross_kind_search),
    ContextRow("search-keywords", "interactive", sample_search_keywords),
    ContextRow("search-stubs", "interactive", sample_search_stubs),
    ContextRow("search-source-recency", "interactive", sample_search_source_recency),
    ContextRow("skill-toc", "interactive", sample_skill_toc),
    ContextRow("skill-overview", "interactive", sample_skill_overview),
    ContextRow("planner-tick", "agentic", sample_planner_tick),
    ContextRow("structural-reviewer", "agentic", sample_structural_reviewer),
    ContextRow("deep-reviewer", "agentic", sample_deep_reviewer),
]

#: Catalog position (1-based, matching ``--list``'s numbering) keyed by slug —
#: used by ``_run`` so an output filename's ``NN-`` prefix is stable across a
#: filtered (``--only``/``--limit``) run rather than a local re-enumeration of
#: whatever subset happened to be selected.
_CATALOG_INDEX: dict[str, int] = {row.slug: i for i, row in enumerate(CATALOG, start=1)}


# ── runner ─────────────────────────────────────────────────────────────


def _connect() -> tuple[Store, PrecisRuntime]:
    """Build a real, read-only-in-practice runtime from ``PRECIS_DATABASE_URL``.

    Uses the same composition root the MCP server boots
    (:func:`precis.runtime.build_runtime`) so a sampled render is byte-for-byte
    what an agent would actually see — not a hand-rolled approximation. This
    harness only issues ``get``/``search`` dispatch calls, so nothing here
    writes, but the connection itself is exactly as capable as any other
    ``agent_rw`` session — point it at a read hop or a dev-DB precis, never
    rely on this script to enforce read-only for you.
    """
    from precis.config import load_config
    from precis.runtime import build_runtime

    cfg = load_config()
    if not cfg.database_url:
        raise SystemExit(
            "PRECIS_DATABASE_URL not set. Point it at a read-only prod hop "
            "(127.0.0.1:6432 as agent_rw — see scripts/prod-psql) or a local "
            "dev-DB precis (scripts/dev). Use --list to see the catalog "
            "without a DB connection."
        )
    runtime = build_runtime(cfg)
    if runtime.store is None:  # pragma: no cover - build_runtime already raises
        raise SystemExit("runtime came up storeless despite database_url set")
    return runtime.store, runtime


def _kind_roster(runtime: PrecisRuntime) -> dict[str, Any]:
    """Snapshot the server's active dispatch-table roster for the manifest.

    A judge reading ``out/manifest.json`` needs to tell "this kind is
    genuinely absent from a cross-kind disclosure" apart from "this build's
    ``Hub`` never registered the kind in the first place" (missing extra,
    unset env var, disabled by ``kind_gate`` — see ``src/precis/dispatch.py``
    §"Failure-mode semantics"). ``Hub.kinds``/``Hub.kinds_supporting`` are the
    authoritative read views for that — same source the boot log's "N kinds
    live" line uses — so record them here rather than letting the judge
    guess from `precis-overview` prose, which can itself drift from the live
    registry (see docs/design/context-quality-eval.md's rubric dimension 5).
    """
    hub = runtime.hub
    return {
        "kinds": sorted(hub.kinds),
        "kinds_supporting_search": sorted(hub.kinds_supporting("search")),
    }


def _run(
    rows: list[ContextRow], out_dir: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    store, runtime = _connect()
    ctx = Ctx(store=store, runtime=runtime)
    kind_roster = _kind_roster(runtime)
    manifest: list[dict[str, Any]] = []
    try:
        for row in rows:
            # Stable index = the row's position in the full CATALOG, not a
            # local re-enumeration of this (possibly filtered) run — so
            # `--only planner-tick` always writes the same `NN-planner-
            # tick.md` a full run would, matching the catalog doc's numbering.
            i = _CATALOG_INDEX[row.slug]
            fname = f"{i:02d}-{row.slug}.md"
            entry: dict[str, Any] = {"slug": row.slug, "axis": row.axis, "file": fname}
            try:
                result = row.sampler(ctx)
                header = (
                    f"<!-- context-audit sample: {row.slug} ({row.axis}) -->\n"
                    f"<!-- source_call: {result.source_call} -->\n"
                    f"<!-- ref_handle: {result.ref_handle or '(none)'} -->\n\n"
                )
                (out_dir / fname).write_text(header + result.text)
                entry.update(
                    {
                        "source_call": result.source_call,
                        "ref_handle": result.ref_handle,
                        "chars": len(result.text),
                        "skipped": False,
                    }
                )
                print(
                    f"[ok]   {row.slug:24s} {len(result.text):>6d} chars  <- {result.source_call}"
                )
            except Exception as exc:  # one bad sampler must not sink the run
                reason = f"{type(exc).__name__}: {exc}"
                entry.update(
                    {
                        "source_call": None,
                        "ref_handle": None,
                        "chars": 0,
                        "skipped": True,
                        "skip_reason": reason,
                    }
                )
                log.warning(
                    "sampler %s raised, skipping: %s", row.slug, reason, exc_info=True
                )
                print(f"[skip] {row.slug:24s} {reason}")
            manifest.append(entry)
    finally:
        store.close()
    return manifest, kind_roster


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the catalog slugs and exit (no DB needed)",
    )
    parser.add_argument(
        "--only", metavar="SLUG", help="sample only this one catalog row"
    )
    parser.add_argument(
        "--limit", type=int, metavar="N", help="sample only the first N rows"
    )
    parser.add_argument(
        "--out",
        metavar="DIR",
        default=str(DEFAULT_OUT_DIR),
        help="output directory (default: out/)",
    )
    args = parser.parse_args(argv)

    if args.list:
        for i, row in enumerate(CATALOG, start=1):
            print(
                f"{i:02d}  {row.slug:24s} [{row.axis}]"
                + (f"  — {row.note}" if row.note else "")
            )
        return 0

    rows = CATALOG
    if args.only:
        rows = [r for r in rows if r.slug == args.only]
        if not rows:
            print(
                f"no catalog row named {args.only!r}; use --list to see slugs",
                file=sys.stderr,
            )
            return 2
    if args.limit is not None:
        rows = rows[: args.limit]

    manifest, kind_roster = _run(rows, Path(args.out))

    out_dir = Path(args.out)
    manifest_doc = {"kind_roster": kind_roster, "rows": manifest}
    (out_dir / "manifest.json").write_text(json.dumps(manifest_doc, indent=2) + "\n")

    n_ok = sum(1 for e in manifest if not e["skipped"])
    n_skipped = len(manifest) - n_ok
    print(
        f"\n{n_ok}/{len(manifest)} sampled ({n_skipped} skipped) -> {out_dir / 'manifest.json'}"
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
