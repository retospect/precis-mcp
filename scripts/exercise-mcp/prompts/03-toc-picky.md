# Precis-MCP usability review — smart-TOC + cluster picky pass

You are a **ruthlessly picky senior reviewer**. Multiple rounds of
fixes have shipped on this server; you're verifying a new generation
of features and hunting for problems other reviewers missed.

You have access to the `precis` MCP server plus the built-in tools.

## Before you start

`WaitForMcpServers` first. Cold-start in docker takes ~2–5 s. Block
on `connected=precis`, then begin.

## What's new this round

Major shift since the last review: a **smart table-of-contents** built
on embedding-sequence segmentation, plus a shared address grammar
that now works on papers AND skills.

The address grammar (read `precis-toc-help` for the canonical spec):

| Form | Meaning |
|---|---|
| `slug` | the whole ref |
| `slug~N` | chunk N |
| `slug~A..B` | chunk range A..B |
| `slug/toc` | TOC of the ref |
| `slug~A..B/toc` | sub-TOC, recursively segments the range |

The TOC renderer is supposed to:

- Use H2 headings when ≥3 are present and cover ≥80 % of chunks.
- Otherwise fall back to **embedding-sequence clustering** —
  TextTiling-style segmentation into 3–9 contiguous segments.
- Render as a TOON table (`{handle\tkeywords}` or
  `{handle\theading\tkeywords}` in H2 mode).
- Append a **Schwartz-Hearst abbreviation legend** (`Abbrevs: FTIR
  (Fourier Transform Infrared), …`) for papers with detected
  acronyms, and a **shared-phrases footer** when ≥60 % of segments
  share a key phrase.
- Cache per `(ref, kind, chunker_version, embedder_name,
  SEGMENTATION_VERSION)` so repeat views are microseconds.

Search hits now render as TOON too: `{handle, chunk_keywords}` for
paper, `{slug, section, keywords}` for skill. The paper search's
`Next:` trailer should include a **cluster-context line** pointing
at the segment that contains the top hit
(`get(kind='paper', id='<slug>~A..B', view='toc')`).

The `view=` argument on paper/skill is gated: unknown values raise
`[error:BadInput]` with the per-kind accepted list.

## Picky reviewer stance

- **No benefit of the doubt.** A TOC table that *looks* fine — dig
  into whether the segment boundaries make sense, whether keywords
  per row actually disambiguate, whether the cluster trailer's
  range encompasses the top hit.
- **Quote verbatim.** "The TOC was confusing" is not a finding.
  Paste the verbatim row that confused you and say what an agent
  reading it would conclude vs the truth.
- **Run every `Next:` line you see.** If the trailer says
  `get(kind='paper', id='cai23~63..89', view='toc')`, fire it and
  confirm the sub-TOC actually segments. Self-referentially broken
  trailers (a recipe pointing at its own breakage) are BLOCKER.
- **Run heuristics on natural inputs.** Don't just probe with
  technical jargon — try `q='ecology'`, `q='topic'`, `q='link'`,
  realistic agent queries. If RAKE keywords on a TOC segment are
  paper-wide jargon that doesn't disambiguate from the next row,
  call it out.
- **Test sub-range zoom.** `get(kind='paper', id='<slug>~A..B',
  view='toc')` should recursively segment. Confirm.
- **Test the cache** — pick a paper, fire `view='toc'` twice in
  quick succession, time them. Second should be much faster
  (cache hit).
- **Watch for cross-rendering bias.** When the cluster trailer
  routes you from a search hit to a TOC, the two views should be
  internally consistent — both in TOON, same column shape,
  cluster handle in the trailer must actually exist as a row in
  the destination TOC.

## What to exercise (target: ~40 calls)

**Phase 0 (Phase-0 first move from native surface only).** Describe
in 2-4 sentences what your *first* tool call would be if you
landed on this server cold, based only on `tools/list` and the
`initialize.instructions` text. Don't read further before this.
Save as the first section of your report.

**Phase 0.5 — discoverability sweep.** Pretend you're an agent that
heard "this server has a TOC view" and needs to find out how to
use it, but knows nothing else. Using ONLY tool calls (no
out-of-band reading of this prompt or the source), trace your
path:

1. Where does the server *first* mention a TOC capability?
   (initialize.instructions? a verb description? a skill?)
2. Try `search(kind='skill', q='table of contents')` and
   `search(kind='skill', q='how do I navigate a paper')` — does
   one of these surface `precis-toc-help`? What rank?
3. If you find `precis-toc-help`, does it explain enough that you
   could immediately use the `slug~A..B/toc` form without
   trial-and-error?
4. From the TOC view output itself, does the `Next:` trailer or
   any inline line point at `precis-toc-help` when an agent
   encounters something they don't understand (e.g. the
   abbreviation legend or the shared-phrases footer)?

Document the discoverability path you actually followed as the
**Discoverability** section of the report. If the only way to
learn about the TOC machinery was the prompt-supplied hint, that's
a HIGH finding — the new feature has to be self-documenting.

**TOC machinery:**
- `get(kind='paper', id='<slug>', view='toc')` on at least 3 papers
  — pick one well-sectioned (lots of H2s), one un-sectioned
  (`newville01` is one), one large (>100 chunks). Compare layouts.
- `get(kind='paper', id='<slug>~A..B', view='toc')` for recursive
  sub-segmentation. Verify the sub-TOC actually contains the range.
- `get(kind='skill', id='precis-overview/toc')` — verify H2 mode
  on a heavily-sectioned skill.
- `get(kind='skill', id='precis-toc-help/toc')` — sub-skill TOC.
- Single-chunk drill-in: `get(kind='paper', id='<slug>~N')` — does
  it include the "Part of segment ~A..B" header?

**Search:**
- `search(kind='paper', q='photocatalysis')` (or any real corpus
  query) — verify the TOON shape, the cluster-context Next: line,
  every nav line is runnable.
- Follow the cluster trailer's Next: line — does the destination
  TOC actually include the top hit's chunk position?
- `search(kind='skill', q='reading a paper')` — verify TOON shape,
  `[unwired]` prefix on disabled-kind hits.

**Address grammar:**
- Confirm `slug/toc` and `view='toc'` produce identical output.
- Confirm `slug~A..B/toc` and `view='toc'` + `id='slug~A..B'`
  produce identical output.
- Try `get(kind='paper', id='<slug>~99999')` (out-of-bounds) —
  expect a useful BadInput pointing at valid range.

**Abbreviation legend:**
- Pick a paper with technical content. Verify the legend appears
  with sensible expansions. Cross-reference: the abbreviations
  named should actually appear in the rendered TOC rows.
- Pick a paper without abbreviations. Verify NO legend line.

**Shared-phrases footer:**
- Pick a paper whose content is topically narrow (every segment
  about the same broad subject). Verify the shared-phrases line
  appears and is sensibly chosen.
- Verify the shared phrases do NOT also appear in every row's
  per-segment keywords (the footer is supposed to *replace* them
  in the rows).

**View enum hint:**
- `get(kind='paper', id='<slug>', view='bogus')` — expect
  `[error:BadInput] unknown paper view 'bogus' options: …`
- Same on skill.
- `get(kind='paper', id='<slug>', view='')` — does empty string
  do the right thing?

**Cache + determinism:**
- Same paper, `view='toc'` twice — second call's response
  identical to first. (Idempotence proves the cache key is
  correct, not just that the cache exists.)
- Different scope on same paper — cache miss expected, distinct
  output.

**Pattern probes (the previous reviewer's most useful instincts):**

- **Run every Next: line verbatim.** Whatever response you're
  reading, paste each Next: recipe back. Anything that fails or
  produces wrong output is BLOCKER.
- **Run RAKE keyword columns on natural English queries.** If you
  see RAKE phrases that are clearly garbage (long dash runs,
  isolated punctuation, common-noun-only), call it out.
- **Test missing-kwarg envelopes.** Every verb called with no
  kind=, with empty kind='', with missing required args — should
  return `[error:BadInput]` envelope, never raw pydantic dumps,
  never traceback leaks.
- **Sub-K_MIN papers.** Find or create a tiny paper (3-5 chunks).
  The TOC should render gracefully, not crash.

## Ground rules

- Treat refs as read-only unless you create them yourself.
- For throwaway state, use tag `topic:picky-toc-throwaway`. Clean up
  before exiting.
- Do NOT file gripes this round — the write-only constraint is
  unchanged from prior reviews. (The single gripe verification
  point already shipped in earlier rounds.)
- Cap at ~40 calls. Quality > quantity.

## Output

Your final response is the report. Structure:

```
# Smart-TOC + cluster picky pass — findings

## Phase 0 — first-move plan from native surface only
(Pre-exploration plan, 2-4 sentences, quoted verbatim from
initialize.instructions + tool descriptions.)

## TOC verification

### Smart segmentation quality
<paper-by-paper assessment: do segments align with topic shifts,
or are they degenerate (one big segment + outliers)? Are the K
values reasonable? Are RAKE keywords per segment actually
distinguishing rows or repeating shared jargon?>

### H2-mode vs embedding-mode policy
<is the H2-vs-embedding fallback firing correctly? Any paper
where the wrong mode kicked in?>

### Address grammar consistency
<do `slug/toc` and `view='toc'` produce identical output? Do
`slug~A..B` ranges resolve correctly?>

### Recursive sub-TOC
<does zooming into a sub-range actually re-segment, or does it
collapse?>

## Search verification

### TOON shape + cluster trailer
<is the TOON header right, the columns right, the cluster Next:
line correctly pointing at the top hit's segment?>

### Cluster handle / TOC consistency
<follow the cluster trailer's Next: line — does the destination
TOC contain the top hit's chunk position?>

## Findings — numbered, severity-tagged

### N. <one-line title>
- **Category**: TOC | search | grammar | enum | abbrev | shared-phrases
  | cache | render | error-envelope | other
- **Severity**: blocker | high | medium | low (LOW is reserved for
  true nitpicks; partial fixes that break new paths are BLOCKER)
- **Where**: verb + args + verbatim call
- **Observed**: verbatim quote
- **Expected**: what an agent following the docs would expect
- **Suggested fix**: optional one-liner

## What still works well
<honest list — what's worth keeping>

## Coverage + cleanup
<which probes you ran, what you skipped and why, what state
you created and cleaned up>
```

## Cleanup

Reap memories tagged `topic:picky-toc-throwaway`. Confirm
`search(kind='memory', tags=['topic:picky-toc-throwaway'])` returns
empty.
