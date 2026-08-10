---
status: draft
title: Topic-report quests — living survey dossiers with weekly taproot-grounded refresh + podcast/Mastodon publish tail
model: opus
---

# Topic-report quests — living surveys, weekly refresh, publish tail

## Motivation / why

Reto wants standing **reports**: per-topic survey documents (NO→NH₃
catalysis, CO₂ capture, ultralight materials, Parkinson's, healthspan, …)
that are written once, then refreshed weekly — gap analysis over newly
ingested papers, new material woven in, a dated "What's new this week"
section, new sections highlighted — and broadcast as a podcast episode plus
a Mastodon post (one account per topic) linking episode + PDF.

**This is not a new subsystem.** A report is a *weave-quest* — the framing
`docs/backlog/paper-writing-pipeline.md` §Framing already fixes ("a document
is a `quest`"): a perpetual striving owning its dossier `draft`
(`dossier-of`) and WORM logbook. Rungs 1–6 of that pipeline are built
(dark): the `topic:` classifier, `view='integration'` gap query, the
place→weave→residual loop (`quest/weave_tick.py`, `precis quest weave`),
the `chunk_review` ledger + lens personas. What's missing is (a) the
quest-creation flow, (b) the weekly maintain-regime tick (rung 7 batching),
(c) freshness + the what's-new appendix (rung 8), (d) taproot-grounded
claims in the weave, and (e) the publish tail. Podcast plumbing is free:
`cast_audio` already narrates any `meta.cast` draft onto the shared RSS
feed; PDF export exists (`export/latex`).

**Claim grounding decision (supersedes pipeline-design Decision 3).** The
weave mints **taproot claim hubs**, not bare `citation` refs: per woven
point, canonicalize (`taproot/canon.place()`) → `mint_hub`/converge →
`attach_evidence` per supporting paper with `pc<id>` grounding; prose cites
`[pub_id]`. This is not new machinery — ADR 0074 + `taproot/cite.py`
already define claim-cites as the terminal draft-citation form, and
`taproot/hub.py` is the single write door with converge-on-collision. What
it buys over `citation`-minting: semantic claim dedup for free (canon's
job — replaces the planned claim-clustering work), `contradicts` edges as a
first-class disagreement signal, cross-report claim sharing (a hub
established in the NO→NH₃ report is the same hub the CO₂ report
corroborates), and a what's-new appendix that can distinguish *new claims*
(new hubs in the window) from *new evidence on known claims* (new edges on
existing hubs).

## In scope

Four slices, each standalone-useful, in dependency order.

### Slice A — topics + quest-creation flow (activation)

- New `topic:` cascade entries (`src/precis/data/topics/*.yaml`):
  `no-to-nh3` (**one** topic covering both ends — NO reduction and NH₃
  synthesis, as dossier sections; distinct from the `autocatpath` discovery
  quest, which keeps its active-search lane), `co2-capture`,
  `ultralight-materials`, `parkinsons`. `healthspan` seed exists.
- Enable `PRECIS_CLASSIFY_TOPICS_ENABLED` + corpus backfill → live
  per-topic paper lists.
- `precis quest create-dossier <topic>` (closes the open
  "topic-dossier weave-quest creation flow" item): mint quest +
  `ensure_dossier` + stamp `topic:<slug>` on the dossier draft (closes the
  rung-2 residual) + `mark_weave_quest` + a weekly recurring tick.
- **Staggered weekly cadence**: each quest's tick day derives from its id
  (hash mod 7), overridable via `meta.weekday` — six reports spread across
  the week, no Monday spike.
- First Make-regime weave per quest produces the initial survey document
  (existing loop, behind `PRECIS_QUEST_LOOP_ENABLED`).

### Slice B — taproot grounds the weave

- `quest/weave.py` / `quest/citation_mint.py`: per woven point,
  canonicalize → hub (mint or converge) → `attach_evidence` (role from the
  weave's judgment: `corroborates` default; `establishes` stays a derived
  promotion) with `source_handle` = the supporting `pc<id>`; prose cites
  `[pub_id]`.
- Export bibliography derives from hub evidence edges (hub → papers) via
  the ADR 0074 resolve path, replacing `cites → paper` for weave-authored
  prose. Hand-authored `citation`s elsewhere are untouched.
- New ADR recording the supersession of pipeline-design Decision 3
  (claims = `citation`) for the weave path.

### Slice C — weekly maintain tick + "What's new"

- **Rung 7 batching**: the maintain-regime tick accumulates the week's
  classified arrivals (placed-but-unwoven), weaves each touched section
  once — never per-paper.
- **Rung 8 freshness**: `since=` / `view='recent'` over chunk `created_at`
  + `ref_events` + edge timestamps; a regenerated, dated **"What's new this
  week"** appendix in the dossier, two parts driven off taproot: new hubs
  in the window (new claims) and new evidence edges on pre-existing hubs
  (new support / **contradiction** — the alert signal).
- **New-section highlighting**: weave stamps touched chunks with the tick
  window (`meta.new_since` or equivalent); carried through to export
  (margin-marked in PDF, badge in smartdraft).

### Slice D — publish tail (podcast + PDF + Mastodon)

- **Weekly digest cast**: new cast profile (reuses `briefing_cast.py`'s
  pattern) composing a per-topic episode from the what's-new appendix;
  fires only when the tick changed something. `cast_audio` narrates +
  publishes onto the existing feed — zero new audio infra. (Closes the open
  "weekly digest cast" item's shareable half; the quiet daily-brief lane is
  NOT in scope here.)
- **PDF snapshot**: per-tick LaTeX export written to a stable served path
  per topic.
- **Mastodon publisher** (net-new): a worker pass, one account per topic.
  Per-topic config = instance URL + token (credentials outside the tree —
  `~/.secrets`/env — never committed); post = 1-para news distilled from
  the appendix + links to episode + PDF. Idempotent via a `meta.toot_id`
  stamp; exponential backoff on failure (same shape as `cast_audio`);
  outbound HTTP through `safe_fetch`.

## Explicitly NOT in scope

- **No new kind.** Reports are quests + dossier drafts; the publish tail is
  tick output.
- Migrating existing hand-authored `citation`s to hubs (that's the shipped
  taproot `[pa]`-arm / backfill lifecycle, `taproot/backfill.py`).
- The quiet daily-brief quest lane (`briefing_cast._lane_quest`, td161129).
- Rung 8's contradiction-driven re-org / split-prune deep tier — the
  appendix *surfaces* contradictions; acting on them stays design-of-record.
- A bespoke Mastodon API client beyond posting (ingest stays the
  `news_sources` RSS path).
- Mastodon account *creation* — operator-side; the publisher consumes
  existing credentials.
- Figure-binary persistence, coverage matrix, `near_duplicates` (pipeline
  follow-ons).

## Acceptance criteria

1. `precis quest create-dossier no-to-nh3` (and the other four topics)
   yields a quest + dossier draft stamped `topic:no-to-nh3`, marked as a
   weave quest, with a weekly recurring tick whose weekday is staggered
   across quests.
2. With the classifier enabled, backfill tags corpus papers; the dossier's
   `view='integration'` lists unintegrated papers for its topic.
3. A Make-regime run produces a survey document whose woven points cite
   `[pub_id]` hub handles; each hub carries ≥1 evidence edge grounded at a
   `pc<id>` of a supporting paper; two papers asserting the same claim
   converge on one hub (canon dedup), not two.
4. A maintain tick over a week with N new topic-tagged papers weaves each
   touched section once (not N times), regenerates the "What's new"
   appendix with new-hubs and new-evidence subsections, and stamps touched
   chunks with the window; the PDF export visibly marks them.
5. A maintain tick with zero new papers publishes nothing: no cast draft,
   no toot, no new PDF snapshot.
6. After an active tick: a cast draft exists and `cast_audio` publishes an
   episode; the Mastodon pass posts once (re-tick does not double-post —
   `meta.toot_id` idempotency) to the topic's account with episode + PDF
   links; a post failure backs off and retries without blocking the tick.
7. All Mastodon requests go through `safe_fetch`; no credential appears in
   the tree or in logs.
8. Full gate green (`scripts/ship`): ruff, mypy, pytest incl. new tests for
   create-dossier, stagger derivation, hub-minting weave, appendix
   generation, publisher idempotency/backoff.

## Target + blast radius

- `src/precis/data/topics/*.yaml`, `workers/classify_topics.py` (enable
  path only) — slice A.
- `src/precis/quest/` (`weave.py`, `citation_mint.py`, `weave_tick.py`,
  new `create_dossier` flow), `workers/job_types/quest_tick.py` — A–C.
- `src/precis/taproot/` (consumers only — `hub.py`/`canon.py` write door
  unchanged) — B.
- Draft views (`handlers/draft.py` `view='recent'`/`since=`),
  `export/latex` (highlight + hub-derived bibliography), smartdraft badge —
  C.
- `src/precis/reading/` (new cast profile), `workers/` (new
  `mastodon_publish` pass + registry row), `workers/cast_audio.py`
  (untouched, consumes the new profile), web route or static path for PDF
  snapshots — D.
- Docs: new ADR (weave claim-grounding supersession), `state-map.md`
  sections for quest layer + publish tail, `precis-quest-help` /
  `precis-audio-help` skills, OPEN-ITEMS deletions (create-dossier flow,
  weekly digest cast, topic-stamp residual) in the shipping commits.

## Open questions / decisions log

- **Decided:** one NO→NH₃ topic (`no-to-nh3`) covering both ends; one
  Mastodon account per topic; weekly staggered cadence; taproot hubs ground
  *all* weave-authored claims (not just the what's-new layer); reports are
  quests, no new kind.
- **Open (blocker for slice D only):** which Mastodon instance(s); do the
  per-topic accounts exist yet (operator-side)?
- **Open (blocker for slice D only):** where PDFs are served from —
  `precis_web` route (new public exposure) vs. external static host.
- **Open (advisory):** should this split into 2–4 proposals per
  the backlog split heuristic (docs/backlog/TEMPLATE.md)? Slices are
  independently shippable (A alone = live gap lists + initial surveys;
  D depends on C's appendix). Suggested split if the fixer is to build it:
  A+B (`topic-report-quests`), C (`report-freshness-appendix`,
  blocked-by A+B), D (`report-publish-tail`, blocked-by C).
- **Open (advisory):** evidence-edge role at weave time — always
  `corroborates` with derived `establishes` promotion (safe default,
  matches `hub.py`'s `_DEFAULT_ROLE`), or let the weave assert
  `contradicts` directly when a paper disputes a hub? Leaning: allow
  `contradicts` (it's in `HUB_ROLES` and is the appendix's alert signal).

## Residuals folded in from topic-dossiers.md (merged here)

- Quiet **daily-brief lane** ("N papers classified today" / "topic X
  integrated Y papers", Reto-only) — out of slice D's scope; tracked as
  td161129 (`briefing_cast._lane_quest`).
- Weave v1 refinements: multi-place (top-1 only today); review-todos
  parented on the quest lack a `level:strategic` ancestor. (The planned
  claim-clustering dedup is superseded by canon-hub dedup, slice B.)
- Whether `noxrr` adopts the synthesis tick body alongside its
  propose-experiment body or stays active-search-driven (ADR 0060
  §"Open questions").
