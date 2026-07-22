# 0060 — Topic dossiers: standing paper→topic classification, quest-family living syntheses, digest cast

- **Status**: proposed (2026-07-22); **classifier slice (§1 taxonomy + cascade)
  implemented 2026-07-22**, default-OFF. As-built:
  `src/precis/data/topics/*.yaml`, `src/precis/workers/classify_topics.py`
  (`run_classify_topics_pass`), registered in `cli/worker.py` /
  `workers/registry.py` (`PRECIS_CLASSIFY_TOPICS_ENABLED` /
  `--only classify_topics`), `tests/test_classify_topics.py`. Tier 2
  (escalate) is not implemented — see open questions. The quest-family
  synthesis tick body, weekly digest cast, and daily-brief lane (§4-5) remain
  unbuilt — tracked in `OPEN-ITEMS.md`.
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0047 — controlled chunk tagging](./0047-controlled-chunk-tagging.md) — reuses
    the cascade pattern (cheap tier-0/1, escalate tier-2, versioned idempotent
    claim-based pass) one level up: papers, not chunks.
  - the `quest` kind (`src/precis/handlers/quest.py`,
    `src/precis/workers/job_types/quest_tick.py`; lifecycle in
    `docs/architecture/state-map.md:697-738`; catalyst-discovery precedent in
    `docs/design/catalyst-discovery-quest.md`) — reuses its coordinator +
    dossier-draft + WORM-log tick loop wholesale, with a new tick body per
    topic-quest family instead of a new mechanism.
  - `src/precis/reading/briefing_cast.py` (morning brief) — lane-union →
    LLM-compose → save as dated `draft` → link sources back — reused for the
    new digest lane/cast.
  - `src/precis/data/skills/precis-paper-tag-axes.md` — the existing open
    `topic:` tag vocabulary on papers is the join key this ADR classifies into;
    no new tag axis is introduced.

## Context

Papers keep arriving into several standing lines of interest — health-span
(anti-inflammation, sleep/circadian, fitness, blood biomarkers including
parabiosis-style effects, data-driven health), molecular electronics
(molecular switches and wires), NOx-reduction catalysis, LLM improvements —
and there is no standing mechanism that (a) routes a newly-ingested paper to
the topic(s) it's relevant to, (b) tracks which relevant papers haven't yet
been folded into that topic's synthesis, or (c) surfaces what changed.

Today NOx catalysis gets this via a hand-built `quest` with its own active
literature-search tick. Every other topic gets nothing: relevant papers sit
in the corpus, findable only if someone happens to search for them, with no
standing document that accumulates the synthesis.

`topic:` tags on papers already exist as an open, freely-coined vocabulary,
but nothing writes them automatically at ingest and nothing consumes them
past ad-hoc search.

## Decision

1. **Classification is a cascade**, structurally identical to ADR 0047's
   chunk cascade, one level up (paper title + abstract, not chunk text):
   tier-0 keyword/regex screen per topic (`data/topics/*.yaml`, mirroring the
   `data/axes/*.yaml` shape), tier-1 cheap local model confirms/expands
   candidates, tier-2 escalates only when tier-1 is unsure. **Tier-1 is
   multi-label** — a paper may get zero, one, or several `topic:` tags.
   Cross-cutting papers are expected (e.g. a MOF paper can be both
   `topic:noxrr` and `topic:healthspan` if it's genuinely relevant to both),
   so the classifier must not force a single pick. A paper matching no topic
   at tier-1 with an above-threshold "novel cluster" signal is queued for
   Reto to review — it is **not** auto-promoted into a new topic.

2. **Two-level taxonomy.** A small, curated, versioned list of **top-level
   topics** (each owns exactly one `quest` + one dossier `draft`), each with
   an **open** set of descriptive sub-tags for internal organization within
   that one draft. Seed list:
   - `healthspan` — rolls up anti-inflammation (incl. the inflammatory
     cascade, rheumatism/rheumatic disease), sleep/circadian, fitness, blood
     biomarkers (incl. parabiosis-style effects), neuroprotection, skin
     repair (incl. sun/UV damage), data-driven health as sub-tags
     (`healthspan-sleep`, `healthspan-fitness`, `healthspan-biomarkers`,
     `healthspan-inflammation`, `healthspan-neuroprotection`,
     `healthspan-skin`, `healthspan-data-driven`, …) inside **one** dossier,
     not one per sub-theme.
   - `molelec` — molecular switches & wires / molecular electronics.
   - `noxrr` — the existing catalyst-discovery quest. This ADR wires the
     classifier to feed it papers passively; it does not replace the quest's
     own active lit-search tick.
   - `llm-improvements`.
   - Open to grow. New top-level topics come from the tier-1 "no fit" queue,
     confirmed by Reto — not auto-minted. This keeps the *top-level* list
     closed even though papers multi-tag freely within it, which is the
     specific failure mode ADR 0047 already measured in this corpus
     (`interest:molecular-computing` vs `topic:molecular-computing` — the
     same concept, split across two folksonomy facets).

3. **"Integrated" is a link, not a new field.**
   `paper --integrated-into--> draft:<topic-dossier>`, written when a
   topic-quest tick folds a paper into its dossier. "Unintegrated papers for
   topic X" is `search(kind='paper', tags=['topic:X'])` minus papers holding
   that link to `X`'s dossier draft — a live view, queryable on demand, not
   only visible at the weekly tick.

4. **One quest per top-level topic**, reusing the coordinator/dossier/WORM-log
   scaffolding as-is, with a **new synthesis tick body**: harvest
   topic-tagged papers lacking the integration link → LLM reads deltas
   against the existing dossier → revises/appends the draft → logs the merge
   in `quest_log` → links the papers. This sits alongside
   catalyst-discovery's propose-experiment tick body as a second tick-body
   variant; the coordinator/heartbeat/dossier machinery is unchanged.

5. **Cadence and output.** A `level:recurring` weekly todo fires each
   topic-quest's synthesis tick. Two outputs:
   - a weekly, shareable **digest cast** — a new cast type on its own
     cadence, reusing `briefing_cast.py`'s lane-union/compose/link-back
     pattern, firing only when there was integration activity;
   - a lighter **daily-brief lane** ("today's topic classifications /
     integrations") for Reto's own visibility between weekly digests —
     usually quiet, fuller once a week.

## Alternatives considered

- **Auto-mint new top-level topics from tier-1 clustering, no human gate.**
  Rejected: ADR 0047 already measured folksonomy drift as a real failure mode
  in this corpus. A closed top-level list with a human-reviewed escape hatch
  avoids repeating it, while sub-tags stay open since they're organizational,
  not identity-defining.
- **Separate dossier per health-span sub-theme** (sleep / fitness /
  biomarkers / inflammation as independent quests+drafts). Rejected — Reto's
  call: one big document, `draft` already handles scale, and cross-cutting
  content works better inside one synthesis than split and reconciled across
  several.
- **A brand-new standing-review mechanism instead of reusing `quest`.**
  Rejected: `quest` already *is* "perpetual investigation over an evolving
  corpus, dossier + WORM log + self-paced tick" — the exact shape needed;
  only the tick body differs.

## Consequences

- **Positive**: NOx catalysis's hand-built pattern generalizes for free to
  any topic; "what's unintegrated" is always a live query, not only a weekly
  artifact; multi-label tagging means genuinely cross-cutting papers aren't
  forced into one bucket; the newsletter reuses proven cast infrastructure
  instead of a new compose path.
- **Negative**: quest's hypothesis/experiment-flavored WORM log entry types
  are stretched to non-experimental lit-synthesis work — implementation
  should read existing entry types (note/hypothesis/result/decision/
  milestone/cost) onto synthesis events rather than growing the schema.
- **Neutral**: no new migration for "integrated" (existing link mechanism);
  the topic taxonomy config (`data/topics/*.yaml`) mirrors the existing
  `data/axes/*.yaml` shape and lifecycle (versioned, gated, prompt-carrying).

## Open questions for implementation (not decided here)

- Exact keyword seed lists and tier-1 prompt per topic.
- Whether `noxrr`'s existing quest is retrofitted to the new synthesis
  tick-body pattern, or kept exactly as-is with the classifier only feeding
  it papers passively alongside its own active search.
- `quest_log` entry-type mapping for synthesis ticks — reuse existing types
  vs. add one (e.g. `integration`); default to reuse.

## See also

- `docs/design/topic-dossiers.md` — mechanics: cascade tiers, taxonomy file
  shape, integration-link query, digest-cast wiring.
- [ADR 0047](./0047-controlled-chunk-tagging.md) — cascade classifier
  precedent, folksonomy-drift evidence.
- `docs/design/catalyst-discovery-quest.md` — quest mechanics precedent.
- `docs/architecture/state-map.md:697-738` — quest lifecycle detail.
