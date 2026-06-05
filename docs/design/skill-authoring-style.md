# Skill authoring style — prose discipline for LLM-facing docs

**Audience**: anyone writing or rewriting a file under
`src/precis/data/skills/`.
**Companion**: [`docs-and-skills-redesign.md`](./docs-and-skills-redesign.md)
covers the ingest substrate (chunker, alias groups, `FLAVOR:` tags,
boot-time scan). This file covers the *prose* that goes inside.
**Scope**: the cut-list, the "describe consequences, not algorithms"
table, the front-matter discipline, and the tone notes apply to
**every flavor** (`reference`, `persona`, `runbook`, `concept`).
The default skeleton at the bottom is `reference`-specific; persona
and runbook structural requirements (the `## Adopt this persona`
H2; `invokes_personas:` front-matter) come from the redesign doc,
decisions 8–9 — not restated here.

## The rule

> **A skill describes the contract, not the implementation.**
> **Write for the LLM. Terse, precise, no warm-up.**

Skills are read by an LLM that is about to call a verb. Anything that
doesn't help the LLM *call* the verb, *interpret* the response, or
*pick the next move* is noise.

The LLM cannot query our database, run our workers, file ADRs, or
read CHANGELOG. It can call the seven verbs. Write to that audience.

Humans rarely read these. Optimise for the LLM:

- One sentence per concept. Two is suspicious. Three needs cutting.
- Inline `# comments` inside code blocks beat a prose paragraph after.
- No transitions ("To do X, …"), no soft framing ("When the paper
  has X, …" — show it via comment), no reassurance.
- If the example carries the meaning, drop the prose entirely.

## What to cut

Categories below come from a grep pass over the existing skill
corpus. Real strings, real files.

### 1. Internal storage names

- `ref_segments`, `ref_segment_sentences`, `chunks.numerics`,
  `chunk_embeddings`, `pg_try_advisory_xact_lock`.
- Migration filenames (`0005`, `0007`, `0018`).
- The LLM cannot read these tables. It can call `get` / `search`.

### 2. Worker, job, and pipeline names

- `segment_toc` worker, `precis worker --only segments`,
  `FileCorpusIndex`, `boot-time scan-and-ingest`, `derived queue`.
- Exception: if the user must run a command to recover from a real
  error condition the LLM will see ("segments not yet computed —
  ask the user to run X"), keep the command. Otherwise cut.

### 3. Algorithm and model names

- `bge-m3`, `RRF`, `tsvector`, `pgvector cosine`, `KeyBERT`,
  `DP-uniform-cost`, `matryoshka-ordered`, `pysbd`, `marker`.
- The consequence stays; the name goes. "Hybrid lexical + semantic,
  rank-fused — there is no honest numeric score, order is the
  signal." Don't say RRF. Don't say bge-m3.

### 4. Implementation-as-marketing

- "served from the persistent discovery layer"
- "pre-computed at ingest, not recomputed on request"
- "the headline feature of ADR 0018"
- "that's by design" / "by design"

The LLM is not shopping for a skill. It is calling a verb. Reassurance
of architectural correctness is for human reviewers.

### 4b. Universal-contract restatement

Don't restate behavior that's universal across a verb:

- `get` raises `NotFound` for missing refs. The LLM knows this from
  the first miss; don't write "raises NotFound if missing" on every
  kind's get-section.
- `tag(remove=...)` is a no-op for tags that aren't present. Same.
- `search` with no hits returns "no X matches" with a `Next:` block.
  Universal.

If a kind has *non-default* error behavior (e.g., put-paper rejects
because bodies are import-only, and the error explains why), that's
worth a sentence — but it lives in the working section it relates
to, not in a universal-error appendix.

### 5. Migration trivia and historical commentary

- "the legacy `request_doi.md` queue still works but is being phased
  out — prefer the structured finding path"
- "we don't surface a misleading numeric — list position is the
  only honest relevance signal"

Tell the LLM what to do now. If there are two ways, name the one
that works; drop the history of the other.

### 5b. Primitives that work in tests but mislead at scale

Some verbs exist and return content but produce useless output on a
real corpus — `get(kind='paper')` returns a page of 50 against a
5k-paper store. The LLM follows the lead and gets a slice that looks
like "all of them" but isn't.

Don't advertise these as discovery paths. The LLM can still discover
the verb exists from the tool schema; if they try it, they get
content (just not useful content). The skill leads with the path
that works at production scale.

### 6. UX-rendered affordances described in prose

- A prose section titled "## Cluster context in the trailer"
  explaining the `Next:` block the runtime already prints into every
  response. The LLM sees the trailer directly in tool output.
- Same for error pointers, copy-pasteable continuation lines,
  inline kind-tags on cross-kind search hits.

**Show, don't describe.** When a response block earns its place (it
demonstrates a rendering feature, a non-obvious shape, or a sub-line
structure that prose would labour to explain), include it verbatim.
The LLM gets the contract from the example.

**Don't show what the runtime will obviously emit.** A response block
that's just "here's the natural output of this verb" is redundant —
the LLM will see it the first time they call the verb. Default to a
one-sentence prose contract; add the response block only when the
sentence can't carry the meaning. The TOC table is one sentence
("rows are drillable; paste a handle as `id=`"); the cross-kind
fan-out's per-hit `kind` tag might warrant an example.

### 7. Format name-drops

- `TOON`: there is one skill (`precis-toon`) that documents the
  format itself. Other skills should *show the shape* in example
  outputs, not name-drop the format.
- Same for any internal data-shape label.

### 8. Decorative front-matter

The loader (`handlers/skill.py:_parse_frontmatter`) reads exactly
four fields: `id`, `title`, `applies-to`, `status`. Plus the
upcoming `flavor`, `invokes_personas`, `available-when` once the
redesign lands.

Drop these — nothing reads them:

- `tier:`, `floor:`, `last-updated:`
- `status: phase-N` (the loader only checks `planned` /
  `aspirational` / `active`; `phase-N` is treated as "active" but
  appears nowhere in output). Use `status: active` or omit.

## What to keep

- **The verbs and arg shapes.** What `get` / `search` / `put` /
  `edit` / `delete` / `tag` / `link` take and return for this kind.
- **Address grammar.** `slug`, `slug~N`, `slug~A..B`, `view='toc'`,
  bare DOI for papers. Show, don't theorize.
- **Realistic examples.** Real call + real response, copy-pasteable.
  One worked example per concept beats two paragraphs about it.
- **Failure modes + the recovery path.** "If you don't know the
  slug, list with `get(kind='X')` first." "If a DOI lookup misses,
  register a finding." A failure-mode line earns its place by
  pointing at the *next call*. A bare "X doesn't work" line does not.
- **Decision aids.** "Use `kind='paper'` for ingested papers; use
  `kind='web'` for ad-hoc URLs." When-to-use, side-by-side.
- **Cross-references to sibling skills.** `precis-citation-help`,
  `precis-finding-help`, etc. The LLM follows these.
- **User-facing concept names** keep their names: TOC, abstract,
  DOI, BibTeX, slug, chunk, segment.

## Describe consequences, not algorithms

| Don't write | Do write |
|---|---|
| matryoshka-ordered keywords | keywords, most-distinctive first |
| RRF-fused hybrid (tsvector + pgvector) | hybrid lexical + semantic search; order is the relevance signal |
| DP-uniform-cost on bge-m3 chunk vectors with a distinctiveness penalty | when the paper has no headings, segments are clustered by content |
| served from `ref_segments`, pre-computed by `segment_toc` worker | (just delete — implementation detail) |
| pgvector cosine rerank against the query embedding | the excerpt shown is the segment's most-on-topic sentence for your query |
| TOON table with `{handle\tkeywords}` header | (show the table; let the shape speak) |

## Worked before/after

**Before** (`precis-paper-help.md`, lines 134–143):

> The `view='toc'` output is a **TOON table** (one row per segment)
> with **matryoshka-ordered keywords** (most-distinctive first) and
> an **indented query-aligned excerpt sub-line** per segment, served
> from the persistent discovery layer (`ref_segments` +
> `ref_segment_sentences`) — pre-computed at ingest by the
> `segment_toc` worker, not recomputed on request. When the paper has
> explicit H1/H2 headings the segmentation uses them; otherwise the
> TOC falls back to embedding-sequence clustering (DP-uniform-cost on
> bge-m3 chunk vectors with a distinctiveness penalty against sibling
> centroids).

~110 words. Five things the LLM can't act on.

**After**:

> `view='toc'` returns one row per segment: a handle, keywords
> (most-distinctive first), and a representative excerpt aligned to
> your query when there is one. Drill into a segment with
> `get(kind='paper', id='<handle>')`. Segmentation follows H1/H2
> headings when the paper has them; otherwise segments are clustered
> by content.

~55 words. Everything actionable preserved. Followed in the source
file by a real example block — that's where the shape lives.

## Concrete examples vs placeholders

When showing a call where the slug / id / handle is the *point* of
the example (the LLM needs to see what a real slug looks like, what
a DOI looks like), use a concrete value:

```python
get(kind='paper', id='abazari2024design')
get(kind='paper', id='10.1038/nature10352')
```

When the slug is incidental and the code is varying the view, the
selector, or the argument shape, use `<slug>` to make the slug
visually disappear:

```python
get(kind='paper', id='<slug>', view='toc')
get(kind='paper', id='<slug>~63..89')
get(kind='paper', id='<slug>~38..42')
```

LLMs handle the `<...>` convention fine (it's standard in tech
docs). Eight uses of the same concrete slug in one block is visual
noise that buries the actual lesson (which view / selector to use).

## Fences and language tags

The triple-backtick fence is load-bearing: it signals literal,
reproduce-exactly content. Without it the LLM may paraphrase a call
and break the syntax. The language tag inside is a weaker signal but
useful for distinguishing role:

- ` ```python ` — tool calls. The content *is* valid Python (function
  calls); editors get free syntax highlighting; the LLM reads it as
  "issue this verbatim."
- ` ```text ` — example response output (TOC tables, search results,
  citation strings). Not code, don't pretend it is.

Don't mix the two inside one block. The role split is more useful
to the LLM than highlighting fidelity.

## Default skeleton (`flavor:reference`)

```markdown
---
id: precis-<kind>-help
title: precis — <short imperative summary>
applies-to: <verb>(kind='<kind>', …)
status: active
---

# precis-<kind>-help — <one-line positioning>

<One sentence. What this kind is for, in user terms.>

## <Goal-voice H2: "Find a paper by topic when I don't know the slug">

```python
<call>
<call>  # inline comment carries the variation
```

<One or two sentences. Contract-level facts only.>

## <next H2>

...

## See also

```python
get(kind='skill', id='precis-<sibling>')   # <why you'd hop there>
```
```

The See also block is a code block of `get(kind='skill', ...)` calls
with inline `# comment` hints — not a bullet list. The LLM gets the
slug, the fetch syntax, and the reason in one move; copy-paste lands
the call. A bullet list of bare slugs forces the LLM to construct the
fetch call from address-scheme memory, which is one extra step.

### No "Not yet" / negative-laundry sections

A list of things the LLM should not try is anti-pattern. The LLM has
to mentally suppress a list it shouldn't be reading, and the list
rots silently as the API grows.

- If the runtime returns a clear, actionable error when the LLM
  tries the unsupported thing, the skill mention is redundant —
  drop it.
- If the runtime error is confusing or unhelpful, **fix the error**
  (point at the alternative in the error string). The skill is the
  wrong place to paper over a bad error message.
- If something is genuinely close to a real API (`get(id='arxiv:…')`
  pattern-matching the bare-DOI form) and the LLM will predictably
  mis-attempt it, fold one line into the relevant working section —
  not into a separate negative list.

### H2 voice

Per the redesign doc (decision 3), H2s read naturally after "I want
to …". Goal-statement, not nominalisation:

- Good: `## Drill into a section of a paper I'm reading`
- Bad: `## Drilling into sections` / `## Block-range views`

The static gate rejects bare-verb H2s (`## Search`, `## Get`, …).

### Alias-group H2s (multi-angle headings on one body)

The chunker (`src/precis/skill_index/chunker.py`, version 2) supports
**alias groups**: consecutive H2 headings with no body between them
share the body that follows. Each heading produces its own chunk
with its own embedding; an alias group with N headings = N chunks
× same body.

```markdown
## See more results
## Get additional papers after a search
## Page through search hits beyond the first batch

```python
search(kind='paper', q='...', page=2)
```

`page=1` is the default. Bump `page=` to walk results.
```

Three angles, one body, three embeddings. Queries phrased as
"how do I page", "more results", and "what's after the first page"
each land on their respective alias.

**When to use alias groups:**

- Sections that take multiple natural-language phrasings and where
  retrieval matters (the high-traffic verbs and discovery operations).
- Sections that real-world misses have hit (capture missed queries
  from the gripe stream as alias candidates).

**When to stay singleton:**

- Meta sections (`## See also`, header chunks).
- Sections whose only natural phrasing is the H2 you already wrote.
- When you can't think of two genuinely-different framings, don't
  pad with synonyms. Two synonym H2s = two redundant chunks; the
  retrieval lift is zero and the storage cost is real.

### Picking H2 aliases that span embedding space

Synonyms don't help (`"See more results"` vs `"Get more results"`
embed near-identical). To spread aliases wide:

1. **Vary the frame** — action / goal / problem.
   - Action: `## Page through search results`
   - Goal: `## See more papers than fit on one page`
   - Problem: `## What if there are more hits than I see?`

2. **Vary the vocabulary register** — jargon / plain / beginner.
   - Jargon: `## Paginate the search response`
   - Plain: `## Get the next page of results`
   - Beginner: `## How do I see more than the first batch?`

3. **Vary the intent vector** — browse / fetch / locate.
   - Browse: `## Walk through everything that matched`
   - Fetch: `## Grab page 2 of results`
   - Locate: `## Find a specific paper deeper in the result list`

4. **Quantitative diversification** (when authoring at scale):
   LLM-generate 10-15 candidates per section; embed each with the
   runtime model (bge-m3); use **greedy farthest-point sampling** —
   start with one anchor, iteratively pick the candidate maximising
   the *minimum* cosine distance to already-selected H2s. Stop at
   K=3 or 4. Guarantees the chosen aliases span the candidate cloud
   instead of clustering.

5. **Source from real misses** — when an LLM query lands on the
   wrong section, capture it as an alias candidate. The gripe stream
   (redesign doc decision 4 — "Gripe feedback flows by adding more
   H2s to the group") is the channel.

The retrieval bench (`scripts/_h2_experiment.py`, referenced in the
redesign doc) is the measurement harness — generate candidates,
evaluate which K-set maximises P@1 against a held-out query set.

**Default size**: 2–3 aliases per high-traffic section. More than 3
hits diminishing returns; the storage cost grows linearly.

### Body length per H2

Per decision 5, one H2 = one chunk = one embedding. If a section's
body overruns the embedder's chunk budget, ingest hard-fails — split
into multiple H2s. Cut prose before splitting; usually the prose
needed cutting anyway.

## Tone notes

- No "by design," "that's by design," "explicit by design."
- No reassurance paragraphs that explain why a choice was made.
  (If the choice surprises the LLM in a way that affects how it
  calls the verb, fold the surprise into the contract description;
  otherwise drop.)
- No `## Status` sections inside the body. Status is the
  front-matter field.
- No "Note:", "Important:", "Tip:" callouts. If it matters, put it
  in line. If it doesn't, cut it.

## When in doubt

Read your draft aloud as if the audience is an LLM about to call
the verb. For every sentence ask: "Can the model do anything
different *because of this sentence*?" If no, cut it.

## See also

- [`docs-and-skills-redesign.md`](./docs-and-skills-redesign.md) —
  ingest, chunking, alias groups, `FLAVOR:` tags, static gates.
- `src/precis/handlers/skill.py:_parse_frontmatter` — the only
  authoritative list of front-matter fields the loader honors.
