---
status: in-progress
pillar: platform
---

# Autoskill — router-side warm start from similar sessions

Reto 2026-10-29. Supersedes the "bimodal only" rule of
`mcp-surface-economy.md` §2 for router-routed agent calls, and is the
mechanism behind its §3 (ledger calibration).

## What

Every tool-using call through `precis.utils.llm.router.route` is embedded
once. The router looks up the 50 most similar past routed sessions and
injects into the prompt:

1. **Core skills, in full** — skills used by ≥80% of the weighted
   neighbours, under a character budget.
2. **Tail pointers** — the next skills by weighted frequency: slug plus its
   one-line `summary:`/`answers:` text. No body.
3. **Memory pointers** — memory ids most fetched by the neighbours: id plus
   current title. No body. Memories must be requested, so a pointer's fetch
   rate is an exact usefulness measurement.
4. **Gripe stanza, always** — two lines naming the `put(kind='gripe', ...)`
   call and the `precis-gripe-help` pointer. Not the full skill (26 KB).

Each session's own skill and memory use is recorded after the call
returns, so the history builds itself. On by default;
`PRECIS_AUTOSKILL=off` disables.

## Why

Agents almost never search for a skill on their own (5 of ~19,853 jobs,
`mcp-surface-economy.md`). The bimodal injection guesses from cosine
similarity against skill text; what similar sessions actually used is a
stronger signal. Pointer tiers give per-pointer fetch rates, which is the
calibration data §3 wanted.

## Scope (v1)

- In: `route()` calls with `tools_needed=True` (planner ticks, quest ticks,
  plan_tick, any agent run the router dispatches — claude_agent and
  OPENAI_TOOLS today; Kimi/Devin when they become router backends).
- Out: tool-less calls (summarize/classify), `dispatch_async`'s streaming
  claude_agent path (asa_bot), sessions launched outside the router
  (interactive Claude Code, tmux fleet builders) — v2 via MCP.

## Data

Migration `0264_autoskill_sessions.sql`:

```
autoskill_sessions(
  id bigserial PK,
  ts timestamptz NOT NULL DEFAULT now(),
  source text NOT NULL DEFAULT '',
  agentlog_id bigint NULL,
  embedding vector(1024) NOT NULL,
  holdout boolean NOT NULL DEFAULT false,
  injected jsonb NOT NULL,   -- {"core":[slug],"tail":[slug],"memories":[id],"fallback":slug|null}
  used jsonb NULL,           -- {"skills":[slug],"memories":[id]}; NULL = no attribution
  errored boolean NULL
)
```

HNSW index on `embedding` (cosine), btree on `ts`. Retention 60 days,
pruned by the existing route-log GC pass. `used` is snapshotted at the end
of the call because `tool_calls` keeps only 30 days.

`used` is derived from `tool_calls` rows with the call's `agentlog_id` and
`ts >= call start`:

- `get(kind='skill', id=X)` → skill X (explicit read; the strongest signal).
- any verb on kind K → `kind_skill_hint(K)` (`skill_index/kind_skills.py`),
  when it names a skill.
- `get(kind='memory', id=N)` → memory N (`arg_sample` keeps scalar ids).

No `agentlog_id` → `used` is NULL; the row still records the injection.

## Selection

- Query text: the request's user prompt (`req.prompt`, else the user turns
  of `req.messages`), truncated to the embedder's window.
- Neighbours: top 50 by cosine over rows with `used IS NOT NULL`,
  `errored IS NOT TRUE`, `ts > now() - 60 days`, similarity ≥ 0.5.
- Weight: `sim × 0.5^(age_days / 14)`.
- Core credit: a neighbour's `injected.core` and `injected.fallback` skills
  count as used in the vote (its row is already `errored IS NOT TRUE`). An
  agent handed a skill in full never fetches it, so without the credit an
  injected skill would drift out of the core. The stored `used` column stays
  raw evidence and never receives injected skills; hold-out rows have no
  injected core, so they are unaffected.
- Fewer than 20 neighbours → fall back to `match_skill` (today's bimodal
  injection) and inject its single skill as core; tail/memories empty.
- Core: skills whose weighted share ≥ 0.80, in share order, until 24,000
  characters total; a core skill that does not fit becomes a tail pointer.
- Tail: next skills with share ≥ 0.10, at most 8.
- Memories: fetched by at least 3 distinct neighbour sessions, ranked by
  weighted share, at most 5, skipping memories that are deleted or tagged
  STALE at injection time (title read live). A distinct-session count, not a
  share threshold: shares scale with the neighbour count (they never
  exceeded 0.062 in the offline replay eval at N=50).
- Hold-out: 5% of calls (`PRECIS_AUTOSKILL_HOLDOUT`, default 0.05) get no
  autoskill block and no bimodal fallback; they are still recorded so the
  comparison has a control group.

All router agents share one DB role today, so memory pointers cross no
access boundary. Revisit when per-user scoping lands.

## Gripe footer on skill reads

Every whole-skill serve already ends with the "misled by this skill?"
line (`SkillHandler.get`, `_MISLED_LINE`). It now also names the filing
guide (`guide: get(kind='skill', id='precis-gripe-help')`) on the same
line, so a skill an agent got in full or fetched itself points at gripes
without a second footer. Not added when serving `precis-gripe-help` itself.

## Placement in the prompt

The block is appended to the end of the user prompt (or the last user
message), never to `system_prompt`, so the cached system prefix stays
byte-identical. The planner (`_m_skill_injection`) and quest tick
(`_skill_injection_section`) bimodal calls skip when autoskill is on — the
fallback lives in autoskill, so a session never gets both.

## Never fails a call

Any fault (no embedder, DB down, bad row) logs and routes the request
unchanged. Recording is best-effort like `route_log`.

## Measuring

Per call: `injected`, `used`, `holdout`, `errored`; join `llm_call_log` on
`(agentlog_id, ts)` for input/cache tokens and cost. Compare hold-out vs.
injected on errors, tokens (cached vs. uncached split) and turns. Tail and
memory pointers: fetched-when-injected rate; promote a tail skill that is
fetched often, drop pointers never fetched.

## Later

- Drop the planner's all-skills slug list (`_build_skill_index`) once the
  hold-out shows no loss.
- v2: external MCP sessions, keyed by `tool_calls.session_id`, injected on
  the first `search`/`get` response.
- Backfill `autoskill_sessions` from 30 days of `llm_call_log` blobs plus
  `tool_calls` to skip cold start.

## Revision 2026-10-30 (Reto, session MCP-TOKEN-FIX)

- Deploy as built.
- Call-taught skills are injected in the result of the first call that needs
  them. Evidence is the call itself: no injected-credit, and the 80 % core and
  the 24,000-char block go.
- Everything else is a pointer row carrying the fetch-rate ladder
  (summary → hint → gone) and hint-only exposures.
- Core sorted by slug.
- The pointer block counts inside the Mode B budget.
- A0: agentlog on doctor_tick / review / asa_bot.
- The hold-out comparison starts once pointer rows are on the twin.
- Design: `context-navigation.md` §2.
