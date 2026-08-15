# Glossary — coined & overloaded vocabulary

> **One entry = one line, no wrapping, no line-length limit** — so
> `grep -i <term> docs/glossary.md` returns the whole entry. Don't reflow
> or wrap entries when editing.
>
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

- **dark** ("ships / merges dark") — landed on `main` but disabled by default, behind an off-by-default env gate; the slice merges without activating. → `src/precis/cli/worker.py` (the `PRECIS_*_ENABLED` gates)
- **watch** — a `level:recurring` todo whose `meta.schedule` (cron / `every:`) drives a per-minute spawner. → `src/precis/workers/schedule/worker.py` · skill `precis-recurring-help`
- **doable** — the view of todos eligible to be picked (open, unblocked, not bubbled). → `src/precis/handlers/_todo_views.py`
- **rotation** — the 1/N round-robin across strategic roots (by 7-day picks) that chooses the next task. → `src/precis/handlers/_todo_views.py`
- **bubble** (failure-bubble / `child-failed`) — a failed job tags its parent `child-failed:<job_id>`, dropping it from the doable rotation until the owner decides retry / switch / give-up. → `src/precis/handlers/_job_bubble.py`
- **intent lane / compute lane** — the two kinds of job parent (`handlers/job.py`): an intent-lane job hangs off a `todo` (enters rotation, bubbles on failure); a compute-lane job hangs off a build artifact (structure/cad/draft — derived, content-addressed, cache-fillable). → `src/precis/handlers/job.py`
- **derived job** — a compute-lane job (DFT relax / route / compile): idempotent + content-addressed, owned by the artifact, no rotation to enter. → `src/precis/handlers/job.py`
- **boot epoch / worker generation** — a uuid4 minted once per worker process at startup (`mint_boot_id`), advertised in `host_heartbeat.meta.boot_ids`; stamped onto every job claim as `meta.lease_boot_id` so a later successor can prove a `STATUS:running` row's prior holder was provably replaced (epoch reclaim) rather than waiting out the lease's full expiry. → `src/precis/workers/executors/_common.py` (`claim_executor_jobs`)
- **planner coroutine / `plan_tick`** — an `LLM:*`-tagged todo run as a resumable coroutine; each tick is a job that may mint children or yield, and an exhaustion (max-turns / timeout) is resumable, not a failure. → `src/precis/workers/job_types/plan_tick.py` · skill `precis-dispatch-help`
- **striving** — a `quest`: a perpetual, unachievable aim. Never `done` (`active|dormant|abandoned`). → `src/precis/handlers/quest.py`
- **serves** — the link relation marking a todo/project/artifact as working toward a quest — the DAG above the todo tree. → `src/precis/handlers/quest.py`
- **deed** — a `milestone` logbook entry; the honest unit of quest progress. → `src/precis/quest/logbook.py`
- **tote** — a running total computed as a *query over a dated log*, not a stored counter (quest lifetime cost; the `llm_tote` call rollup). → `src/precis/quest/logbook.py` · `src/precis/llm_catalog.py`
- **logbook** — an append-only, WORM, dated entry stream on a ref (`quest_log`; the gripe body+comment pattern). → `src/precis/quest/logbook.py`
- **reweight / striving weight** — priority flowing down the `serves` DAG into rotation / acquisition / reading (max-agg, decay per hop; active quests only). → `src/precis/quest/reweight.py`
- **frontier** — the Pareto split of a quest's candidate structures over its objective axes. → `src/precis/quest/frontier.py`
- **tier ladder** — a quest candidate's progressive-fidelity autocatpath ladder, screening → neb → verify; code-driven, capped promotion, no LLM surface. → `src/precis/quest/compute.py` (`promote_tiers`)
- **screening tier** — the ladder's cheapest rung: relax-only, catpath emits no barrier scalar, ranks on thermodynamics alone. → `src/precis/quest/compute.py::_apply_tier_config`
- **verify tier / coadsorbed template** — the ladder's highest-fidelity rung: a full NEB over catpath's `template="coadsorbed"` network, which drops the parking approximation (below). Verify-gates graduation on a ladder-on quest. → `src/precis/quest/graduate.py`
- **parking approximation** — the `neb`-tier network's simplifying assumption (a dissociated fragment "parks" in a reservoir rather than staying co-adsorbed); the `verify` tier removes it. → `catpath` `network.py::build_coadsorbed_ammonia_network`
- **CHE / potential lever** — the computational hydrogen electrode formalism: an applied potential U (vs RHE) shifts each node's energy by `n_H·eU`, closed-form (no extra compute) — `U_L`/`U_opt`/`span_at_U*`/`P_side` are its derived scalars. → `src/precis/quest/compute.py` (the CHE scalar block)
- **frontier tree** — a quest's candidate lineage (`derived-from` parents) rendered as an indented markdown tree, code-regenerated into a pinned dossier chunk every tick. → `src/precis/quest/frontier.py::render_frontier_tree`
- **publish row** — the ONE mutable working row per claim hub in `nanopub_publish`: the frozen approved string + state machine; the append-only signed bytes live in `nanopub_artifacts`. → `src/precis/store/_nanopub_ops.py` · the `precis.nanopub` package docstring
- **hanging claim** — a claim minted with unresolved provenance (`grounding.hanging`), e.g. hearsay grounding rejected while the original-paper hunt runs; mintable, but publish preflight blocks it like withheld edges. → `src/precis/nanopub/gates.py`
- **trusty URI** — a nanopub's content-hash identity (`https://w3id.org/np/RA…`), created by signing; distinct from the claim's text-derived AIDA URI, which survives re-signing. → `src/precis/nanopub/mint.py`
- **searchSnip** — the lowercase-ASCII-token locator published beside a source quote; validated unique-within-paper at mint, doubles as the PDF deep-link query. → `src/precis/nanopub/snip.py`
- **cast** — a daily audio episode (morning `reading` brief; evening `nidra` meditation) on the produce→narrate→publish spine. → `src/precis/reading/cast_common.py` · skill `precis-audio-help`
- **lane** (brief) — a contributor to a morning-brief cast (system / reading / recall / quest), each degrade-to-empty. The world-news wire is not a lane — `cast_audio` prepends it at narration time instead. → `src/precis/reading/briefing_cast.py`
- **nursery** — the SQL-only, per-minute reviewer that raises health/ops alerts (spin loops, worker health). → `src/precis/workers/nursery.py` · skill `precis-nursery-help`
- **dream** — the autonomous 15-min `dream_agent` pass. → `src/precis/workers/dream_agent.py`
- **spin loop** — a `(ref_id, source)` re-emitting > 200 `ref_events`/24h; the nursery flags it. Usually a *stale deploy*, not a new bug. → `src/precis/workers/nursery.py`
- **stale deploy** — prod running pre-fix code after a merge; the usual cause of a recurring spin-loop or alert. Check the deployed sha, not the source.
- **jetsam** — a launchd daemon culled by macOS under RAM pressure; the nursery `worker-restart` alert (`WORKER_RESTART_STORM_1H`) is the in-DB signal. → `src/precis/workers/nursery.py`
- **keystone kind** — a kind that owns a legible IR and rents the heavy kernel only at export (cad/pcb/structure); the LLM traverses a graph, never pixels. → `src/precis/handlers/pcb.py` · `src/precis/structure/__init__.py`
- **emits_card / "a card is a vector"** — a `KindSpec` flag: the kind emits a `card_combined` chunk (ord=-1) so the ref itself embeds + searches. → `src/precis/protocol.py` (`KindSpec.emits_card`)
- **handle** — the one kind-agnostic address for any record or chunk: a 2-char type code + the row's decimal id (`pa5`, `pc10`, `td42`). Computed, never stored — a pure function of `(kind, id)`, so no minting or backfill; resolves without a `kind=`. → `src/precis/utils/handle_registry.py`
- **admit** — the pre-flight fit-check that refuses a (context, model) pairing too big for the model's window, with the numbers. → `src/precis/utils/llm/admit.py`
- **taproot** — the evidence-grounded claim graph: unify a claim into one hub node, ground it in many papers as typed graded evidence, resolve citations onto it. Phased build (1–5). → `docs/backlog/taproot.md` · `src/precis/taproot/`
- **claim hub** — a `finding` tagged `TAPROOT:claim`, promoted to *the* node a claim lives on; many papers attach as `establishes`/`corroborates`/`contradicts` evidence edges (taproot Phase 2). → `docs/backlog/taproot-phase2-hub-node.md`
- **TAPROOT** — the `finding`-ref discriminator axis: `TAPROOT:claim` (grounded world-claim, a taproot hub) vs `TAPROOT:review` (editorial/manuscript note, excluded from the claim graph). Classifier = `data/axes/taproot.yaml` via `axis_pass`. → `src/precis/data/axes/taproot.yaml`
- **establishes / corroborates** — taproot evidence-edge roles (paper → claim hub): the *originator(s)* that first showed a claim `establishes` it; later papers that cite them `corroborate`. Derived from the citation graph, not hand-set. → `docs/backlog/taproot.md` §"Seniority is derived"
- **atom / compound claim hub** — a decomposed sentence's split: each **atom** is an ordinary evidence-bearing claim hub; the **compound** is the un-split bundling sentence, linked `conjunct-of` from each atom (migration 0126), holds no direct evidence, and rolls up worst-of its atoms' trust. → `src/precis/taproot/hub.py::apply_extraction`
- **world-claim vs legal claim** — "claim" is overloaded; always use the unique name. A *world-claim* is a taproot claim (a `TAPROOT:claim` finding — a statement about the world). A *legal claim* is a numbered entry in a patent's claims section (legal scope, not an empirical result; the `claims`-section blocks with `claim_number` meta). World-claims ground in patent *description* blocks, never legal-claim blocks. → `src/precis/workers/hub_refine.py`
- **worked vs prophetic example** — patent-example evidentiary weight (US convention, tense-of-performance test): a *worked* example narrates an experiment in past tense as actually performed; a *prophetic* example describes it in present/future/modal voice as merely doable. Chunk-level classifier axis (`worked`/`prophetic`/`none`); a prophetic supporting block earns a fixed evidence-edge caveat ("corroborates at best"), never a hard exclusion. → `src/precis/data/axes/patent_example.yaml`
- **patent family / family representative** — family identity is authoritative (DOCDB family ID from EPO OPS), so no hub node: `family_id` in ref meta, a deterministic *representative* (earliest-published ingested member), siblings in the same *simple* family ingested as stub refs linked `same-family-as` (new-matter members — CIP/divisional — get full ingest). Cites render one representative per family. → `src/precis/handlers/_patent_family.py`
- **fisheye** (eye) — a degree-of-interest render: focus one node and get it plus its scaled-by-distance surroundings, not a bare chunk (turn-taking design, git-only). `get(..., view='fisheye'/'fisheye+1hop')`. → `src/precis/utils/fisheye.py`, `src/precis/utils/eye_render.py` · skill `precis-fisheye-help`
- **extent ladder** — the `kwd < summary < verbatim < fisheye < fisheye+1hop` ordinal rungs (each strictly containing the previous) that `view=` picks a point on. → `src/precis/workers/working_set.py::Extent` · skill `precis-fisheye-help`

## Overloaded — which one?

(These also leak into MCP output, so the short version is echoed in the
`precis-overview` skill for runtime agents.)

- **tier** — classifier tiers (0/1/2) · reviewer tiers (nursery/structural/deep_review) · search tiers (Tier 1 RRF / Tier 2 good-search) · LLM tiers (`tier_floor`, `gate_tier`, opus/sonnet/haiku). → `src/precis/workers/classify.py` · `src/precis/workers/review.py` · `src/precis/utils/llm/router.py`
- **card** — an embedding chunk (`card_combined` / `card_glossary`, the searchable vector) · an Anki flashcard (`card_forge` mints these) · a catalog entry (`llm` / `quest` / `concept`). → `src/precis/protocol.py` (emits_card) · `src/precis/handlers/anki.py` · `src/precis/handlers/llm.py`
- **role** — the classifier content axis `role` / `role3` (own/background/furniture) · `corpus_role` (evidence/spec/none — citability) · `KindSpec.role` (artifact/corpus/stream/system — folder placement). → `src/precis/data/axes/` · `src/precis/protocol.py`
- **lane** — job parent lane (intent vs compute) · morning-brief lane (news/recall/quest). → `src/precis/handlers/job.py` · `src/precis/reading/briefing_cast.py`
- **dispatch** — the `dispatch` worker (mints jobs from doable todos) · `runtime.dispatch` (in-process MCP verb call) · `dispatch(LlmRequest)` (the LLM router). → `src/precis/workers/dispatch.py` · `src/precis/runtime/dispatch.py` · `src/precis/utils/llm/router.py`
- **plan** — the `plan` kind (a thread's reasoning outline) vs `plan_tick` (the planner-coroutine job). → `src/precis/handlers/plan.py` · `src/precis/workers/job_types/plan_tick.py`
- **fetch / chase** — `fetch` / `fetch_oa` (acquire a paper PDF) vs the finding-`chase` verb (resolve an open finding; exponential-backoff). **`chase` is the verb, not the graph** — keep it distinct from **taproot** (the claim graph it feeds) and a **claim hub** (the `fi<id>` node). The verb now spans **three independently-gated passes**, all dark (default-OFF): (1) outbound support-verdict `PRECIS_CHASE_LLM` (`workers/chase.py` · `_chase_llm.py`), (2) inbound corpus sweep `PRECIS_INBOUND_CHASE_ENABLED` (`workers/inbound_chase.py`), (3) the **taproot forward-bridge** `PRECIS_TAPROOT_CHASE_ENABLED` that mints/attaches claim-hub evidence edges (`workers/chase.py` + `taproot/hub.py`; taproot Phase 3 W1, `bb2eb73e`). → `src/precis/workers/{fetch_oa,chase,inbound_chase}.py`
- **source** — an episode's producer tag (`brief` / `meditation` / `news`) · a chunk's provenance (`meta.source`) · the OA fetch backoff arms on `fetcher:%` events. → `src/precis/audio_feed.py`

## Projects & quests — informal name → canonical pointer

Same start-here discipline as above, but the pointer is a `todo`/`quest` id,
not a file — these don't have a code home. The point is to survive the
spoken/informal name (what Reto actually says) not matching what's indexed
in `search(kind='todo'|'quest', ...)`. One line per item; when a project ships
or a quest's id changes, edit in place — don't append a status history (live
`STATUS` is a DB fact, query `get(kind='quest'|'todo', id=…)` for the current
value, don't trust a snapshot here).

- **autocatpath** ("lm-potential", "Pd/NO→NH₃", "the palladium catalyst quest", "catalyst-discovery loop") — the autonomous catalyst-discovery loop. "LM-potential" (the autocatpath/MACE machine-learned-potential barrier step) and "lit" (the literature-grounding step) are two steps *inside* this quest's own compute cycle (setup → LM-potential → review/Pareto → lit → setup-new → maintain front), not separate projects. → quest `164903` (`get(kind='quest', id=164903)`); design docs `docs/backlog/autocatpath-integration.md` · `docs/backlog/catalyst-discovery-quest.md`; autocatpath itself is a separate repo, github.com/retospect/catpath, published as a precis-free science library — precis-mcp depends on it (`[catalyst]`/`[catalyst-gpu]` extras), and the `pathway` kind's glue is bundled in-tree at `src/precis_pathway/` (mirrors `src/precis_bio/`), not a plugin shipped from the catpath repo. Sibling quest `161910` shares the Pd/NO→NH₃ theme. Dormant quests auto-cool after stale ticks — not a hard block.
- **NOx to Ammonia** ("nox2nh3") — the DFT/operando-MS design-loop manuscript project; the write-up, distinct from quest `164903`'s ongoing autonomous-discovery loop. → todo `td34571` (`projects/nox2nh3_auto`).
- **nanotrans_auto** ("Nano-transistors") — → todo `td6649` (`projects/nanotrans_auto`).
- **nanotrans2** — survey of the state of carbon-nanoribbon transistors. → todo `td44368` (`projects/nanotrans2`).
- **dftmodelmcp** — an MCP for DFT modeling. → todo `td44759` (`projects/dftmodelmcp`).
- **mofs-for-electrodes** — → todo `td48056` (`projects/mofs-for-electrodes`).
- **gold-sea** / **gold-sea-2** — gold recovery from seawater. → todo `td43250` / `td43578` (`projects/gold-sea` / `projects/gold-sea-2`).
- **mechacard** — mechanical-cartridge project. → todo `td55666` (`projects/mechacard`).
- **dream-review** — → todo `td48091` (`projects/dream-review`).
- **screwholder** — patent application. → todo `td41686` (`projects/screwholder`).
- **workshop260624-ai** — Bernal Generative AI workshop notes. → todo `td41729` (`projects/workshop260624-ai`).

Other named quests (id → one-line gloss; query `get(kind='quest', id=…)` for
current `STATUS`):

- `161906` — "a world that runs light on the planet"; the umbrella quest, served by 161907/161908/161910.
- `161907` — self-assembling, atomically-precise compute substrate (DNA-tile / molecular computing angle).
- `161908` — "structures lighter than air" (ultralight aerostructures).
- `161909` — grow atomically-precise structure — switches, boxels, tilings (DNA-tile self-assembly yield research).
- `169855` — keep scientific-literature integrity practices at the frontier; citation/claim-grounding meta-quest, relates to the ROLE3:own filter.
- `169953` — "don't let precis get bamboozled by a bad paper"; evidence-grounding meta-quest, same ROLE3 relation.
