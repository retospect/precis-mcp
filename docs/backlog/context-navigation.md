---
status: draft
title: Context navigation — the twin, sets, row grammar, managed context (Mode B)
pillar: platform
prio: high
---

# Context navigation — token-efficient, effective navigation for every agent

Rulings Reto 2026-10-30 (session MCP-TOKEN-FIX), over Fable's draft of the same
day and ADR 0051 (deleted 2026-08-09; `git show 2d54543e6^:docs/decisions/0051-…`).
Thread: `threads/context-navigation.md`. Supersedes `mcp-seen-state-dedup.md`,
`fisheye-level2.md`, `server-side-session-context.md`,
`fisheye-context-eval-study.md` (deleted in the same commit; their rulings are
carried below). Builds on `block-navigation.md` (rulings 2026-10-28) and
`autoskill.md`.

## 0. Size is a meter, not an error

Shipped (gr540851): results return whole up to the 24 kB transport cap, head +
outline above it; `tool_calls.result_bytes` is the meter. Ruling that stands:
big alone is not bad — a later auto-gripe fires on "rendered at full, never
cited" (visible in the twin, §7), never on size.

## 1. Goal and measures

Goal: an agent reaches a correct answer with the fewest result bytes and hops,
with every needed block present. Spend server compute to save model work. Every
list supports a choice. Generic across kinds; the one requirement on a kind is
that every yield is addressable by a handle. Falsifiable: each change is judged
against the meters.

Meters, all read from the twin (§7):

| meter | definition |
|---|---|
| bytes-to-answer | Σ result bytes of read calls until the answer |
| hops | read calls until the answer; "turns" ≈ calls within 2 s |
| re-fetch share | a handle rendered twice in one ctx at ≥ summary extent |
| pointer fetch rate | a pointer row followed later by a body row for the same handle |
| searches-per-ctx | convergence of pointers: a search whose first hit a pointer already named |
| recall@answer | gold blocks rendered / gold blocks — eval run only |

Eval set: 40 questions, 10 each for paper, memory, skill, gripe/todo; a
small-model agent starting from TOCs; scored on correctness (judge), bytes,
hops, wrong-branch count (block fetched, never used). Baseline before targets.
Pointer injection (§2) runs from day one: search is always the fallback, so a
wrong pointer costs nothing, and searches-per-ctx should fall as pointers
converge.

## 2. Autoskill, revised

Ships as built (543814513, 65a11f476; see `autoskill.md`). Then the core block
is replaced by one rule with two classes:

- **Call-taught skills** (a handler names the skill in a refusal `next=` or in
  its kind's help) arrive in the result of the first call that needs them, then
  stub. Evidence = the call. No self-credit: `_injected_credit`, the 80 % core
  and the 24,000-char block go. Classification is a frontmatter field + lint.
- **Everything else** (memories, method/workflow skills) is a pointer row (§3)
  in the prompt. Evidence = the fetch. Ladder: summary row → hint only → gone,
  driven by fetch rate; occasional hint-only exposures test whether the summary
  is doing the work. Stats per pointer.

Core sorted by slug (byte-stable). The pointer block counts inside the Mode B
cycle budget (§8). Mode A later option: a UserPromptSubmit hook emitting
pointer rows only. The B4 thread_type registry is dropped; `match_skill` covers
the <20-neighbour case.

**A0 (shared prerequisite):** doctor_tick, review and asa_bot mint an agentlog
(gr540852); verify on prod that their new `tool_calls` rows carry `agentlog_id`
and `source` before any meter is trusted. The ratchet
`tests/test_agentlog_launch_sites.py` lists the launch sites still without one
(`PENDING`: asa_slack, diagram agent, web follow-up).

## 3. Row grammar

One table per result. A header line names columns and units once; values bare.

```
handle      kB  hits/share/rank  links  r   age  where                      what
pc5531229    4  president,radio      3  12  3mo  pa449667 §4.2 Caching …   dynamic tool results invalidate the prefix …
```

- The chip is one column, named by the header: `hits` (query labels, §5),
  `share` (pointer), `rank`.
- `links` (degree) and `r` (reads) are display-only, never rank inputs. `age`
  is time since last change; last-read is a ranking input for the novel half of
  rings only, not shown.
- `where` = owner (the ref whose toc the row lives in; what you would `scope=`
  to) + section. `what` = gloss, rung 2 of the summary ladder (title → gloss →
  summary → full); it must add to the title (render lint: drop it if more than
  half its words are in the title). `kw: a, b, c` when no summary exists yet.
- Two glyphs only: `↺` seen (minimal stub `↺ pc5531229`), `├` toc slice.
- Toc slice only where the owner has ≥ 3 sections, top-3 rows; its worth is
  tested on the eval set's wrong-branch count.
- Chunk summaries die with the chunk (`chunk_summaries.chunk_id` cascade;
  confirm the constraint survived the baseline). Ref-level summaries are
  cleared on body change. Never aged, never shown stale.
- Wrapped rows are fine; the consumer is an LLM.

Why one grammar: an agent cannot weigh a row it cannot read, and a trained skill
learns one table. Hearst's clustering-vs-facets result (predictable categories
beat computed clusters for humans) transfers as predictability; label quality,
the main complaint, is covered by keywords.

## 4. Sets: every row is a handle

`get(h)` fetches; `get(h, extent='toc')` breaks down one level. A paper breaks
into sections, a hub into members, a search result into groups, a group into
members or sub-groups. Search returns a set.

```
rs184   30 rows  61 kB  kw: caching, prefix, invalidation   pa449667~23..28 · sk234211 · me2342 · +27
```

- Handle `rs<n>`: global sequence; the counter resets at the weekly reap when
  the table is empty (no live collision possible; a week-old snapshot already
  points at nothing). Three inline members, then `+n`.
- Breakdown ladder: owner → kind → keyword cluster, stepping down when a level is
  degenerate (one group, or all singletons). Addressability is what makes
  computed clusters admissible: a group with a handle can be requested.
- Definition = (ctx, query, facet path), unique per ctx; recomputed on `get`,
  members cached for handoff and audit. Kept a week, then reaped.
- `search(q=…, scope='rs184')` searches inside a set.
- This replaces facet rest-lines, `next:` tables and all paging. The one
  allowed continuation is a named set or item list, never an ordinal. The rule
  is "no paging over unchosen lists", not "no second calls".

## 5. Multi-query and multi-get

**Fan-in.** `search(kind=K|'*', q=Q, args={'queries': {'label': 'text', …}})`
on every kind (paper has it today: `_paper_search`, 8-leg cap, RRF in
`search_merge`). Legs fused by RRF, deduped on `(ref, chunk)`, the `hits`
column shows the label set per row — "found by both" is readable. Unlabelled
lists → `q1,q2,…`. 3–5 legs. Parallel calls from one client dedupe through
atomic twin claims (insert-first per handle); no 2 s window, no `batch=`.

**Multi-get.** `get(id=[...])` on every kind (gripe only today): ranges on one
ref merged (`~4..6` ∪ `~5..8` → `~4..8`); a chunk inside a requested range is
not sent twice; a seen handle is a stub; whole items in rank order; what does
not fit renders as nested `rs` rows, never a partial body.

**Injection**, on by default: let R = requested ∪ seen; count links from R to
nodes outside R (`count_links_for_refs`, one grouped query), weighted by
`1/log(global in-degree)` so hubs stop winning by default; nodes over the
threshold are injected as toc rows only, ≤ 10 % of the budget, marked
`referenced by 3 of yours`; the closing line `referenced-but-absent: n (…)` is
the convergence meter. `inject=False` for pure fetches. Fires rarely in
practice; when it fires, that convergence is the signal.

The 3-turn protocol is then: search → get what you want → get the rest; a
converged read in ≈ 30 kB.

## 6. Two axes: size × shape

- **Size** words: `small` 4 kB, `medium` 16 kB, `large` 24 kB (transport cap).
  Numeric accepted for runners. Defaults: lists 6 kB, a chosen `get` 16 kB.
- **Shape** words: `index` (rows only), `balanced` (top items full until half
  the budget, rest rows), `fill` (bodies in rank order, rest rows). Defaults:
  list → index, chosen get → fill, set get → balanced. `shape=` overrides.
- The agent reasons in those six words, never in bytes; the budget is how much
  of its own context it is willing to spend, since a 16 kB result is ≈ 4k tokens
  carried every later turn.
- Our 24 kB transport cap stays under Claude Code's client cap
  (`MAX_MCP_OUTPUT_TOKENS`, 25k tokens) so the server, never the client, decides
  what is cut. Bytes stay the unit (≈ 3–4 B/token).

## 7. The twin and `_ctx`

One record of everything the server put into a context, one writer at
`Dispatcher.dispatch_with_status` after render:

```
context_items(ctx, seq, handle, extent, bytes, entry_kind, rendered_at, alive)
entry_kind ∈ body | outline | toc_row | pointer | stub | set | injected | receipt
```

It replaces the seen ledger, the `tool_call_refs` table, the set store and
`WorkingSet`'s `job.meta` storage. Readers: seen stubs, `rs` sets, the
compaction handoff, every §1 meter, autoskill fetch rates, the Mode B render,
rut lints. Exact for our bytes between resets; blind to what the agent typed or
read through other tools.

**`_ctx`**: a reserved key inside `args` (`args={'_ctx': {...}}`), popped by the
dispatcher before the per-kind unknown-key check. It rides in `args` because
FastMCP drops undeclared top-level keys before our code runs (hook probe
2026-10-30, gr540853 comment 1), and declaring it on every verb would put it in
the schema the model reads. Never typed, remembered or passed by a model.
Deterministic: `{session_id, agent_id or 'main'}`, plain, not hashed. Probe
results: `session_id` is constant across parent and subagents; `agent_id` is
present only inside a subagent; the hook fires under `claude -p`; parent and
subagents share one MCP session id server-side.

| source | how `_ctx` gets there | grain |
|---|---|---|
| Claude Code (CLI, desktop, `claude -p` in containers) | PreToolUse hook on `mcp__precis__.*`, `hookSpecificOutput.updatedInput`; in containers the script reads the agentlog env | per agent, subagents included |
| Router lanes (castor, hosted models via API, Mode B runners) | the router sets it | per job |
| Top-level clients in tmux (Devin, Kimi, Codex) | none; connection or bearer token | per client |
| Hosted models behind a Claude Code base-URL shim (optional) | as Claude Code | per agent |

Why a hook: P and C (parent and subagent) share one MCP client in one process;
the JSON-RPC `id` demultiplexes responses in client memory and never reaches the
server, so nothing on the wire says who called. The hook runs in the client
process with `agent_id` in hand. Verified in hooks.md / sub-agents.md:
PreToolUse fires inside subagents with `agent_id`, `agent_type`, `mcp_server`;
`updatedInput` replaces arguments; SubagentStart/Stop exist; by-name
`mcpServers` share the parent connection, inline ones open their own. Not
documented: `session_id` constancy inside subagents, PreCompact fields.
**Before building: one logging PreToolUse hook for an afternoon** to confirm
both. Protocol context (perplexity report ref 540503, unverified): no MCP
version defines an agent identity; 2026-07-28 reportedly removes
`Mcp-Session-Id` and requires explicit application handles per request —
`_ctx` is that handle, not a workaround.

Resets: PreCompact snapshots live rows and marks them dead; SessionStart(compact)
re-renders the snapshot as pointer rows, no bodies (the compaction handoff;
`compact-handoff.md` keeps the `/next` node as the intent layer); SessionStart
(clear) kills. Subagent twins start empty, forks too. Rows reaped after a week.

Deferred until the meters ask: parent→child receipts and subtree sums,
`begin`/`end` verbs for hookless orchestrators, fork inheritance, per-agent
connections.

Carried rulings: see "Carried from superseded items" at the end.

## 8. Mode B — managed context

For runners we own there is no transcript. Every cycle: render from the store →
one model turn → yields to the twin → render again. **A consumed call collapses
to its yield**: a search → toc eyes for what the model focused (query text
gone); a get → an eye at the requested extent in its structural position; a
write → one receipt line. Reto 2026-10-30: "once the agent has requested xyz,
we don't need to see the query and the request that led there any more."

Why: observation masking beats LLM summarisation at lower cost (`pa529984`);
tool results are 57 % of carried context in our sessions; prompt caching pays
only on a byte-stable prefix (`pa529987`); long contexts degrade attention
(`pa529981`).

Layout, one render per cycle:

```
[1] persona skill + pointer rows (autoskill)        stable per thread type   ← cache break
[2] logbook: pinned items only (cursor + what the agent pinned)             ← cache break
[3] working set: eyes at full / summary / toc, in structural position
[4] digest: recent cycles full, older as ledger lines
[5] seen-map: handles + sizes, no bodies
[6] expiring: what drops next cycle, countdown
[7] this cycle's request
```

[1]–[2] byte-identical across cycles; [2] changes only on a deliberate pin,
unpin or memory edit. On `claude -p` lanes the CLI places `cache_control`; byte
stability is our lever. On router lanes breakpoints go after [1] and [2].

**Crunch schedule.** Between crunches the render is byte-identical except the
appended tail (full cache hit). At a crunch (every N cycles or when over
budget) expiries, decays (full → summary → toc → gone, `†` on next-to-drop,
`◦` on inferred eyes) and the re-sort happen together, one break. Sort at
crunch by remaining lifetime, longest first: pinned, then newest; oldest nearest
the tail, so later tail-only drops keep the prefix. Keep everything until
crunch; crunch often-ish; no further tuning until a crunch is measured to cost
something.

**Budget.** 64 kB per render (≈ 16k tokens) excluding the pinned floor, tracked
per adopter; inline reads within a pass may use the transport cap because they
are discarded at re-render. The pointer block counts inside it.

**Seen-state** is the working set itself: the same twin rows with decay on.
**Generic by construction:** a kind needs only addressable yields; `extent=`
already reaches every kind's `get` via `Handler.eye`.

What exists: Level 1 (`workers/working_set.py`, `utils/working_set_render.py`,
`utils/fisheye.py`; wired in plan_tick, planner prompt, dream). Slices:

- **B0** asa preamble reorder (stable → volatile; last-turn signal and recent
  turns to the user message) + `cache_read_tokens` before/after. Independent.
- **B1** inventory of yields with no handle (`se` check report, `pcb` DRC,
  `calc`) → `jb<id>` receipts.
- **B2** `focus(handles, level)` on the MCP surface over `WorkingSet.focus`;
  `search(..., eye=)` plants a toc eye for the next cycle.
- **B3** the driver: render → `--max-turns 1` → twin → render, behind
  `PRECIS_TURN_LOOP` in `claude_agent.py`; collapse happens here.
- **B4** adopters in order: plan_tick, gripe runners, dream, doctor_tick,
  quest_tick, asa.

## 9. Cache-backed sources (youtube, perplexity-*, web, websearch, wikipedia)

1. Chunk at cache-write, mechanically: transcripts into ~1.5 kB time-stamped
   blocks; pages and reports at headings, size-split fallback. Backfill existing
   cache-backed refs (DELETE + INSERT; summaries cascade and re-derive).
2. Derive in workers (`chunk_keywords`, `chunk_summaries`) with the touched-ref
   priority lane: a `get` bumps the ref's chunks to the head of both queues.
3. Render what exists: toc + ranges + sizes immediately, `kw:` until summaries
   land, a bounded ~2 s wait. No LLM call inside the MCP request.
4. Paid kinds: fetch and cache regardless of budget (already paid), return head
   + toc. Never "paid, cached, unreadable" (gr540306).
5. LLM summaries for transcripts: yes, when the worker lane exists.

## 10. Order

1. Deploy autoskill + container fix; verify `agentlog_id`/`source` on new dream
   and plan_tick rows and `autoskill_sessions.used IS NOT NULL`; close gr537773.
2. A0 attribution on every runner.
3. Twin: table + dispatcher `_ctx` pop + Claude Code hook + reset hooks, after
   the afternoon of hook logging. Migration takes the next free number.
4. Readers: stubs, `rs` sets, compaction handoff, meters + eval bank + baseline.
5. Autoskill pointer rows on the twin; the hold-out comparison starts here.
6. Row grammar → multi-get + injection → multi-query on every kind.
7. Cache-backed chunking + backfill.
8. B0 any time; B1 → B2 → B3 (plan_tick) → B4.

Held WUs from the 2026-10-30 census (WU2 list shape, WU5 hub rings, WU7 chips +
recall slot, WU8 seen-stubs) unblock: WU2/WU7 under §3, WU5 under §3 plus the
half-star/half-novel ring rule, WU8 as the twin's stub reader.

## 11. Rings and ruts (unchanged from the draft)

A ring group of n rows takes ⌈n/2⌉ by rank and ⌊n/2⌋ least-recently-read first,
plus one `recall:` row per render. Rut lints: per hub, read concentration on the
top-8 by degree > 0.8; per corpus, `links ≥ 10` and `r0` in 60 d.

## 12. Open

- `session_id` constancy inside subagents (empirical check, §7).
- REF_BYTES trigger shape: row-level trigger on `chunks` vs statement-level with
  transition tables vs writer-side set + worker reconcile — for the Opus review.
- `compact-handoff.md`: merge into the twin reader once that reader exists.

## Carried from superseded items

Rulings quoted from the items this spec deletes, so `git log` is not the only
place they live.

### mcp-seen-state-dedup.md

- Title: Seen-state dedup — the MCP answers a repeat with a `^^` back-reference
- Reto 2026-10-22: "it keeps the MCP from re-issuing info we already sent,
  but required agent cooperation. Do we know when the agent compacts? We
  could say (^^ for see above) for things sent since start or last
  compaction."
- Reto 2026-10-22 review:
  - **A hash is enough.** The map stays `body_sha → entry` plus a second
    `handle → last body_sha`; the repeat check is one set lookup.
  - **Say "supersedes".** When a handle comes back with a different sha, the
    full body is prefixed with one line: `supersedes the copy from call #12
    (sha 1a2b3c4d)`, so the agent does not merge two versions.
  - **Invalidate by timestamp.** Any reset signal (compaction hook, a detected
    or suspected tool-result clear, `args={'fresh': True}` on any call, or an
    explicit `seen reset`) drops every entry sent before that moment, not just
    one entry. The rolling window is the same rule applied continuously.
  - **Self-describing marker, no mnemonic** (Reto agreed 2026-10-22). The marker always states what it
    is in plain words (as above), and names the skill once per window
    (`skill:precis-seen-help`). Reto floated: a verbose first
    explanation then a short tag, and a rare-token mnemonic such as `緩存` found
    via the skills.
- Open question 1, fallback if S10.0 shows `updatedInput` does not reach MCP tools: Ruled (Reto 2026-10-22): yes, approved.
- Open question 2, window (30 minutes or 40 calls per agent, whichever first): Ruled (Reto 2026-10-22): yes.

### fisheye-level2.md

- Title: Fisheye level2

### server-side-session-context.md

- Title: A per-session context selector living on the shared MCP server
- Precondition: `td458385` (every session moves onto the shared HTTP server —
  decided, Reto 2026-09-30, "supervisor and watchdog, and we move to HTTP").

### fisheye-context-eval-study.md

- Title: Fisheye-rail context-efficiency study — eval + paper (gr56588)
- Gripe pointer: "Consolidated from gripe 56588 (tags: adr-0051 fisheye research eval)."
