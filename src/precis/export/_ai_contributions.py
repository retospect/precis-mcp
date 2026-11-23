"""The generated "AI contributions" back-matter section (gr725421).

Every exported manuscript carries the statement: when the draft has no
author-written section under that heading, the export generates one so no
export lacks it. A section the author wrote is used as written — the
exporters check :func:`precis.export.walker.has_ai_contributions` and
supply the block only when the draft itself does not.

Stance and wording follow the ``precis-ai-contributions-help`` skill
(Lieblich, "On disclosure and shame", 2026-10-24): say what each model
did; never call it a disclosure. The generated lines name, in order:

* each writing model on record for the draft's chunks, rolled up from
  ``chunk_events.source.writer`` (the stamp
  :mod:`precis.store._draft_ops` merges in at write time — an MCP-session
  edit leaves no ``agentlog`` run, so the event itself carries it):
  distinct models with write counts, first/last dates and the kinds of
  work;
* each model with a logged ``agentlog`` run ``touched``-linked to the
  draft — the ``claude -p`` / Codex worker path that does leave a run
  (named as *used in* the run: ``models_used`` covers work anywhere in
  the run and ``touched`` also covers moves, so the record supports no
  stronger claim);
* each model behind a machine-minted claim hub the draft cites — only
  live ``TAPROOT:claim`` findings count, and the model that minted the
  hub (``meta.filed_by``) is told apart from models that later edited
  its content (an ``edited`` revision with a changed sha; a
  ``trust-recorded`` or no-change revision is a review, not drafting) —
  or a "not recorded" line for the hubs whose model is unstamped;
* precis itself, cited whole through the renderers' own citation
  machinery (the install's software record, found by its Zenodo DOI —
  :func:`software_record_cite_key`), and the models it runs in the
  background: the models called for passage summarisation (including
  escalation) and the configured embedder (the ``.model`` property ``precis-status``
  reads — a ``remote`` backend's served model, not the backend label);
* a marked line asking the human authors to state what they checked —
  that cannot be generated — printed red like the retraction-coverage
  line;
* when no writing model is on record anywhere, one line saying so
  instead of the section silently going thin.

One line per model (gr725421 review): a model that wrote text, ran a
logged agent run and minted a cited hub gets one line whose clauses
name each role, not three lines.

Both exporters share :func:`ai_contribution_entries`; only the markup
differs (``\\textcolor{red}`` in LaTeX, a red run in docx). An entry's
``cite`` names a bibliography key the renderer emits through its own
citation machinery (``\\cite`` / a numbered ``[n]`` / an in-text patent
citation) — the key is registered by the caller while its reference
list is still open, never inside the generated text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: The one heading both the authored-section detector and the generated
#: block use (case-insensitive on the author's side).
SECTION_TITLE = "AI contributions"

#: The Zenodo DOI of precis-mcp's own software record (Reto 2026-11-18;
#: the DOI is stable, 2026-10-24). The generated line cites the record
#: whole through the renderers' citation machinery — this DOI is only
#: the lookup key, and the fallback text when this corpus holds no such
#: record (a dev/test install).
PRECIS_SOFTWARE_DOI = "10.5281/zenodo.22863181"

#: The precis line when its software record resolves: the citation mark
#: goes after the sentence (the bib entry carries authors/DOI).
_PRECIS_LINE = (
    "precis-mcp: the manuscript-preparation system that held this "
    "draft's text, notes, sources and claim records."
)

#: The same line when the install's record is not in this corpus — the
#: DOI in prose is then the only way to name it.
_PRECIS_LINE_UNCITED = (
    "precis-mcp (Stamm, R.; Zenodo, DOI " + PRECIS_SOFTWARE_DOI + "): the "
    "manuscript-preparation system that held this draft's text, notes, "
    "sources and claim records."
)

#: The marked line every generated section ends with: what a human
#: checked cannot be generated, so the block asks the authors to state
#: it, in the manner of the red retraction-coverage line.
HUMAN_CHECK_LINE = (
    "The human authors state here what they checked and take "
    "responsibility for: which AI-assisted text was verified, and "
    "against what (the source passages, the lab notebook, the test "
    "suites)."
)

#: ``chunk_events.event_kind`` → the contribution verb a model's line
#: prints, in display order.
_EVENT_VERBS: dict[str, str] = {
    "created": "drafted",
    "copied": "copied",
    "edited": "edited",
    "moved": "reordered",
    "reparented": "reordered",
    "retired": "removed",
    "restored": "restored",
}

#: Configured embedder name → the model the generated line prints.
_EMBEDDER_LABELS: dict[str, str] = {
    "bge-m3": "BAAI/bge-m3",
    "remote": "the configured remote embedding model",
    "mock": "a mock embedder (development install)",
}


@dataclass(frozen=True, slots=True)
class AiContributionLine:
    """One line of the generated section; ``marked`` prints it as needing
    the authors' attention (red in both exporters). ``cite`` is a
    bibliography key the renderer emits through its own citation
    machinery right after the line (a ``\\cite`` in LaTeX, a numbered
    mark or in-text patent citation in docx)."""

    text: str
    marked: bool = False
    cite: str | None = None


@dataclass(frozen=True, slots=True)
class ModelWrites:
    """A rolled-up writing model: total writes, first/last dates, the
    distinct kinds of work (event kinds with fork creation counted as copying)."""

    model: str
    writes: int
    first: Any
    last: Any
    kinds: frozenset[str]


def _span(first: Any, last: Any) -> str:
    """``on 2026-11-18`` or ``2026-11-18 to 2026-11-20`` for the rollup."""
    first, last = str(first), str(last)
    return f"on {first}" if first == last else f"{first} to {last}"


def _work_phrase(kinds: frozenset[str]) -> str:
    verbs = [v for k, v in _EVENT_VERBS.items() if k in kinds]
    return " and ".join(dict.fromkeys(verbs)) or "worked on"


def _hub_role(minters: dict[str, int], editors: dict[str, int], model: str) -> str:
    """The hub-work clause of a model's line: mints (``meta.filed_by``)
    named apart from later content edits (gr725421 review)."""
    minted, edited = minters.get(model, 0), editors.get(model, 0)
    if minted and edited:
        return (
            f"minted {minted} and edited {edited} claim record"
            f"{'s' if minted + edited != 1 else ''} this manuscript cites"
        )
    if minted:
        return (
            f"minted {minted} claim record{'s' if minted != 1 else ''} "
            "this manuscript cites"
        )
    return (
        f"edited {edited} claim record{'s' if edited != 1 else ''} "
        "this manuscript cites"
    )


def compose_lines(
    chunk_models: list[ModelWrites],
    unrecorded_writes: int,
    hub_minters: dict[str, int],
    hub_editors: dict[str, int],
    unmodelled_hubs: int,
    summarisers: list[tuple[str, int]],
    embedder: str | None,
    *,
    run_models: list[ModelWrites] | None = None,
    unrecorded_runs: int = 0,
    precis_cite: str | None = None,
    summariser_spans: dict[str, tuple[Any, Any]] | None = None,
) -> list[AiContributionLine]:
    """The section's lines from the gathered facts (pure; the DB reads
    live in :func:`ai_contribution_entries`). ``precis_cite`` is the
    install's software record's cite key — the precis line carries it
    for the renderer to cite whole.

    One line per model: each model's roles merge into one line's
    clauses — manuscript writes, logged runs that touched the draft,
    claim-record mints and edits, and background roles. For a run-only
    model the clause reads *used in* the run: ``models_used`` covers work anywhere in the run and
    ``touched`` also covers moves, so "edited the manuscript" claims
    more than the record shows (gr725421 review)."""
    lines: list[AiContributionLine] = []
    chunk_by = {m.model: m for m in chunk_models}
    runs_by = {m.model: m for m in run_models or []}
    manuscript_models = dict.fromkeys(
        [*chunk_by, *runs_by, *sorted(hub_minters.keys() | hub_editors.keys())]
    )
    background: dict[str, list[str]] = {}
    for model, calls in summarisers:
        span = (summariser_spans or {}).get(model)
        when = f", {_span(*span)}" if span else ""
        background[model] = [
            "recorded in the passage-summarisation call log "
            f"({calls} call{'s' if calls != 1 else ''} in the last 30 days{when})"
        ]
    if embedder:
        background.setdefault(embedder, []).append(
            "computed the vectors behind semantic search"
        )
    for model in manuscript_models:
        roles: list[str] = []
        chunk = chunk_by.get(model)
        if chunk is not None:
            roles.append(
                f"{_work_phrase(chunk.kinds)} the manuscript text "
                f"({chunk.writes} write{'s' if chunk.writes != 1 else ''}, "
                f"{_span(chunk.first, chunk.last)})"
            )
        run = runs_by.get(model)
        if run is not None:
            roles.append(
                f"used in {run.writes} logged agent run"
                f"{'s' if run.writes != 1 else ''} that touched this "
                f"draft ({_span(run.first, run.last)})"
            )
        if model in hub_minters or model in hub_editors:
            roles.append(_hub_role(hub_minters, hub_editors, model))
        roles.extend(background.pop(model, []))
        lines.append(AiContributionLine(f"{model}: {'; '.join(roles)}."))
    if unrecorded_writes:
        lines.append(
            AiContributionLine(
                f"{unrecorded_writes} further chunk write"
                f"{'s' if unrecorded_writes != 1 else ''} carry no "
                "writing-model record."
            )
        )
    if unrecorded_runs:
        lines.append(
            AiContributionLine(
                f"{unrecorded_runs} logged agent run"
                f"{'s' if unrecorded_runs != 1 else ''} touching this "
                "draft carry no model record."
            )
        )
    if unmodelled_hubs:
        lines.append(
            AiContributionLine(
                f"The model behind {unmodelled_hubs} cited claim record"
                f"{'s' if unmodelled_hubs != 1 else ''} is not recorded."
            )
        )
    lines.append(
        AiContributionLine(
            _PRECIS_LINE if precis_cite else _PRECIS_LINE_UNCITED,
            cite=precis_cite,
        )
    )
    if not summarisers:
        lines.append(
            AiContributionLine(
                "No passage-summarisation model is recorded in the call log "
                "for the last 30 days."
            )
        )
    for model, roles in background.items():
        lines.append(AiContributionLine(f"{model}: {'; '.join(roles)}."))
    if summarisers:
        lines.append(
            AiContributionLine(
                "The call log records attempts. Individual passage summaries "
                "do not record which model wrote them."
            )
        )
    if not manuscript_models:
        lines.append(
            AiContributionLine(
                "No writing model is on record for this draft; if AI "
                "tools contributed to the text, the authors name each "
                "and what it did."
            )
        )
    lines.append(AiContributionLine(HUMAN_CHECK_LINE, marked=True))
    return lines


def _chunk_writers(store: Any, ref_id: int) -> tuple[list[ModelWrites], int]:
    """Distinct writer models over the draft's ``chunk_events``, plus the
    count of events still carrying no model. Metadata-only events
    (``set-style``, derived figure refreshes, ``patch-meta``: an
    ``edited`` event with no ``content_sha``) change no text and are not
    counted. Forks create new chunk identities with existing text, so
    their creation events credit copying. Follow each copy to the source's
    text history at the time of the fork, including earlier generations.
    Ancestor moves and retirements describe the source draft, so only
    its content events are inherited."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE lineage(chunk_id, cutoff) AS (
                SELECT chunk_id, NULL::bigint FROM chunks WHERE ref_id = %s
                UNION
                SELECT (e.source ->> 'src_chunk_id')::bigint,
                       coalesce((e.source ->> 'src_event_id')::bigint, e.event_id - 1)
                  FROM lineage l
                  JOIN chunk_events e ON e.chunk_id = l.chunk_id
                 WHERE e.event_kind = 'created' AND e.source ->> 'reason' = 'fork'
                   AND e.source ->> 'src_chunk_id' IS NOT NULL
                   AND (l.cutoff IS NULL OR e.event_id <= l.cutoff)
            ),
            draft_events AS (
                SELECT DISTINCT e.event_id, e.source, e.ts, e.event_kind
                  FROM lineage l
                  JOIN chunk_events e ON e.chunk_id = l.chunk_id
                 WHERE (l.cutoff IS NULL OR (
                            e.event_id <= l.cutoff AND e.event_kind IN ('created', 'edited')
                       ))
                   AND NOT (e.event_kind = 'edited' AND e.content_sha IS NULL)
            )
            SELECT coalesce(e.source #>> '{writer,model}',
                            e.source ->> 'model') AS model,
                   count(*) AS writes,
                   min(e.ts)::date AS first,
                   max(e.ts)::date AS last,
                   array_agg(DISTINCT CASE
                       WHEN e.event_kind = 'created' AND e.source ->> 'reason' = 'fork'
                       THEN 'copied'
                       ELSE e.event_kind
                   END) AS kinds
              FROM draft_events e
             GROUP BY 1
             ORDER BY min(e.ts)
            """,
            (ref_id,),
        ).fetchall()
    models: list[ModelWrites] = []
    unrecorded = 0
    for model, writes, first, last, kinds in rows:
        if model:
            models.append(
                ModelWrites(str(model), int(writes), first, last, frozenset(kinds))
            )
        else:
            unrecorded += int(writes)
    return models, unrecorded


def _agentlog_runs(store: Any, ref_id: int) -> tuple[list[ModelWrites], int]:
    """Distinct models over the ``agentlog`` runs linked to the draft —
    an MCP-session edit leaves no run, but a ``claude -p`` / Codex
    worker's run ``touched``-links every chunk it wrote, so those runs
    are contribution evidence too (gr725421). ``ModelWrites.writes``
    carries the run count here; ``kinds`` stays empty (a run's work is
    not sliced). Returns the runs with no model on record alongside."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT m.model,
                   count(DISTINCT r.ref_id) AS runs,
                   min(r.created_at)::date AS first,
                   max(r.created_at)::date AS last
              FROM links l
              JOIN refs r ON r.ref_id = l.src_ref_id
                   AND r.retired_at IS NULL
              LEFT JOIN LATERAL (
                   SELECT x AS model FROM jsonb_array_elements_text(
                       CASE WHEN jsonb_typeof(r.meta -> 'models_used') = 'array'
                            THEN r.meta -> 'models_used'
                            ELSE '[]'::jsonb END) AS x
                   UNION
                   SELECT r.meta ->> 'model_actual'
                    WHERE jsonb_typeof(r.meta -> 'models_used') IS DISTINCT FROM 'array'
                      OR jsonb_array_length(r.meta -> 'models_used') = 0
                   UNION
                   SELECT r.meta ->> 'model'
                    WHERE (jsonb_typeof(r.meta -> 'models_used') IS DISTINCT FROM 'array'
                           OR jsonb_array_length(r.meta -> 'models_used') = 0)
                      AND r.meta ->> 'model_actual' IS NULL
              ) m ON true
             WHERE l.relation = 'touched' AND l.dst_ref_id = %s
               AND r.kind = 'agentlog'
             GROUP BY 1
             ORDER BY min(r.created_at)
            """,
            (ref_id,),
        ).fetchall()
    models: list[ModelWrites] = []
    unrecorded = 0
    for model, runs, first, last in rows:
        if model:
            models.append(ModelWrites(str(model), int(runs), first, last, frozenset()))
        else:
            unrecorded += int(runs)
    return models, unrecorded


def _hub_writers(store: Any, ref_id: int) -> tuple[dict[str, int], dict[str, int], int]:
    """``(minters, editors, unmodelled)`` over the claim hubs the draft
    cites — ``{model: hub count}`` per role plus the count of cited
    hubs with no model on record at all.

    Only live ``TAPROOT:claim`` findings roll up: an ordinary cited
    finding is prose evidence, not a machine-minted claim record
    (gr725421 review). The model that *minted* a hub
    (``meta.filed_by.model``, stamped on the create) counts apart from
    models that *later edited* its content: ``event='edited'`` with a
    changed sha — a ``trust-recorded`` (or no-change) revision is a
    review, not drafting (the seal trigger deletes those, so the filter
    also guards rows a trigger never sealed).
    """
    from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE

    with store.pool.connection() as conn:
        hub_ids = [
            int(r[0])
            for r in conn.execute(
                """
                SELECT DISTINCT l.dst_ref_id
                  FROM links l
                  JOIN refs r ON r.ref_id = l.dst_ref_id
                       AND r.retired_at IS NULL
                 WHERE l.src_ref_id = %s AND l.relation = 'cites'
                   AND r.kind = 'finding'
                   AND EXISTS (
                       SELECT 1
                         FROM ref_tags rt
                         JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND t.namespace = %s AND t.value = %s)
                """,
                (ref_id, TAPROOT_NAMESPACE, TAPROOT_CLAIM),
            ).fetchall()
        ]
        if not hub_ids:
            return {}, {}, 0
        rev_rows = conn.execute(
            """
            SELECT target_id, writer ->> 'model'
              FROM revisions
             WHERE target_kind = 'ref' AND target_id = ANY(%s)
               AND event = 'edited'
               AND prev_sha IS DISTINCT FROM new_sha
               AND writer ? 'model'
            """,
            (hub_ids,),
        ).fetchall()
        filed_rows = conn.execute(
            """
            SELECT ref_id, meta #>> '{filed_by,model}'
              FROM refs WHERE ref_id = ANY(%s)
            """,
            (hub_ids,),
        ).fetchall()
    edited_of: dict[int, set[str]] = {}
    minted_of: dict[int, set[str]] = {}
    for fid, model in rev_rows:
        if model:
            edited_of.setdefault(int(fid), set()).add(str(model))
    for fid, model in filed_rows:
        if model:
            minted_of.setdefault(int(fid), set()).add(str(model))
    minters: dict[str, int] = {}
    editors: dict[str, int] = {}
    unmodelled = 0
    for fid in hub_ids:
        mint, edit = minted_of.get(fid, set()), edited_of.get(fid, set())
        if not mint and not edit:
            unmodelled += 1
            continue
        for model in mint:
            minters[model] = minters.get(model, 0) + 1
        for model in edit:
            editors[model] = editors.get(model, 0) + 1
    return minters, editors, unmodelled


def _summariser_models(
    store: Any,
) -> tuple[list[tuple[str, int]], dict[str, tuple[Any, Any]]]:
    """Usage across both passage-summarisation sources, including escalation."""
    from precis import llm_catalog

    rows = llm_catalog.llm_op_model_usage(
        store, ["llm_summarize", "llm_summarize:escalate"]
    )
    return (
        [(model, calls) for model, calls, _, _ in rows],
        {model: (first, last) for model, _, first, last in rows},
    )


def _embedder_name() -> str | None:
    """The configured embedder's display name — the ``.model`` property
    the ``precis-status`` Embedder block reads. A ``remote`` backend
    answers its served model over ``/model`` (one guarded round trip on
    a short budget; unreachable → a generic label, never a failed
    export). The other backends name themselves from config alone."""
    from precis.config import load_config

    cfg = load_config()
    name = str(cfg.embedder or "")
    if name == "remote":
        try:
            from precis.embedder import RemoteEmbedder

            embedder = RemoteEmbedder(
                str(cfg.embedder_url or ""),
                expected_dim=None,
                timeout=min(float(cfg.embedder_timeout or 300.0), 15.0),
                max_retries=0,
            )
            return str(embedder.model)
        except Exception:
            log.debug(
                "ai-contributions: remote embedder /model probe failed",
                exc_info=True,
            )
            return "the configured remote embedding model"
    return _EMBEDDER_LABELS.get(name, name or None)


def software_record_cite_key(store: Any) -> str | None:
    """The cite key of this install's own precis-mcp software record —
    :data:`PRECIS_SOFTWARE_DOI` looked up in ``ref_identifiers``, then
    its ``pa<id>`` handle resolved to the canonical cite key the
    renderers' machinery prints. ``None`` when this corpus holds no
    such record or it does not resolve."""
    if store is None:
        return None
    from precis.export import walker

    try:
        owner = store.identifier_owner("doi", PRECIS_SOFTWARE_DOI)
        if owner is None:
            return None
        hit = walker.resolve_cite_handle(store, f"pa{int(owner)}")
        return str(hit[0]) if hit else None
    except Exception:
        log.debug("ai-contributions: software record lookup failed", exc_info=True)
        return None


def ai_contribution_entries(store: Any, ref_id: int) -> list[AiContributionLine]:
    """The generated section's lines for draft ``ref_id``.

    Every lookup is best-effort: a failed read degrades to a "not
    recorded" line, never to a failed export.
    """
    chunk_models: list[ModelWrites] = []
    unrecorded = 0
    run_models: list[ModelWrites] = []
    unrecorded_runs = 0
    hub_minters: dict[str, int] = {}
    hub_editors: dict[str, int] = {}
    unmodelled_hubs = 0
    summarisers: list[tuple[str, int]] = []
    summariser_spans: dict[str, tuple[Any, Any]] = {}
    embedder = None
    precis_cite = None
    try:
        chunk_models, unrecorded = _chunk_writers(store, ref_id)
    except Exception:
        log.debug("ai-contributions: chunk writer rollup failed", exc_info=True)
    try:
        run_models, unrecorded_runs = _agentlog_runs(store, ref_id)
    except Exception:
        log.debug("ai-contributions: agentlog rollup failed", exc_info=True)
    try:
        hub_minters, hub_editors, unmodelled_hubs = _hub_writers(store, ref_id)
    except Exception:
        log.debug("ai-contributions: hub writer rollup failed", exc_info=True)
    try:
        precis_cite = software_record_cite_key(store)
    except Exception:
        log.debug("ai-contributions: software record lookup failed", exc_info=True)
    try:
        summarisers, summariser_spans = _summariser_models(store)
    except Exception:
        log.debug("ai-contributions: summariser rollup failed", exc_info=True)
    try:
        embedder = _embedder_name()
    except Exception:
        log.debug("ai-contributions: embedder lookup failed", exc_info=True)
    return compose_lines(
        chunk_models,
        unrecorded,
        hub_minters,
        hub_editors,
        unmodelled_hubs,
        summarisers,
        embedder,
        run_models=run_models,
        unrecorded_runs=unrecorded_runs,
        precis_cite=precis_cite,
        summariser_spans=summariser_spans,
    )
