# Glossary — coined & overloaded vocabulary

> **Audience: Claude Code + humans working in the source tree.** It maps the
> project's *coined* words (memorable but non-obvious) and its *overloaded*
> words (same token, several unrelated meanings) to the **one best file to
> start reading** for each. It is a code entry-point index, not a dictionary —
> deliberately thin. The **kinds** themselves (`paper`, `todo`, `quest`, …)
> live in the `precis-overview` skill's master table, not here; runtime agents
> get their vocabulary through the skills, so only the small overloaded set
> that leaks into MCP output is echoed there.
>
> **Format:** `term — one-line gloss. → best-entry-point file · skill (if any)`.
> The pointer is a *start-here*, not a grep dump.
>
> **Keep it true:** a new coined/overloaded term is added here in the same
> commit that introduces it — one line, one pointer. When a term's home moves,
> only the pointer changes.

## Coined terms

- **dark** ("ships / merges dark") — landed on `main` but disabled by default,
  behind an off-by-default env gate; the slice merges without activating.
  → `src/precis/cli/worker.py` (the `PRECIS_*_ENABLED` gates)
- **watch** — a `level:recurring` todo whose `meta.schedule` (cron / `every:`)
  drives a per-minute spawner. → `src/precis/workers/schedule/worker.py` ·
  skill `precis-recurring-help`
- **doable** — the view of todos eligible to be picked (open, unblocked, not
  bubbled). → `src/precis/handlers/_todo_views.py`
- **rotation** — the 1/N round-robin across strategic roots (by 7-day picks)
  that chooses the next task. → `src/precis/handlers/_todo_views.py`
- **bubble** (failure-bubble / `child-failed`) — a failed job tags its parent
  `child-failed:<job_id>`, dropping it from the doable rotation until the owner
  decides retry / switch / give-up. → `src/precis/handlers/_job_bubble.py`
- **intent lane / compute lane** — the two kinds of job parent (ADR 0044): an
  intent-lane job hangs off a `todo` (enters rotation, bubbles on failure); a
  compute-lane job hangs off a build artifact (structure/cad/draft — derived,
  content-addressed, cache-fillable). → `docs/decisions/0044-derived-job-lane.md`
- **derived job** — a compute-lane job (DFT relax / route / compile): idempotent
  + content-addressed, owned by the artifact, no rotation to enter.
  → `docs/decisions/0044-derived-job-lane.md`
- **planner coroutine / `plan_tick`** — an `LLM:*`-tagged todo run as a resumable
  coroutine; each tick is a job that may mint children or yield, and an
  exhaustion (max-turns / timeout) is resumable, not a failure.
  → `src/precis/workers/job_types/plan_tick.py` · skill `precis-dispatch-help`
- **striving** — a `quest`: a perpetual, unachievable aim. Never `done`
  (`active|dormant|abandoned`). → `src/precis/handlers/quest.py`
- **serves** — the link relation marking a todo/project/artifact as working
  toward a quest — the DAG above the todo tree. → `src/precis/handlers/quest.py`
- **deed** — a `milestone` logbook entry; the honest unit of quest progress.
  → `src/precis/quest/logbook.py`
- **tote** — a running total computed as a *query over a dated log*, not a stored
  counter (quest lifetime cost; the `llm_tote` call rollup).
  → `src/precis/quest/logbook.py` · `src/precis/llm_catalog.py`
- **logbook** — an append-only, WORM, dated entry stream on a ref (`quest_log`;
  the gripe body+comment pattern). → `src/precis/quest/logbook.py`
- **reweight / striving weight** — priority flowing down the `serves` DAG into
  rotation / acquisition / reading (max-agg, decay per hop; active quests only).
  → `src/precis/quest/reweight.py`
- **frontier** — the Pareto split of a quest's candidate structures over its
  objective axes. → `src/precis/quest/frontier.py`
- **cast** — a daily audio episode (morning `reading` brief; evening `nidra`
  meditation) on the produce→narrate→publish spine.
  → `src/precis/reading/cast_common.py` · skill `precis-audio-help`
- **lane** (brief) — a contributor to a morning-brief cast (news / system /
  recall / quest), each degrade-to-empty. → `src/precis/reading/briefing_cast.py`
- **nursery** — the SQL-only, per-minute reviewer that raises health/ops alerts
  (spin loops, worker health). → `src/precis/workers/nursery.py` ·
  skill `precis-nursery-help`
- **dream** — the autonomous 15-min `dream_agent` pass.
  → `src/precis/workers/dream_agent.py`
- **spin loop** — a `(ref_id, source)` re-emitting > 200 `ref_events`/24h; the
  nursery flags it. Usually a *stale deploy*, not a new bug.
  → `src/precis/workers/nursery.py`
- **stale deploy** — prod running pre-fix code after a merge; the usual cause of
  a recurring spin-loop or alert. Check the deployed sha, not the source.
- **jetsam** — a launchd daemon culled by macOS under RAM pressure; the nursery
  `worker-restart` alert (`WORKER_RESTART_STORM_1H`) is the in-DB signal.
  → `src/precis/workers/nursery.py`
- **keystone kind** — a kind that owns a legible IR and rents the heavy kernel
  only at export (cad/pcb/structure); the LLM traverses a graph, never pixels.
  → `docs/decisions/0041-cad-kind-analytic-ir.md` (also 0042, 0043)
- **emits_card / "a card is a vector"** — a `KindSpec` flag: the kind emits a
  `card_combined` chunk (ord=-1) so the ref itself embeds + searches.
  → `src/precis/protocol.py` (`KindSpec.emits_card`)
- **handle** — a terse per-kind pointer (`qu`/`dc`/`pc`/`me`… + id) that resolves
  to a ref or chunk. → `src/precis/handlers/_numeric_ref.py` · `src/precis/runtime/dispatch.py`
- **admit** — the pre-flight fit-check that refuses a (context, model) pairing
  too big for the model's window, with the numbers. → `src/precis/utils/llm/admit.py`

## Overloaded — which one?

(These also leak into MCP output, so the short version is echoed in the
`precis-overview` skill for runtime agents.)

- **tier** — classifier tiers (0/1/2) · reviewer tiers (nursery/structural/
  deep_review) · search tiers (Tier 1 RRF / Tier 2 good-search) · LLM tiers
  (`tier_floor`, `gate_tier`, opus/sonnet/haiku).
  → `src/precis/workers/classify.py` · `src/precis/workers/review.py` ·
  `src/precis/utils/llm/router.py`
- **card** — an embedding chunk (`card_combined` / `card_glossary`, the
  searchable vector) · an Anki flashcard (`card_forge` mints these) · a catalog
  entry (`llm` / `quest` / `concept`).
  → `src/precis/protocol.py` (emits_card) · `src/precis/handlers/anki.py` ·
  `src/precis/handlers/llm.py`
- **role** — the classifier content axis `role` / `role3` (own/background/
  furniture) · `corpus_role` (evidence/spec/none — citability) · `KindSpec.role`
  (artifact/corpus/stream/system — folder placement).
  → `src/precis/data/axes/` · `src/precis/protocol.py`
- **lane** — job parent lane (intent vs compute, ADR 0044) · morning-brief lane
  (news/recall/quest). → `docs/decisions/0044-derived-job-lane.md` ·
  `src/precis/reading/briefing_cast.py`
- **dispatch** — the `dispatch` worker (mints jobs from doable todos) ·
  `runtime.dispatch` (in-process MCP verb call) · `dispatch(LlmRequest)` (the
  LLM router). → `src/precis/workers/dispatch.py` · `src/precis/runtime/dispatch.py` ·
  `src/precis/utils/llm/router.py`
- **plan** — the `plan` kind (a thread's reasoning outline, ADR 0051) vs
  `plan_tick` (the planner-coroutine job).
  → `src/precis/handlers/plan.py` · `src/precis/workers/job_types/plan_tick.py`
- **fetch / chase** — `fetch` / `fetch_oa` (acquire a paper PDF) vs finding-
  `chase` (resolve an open finding). Both exponential-backoff.
  → `src/precis/workers/fetch_oa.py` · `src/precis/workers/chase.py`
- **source** — an episode's producer tag (`brief` / `meditation` / `news`) · a
  chunk's provenance (`meta.source`) · the OA fetch backoff arms on `fetcher:%`
  events. → `src/precis/audio_feed.py`

## Projects & quests — informal name → canonical pointer

Same start-here discipline as above, but the pointer is a `todo`/`quest` id,
not a file — these don't have a code home. The point is to survive the
spoken/informal name (what Reto actually says) not matching what's indexed
in `search(kind='todo'|'quest', ...)`. One line per item; when a project ships
or a quest's id changes, edit in place — don't append a status history (live
`STATUS` is a DB fact, query `get(kind='quest'|'todo', id=…)` for the current
value, don't trust a snapshot here).

- **catpath** ("lm-potential", "Pd/NO→NH₃", "the palladium catalyst quest",
  "catalyst-discovery loop") — the autonomous catalyst-discovery loop.
  "LM-potential" (the catpath/MACE machine-learned-potential barrier step) and
  "lit" (the literature-grounding step) are two steps *inside* this quest's
  own compute cycle (setup → LM-potential → review/Pareto → lit → setup-new →
  maintain front), not separate projects. → quest `164903`
  (`get(kind='quest', id=164903)`); design docs
  `docs/design/catpath-integration.md` ·
  `docs/design/catalyst-discovery-quest.md`; catpath itself is a separate
  repo, github.com/retospect/catpath, integrated into precis as the `pathway`
  kind. Sibling quest `161910` shares the Pd/NO→NH₃ theme. Dormant quests
  auto-cool after stale ticks — not a hard block.
- **NOx to Ammonia** ("nox2nh3") — the DFT/operando-MS design-loop
  manuscript project; the write-up, distinct from quest `164903`'s ongoing
  autonomous-discovery loop. → todo `td34571` (`projects/nox2nh3_auto`).
- **nanotrans_auto** ("Nano-transistors") — → todo `td6649`
  (`projects/nanotrans_auto`).
- **nanotrans2** — survey of the state of carbon-nanoribbon transistors.
  → todo `td44368` (`projects/nanotrans2`).
- **dftmodelmcp** — an MCP for DFT modeling. → todo `td44759`
  (`projects/dftmodelmcp`).
- **mofs-for-electrodes** — → todo `td48056` (`projects/mofs-for-electrodes`).
- **gold-sea** / **gold-sea-2** — gold recovery from seawater. → todo
  `td43250` / `td43578` (`projects/gold-sea` / `projects/gold-sea-2`).
- **mechacard** — mechanical-cartridge project. → todo `td55666`
  (`projects/mechacard`).
- **dream-review** — → todo `td48091` (`projects/dream-review`).
- **screwholder** — patent application. → todo `td41686`
  (`projects/screwholder`).
- **workshop260624-ai** — Bernal Generative AI workshop notes. → todo
  `td41729` (`projects/workshop260624-ai`).

Other named quests (id → one-line gloss; query `get(kind='quest', id=…)` for
current `STATUS`):

- `161906` — "a world that runs light on the planet"; the umbrella quest,
  served by 161907/161908/161910.
- `161907` — self-assembling, atomically-precise compute substrate (DNA-tile
  / molecular computing angle).
- `161908` — "structures lighter than air" (ultralight aerostructures).
- `161909` — grow atomically-precise structure — switches, boxels, tilings
  (DNA-tile self-assembly yield research).
- `169855` — keep scientific-literature integrity practices at the frontier;
  citation/claim-grounding meta-quest, relates to ADR 0047's ROLE3:own filter.
- `169953` — "don't let precis get bamboozled by a bad paper"; evidence-
  grounding meta-quest, same ADR 0047 relation.
