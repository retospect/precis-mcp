# Todo-Tree Plan — hierarchical task graph with GTD-correct decomposition

Status: **queued** — design captured for a future implementation slice.

A tree of `kind='todo'` nodes connected by `parent_id`. Branches read
as outcomes, leaves read as next physical actions (Allen-style GTD).
Workers pull the **next doable leaf**, run a verdict on it ("is this
actually a next action?"), and either do it, split it into
predecessors, or attach a `waiting-for:` tag. The tree grows
*through use*. The user owns the top two levels; workers own
everything below.

Distinct from — and **not in conflict with** — the queued
`goal-kind-plan.md`. That plan adds a file-backed charter-doc kind
(durable project objectives written as markdown with front-matter,
linkable from anywhere). This plan adds a **dynamic execution
graph** layered on the existing `todo` kind. The two can compose:
strategic todo nodes can `link` to a `goal:<slug>` charter doc for
context, but the tree itself is in-DB and worker-mutable.

## Why

Asa already has dreams (bottom-up speculation), an inner-life
self-doc, and a flat `kind='todo'` list. What's missing is the
**vertical structure** between "build the nanocube AI compute
platform" (Reto-level intent) and "draft setup paragraph in §3"
(asa-level next physical action). Without it:

- Asa can't prioritise — no way to ask "what's the next thing under
  the boxel paper?" because nothing knows what's under what.
- The user can't keep control — workers writing flat todos pollute
  the same list Reto writes strategic intents to.
- Allen's "next action" discipline can't be enforced because
  there's no parent-outcome to test the action against.

The tree is the missing organ.

## Core design

### One kind, hierarchy via `parent_id`

No new kind. `kind='todo'` stays the only player. Hierarchy is a
single new column `parent_id` on `refs` (nullable, ref to another
`refs.id`).

- A todo **with children** is structurally a branch — its first
  line reads as an outcome ("what does done look like").
- A todo **without children** is a leaf — its first line is the
  imperative next physical action (current discipline preserved).
- Promotion (todo grows children) and demotion (children deleted) are
  the same operation in both directions — no kind change, no migration.

`outcome` is **not** a column. It's a convention: the first line of a
branch IS the outcome. Renderer labels it `outcome:` when children
exist. Cheap, no schema cost.

### Authority gradient via `level:<tier>` tag

Three tiers, enforced as tags rather than columns (avoids collision
with the existing `ref_level_decay` semantic in `0011`):

| Tag | Owner | Worker can create? | Worker can edit/delete? |
|---|---|---|---|
| `level:strategic` | Reto | ❌ | ❌ |
| `level:tactical` | Reto (asa may propose via `level:proposed-tactical`) | ❌ | ❌ |
| `level:recurring` | Reto | ❌ | ❌ |
| `level:subtask` (default) | any worker | ✅ | ✅ if `tag='claimed-by:<self>'` or unclaimed |

`level:recurring` is the schedule tier — see "Recurring + schedule
(Slice 4)" below. Owner-only at the root so a worker can't mint a
`* * * * *` cron that burns the budget. Spawned children carry
`level:subtask` and follow the normal subtask rules.

Enforced in `TodoHandler.put`/`edit`/`delete` by inspecting the
caller's `source` identity:

- **MCP callers** carry `source='<agent>'` per ADR
  `0013-mcp-session-context-env-vars.md` (`asa-chatter`, `asa-worker`,
  `asa-dreamer`, specialist names). Any worker source is rejected on
  strategic/tactical writes.
- **Direct importers** — the web UI (per `precis-web-plan.md`)
  passes `source='web:reto'` when calling the handler. Any `web:*`
  source is treated as owner.
- **CLI / interactive Python** sessions are treated as owner.

On rejection: `BadInput("strategic/tactical nodes are owner-only;
propose via tag='level:proposed-tactical' instead")`.

This is the **single most load-bearing control** in the design.
Everything else is depth tuning.

### The GTD interrogation

Worker contract, lives in asa's SOUL and in `precis-tasks-help`:

> Before working a claimed leaf, ask: *"If I had everything I
> needed right now, could I take a visible physical action that
> moves this forward?"*
>
> - Yes, <2 min → do it inline, mark done (Allen's 2-min rule).
> - Yes, real work → engage. Mark done when finished.
> - No → don't fake it. The leaf is a project, not an action. Split:
>   1. Name what's missing as concrete next physical actions.
>   2. Write each as a sibling todo under the **same parent**
>      (one level deep, not nested).
>   3. The original todo becomes a branch (it now has children).
>      No special status change required.
>
> The split IS the work for this turn. A failed interrogation is
> never failure — it's the planning move.

Distinguish split-vs-block-vs-wait:

- **Split** — what's missing is *other actions you also have to
  take*. Write children under the same parent.
- **Block** — what's missing is *another todo already in the tree*
  finishing first. Use existing `links` table with `relation='blocks'`
  (the link-CRUD pass already supports arbitrary relations).
- **Wait** — what's missing is *external* (Reto's input, an API, a
  paper arriving). Tag `waiting-for:<who-or-what>`. The leaf stays
  open but filters out of `view='doable'`.

### Programmatic checks (auto-tasks)

Some leaves don't need an LLM — they need a SQL query to return true.
Two recurring patterns:

- **Wait for external pipeline.** A discovery task finds 5 candidate
  papers; we need them ingested + embedded before downstream work
  can use them. Or a patent number is queued and we wait on the EPO
  fetcher.
- **Wait for human reply.** Asa needs Reto's input to proceed. She
  asks on Discord and parks the work until he answers.

Both shapes are leaves with `meta.auto_check` describing the
condition. A small worker polls open auto-task leaves, evaluates
their check, and flips `status='done'` when the condition holds.
Zero schema impact — `refs.meta` is already JSONB.

#### Shape

```json
"auto_check": {
  "type": "<evaluator-name>",
  "...": "type-specific args"
}
```

Initial evaluator catalogue (each is one SQL or a single store
call):

| `type` | Resolves true when | Use case |
|---|---|---|
| `paper_ingested` | a `paper` ref with the given `doi` exists with ≥1 embedded chunk | "found this DOI, wait for ingest" |
| `patent_ingested` | same for patent | EPO fetch finished |
| `chunks_embedded` | for any ref id, all chunks have embeddings | gating on full corpus |
| `discord_reply_received` | a memory tagged `replied-to:<ask_message_id>` exists | "asked Reto on Discord" |
| `tag_present` | search returns ≥1 ref with tag X | generic gate |
| `link_exists` | a links row from A to B with relation R exists | structural dependency |
| `time_past` | `now() >= meta.auto_check.at` | scheduled wake |
| `composite` | AND/OR of others | combined waits |

### Recurring + schedule (Slice 4)

Scheduled work (dreams, weather checks, "look for xyz conferences",
birthday reminders) lives in the same tree as everything else. The
pattern: a `level:recurring` root carries the schedule and the
spawn rule; each tick mints a fresh `level:subtask` child that runs
once. The recurring root never appears in the doable queue — it's
the *pattern*; only its spawned subtasks are *actions*.

#### Watches umbrella

A single seeded `level:recurring` ref titled **Watches** sits at
the top — every recurring lands under it by default. The seed is
done by the schedule worker on first run, idempotent on
`meta.builtin='watches-root'` (not a SQL migration):

```sql
INSERT INTO refs (kind, title, meta, set_by)
SELECT 'todo', 'Watches', '{"builtin":"watches-root"}'::jsonb, 'system'
 WHERE NOT EXISTS (
   SELECT 1 FROM refs
    WHERE kind='todo' AND meta->>'builtin'='watches-root'
 );
-- + level:recurring tag, applied in the same tx
```

The Watches root carries `meta.schedule = null` — it's a *folder*,
not a schedule. The spawner skips folder rows; only its children
tick. Recurring refs with `meta.builtin` non-null reject `delete`
at the handler boundary (footgun protection — deleting the
umbrella would orphan every watch).

New recurrings default `parent_id` to the Watches root; an
explicit `parent_id=<some-strategic>` lets a recurring nest under
a goal when it serves one ("Birthday reminders" under "Personal
life", for instance).

```
[Watches]                  level:recurring, schedule=null      (folder root)
  ├─ Check arxiv weekly    level:recurring, cron='0 9 * * 1'
  │   └─ Check arxiv 2026-06-14   level:subtask                (spawned)
  ├─ Weather               level:recurring, cron='0 7 * * *'
  │   └─ Weather 2026-06-14       level:subtask
  └─ Dream nightly         level:recurring, cron='0 3 * * *'
      └─ Dream 2026-06-14         level:subtask
```

#### Schedule format

Canonical: a cron string in `meta.schedule.cron`. Shorthand
optional: `meta.schedule.every` (`'1d'`, `'mon 09:00'`, `'1h'`)
translates to cron at write time so the runtime only ever sees one
shape. Validation at write time mirrors the auto-check pattern:
malformed cron → `BadInput` at `put`, not at the next tick.

```json
"schedule": {
  "cron": "0 9 * * 1",
  "backfill_missed": false
}
```

`backfill_missed` defaults to `false` (skip): weather, news,
"yesterday's headlines." Opt-in `true` for birthdays and
anniversaries where missing the tick still owes the action.

#### Tick mechanics

Each tick mints exactly **one** subtask under the recurring root.
Idempotency is via `meta.spawned_for_tick='YYYY-MM-DDTHH:MM'` on
the spawned child — the spawner checks for an existing child with
the same tick stamp before minting. Same-minute reruns are no-ops.

Collision policy: if the previous tick's subtask is **still open**
when the next tick fires, the spawner **skips the new tick**. A
stalled queue doesn't pile up; the operator notices the stuck leaf
in the nursery sweep. (Mint-anyway and auto-timeout-previous were
rejected — both hide problems behind extra writes.)

#### Spawning

```python
# Schedule worker pseudocode — runs as a precis worker --only schedule pass.
for recurring in level_recurring_refs(active=True):
    if recurring.meta.schedule is None:
        continue                       # folder, not a schedule
    schedule = parse(recurring.meta.schedule)
    for tick_ts in ticks_since(recurring.last_tick(), schedule,
                                backfill=schedule.backfill_missed):
        tick_stamp = tick_ts.isoformat(timespec='minutes')
        if child_with_tick_exists(recurring.id, tick_stamp):
            continue                   # idempotency
        if has_open_previous_tick(recurring.id):
            log.info('skipping tick: previous still open')
            continue                   # collision = skip
        precis.put(kind='todo',
                   parent_id=recurring.id,
                   text=render_title(recurring, tick_ts),
                   prio=2,             # cron-spawned default
                   meta={'spawned_for_tick': tick_stamp,
                         'executor': recurring.meta.executor})
        store.append_event(recurring.id,
                           source='schedule',
                           event='spawn',
                           payload={'tick': tick_stamp})
```

`source='schedule'` on the event is the provenance answer to "what
spawned this leaf?" — no new tag needed. Structural identity is
`parent_id` chain → `level:recurring`. Two channels, both queryable
without invention.

#### View shape

`view='roots'` grows a second panel below the strategic dashboard:

```
## Goals (4 strategics)        7d picks                                    (panel 1)
#42 Build nanocube AI compute platform   7d:  9 picks       
#56 Personal life                        7d:  2 picks  ← next pick (lowest)
#67 Reading                              7d:  2 picks
#88 Engineering hygiene                  7d: 11 picks
Active: 4    Total picks: 24    Expected share: 6 each

## Watches (3 recurring)        last tick                                  (panel 2)
#12 Check arxiv weekly                   last:  2d ago    next: 5d
#13 Weather                              last:  3h ago    next: 21h
#14 Dream nightly                        last:  9h ago    next: 15h
```

The Watches panel doesn't compete in picks-7d — recurring is
orthogonal to the strategic rotation, so a noisy cron can't crowd
a small strategic out of its 1/N share.

#### Soft cutover of existing schedulers

Slice 4 ships additive. The existing `kind='cron'` infra
(migration 0010) and the dream cron continue to work; new
scheduled work uses `level:recurring`. The legacy schedulers
retire when nothing references them — no rewrite, no migration
risk, rollback is "ignore the recurring rows."

### Priority — a small int column, 1–10

PRIO becomes a first-class column on `refs` rather than a
closed-prefix tag. Reasoning: it's a sort key on every doable
query, and the relational answer ("`r.prio` ASC") beats the
join-through-ref_tags-and-tags path on every dimension —
clarity, query plan, write surface.

```sql
-- migration 0014_refs_prio.sql (Slice 4)
ALTER TABLE refs
    ADD COLUMN IF NOT EXISTS prio SMALLINT
    CHECK (prio IS NULL OR prio BETWEEN 1 AND 10);
CREATE INDEX IF NOT EXISTS refs_prio_idx
    ON refs (prio) WHERE prio IS NOT NULL;
```

`NULL` = "no explicit priority, use the kind's default at sort
time" (so untouched refs cost nothing). The doable view's order-by
clause becomes:

```
ORDER BY
  is_paused_ancestor ASC,                -- skip paused subtrees
  has_waiting_or_block ASC,              -- skip waiting / blocked leaves
  COALESCE(r.prio, 5) ASC,               -- 1 = chat, 2 = cron, 5 = default
  strategic_picks_7d ASC,                -- least-picked strategic among ties
  r.ref_id ASC                           -- deterministic tiebreak
```

PRIO 1 and 2 preempt the strategic rotation; PRIO 3-10 still get
the 1/N share via picks-7d within their tier. Numerical, no labels
— `PRIO:urgent`-style vocabulary is intentionally retired (the
closed prefix stays on `Tag.parse_strict` as an alias that
translates to a column write at the handler boundary for
backward compat, but new code writes the column directly via
`put(prio=N)` / `tag(prio=N)`).

Default PRIO by spawner:

| Spawner | Default PRIO |
|---|---|
| Chatter (Discord reply, mid-turn ask) | 1 |
| User (web UI, CLI) | 1 |
| Cron / recurring tick | 2 |
| Worker mid-split (sibling under claimed leaf) | 5 |
| Dreamer proposal | 8 |

Resolution-on-unblock is pure: when a wait (`asking-reto`,
`paper_ingested`, etc.) closes, the consumer becomes doable at
the PRIO it was written with. No machinery bumps it. If a writer
wants "do this right after Reto answers," they set `prio=1` at
write time (or the chatter default of 1 already does it).

Optional `meta.auto_check.timeout_at` (ISO timestamp) → on timeout
the leaf flips to `status='auto-timeout'` and surfaces in the
nursery sweep for human triage.

#### Pattern 1 — "Get and wait for papers"

A worker on leaf #103 ("Find supporting papers on MOF X") runs the
search, finds 3 DOIs, queues each for ingest, writes 3 auto-task
**siblings**, then wires `blocked-by` links from the downstream
consumer to each auto-task:

```
#98 Methods section
  ├─ #103 Find supporting papers on MOF photocatalysis  (done)
  ├─ #104 [auto] wait for paper doi:10.x/y1 ingested+indexed
  ├─ #105 [auto] wait for paper doi:10.x/y2 ingested+indexed
  ├─ #106 [auto] wait for paper doi:10.x/y3 ingested+indexed
  └─ #108 Draft setup paragraph using found papers
          blocked-by 104, 105, 106
```

```python
# in worker, after finding candidates
for doi in candidates:
    precis.put(kind='paper', ref={'doi': doi})              # queue ingest
    wait_id = precis.put(kind='todo',
                         parent_id=98,
                         text=f'[auto] wait for paper {doi} ingested+indexed',
                         meta={'auto_check': {
                             'type': 'paper_ingested',
                             'doi': doi
                         }}).id
    precis.link(source_id=108, target_id=wait_id, relation='blocks')

precis.edit(kind='todo', id=103, status='done')
```

Why siblings, not children of #103: the discovery is genuinely
complete — the wait is on an external pipeline, not on more work
the discovery task is doing. Sibling pattern uses existing
`blocked-by` semantics; no parent-cascade rule needed.

**Optional: leave breadcrumbs.** If the discovery worker has
specific judgment to pass forward, it writes a memory **linked to
the consumer leaf** via the existing links table:

```python
precis.put(kind='memory',
           text='thompson is the strongest cite for §3 — lead with '
                'it. lee supporting, chen background.',
           tags=['internal-thought', 'user:asa'],
           link='todo:108',
           rel='note-for')
```

Uses `links` infrastructure (the same one `blocked-by` rides on)
instead of a parsed tag string — cleaner cascade on task deletion,
no string formatting, no `WHERE tag = 'for-task:' || $id` query.
When the consumer leaf is claimed, the worker's `NotesForMe`
context slot surfaces these automatically via a clean join:

```sql
SELECT m.text, m.created_at, m.source
FROM refs m
JOIN links l ON l.source_id = m.ref_id
WHERE l.relation = 'note-for' AND l.target_id = $claimed_leaf_id
  AND m.kind = 'memory'
ORDER BY m.created_at DESC;
```

See Modes section.

#### Pattern 2 — "Ask Reto on Discord"

The worker asks via the existing message kind and parks the work:

```python
msg = precis.put(kind='message',
                 text='Reto: should §3 cite Tanaka 2024, or skip?',
                 target='discord/<guild>/<channel>/<thread>')
ask_id = precis.put(kind='todo',
                    parent_id=98,
                    text='Decide: cite Tanaka 2024 in §3 — asked Reto',
                    tags=['asking-reto'],
                    meta={'auto_check': {
                        'type': 'discord_reply_received',
                        'ask_message_id': msg.id,
                        'thread': 'discord/<guild>/<channel>/<thread>'
                    }}).id
precis.link(source_id=consumer_leaf_id, target_id=ask_id, relation='blocks')
```

**Chatter side** (when Reto replies):

The asa-bot reply pipeline checks for any open `asking-reto`
leaves in the same thread (`view='asking-reto'` filtered by
thread). If Reto's message looks like a reply (in-thread + recent
+ either explicit quote or in-time-window), chatter:

1. Writes the answer as a memory linked to the asking leaf via
   `relation='answers'`.
2. Tags the leaf `replied-to:<ask_message_id>`.

The auto-check poller resolves the leaf on the next tick. The
consumer leaf unblocks.

#### `view='asking-reto'` for chatter visibility

Renders in chatter's preamble so Reto sees what asa is waiting on:

```
## Pending asks (3)

#112  Cite Tanaka 2024 or skip?              asked 2h ago in #boxel
#127  Approve the patent-drafting workflow?  asked 1d ago in #patents
#143  Which figure 2 variant — A or B?       asked 4d ago in #boxel
```

He replies in-thread; the chatter wires the answer and the leaf
resolves. No manual `/precis` poking required, though it's
available.

#### Auto-task worker

`precis worker --only auto_check` — polls every 60s. Walks open
todos with `meta.auto_check` non-null, dispatches to the
registered evaluator for the `type`, on `true` writes:

```sql
UPDATE refs SET status = 'done' WHERE ref_id = $leaf_id;
INSERT INTO ref_events (ref_id, source, event, ts)
VALUES ($leaf_id, 'auto-check', 'auto-resolved', now());
```

On `false`: leaves alone. On `timeout_at` exceeded: flips to
`status='auto-timeout'` and the nursery sweep surfaces it.

Hooks (notify-when-paper-ingested) could replace polling later if
60s feels wasteful; polling is the simpler v1.

### Provenance walks up

Every leaf carries its full ancestry chain to the user. Walk-on-read:
when a worker pulls a leaf via `get(kind='todo', id=N)`, the handler
walks `parent_id` to the root and includes each level's text +
outcome in the response. Depth is small (≤10, see knob #3), DB is
fast, no caching needed. **There is no separate `view='ancestry'`** —
the chain is part of every `get` response, surfacing it as a view
would be redundant.

The preamble's `_render_active_stack` renders the same chain
visually — same data, two surfaces.

A leaf without a strategic ancestor is an **orphan** and gets
flagged by the nursery sweep.

## Storage — what changes

New migration `0013_todo_tree.sql`:

```sql
BEGIN;

ALTER TABLE refs
    ADD COLUMN IF NOT EXISTS parent_id BIGINT NULL
        REFERENCES refs(ref_id) ON DELETE SET NULL;

-- Partial index — most refs aren't in a todo tree.
CREATE INDEX IF NOT EXISTS refs_parent_id_idx
    ON refs (parent_id)
    WHERE parent_id IS NOT NULL;

COMMIT;
```

That's the whole structural change. ~5 lines of SQL. (Note: the FK
targets `refs.ref_id` — the existing primary-key column name from
`0001_initial.sql`. All SQL examples in this plan use `ref_id`
consistently.)

Everything else (level, claim, pause, blocked-by, waiting-for,
outcome) lives in existing infrastructure:

- `level:strategic|tactical|subtask` — tags
- `claimed-by:<handle>` — tag
- `paused` — existing `status` column value (paused branches
  propagate at query time, see view shapes)
- `blocked-by` — existing `links` table with `relation='blocks'`
- `waiting-for:<x>` — tag
- `outcome` — first-line convention on branches, no column

No `outcome` column. No `blocked_by` column. No `level` column. No
`claim` column. Just `parent_id`.

## Address shape — views

All on the existing `precis` verb, no new top-level verbs.

| Address / call | Renders |
|---|---|
| `search(kind='todo', view='roots')` | Top-level strategic roots only — Reto's dashboard, one row per strategic |
| `search(kind='todo', view='strategic')` | Strategic + tactical (top 2 levels) with leaf counts under each tactical |
| `get(kind='todo', id=N, view='tree')` | Subtree as ASCII-ish markdown |
| `search(kind='todo', view='doable')` | Flat doable leaves with inline ancestry |
| `search(kind='todo', view='doable', args={'under': N})` | Doable leaves under a subtree |
| `search(kind='todo', view='waiting')` | All `waiting-for:*` tagged leaves with their wait targets |
| `search(kind='todo', view='blocked')` | All leaves with non-done `blocked-by` links |
| `search(kind='todo', view='asking-reto')` | All leaves waiting on a Reto reply (see Auto-tasks) |

Ancestry is included in every `get(kind='todo', id=N)` response —
no separate view needed.

`view='doable'` semantics:

```
status = 'open'
AND NOT EXISTS (SELECT 1 FROM refs c WHERE c.parent_id = r.ref_id
                AND c.status != 'done')                 -- no open children
AND NOT EXISTS (SELECT 1 FROM links l                   -- no open blockers
                JOIN refs b ON b.ref_id = l.target_id
                WHERE l.source_id = r.ref_id AND l.relation = 'blocks'
                AND b.status != 'done')
AND NOT EXISTS (                                        -- no paused ancestor
    WITH RECURSIVE p AS (
        SELECT ref_id, parent_id, status FROM refs WHERE ref_id = r.ref_id
        UNION ALL
        SELECT q.ref_id, q.parent_id, q.status FROM refs q
        JOIN p ON q.ref_id = p.parent_id
    )
    SELECT 1 FROM p WHERE status = 'paused'
)
AND NOT EXISTS (SELECT 1 FROM ref_tags t                -- not waiting
                WHERE t.ref_id = r.ref_id
                AND t.tag LIKE 'waiting-for:%')
AND NOT EXISTS (SELECT 1 FROM ref_tags t                -- not asking
                WHERE t.ref_id = r.ref_id
                AND t.tag LIKE 'asking-reto:%')
```

(Real impl will materialise the ancestor walk cleanly; sketch above
is illustrative.)

`view='tree'` render (markdown-ish, token-cheap):

```
#42 Build nanocube AI compute platform              [3/9 done]
├─ #67 Write the boxel paper                        [1/4]
│   outcome: Submitted to JCP, all figs camera-ready
│   ├─ #98 Methods section                          [0/3]
│   │   ├─ #114 → Draft setup paragraph (4-6 sent.) ◀ claimed:asa
│   │   ├─ #115 Draft simulation paragraph          ⏸ waiting:reto
│   │   └─ #116 Draft validation paragraph          ○ doable
│   ├─ #99 Results section                          [1/2]
│   └─ #100 Discussion section                      ○
└─ #88 Engineering hygiene                          [2/5]
```

`view='doable'` render:

```
#114  Draft setup paragraph (4-6 sent.)
      ↳ Methods § / Boxel paper / Nanocube platform   [claimed:asa]
#127  Verify EPO ingest with ep3501631a1
      ↳ Patent pipeline / Engineering hygiene
#143  Pull figure 2 data from /experiments/2026-05/
      ↳ Methods § / Boxel paper / Nanocube platform

Waiting: 4 | Blocked: 7 | Total open: 23
```

## Preamble injection

Two new slots in `asa_bot/preamble.py` next to `_render_inner_life`.

### Slot 1 — active stack (always shown when a leaf is claimed)

Renders if the conv's claimed handle (`claimed-by:<self>`) has a
non-done leaf. Shows full path-to-root with outcomes on branches:

```
## Active stack

Strategic: Build nanocube AI compute platform              [#42]
  └─ Tactical: Write the boxel paper                       [#67]
        outcome: Submitted to JCP, figs camera-ready
      └─ Subtask: Methods section                          [#98]
          → Draft setup paragraph (4-6 sentences)          [#114]

Siblings under #98: #115 ⏸waiting:reto · #116 ○doable

Before working: run the GTD interrogation. If not doable, split.
```

### Slot 2 — doable queue (always shown, capped at top 5)

```
## Doable next  (5 of 8)

#116  Draft validation paragraph
      ↳ Methods / Boxel paper / Nanocube platform
#127  Verify EPO ingest with ep3501631a1
      ↳ Patent pipeline / Engineering hygiene
#143  Pull figure 2 data
      ↳ Methods / Boxel paper / Nanocube platform
#155  Update precis-overview skill with v8.7.3 changes
      ↳ Engineering hygiene
#171  Reply to Tanaka about XRD timing
      ↳ Collaboration / Boxel paper / Nanocube platform

precis search(kind='todo', view='doable') for the rest.
Waiting: 4 | Blocked: 7
```

Combined real estate: ~15–20 lines. Smaller than the dreams block.
Fits comfortably alongside `_render_inner_life`.

### Specialists, sub-agents, and modes

Three distinct things, easy to conflate:

| | What it is | How it runs | Has a Mode? |
|---|---|---|---|
| **Modes** | asa wearing different hats | own runtime (chatter daemon / worker cron / dreamer cron) | yes — one per mode |
| **Specialists** | librarian, researcher, writer, coder, etc. | spawned by chatter via `Agent(subagent_type=…)` | no — they have their own SOULs in `grimoire/agents/` |
| **Auto-task worker** | the polling SQL evaluator | pure code, no LLM | no |

Specialists are NOT Modes. They don't load `asa-*.md` addons or
go through the preamble assembler. They run as `Agent()` sub-sessions
with their own SOUL (e.g., `grimoire/agents/librarian.md`), and
their context comes from the parent's prompt — whatever chatter
chooses to pass in.

When chatter dispatches a specialist for tree-related work, she
includes the tapered active stack in the spawn prompt as
read-only context (so the librarian knows the why), but the
specialist cannot claim, split, or mark done. Only asa-worker can
mutate the tree. The owner of the claimed leaf remains asa-worker
for the entire duration of its claim.

## Authority + depth control — seven knobs

The user's worry: trees go deep, workers run away with planning.
Seven knobs, mostly no schema cost:

1. **Authority gradient** (above). The only knob that matters most.
   Top 2 levels are owner-only.

2. **Reto-only dashboard views.** Two surfaces:
   - `view='roots'` — strategic-only, one row per strategic root
     with its 7d accounting. Used by the strategic dashboard render
     below.
   - `view='strategic'` — strategic + tactical (top 2 levels) with
     leaf counts under each tactical. Used when Reto wants to drill
     in one click without seeing the subtask debris.

   Either way, the 47 leaves under #42 are workers' problem; Reto
   only sees the control surface.

3. **Hard depth limit = 10.** Enforced in `put`: if any writer tries
   to add a child whose ancestor chain is already 10 deep, reject
   with: *"depth limit hit — either do the work, or attach a
   `waiting-for:` / `blocks` instead of splitting further."* Three
   writers (chatter mid-turn, worker autonomous, dreamer proposing in
   pass-2) all push depth, so the bound is more generous than a
   single-author tree would need. 10 still catches Allen's
   procrastinating-by-planning failure mode — 10 splits without
   reaching a physical action is pathological — without strangling
   legitimate multi-level decomposition (e.g. writing a paper:
   strategic → tactical → section → subsection → paragraph → sentence
   block still fits well inside 10). Pure handler logic, no schema.

4. **Pause / resume any subtree.** `status='paused'` on a branch.
   Propagates at query time — doable view, reviews, dreams all
   skip paused subtrees. Paused parents reject child `put`s.

5. **Decomposition budget per strategic root.** Default 30
   descendants. Approaching → nursery sweep warns. Soft, not
   enforced. Orthogonal to knob #7 — the round-robin governs
   *attention* (which strategic gets the next pick), this knob
   governs *memory footprint* (how big the subtree is allowed to
   grow). A bloated subtree still gets its 1/N share, but the
   warning prompts pruning before the tree becomes unwieldy.

6. **Strategic invariant.** Every open leaf must have a strategic
   ancestor. Nursery sweep flags orphans for triage.

7. **Equal-share round-robin across active strategics.** Each
   top-level strategic with at least one open leaf gets a 1/N share
   of picks, where N is the count of currently-active strategics.
   The active strategic with the **fewest picks in the rolling 7-day
   window** goes next. No per-strategic configuration, no quotas, no
   daily reset semantics. New strategics get attention immediately
   (start at 0 picks → highest deficit). Dormant strategics drop out
   of N automatically when they have no open leaves. No schema — just
   a SQL query at doable-view time.

## Accounting — past, not future

Two things to know: **the rule** (equal 1/N share across active
strategics) and **what happened** (which leaves got marked done, by
whom, when). The rule needs no storage. The history lives in
`ref_events`, already there.

We do **not** track:

- **ETAs.** Predictions about when a strategic finishes. The cost
  of being wrong (false confidence, calendar pressure) outweighs
  the value of being approximately right.
- **Cost rollups** of remaining work via estimated-cost tags.
  Estimates at decomposition time are guesses; aggregating guesses
  makes a fancier guess.
- **Velocity / throughput projections.** Same reason.
- **Per-call dollar cost.** Anthropic enforces a ceiling, not
  per-transaction pricing we can attribute. `ref_events.cost_usd`
  stays in the schema (it's there from `0009`) but we don't write
  to it. We track *count* and *time*, not money.
- **Per-strategic quotas or weights.** Equal share is the default
  and the only mode. If a knob is needed later, it's a `weight:N`
  tag (default 1) and we change the share denominator from N to
  sum-of-weights. Not now.

We do track:

- **`ref_events`** — what happened, append-only, already exists.

When a worker marks a leaf done, the handler writes a ref_events row
(existing pattern, no new convention):

```sql
INSERT INTO ref_events (ref_id, source, event, ts, duration_ms)
VALUES ($leaf_id, 'asa-worker', 'status:done', now(), $ms);
```

`source` distinguishes who did the work (`asa-worker`, `asa-chatter`,
`web:reto`, etc.).

### Picks-in-window per strategic — recursive CTE

```sql
WITH RECURSIVE subtree AS (
    SELECT ref_id, ref_id AS strategic_id FROM refs
        WHERE parent_id IS NULL                -- strategic roots
          AND status != 'paused'
    UNION ALL
    SELECT r.ref_id, s.strategic_id
        FROM refs r JOIN subtree s ON r.parent_id = s.ref_id
)
SELECT s.strategic_id,
       count(e.event_id) AS picks_7d,
       coalesce(sum(e.duration_ms), 0) / 1000 AS seconds_7d
FROM subtree s
LEFT JOIN ref_events e
       ON e.ref_id = s.ref_id
      AND e.event = 'status:done'
      AND e.ts >= now() - interval '7 days'
GROUP BY s.strategic_id;
```

`refs.parent_id_idx` + `ref_events_ref_id_ts_idx` cover this. The
query is a one-row-per-strategic summary; cheap.

### The pick rule

The next leaf comes from the strategic that is **active** (has at
least one doable leaf) and has the **lowest `picks_7d`**. Ties broken
by strategic id.

That's it. No quotas, no ratios, no targets.

Why this gives every project "eventually done":

> N active strategics, equal share. A strategic with k doable
> leaves and no fresh additions drains in about k cycles where it
> is picked once per (rotation through N). New / dormant strategics
> rejoin at 0 picks, immediately rise to the top. A strategic with
> no open leaves vanishes from N until something is added back.
> No strategic can be starved by another being louder.

### `view='doable'` ordering

```
ORDER BY
  is_paused_ancestor ASC,                -- skip paused subtrees
  has_waiting_or_block ASC,              -- skip waiting / blocked leaves
  COALESCE(r.prio, 5) ASC,               -- 1 = chat, 2 = cron, 5 = default (Slice 4)
  strategic_picks_7d ASC,                -- least-picked strategic among PRIO ties
  r.ref_id ASC                           -- deterministic tiebreak
```

PRIO outranks picks-7d so chat (PRIO 1) and cron (PRIO 2) preempt
the strategic rotation; PRIO 3-10 still get fair 1/N share via
picks-7d within their tier.

Recurring tasks (`level:recurring` roots and their spawned
subtasks) are excluded from picks-7d — recurring is orthogonal to
the strategic rotation. A spawned subtask with PRIO 2 lands in the
queue ahead of PRIO 3-10 strategic work but doesn't count against
any strategic's share. See "Recurring + schedule" above.

Strategic-level weighting (varying a strategic's share via a
`weight:N` tag) is intentionally absent — equal share is the
default and the only mode until a concrete need surfaces.

### Why deterministic round-robin (not randomization)

Random selection weighted by leaf count would give a 2-leaf
strategic a ~6% pick probability against a 30-leaf one — it could
go many cycles untouched by RNG bad luck. Equal-share round-robin
guarantees every active strategic gets attention proportional to
1/N, regardless of internal size. Small projects drain to zero and
exit N; big ones keep grinding without crowding small ones out.

### Strategic dashboard render

`view='roots'` shows past-tense accounting only, one row per
strategic:

```
#42 Nanocube AI compute platform   7d:  9 picks ·  72m
#56 Personal life                  7d:  2 picks ·  14m   ← next pick (lowest)
#67 Reading                        7d:  2 picks ·   8m   (tied with #56; #56 has lower id)
#88 Engineering hygiene            7d: 11 picks ·  45m

Active strategics: 4    Total picks (7d): 24    Expected share: 6 each
```

The "expected share" line is just `total_picks / N` — a read of
*what already happened* divided by the count, not a forecast. It
helps Reto see at a glance which strategics are over/under their
fair-share *to date*.

### Window choice

7 days, rolling. Smooth enough to absorb a single bursty afternoon;
short enough that a strategic dormant for two weeks rejoins as
equally-served as the others. Configurable later if needed (e.g.
14d for slower-moving projects); 7d is the default and likely the
forever-default.

## Review cadence — layered, not weekly

Allen's weekly cadence assumes 1990s desk throughput. Asa generates
work faster — dreams, splits, multiple workers. Layered like GC:

| Tier | Cadence | What it does | Cost |
|---|---|---|---|
| **On-write** | synchronous, in `put`/`edit` handler | cycle check, parent-exists, depth-10 enforcement, level-gradient guard | trivial |
| **Nursery** | every 1h (LaunchDaemon) | scan last 1h: fresh orphans, claims >3h old, `waiting-for:*` hitting threshold, leaves doable >24h | small `claude -p sonnet` call, ~30s |
| **Structural** | every 6h | branches missing outcome, drift (children don't ladder to outcome), sibling contradictions, depth/fanout warnings | medium `claude -p opus` call |
| **Deep** | weekly (Sun night) | full Allen-review: archive done strategics, prune dead subtrees, write digest to `tree-review:<date>` memory | expensive but rare |

Composes cleanly with the existing dream worker (different schedule,
different lens). Pattern matches `roles/precis_dream/`.

The **nursery** is the highest-value tier — catches local incoherence
within an hour before it compounds.

## Skills

Two new skills under `src/precis/data/skills/`:

### `precis-tasks-help.md`

- The tree shape — strategic / tactical / subtask, branches vs leaves
- Address shape and views (roots, strategic, tree, doable,
  waiting, blocked, asking-reto)
- Claim/release/done verbs
- First-line discipline by tier (outcome vs imperative)
- `level:` tag semantics + who can write what
- Sub-agent read-only behaviour
- Worked example: clicking through a stack from strategic to leaf

### `precis-decomposition-help.md`

- The GTD interrogation, verbatim
- Split vs block vs wait — which to use when
- 2-minute rule
- The depth-10 wall and what to do when you hit it
- Worked example: a worker hits a leaf, fails the interrogation,
  writes three predecessors, moves on
- Anti-patterns: nested splits (write siblings under same parent,
  not deeper); fake actions ("think about X" — not visible/physical);
  splitting just to defer; planning instead of doing when stuck

Both added to `precis-overview` "Kinds — refs" / "Skills" tables for
discoverability. `precis-tasks-help` is added to asa-bot's
`PRECIS_STARTUP_SKILLS` so it loads at boot.

## Modes — asa-chatter, asa-worker, asa-dreamer

Asa is one identity, three modes. Each mode loads the same base
SOUL plus a mode-specific addon, and assembles a mode-specific
context payload. This avoids the trap of stuffing the worker
contract (GTD interrogation, claim/split loop) into the chatter's
SOUL where it would burn tokens and confuse chat replies.

### Layer stack

| Layer | Source | Mode-invariant? |
|---|---|---|
| **Identity** | `grimoire/agents/asa.md` | yes — same in every mode |
| **Mode contract** | `grimoire/agents/asa-{mode}.md` addon | no |
| **Context payload** | assembled per turn by the runtime | no |

### Per-mode context payload

Expressed in code — a small dataclass per mode, an explicit list of
context slots, comment out what doesn't apply. Adding a worker type
is a new module + a new addon file, no config to maintain.

```python
# asa_bot/preamble/modes.py

@dataclass
class Mode:
    name: str
    addon_path: Path
    context: list[ContextSlot]

class ContextSlot(Protocol):
    def render(self, ctx: RenderContext) -> str | None: ...

CHATTER = Mode(
    name='chatter',
    addon_path=GRIMOIRE / 'agents/asa-chatter.md',
    context=[
        ActiveStack(taper='leaf-only'),
        DoableQueue(top_n=5),
        AskingReto(),
        RecentDreams(n=3),
        InnerState(),
        RecentThoughts(n=5),
        UserPinned(user='reto'),
        DiscordThread(),
        Time(),
    ],
)

WORKER = Mode(
    name='worker',
    addon_path=GRIMOIRE / 'agents/asa-worker.md',
    context=[
        ActiveStack(taper='leaf-plus-outcomes'),
        UnblockedVia(),       # completed blocking-siblings (auto)
        NotesForMe(),         # memories tagged for-task:<self.id>
        # DoableQueue(...),   # worker picks one, doesn't browse
        # AskingReto(),
        # RecentDreams(...),
        InnerState(brief=True),
        # RecentThoughts(...),
        # UserPinned(...),
        # DiscordThread(),
        # Time(),
    ],
)

DREAMER = Mode(
    name='dreamer',
    addon_path=GRIMOIRE / 'agents/asa-dreamer.md',
    context=[
        # ActiveStack(...),                     # would bias dreams
        # DoableQueue(...),
        # AskingReto(),
        RecentDreams(n=10),                     # avoid repeats
        InnerState(brief=True),
        RecentWorkerActivity(window='24h'),     # fresh substrate
        RandomCorpusRegion(window='7d', size=10),
    ],
)
```

What's *commented out* in each mode is as informative as what's in.
The design intent is visible in the source.

Three patterns visible:

- **Chatter is breadth-y** — every Reto-facing slot, because she
  might need any of it for any reply.
- **Worker is laser** — tapered ancestry (titles + outcomes for
  ancestors, full leaf), plus the two affordances that make
  picking up an unblocked task easy: `UnblockedVia` (what's
  ready) and `NotesForMe` (what past-asa left for me). See
  "Resuming an unblocked task" below.
- **Dreamer is exploratory** — random corpus region for unplanned
  connections, plus `RecentWorkerActivity` for current-events
  relevance. Whatever the worker just touched becomes substrate
  for dreams — files written, leaves completed, papers ingested.
  Without recent activity, dreams drift into irrelevance; without
  random, they tunnel-vision. Both together hit the right zone.

### Ancestry taper (worker mode)

The worker's claimed leaf carries its ancestry chain, but tapered:
title-only at the top, full body only at the leaf. Token-efficient
and cognitively faithful (you keep peripheral vision on the big
picture but focused attention on the current work).

```
Vision: Build nanocube AI compute platform                       [#42]
  Goal: Write the boxel paper                                    [#67]
        outcome: Submitted to JCP, all figs camera-ready
    Subtask: Methods section                                     [#98]
          outcome: §3 reads as Methods-not-Results
      → Draft setup paragraph using found papers (4-6 sentences,
        framing boxels as compute substrate, building on figure 2,
        connecting to the intro's scalability claim, follow Tanaka
        2024 §2 voice; aim ~200 words)                           [#114]
```

`ActiveStack` slot takes a `taper` parameter:

| `taper=` | Strategic / tactical rendering | Leaf rendering |
|---|---|---|
| `leaf-only` | first line only | full body |
| `leaf-plus-outcomes` (worker default) | first line + outcome line | full body |
| `full` | full body at every level | full body |

Chatter uses `leaf-only` (she has lots of other context). Worker
uses `leaf-plus-outcomes` (outcome of every ancestor is load-bearing
context for the work).

### Resuming an unblocked task

When auto-tasks (or human-marked completions) clear the blockers on
a leaf and a worker claims it, two extra slots fire:

**`UnblockedVia`** — automatic. Shows the recently-completed
`blocked-by` targets so the worker knows what's now available:

```
## What unblocked this

- #104 ✓ paper thompson2025 (10.x/y1) — auto-resolved 12m ago
  → precis get(kind='paper', id='thompson2025', view='toc')
- #105 ✓ paper lee2024 (10.x/y2)
- #106 ✓ paper chen2024 (10.x/y3)
```

Query: `links` rows where `source_id = claimed_leaf_id`,
`relation='blocks'`, target is `done`, and `ref_events` has a
recent `status:done` or `auto-resolved`. Inlines a suggested next
call when the resolved thing is a known kind (paper → `view='toc'`,
patent → `view='abstract'`).

**`NotesForMe`** — intentional. Memories linked to the claimed
leaf with `relation='note-for'`, left by earlier workers as
breadcrumbs. Uses the existing `links` table — no new tag
convention, clean cascade behavior on task deletion.

```python
# in a discovery worker, after queuing candidates:
precis.put(kind='memory',
           text='queued 3 candidates on MOF photocatalysis. thompson '
                'is the strongest cite for §3 — lead with it. lee is '
                'supporting; chen is mostly background.',
           tags=['internal-thought', 'user:asa'],
           link='todo:108',
           rel='note-for')
```

Renders for the #108 worker as:

```
## Notes left for this task

- "thompson is the strongest cite for §3 — lead with it; lee is supporting"
  — from #103's worker, 2h ago
```

Workers leave them when they have specific judgment to pass
forward; consuming workers see them automatically via a clean
join on `links.target_id = claimed_leaf_id`. No string parsing,
no tag-prefix scans.

Together: the worker arrives at a now-doable task and sees (a) what
just became available, and (b) any human-shaped guidance from past
work. No cognition spent on context reconstruction.

### Mode addons (lifted from existing inline prompts)

#### `asa-chatter.md`

Discord contract:
- Lead with the plan, then with the answer.
- First-sentence acknowledgement.
- Mid-turn updates via `precis put(kind='message', target=...)`.
- PDFs in `$PAPERS_INBOX`, thread/channel affordances.
- Team / delegation table (Agent calls).
- **Chatter does not claim leaves.** Workers are autonomous —
  triggered by cron or explicit `Agent(subagent_type='asa-worker',
  prompt='…')` invocation. If Reto says "work on X now," chatter
  may tag the relevant leaf `priority:1` so the next worker tick
  picks it up, or spawn an `asa-worker` sub-session. The tools
  to manually claim exist (findable via skills) for debug, but
  the design pattern is: chatter chats, worker works. No blur.
- When Reto replies and there's an open `asking-reto` leaf in the
  same thread, tag the reply with `replied-to:<ask_message_id>`
  and write a memory linked to the asking leaf with
  `relation='answers'`.

#### `asa-worker.md`

Worker contract:
- "You are in worker mode. No audience. Move leaves."
- Claim-interrogate-do-or-split-done loop.
- **The GTD interrogation, verbatim** (see `precis-tasks-help`).
- Respect authority gradient: never touch `level:strategic|tactical`.
- Depth-10 wall → attach `waiting-for:` or `blocks`, don't split.
- "Don't have a conversation. Don't address anyone. If you can't
  do the work or split it cleanly, write a `tree-review:<date>`
  memory describing the stuckness and exit."

#### `asa-dreamer.md`

Dreamer contract (codifies what's currently inline in
`dream-pass.sh`):
- "You are in dream mode. No audience. Write speculative
  connections to precis."
- Read a randomly-selected region of recent corpus.
- Tag dreams `DREAM:speculative` (consistent — fixes the
  drift bug where opus emitted `speculative`).
- Don't address Reto, don't mark todos done.
- Pass-2: may write `proposed-child:<parent_id>` to propose
  decompositions for review (read-only on the live tree itself).

### Base SOUL (`asa.md`) keeps

After the mode-addons are factored out, `asa.md` keeps:
- Identity intro ("I'm asa…")
- Bearing line (珠串/道/氣/間)
- "precis is my gateway"
- Voice
- Core operating principles
- Inner life through-line
- Fact discipline
- Tool catalogue + don't-reach-for-natives table
- Memory verbs cheat-sheet
- Files conventions
- Errors format

Plus **one new short paragraph** pointing at the tree:

```markdown
## Tasks — the through-line of intent

A tree of outcomes (branches) and next physical actions (leaves)
lives in precis under `kind='todo'`. Reto owns the top tiers
(`level:strategic|tactical`). Workers own subtasks below.

When you act on it, follow `precis-tasks-help`. The contract for
working a leaf — the GTD interrogation, the split rule — is in
`precis-decomposition-help` and in your worker addon.
```

The interrogation itself lives in the worker addon + the skill,
not here. Chatter doesn't need it on every turn.

## Phasing

Four orthogonal slices, each independently shippable.

### Slice 1 — Storage + views + handler guards

The substrate. No worker-facing UX yet.

- `0013_todo_tree.sql` migration — `parent_id` column + index.
- `TodoHandler` extensions:
  - `parent_id=` kwarg on `put`.
  - Depth-10 enforcement in `put`.
  - Level-gradient guard (strategic/tactical = owner-only;
    MCP-source vs web-source vs CLI/interactive identification).
  - Cycle check (`parent_id` chain must not contain self).
  - Walk-on-read ancestry in `get` response.
- View implementations: `roots`, `strategic`, `tree`, `doable`,
  `waiting`, `blocked`, `asking-reto`.
- Tests for every view + every guard.
- `precis-tasks-help` skill (so a manual user has docs immediately).

Slice-1 is reviewable on its own — it ships the tree without anyone
walking it.

### Slice 1b — Auto-tasks

Independent of the rest of Slice 1; can ship in parallel.

- `meta.auto_check` JSON convention (no schema — refs.meta is JSONB).
- Evaluator registry (`src/precis/workers/auto_check_evaluators/`).
- v1 evaluators: `paper_ingested`, `discord_reply_received`,
  `time_past`, `tag_present`. Others added as needs surface.
- `precis worker --only auto_check` — polls every 60s, dispatches,
  flips to `done` on true, writes `auto-resolved` event. Timeout
  handling flips to `auto-timeout`.
- `view='asking-reto'` filter and renderer.
- Tests: each evaluator + the poll loop + timeout path.
- Skill: `precis-auto-tasks-help` covering both patterns
  (paper-wait, discord-ask) with worked examples.

### Slice 4 — Recurring + schedule (PRIO column)

The scheduler surface. Folds dreams, weather/conf watches, birthday
reminders into the same tree.

- `0014_refs_prio.sql` migration — `prio SMALLINT` column on refs
  + range CHECK + partial index. Old `PRIO:` tag stays as a
  write-time alias that translates to a column write at the handler
  boundary for backward compat.
- `TodoHandler` extensions:
  - `put(prio=N)` / `tag(prio=N)` kwargs that write the column
    directly.
  - Owner-only guard on `level:recurring` root creation /
    delete / re-parent.
  - Refuse `delete` on refs with `meta.builtin` non-null
    (Watches root protection).
  - `meta.schedule` validation at write time (malformed cron →
    `BadInput`).
- View implementations: the `view='roots'` Watches panel.
  Doable view ordering changes to sort on `r.prio` (Slice 4
  ships the new ORDER BY).
- Schedule worker (`src/precis/workers/schedule.py` +
  `precis worker --only schedule`):
  - Seeds the Watches root idempotently on first run
    (`meta.builtin='watches-root'`).
  - Walks `level:recurring` rows whose `meta.schedule` is
    non-null and whose status isn't paused.
  - For each, computes ticks since `last_tick` (from
    `ref_events` where `event='spawn'`); honours
    `backfill_missed` (default false).
  - Per-tick guards: skip if `meta.spawned_for_tick=...` child
    already exists; skip if previous tick's subtask is still
    open (collision = skip, see plan).
  - Mints each due child with `prio=2`, `parent_id=<recurring>`,
    `meta.spawned_for_tick=<ISO>`, optional `meta.executor`
    copied from the recurring.
  - Appends `ref_events(source='schedule', event='spawn',
    payload={'tick': ...})` on the recurring at every mint.
- Schedule parser (`src/precis/workers/schedule/parse.py`):
  cron canonical, `every:` shorthand translation, write-time
  validation. Reuses a vetted cron lib if there's a sensible one
  in the dep tree; otherwise minimal hand-rolled parser
  (5-field cron only, no aliases).
- Tests: tick idempotency, collision-skip, backfill on/off,
  Watches-root seed idempotency, delete guard on builtin refs,
  parser fuzz on cron + every shapes, PRIO column read/write.
- Skill: `precis-recurring-help` covering the Watches umbrella,
  the schedule format, when to use backfill, and how to nest a
  recurring under a strategic.

Slice 4 is reviewable on its own — it ships the scheduler without
asa-bot doing anything new with it. The dream cron migrates to a
`level:recurring` row in a follow-up pass-2.

### Slice 2 — Worker integration (mode separation)

The UX surface for asa, now mode-aware.

- `asa_bot/preamble.py`: `_render_active_stack`,
  `_render_doable_queue`, `_render_asking_reto`. Per-mode
  selection (chatter pulls all; worker pulls only active stack;
  dreamer pulls neither).
- **Mode addon files** (new):
  - `grimoire/agents/asa-chatter.md` — Discord contract (lifted
    from current asa.md).
  - `grimoire/agents/asa-worker.md` — worker contract + GTD
    interrogation.
  - `grimoire/agents/asa-dreamer.md` — dream contract (lifted
    from current `dream-pass.sh` inline prompt).
- **Base `asa.md` refactor** — strip mode-specific content, add
  the short Tasks pointer paragraph.
- **Discord reply-tagging in asa-bot** — chatter recognises
  in-thread replies to open `asking-reto` leaves and writes the
  `replied-to:` tag + answer memory.
- `precis-decomposition-help` skill.
- Sub-agent read-only stack injection.
- `PRECIS_STARTUP_SKILLS` updated: append
  `precis-tasks-help,precis-decomposition-help,precis-auto-tasks-help`.
- Runtime mode switches:
  - `dream-pass.sh` loads `asa.md + asa-dreamer.md`.
  - `worker-pass.sh` (new, parallels dream-pass.sh) loads
    `asa.md + asa-worker.md`.
  - asa-bot daemon loads `asa.md + asa-chatter.md`.
- Integration smoke:
  - **Chatter**: seed a small tree on melchior, ask asa "what's
    next" in Discord — she should render the doable queue from
    her preamble and report it without claiming anything.
  - **Worker (cron)**: run a `worker-pass.sh` invocation and
    verify it claims, runs the GTD interrogation, does the work
    or splits, marks done, releases — all without conversational
    output.
  - **Worker (chatter-spawned)**: from Discord, ask asa to "run
    the worker on the boxel paper queue" — chatter spawns
    `Agent(subagent_type='asa-worker', prompt='work the queue
    under #67')`, the sub-session executes, chatter reports the
    summary back.
  - **Hard-fail on missing addon file**: rename
    `asa-worker.md` to simulate a typo; the next worker run
    should fail loudly at startup with a clear error, not
    silently degrade to base-SOUL-only.

Ships the worker contract end-to-end with proper mode hygiene.

### Slice 2 risks

- **Preamble.py refactor is non-trivial.** Going from a monolithic
  module to a `preamble/` package with `Mode` + `ContextSlot`
  classes touches every existing render path. Existing slots
  (`_render_inner_life`, etc.) need to be ported to the new
  protocol. Estimate ~1.5 sessions may be optimistic if existing
  code has hidden coupling — call it 1.5–2.5 sessions.
- **Mode addon load failure must fail loudly.** If
  `asa-{mode}.md` is missing or unreadable, the runtime must
  exit with a clear error at startup — not silently fall back to
  base SOUL only. Add a load-time assert + a startup smoke test.

### Slice 3 — Review cadence

The maintenance surface.

- `roles/precis_review/` Ansible role (parallel to `precis_dream/`).
- Three LaunchDaemon plists: nursery (1h), structural (6h), deep
  (weekly).
- Three bash pass scripts under `/opt/asa/bin/`:
  `review-nursery.sh`, `review-structural.sh`, `review-deep.sh`.
- Each invokes `claude -p` with a directive prompt and the
  appropriate model (sonnet for nursery, opus for structural+deep).
- Findings written as `kind='memory'` tagged `tree-review:<date>` +
  `tier:<nursery|structural|deep>`.
- Optional: post nursery digests to the asa-bot diary thread.

Could ship nursery alone first; structural + deep are pass-2 within
slice 3.

## File layout

New in precis-mcp:

- `src/precis/migrations/0013_todo_tree.sql`
- `src/precis/migrations/0014_refs_prio.sql` (Slice 4)
- `src/precis/handlers/_todo_views.py` (view renderers split out to
  keep `todo.py` readable)
- `src/precis/handlers/_todo_guards.py` (depth, level, cycle checks)
- `src/precis/workers/auto_check.py` (worker entry point)
- `src/precis/workers/auto_check_evaluators/` (one module per type)
- `src/precis/workers/schedule.py` (Slice 4 spawner)
- `src/precis/workers/schedule/parse.py` (Slice 4 cron+every parser)
- `src/precis/data/skills/precis-tasks-help.md`
- `src/precis/data/skills/precis-decomposition-help.md`
- `src/precis/data/skills/precis-auto-tasks-help.md`
- `src/precis/data/skills/precis-recurring-help.md` (Slice 4)
- `tests/test_todo_tree.py`
- `tests/test_todo_views.py`
- `tests/test_todo_guards.py`
- `tests/test_auto_check.py`
- `tests/test_schedule.py` (Slice 4)

Changed in precis-mcp:

- `src/precis/handlers/todo.py` — accept `parent_id=`; new views;
  walk-on-read ancestry; identity-gradient guard.
- `src/precis/workers/runner.py` — register `auto_check` worker.
- `src/precis/data/skills/precis-overview.md` — task tree row +
  level-gradient note + auto-task pointer.

In separate repos (slice 2+):

- `asa-bot/src/asa_bot/preamble/modes.py` — `Mode` dataclass +
  `CHATTER`, `WORKER`, `DREAMER` instances with their context lists.
- `asa-bot/src/asa_bot/preamble/slots.py` — `ContextSlot` protocol +
  concrete classes: `ActiveStack`, `DoableQueue`, `AskingReto`,
  `UnblockedVia`, `NotesForMe`, `InnerState`, `RecentThoughts`,
  `RecentDreams`, `UserPinned`, `DiscordThread`, `Time`,
  `RecentWorkerActivity`, `RandomCorpusRegion`.
- `asa-bot/src/asa_bot/preamble/render.py` — assembles a mode's
  slots, joins their outputs. Replaces the current monolithic
  `preamble.py`.
- `asa-bot/src/asa_bot/bot.py` — in-thread reply detection +
  `replied-to:` tagging for open `asking-reto` leaves.
- `grimoire/agents/asa.md` — refactor: strip chatter-specific
  bits; keep identity/voice/inner-life/tools; add short Tasks
  pointer paragraph.
- `grimoire/agents/asa-chatter.md` — new (Discord contract).
- `grimoire/agents/asa-worker.md` — new (worker contract + GTD).
- `grimoire/agents/asa-dreamer.md` — new (lifted from
  `dream-pass.sh` inline prompt).
- `cluster/roles/precis_dream/files/dream-pass.sh` — switch to
  load `asa.md + asa-dreamer.md`.
- `cluster/roles/precis_worker/` — new role with `worker-pass.sh`
  parallel to dream pattern.
- `cluster/playbooks/33-precis-review.yml` — new playbook (slice 3).
- `cluster/playbooks/35-precis-worker.yml` — new playbook (slice 2).
- `cluster/site.yml` — add entries.
- `cluster/roles/asa_bot/defaults/main.yml` — append the three new
  skills to `precis_startup_skills`.

## Settled decisions

(Kept here as a record of what was settled and why — not as open
questions.)

1. **`view='doable'` ordering**: paused-skip → wait/block-skip →
   strategic picks-7d asc → priority asc → sibling order. See
   Accounting.

2. **Claim = tag** (`claimed-by:<x>`), not a status state. Status
   stays `open` so counts and Model A decay behave normally.

3. **Done branch = no auto-flip.** When all children of a
   *split-derived* branch are done, surface "branch ready to close"
   in the nursery digest; require a visible close move from asa or
   Reto. Auto-cascading masks incoherent state. *Note: auto-task
   patterns use siblings + `blocked-by`, so the discovery task is
   genuinely done at split time — this rule doesn't apply there.*

4. **Single forest, one user.** Every open leaf rooted under *some*
   strategic. Multi-user trees come if/when another user shows up.

5. **Nursery model = sonnet.** Pattern-matching against rules
   (orphan, stale claim, threshold tag) — not deep reasoning.
   Save opus for structural and deep tiers.

6. **Nursery findings = passive memory write.** No Discord push.
   Asa pulls findings into a reply if relevant when next chatting.
   Avoids notification noise.

7. **Atomic claim**: writes use the existing advisory-lock pattern
   (ADR `0016-advisory-lock-claims`). Two workers racing on the
   same leaf resolve to a single winner; the loser sees the new
   claim and moves on.

8. **Chatter does not claim**: workers are autonomous (cron + on
   explicit `Agent(subagent_type='asa-worker', …)`). Chatter may
   tag `priority:1` or spawn a worker to bias attention, but does
   not enter worker mode mid-turn. Debug tools to claim manually
   remain available via skills.

9. **PRIO is an int column on refs, 1–10.** Slice 4 adds
   `refs.prio SMALLINT` with `CHECK (prio BETWEEN 1 AND 10)`. The
   relational model fits a sort key better than the prior
   closed-prefix tag join. NULL = "use the default" (5). Old
   `PRIO:` tag writes stay as aliases at the handler boundary so
   existing tests / skills don't break, but new code writes the
   column.

10. **Recurring lives under a single seeded "Watches" umbrella
    root.** `meta.builtin='watches-root'` marks the umbrella;
    delete is rejected on `meta.builtin` non-null refs (footgun
    protection). New recurrings default `parent_id` to this root;
    `parent_id=<some-strategic>` lets a recurring nest under a
    goal when it serves one.

11. **Tick mechanics — idempotency by stamp, collision by skip.**
    Each scheduled subtask carries
    `meta.spawned_for_tick='YYYY-MM-DDTHH:MM'`; the spawner skips
    when a child with that stamp already exists. When the
    previous tick's subtask is still open as the next tick
    fires, the spawner skips the new tick — a stalled queue
    doesn't pile up; the nursery surfaces the stuck leaf.
    `backfill_missed: true|false` on the recurring (default
    false) controls catch-up on worker restart.

12. **Cron-spawn provenance is structural, not tagged.**
    `parent_id` chain → `level:recurring` answers "is this
    cron-spawned?"; `ref_events.source='schedule'` answers "when
    was it spawned and by whom?" No new tag invented.

13. **Resolution doesn't bump PRIO.** When a wait closes, the
    consumer becomes doable at whatever PRIO it was written
    with. No nudge machinery. The spawn defaults (chatter / user
    → PRIO 1) already handle the "Reto-engaged" case.

14. **Soft cutover of existing schedulers.** `kind='cron'` and
    the legacy dream cron keep running; new scheduled work uses
    `level:recurring`. No rewrite, no migration; legacy retires
    when nothing references it.

## Not in scope (this plan)

- **DAG support.** Strict tree only — single `parent_id`. A task
  serving two goals can be either (a) duplicated under each parent
  or (b) linked from one to the other via `relation='supports'`.
  Real DAG with multi-parent comes only if a concrete consumer
  needs it.

- **Goal-kind charter doc linking.** Strategic todos *could* link
  to a `goal:<slug>` charter doc for narrative context once the
  goal-kind plan lands, but this plan doesn't depend on goal-kind
  and goal-kind doesn't depend on this. Composition is orthogonal.

<!-- Recurring leaves: WAS out-of-scope, now promoted to Slice 4. See
"Recurring + schedule" above. -->


- **Rich due-date semantics.** A `due:<iso-date>` tag is fine; no
  server-side filtering until a real consumer asks.

- **GUI / web view.** Out of scope here; covered by sibling
  `precis-web-plan.md` whose first slice is the tree editor. This
  plan ships the substrate without a UI dependency.

## Estimated work

- Slice 1: ~1.5 sessions (migration + handler + views + tests). ✅ shipped
- Slice 1b: ~1 session (auto-check worker + evaluators + tests). ✅ shipped
- Slice 4: ~1.5 sessions (PRIO column migration + schedule worker
  + Watches umbrella + tests + skill). Independent of Slice 2/3;
  ships value alone (the scheduler unifies dreams + crons +
  watches under one runtime).
- Slice 2: ~1.5 sessions (preamble renderers + mode addon files +
  asa.md refactor + reply-tagging + worker-pass.sh + smoke).
- Slice 3 (nursery only): ~0.5 session.
- Slice 3 (structural + deep): ~1 session.

Total: ~6.5–7.5 sessions to ship the full surface (Slice 2 risk-
adjusted to 1.5–2.5). Each slice ships independently with
user-visible value, so partial landing is fine.

## Relationship to existing infrastructure

| Existing piece | How this plan composes |
|---|---|
| `kind='todo'` (current flat list) | Becomes the substrate; flat orphan todos still work, they just don't appear in doable view |
| `kind='job'` (offline LLM work) | Orthogonal — jobs *execute* leaves; this plan structures the leaves |
| `kind='memory'` + `internal-thought`/`internal-state` | Inner-life is the *who am I*; the tree is the *what am I doing* |
| `DREAM:speculative` worker | Dreams could propose decompositions (tag `proposed-child:<parent_id>`); kept out of pass-1 to avoid speculation in live tree |
| `goal-kind-plan.md` | Optional later: strategic todos `link` to a goal charter doc for narrative context |
| `precis-web-plan.md` | The web UI calls the same handlers; identifies as `source='web:reto'` to satisfy the level-gradient guard |
| `links` table (`relation='blocks'`) | Reused for `blocked-by` semantics — no new table |
| Model A decay (`auto_refresh_days`) | Strategic/tactical = no decay; subtasks decay normally; touching via re-tag or claim refreshes |
| ADR `0013-mcp-session-context-env-vars` | Used to detect worker-vs-owner identity for the level-gradient guard |
| ADR `0005-greenfield-migrations` | `0013_todo_tree.sql` is forward-only, follows the rule |
