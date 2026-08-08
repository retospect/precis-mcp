# precis — current state of the system

> A dark factory for research. precis & asa — and the way we build them.
> One Postgres DB · ~50 kinds · 7 verbs · a 4-node Mac + Linux cluster.
>
> _Slides separated by `---` (Marp / reveal.js compatible). Status tags: `[shipped]` `[live]` `[dark]` `[design]` `[blocked]`._

---

## One machine, pointed two ways

**THE THESIS**

precis ingests literature, builds a queryable substrate, and runs **perpetual agent work** over it on a small cluster. The way we **build** precis is the same factory, pointed at its own codebase.

- **The product — precis:** ingest → substrate → autonomous work over papers, patents, drafts, quests. Agents operate it through 7 verbs; it never tires.
- **The method — building it:** worktrees → container gate → `/go` → deploy. Dozens of autonomous Claude Code sessions in parallel, shipping to the same main.

> ◆ The same five patterns run on both sides — **tiered LLM routing · perpetual autonomous work · self-monitoring · grounded knowledge · forward-only discipline**. The deck makes you see them twice.

---

# PART A — The product

precis + asa: what it is, how it sees, talks, thinks, finds, writes, and what runs it.

---

## One database. Two tables. ~50 kinds.

**PRODUCT · THE SUBSTRATE**

precis is an MCP server — `precis serve` exposes 7 verbs over ~50 content kinds; cluster agents operate the product entirely through those verbs.

- **refs.** One row per thing, discriminated by `kind`: `todo`, `paper`, `patent`, `draft`, `quest`, `llm`, `gripe`, `concept`… A kind is a handler, not a table.
- **chunks.** The body text, append-only. Body rows (`ord >= 0`) are never mutated in place — the single most load-bearing invariant.
- **Derived data cascades** off chunks: `chunk_embeddings` (bge-m3, NULL at ingest, worker-filled), `chunk_summaries`, `chunks.keywords` (KeyBERT). To "update" text you DELETE + INSERT so the cascade re-runs.
- **Relations** are typed links (the `link` verb) — not foreign-key columns: `cites` / `serves` / `parent` / `dossier-of`.
- **Schema evolves forward-only:** a new migration, never edit a sealed file.
- **The 7 verbs:** `get` · `search` · `put` · `edit` · `delete` · `tag` · `link`

> ◆ The whole thesis on one slide — everything downstream is a kind, a chunk, or a link.

---

## The front door — and the honesty layer

**PRODUCT · INGEST & GROUNDING**

**Ingest**
- `marker` PDF → chunk → embed (bge-m3)
- papers, patents, books
- Embeddings filled by the worker, not the ingest path (ADR 0007)

**Categorizers (grounding)**
- Chunk-tag classifier (ADR 0047) — a cascade: regex → local `role3` model → optional escalate (tiers 0/1/2)
- Axes: `role3` (own/background/furniture) and `corpus_role` (evidence/spec/none = citability)
- `ROLE3:own` = the citation-grounding filter (default-OFF)
- KeyBERT discovery layer (F20): per-chunk `chunks.keywords`, powers `view='toc'`

> ◆ "Grounded" is a value, not a feature — citations, citability, and the "don't get bamboozled by a bad paper" meta-quest keep the system honest.

---

## Fisheye — focus, with graduated context

**PRODUCT · HOW IT SEES** · `[live]`

A degree-of-interest render (ADR 0051): a verbatim center, neighbors fading to summary then keyword by distance.

- **Extent ladder:** `kwd` < `summary` < `verbatim` < `fisheye` < `fisheye+1hop` — the +1hop adds the reference ring (cited papers / linked notes).
- **Two axes:** extent (how much) × persistence (decay TTL). A `WorkingSet` = eyes + cursor, snapshotted per tick.
- **Multi-eye:** several foci held at once.
- **Wired default-ON:** the planner prompt, the dream agent (eye-draw), draft / smartdraft, and `plan` (the todo tree).
- **Part of a wider `view=` family:** `paper` → abstract/toc/summaries/bibtex; `todo` → tree/doable/strategic; Drive → the cross-kind unified item view.

---

## Two faces — conversational and cockpit

**PRODUCT · HOW IT TALKS**

**asa — the conversational agent**
- Discord bridge — streaming, with a live progress indicator (`CLOUD_SUPER` tier)
- A new Slack bridge (ADR 0062) `[new]` — forced-sonnet plus a hard per-turn kind allowlist
- Both route through the ADR-0046 `dispatch()` seam

**The web interface**
- Drive — the cross-kind unified item view (one-best-chunk-per-ref, faceted by kind/tag/folder)
- The `/factory` console — live status plus the model-switch button
- Draft / paper / figure editors

> ◆ asa is precis's human face; Drive and /factory are its cockpit.

---

## A factory that runs while you sleep

**PRODUCT · HOW IT THINKS**

Autonomous work rides the **todo tree** — a hierarchical task graph with jobs hanging off it. This is **"the dark factory."**

- **The todo tree (5 slices).** A strategic/tactical gradient, `auto_check` leaves, recurring watches, jobs in two lanes (intent vs compute, ADR 0044), and planner coroutines (`plan_tick`).
- **Derived queue, no blocking jobs** (ADR 0007/0017): workers pick work by SQL. `dispatch` mints jobs from doable todos.
- **Dreaming.** The `dream_agent` is an autonomous 15-minute free-association pass over the corpus (default-ON fisheye eye-draw) `[live]`. New findings and hypotheses fold back into the substrate.
- **Runs on melchior.** The agent worker (`claude_inproc`) — the system thinks between your sessions.

---

## The catalyst quest — a striving that never ends

**PRODUCT · THE FLAGSHIP LOOP**

A **quest** is a "striving": perpetual, unachievable, never done (only active / dormant / abandoned). The catalyst-discovery loop is one instance of a general engine.

- **Each tick:** the LLM edits a `structure` → two compute jobs score it: an ML relax (stability / energy) and `autocatpath` (the reaction barrier).
- Results harvest into the logbook; a generalised Pareto frontier ranks candidates on its objective axes.
- Designs crossing the barrier ceiling graduate to `needs-experiment`.

**Logbook — what happened**
- WORM, append-only, dated, immutable.
- Entry types: `note` / `observation` / `hypothesis` / `result` / `dead-end` / `milestone` / `cost`.
- A milestone is a deed; dead-ends are first-class; `cost=` sums into the tote.

**Dossier — current synthesis**
- A `draft` the quest owns (`dossier-of`), whole-rewritten each tick as rolling context.
- ADR 0064: a `paper` is a render of dossier + frontier snapshot.
- The ruled-out ledger is pinned in `chunk-meta` so a rewrite can't silently drop it.

> ◆ Anti-spin — `progress_factor` decays geometrically on ticks-since-frontier-improve (resets only on external evidence); ADR 0065 adds failed-loop exponential backoff and a nursery `quest-loop-failing` detector.

---

## The loop that grows the corpus

**PRODUCT · HOW IT FINDS MORE**

- **Reconcile passes.** `paper_reconcile` + `corpus_reconcile` continuously heal and extend the corpus.
- **Acquire with backoff.** `fetch` / `fetch_oa` (PDF) plus finding-`chase`, both exponential-backoff. A stub backlog (`view='stubs'`, "papers-needed").
- **Search-as-discovery, 3 tiers.** Tier-1 RRF hybrid `[shipped]` → Tier-2 agentic "good-search" (+ Qwen) `[next]`, with HyDE (hypothetical queries/answers fused).
- **Citation-following / source-backfill.** Pull the reference ring of what you're reading.
- **Quest-pulled acquisition.** The `serves` reweight biases what gets fetched next toward active quests.

---

## Drafts, tex, and figures that know their sources

**PRODUCT · HOW IT WRITES**

**Writing**
- **Chunk-native.** `draft` is an editable document on the same body-chunk substrate as papers (ADR 0033) — it embeds, searches, and exports (`docx` / `pdf`, tex-compile). `tex` is the file-store sibling.
- **Tagging.** Closed UPPERCASE axes — `STATUS:` `PRIO:` `SRC:` `ROLE3:` — replace-within-prefix, plus free-form tags.
- **Linking.** Typed relations via the `link` verb, not FK columns: `cites` / `contradicts` / `serves` / `dossier-of` / `parent` — the knowledge graph.

**Figures — image tracking**
- **`figure` kind (ADR 0057)** `[slice 1]`. An interactive SVG canvas the model draws with you — SVG source (`no_index`), a searchable vocabulary, notes, and a resumable chat log.
- **Image tracking.** Element→chunk binding: each drawable element `depicts` the chunks it depicts (a draft section, a CAD cross-section, a paper passage) — "which figures use this result?" is a graph query, and the model detects/fixes drift when a source changes.
- **Export.** SVG rasterizes on-demand (→PNG via `resvg`) into docx/pdf; degrades to caption-only + warning.
- Draft-embedding is `[deferred]`.

---

## A paragraph that re-opens the moment you edit it

**PRODUCT · REVIEW & READINESS**

One idea ties it together — everything is content-addressed by `content_sha`, so review survives editing.

**Para approval — the unit**
- Every draft chunk (`dc<id>` — paragraph, table, figure) is independently approvable.
- `edit(kind='draft', id=dc.., review=<checker>, verdict=..)` pins the current sha.
- Checkers are an open vocabulary — `human` / `flow` / `cites` / `structure` / `adversarial`.
- A weave bumps the sha → the chunk goes dirty for ALL checkers → reviewers re-run only on dirty chunks.

**Paper readiness — the aggregate**
- The `chunk_review` ledger (migration 0086) = `(chunk_id, checker, approved_sha, verdict)`.
- Export-ready = "sections human-passed at current sha."
- Views: `view='review'` (whole-draft table) and `view='review-diff'` (approved→current diff).

> ◆ Approve a paragraph and it pins the sha it approved; re-weave it and it goes dirty again — review can never silently go stale.

`[shipped]` ledger + verb + views — `[last wire]` auto-clear-on-pass + export-gate enforcement.

---

## A switchable router over a 4-node fleet

**PRODUCT · WHAT RUNS IT**

- **Switchable LLM router (ADR 0046).** Everything goes through `dispatch(LlmRequest)`. Tiers: `opus` / `sonnet` / `haiku`, `CLOUD_SUPER`, `LOCAL_BIG` / `LOCAL_HUGE`, gated by `tier_floor` / `gate_tier`.
- **`admit`.** A pre-flight fit-check that refuses a (context, model) pairing too big for the window, with the numbers.
- **The `llm` catalog kind** (17 cards) — models as first-class searchable refs.
- **Live flip from /factory.** `POST /factory/llm` presets (GLM-5.2 / OpenRouter). Full-fleet flip not yet safe (gripe 171782). `[blocked]`
- **`slullama`.** Route the new `LOCAL_HUGE` tier to a Slurm GPU cluster via an autossh shim + a static `served_by` card as throttle. Built dark, blocked on the HPC login dest. `[dark]`
- **The fleet.** melchior / caspar / balthazar (Mac launchd) + spark (Linux systemd), sharing a NAS. Review tiers watch it all — `nursery` (SQL/min, critical alerts) → `structural` → `deep_review` (opus), with a budget breaker.

---

## Not just papers — the whole workflow

**PRODUCT · THE BREADTH**

The same refs+chunks model absorbs wildly different domains behind the same 7 verbs.

- **Science:** `protein` (AlphaFold3 on spark), `structure` / catalysis (DFT), `pathway` (autocatpath barriers).
- **Engineering:** `cad` / `pcb` (EDA) — "keystone kinds" with an analytic IR; the LLM traverses a graph, never pixels.
- **Literature & IP:** `paper`, `patent` (+ an authoring loop), `citation`, `concept`.
- **Connectors:** `email` (live IMAP, read-only), `edgar`, `orcid`, `news`.
- **Output:** audio cast (morning reading brief + evening nidra meditation), `anki` cards (card-forge spaced repetition).

> ◆ One verb surface, dozens of domains — that's the leverage.

---

# PART B — The mirror

How we build precis *as* a dark factory — the same patterns, pointed at the codebase.

---

## Dozens of sessions, one keystroke to ship

**METHOD · THE SHIP LINE** · `[live]`

- **Worktree-per-session.** `claude -w` gives each session an isolated branch. Many run at once — this machine had 9 in flight.
- **One keystroke.** `/land` (ship) and `/go` (ship + deploy) — commit WIP → sync main → container gate → squash-merge → deploy.
- `scripts/ship` is idempotent — a re-run resumes cleanly.
- **A real race, engineered around.** Concurrent worktrees share one `.git/index`, so ship uses `commit-tree` plumbing + a CAS `--force-with-lease` push to avoid co-mingling staged files.

> ◆ `/go` is the product's dark factory pointed at the repo — from commit to deployed with no human in the middle of the mechanics.

---

## Nothing merges red

**METHOD · GATE & RELEASE**

**The gate + testing**
- The container gate auto-fixes `ruff`, then runs authoritative `ruff` · `format` · `mypy` · `pytest`.
- `scripts/test` — never a bare `pytest` — reproducibility via `uv`.
- `--impacted` runs only the tests your change touches (testmon).
- A container-vs-host split — some extras (`marker` / `torch`) exist only in-container.

**Release / ansible**
- `scripts/deploy` = the `redeploy-precis.yml` ansible playbook — auto-applies pending migrations before web boots.
- Multi-host convergence across Mac `launchd` + Linux `systemd` sharing a NAS.
- **SHA-check.** Verify deployed code via the venv `direct_url.json`, not the checkout cache.
- **Forward-only migrations.** Never edit a sealed `.sql`.

---

## Agent sizing is the LLM router, again

**METHOD · THE MIRROR**

The dev side has the same tiered routing as the product — the **cheapest tier that fits** does the work; the coordinator keeps only judgment.

**Product — the LLM router**
- `dispatch(LlmRequest)` picks `opus` / `sonnet` / `haiku` / `LOCAL` by `tier_floor` and the admit fit-check.

**Dev — agent sizing**
- The Opus main loop coordinates; Sonnet agents (`coder` / `reviewer` / `documenter`), Haiku agents (`navigator` / `extract` / `test-runner`), and script (no-model) agents do the work.

> ◆ Delegating down is the primary cost lever on both sides — same principle, two substrates. (This very deck was assembled by four parallel agents.)

---

## We build precis inside precis

**METHOD · SELF-REFERENCE**

- **Dogfooding, honestly stated.** The dev session's `precis` MCP writes to PROD read-only. The team files `gripe`s, runs `quest`s, and stores `memory` in precis while building precis.
- **Persistent memory.** File-based memory + a `MEMORY.md` index + a reconsolidation cadence — the agent carries context across sessions.
- **Skills are runtime docs.** The same `get(kind='skill')` surface serves the product's agents AND documents the dev workflow — one channel, two audiences.
- **Orientation tooling.** Semantic code search (Milvus) + `coderef` (exact who-calls) + a docs ladder (`codebase.md` → package docstrings → `glossary`).
- `rtk` transparently compresses noisy command output; self-healing hooks inject the in-flight worktree table, auto-reap merged worktrees, and heal the primary branch.

> ◆ The elegant part — skills-as-runtime-docs means one artifact runs the product and teaches the team.

---

## What's live, what's dark, what's next

**CURRENT STATE · HONEST**

The mechanisms are built and mostly shipped. Several are **dark** (merged, gated off) or waiting on one last wire. Nothing here is vapor — each has a decision record.

| Status | Item | Note |
|---|---|---|
| `[blocked]` | slullama — LOCAL_HUGE → Slurm GPU tier | Built dark; blocked on the HPC login destination. |
| `[blocked]` | Full-fleet model flip | GLM-5.2 works per-path; classify + dream still break (gripe 171782). |
| `[dark]` | Paper-readiness auto-enforce (pipeline rung 6f) | Ledger is live; reviewer-PASS auto-clear + export gate are the last wire. |
| `[design]` | Figure draft-embedding + autonomous raster export | Figure slice 2 — unlocks full paper-pipeline integration. |
| `[design]` | OpenRouter fallback cascade (local → OpenRouter → Anthropic) | Agreed design; not yet built. |
| `[next]` | Tier-2 agentic paper search (good-search + Qwen) | Tier-1 RRF hybrid shipped; the agentic tier is the follow-on. |

---

## Content-addressing, all the way down

**THE THROUGH-LINE**

Chunks are append-only. The quest logbook is WORM. A paragraph's approval pins a `content_sha`. A figure binds to the chunks it depicts. Same discipline everywhere — which is why **review survives editing** and the system can run unattended.

- **One substrate:** ~50 kinds, 7 verbs — everything is a ref you `get` `search` `put` `edit` `tag` `link`.
- **Perpetual · grounded · self-watching:** quests strive, dreams synthesize, the nursery watches, citations keep it honest.

> ◆ **Built the way it runs** — worktrees, one-keystroke ship, tiered agents, dogfooded on itself. precis doesn't tire; neither does the line that builds it.
