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
>
> **Hyphenation:** coined multiword terms are unhyphenated as nouns (claim hub, spin loop, failure bubble); hyphenate only attributively (claim-hub node).
>
> **Retired synonyms:** when a term loses a synonym-collapse, note the loser inside the winner's entry as `(legacy: X)` — never a separate line — and only while X is still encounterable (persisted strings, an unrenamed surface). Fully purged ⇒ delete the note; git log is the history.

## Coined terms

- **dark** ("ships / merges dark") — landed on `main` but disabled by default, behind a **dark switch** (the canonical noun: an off-by-default `PRECIS_*` env flag); distinct from **dark factory** (below). → `docs/conventions/dark-switches.md` · `src/precis/cli/worker.py`
- **dark factory** — the lights-out autonomous repo-dev loop (gripe → diagnose → fix → gate → land, no human at the keyboard); unrelated to a slice shipping **dark**. → `src/precis/fixer/__init__.py` · `docs/backlog/dark-factory-arming.md`
- **fixer** — the laptop repo-dev CI scheduler that closes the dark-factory loop; deliberately not riding precis dispatch. → `src/precis/fixer/__init__.py`
- **watch** — a `level:recurring` todo whose `meta.schedule` (cron / `every:`) drives a per-minute spawner. → `src/precis/workers/schedule/worker.py` · skill `precis-recurring-help`
- **doable** — the view of todos available to be picked (open, unblocked, not bubbled); the repo-dev lane's analogue is `pickable` (`fixer/intake.py`). → `src/precis/handlers/_todo_views.py`
- **cadence** — the scheduler's unit of recurring work, claimed by atomic conditional advance on `scheduler_leases` (`Cadence.eligible` is its cheap pre-claim gate); the *other* recurring trigger besides a **watch**. → `src/precis/workers/scheduler.py`
- **rotation** — the 1/N round-robin across strategic roots (by 7-day picks) that chooses the next task. → `src/precis/handlers/_todo_views.py`
- **bubble** (failure-bubble / `child-failed`) — a failed job tags its parent `child-failed:<job_id>`, dropping it from the doable rotation until the owner decides retry / switch / give-up. → `src/precis/handlers/_job_bubble.py`
- **intent lane / compute lane** — the two kinds of job parent (`handlers/job.py`): an intent-lane job hangs off a `todo` (enters rotation, bubbles on failure); a compute-lane job hangs off a build artifact (structure/cad/draft — derived, content-addressed, cache-fillable). → `src/precis/handlers/job.py`
- **derived job** — a compute-lane job (DFT relax / route / compile): idempotent + content-addressed, owned by the artifact, no rotation to enter. → `src/precis/handlers/job.py`
- **minter** (legacy: `dispatch` worker) — the registry-named pass (`precis worker --only minter`) that mints `kind='job'` children under open todos carrying `meta.executor`; the module + `worker_logs` log-line prefix are still named `dispatch` (`registry.py`'s `log_name="dispatch"` bridges the two — `ref_events.source` writes `'minter'`). → `src/precis/workers/dispatch.py` · skill `precis-minter-help`
- **boot epoch / worker generation** — a uuid4 minted once per worker process at startup (`mint_boot_id`), advertised in `host_heartbeat.meta.boot_ids`; stamped onto every job claim as `meta.lease_boot_id` so a later successor can prove a `STATUS:running` row's prior holder was provably replaced (epoch reclaim) rather than waiting out the lease's full expiry. → `src/precis/workers/heartbeat.py::mint_boot_id` · `src/precis/workers/executors/_common.py` (`claim_executor_jobs`)
- **planner coroutine / `plan_tick`** — an `LLM:*`-tagged todo run as a resumable coroutine; each tick is a job that may mint children or yield, and an exhaustion (max-turns / timeout) is resumable, not a failure. → `src/precis/workers/job_types/plan_tick.py` · skill `precis-minter-help`
- **striving** — a `quest`: a perpetual, unachievable aim. Never `done` (`active|dormant|abandoned`). → `src/precis/handlers/quest.py`
- **serves** — the link relation marking a todo/project/artifact as working toward a quest — the DAG above the todo tree. → `src/precis/handlers/quest.py`
- **deed** — a `milestone` logbook entry; the honest unit of quest progress. → `src/precis/quest/logbook.py`
- **tote** — a running total computed as a *query over a dated log*, not a stored counter (quest lifetime cost; the `llm_tote` call rollup). → `src/precis/quest/logbook.py` · `src/precis/llm_catalog.py`
- **logbook** — an append-only, WORM, dated entry stream on a ref (`quest_log`; the gripe body+comment pattern). → `src/precis/quest/logbook.py`
- **dossier** — the living research synthesis a process owns: a `draft` linked `dossier-of`, rewritten in place — the mutable twin of the append-only **logbook**. → `src/precis/quest/dossier.py`
- **momentum** — a quest's recent-progress scalar, an input to quest health alongside the alignment floor; narrated in the morning brief. → `src/precis/handlers/quest.py`
- **reweight / striving weight** — priority flowing down the `serves` DAG into rotation / acquisition / reading (max-agg, decay per hop; active quests only). → `src/precis/quest/reweight.py`
- **frontier** — the Pareto split of a quest's candidate structures over its objective axes. → `src/precis/quest/frontier.py`
- **fidelity ladder** (legacy: tier ladder) — a quest candidate's progressive-fidelity autocatpath ladder, screening → neb → verify; code-driven, capped promotion, no LLM surface. → `src/precis/quest/compute.py` (`promote_tiers`)
- **screening tier** — the ladder's cheapest rung: relax-only, catpath emits no barrier scalar, ranks on thermodynamics alone. → `src/precis/quest/compute.py::_apply_tier_config`
- **verify tier / coadsorbed template** — the ladder's highest-fidelity rung: a full NEB over catpath's `template="coadsorbed"` network, which drops the parking approximation (below). Verify-gates graduation on a ladder-on quest. → `src/precis/quest/graduate.py`
- **parking approximation** — the `neb`-tier network's simplifying assumption (a dissociated fragment "parks" in a reservoir rather than staying co-adsorbed); the `verify` tier removes it. → `catpath` `network.py::build_coadsorbed_ammonia_network`
- **CHE / potential lever** — the computational hydrogen electrode formalism: an applied potential U (vs RHE) shifts each node's energy by `n_H·eU`, closed-form (no extra compute) — `U_L`/`U_opt`/`span_at_U*`/`P_side` are its derived scalars. → `src/precis/quest/compute.py` (the CHE scalar block)
- **frontier tree** — a quest's candidate lineage (`derived-from` parents) rendered as an indented markdown tree, code-regenerated into a pinned dossier chunk every tick. → `src/precis/quest/frontier.py::render_frontier_tree`
- **publish row** — the ONE mutable working row per claim hub in `nanopub_publish`: the frozen approved string + state machine; the append-only signed bytes live in `nanopub_artifacts`. → `src/precis/store/_nanopub_ops.py` · the `precis.nanopub` package docstring
- **hanging claim** — a claim minted with unresolved provenance (`grounding.hanging`), e.g. hearsay grounding rejected while the original-paper hunt runs; mintable, but publish preflight blocks it like withheld edges. → `src/precis/nanopub/gates.py`
- **trusty URI** — a nanopub's content-hash identity (`https://w3id.org/np/RA…`), created by signing; distinct from the claim's text-derived AIDA URI, which survives re-signing. → `src/precis/nanopub/mint.py`
- **withheld edge** — an inbound evidence edge with neither a support verdict (`links.meta['support']`) nor human sign-off (`links.meta['publish_signoff']`); each one blocks the registry POST — no mute button. → `src/precis/nanopub/preflight.py`
- **freeze line** — where a publish state stops being ours to walk back: below it (`candidate`) nothing is frozen, at it (`reviewed`/`signed`) the string/artifact pointer is, above it (`anchored`/`published`) a third party holds the trusty URI. → `src/precis/nanopub/state.py::frozen_rung`
- **demotion** — the publish ladder walked *downward* when evidence turns: a new `contradicts` edge reopens a `reviewed`/`signed` hub to `candidate`; past the anchor it can only alert for a human supersede/retract. The answer to taproot's ratchet. → `src/precis/nanopub/demote.py`
- **trust axis** ("trust tier") — the epistemic axis on `search(kind='finding', trust=…)` — `signed` / `verified` / `disputed` — orthogonal to `status=`, which is the chase lifecycle and says nothing about whether anyone checked the claim. → `src/precis/handlers/finding.py`
- **trust allowlist** — `nanopub_trust_allowlist`: pinned (identity, key-fingerprint) pairs consulted at publish; flat, zero transitivity, and only an *attesting* entry publishes (a bot signature alone publishes nothing). → migration 0129 · `src/precis/nanopub/preflight.py`
- **searchSnip** — the lowercase-ASCII-token locator published beside a source quote; validated unique-within-paper at mint, doubles as the PDF deep-link query. → `src/precis/nanopub/snip.py`
- **cast** — a daily audio episode (morning `reading` brief; evening `nidra` meditation) on the produce→narrate→publish spine. → `src/precis/reading/cast_common.py` · skill `precis-audio-help`
- **lane** (brief) — a contributor to a morning-brief cast (system / reading / recall / quest), each degrade-to-empty. The world-news wire is not a lane — `cast_audio` prepends it at narration time instead. → `src/precis/reading/briefing_cast.py`
- **nursery** — the SQL-only, per-minute reviewer that raises health/ops alerts (spin loops, worker health). → `src/precis/workers/nursery.py` · skill `precis-nursery-help`
- **dream** — the autonomous 15-min `dream_agent` pass. → `src/precis/workers/dream_agent.py`
- **spin loop** — a `(ref_id, source)` re-emitting > 200 `ref_events`/24h; the nursery flags it. Usually a *stale deploy*, not a new bug. → `src/precis/workers/nursery.py`
- **stale deploy** — prod running pre-fix code after a merge; the usual cause of a recurring spin loop or alert. Check the deployed sha, not the source.
- **jetsam** — a launchd daemon culled by macOS under RAM pressure; the nursery `worker-restart` alert (`WORKER_RESTART_STORM_1H`) is the in-DB signal. → `src/precis/workers/nursery.py`
- **keystone kind** — a kind that owns a legible IR and rents the heavy kernel only at export (cad/pcb/structure); the LLM traverses a graph, never pixels. → `src/precis/pcb/__init__.py`
- **emits_card / "a card is a vector"** — a per-handler class flag (`emits_card = True`): the kind emits a `card_combined` chunk (ord=-1) so the ref itself embeds + searches. → `src/precis/handlers/quest.py` · `src/precis/store/_chunks_ops.py::upsert_card_combined`
- **handle** — the one kind-agnostic address for any record or chunk: a 2-char type code + the row's decimal id (`pa5`, `pc10`, `td42`). Computed, never stored — a pure function of `(kind, id)`, so no minting or backfill; resolves without a `kind=`. → `src/precis/utils/handle_registry.py`
- **admit** — the pre-flight fit-check that refuses a (context, model) pairing too big for the model's window, with the numbers. → `src/precis/utils/llm/admit.py`
- **reground** — re-attach a claim atom to a real source; the "no source, no atom" rule. → `src/precis/taproot/reground.py`
- **angle spray / dreamable** — `angle=`+`like=` diverse-cone semantic sampling at a target cosine; `view='dreamable'` is the browse built on it. → `src/precis/runtime/angle.py`
- **persona** — the voice/stance prompt a reviewer or producer adopts; review personas (`flow`/`cites`/`structure`) drive the review fanout. → `src/precis/quest/review_fanout.py` · `src/precis/workers/planner_prompt.py::_load_review_persona`
- **digest tag** — the `OPEN` open-tag literal (`digest:structural` / `digest:deep`) `run_review_pass` stamps on a reviewer's digest memory so a later pass can dedup/find it; `Reviewer.digest_tag` (legacy: `tier_tag`, the `tier:*` value). → `src/precis/workers/review.py`
- **seam** — the single designated boundary through which all traffic of a kind flows (outbound HTTP, embeddings, LLM calls); code says "the X seam". → `src/precis/utils/http.py`
- **taproot** — the evidence-grounded claim graph: canonicalize a claim onto one hub node, ground it in many papers as typed graded evidence, resolve citations onto it. Phased build (1–5). → `docs/backlog/taproot.md` · `src/precis/taproot/`
- **claim hub** — a `finding` tagged `TAPROOT:claim`, promoted to *the* node a claim lives on; many papers attach as `establishes`/`corroborates`/`contradicts` evidence edges (taproot Phase 2). → `src/precis/taproot/hub.py` · `src/precis/handlers/finding.py`
- **TAPROOT** — the `finding`-ref discriminator axis: `TAPROOT:claim` (grounded world-claim, a taproot hub) vs `TAPROOT:review` (editorial/manuscript note, excluded from the claim graph). Classifier = `data/axes/taproot.yaml` via `axis_pass`. → `src/precis/data/axes/taproot.yaml`
- **establishes / corroborates** — taproot evidence-edge roles (paper → claim hub): the *originator(s)* that first showed a claim `establishes` it; later papers that cite them `corroborate`. Derived from the citation graph, not hand-set. → `src/precis/taproot/seniority.py` · `docs/backlog/taproot.md` §"Seniority is derived"
- **atom / compound claim hub** — a decomposed sentence's split: each **atom** is an ordinary evidence-bearing claim hub; the **compound** is the un-split bundling sentence, linked `conjunct-of` from each atom (migration 0126), holds no direct evidence, and rolls up worst-of its atoms' trust. → `src/precis/taproot/hub.py::apply_extraction`
- **world-claim vs legal claim** — "claim" is overloaded; always use the unique name. A *world-claim* is a taproot claim (a `TAPROOT:claim` finding — a statement about the world). A *legal claim* is a numbered entry in a patent's claims section (legal scope, not an empirical result; the `patent_claim` chunks with `claim_number` meta). World-claims ground in patent *description* blocks, never legal-claim blocks. → `src/precis/workers/hub_refine.py`
- **worked vs prophetic example** — patent-example evidentiary weight (US convention, tense-of-performance test): a *worked* example narrates an experiment in past tense as actually performed; a *prophetic* example describes it in present/future/modal voice as merely doable. Chunk-level classifier axis (`worked`/`prophetic`/`none`); a prophetic supporting block earns a fixed evidence-edge caveat ("corroborates at best"), never a hard exclusion. → `src/precis/data/axes/patent_example.yaml`
- **patent family / family representative** — family identity is authoritative (DOCDB family ID from EPO OPS), so no hub node: `family_id` in ref meta, a deterministic *representative* (earliest-published ingested member), siblings in the same *simple* family ingested as stub refs linked `same-family-as` (new-matter members — CIP/divisional — get full ingest). Cites render one representative per family. → `src/precis/handlers/_patent_family.py`
- **fisheye** — a degree-of-interest render: focus one node and get it plus its scaled-by-distance surroundings, not a bare chunk (turn-taking design, git-only). `get(..., view='fisheye'/'fisheye+1hop')`. → `src/precis/utils/fisheye.py`, `src/precis/utils/eye_render.py` · skill `precis-fisheye-help`
- **eye / crunch** — an **eye** is a model-manipulated unit of attention in the working set (extent × persistence × provenance); **crunch** is the per-tick decay step (`transient` dies at the next crunch). The fisheye *render* is downstream of an eye, not a synonym. → `src/precis/workers/working_set.py`
- **reference ring** — everything a node points at, one edge out; what `fisheye+1hop` adds over `fisheye`. → `src/precis/utils/refeye.py`
- **fisheye rail / eye-pressure** — the web reader's relevance side rail; smartdraft ranks chunks by *eye-pressure* (how much a chunk wants attention right now), LLM-free. → `src/precis_web/smartdraft.py`
- **cite head** — the status glyph set rendered on an inline claim-hub citation in the web reader (claim / pending / refuted / hypothesis precedence). → `src/precis_web/linkify.py`
- **extent ladder** — the `kwd < summary < verbatim < fisheye < fisheye+1hop` ordinal rungs (each strictly containing the previous) that `view=` picks a point on. → `src/precis/workers/working_set.py::Extent` · skill `precis-fisheye-help`

## Overloaded — which one?

- **chunk** — the atomic content row (`chunks` table): body rows `ord >= 0`, card variants `ord < 0`. (legacy: *block* — only the prompt-assembly `Block` type still uses the word.) → `src/precis/store/_chunks_ops.py` · `docs/codebase.md`
- **retire** — take a row out of service, kept for recovery (`retired_at` column + `Store.retire_ref`, uniform across refs/chunks/cad/pcb and every other table since Stage E). The machine execution unit is a `job`; a `todo` node is never called a "task" — that synonym was fully retired in Stage E. → `src/precis/store/_refs_ops.py`
- **finding / taproot / nanopub(lication)** — one domain, three layers, not synonyms: `finding` is the ref kind (a tracked claim/observation) · **taproot** is the claim graph built on findings (claim hubs + typed evidence edges) · **nanopub** is the publish pipeline that freezes an approved claim hub into a signed *nanopublication* (the external artifact, trusty URI). Each layer's package docstring owns its layer only. → `src/precis/handlers/finding.py` · `src/precis/taproot/__init__.py` · `src/precis/nanopub/__init__.py`

(These also leak into MCP output, so the short version is echoed in the
`precis-overview` skill for runtime agents.)

- **tier** — classifier tiers (0/1/2) · email-scan depth (0/1/2, see `email_scan.depth`) · search tiers (Tier 1 RRF / Tier 2 good-search) · LLM tiers (`tier_floor`, `gate_tier`, opus/sonnet/haiku) · quest fidelity tiers (see **fidelity ladder**) · registry maturity tiers (component/material); the review-digest sense retired to **digest tag** (above, Coined terms). → `src/precis/workers/classify.py` · `src/precis/utils/llm/router.py`
- **card** — an embedding chunk (`card_combined` / `card_glossary`, the searchable vector) · an Anki flashcard (`card_forge` mints these) · a catalog entry (`llm` / `quest` / `concept`). → `src/precis/store/_chunks_ops.py::upsert_card_combined` · `src/precis/handlers/anki.py` · `src/precis/handlers/llm.py`
- **role** — the classifier content axis `role` / `role3` (own/background/furniture) · `corpus_role` (evidence/spec/none — citability) · nanopub evidence role (establishes/corroborates/contradicts) · dossier entry role (support/counter/experiment). Folder placement is `KindSpec.placement` now, not a role. → `src/precis/data/axes/` · `src/precis/protocol.py`
- **lane** — job parent lane (intent vs compute; the canonical sense) · morning-brief lane (prose; the code says *contributor*) · repo-dev/gripe-intake lane (fixer prose). The route_log "lane" is the `llm_call_log.source` caller label, not a lane. → `src/precis/handlers/job.py` · `src/precis/reading/briefing_cast.py` · `src/precis/fixer/intake.py`
- **dispatch** — the seven-verb `Hub` registration/dispatch table (`src/precis/dispatch.py`) · `runtime.dispatch` (in-process MCP verb call). The worker that mints jobs from doable todos is the **minter** now (below); the LLM router's entry point is `route(LlmRequest)`, no longer a dispatch. → `src/precis/dispatch.py` · `src/precis/runtime/dispatch.py` · `src/precis/utils/llm/router.py::route`
- **plan** — the `plan` kind (a thread's reasoning outline) vs `plan_tick` (the planner-coroutine job). → `src/precis/handlers/plan.py` · `src/precis/workers/job_types/plan_tick.py`
- **fetch / chase** — `fetch` / `fetch_oa` (acquire a paper PDF) vs the finding-`chase` verb (resolve an open finding; exponential-backoff). **`chase` is the verb, not the graph** — keep it distinct from **taproot** (the claim graph it feeds) and a **claim hub** (the `fi<id>` node). The verb now spans **three independently-gated passes**, all dark (default-OFF): (1) outbound support-verdict `PRECIS_CHASE_LLM` (`workers/chase.py` · `_chase_llm.py`), (2) inbound corpus sweep `PRECIS_INBOUND_CHASE_ENABLED` (`workers/inbound_chase.py`), (3) the **taproot forward-bridge** `PRECIS_TAPROOT_CHASE_ENABLED` that mints/attaches claim-hub evidence edges (`workers/chase.py` + `taproot/hub.py`; taproot Phase 3 W1, `bb2eb73e`). → `src/precis/workers/{fetch_oa,chase,inbound_chase}.py`
- **source** — an episode's producer tag (`brief` / `meditation` / `news`) · a chunk's provenance (`meta.source`) · the OA fetch backoff arms on `fetcher:%` events · the `SRC:` tag axis (patent primary/secondary) · `source=` the patent-only search param. → `src/precis/audio_feed.py` · `src/precis/store/types.py`
- **ratchet** — taproot's evidence ratchet (trust only walks up; **demotion** is the answer) · the dossier narrative growth-ratchet · the frontier viewport ratchet. → `src/precis/taproot/__init__.py` · `src/precis/quest/narrative_budget.py` · `src/precis/quest/compute.py::_ratchet_frontier_viewport`
- **trust ladder** — the LLM-catalog provenance ladder (observed-telemetry > measured-eval > published) · taproot read-time citation trust; distinct from the nanopub **publish ladder** (the state machine). → `src/precis/llm_eval/__init__.py` · `src/precis/taproot/trust.py`
- **orphan** — structural: a job with no `parent_id` · liveness: a lease whose holder died (reboot-orphan reap; `workers/reaper.py` says **zombie** for the agentlog case). → `src/precis/handlers/job.py` · `src/precis/quest/loop.py`
- **lens** — the oracle/dream lens (wisdom-tradition sampling policy; `get(kind='oracle', args={'lens': …})`, `PRECIS_DREAM_LENS`) · auto-lens (an inferred **eye**). Review lenses are **personas** now (`quest/review_fanout.py`; legacy: the web `"lens"` JSON field + CLI `--lenses` flag until the surface stage); citation-graph recall is `citation_recall`. → `src/precis/utils/oracle_lens.py` · `src/precis/workers/working_set.py`
- **watch** — a `level:recurring` todo whose `meta.schedule` drives the per-minute spawner (see **watch** in Coined terms) · unrelated `precis ingest --watch` CLI (`cli/watch.py`; legacy CLI name `precis watch`): a directory watcher that auto-ingests dropped PDFs (`watchdog`-based, no todo/schedule involvement) — `patent_watch` and the `WATCH:` tag axis are separate canonical senses that kept the word. → `src/precis/workers/schedule/worker.py` · `src/precis/cli/watch.py`

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
