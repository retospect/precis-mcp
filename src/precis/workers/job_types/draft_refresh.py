"""``draft_refresh`` job_type — one bounded, section-scoped refresh of a
living draft (docs/backlog/draft-refresh.md, Part 1: the job + skill; the
scheduler cadence that mints these jobs is a separate, later round).

Given ``{'draft': '<slug>', 'scope': 'dc<id>'}`` (``scope`` a heading
anchor), the dispatcher runs six steps for that ONE section:

1. **Workspace** — the section's paragraph prose (what gets rewritten) plus
   its preserved content — table/figure/term chunks and nested subsections,
   read-only context — via the draft handler's own scope machinery
   (:meth:`~precis.handlers.draft.DraftHandler._scope_chunks`), plus the
   whole-draft TOC for context.
2. **Evidence** — corpus-side missing-citation candidates for this section
   (:mod:`precis.backfill`), plus a **research arm**: when the draft
   ``serves`` a quest, that quest's Pareto-frontier digest, and the
   frontier digests of quests that in turn ``serves`` *it* (one bounded
   transitive walk — the sub-quest "arms"). All best-effort: any leg that
   fails or comes back empty degrades to omission, never a job failure.
3. **New-paper seeking** — deliberately NOT implemented in v1: no external
   search runs here. The backfill candidates from step 2 already are the
   "papers you should cite" set; the external S2 leg is a later slice.
4. **Critique + rewrite** — one ``Tier.BIG`` LLM dispatch: critique the
   section, then emit a rewrite under a parseable sentinel.
5. **Growth gate + apply** — :func:`precis.quest.narrative_budget.narrative_growth_gate`
   decides accept-or-refuse; on accept, ONLY the section's ``paragraph``-kind
   body chunks are INSERT-then-DELETEd (never in-place UPDATE, so the
   embedding/summary/autolink cascade re-runs) — this is a PROSE refresh, so
   a ``table``/``figure``/``term`` chunk, and any nested sub-``heading``
   (with its whole subtree), is left untouched and can never be destroyed by
   a rewrite. INSERT runs before DELETE: each store call is its own
   committed transaction, so a failure between them must never leave the
   section with zero live paragraphs (silently dropping it off the scan's
   staleness clock forever) — inserting first means a mid-retire failure
   instead leaves duplicated (old + new) prose, which self-heals on a later
   refresh. New paragraphs land right after the section heading; a
   preserved chunk that used to sit before the old paragraphs may end up
   after the new prose — acceptable in v1. The heading chunk itself stays in
   place (its text only edited if the rewrite changed it).
6. **Process memory** — on the owning quest (when there is one): a logbook
   entry (:func:`precis.quest.logbook.append_entry`) and an attempt-tree
   node (:mod:`precis.quest.dossier`). A questless draft skips this step
   entirely — the book itself stays pure content either way.

Runs via plugin ``dispatch`` under ``claude_inproc`` — no claude
subprocess, same shape as ``taproot_backfill``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "draft": {"type": "string"},  # the owning draft's slug
        "scope": {"type": "string"},  # a dc<id> heading anchor
    },
    "required": ["draft", "scope"],
    "additionalProperties": False,
}

#: The parseable boundary between the model's critique and its rewrite.
_SENTINEL = "=== REWRITE ==="

#: Cheap shape check for a ``dc<id>`` chunk address — the scope must be a
#: chunk anchor (a heading), never a bare draft slug (which ``_scope_chunks``
#: would otherwise happily resolve to the WHOLE draft).
_DC_ADDR_RE = re.compile(r"^dc\d+$")

#: Prefixes worth counting as a "citation handle" for the growth gate's
#: progress-evidence check — paper-chunk, whole-paper, and finding/claim-hub
#: cites (the draft grammar's citable kinds).
_CITE_PREFIXES = ("pc", "pa", "fi")

#: Bounded hop cap for the research-arm's transitive ``serves`` walk (mirrors
#: the bounded ladder in ``precis.quest.reweight``).
_MAX_ARM_HOPS = 5

_PROMPT = """\
You are refreshing ONE section of a living technical draft. First critique
it, then rewrite it.

DRAFT OUTLINE (context — the section below is somewhere in this tree):
{toc_text}

SECTION HEADING: {heading_text}

CURRENT SECTION PROSE (this is what you are critiquing and rewriting):
{body_text}

PRESERVED CONTENT (tables, figures, glossary terms, and subsections in this
section — these are kept automatically and are NOT part of your rewrite; do
NOT reproduce, restate, or re-describe them in your output, read-only
context):
{preserved_text}

EVIDENCE — corpus sources relevant to this section it does not yet cite:
{evidence_text}

RESEARCH ARM — the owning quest's compute results (Pareto frontier), if any:
{research_text}

Task:
1. Critique the section's PROSE (not the preserved content): what is stale,
   unsupported, or could be tightened.
2. Then rewrite the PROSE only. House style: plain prose, no bold/italics
   markup, no em-dashes. Keep citations as bracketed handles ([pc<id>] a
   paper chunk, [pa<id>] a whole paper, [fi<id>] a finding/claim hub) —
   weave in new ones from the evidence above where they genuinely support a
   claim. Do not grow the section more than modestly — tighten more than
   you add. Do not output a table, figure, or anything from PRESERVED
   CONTENT — those stay exactly as they are, untouched by this job.

Output the critique first as plain prose. Then, on its own line, the
sentinel "{sentinel}". Then the rewritten prose: the heading text on the
first line, followed by the body as one or more blank-line-separated
paragraphs. Nothing after the last paragraph.
"""


def _citation_handles(text: str) -> set[str]:
    """Every ``[pc<id>]``/``[pa<id>]``/``[fi<id>]`` handle in ``text`` —
    used only to detect whether a rewrite *added* a citation (the growth
    gate's ``progress_evidence`` fact), not to resolve/validate them."""
    from precis.utils.mentions import BARE_BRACKET_REF_PATTERN

    out: set[str] = set()
    for m in BARE_BRACKET_REF_PATTERN.finditer(text or ""):
        bare = m.group("bare")
        if bare[:2] in _CITE_PREFIXES:
            out.add(bare)
    return out


def _parse_rewrite(raw: str) -> tuple[str, str] | None:
    """Split the LLM's output at :data:`_SENTINEL` into ``(heading, body)``.
    ``None`` when the sentinel is missing or the section after it is empty —
    the caller turns that into a clean job failure rather than applying
    garbage."""
    idx = raw.find(_SENTINEL)
    if idx == -1:
        return None
    rest = raw[idx + len(_SENTINEL) :].strip("\n")
    if not rest.strip():
        return None
    head, _sep, tail = rest.partition("\n")
    heading = head.strip()
    body = tail.strip("\n").strip()
    if not heading or not body:
        return None
    return heading, body


def _split_body_chunks(body_chunks: list[Any]) -> tuple[list[Any], list[Any]]:
    """Partition a section's flat (DFS reading-order) subtree — excluding
    the section's own heading — into ``(retire_targets, preserved)``.

    This is a PROSE refresh: ``retire_targets`` is every ``paragraph``-kind
    chunk directly under the section (not nested inside a sub-heading) —
    the ONLY chunks this job may ever retire. ``preserved`` is every other
    chunk in the subtree: ``table``/``figure``/``term``/any other
    non-paragraph kind at that same level, PLUS every sub-``heading`` and
    its whole subtree (including any paragraphs nested inside IT — those
    are never retire targets either). A rewrite can therefore never destroy
    a table/figure/term or a nested subsection, only replace the section's
    own direct paragraphs."""
    retire_targets: list[Any] = []
    preserved: list[Any] = []
    skip_until_depth: int | None = None
    for c in body_chunks:
        if skip_until_depth is not None:
            if c.depth > skip_until_depth:
                preserved.append(c)  # inside a sub-heading's subtree
                continue
            skip_until_depth = None
        if c.chunk_kind == "heading":
            skip_until_depth = c.depth  # everything under it is preserved too
            preserved.append(c)
            continue
        if c.chunk_kind == "paragraph":
            retire_targets.append(c)
        else:
            preserved.append(c)
    return retire_targets, preserved


def _render_preserved(preserved: list[Any]) -> str:
    """A read-only digest of the section's preserved chunks for the
    rewrite prompt — table markdown, figure captions, term definitions,
    and any sub-heading/subsection text. ``"(none)"`` when there's nothing
    preserved."""
    if not preserved:
        return "(none)"
    lines = []
    for c in preserved:
        text = (c.text or "").strip()
        if text:
            lines.append(f"[{c.chunk_kind}] {text}")
    return "\n\n".join(lines) or "(none)"


def _owning_quest_id(store: Any, draft_ref_id: int) -> int | None:
    """The quest this draft ``serves``, or ``None`` — a quest-less draft is
    legal (``review_fanout.py`` notes the same). Best-effort: any lookup
    failure degrades to ``None``."""
    try:
        links = store.links_for(draft_ref_id, direction="out", relation="serves")
        dst_ids = [int(link.dst_ref_id) for link in links]
        if not dst_ids:
            return None
        refs = store.fetch_refs_by_ids(dst_ids)
        quest_ids = [
            int(link.dst_ref_id)
            for link in links
            if (ref := refs.get(int(link.dst_ref_id))) is not None
            and getattr(ref, "kind", None) == "quest"
            and getattr(ref, "deleted_at", None) is None
        ]
        # Deterministic when a draft serves >1 quest: the lowest ref_id wins
        # (arbitrary but stable — was link-scan order, i.e. undefined).
        return min(quest_ids) if quest_ids else None
    except Exception:
        log.warning(
            "draft_refresh: owning-quest lookup failed for draft ref %s",
            draft_ref_id,
            exc_info=True,
        )
    return None


def _serving_quest_ids(store: Any, quest_id: int) -> list[int]:
    """Quests that ``serves`` ``quest_id``, transitively, up to
    :data:`_MAX_ARM_HOPS` hops — the "arms" of an umbrella quest, so a
    survey book hanging on the umbrella sees every arm's results. Bounded
    max-hop BFS, mirroring the bounded ladder in
    :func:`precis.quest.reweight.active_quest_weights`. Best-effort: a
    lookup failure degrades to an empty list."""
    seen = {quest_id}
    frontier = {quest_id}
    try:
        for _ in range(_MAX_ARM_HOPS):
            with store.pool.connection() as conn:
                rows = conn.execute(
                    "SELECT l.src_ref_id FROM links l "
                    "JOIN refs r ON r.ref_id = l.src_ref_id "
                    "WHERE l.relation = 'serves' AND l.dst_ref_id = ANY(%s) "
                    "AND r.kind = 'quest' AND r.deleted_at IS NULL",
                    (list(frontier),),
                ).fetchall()
            new = {int(r[0]) for r in rows} - seen
            if not new:
                break
            seen |= new
            frontier = new
    except Exception:
        log.warning(
            "draft_refresh: serving-quest walk failed for quest %s",
            quest_id,
            exc_info=True,
        )
        return []
    seen.discard(quest_id)
    return sorted(seen)


def _frontier_digest(store: Any, quest_id: int) -> str:
    """A compact Pareto-frontier rendering for ``quest_id`` — the same
    underlying data ``view='frontier'`` renders
    (:func:`precis.quest.frontier.quest_frontier`), condensed for the
    rewrite prompt. ``""`` on any failure or an empty frontier (best-effort
    — never raises)."""
    try:
        from precis.quest.frontier import quest_frontier

        fr = quest_frontier(store, quest_id)
    except Exception:
        log.warning(
            "draft_refresh: frontier digest failed for quest %s",
            quest_id,
            exc_info=True,
        )
        return ""
    if not (fr.frontier or fr.dominated):
        return ""
    lines = [f"objective: {' · '.join(f'{k} ({s})' for k, s in fr.objectives)}"]
    for c in fr.frontier[:8]:
        ms = " ".join(f"{k}={v:g}" for k, v in sorted(c.measures.items()))
        lines.append(f"  FRONTIER {c.handle} {c.name} — {ms or '(no measures)'}")
    for c in fr.dominated[:4]:
        ms = " ".join(f"{k}={v:g}" for k, v in sorted(c.measures.items()))
        lines.append(f"  beaten   {c.handle} {c.name} — {ms or '(no measures)'}")
    return "\n".join(lines)


def _research_arm_digest(store: Any, quest_id: int) -> str:
    """The owning quest's frontier digest plus every quest that ``serves``
    it (the research arms), each labeled by its handle. ``""`` when the
    quest has no compute lane at all — folds into the prompt as an omitted
    section, never a placeholder."""
    parts: list[str] = []
    own = _frontier_digest(store, quest_id)
    if own:
        parts.append(f"— quest {quest_id} —\n{own}")
    for arm_id in _serving_quest_ids(store, quest_id):
        arm = _frontier_digest(store, arm_id)
        if arm:
            parts.append(f"— quest {arm_id} (serves quest {quest_id}) —\n{arm}")
    return "\n\n".join(parts)


def _evidence_digest(store: Any, scope: str) -> str:
    """Corpus-side missing-citation candidates for ``scope`` — the
    uncited-but-relevant hits :mod:`precis.backfill` surfaces. Uses the
    lightweight remote/none tick-time embedder
    (:func:`precis.backfill.workspace.recall_embedder`), never the heavy
    local model — this runs off the worker's own hub, not the server's.
    ``""`` on any failure or an empty candidate list (best-effort)."""
    try:
        from precis.backfill.workspace import assemble, recall_embedder

        embedder = recall_embedder(store)
        _ws, candidates, _cited = assemble(
            store, embedder, [scope], kind="draft", max_candidates=8
        )
    except Exception:
        log.warning(
            "draft_refresh: evidence gathering failed for scope %s",
            scope,
            exc_info=True,
        )
        return ""
    if not candidates:
        return ""
    lines = ["candidate sources (uncited, corpus-recalled):"]
    for cand in candidates:
        addr = f"{cand.paper_handle} {cand.chunk_handle}".strip()
        title = cand.title[:90] or "(untitled)"
        lines.append(f"  {addr} — {title}")
    return "\n".join(lines)


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job.
    ``ctx`` is a :class:`~precis.workers.executors._context.DispatchContext`."""
    from precis.dispatch import Hub
    from precis.errors import BadInput, NotFound
    from precis.handlers.draft import DraftHandler
    from precis.quest import dossier as dossier_mod
    from precis.quest import logbook
    from precis.quest.narrative_budget import narrative_growth_gate
    from precis.utils.llm.router import LlmRequest, Tier
    from precis.utils.llm.router import dispatch as llm_dispatch

    params = (ctx.meta or {}).get("params") or {}
    draft_slug = str(params.get("draft") or "").strip()
    scope = str(params.get("scope") or "").strip()
    if not draft_slug:
        ctx.record_failure("draft_refresh: params.draft is required")
        return
    if not scope:
        ctx.record_failure("draft_refresh: params.scope is required")
        return
    if not _DC_ADDR_RE.match(scope):
        ctx.record_failure(
            f"draft_refresh: params.scope must be a dc<id> heading anchor, "
            f"got {scope!r}"
        )
        return

    draft_handler = DraftHandler(hub=Hub(store=ctx.store))
    try:
        pairs, _where = draft_handler._scope_chunks(scope, allow_all=False)
    except (NotFound, BadInput) as exc:
        ctx.record_failure(f"draft_refresh: {exc}")
        return
    if not pairs:
        ctx.record_failure(f"draft_refresh: scope {scope!r} resolved to no chunks")
        return

    slug, heading = pairs[0]
    if slug != draft_slug:
        ctx.record_failure(
            f"draft_refresh: scope {scope!r} belongs to draft {slug!r}, "
            f"not params.draft={draft_slug!r}"
        )
        return
    if heading.chunk_kind != "heading":
        ctx.record_failure(
            f"draft_refresh: scope {scope!r} is not a heading anchor "
            f"(chunk_kind={heading.chunk_kind!r})"
        )
        return

    ref_id = heading.ref_id
    body_chunks = [c for _s, c in pairs[1:]]
    retire_targets, preserved_chunks = _split_body_chunks(body_chunks)

    # 1. Workspace — the section's paragraph prose + preserved content
    # (read-only context) + the whole-draft TOC.
    old_body_text = "\n\n".join(
        (c.text or "").strip() for c in retire_targets if (c.text or "").strip()
    )
    preserved_text = _render_preserved(preserved_chunks)
    try:
        toc = ctx.store.drafts.draft_toc(ref_id)
        toc_text = (
            "\n".join(f"{'  ' * e.depth}- {e.title}" for e in toc) or "(no headings)"
        )
    except Exception:
        log.warning(
            "draft_refresh: toc render failed for ref %s", ref_id, exc_info=True
        )
        toc_text = "(unavailable)"
    ctx.append_chunk(
        "job_event",
        f"draft_refresh: workspace assembled for {scope} in {draft_slug!r} "
        f"({len(retire_targets)} paragraph(s), {len(preserved_chunks)} "
        "preserved chunk(s))",
    )

    quest_id = _owning_quest_id(ctx.store, ref_id)

    # 2. Evidence — corpus backfill candidates + the research arm.
    evidence_text = _evidence_digest(ctx.store, scope) or "(none found)"
    research_text = ""
    if quest_id is not None:
        research_text = _research_arm_digest(ctx.store, quest_id)
    research_text = research_text or "(no compute lane / not a quest-owned draft)"
    ctx.append_chunk("job_event", "draft_refresh: evidence gathered")

    # 3. New-paper seeking — deliberately NOT implemented in v1 (see module
    # docstring); the backfill candidates above are the "papers you should
    # cite" set for this slice.

    attempt_label = f"draft_refresh {draft_slug} {scope}"
    if quest_id is not None:
        dossier_mod.add_attempt(ctx.store, quest_id, attempt_label)

    # 4. Critique + rewrite — one Tier.BIG dispatch.
    prompt = _PROMPT.format(
        toc_text=toc_text,
        heading_text=heading.text or "",
        body_text=old_body_text or "(empty section)",
        preserved_text=preserved_text,
        evidence_text=evidence_text,
        research_text=research_text,
        sentinel=_SENTINEL,
    )
    res = llm_dispatch(LlmRequest(tier=Tier.BIG, prompt=prompt, source="draft_refresh"))
    if res.error:
        if quest_id is not None:
            dossier_mod.mark_attempt(ctx.store, quest_id, attempt_label, "ruled-out")
        ctx.record_failure(f"draft_refresh: LLM dispatch failed: {res.error}")
        return

    parsed = _parse_rewrite(res.text or "")
    if parsed is None:
        if quest_id is not None:
            dossier_mod.mark_attempt(ctx.store, quest_id, attempt_label, "ruled-out")
        ctx.record_failure(
            f"draft_refresh: could not parse LLM rewrite (no {_SENTINEL!r} "
            "sentinel, or an empty section after it)"
        )
        return
    new_heading_text, new_body_text = parsed

    # 5. Growth gate + apply.
    prev_words = len(old_body_text.split())
    new_words = len(new_body_text.split())
    progress_evidence = bool(
        _citation_handles(new_body_text) - _citation_handles(old_body_text)
    )
    gate = narrative_growth_gate(prev_words, new_words, progress_evidence)
    if not gate.ok:
        # The section is left untouched on a gate refusal. Its idem_key (the
        # scheduler side, docs/backlog/draft-refresh.md) encodes the
        # section's current min chunk created_at, so THIS exact job never
        # re-fires until the chunks actually change — a refused rewrite
        # does not naturally re-arm on the next cadence tick. Acceptable
        # per the spec (noted there too); a manual edit or an operator
        # re-poke is what breaks the stall.
        if quest_id is not None:
            dossier_mod.mark_attempt(ctx.store, quest_id, attempt_label, "ruled-out")
        ctx.append_chunk(
            "job_event",
            f"draft_refresh: growth gate refused ({gate.reason}) — "
            f"{prev_words}→{new_words} words, "
            f"progress_evidence={progress_evidence}; section left unchanged",
        )
        ctx.set_meta(applied=False, gate_reason=gate.reason)
        ctx.append_chunk(
            "job_summary",
            f"draft_refresh — {draft_slug}:{scope}: refused ({gate.reason})",
        )
        return

    # PROSE refresh only: retire ONLY the paragraph chunks. table/figure/
    # term chunks and any sub-heading subtree (preserved_chunks + whatever
    # sat under a nested heading) are never touched — a rewrite can't
    # destroy them. New paragraphs land right after the heading
    # (at=first), so a preserved chunk that used to precede them may end
    # up after the new prose — acceptable in v1.
    #
    # INVARIANT: insert-before-retire, never the reverse. Each store call
    # below is its own committed transaction (no cross-call atomicity), so
    # if a failure lands between them the section must never be left with
    # ZERO live paragraphs — that would silently orphan it (the scan's
    # `if not retire_targets: continue` never re-nominates a paragraph-less
    # section, so it would drop off the staleness clock forever). Inserting
    # first means a mid-retire failure instead leaves DUPLICATED prose
    # (old + new paragraphs both live) — messy but self-healing, since the
    # section still has live paragraphs and gets picked up again.
    try:
        new_chunks = ctx.store.drafts.add_chunks(
            ref_id=ref_id,
            chunk_kind="paragraph",
            text=new_body_text,
            at={"into": heading.dc, "first": True},
        )
        for c in retire_targets:
            ctx.store.drafts.retire_chunk(c.handle, mode="cascade")
        if new_heading_text and new_heading_text != (heading.text or "").strip():
            ctx.store.drafts.edit_text(
                heading.handle, new_heading_text, source={"reason": "draft_refresh"}
            )
        draft_handler._sync_draft_links(ref_id)
        draft_handler._attribute_touch([c.chunk_id for c in new_chunks])
    except Exception as exc:
        if quest_id is not None:
            dossier_mod.mark_attempt(ctx.store, quest_id, attempt_label, "ruled-out")
        ctx.record_failure(
            f"draft_refresh: apply failed for {scope} in {draft_slug!r} "
            f"(new paragraphs may already be live alongside the old ones — "
            f"self-heals on a later refresh): {exc}"
        )
        return

    ctx.append_chunk(
        "job_event",
        f"draft_refresh: applied — {len(retire_targets)} paragraph chunk(s) "
        f"retired, {len(new_chunks)} new chunk(s) inserted under "
        f"{heading.dc} ({len(preserved_chunks)} preserved chunk(s) untouched)",
    )
    ctx.set_meta(applied=True, words_before=prev_words, words_after=new_words)

    # 6. Process memory — the owning quest only; a questless draft skips
    # this step entirely.
    if quest_id is not None:
        dossier_mod.mark_attempt(ctx.store, quest_id, attempt_label, "tried")
        digest = (
            f"draft_refresh: refreshed {scope} in draft {draft_slug!r} "
            f"({prev_words}→{new_words} words"
            + (", new citations woven in" if progress_evidence else "")
            + ")."
        )
        logbook.append_entry(
            ctx.store, quest_id, text=digest, entry_type="result", by="agent"
        )

    ctx.append_chunk(
        "job_summary",
        f"draft_refresh — {draft_slug}:{scope}: applied "
        f"({prev_words}→{new_words} words)",
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("draft_refresh runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="draft_refresh",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),
    description=(
        "Refresh one section of a living draft (critique + rewrite against "
        "corpus + research-arm evidence, growth-gated apply)."
    ),
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
