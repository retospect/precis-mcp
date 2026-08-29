# Context-quality eval — catalog + rubric

Most server work is spent assembling a **context** for an LLM: a rendered
`get`/`search` response for a cluster agent, or a system+user prompt pair a
worker builds and dispatches itself. There is no standing check that any of
these are *good* — that the right skill is reachable from inside the
context, that a field the next action needs isn't silently empty because no
classifier/pre-worker ever populated it, that the render still matches what
the skill claims. This doc is the catalog + rubric; the runnable capture/score
procedure is `scripts/context-audit/` (built separately — reference it here,
don't duplicate its logic).

## Section 1 — Context catalog

Two axes. **Axis A** — a context rendered synchronously inside a read-verb
call, consumed by whatever agent issued the call. **Axis B** — a context the
server assembles into a prompt and dispatches to an LLM itself (a worker
pass); the "consumer" is that dispatched call, not the caller who triggered
the job.

### Axis A — interactive (read-verb) contexts

#### Cross-kind search render

| context | producer | how to poke | what it ought to do |
|---|---|---|---|
| Merged/ranked hit list (any `search`, single- or cross-kind) | `src/precis/utils/search_merge.py::merge_and_render` (shape helpers: `_render_hit` markdown default, `_render_toon_table`, `_render_keywords_table`) | `search(kind='paper', q='…')`, `search(kind='*', q='…')`, `search(view='keywords', q='…')` | Rank + dedupe hits, render the cheapest shape that answers the query; every hit carries a resolvable handle (`SearchHit.handle`/`uhandle`) so the agent can `get` it next. |
| Search headline (`# N of K … for 'q'`) | `src/precis/utils/search_header.py::format_search_headline` (+`detect_score_cliff`) | any `search()` call — first line of the body | Tell the agent whether it saw everything (`N`) or is paginated (`N of K`), and flag a score cliff (`(N strong)`) so it doesn't waste turns paging low-confidence tail hits. |

#### Special search views (`src/precis/runtime/dispatch.py::DispatchMixin._dispatch_inner`)

| context | producer | how to poke | what it ought to do |
|---|---|---|---|
| `view='dreamable'` — salience seed + ANN ring | `src/precis/runtime/angle.py::AngleMixin._dispatch_dreamable` | `search(kind='memory', view='dreamable')` | Surface the highest-salience unread neighborhood for a dream/reflection pass — see `docs/backlog/dreaming.md`. |
| `view='stubs'` — paper backlog | `src/precis/runtime/search.py::SearchMixin._dispatch_stubs` | `search(kind='paper', view='stubs')` | List papers with an external id but no PDF yet — the acquire backlog (the stub surfaces). |
| `angle=`/`like=` spray | `src/precis/runtime/angle.py::AngleMixin._dispatch_angle` | `search(angle=0.3, like='kind:slug')` | Diverse-cone semantic sampler for exploration, not top-K relevance. |
| `view='keywords'` — keyword-only TOON | `src/precis/utils/search_merge.py::_render_keywords_table` (routed via `_dispatch_cross_kind`) | `search(view='keywords', q='…')` | Cheap "what topics span the corpus" scan — no preview text, just per-hit keyword arrays from `chunks.keywords`. |
| `folder=` scope | `_dispatch_inner` → `_dispatch_cross_kind` (ADR 0045) | `search(folder='…', q='…')` | Restrict the cross-kind fan-out to a folder subtree. |
| Source search (`sort=`/`since=`/`until=`) | `src/precis/runtime/search.py::SearchMixin._dispatch_source_search` (gate: `_is_source_search_request`) | `search(kind='paper,memory', since='2026-07-01')` | One best-chunk-per-ref cross-kind primitive ordered by recency or relevance — the unified-item-view Slice 2 shape. |
| `title=`/`author=` byline lookup | `src/precis/handlers/paper.py::PaperHandler.search` | `search(kind='paper', title='…')` / `search(kind='paper', author='Vaswani')` | Return paper *records* (handle + citation), not block hits — the targeted alternative when you know the title/author and don't want a semantic scan. |

#### Per-kind `get(view=…)`

| context | producer | how to poke | what it ought to do |
|---|---|---|---|
| Numeric-ref default render | `src/precis/handlers/_numeric_ref.py::NumericRefHandler._render_one` | `get(kind='todo', id=N)` (also `gripe`/`memory`/`finding`/`alert`/`quest`) | One-screen summary of the ref: title, body, tags, breadcrumb. |
| `view='links'` | `src/precis/handlers/_numeric_ref.py` (§"Render `view='links'`") | `get(kind='todo', id=N, view='links')` | Outbound + inbound link graph for the ref. |
| `view='log'` | `src/precis/handlers/_numeric_ref.py` (§"Subsystem to filter `view='log'`") | `get(kind='todo', id=N, view='log')` | Event/audit trail, optionally filtered to one subsystem. |
| `view='raw'` | `src/precis/handlers/_numeric_ref.py` (§"`view='raw'` — the verbatim record") | `get(kind='todo', id=N, view='raw')` | Verbatim scalar columns + full body — escape hatch when the rendered view hides a field. |
| List views (`id='/<view>'`) | `src/precis/handlers/_numeric_ref.py::NumericRefHandler` | `get(kind='memory', id='/sticky')`, `get(kind='gripe', id='/open')`, `get(kind='gripe', id='/wontfix')` | Corpus-wide filtered list for the kind (pinned memories, open/wontfix gripes). |
| Paper default overview | `src/precis/handlers/paper.py::PaperHandler._render_overview` | `get(kind='paper', id='<slug>')` | Title/authors/abstract/identifiers + a `Next:` into `toc`/`abstract`. |
| `view='abstract'` | `paper.py::PaperHandler.get` (abstract branch) | `get(kind='paper', id='<slug>', view='abstract')` | Abstract text without paying for the full TOC. |
| `view='toc'` | `src/precis/handlers/_paper_toc.py::render_toc` | `get(kind='paper', id='<slug>', view='toc')` | Section/heading skeleton with chunk ranges — the drill-in map into the body. |
| `view='summaries'` | `paper.py::PaperHandler.get` (§"`view='summaries'` — per-chunk gloss list") | `get(kind='paper', id='<slug>', view='summaries')` | Per-chunk gloss for the whole body — a cheaper read than the full text. |
| `view='bibtex'`/`'ris'`/`'endnote'` | `paper.py::PaperHandler.get` (bibtex/ris/endnote branch) | `get(kind='paper', id='<slug>', view='bibtex')` | Citation-manager-ready export in the requested format. |
| `view='health'` | `paper.py::PaperHandler._render_health` | `get(kind='paper', id='<slug>', view='health')` | Identifier/metadata completeness check for the ref. |
| `view='abbrevs'` | `paper.py::PaperHandler.get` (abbrevs branch, reads `ref.meta['abbrevs']`) | `get(kind='paper', id='<slug>', view='abbrevs')` | Abbreviation-expansion table lazily populated for the paper. |
| `view='bibliography'` | `paper.py::PaperHandler._render_bibliography` | `get(kind='paper', id='<slug>', view='bibliography')` | Citations referencing this paper. |
| `view='log'` | `paper.py::PaperHandler.get` (log branch) | `get(kind='paper', id='<slug>', view='log')` | Ingest/edit audit trail for the paper. |
| `view='links'` | `paper.py::PaperHandler.get` (links branch) | `get(kind='paper', id='<slug>', view='links')` | Outbound + inbound link graph for the paper ref. |
| Chunk-range read | `paper.py::PaperHandler._render_chunks` | `get(kind='paper', id='<slug>~lo..hi')` | Verbatim body text for a chunk range — the actual source text, not a summary. |
| Todo search-side views | `src/precis/handlers/_todo_views.py::render_roots` / `render_projects` / `render_strategic` / `render_doable` / `render_waiting` / `render_blocked` / `render_ask_user` / `render_attention` (dispatch table keyed on `src/precis/handlers/todo.py::TodoView` enum) | `search(kind='todo', view='roots'\|'projects'\|'strategic'\|'doable'\|'waiting'\|'blocked'\|'ask-user'\|'attention')` | Each is a different cut of the todo tree — strategic dashboard, project rollup, unblocked-work queue, parked-on-a-human queue, etc. `doable` is the one an autonomous planner should be able to act on with zero round-trips. |
| Todo `view='tree'` | `src/precis/handlers/_todo_views.py::render_tree` (dispatched from `todo.py::TodoHandler.get`) | `get(kind='todo', id=N, view='tree')` | Full subtree rollup under one root — children + status. |
| Quest `view='tree'`/`'gaps'`/`'dossier'`/`'frontier'`/`'leaderboard'` | `src/precis/handlers/quest.py::QuestHandler.get` (per-view branches) | `get(kind='quest', id=N, view='tree'\|'gaps'\|'dossier'\|'frontier'\|'leaderboard')` | Servers+deed-ledger+health rollup; the focused exploration queue; the living research synthesis; the Pareto-frontier summary; the same frontier as a TOON design table. |
| Quest list views `id='/gaps'`/`'/active'`/`'/dormant'`/`'/abandoned'` | `src/precis/handlers/quest.py::QuestHandler.get` (path-view branch) | `get(kind='quest', id='/gaps')` | Corpus-wide exploration queue / lifecycle-filtered quest list. |
| Draft `view='toc'`/`'wordcount'`/`'backfill'`/`'links'` | `src/precis/handlers/draft.py::DraftHandler.get` | `get(kind='draft', id='<slug>', view='toc'\|'wordcount'\|'backfill'\|'links')` | Heading skeleton; per-section word counts vs. targets; source-backfill workspace for one heading; link graph. |
| Draft default node view | `src/precis/handlers/draft.py::DraftHandler.get` (default branch) | `get(kind='draft', id='dc123')` | One heading node's rendered content. |
| Draft search render | `src/precis/handlers/draft.py::DraftHandler._render_search` | `search(kind='draft', q='…')` | Block-level hits inside draft prose, headings-only mode included. |
| Skill default card+body | `src/precis/handlers/skill.py::SkillHandler.get` | `get(kind='skill', id='precis-overview')` | Full skill body — the agent-facing reference/procedure text. |
| Skill `view='toc'` (per-skill) | `skill.py::SkillHandler.get` (`effective_view == 'toc'` branch) | `get(kind='skill', id='precis-overview', view='toc')` | Section skeleton of one skill file. |
| Skill corpus index | `skill.py::SkillHandler._render_toc` | `get(kind='skill', id='toc')` | Every skill, one line each — the entry point for "which skill do I need." |

#### Composite blobs (server assembles the LLM prompt AND persists the rendered result)

These differ from the rows above: each makes its own LLM call (not a
lexical/semantic render) and the "poke" is a two-step trigger-then-read, not
a single read-verb round-trip. Cataloged here because the *result* is
inspected the same way (`get` on the persisted ref), but flagged: they are
really Axis-B-shaped pipelines with an Axis-A-shaped output.

| context | producer | how to poke | what it ought to do |
|---|---|---|---|
| Morning reading-brief | `src/precis/reading/briefing_cast.py::build_reading_briefing` (lane gather: `_gather_lanes`; one LLM call: `_compose`) | `precis cast run reading` (CLI, `src/precis/cli/cast.py`), then `get(kind='draft', id=<returned id>)` | Union news/system/reading/recall/quest lanes into one spoken-length morning script; idempotent per day; degrade any lane to `""` rather than fail the whole brief. |
| News digest | `src/precis/workers/briefing.py::run_briefing` (context block: `_format_context`; prompt modules: `_BRIEFING_MODULES`) | triggered by the `news_poll`/briefing watch; inspect via `get(kind='news', id='briefing-<date>')` | Editorial digest over overnight `news` refs — separate "the loud story" from "what government actually did"; delivered as a `message` ref when `deliver_to` is set. |
| Evening meditation (nidra) | `src/precis/reading/meditation.py::compose_script` (long-form: `_compose_long`) | `precis cast run nidra` (CLI), then `get(kind='draft', id=<returned id>)` | Calm walk through recently-touched concepts in adjacency order, word-budgeted to the target runtime. |
| Skill index toc | (see above — `skill.py::SkillHandler._render_toc`) | `get(kind='skill', id='toc')` | — |

### Axis B — server-built (agentic) contexts

The server assembles a system+user prompt and dispatches it to an LLM
itself; the caller who triggered the job never sees the prompt unless they
go looking. Most route through the ADR-0038 assembler:
`src/precis/utils/prompt/assembler.py::assemble(modules, ctx) -> list[Block]`,
rendered per-backend by an adapter (`ClaudeAgentAdapter.render` /
`LiteLLMAdapter.render`). The planner is the largest: modules + assembly in
`src/precis/workers/planner_prompt.py` — cached system layer built from
`_CACHED_MODULES` (`_build_system_prompt`), the per-tick user prompt from
`_build_user_prompt` and its per-section helpers (`_render_project_brief`,
`_render_workspace_status`, `_render_seeds`, `_render_glossary`, …).

| context | producer | how to poke | what it ought to do |
|---|---|---|---|
| Planner tick | `src/precis/workers/job_types/plan_tick.py::run` | `/env?agent=plan_tick` (static system/directive/MCP-config inspection) + `scripts/context-audit` (per-run capture, once built) | Give the planner enough of the todo tree + draft + glossary + prior-turn state to pick or execute one next action without a round-trip. |
| Quest tick | `src/precis/workers/job_types/quest_tick.py::_dispatch` | `/env?agent=quest_tick` | Drive one simulation/exploration step of a quest's frontier search. |
| Deep-search campaign | `src/precis/workers/job_types/good_search.py::_dispatch` (+ `_triage_dispatch`) | `/env?agent=good_search` | Plan a multi-leg broad search, then triage + merge child verdicts. |
| Gripe auto-fix | `src/precis/workers/job_types/fix_gripe.py::run` | `/env?agent=fix_gripe` | Reproduce + patch a filed gripe against the resolved repo path. |
| Morning briefing dispatch | `src/precis/workers/job_types/briefing.py::_dispatch` | `/env?agent=briefing` | Wrapper job around `workers/briefing.py::run_briefing` (see composite-blob row above). |
| Reading-brief dispatch | `src/precis/workers/job_types/reading_brief.py::_dispatch` | `/env?agent=reading_brief` | Wrapper job around `briefing_cast.py::build_reading_briefing`. |
| Meditation dispatch | `src/precis/workers/job_types/meditation.py::_dispatch` | `/env?agent=meditation` | Wrapper job around `meditation.py::compose_script`/`_compose_long`. |
| News poll | `src/precis/workers/job_types/news_poll.py::_dispatch` | `/env?agent=news_poll` | Fetch + classify incoming news items. |
| Card forge | `src/precis/workers/job_types/card_forge.py::_dispatch` | `/env?agent=card_forge` | Synthesize `ord<0` card variants for a ref. |
| Structure propose | `src/precis/workers/job_types/structure_propose.py::_dispatch` (prompt: `build_prompt`, parse: `parse_proposal`) | `/env?agent=structure_propose` | Propose a scene/structure edit as parseable ops, dry-runnable before commit. |
| Diagram propose | `src/precis/workers/job_types/diagram_propose.py::_dispatch` (message: `compose_message`) | `/env?agent=diagram_propose` | Propose a mermaid diagram edit from seeds + instruction. |
| CAD propose | `src/precis/workers/job_types/cad_propose.py::_dispatch` (prompt: `build_prompt`, parse: `parse_proposal`) | `/env?agent=cad_propose` | Propose a CAD-source edit, dry-runnable before commit. |
| CAD discuss | `src/precis/workers/job_types/cad_discuss.py::_dispatch` (prompt: `build_prompt`) | `/env?agent=cad_discuss` | Turn-based design discussion grounded in feature bounds + prior turns. |
| Structure relax | `src/precis/workers/job_types/struct_relax.py::build_run_argv` (dispatch is a container run, not an LLM prompt — no assembler module here) | `/env?agent=struct_relax` | Not an LLM context — flagged here only because it shares the job_types directory; a relaxation container invocation. |
| Sandbox run | `src/precis/workers/job_types/sandbox_run.py::validate_submit` | `/env?agent=sandbox_run` | Validate + gate a sandboxed compute job before it runs (ADR 0048). |
| Draft export | `src/precis/workers/job_types/draft_export.py::_dispatch` | `/env?agent=draft_export` | Render a draft to docx/pdf via the export path — mostly deterministic, may call an LLM for a cover/summary pass. |
| ReMarkable send | `src/precis/workers/job_types/remarkable_send.py::_dispatch` | `/env?agent=remarkable_send` | Push a rendered doc to a paired reMarkable device. |
| ReMarkable papers send | `src/precis/workers/job_types/remarkable_papers_send.py::_dispatch` | `/env?agent=remarkable_papers_send` | Not an LLM context — deterministic push of a draft's cited source PDFs to a paired reMarkable device. |
| Structural review digest | `src/precis/workers/structural.py::_structural_body` (context: `_structural_context`; modules: `_STRUCTURAL_MODULES`; driver: `src/precis/workers/review.py::run_review_pass`) | `/env?agent=structural` | Cheap SQL-scoped watch over the strategic layer + recent nursery excerpt; escalate to a `gripe`/digest memory only on a real finding. |
| Deep review digest | `src/precis/workers/deep_review.py::_deep_body` (context: `_deep_context`; driver: `review.py::run_review_pass`) | `/env?agent=deep_review` | Opus-tier deep pass over the strategic dashboard + recent review summary — the expensive tier, gated to avoid duplicate digests inside `min_interval_hours`. |

**`/env?agent=<name>`** (`src/precis_web/routes/env.py`) today shows the
*static* wiring for an agent profile — resolved system-prompt file,
directive-prompt file, MCP config, model, and which env vars are set — read
from the target LaunchDaemon's plist, not the web process's own env. It does
**not** yet render the *live per-run* assembled prompt (real ref data
substituted into the modules above) — that's the gap Part 3 (being built in
parallel, alongside `scripts/context-audit/`) closes.

**Coverage**: 2 cross-kind render primitives, 7 special search views, 19
per-kind `get(view=)` rows, 4 composite blobs = **32 Axis A rows**. 17 Axis B
job-type/reviewer producers (one, `struct_relax`, is a non-LLM container
invocation, flagged rather than dropped so the catalog stays exhaustive over
`job_types/`).

## Section 2 — The inspection rubric

Apply to any sampled context (an Axis A response captured live, or an Axis B
prompt captured via `/env` + `scripts/context-audit`). Every finding files as
a `gripe` at the severity the sampled instance warrants — this repo's
existing taxonomy (`docs/mcp-critic-review-2026-05-02.md`) uses `MAJOR-C`
(correctness) / `MAJOR-$` (cost) / `MINOR-C` / `NIT`; reuse those suffixes,
don't invent a new scale. Gate vocabulary (static hard-fail vs. LLM-judged
soft-fail-to-gripe) follows `docs/backlog/docs-and-skills-redesign.md`
§"Quality gates".

1. **Skills reachable?** Does the context name/link the skill the next
   action needs, and does `get(kind='skill', id=…)` actually return it (not
   a 404, not a stale toc entry)?
2. **Info sufficient to make the call?** Every field the next action needs
   is present in-band, or does the agent have to guess or round-trip for
   it? (E.g. a `Next:` hint that names a handle the response never
   surfaced.)
3. **Breadcrumb / `Next:` correctness.** Does the trailer point at a real,
   runnable next verb — not a stale view name, not a kind the build has
   disabled?
4. **Progressive disclosure.** Right altitude for the ask — not a wall of
   text for a one-line question, not a truncated stub when the agent asked
   for the whole thing. Pagination (`# N of K`) sane and consistent with
   `format_search_headline`'s contract.
5. **Surface↔behavior drift.** Does the render match what the corresponding
   skill/`precis-overview` row claims it does? (The MAJOR-C `precis-overview`
   drift-from-live-registry finding in the mcp-critic review is the
   canonical example of this class.)
6. **Classifier / pre-worker gap.** Is a needed field empty because *no*
   upstream pass populated it yet (missing `chunks.keywords`, unclassified
   `role3`, absent chunk summary, un-run health check)? This is the
   load-bearing "do we need another classifier/pre-worker" signal — file it
   as its own finding class, not folded into #2, so it's queryable
   separately from "the field exists but the render forgot to show it."

The runnable capture/score procedure lives in `scripts/context-audit/`
(built separately) — this doc is the catalog + rubric it executes against.

## Residuals (from OPEN-ITEMS)

Round-2 agent-facing render bugs, precisely root-caused, unfixed:
- Attention view drops a halted todo's reason —
  `src/precis/handlers/_todo_views.py::render_attention` builds h['reasons']
  from halt:<reason> tags but only prints id+title (the sibling child-failed
  loop shows its reason). test: a halt-tagged leaf shows the reason inline.
- Cross-kind / view='keywords' TOON tables drop the universal handle for
  numeric-ref kinds (bare integer) —
  `src/precis/handlers/_numeric_ref.py::_body_search_hits` missing uhandle=;
  `src/precis/utils/search_merge.py` table renderers fall back to
  str(ref_id); handle_registry CHUNK_CODES missing "orcid". test: renders
  m<id>/oi<id>, not bare ints.
- Quest frontier shows the default "objective: energy (min)" for
  non-materials quests — suppress/qualify when no candidates and
  meta.rubric_objectives unset (`quest.py::_render_frontier`).
- sort='recency' source-search omits the N-of-K total + per-kind breakdown —
  `src/precis/runtime/search.py::_dispatch_source_search`.
- view='strategic' has no scoping/pagination (deferred — possibly an
  intentional dashboard). The quest-domain classifier gap is gr170252.
