---
status: draft
title: Memory gardener — text-level detectors, a reviewed rewrite door, a retire door that owns its mentions, and one-fact-one-ref for memory bodies
pillar: memory-graph
prio: high
model: opus
---

# Memory gardener

Reto, 2026-11-03 (session THINKING_SPEC): "design the gardener to fix it";
retiring must work when the references were not linked; update memories
where practical (spark); keep memories DRY, one node per entity that
others point at. Extends the graph gardener
(`precis-gardener-help`, [graph-gardener-value](graph-gardener-value.md),
[local-mesh-upkeep](local-mesh-upkeep.md) §2) and the trust contract
([graph-node-trust-state](graph-node-trust-state.md)); it does not start
a second loop.

## Motivation / why

A 60-node stratified sample of repo-dev memory (2026-11-03, six Sonnet
detectors, two Opus verifiers, read-only) found 34 nodes with a
substantive smell and 12 more with a title-shape smell; 14 were clean. Of
19 flagged rows re-judged by Opus, 17 held and 2 were false alarms. The
confirmed smells are the graph lagging the repo: a later commit, deploy
or node overturned the claim and nothing on the write path noticed.

| code | claimed | held |
|---|---:|---:|
| anchor rot (qualname, script, doc section, sha, handle) | 7 | 6 |
| scope (true only in an unstated context) | 2 | 2 |
| missing dated instance on a gotcha | 3 | 0 |
| authority (workflow rule without who/when) | 1 | 1 |
| conflict with a live neighbour | 4 | 3 |
| landed history, not a standing fact | 7 | 6 |
| negative claim without where-looked/sha | 3 | 3 |
| duplicate | 2 | 1 |

Confirmed examples (prod handles, for the builder's orientation, not test
fixtures): me474318 says spark is permanently retired while 932364d0a and
me538486 have it on compute; me474450 says no coverage is measured while
pyproject carries pytest-cov and a diff-cover gate; me474428 cites
me477682, soft-deleted, whose content moved to me478001; me538308 says
`ship --quick` runs mypy, which 662db65c8 removed; me538275 points at
"section 9" of a 25-line backlog file; five of six sampled thread nodes
are end-of-day snapshots with no `Left:` line, which the daily sweep
keys on and therefore never retires.

The gardener today cannot see any of this: its detectors are structural
(dup pairs, dangling edges, untagged, redundant links, orphans) and its
apply surface is merge and relink. A mention edge is only materialised to
a live ref (`handlers/_numeric_ref.py::NumericRefHandler._sync_mention_links`)
and auto-prune deletes it once the target retires, so a prose handle to a
retired node has no edge left to find it by.

## In scope

### 1. Deterministic detectors (no model, windowed under the existing cursors)

Each candidate is pinned to the node's content sha and, where it checks
the repo, to the main sha it checked against.

- **dangling_mention** — regex over live memory bodies for handles,
  joined to retired refs. Carries the successor when the retired ref has
  one (`meta.superseded_by` or an inbound `supersedes` edge).
- **anchor_rot** — `path/file.py::Qual.name` and dotted qualnames against
  the python index at the main sha; `scripts/<name>` and `docs/<path>`
  against the tree; `§ <section>` / `#fragment` pointers against the md
  chunk headings; 7+ hex shas against git.
- **thread_shape** — a `section:threads` node, including `part-of`
  children carrying the tag, whose first line is not `Left:`; or whose
  `Left:` names a todo/gripe in a terminal STATUS or a sha already on
  `origin/prod`.
- **title_shape** — slug titles (`^[a-z0-9_]+$`), path-like titles, more
  than sixteen words, dated-snapshot titles on thread nodes.
- **negative_undated** — a line asserting absence ("no X", "NO door",
  "none", "not yet", "does not exist") with no date on the line; the
  candidate carries a grep brief for the evaluator.
- **source_drift** — a `Source:` line naming an md section whose chunk
  sha changed after the memory was last touched.
- **entity_claim_stale** — a body mentioning an entity ref (machine,
  skill, md section, python symbol, gripe, todo) in a state-verb sentence,
  touched before that ref last changed.
- **review_due** — no current `kg-review` stamp at this sha and checklist
  version, by type age (reference/gotchas 30 d, runbooks 60 d, workflow
  and rulings at `review_by`, threads never: the sweep owns them). This is
  the backfill walk.

### 2. Evaluator (the existing proposer, a memory-body mode)

Input: the node's `fisheye+1hop`, the detector's evidence, and the
per-type checklist below. Output: exactly one action as a proposal row —
`rewrite` (anchored find/replace ops plus `reason`), `qualify` (a caveat
line, or a `qualifies` edge from a named gotcha), `supersede` (by a named
live node), `retire`, `no-change` (with why; closes the key while the sha
holds, as a proposer reject does today), or `ask` (a `memory-review` todo
with `llm_tier` by smell class). Routed on the cheap lane through
`llm.op.graph_garden:propose`.

| type | checks |
|---|---|
| reference | route present (an evidence edge, a Source line or a `route:` tag); confident wording on one root; quoted span supports the claim; anchors resolve |
| gotchas | one dated case, a handle resolving to a dated gripe/todo counts; cause already fixed; scope stated |
| runbooks | every named script/flag exists; preconditions; last run |
| workflow, rulings | who, when, in force, absorbed where, conflict with another in-force ruling; no route check (an imperative has authority and scope, not truth) |
| threads | `Left:` current, due items linked |
| DREAM | duplicate/merge only (Reto 2026-10-24: never bulk-retire dreams) |

Title shape is checked on every type: the title states the body's
conclusion in about twelve words, no dangling it/this.

Checks on every type (Reto's review of the sample rewrites, 2026-11-03):

- A rewrite states the claim, never a label. "put ops wipe" is a trap's
  name, not the trap: write what bites, when, and what to do, or point
  at the atomic node whose title says it. A list of child titles in a
  body restates the `part-of` edges and is dropped.
- Unanchored plan labels ("slice 4a", "round 6", "R17", "leg C") with
  no handle or date: a slice is a unit of work and has its own ref;
  resolve to it or drop.
- Commit shas in standing-claim sentences age out after 30 days: keep
  the date, drop the sha; a sentence that only records "X landed at
  <sha>" is history and goes.
- Handoff/resume blocks and dated campaign narratives older than 14
  days in a non-thread node are history and go; the thread file and git
  hold them.
- Belongs in the repo: token grammars, naming standards, conventions
  and door descriptions already in a skill or `docs/conventions` are
  promoted (a promote-to-doc list for Reto; the memory keeps the pointer
  and the ruling date) or deleted as a restatement when the doc already
  says it.
- An instance is one dated clause ("pa5837, 2026-08-27"); the repair
  narrative is history.
- Atomicity: one claim per node, the title is the claim. A grab-bag
  (me477662: fifteen gotchas under one campaign title; me477665: "two
  things, kept separate" in one card) is a `split` candidate (slice 13).
  Recall cuts on title/hook words, so a grab-bag's content is mostly
  unfindable; the courtesy line and the one-fact-one-ref rule both need
  atomic targets.

**Standard of done for a rewrite** (Reto 2026-11-03: "fix the memory,
do the archaeology now, so whoever reads it is that much closer to the
truth"). The evaluator resolves the faulty reference at detection time,
with tools (tombstone `view='retired'`, the successor's body, the
python and md kinds at main, git log, the machine ref), and writes the
result into the node: the claim restated as currently true, one live
anchor, a date or sha. A cold reader gets the truth and never follows a
trail. The trail goes into the edit `reason` and is readable in
`view='log'`. Strip, demote and "unverified since" are not outcomes;
`ask` is the only fallback and only when the fact needs a human. The
rewritten body is no longer than before plus one clause.

Before and after, from the sample: me474428 "me477682 traps (put ops
wipe, …)" becomes "Traps: put ops wipe, stale chain designs, caDNAno
handedness, harness limits; the list is me478001". me474318 "spark
PERMANENTLY retired" becomes "spark left cluster duty 2026-08-29
(thermal) and rejoined as a compute worker 2026-10-15, 932364d0a;
current role on ma_spark". me477665 "NO door" becomes "a wrong DOI
clears with doi='' (8f20218c1); the PDF moves with the rekey door
(1729f2bdf)".

**How the evaluator gets there.** The sentence carries the mention's
*role* and the tombstone carries the target's *fate*; the cell says
where to look, the standard above says what to write.

| role \ fate | duplicate successor | different or absorbed | obsolete | error or typo |
|---|---|---|---|---|
| pointer | rewrite handle | write the content in, with the live handle it now lives at | write what it was and what replaced it, dated | nearest resolving handle if the context matches, else write the content in from search |
| evidence | rewrite handle | re-verify the claim against the successor, write the verified claim | re-verify against the repo now; write the claim as it stands with date and sha | re-verify now; write the verified claim with a live anchor |
| scope | rewrite handle | re-check and restate the scope | date-bound the claim in the sentence | restate the scope from what the body shows, else ask |
| structure, example | rewrite handle | rewrite / replace | replace with the live equivalent | replace with a live example |
| restatement | point at the successor | keep the newer truth once | this node is now the only copy: keep it, say so | keep it, say so |

The first column is the no-model auto-apply; every other cell is the
evaluator writing the current truth in. A handle inside a dated history
line is a record, not a claim, and is left alone.
Forward rule for the skills: the role is the relation — evidence is
`cites`/`derived-from`, scope is `qualifies`, a bare prose mention means
see-also only — so a future dangling edge resolves by its relation.

### 3. Consumer and apply

- The consumer's Opus review covers `rewrite`, `supersede` and `retire`
  like merge/relink today, with the Fable cross-check sample unchanged.
- **apply_rewrite** (new door in `graph/garden/apply.py`): an anchored
  memory edit with the pinned sha as `base_sha`, `reason='garden:<detector>:<proposal>'`,
  the `kg-review:<model>:<date>:fixed` trust stamp, and a reversal record
  `meta.garden_rewrite` (proposal id, prior sha, prior body chunk id) so
  `precis garden undo <rewrite_id>` replays the prior body. Apply-time
  re-verification: the proposal is current, the anchor still fails /
  the entity still says what the evidence said, the node sha is unchanged.
- **apply_supersede**: `supersedes` edge, retire the loser, write
  `meta.superseded_by` on the tombstone so `get` redirects exactly as a
  garden merge does. **apply_retire**: through the retire door below.
- **Human protection**: a node with `trust_origin='human'` or a current
  `considered` stamp is held (`garden_review` row, `held=human`), never
  applied by the consumer.
- **One mechanical auto-apply**, in the spirit of auto-prune:
  `dangling_mention` whose retired ref carries a **duplicate** successor
  (a merge-made redirect, or a `supersedes` edge to a node with the same
  normalised title) rewrites the handle text without a model call,
  recorded like a prune (`garden.applied='mention'`, the before/after
  span in the row). An agent-written `supersedes` with different content
  goes to the evaluator, which checks the span is still supported.

### 4. Retire door

`delete(kind='memory')` today soft-deletes; `get` then returns `Gone`; the
mention edges are pruned later; prose mentions survive twice orphaned.
New behaviour, inside the delete transaction:

- Find live mentions by text (the `dangling_mention` scan for this one
  handle).
- With a successor — an existing `supersedes` edge into this node, or
  `args={'superseded_by': '<handle>'}` of **any kind** (a memory, a
  skill, an md section, a todo: "absorbed elsewhere" is the common fate
  of rulings and threads) — write `meta.superseded_by` on the tombstone
  (redirect on `get`), and ack the mentioning nodes with ready-to-run
  `edit(... find='me<old>', text='<new handle>')` calls; the gardener
  rewrites whatever the caller does not, on the next tick.
- A mention inside a dated history line is listed in the ack but exempt
  from the refuse rule: a record may name retired things.
- **The "also update" list is produced here**, because the retiring
  session knows why the node goes and what replaced it; the gardener is
  the backstop for what it skipped. `delete(kind='memory', id=N,
  dry_run=True)` returns the list without retiring; the real delete acks
  the same list. Each entry is the mentioning node, the sentence holding
  the mention, and a prefilled `edit(..., find='<that sentence>',
  text='<write the current truth in>', reason='me<N> retired <date>:
  <successor or why>')`. The `remember` and `reconsolidate` skills gain
  the step: dry-run the delete, fix the listed nodes, then retire.
- Without a successor and with live mentions: refuse, listing the
  mentioning nodes and the two ways out, `superseded_by=` or
  `args={'force': True}` (the mentions then go to the evaluator as
  `dangling_mention` candidates with no successor).
- A tombstone `get` with `meta.superseded_by` redirects like a merged
  loser; `view='retired'` still shows the tombstone.

### 4b. Compaction before any retire

Reto 2026-11-03: not "retire the record" but "see if anything needs to
be worked into other related memories, a compaction at the end", for any
record that is transient or rarely read. Rule: no node is retired and no
sentence is dropped before compaction. Triggers: expiry passed; zero
reads in 90 days and older than 90 days; history density at or above
half the lines; or any `retire` proposal, which runs compaction first.
The evaluator decides per sentence: keep, move to a related node (hub
siblings, qualifies targets, `+recall` neighbours, restated-claim
matches), mint as an atomic node, or drop, where drop is only history
or restatement. The plan applies as one unit with one undo. The
reviewer's check is the reconsolidate rule: no still-true claim dropped
without being moved. This is what the reconsolidate skill does by hand
for its judgment findings; after this slice the skill shrinks to
reading stats, deciding held items and the promote-to-doc list.

### 4c. The write path

The gardener fixes legacy; the write path prevents new grab-bags. A
shape card of fifteen lines at the top of the `remember` skill,
`precis-memory-help`, `precis-firstline-help` and AGENTS.md: one claim
per node, the title is the claim, the body is the gloss (why, when it
bites, what to do, one live anchor, a date), edges carry roles, entities
by handle, expiry by shape, no history, no children lists, no plan
labels without a handle, no shas older than 30 days, no second topic.
Insert-time checks on put/edit as code, never triggers: grab-bag
predicate, unanchored plan labels, old shas, history density, missing
route, title shape, reported as ack lines and `AUDIT:` tags the way the
ungrounded-number check does today, never refusals; `dry_run` returns
the proposed atomic titles for a grab-bag. `review_due` reads the
`AUDIT:` tags first, and a clean rewrite clears them.

### 5. One fact, one ref

Rule (goes into `precis-memory-help` and the `remember` skill): the
current state of an entity lives on the entity's ref; a memory records a
dated event and the why, and points at the handle. Refs exist for hosts
(`machine`, stable facts plus live overlay), skills, md sections (chunk
shas), python symbols, gripes and todos. Spark's role belongs on
`ma_spark`; me474318 keeps "left cluster duty 2026-08-29 because …;
rejoined compute at 932364d0a" as two dated events.

- **Courtesy line**: a body handle to an entity ref renders its current
  state beside the handle in `get` and the fisheye neighbour rows
  (machine: role and status; gripe/todo: STATUS; skill/md: title), so a
  reader never trusts prose for state.
- **restated_claim** (last): sentence-level near-duplicate across live
  memories inside one hub (sentence embedding, not whole-body), proposing
  one keeper and a handle mention in the other.
- **Host facts pass**: a one-off script moves host role/status facts found
  in repo-dev memory bodies onto `machine` refs and rewrites the sentence
  to a dated event plus handle; proposals through the ledger, applied
  through `apply_rewrite`.

## Slices (one gripe each, cluster `memory-gardener`; filed 2026-11-03)

Detectors first (proposal-only), the rewrite door second, the evaluator
third, the entity normal form last. `blocked-by` edges carry the order.

1. gr560757 — `dangling_mention` detector (small; first).
2. gr560758 — `anchor_rot` detector (medium).
3. gr560759 — `thread_shape` detector (small).
4. gr560760 — `rewrite`/`supersede`/`retire` actions, `apply_rewrite`
   with undo, consumer review extended, human hold, the successor-handle
   auto-apply (medium; blocks 6 and 11).
5. gr560761 — the retire door: mention scan, tombstone redirect,
   refuse-unless-successor (medium; independent).
6. gr560762 — evaluator memory-body mode and the per-type checklist
   (medium; after 4; blocks 7, 8, 9, 12).
7. gr560763 — `review_due`, `title_shape`, `negative_undated` detectors
   (medium; after 6).
8. gr560764 — `view='smells'`, per-detector stats, the `kg-review` ok
   stamp on a clean pass (small; after 6).
9. gr560765 — `source_drift` and `entity_claim_stale` detectors (medium;
   after 6).
10. gr560766 — entity courtesy line in get and the fisheye rows (small;
    independent; blocks 11).
11. gr560767 — host facts onto `machine` refs, the spark pass (small;
    after 4 and 10).
12. gr560768 — `restated_claim`, the sentence-level DRY detector (medium;
    after 6).
13. gr561019 — `grab_bag` detector, `split` action, `apply_split` with
    undo (large; after 4 and 6).
14. gr561025 — `compact`: no retire and no dropped sentence before every
    sentence is kept, moved to a related node, minted atomic, or dropped
    as history/restatement; `apply_compact` with one undo (large; after
    4 and 6; last).
15. gr561026 — the write path: the shape card in the skills and the
    insert-time shape checks as ack lines and `AUDIT:` tags (medium;
    independent; the highest-leverage slice, start it early).

Precondition for 11: `search(kind='machine')` returned zero refs on
2026-11-03; the slice starts with `put(kind='machine', mode='sync')` plus
the hosts that never heartbeat.

Adjacent, not in the cluster: gr561018 (web: handles inside a body
table are not clickable).

## Acceptance criteria

- Each detector is a pure read-only function in
  `graph/garden/candidates.py` with a cursor, a `limit`, a deadline and a
  unit test on dev-DB fixtures that reproduces one confirmed smell shape
  from the sample (a retired handle in prose; a qualname missing from the
  index; a thread child without `Left:`; a `Source:` line whose section
  sha changed).
- `apply_rewrite` and `precis garden undo <rewrite_id>` round-trip a body
  byte-for-byte; apply refuses on a sha mismatch, on a human-origin node,
  and on a stale proposal; the `kg-review` stamp lands in the same
  transaction.
- `delete(kind='memory')` refuses with the mention list when live
  mentions exist and no successor is given; with a successor, `get` on the
  tombstone redirects and the ack lists the edit calls.
- The superseded-handle auto-apply never runs a model and records the
  before/after span.
- `precis garden stats` reports pending, sampled and the Opus agreement
  rate per new detector; a detector below 0.8 agreement over its first 50
  verdicts stays proposal-only (no auto-apply widening) until its prompt
  or predicate changes.
- Re-running the 60-node sample on dev-DB copies reproduces the twelve
  confirmed rows as proposals.

## Explicitly NOT in scope

- Governed context (`governed-by` edge, nearest-rule resolution, the
  first-line render): its own spec.
- Findings, taxa, concepts, papers: these detectors read memory bodies
  only; the structural detectors keep covering every kind.
- A per-write trigger bus or a todo per memory write: the gardener tick is
  the trigger; `review_due` is the backfill.
- A new reviews table or verdict value; `rewrite` is a `garden.action`,
  applied is `approved` plus `garden.applied`, as today.
- Rewriting dream memories beyond duplicate/merge.

## Target + blast radius

`src/precis/graph/garden/{candidates,proposals,consumer,apply}.py`, the
proposer prompt, `handlers/memory.py` delete and get (redirect),
`cli/garden.py` (undo, stats), `precis-gardener-help`,
`precis-memory-help`, the `remember` skill. Runs under the existing
2-hourly recurring todo, budget and dark gate, pass name `memory`; nothing
runs until the `graph_garden` service row is lit for it.

## Open questions / decisions log

- 2026-11-03: detectors first, rewrite door second, evaluator third; the
  entity normal form last. Decided (Reto, chat).
- 2026-11-03: refuse-unless-successor at the retire door rather than
  cascade-rewrite inside the transaction: the invariant lives at the door,
  reads never break (redirect), rewrites are eventual and idempotent.
  Decided (this spec; Reto asked for ACID "but keep it clean").
- 2026-11-03: the instance check resolves handles before claiming "no
  dated case" (3 of 3 false alarms in the sample came from this).
- Open: age thresholds for `review_due` per type (defaults above are a
  first guess; tune from `precis garden stats`).
- Pending Reto's yes (his question 2026-11-03): an expiry on operational
  memories. `meta.expires` set at put time, by the writer or defaulted by
  shape (threads 14 d as the sweep already does; handoff, resume and
  "state as of" nodes 14 d; incident and campaign write-ups 90 d; rulings
  at `review_by`; reference/gotchas/runbooks none; DREAM never). Expired
  is never an auto-delete: the node goes first in `review_due` and the
  evaluator's default is extract-then-retire, or re-arm when the node is
  still live state. Recorded on gr560763.
- 2026-11-03: a node like me477662 (workflow, part-of a reference node,
  one qualifies edge, no `Left:`) is reachable by nothing that retires:
  the sweep keys on threads, the prune detector wants unlinked and
  unviewed, memory-lint's `cold` is a judgment finding. It sinks. The
  split (13) and the expiry above are what reach it.
- Open: whether `source_drift` should compare the memory's claim against
  the new section text in the evaluator, or only flag.
