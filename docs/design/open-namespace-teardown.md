# OPEN-namespace teardown

> **Status:** design / prerequisite for ADR 0047 rollout.
> **Goal:** retire the free-form `OPEN` tag namespace as a *machine*
> substrate — every deterministic code writer moves to a real
> namespace or a column — so the residual `OPEN` rows are purely the
> human/agent folksonomy and can be culled or consolidated without
> collateral damage. This is the "nuke OPEN\* soonish" work.

## Why

`OPEN` is currently three things wearing one namespace:

1. **Machine state** written by background code — alert lifecycle,
   todo hierarchy level, review tiers, ingest markers. Deterministic,
   read back by `LIKE 'prefix:%'` filters. Must NOT be culled.
2. **A curated-axis staging area** — ADR 0047 moved curated tags OUT
   of `OPEN` into per-axis UPPERCASE namespaces (`ROLE:`, `SCALE:`,
   `OPEN-QUESTION:`, …). None land in `OPEN`.
3. **The folksonomy** — free `topic:`/`interest:`/`area:`/bare-flag
   values coined by agents via the `tag` verb. This is what "cull
   OPEN" actually targets.

Prod evidence (precis\_prod, 2026-07): ~45 distinct `OPEN` prefixes;
52% of `OPEN` values are singletons; `interest:` alone holds ~1,200
distinct values and `topic:` ~795 — the folksonomy long tail. But
mixed into the same namespace are ~5,200 `tier:` rows and the entire
alert/todo control plane. **A `LIKE 'OPEN%'` cull would delete the
control plane and the ADR-0047 curated axes alike.** Hence this doc:
classify every prefix, migrate the machine ones, and pin down an
exact-match cull rule.

## The three piles

### Pile A — MACHINE (migrate to a real namespace/column first)

Written by deterministic code via `store.add_tag(..., Tag.open(...),
set_by="system")`, which **bypasses `Tag.parse_strict`** (no
validation). Each needs a destination namespace (a new UPPERCASE
closed axis or an existing column) and its writer + every
`LIKE`/exact reader updated in the same migration.

| prefix | writer (file:line) | target | readers to update |
|---|---|---|---|
| `tier:` | `workers/review.py:308` (values `tier:structural` `workers/structural.py:201`, `tier:deep` `workers/deep_review.py:220`) | `TIER:` closed axis | `deep_review.py:120`, structural filter |
| `severity:` | `alerts.py:153` `create_alert`, `alerts.py:261` `set_severity` | `ASEV:` closed axis (alert severity — **distinct from `SEV`**, see §collision) | alert reads |
| `alert-state:` | `alerts.py:151,195` (`STATE_OPEN`→resolved) | `ALERT:` axis or a `refs.meta` field | alert lifecycle reads |
| `alert-source:` | `alerts.py:152` | `ALERT-SRC:` axis / meta | alert dedup |
| `level:` | `handlers/todo.py:518`, `workers/schedule/worker.py:414`, `schedule/seed.py:81`, `draftimport/build.py:327` (constants `_todo_guards.py:63-70`) | `LEVEL:` closed axis (or `refs.level` column) | **heavy**: `_todo_views.py:170` `LIKE 'level:%'` + exact readers `schedule/worker.py:154`, `structural.py:70`, `deep_review.py:61`, `nursery.py:207,214,441`, `_todo_views.py:209,266,525,558,625,669,1000,1087` |
| `halt:` | `workers/dispatch.py:103`, `workers/planner_guardrails.py:204` (`halt:tick-cap`/`halt:cost-cap`) | `HALT:` closed axis | `_todo_views.py:1411,1427` (`LIKE`); also agent-writable (`planner_prompt.py:261`) |
| `child-failed:` | `handlers/_job_bubble.py:81`, `handlers/job.py:443` | `BUBBLE:` axis or job-meta | `LIKE`: `schedule/worker.py:345`, `_todo_views.py:1458`, `_draft_ops.py:742` |
| `project:` | derived `utils/workspace.py:165`; stamped `handlers/todo.py:483-496`, `executors/claude_inproc.py:564` | keep semantics, move to `PROJECT:` axis | `LIKE`: `_todo_guards.py:599,603,624,628` |
| `user:` | `workers/review.py:309` (`user:asa`); migration `0028` seeds `user:elmsfeuer` | `OWNER:` axis (already an identity concept) | owner filters |
| `source:` | `workers/watch_poll.py:223`, `workers/news_poll.py:156` | `SRC:` **exists as a closed axis** (`types.py`) — fold in | verify value overlap |
| `discovered-via:` | `workers/watch_poll.py:223` (`discovered-via:cite:<seed>`) | `VIA:` axis or watch-meta | provenance reads |
| `category:` | `workers/news_poll.py:156`, `workers/briefing.py:189` | `CATEGORY:` axis | news/briefing filters |
| `published:` | `workers/news_poll.py:159`, `workers/briefing.py:189` (ISO date) | `refs.meta` field (a date, not a tag) | none tag-side |
| `tree-review:` | `workers/review.py:307` (date-stamped) | `refs.meta` field | tree-review reads |
| `swept:` | `workers/sweeper.py:262` (`swept:claim-orphaned`) | `SWEPT:` axis or job-meta | bubble cascade |
| `agentlog-source:` | `agentlog.py:98` | `refs.meta` (agentlog is machine, not embedded) | agentlog reads |
| `acquire:` | `handlers/paper.py:648` (`acquire:unverified`) | `ACQUIRE:` axis | acquire reads |
| `kind:` (patent) | `handlers/_patent_ingest.py:419` (`kind:<docdb.kind_full>`) | `PATENT-KIND:` axis (avoid the `kind:id` handle collision) | patent reads |
| `country:` | `handlers/_patent_ingest.py:418` | `COUNTRY:` axis | patent facets |
| `workspace:` | `handlers/plaintext.py:703` | `refs.meta` field | workspace reads |
| `topic:` (machine leg) | `cli/watch.py:929,936` (`topic:book`, folder-drop), `routes/drafts.py:1026` web seed | **stays `topic:` folksonomy** — but the machine legs should emit a curated axis value, see §topic | — |

**Bare flags with machine writers** (migrate alongside):

| flag | writer | target |
|---|---|---|
| `needs-triage` | `ingest/remediate.py:53,212,223` | `TRIAGE:` axis / meta |
| `internal-thought` | `workers/review.py:310` (also agent) | keep as folksonomy flag OR `THOUGHT:` — dual-writer, see §dual |
| `briefing` | `workers/briefing.py:189` | `CATEGORY:briefing` (fold) |
| `built-in` | `jobs/ingest_oracles.py` (`_BUILTIN_TAG`) | `ORIGIN:built-in` (ORIGIN axis exists) |
| `awaiting-fulltext` | `jobs/patent_fulltext_sweep.py` (`_patent_ingest.py:57`) | `FULLTEXT:` axis / meta |
| `fulltext-unavailable` | `jobs/patent_fulltext_sweep.py:443` | `FULLTEXT:` axis / meta |
| `subtype:book` | `cli/watch.py:929` | `SUBTYPE:` axis |

### Pile B — CONSOLIDATE (folksonomy → curated axis)

The bulk of the cull target. These have **no code writer** — only the
`tag` verb reaches them — but they carry signal worth preserving as a
curated axis rather than dropping:

- `topic:` (~795 distinct) and `interest:` (~1,200 distinct) — the ADR
  0047 `interest:`/`topic:` consolidation. Feed the mined values
  through the axis-minting lifecycle (§curation in ADR 0047); admit
  ≥30-ref values into a `TOPIC:` curated namespace, drop the singleton
  tail.
- `area:`, `field:` — subsumed by the ADR 0047 `domain:` axis (already
  a curated `DOMAIN:` destination).

### Pile C — DELETE (junk + free folksonomy tail)

No writer, no signal, or actively wrong:

- `status:exercise-mcp-throwaway` (2 rows, agent test junk).
- `prio:high` / `prio:low` — **shadow** of the `PRIO` closed axis AND
  the `refs.prio` integer column. No code writes them; no `PRIO:`
  filter or the integer column ever matches them. `precis-tags.md:185`
  currently *teaches* `prio:high` ("lowercase = OK") — fix the skill,
  then delete the rows.
- `projects:` (plural) — typo variant of `project:`, no writer.
- `kind:a` / `kind:a1` / `kind:b1` — opaque system codes; investigate
  origin (no writer found in `src/` — possibly a dead experiment or a
  prod-only script), then delete.
- The singleton free-flag / free-prefix tail (`section:`, `chunk:`,
  `audit:`, `wave:`, `cluster:`, `discovery:`, `changed-mind:`,
  `cross-domain:`, `bug:`, `for:`, `tool:`, one-off bare flags).

**`sticky:` caveat** — no code *writer* but it HAS machine *consumers*
(`handlers/memory.py:82`, `store/_tag_filter.py:139` TTL). Agent-
authored, taught in `precis-memory-help.md`. Do **not** cull; it is a
folksonomy prefix with a real read contract. Either promote to a
`STICKY:` axis or leave it as a blessed folksonomy prefix (whitelist,
§parse\_strict below).

## The exact-match cull rule (the OPEN-QUESTION footgun)

ADR 0047 puts the chunk axis `open-question:` into its **own**
namespace `OPEN-QUESTION` (uppercased at load). A cull written as:

```sql
DELETE FROM tags WHERE namespace LIKE 'OPEN%';   -- WRONG
```

would delete `OPEN-QUESTION` (and any future `OPEN-*` axis) along with
`OPEN`. **Mandate exact match:**

```sql
DELETE FROM tags WHERE namespace = 'OPEN' AND value LIKE '<prefix>:%';
```

The namespace column already stores `OPEN` (not `OPEN:foo`) — the
prefix lives inside `value` (`Tag.open`, `store/types.py:296`). So a
cull is always `namespace = 'OPEN'` + a `value` predicate; never a
namespace `LIKE`. This is the single rule that keeps curated axes out
of the blast radius without renaming `open-question` to `QUESTION:`/
`GAP:` (rename stays available as a belt-and-suspenders option, but is
not required if the exact-match rule holds).

## Collisions to preserve (do NOT merge)

- **`severity:` (OPEN) ≠ `SEV` (closed).** Different vocabularies for
  different things: `OPEN severity:` is *alert* severity
  (`critical/info/major/minor/warn`, `alerts.py`); `SEV` is *review*
  severity (`nit/technical/gaps/adversary/rigor`, `types.py`). Migrate
  `severity:` to a **new** `ASEV:` (alert-severity) axis — folding it
  into `SEV` corrupts both vocabularies.
- **`prio:` (OPEN) vs `PRIO` (closed) vs `refs.prio` (int column).**
  Three distinct things (§Pile C). The OPEN rows are pure shadow —
  delete, don't migrate.
- **`status:` (OPEN) vs `STATUS` (closed).** No OPEN `status:` tag rows
  exist — `status:done` etc. are `ref_events.event` values only
  (`todo.py:808`). Nothing to migrate; just don't let a future writer
  coin an OPEN `status:` tag.
- **`kind:` (OPEN patent tag) vs `kind:id` handle vs `refs.kind`.**
  The patent doc-kind tag must move to `PATENT-KIND:` so it never
  collides with the universal `kind:id` link syntax (`_link_target.py`).

## The `parse_strict` whitelist seam

Once Pile A is migrated, tighten the write boundary so the folksonomy
cannot regrow uncontrolled. The seam is `Tag.parse_strict`
(`store/types.py:397+`, the fall-through before line 506) reached by
every agent add via `apply_tag_ops` (`handlers/_link_tag_ops.py:220`).
Options, cheapest first:

1. **Blessed-prefix whitelist.** Allow OPEN `prefix:value` only for a
   short curated set (`topic:`, `sticky:`, `interest:` during
   transition); reject unknown prefixes with a "use an axis or a
   folder" error. Bare flags stay open (guarded only by
   `_RESERVED_FLAGS`).
2. **Freeze OPEN entirely** once Pile B is consolidated — the `tag`
   verb only writes closed axes + folders; OPEN becomes read-only
   legacy.

Note: machine `Tag.open()` calls bypass `parse_strict`, so the
whitelist alone does **not** stop the control plane — Pile A migration
is what removes those writers. The whitelist only governs the
agent-facing regrowth.

## Teardown sequence

1. **Freeze new axes' destination.** Confirm the ADR 0047 axis loader
   registers UPPERCASE namespaces from `data/axes/*.yaml` at boot (it
   does — `axes/README.md:62`). New curated tags already avoid OPEN.
2. **Migrate Pile A, one prefix per forward migration** (forward-only,
   never edit sealed SQL). Each migration: (a) create/confirm the
   target namespace, (b) copy rows `OPEN value 'p:x'` → `TARGET value
   'x'`, (c) update the writer(s), (d) update every `LIKE`/exact
   reader, (e) delete the old OPEN rows for that prefix. Ship + deploy
   + verify before the next prefix — the heavy-reader ones (`level:`,
   `child-failed:`, `project:`, `halt:`) are the risky ones and want
   their own PRs.
3. **Consolidate Pile B** through the ADR 0047 minting lifecycle
   (mine → admit ≥30-ref → `TOPIC:`/`DOMAIN:` curated axis → migrate
   admitted values → drop tail).
4. **Tighten `parse_strict`** (whitelist or freeze).
5. **Cull Pile C** with exact-match `namespace='OPEN'` deletes.
6. **Final sweep:** any residual `namespace='OPEN'` rows are the
   accepted folksonomy or nothing; assert the count is what's expected.

Each step is independently shippable and reversible (forward
migrations; row copies before deletes). Nothing here is a big-bang.

## Open questions for Reto

- **`level:` destination** — new `LEVEL:` closed axis vs a
  first-class `refs.level` column? The column is cleaner (it's core
  todo state with ~14 read sites) but is a bigger migration. The axis
  is a smaller diff and matches the existing `PRIO:`/`refs.prio`
  precedent (column + back-compat tag alias).
- **`internal-thought` dual-writer** — `review.py:310` (machine) and
  the inner-life skill (agent) both write it. Keep as a blessed
  folksonomy flag, or split machine → `THOUGHT:` and leave the agent
  flag? (§dual)
- **`sticky:`** — promote to `STICKY:` axis, or bless as a
  whitelisted folksonomy prefix given its read contract?
- Investigate the `kind:a`/`kind:a1`/`kind:b1` origin before deleting
  (no `src/` writer — likely a prod-only script or dead experiment).
