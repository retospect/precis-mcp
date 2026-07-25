# Runbook — skill-search-review (discoverability cadence)

A recurring pass that asks one question: *when agents `search(kind='skill',
q=…)`, do they find the right skill — and if not, what "search bits" (skill
vocabulary, the matcher itself) would fix it?* Cadence is 30 days, enforced
advisory-style by `scripts/skill-search-review` (surfaced in `/whatneedsdoing`,
next to `token-review` / `db-thrash-review`). Skill search is the *fallback*
discovery path — agents mostly `get(kind='skill', id='<slug>')` by known slug;
they only search when they **don't** know the slug, i.e. exactly the novel-need
case where discoverability matters most. So a small search volume still carries
outsized signal.

Tiering (per CLAUDE.md): the *cadence check* is a script (tier 1, zero model);
the *audit itself* is a judgment session (tier 3) — the extraction + stats are
scripts, but judging whether a returned menu satisfied the caller needs a model
reading the query/menu/outcome triples. The script only tells you **when** it's
due; you run the pass.

## When

`scripts/skill-search-review` prints `skill-search-review: DUE` when the newest
dated line in this file's `## Log` is >30 days old (or absent). Inside the
window it's quiet. Run the pass when DUE, then append a dated line — that resets
the clock.

## Why there's no query log to grep

Skill-search tool calls are **not** durably logged. There is no `tool_calls`
ledger (unbuilt), `worker_logs` has no `mcp_calls` logger in prod, and
`ref_events` only records corpus mutations. The population is reconstructed from
the two transcript stores that DO survive:

- **Local dev sessions** — `~/.claude/projects/*/*.jsonl`. Full fidelity: the
  `tool_use` (query) + `tool_result` (rendered menu) + the caller's next action.
- **Prod cluster agents** — `refs.meta->'transcript'` on `kind='job'` (the only
  prod kind carrying a tool-call transcript; agentlog's `prompt` is the
  assembled context, not the tool stream). Export via
  `\copy (…) TO STDOUT WITH (FORMAT csv)` — plain `TO STDOUT` COPY-escapes
  `\n`/`\t`/`\\` and the lines won't re-parse as JSON.

## The pass (scripts do the reading — keep raw transcripts off the main loop)

The 2026-07 audit's scratch scripts are the reference implementation; re-derive
them (they live in a session scratchpad, not the tree). The shape:

1. **Extract** — one parser over both stores. Both use the same
   `message.content[]` tool_use/tool_result shape (prod's is stream-json inside
   the CSV field). For each `search` with `input.kind=='skill'`, record: query
   `q`, the returned menu (ranked slugs, parsed from the `{slug	section	more	
   keywords}` table), and the caller's next skill action → `select` (opened a
   menu slug), `select_offmenu` (opened a skill NOT in the menu — the menu
   failed), `research` (re-searched — refined), or `abandon`.
2. **Classify execution FIRST.** Most raw calls never ran: a `tool_result`
   starting `Claude requested permissions … haven't granted` = **blocked**
   (permission never granted — a config/harness issue, NOT search quality);
   `tool use was rejected` = user declined; `sent no response … aborting` =
   server hang; `no skills mention '…'` = genuine **zero** match; a `{slug`
   table = **ok**. Only `ok`+`zero` are search-quality data — segment them out
   before judging relevance, or blocked/rejected noise dominates the stats.
3. **Stat** — volume by source, unique/normalized queries, query-length dist,
   error/zero rate, outcome mix, selected-rank distribution (top-1 vs top-3 hit
   rate among selects), off-menu selects, re-search rate.
4. **LLM-eval** (delegate to a sonnet agent reading the executed subset + the
   real skill files) — per query: was a relevant skill in the menu? was it
   top-ranked? satisfaction proxy from the caller's next action. Then read the
   matcher (`src/precis/handlers/skill.py`) to explain *why* misses happened
   (ranking bug vs content/vocabulary gap vs cold embedder), and propose
   concrete fixes: which skill file to edit + what H2/vocabulary to add, or a
   matcher change. Watch the embedder-cold trap — a degraded dev embedder drops
   search to the lexical leg and inflates the zero-rate; live-re-query a couple
   of "zero" queries before trusting them.

## Levers ("better search bits")

- **The matcher** (`src/precis/handlers/skill.py`): hybrid lexical + semantic +
  a title/identity boost. The lexical leg scores query-word overlap; the boost
  fires on overlap against a skill's *identity* (slug + `title:` + `# H1`),
  supplemented by its section headers (`## H2` + `summary:`). (Pre-2026-07 both
  legs required the whole query as one contiguous substring — reordered /
  punctuated natural-language queries surfaced nothing; that's fixed.)
- **Skill content**: to make a skill surface for a query, put the vocabulary in
  an `## H2` header (a section token) + body prose. To make it *pin to the top*,
  the vocabulary must land in the skill's identity (slug/title/H1) — so a bare
  common word promotes only skills that skill NAMES the subject, not every skill
  that mentions it. The displayed "keywords" column is RAKE over the winning
  snippet, not editable metadata — you change it only by changing section text.

## Known limitation — ubiquitous identity words

A content word present in *most* skills' identity — `precis` (every slug),
`help` (~90/140) — makes the single-word title boost fire near-catalogue-wide,
collapsing the top to score ties broken by dict-insertion order, not relevance
(e.g. bare `search(q='precis')`). This predates the 2026-07 tokenisation and
the single-word identity path doesn't close it. Low impact (nobody searches a
bare corpus-wide word), left as a documented edge, not fixed — a future pass
could down-weight identity tokens whose document frequency exceeds some
fraction of the catalogue.

## Log

Newest first. One line per pass: date, corpus size, headline, what shipped.

- **2026-07-25** — First audit. 56 skill searches over 20d (30 local dev, 26
  prod); only 27 executed (22 blocked by a transient 2026-07-20 permission
  window, 6 user-rejected, 1 hang). Top-1 hit 5/11 among selects. Root cause:
  lexical leg + title boost were atomic whole-query substring matches → fixed to
  tokenized word-overlap + identity-weighted boost + H2-header awareness
  (`skill.py`, regression tests in `test_skill.py`). Content fixes: draft
  hygiene/footer vocab, `ask_user` docs, skill-authoring redirect, llm
  router/tier vocab. Behavioral note filed: agents conflate "tool blocked" with
  "zero matches" and thrash reformulations — worth a nursery friction-detector.
