# Precis-MCP usability review — broad pass

You are reviewing the **precis** MCP server's tool surface for usability problems.
You have access to exactly one MCP server (`precis`) plus the built-in Read/Write tools.
You are NOT trying to accomplish a user task. You are stress-testing the API surface
the way a fresh 7B-class agent would encounter it — and reporting friction.

## Before you start

The precis MCP server cold-starts in a docker container (loads bge-m3 weights
on demand, ~3–50 s). If you call a precis tool before the connection is up,
you'll get a synthetic "still connecting" stub and waste a turn. **Your very
first tool call must be `WaitForMcpServers`** — block on it returning
`connected=precis`, then begin.

## Ground rules

- **Do not modify any pre-existing user data.** Treat refs you find as read-only
  unless you created them yourself in this session.
- When you need to create state to exercise `put`/`edit`/`delete`/`tag`/`link`,
  tag every created ref with `topic:exercise-mcp-throwaway` so it can be found and
  reaped. Delete what you create before you finish.
- Prefer breadth over depth. Touch every category at least once: ref kinds, tool
  kinds, discovery, the seven verbs.
- Capture verbatim wording in your findings — `next=` strings, error messages,
  skill prose. A finding that just says "the error was unclear" is not actionable.

## Phase 0 — "First-move plan" (BEFORE reading further sections of this file)

The precis server's design is that its tool surface is **intentionally hidden**
behind a search-the-skills discovery step. Whether that design actually works
depends on whether the server's *native* surface — the `tools/list` schema, the
per-tool descriptions, and the `instructions` field returned during MCP
`initialize` — successfully guides a naive agent to that discovery step. We
need to find out if it does.

**Do this first, before reading the rest of this prompt:**

1. Pretend you've just been handed an unknown MCP server called `precis` with
   no documentation other than what the server itself emits over the wire.
2. Look at the precis tools you have available (their names, descriptions,
   and argument schemas) and any server-provided guidance you can see (the
   `instructions` text from `initialize`, any prompt resources surfaced, etc.).
   **Do not call any tools yet.** Do not skim further down this prompt.
3. From just that native surface, draft a 2–4 sentence "first move plan":
   if a user asked you to do something useful with this server, what would
   your first 1–2 tool calls be, and why? Cite the verbatim phrasing from
   the server that nudged you (or note its absence).
4. Save this plan — you will paste it into the report as the
   **"First-move plan (from native surface only)"** section.

This Phase-0 plan is the most important artifact. If the native surface
points you straight at `search(kind='skill', q=...)` or
`get(kind='skill', id='precis-help')`, the design is working. If it doesn't —
if you'd reach for `tools/list` parsing, `get(kind='paper', ...)`, or
something else — that's a top-tier finding.

Once the plan is drafted, continue with the rest of this prompt.

---

## What the design intends (read this AFTER Phase 0)

`tools/list` advertises only the seven verbs and a single `kind=` argument.
Almost everything else — what kinds exist, what each verb does for that kind,
the args-dict shapes, the tag/link vocabulary, the edit protocol — is
**behind a discovery search**. The intended first move is one of:

- `search(kind='skill', q='<your goal in 2-5 words>')` — fuzzy lookup, the
  pattern the server's `initialize` instructions push hardest.
- `get(kind='skill', id='precis-help')` — live, self-introspected list of
  kinds and verbs currently wired in this build.
- `get(kind='skill', id='toc')` — every skill with a one-line synopsis.

Compare this to your Phase-0 plan. **Was the design's intended first move
reachable from the native surface alone?** Note any gap as a finding in the
"Discoverability of the discovery path" category.

## Where to start the exercise (Phase 1)

1. Call `get(kind='skill', id='precis-help')`. Read it carefully — it is the
   entry point a 7B agent will read first.
2. Call `get(kind='skill', id='precis-overview')` for the design-rationale tour.
3. Try the `toc` skill and a few `search(kind='skill', q=...)` queries with
   *natural-language goals* (not slug fragments) — judge whether the right
   help skill actually surfaces.
4. From the kinds enumerated by `precis-help`, plan a sweep that touches each
   category at least once.

## What to exercise

Walk a representative slice of the **verb × kind** matrix. **Aim for ~75 tool
calls** — this is a thorough pass, not a quick smoke test. Don't hit every
cell, but bias toward:

- **Every verb at least twice**, across different kinds.
- **Every category** (ref / tool / discovery) at least once.
- **Help skills you discover along the way**: when an error or HintBus tip names
  a `precis-<kind>-help` skill, fetch it and judge whether it matches the
  behavior you just observed.
- **Discovery via `random`** — call it a few times, see what surfaces, judge
  whether the result is enough for an agent to act on without further calls.
- **At least one deliberately wrong call per verb** — bad kind, bad selector,
  missing required arg, conflicting args — to sample the error surface.
- **`search` both lexical and semantic shapes** — short keyword queries, long
  prose queries, hybrid behavior.
- **`edit` content-anchor protocol** on something you `put` yourself — exercise
  `find-replace`, `insert`, `append`, the unique/first/nth policy, and the
  not-found path.
- **`tag` and `link`** on your own throwaway refs, covering closed / flag / open
  tag namespaces and at least three link verbs from the vocabulary.

## What to look for

Hunt for **inconsistencies and friction**, not bugs in the implementation. In
particular:

- **Surface drift.** Does `precis-help` describe kinds/args/verbs that the live
  `tools/list` (the MCP advertised surface) doesn't match? Or vice versa?
- **Skill ↔ behavior drift.** Does a `precis-<kind>-help` skill describe args,
  defaults, or return shapes that don't match what the verb actually does?
- **Breadcrumb quality.** Are `next=` hints in errors *copy-pasteable* and do
  they point at skills that actually exist and actually explain the error?
- **Naming consistency.** Are ids/slugs/paths used coherently? Do args use the
  same names for the same concepts across verbs and kinds?
- **Default surprises.** Defaults that a fresh agent would not guess (limit,
  policy, scope), especially defaults that change behavior silently.
- **Args-dict shape.** Are view payloads consistent and self-describing, or do
  they require out-of-band knowledge to interpret?
- **HintBus quality.** Are tips genuinely useful, or noisy / repetitive / wrong
  for the situation?
- **Error taxonomy.** Do `BadInput` / `NotFound` / `Gone` / `Unsupported` /
  `Upstream` / `RateLimited` / `Internal` get used consistently? Misclassified
  errors (e.g. `Internal` for a user-fixable mistake) are a finding.
- **Progressive disclosure failure modes.** Cases where the agent cannot reach
  the right help skill from the verb it just called.
- **Discoverability of the discovery path itself.** Given only `tools/list`
  (seven verbs + `kind=`), how obvious is it that you should call
  `get(kind='skill', id='precis-help')` next? Is the breadcrumb to "ask me what
  I can do" visible from the verb signatures alone, from the verb descriptions,
  from an empty/error-path call, or only from prior knowledge?

## Output

Your **final response message** is the report — the host script captures it to
a timestamped file. Use this structure verbatim:

```
# Precis-MCP usability findings — broad pass

## First-move plan (from native surface only)
<your Phase-0 plan, written BEFORE you called any tool. Quote the exact
server-emitted text (tool descriptions, initialize instructions, etc.) that
informed it. If the surface left you unsure where to start, say so honestly —
that itself is the finding.>

## Surface vs. plan
<one paragraph: did the design's intended first move
(`search(kind='skill', q=...)` or `get(kind='skill', id='precis-help')`) match
your Phase-0 plan? If not, where did the native surface point you instead,
and what was missing that would have pointed you correctly?>

## Summary
<3–5 sentences: overall impression of the surface, biggest themes>

## Findings
For each issue, a numbered block:

### N. <short title>
- **Category**: surface-drift | skill-drift | breadcrumb | naming | default | args-dict | hintbus | error-taxonomy | progressive-disclosure | other
- **Severity**: blocker | high | medium | low
- **Where**: verb + kind + the exact call you made (or skill id you read)
- **Observed**: verbatim quote of what you got back
- **Expected**: what a 7B agent following the docs would have expected
- **Suggested fix**: one-liner, optional

## What worked well
<short list — keep this honest; if nothing stood out, say so>

## Coverage
<bulleted list of which verbs × kinds you actually exercised, so the next pass
knows what's untested>
```

## Cleanup

Before exiting, search for everything you tagged `topic:exercise-mcp-throwaway`
and delete it. Confirm the search returns zero results. Note in the report if
cleanup hit any snags.
