---
status: draft
title: Per-operation model routing — DB-backed, UI-editable, defaults visible
model: opus
---

# Per-operation model routing — DB-backed, UI-editable, defaults visible

> **Phases 1 + 2 landed.** Phase 1: router resolution layer +
> `utils/llm/operations.py` registry + `live_config.op_override` + `precis llm op`
> CLI + cast-pin migration (ships dark). Phase 2: the editable
> `/status?tab=services` ops panel — one row per operation (union of registry +
> observed `llm_call_log.source`, last-run desc), steerable ops get a
> `default/frontier/big/medium/small/pinned` control + model picker (`POST
> /factory/llm/op`, blank/`default` clears), excluded/unregistered ops render
> read-only with their reason; `effective` is steerable-gated so a stale row on a
> demoted op never shows as live. **Still open:** AC6's full `source=`-literal
> drift scan (flag a code source with neither a registry entry nor an excluded
> marker) — the registry/excluded disjoint + well-formed guard and the
> classify-not-clobbered precedence guard ship, but not the whole-codebase scan.
> Named follow-up: route `fix_gripe` through `dispatch()` (closes its
> read-only-op exclusion + `glm-fleet-flip-safety.md` Part 3).

## Motivation / why

Per-operation model choice is scattered and invisible. Which model an LLM
call uses is fixed in one of three places today: the tier default
(`resolve_model`, e.g. `FRONTIER → claude-opus-4-8`), a per-call-site `model=`
argument (e.g. the cast compose sites, which this session pinned to
`model=os.environ.get("PRECIS_..._MODEL") or "claude-sonnet-5"` in
`reading/briefing_cast.py` / `reading/meditation.py` — an uncommitted change,
the motivating example), or a `PRECIS_*_MODEL` env var read at that arg. None
of these is visible or tunable at runtime: to see "which operation runs on
what", or to move one operation to a cheaper/different model, you edit code
and redeploy — or edit a plist env and bounce a daemon (and per the deploy
`kickstart -k` gap, the bounce may not even reload it).

We already centralized the two *coarser* routing layers in
`/status?tab=services`: per-**tier** placement chains (`llm.chain.<tier>`,
ADR 0066) and per-**service** prio/model (`service_config`). The missing
rung is per-**operation** — the individual LLM call, identified by its
`source=` tag (`reading_brief`, `meditation`, `dream`, `review:deep_review`,
`card_forge`, `figure`, `classify`, …). This proposal adds a DB-backed,
UI-editable override for that rung, with each operation's code default always
visible and a one-click revert, and the operation list driven by what the
fleet actually runs (route-log observed, most-recent first).

**Two hard limits the `ready` gate surfaced, designed around below (not
wished away):** (a) some operations don't route through `dispatch()` at all —
`fix_gripe` calls `resolve_model` + a raw `claude -p` subprocess (the
un-forked site of `glm-fleet-flip-safety.md` Part 3) — so a `dispatch()`
layer structurally can't steer them; (b) some `source=` sites pin `req.model`
for *correctness*, not as a default — `classify`/`classify_topics` pin
`"summarizer"` to hit the local-serving alias (`glm-fleet-flip-safety.md`
Part 1) — so a blanket "override beats the pin" would silently reopen a fixed
bug. The design's answer: the registry is an **opt-in allow-list of routed,
safe-to-steer operations**; every *other* observed operation is still shown
(visibility), but **read-only, with the reason**.

## In scope

1. **Router resolution layer** keyed on `req.source`, slotted into
   `dispatch()` just before `model = req.model or resolve_model(...)`
   (`router.py:1255`). An `llm.op.<source>` override may (a) remap the
   effective tier (`{"tier":"big"}`) or (b) pin a model
   (`{"model":"claude-sonnet-5"}`), or both. A model pin keeps the
   operation's tier for transport + breaker (today's cast semantics:
   `FRONTIER` band, Sonnet model).

2. **A declared operation registry** (`utils/llm/operations.py`) —
   **an opt-in allow-list**: `LLM_OPERATIONS: dict[source, OpDefault(tier,
   model|None, label, description)]`, the single source of each *steerable*
   operation's default + human label/description (the "defaults visible"
   surface). A `source` is in the registry only if it (a) routes through
   `dispatch()` and (b) carries no *functional* `req.model` pin. The cast
   Sonnet default **migrates here** (its `model=` arg dropped), so that
   default lives in one place. Operations that bypass `dispatch()`
   (`fix_gripe`) or pin `req.model` functionally (`classify`/`classify_topics`
   → `"summarizer"`) are **deliberately excluded** from the allow-list — the
   override layer never touches them — and are surfaced read-only (item 4).
   Dropping the cast `model=` arg removes its `PRECIS_*_MODEL` env hatch; the
   DB override + registry default is its intentional replacement (a strictly
   better hatch: runtime, no redeploy).

3. **DB override reader/writer** — `live_config.op_override(source)` (cached,
   mirroring `chain_override`), `op_key(source)`; writes/clears via
   `budget_settings.set_setting/clear_setting` + `live_config.bust_cache()`.
   Storage is the existing `app_settings` KV (`llm.op.<source>` → JSON), no
   migration — consistent with `llm.chain.*` / the cloud dial.

4. **UI panel folded into `/status?tab=services`** (alongside the chain
   editor). One row per operation, the row set being
   **union(registry keys, observed `llm_call_log.source`)** sorted by
   **last-run desc** so "what we run recently" is top-of-page. Each row shows:
   label + description, the **default** inline (`default: frontier /
   claude-opus-4-8`), the current effective choice, **last-seen model**,
   **last-run (ago)**, and **7-day call count** — all from `llm_call_log`.
   For a **registry (steerable)** operation the control is a `<select>`
   `{frontier · big · medium · small · Pinned…}`; choosing *Pinned* reveals a
   model `<select>` populated from the existing `llm` catalog (`_llm_models`),
   and a blank/absent value = revert to default. An **observed-but-not-in-the-
   registry** operation renders **read-only** with a one-line reason
   (`bypasses the router` / `model pinned in code for correctness`) and no
   control. A single `POST /factory/llm/op` endpoint writes-or-clears (blank
   clears), then `bust_cache()` + redirect — matching the existing
   `POST /llm/chain` / `POST /llm/cloud` one-endpoint-blank-clears pattern
   (`factory.py:482`/`511`), not two endpoints.

5. **Source taxonomy curation** — the registry curates/normalizes the raw
   `source=` strings (dedupe `brief`/`briefing`/`reading_brief`; label
   `classify` vs `classify_topics`; drop noise like `unknown`), with a
   **mouseover note on any row whose default or grouping took real judgment**.

## Explicitly NOT in scope

- **The per-tier chain override and cloud dial are untouched** — this
  composes with them (an `llm.chain.<tier>` rung still captures its tier).
- **No new model catalog** — the pin picker reuses the existing `llm`-kind
  catalog ids.
- **No per-host operation scoping** — operations are fleet-wide; host scoping
  stays a `service_config` concern.
- **Not retiring `service_config.model_pref`** — a follow-up once we confirm
  whether it reaches the router at all (grep suggests it currently does not).
- **No auto-tuning / cost optimizer** — this is manual operator control, not a
  policy that *picks* models.
- **No hard coherence enforcement** — a model pin incoherent with its tier's
  transport (an OSS id on `claude_agent`) gets a UI **warning**, not a block —
  same operator-responsibility contract as the chain editor.

## Acceptance criteria

1. **Ships dark.** With no `app_settings` `llm.op.*` rows and the registry
   mirroring today's code defaults, `dispatch()` resolves byte-identically to
   pre-change for every known `source` (golden test enumerating the sources).
2. **Model pin works.** `llm.op.reading_brief = {"model":"claude-sonnet-5"}`
   → a `FRONTIER` cast call resolves to `claude-sonnet-5` on `claude_agent`;
   clearing the row reverts to the registry default. (unit test)
3. **Tier remap works.** `llm.op.<x> = {"tier":"big"}` → that op resolves
   through the BIG chain (`z-ai/glm-5.2` today). (unit test)
4. **Precedence, each rung asserted — and scoped to the allow-list:** for a
   registered source, `llm.chain.<tier>` rung model > `llm.op.<source>` model
   > registry default > `resolve_model`. For an **unregistered** source the
   op-layer is a no-op: a call-site's functional `req.model` (e.g. `classify`
   → `"summarizer"`) is passed through untouched even if an `llm.op.<that>`
   row somehow exists. (unit tests, incl. a classify-not-clobbered case)
5. **UI.** `/status?tab=services` renders one row per operation over
   union(registry, observed sources), sorted last-run desc, each showing
   label · default · effective · last-seen model · last-run ago · 7-day count;
   the picker writes/clears an override and it takes effect with no redeploy
   (cache bust). (route/render test + manual)
6. **Drift guard.** A test fails if a `source=` literal in the code has
   neither a registry entry nor an explicit excluded-with-reason marker; and
   it fails if a *registered* source's call-site also passes a functional
   `req.model` (the two must not conflict — guards the classify class).
7. **Cast pins centralized.** The `model=` arg is gone from
   `briefing_cast.py` / `meditation.py`; their model comes from the registry
   default, and the DB override replaces the retired `PRECIS_*_MODEL` env
   hatch (behavior covered by criteria 1–2).
8. **Non-steerable ops are visible but inert.** An operation that bypasses
   `dispatch()` (`fix_gripe`) or carries a functional pin (`classify`) appears
   in the panel **read-only with its reason**, and setting an `llm.op.<it>`
   row has no effect on its model (asserts blockers 2 + 3 don't regress).

## Target + blast radius

- `src/precis/utils/llm/router.py::dispatch` — the new resolution layer +
  precedence.
- **new** `src/precis/utils/llm/operations.py` — `OpDefault`, the registry,
  the allow-list membership test, the resolve helper, and the audited list of
  **excluded** sources (router-bypassers + functional pins) with reasons.
- `src/precis/workers/job_types/fix_gripe.py` — **audit only, not modified**
  in this proposal: it calls `resolve_model` + raw `claude -p`, so it's an
  excluded (read-only) op. Routing it through `dispatch()` is a named
  follow-up (it would also close `glm-fleet-flip-safety.md` Part 3's gap).
- `src/precis/utils/llm/live_config.py` — `op_override`, `op_key`, cache.
- `src/precis/reading/briefing_cast.py`, `.../meditation.py` — drop the
  hard-coded `model=`.
- `src/precis_web/routes/factory.py` — `POST /factory/llm/op` + clear;
  `src/precis_web/routes/status.py::_services_ctx` — panel context (registry ⨝
  `app_settings` ⨝ `llm_call_log`); a template partial under
  `src/precis_web/templates/`.
- tests: `tests/test_router*.py` (resolution/precedence), **new**
  `tests/test_llm_operations.py` (registry + drift guard + classify-not-
  clobbered), a web route test.
- `llm_call_log` read path — the panel's `GROUP BY source` last-run/7-day
  query. Note: migration `0078_drop_dead_indexes.sql` dropped the
  `(source, ts)` composite; a live join is fine at today's table size, but if
  the query lags, re-add a partial index rather than pre-aggregating.
- docs: `docs/architecture/state-map.md` (LLM-routing subsystem), an ADR 0066
  cross-reference note.

## Open questions / decisions log

**Decided (this session):**
- **Tier labels** — standard `frontier / big / medium / small` (the raw tier
  names), not friendly high/med/low aliases.
- **Source curation** — curate/normalize the `source` taxonomy at the
  implementer's discretion; leave a **mouseover note** on any row whose
  default or grouping took real judgment.
- **Operation list** — driven by what the fleet actually runs: **union of the
  registry and observed `llm_call_log.source`**, with a **last-run** timestamp
  per operation (`max(llm_call_log.ts)`), sorted **most-recent first**.
  Unregistered-but-observed operations still appear (generic label) and are
  still editable.
- **Placement** — folded into `/status?tab=services` alongside the existing
  chain editor, not a separate tab.

**Open (implementer latitude, non-blocking):**
- Exact override JSON shape — `{tier}` | `{model}` | `{tier, model}`
  (proposed as written above).
- Coherence handling — soft UI warning vs hard block (proposed: soft warning).
- Whether the "last-run / last-seen model / volume" enrichment is a live join
  per render or a small cached rollup (proposed: live join, it's a bounded
  `GROUP BY source`).

**`ready`-gate findings (ADR 0048, verified against the code as of this
session):**

- **blocker** — Motivation's "cast Sonnet pin in `reading/briefing_cast.py` /
  `reading/meditation.py`" is factually wrong. Both call sites are
  `tier=Tier.FRONTIER` with `model=os.environ.get("PRECIS_..._MODEL") or
  None` (`briefing_cast.py:804-811`, `meditation.py:393-400`) — there is no
  hard-coded model literal there today, and `Tier.FRONTIER`'s compiled
  default is `claude-opus-4-8`, not Sonnet (`router.py:214`, `_TIER_MODEL`).
  `git show 5f1d9cb3` confirms this call site was already swept onto
  capability tiers 5 days ago with the same env-or-None shape. Acceptance
  criterion 7 ("cast pins centralized... `model=` arg is gone from
  `briefing_cast.py`/`meditation.py`") is built on this wrong premise, and
  doesn't note that dropping `model=` there also removes the existing
  `PRECIS_READING_BRIEF_MODEL`/`PRECIS_MEDITATION_MODEL` env-override escape
  hatches — no replacement for that capability is described.
- **blocker** — `fix_gripe` is listed in the Motivation as one of the
  covered `source=` operations, but `workers/job_types/fix_gripe.py` never
  calls `dispatch()` at all: it calls `resolve_model(Tier.FRONTIER)` directly
  and spawns a raw `claude -p` subprocess (the same "one truly un-forked
  site" already documented in `docs/proposals/glm-fleet-flip-safety.md` Part
  3). Item 1's resolution layer, injected inside `dispatch()`, structurally
  cannot reach this call site — an `llm.op.fix_gripe` override set via the
  UI would be a silent no-op. No acceptance criterion catches this, and
  `fix_gripe.py` is absent from Target + blast radius.
- **blocker** — Acceptance criterion 4's precedence (`llm.op.<source>` model
  beats `req.model`) is asserted globally, but at least one registered
  source already pins `req.model` at the call site for functional (not
  default-picking) reasons: `cli/classify.py`'s `source="classify"` /
  `"classify_topics"` clients pin `model="summarizer"` specifically to hit
  the local-serving alias (`router.py`'s `_hosted_small_remap`/
  `_LOCAL_ONLY_MODEL_ALIASES`, built in `glm-fleet-flip-safety.md` Part 1 to
  fix exactly this class of bug). Under the stated precedence, an operator
  UI override on `llm.op.classify` would silently override that pin and can
  reopen the empty-response failure Part 1 fixed. The spec only names
  `briefing_cast.py`/`meditation.py` as migrated call sites (items 2/7);
  it doesn't distinguish "registry-owned default" call sites from
  "functional model pins that happen to also carry a `source=`" for
  precedence purposes, and the drift guard (AC6) doesn't test this
  interaction either.
- advisory — item 4's "New endpoints `POST /factory/llm/op` (write) and its
  clear... the established factory pattern" doesn't match the actual
  established pattern: both existing dials (`POST /llm/chain`, `POST
  /llm/cloud`, `factory.py:482`/`511`) are a single endpoint where a
  blank/false value clears the override, not two endpoints.
- advisory — migration `0078_drop_dead_indexes.sql` dropped
  `llm_call_log_source_ts_idx` (the `(source, ts)` composite) as unused
  5 days ago; the proposed UI panel's "last-run desc" / "7-day count" query
  is exactly the `GROUP BY source` shape that index would have served. Not
  necessarily wrong to do a live join without it at today's table size, but
  the spec doesn't acknowledge the index was just removed.
- advisory — the Motivation's illustrative `source=` list includes
  `deep_review`, but the actual observed value is `review:deep_review`
  (`workers/review.py:218`, `source=f"review:{reviewer.name}"`,
  `deep_review.py:222`'s `name="deep_review"`). Low severity since item 5's
  curation scope already covers exactly this kind of normalization.

**Resolutions (author, post-`ready`):**

- **Blocker 1 — not a defect (verification artifact).** `ready` read the
  committed history (`git show 5f1d9cb3`); this session's *uncommitted*
  working-tree edits pin `model=env or "claude-sonnet-5"` at both cast sites
  (confirmed `git status` = `M`). Motivation corrected to describe the pin
  accurately as this-session/uncommitted, and AC7 now states the DB override
  is the intentional replacement for the retired env hatch.
- **Blocker 2 (fix_gripe bypass) — designed around.** The registry is an
  opt-in allow-list of `dispatch()`-routed ops; bypass sites are excluded and
  shown read-only (In-scope 2 + 4, AC8, blast-radius audit). Routing
  `fix_gripe` through `dispatch()` is a named follow-up.
- **Blocker 3 (functional pins) — designed around.** Unregistered sources are
  never touched by the override layer; `classify`'s `"summarizer"` pin stands
  (AC4 + AC8 assert it; AC6 drift-guards a registered op from carrying a
  conflicting functional pin).
- **Advisories** — single write-or-clear endpoint (In-scope 4); dropped
  `(source, ts)` index acknowledged (blast radius); `review:deep_review`
  corrected in Motivation.
- **Split** — kept as one proposal (UI-editability is the premise), but Phase
  1 (router layer + registry + a CLI setter, ships dark) is landable before
  Phase 2 (the panel), per the plan's phasing.
