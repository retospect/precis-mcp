---
status: ready
title: Quest tick — a meta write path, and stop the catalyst-facet fallback firing on every quest
prio: high
---

# Quest tick — a meta write path, and stop the catalyst-facet fallback firing on every quest

## Motivation / why

Split out of `quest-flavours.md` after a Fable review (2026-09-24) showed
the incident that prompted it has a much smaller cause than a tick
redesign. Both fixes here are independently shippable and neither needs
the body work.

The incident: `qu401863` ("A standing flow of money for open, independent
research") was minted 2026-09-21 active + high. Its loop logged 40
entries over 4 ticks (~$0.13/tick, $0.53 total) with zero deeds, and
accreted nine unrelated papers as `serves` servers — DFT hydrogen storage
on vacancies, black-hole thermodynamics, postbiotics, LRRK2/Parkinson's,
Pd carbonylation. Filed as `gr447337`.

**Cause 1 — the guaranteed-acquisition fallback hardcodes catalysis
facets.** `workers/job_types/quest_tick.py::_fallback_queries` fires
whenever a tick's propose step emitted no `searches` of its own, and
builds its query by appending one of three facets to the quest's title:

```
f"{topic} DFT barrier mechanism",
f"{topic} dopant single-atom-alloy catalyst",
f"{topic} review 2023 2024",
```

So the funding quest searched the corpus for *"A standing flow of money
for open, independent research … DFT barrier mechanism"* and linked what
came back. The papers were not mis-ranked; **the query was catalytic**.
This matters for the fix: a relevance floor alone would have scored those
same catalysis papers highly and linked them anyway. The logbook's 80-char
truncation hides the appended facet, which is why the entries read as an
inexplicable mismatch. `_force_acquire_enabled` defaults ON
(`PRECIS_QUEST_FORCE_ACQUIRE`, default `"true"`) and is **not** gated on
the compute lane, so it fires even on a quest declared reason-only.

**Cause 2 — no operator path to quest meta.** `meta.compute_lane == "off"`
has been the shipped switch since 2026-08-28 and would have stopped most
of this on day one, but nothing can set it: `handlers/quest.py`'s
`quest.edit` is replace-only (`id`/`mode`/`text`; MCP
`edit(kind='quest', meta=…)` → `BadInput`), and `precis quest` has no
set-meta subcommand (tick · weave · review-all · dossier · dossier-dedup ·
gaps · frontier · figure · redispatch · reset-compute · status ·
seed-catalyst · tag-papers · run). The primitive exists and has a
precedent — `store.stamp_ref_meta`, used by
`quest/weave_tick.py::mark_weave_quest` to stamp `meta.quest_body`, and
`quest/allocator.py::_merge_meta` — so this is exposing an existing
primitive, not inventing a mechanism. This is also why §1 of
`quest-compute-lane-runs-on-a-literature-only-quest.md` has stayed open.

## In scope

1. **A meta write path**, allowlisted: `compute_lane`, `quest_body`,
   `rubric_objectives`. Either `quest.edit` grows `meta=` (patch-merge via
   `stamp_ref_meta`) or a `precis quest set` subcommand, or both. An
   unknown key is refused, naming the allowlist.
2. **Gate the fallback on the compute lane.** `_force_acquire_enabled`
   already reads an env var; it must also respect
   `meta.compute_lane == "off"`. A quest declared reason-only should not
   be force-acquiring literature.
3. **Drop the catalysis facets unless the quest declares chemistry.**
   With `meta.reaction_config` present, keep today's three facets
   verbatim. Without it, fall back to the bare topic (or a
   domain-neutral facet set). This is the actual root-cause fix.
4. **A relevance floor in `quest/search.py::run_search_step`.** Both legs
   already compute a score and discard it (`(r, _rank)` in
   `_default_paper_search`, `_score` in the acquiring search); link only
   above a floor and log what was dropped and why. Secondary to item 3,
   but it bounds the damage when a query is merely weak rather than wrong.
5. **The floor must also cover the S2 acquire leg.** `make_acquiring_search`
   passes `context_ref_id=quest_id` to `PaperHandler.acquire`, which links
   `related-to` per acquired DOI *before* `run_search_step` slices to
   `MAX_LINK_PER_QUERY`. A floor applied only in `run_search_step` leaves
   that path unbounded — it is where `qu401863`'s four surviving
   `related-to` papers (pa410522–25) came from.

## Explicitly NOT in scope

- No tick-body or prompt redesign. That is `quest-flavours.md`, which
  this item does not block and is not blocked by.
- No change to `catalyst`/materials tick behaviour beyond items 2–5, and
  none at all for a quest carrying `reaction_config` (its facets stay
  byte-identical).
- Not removing the fallback. A quiet quest should still ask the
  literature something each slice; the bug is *what* it asks.
- No migration. `stamp_ref_meta` writes the existing `meta` JSON.

## Acceptance criteria

1. `edit(kind='quest', id=401863, meta={'compute_lane': 'off'})` (or the
   CLI equivalent) succeeds, and a re-read shows the key. An unknown key
   is refused naming the allowlist.
2. With `compute_lane='off'`, a tick runs no force-acquire fallback and
   links no paper.
3. A quest with no `meta.reaction_config` produces fallback queries
   containing no "DFT" / "dopant" / "single-atom-alloy" / "barrier"
   tokens. A quest **with** `reaction_config` produces today's three
   facets unchanged — assert both in one test.
4. A search whose best hit is below the floor links nothing and logs the
   drop with the score; this holds for the S2 acquire leg as well as
   `run_search_step`.
5. `qu164903` and `qu202467` tick with no behavioural change. `qu202467`
   is the one to watch: it carries **no** `reaction_config`,
   `compute_lane` or `rubric_objectives`, yet has `tick_count: 291`,
   `results_seen: 336`, `ticks_since_experiment: 0` — a live materials
   campaign that any "infer the domain from meta" shortcut will
   misclassify. Item 3 keys on `reaction_config` for *facets only*, which
   is safe here (202467 loses catalysis facets it was never using
   productively); confirm that on a dry-run before shipping.

## Target + blast radius

- `src/precis/workers/job_types/quest_tick.py` — `_fallback_queries`,
  `_force_acquire_enabled`, `_quest_compute_enabled`,
  `COMPUTE_LANE_META_KEY`.
- `src/precis/quest/search.py` — `run_search_step`,
  `_default_paper_search`, `make_acquiring_search`, `MAX_LINK_PER_QUERY`.
- `src/precis/handlers/quest.py` — `quest.edit` signature + the verb
  schema (per the `mcp_verb_kwarg_silent_drop` trap, the guard is
  verb-side: changing the handler alone will not expose `meta=`).
- `src/precis/cli/quest.py` — a `set` subcommand, if that route is taken.
- `src/precis/handlers/paper.py` — the `acquire` context-link path.
- Runtime docs: `precis-quest-help` (meta keys + how to set them).

## Open questions / decisions log

- **Which write route** — verb, CLI, or both? Verb is what an agent can
  reach; CLI is what an operator reaches during an incident. Both is
  cheap.
- **Floor value.** Wants a number from real score distributions, not a
  guess. Log-and-don't-link for one day first, then set it.
- **Domain-neutral facets.** "mechanism", "review 2023 2024" and "recent
  advances" are plausible for a non-chemistry quest; unresolved whether
  the fallback should instead emit the bare topic and nothing else.
- **Done, not open:** the nine junk `serves` links on `qu401863` were
  removed 2026-09-24 (pa207551, pa195225, pa245431, pa1161, pa244046,
  pa1200, pa197928, pa3032, pa232589). Four `related-to` papers from the
  S2 acquire leg (pa410522–25) remain and are topically reasonable; they
  are the evidence for item 5, leave them.
