# Tool-friction reflection + dream diversification

Status (2026-07-04): **Part A built (default-OFF)** in
`utils/friction_reflect.py`; **Part B lens seed built** in
`utils/dream_seed.py` + `workers/dream_agent.py`. Mode rotation,
gripe→agentlog linking, prod-enable, and active-build dreams are filed
residuals (see `OPEN-ITEMS.md`). Two related idle-time self-improvement
moves, discussed together because they share a host
(`utils/claude_agent.py`) and a substrate (`kind='gripe'` +
`kind='memory'`).

- **Part A — end-of-run tool-friction reflection.** Every eligible
  agentic run, at the end, reflects on whether the MCP surface fought
  it and files one grounded `gripe` if so. Turns *soft* friction (the
  tool succeeded but was clumsy) into a tracked work item — the signal
  that never throws an `[error:*]` and so is invisible to transcript
  mining.
- **Part B — dream diversification.** Generalize `dream-prompt.md`
  from a single narrow mode (cross-kind connection-finding) to a
  rotating set of modes + lenses, and (later) let dreams *act* on the
  compute lanes (DFT / CAD).

## Why

Two facts from the current surface:

1. **The model does not reliably self-report friction.** `/whattodo`
   step 5 exists precisely because it doesn't — it mines `[error:*]`
   out of `refs.meta.transcript` after the fact (702 errors / 48h on
   the first prod run, none of which became gripes). But that only
   catches *hard* friction (errors). The valuable friction is the tool
   that **succeeded and was still wrong**: no verb for what you wanted,
   N calls where one should have worked, a result shape that forced a
   re-query, a surprising argument. None of that errors, so nothing
   sees it unless the agent is asked to look back.
2. **Gripes have no programmatic raise path.** Unlike alerts
   (`precis.alerts.raise_alert`, fingerprint-deduped, machine-raised),
   a gripe is only ever born from an LLM calling `put(kind='gripe')`
   (`handlers/gripe.py`). So the natural place to capture lived
   friction is *inside the agent that felt it*, at the end of its run.

Dreams are the same dogfooding idea's first host, but too narrow: every
dream shares one intent (find a connection), so they all press on the
same ~5 verbs. The diversity of *intent* across agent types
(`plan_tick` wants scheduling, `cad_propose` wants geometry ops, the
web follow-up wants fast interactive retrieval) is what gives broad,
honest tool-surface coverage — which is why Part A lives at the shared
chokepoint, not only in dreams.

## Part A — end-of-run tool-friction reflection

### Mechanism

- **Injection point.** `utils/claude_agent.py` — the unified `claude
  -p` agentic dispatch shared by the reviewers, `dream_agent`, and the
  web follow-up path. Append the reflection to `--append-system-prompt`
  on the eligible run only (keeps the cached base prompt intact for
  ineligible runs; same reasoning as the per-project brief living in
  the variable layer).
- **Eligibility gate (call-time flags).** Only inject when the run is
  agentic + has MCP `put` access + carries no rigid output schema.
  This **excludes the one-shot JSON judges** (`utils/claude_p.py`),
  which would be corrupted by a free-form gripe ask. Skip on
  tight-budget / low-`--max-turns` runs.
- **Every eligible run, binary-first.** *Not* a 10% sample. The
  sampling coin-flip was a proxy for "can't afford to ask every time";
  a cheap binary-first ask dissolves the proxy — the model
  self-samples on *real* friction instead of chance, so you never miss
  a genuine gap and a clean run costs one word. Phrase so **"none" is
  the honored default** (kills the confabulation pressure of always
  being asked). It's terminal — the task is already done, so it can't
  derail the work — and "do not run more tools to investigate" bounds
  it to one reflective message.
- **General reflection slot.** There is no existing generic end-of-run
  question today (grep for reflect / would-you / anything-missing is
  empty; the closest is the dream's own Step 7 self-review). So make
  this **the** general end-of-run reflection footer, with the
  tool-friction question as its first tenant; later reflections (did
  you finish, confidence, anything the operator should know) live in
  the same slot.

Draft footer text:

> Before you finish: did any tool actually get in your way — a call
> you couldn't make, N calls where one should've worked, a result
> shape you had to re-query, an argument that surprised you? If not,
> say "none" and stop. If yes: check the relevant `precis-*-help`
> skill (does the verb already exist?), then file **one** gripe naming
> the ideal call you wanted and citing the calls you actually had to
> make. Describe the *tool* interaction, not the task content. Don't
> run more tools to investigate — reflect from what you already did.

### What it produces

- One `put(kind='gripe', ...)` per genuinely-fumbled run, tagged
  `friction` so triage can batch them (no new *status* — the
  auto-working branch's human spec gate already protects against a
  speculative wish triggering a bad autonomous fix, so no holding pen
  is needed).
- The gripe **`link`s to the run's `agentlog` ref** — which already
  carries **model + prompt + a transcript pointer** in `meta`
  (`agentlog.py`, `RETENTION_DAYS = 30`). That is the "filing model +
  transcript" record for free; nothing is inlined. The 30-day window
  is a natural forcing function: triage the friction gripe within a
  month or the deep-debug trail ages out (the gripe text survives).
  - **Wiring check:** confirm every *eligible* run emits an agentlog
    to link to. If some agentic paths (e.g. the web follow-up) skip
    it, that's a small wiring item.

### Discipline

- **Don't make the filer diagnose.** Capability-gap vs discovery-gap
  (verb exists, skill didn't surface it) vs intentional-absence
  (append-only body chunks, no raw-SQL verb, SSRF-blocked fetch) all
  *feel* identical from inside and route to different fixes (build /
  better skill / wontfix). The filer only reports "I wanted X and
  couldn't get there"; **root-cause is a fixing-side job**, often just
  a better skill. The check-skill-first gate collapses most false
  "capability" gripes into no-gripe or a correctly-flavored
  discoverability one.
- **Ground on the real call sequence.** A friction gripe carrying a
  proposed signature + the actual calls is verifiable; a free
  "search is annoying" is the nursery-digest flood in a gripe costume.
- **Weak-model rescue.** Weak models (`plan_tick` runs on haiku) hit
  the most friction and articulate it worst. The **transcript is how a
  weak model asks for help without being able to** — the fixing side
  reads the actual call sequence and root-causes it *for* the model
  that couldn't. So the filer's job is to raise a hand and leave
  evidence, not to write a good bug report; filing-model + transcript
  + "I got stuck" is a complete signal even from haiku.

### Downstream (out of scope here — on the auto-working branch)

- An **LLM triage pass groups + categorizes** the `friction` gripes
  (semantic clustering; the gripe body is embedded, so similarity is
  in-substrate). This replaces file-time fingerprint dedup — the filer
  just files.
- Root-cause → often a skill edit; the human spec gate precedes any
  autonomous code change.
- **Pairs with** the tool-call ledger backlog item (objective friction
  — error counts, round-trip patterns, mined at the dispatch
  chokepoint): the ledger says *that* something was awkward, the
  reflection says *what would have been better*. Cross-corroboration,
  but the ledger is not a dependency.

### Cost & governor

Per-run cost is one word when clean; the binary-first structure makes
"every time" affordable. The governor on volume is **dedup, not a
token cap** — the rough-edge set is finite, so once a gap is filed,
refiling collapses in triage and filings self-taper as the surface
smooths. Flat coverage (every eligible run) naturally weights the
most-used surfaces, which is the right bias — fix what's used most.
Later: a stratified floor to hear more from rare agent types.

## Part B — dream diversification

Today `dream-prompt.md` hardcodes one deliverable: ≥2 memories each
linking ≥2 kinds with ≥1 fact-bearing leg (cross-kind connection). Good
but monotone. Keep the front-end (diverse cross-kind sample) and the
≥1-concrete-artifact floor; vary the rest via seeds passed in the
prompt's **variable layer** (no new tables), chosen per cycle by a
rotating seed the worker (`workers/dream_agent.py`) supplies.

- **Mode seed (biggest lever).** Rotate the cycle's deliverable:
  *connection* (today) / *library-gap* (missing papers, un-tagged
  clusters, two held papers that contradict) / *open-question*
  (surface a question the corpus implies; seed a research todo) /
  *consolidation* (merge redundant memories, re-check an old hunch
  against new arrivals) / *analogy-transfer* (map a mechanism from
  domain A onto B).
- **Lens seed (nearly free).** Stamp each dream with a named
  stance to break the monotone "I notice X↔Y" register. The lenses
  live in **`src/precis/data/dream_lenses.yaml`** (the `data/axes/`
  convention — versioned, prompt-bearing data the worker loads and
  injects into the variable layer). Two shapes:
  - **persona** — a single stance held for the whole cycle: Feynman
    (first-principles rebuilding), Napoleon (terrain-first), Churchill
    (rhetorical architecture), Newton (obsessive formalism), Einstein
    (Gedankenexperiment / visual thought), Archimedes (physical insight
    → proof, bounding from both sides), Galileo (experiment as arbiter),
    Shannon (radical simplification / analogy-transfer).
  - **process** — a sequential multi-phase pass within one cycle:
    Disney (Dreamer → Realist → Critic), which self-contains a
    produce-then-verify loop. New lenses are added by dropping an entry
    in the yaml; no code change.
- **Sample seed (follow-up).** Today the seed is salience×staleness
  only. Occasionally force a *kind mix* (patent-heavy, structure-heavy),
  an under-dreamt cluster, or an old-vs-new time window, so the raw
  material varies, not just the framing.

Ship **mode + lens** first (two variables), sample-seed next.

### Remove the dream's own gripe hook

`dream_agent` runs *through* `claude_agent.py`, so once Part A lands it
gets the end-of-run friction reflection like any other agent. **Remove
Step 6c** from `dream-prompt.md` so there is one friction mechanism,
not two overlapping ones.

### Future — active dreams (DFT / CAD / compute lanes)

**Deferred; we want this, not yet.** Today dreams are read-heavy: they
sample, read, connect, and at most mint paper stubs (Step 6b). A richer
dream would *act* on the build substrates during idle time — kick a
derived-lane job (DFT relax on the GPU node, `cad_propose`, structure
relax, a route/compile) on a subject its wandering surfaced, then
connect the *result* back into a memory. This exercises the compute
lanes (and, via Part A, surfaces their tool-surface friction too) and
turns idle time into speculative build progress, not just speculative
notes. Design it as another mode seed (`active-build`) with strict
cost/idempotency gating — derived jobs are content-addressed and
cache-fillable (ADR 0044), so a wandering dream that requests a relax
already held is a cheap cache hit. Gate behind load ceiling + a budget
cap so idle dreaming can't starve real compute.

## Open / deferred

- Stratified sampling floor for rare agent types (Part A) — start flat.
- Success metric (filed → built | wontfix | dup ratios) to tune
  coverage — defer; gauge junk-rate once running.
- Chunk-handle supersede redirect, unrelated (see OPEN-ITEMS).

## Cross-refs

- `utils/claude_agent.py` (chokepoint), `utils/claude_p.py` (excluded
  judges), `agentlog.py` (30-day transcript home),
  `handlers/gripe.py` (no programmatic raise — this is the surrogate),
  `workers/dream_agent.py` + `data/prompts/dream-prompt.md` (Part B),
  `workers/job_types/fix_gripe.py` (downstream consumer).
- `docs/design/dreaming.md`, `docs/design/dream-agent-loop.md` (prior
  dream design).
- Backlog: tool-call ledger (objective friction sibling); LLM-confusion
  mining (`/whattodo` step 5, the hard-friction cousin).
</content>
</invoke>
