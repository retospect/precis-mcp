# Stateless primitives — `clock:`, `random:`, `rng:`, `iching:` + wisdom corpus

**Goal.** Four small stateless kinds that give the agent things it
otherwise has to guess at or go without, plus one new corpus that
piggybacks on existing infrastructure:

- `clock:` — current time, date, timezone, durations (LLMs can't tell time).
- `random:` — sample from vector space, uniform or blast-radius.
- `rng:` — number generator (ints + ranges, coin-flip default).
- `iching:` — 64-archetype reframing tool with three-layer interpretation.
- *(corpus)* `wisdom` — proverbs, chengyu, koans, stoic maxims; accessed
  via `random:?corpus=wisdom` and `search(type='wisdom', …)`.  No new
  kind required.

Two skills ride on top: `ideation` (synthesise from random bits, with
an anchored evaluation loop, optionally pulling from the wisdom corpus)
and `iching-consult` (use the three-layer archetype as a re-framing
prompt).

Three of the four kinds are stochastic; `clock:` is deterministic.
They are grouped here because they're all small, stateless, free, and
all share the same "minimal primitive the agent shouldn't have to
build itself" shape.

**Author.** Reto Stamm · 2026-04-25
**Status.** Spec — not yet implemented.  Data file
(`precis-mcp/docs/iching.txt`, 64 entries) is validated and ready to
ship.

**Design lens — the consumer is an LLM.**  These kinds are read by an
LLM agent acting on behalf of a human user.  Optimisations and design
choices throughout this spec privilege LLM consumption: predictable
output structure over aesthetic flourish, named tools (cognitive
heuristics) over poetic prose, paraphrases over archaic translations,
keyword-dense headers over decorative ASCII.  When in doubt: structure
beats decoration.

---

## Why these belong in precis

The agent stack has tools for *being right* (`paper:`, `web:`, `calc:`,
`math:`).  It has fewer for *being surprised* or *being grounded in
basic facts*.  This spec adds:

- **Grounding** — `clock:` so the agent knows what time it is.
- **Surprise** — `random:` for serendipity from the agent's own corpus.
- **Decision support** — `iching:` for forced reframing through 64
  archetype patterns × three interpretation layers.
- **Branching** — `rng:` so the agent can flip a coin without computing.
- **Cultural inheritance** — the `wisdom` corpus, embedded into the
  same vector store as everything else, queryable by semantic
  proximity to a problem.

Putting these in precis means:

- They share the URI-scheme + `KindSpec` plumbing (free, footer, /help).
- They reuse `acatome-store` for vector-space sampling and semantic
  search — no new infrastructure.
- The wisdom corpus is *just another corpus* — same Block/Ref schema,
  same embedding pipeline, same filter machinery.  Building it as a
  kind would be reimplementing what already works.
- The agent discovers them via the same `get(id='/stats')` surface as
  every other kind.
- Skills (`skill:ideation`, `skill:iching-consult`) live next to the
  kinds they invoke, in `precis/skills/`.

---

## Kind 1 — `random:` (vector-space sampler)

**Stateless, read-only, free.  No new tables.**

Two modes, dispatched by id shape:

| URI                                       | Behaviour                                |
|-------------------------------------------|------------------------------------------|
| `random:`                                 | One uniform-random ref from any corpus   |
| `random:?n=3`                             | Three refs                               |
| `random:?corpus=papers`                   | Filter to one corpus (or `corpora=a,b,c`) |
| `random:?corpus=wisdom&tradition=chengyu` | Filter by metadata tag                   |
| `random:?tag=framing,humility`            | AND-match across tags                    |
| `random:?unit=block`                      | Sample blocks (~1M rows) instead of refs (~2.5K) |
| `random:<seed-string>`                    | Top-K within blast radius of seed's embedding |
| `random:<seed>?radius=0.3`                | Tighten / loosen the cosine-distance ceiling |
| `random:<my problem>?corpus=wisdom&n=3`   | Wisdom semantically near the problem     |
| `random:<seed>?n=5&corpus=memories`       | Compose                                  |
| `random:/help`                            | Onboarding skill inline                  |

### Output shape

For uniform mode, render each pick as one block:

```
🎲 random pick (papers · 1 of 2519)
**wang2020state** — State-of-the-art membranes for CO₂ capture
> First 200 chars of blocks[0].text…
get(id='paper:wang2020state') for the full ref.
```

For blast-radius mode, group by corpus, prefix with the cosine distance:

```
🎯 near "membrane fouling" (radius ≤ 0.30, 4 hits)

📄 papers
  0.18  liu2019antifouling — Antifouling strategies for…
  0.22  chen2021biofilm   — Biofilm formation on RO membranes…

🧠 memories
  0.27  /2026-04-12-membrane-meeting — discussion with Matthias…
```

### Sampling unit — refs vs. blocks

Default is **ref-level sampling** — one row per paper / memory / web
bookmark / book note.  Refs are the right grain for ideation: returning
a whole ref (title + first block) is evocative; a random block-level
chunk is often just transitional filler ("As shown in Figure 3…")
with no standalone meaning.

Opt-in block-level via `?unit=block`.  Useful for fine-grained
serendipity when the user wants to surface a specific paragraph or
quote, at the cost of hit quality.

Scale matters here:

- **refs**: ~2.5K rows today.  `ORDER BY RANDOM() LIMIT N` is sub-
  millisecond; no optimisation needed.
- **blocks**: ~1M rows today (every chunk of every ingested paper).
  `ORDER BY RANDOM()` at this scale is a full table scan + external
  sort.  Use `TABLESAMPLE SYSTEM_ROWS(N*10)` + an in-memory shuffle +
  `LIMIT N` from the start — never the naive approach.

### Implementation

- **Uniform, ref-level (default)**:
  ```sql
  SELECT id, slug, title, corpus_id FROM refs
    WHERE corpus_id = ANY(:corpora)  -- optional
    ORDER BY RANDOM()
    LIMIT :n
  ```
  Fine at 2.5K rows.
- **Uniform, block-level (`?unit=block`)**:
  ```sql
  SELECT b.id, b.ref_id, b.text, r.slug, r.title, r.corpus_id
    FROM blocks b TABLESAMPLE SYSTEM_ROWS(:n * 10)
    JOIN refs r ON r.id = b.ref_id
    WHERE r.corpus_id = ANY(:corpora)  -- optional
    ORDER BY RANDOM()  -- shuffle the sample
    LIMIT :n
  ```
  `SYSTEM_ROWS` gives an approximate N×10 sample cheaply (uses PG's
  `tsm_system_rows` extension — already enabled in the `cluster` DB
  for other things; verify at phase start).  Over-sample then shuffle
  + trim to hit exactly N with fair randomness per row.
- **Blast radius**: embed the seed via the same encoder used by
  `search_text`, then run the existing pgvector cosine-distance query
  with a `<= radius` filter and `LIMIT n`.  Reuse
  `acatome_store.vector.PgvectorIndex.search_text` with the `where`
  and `top_k` it already accepts; add a `max_distance` arg.  Blast
  radius always operates at block level (that's where the embeddings
  live) but renders the parent ref's title alongside each hit.
- **Footer**: `_Sampled by precis · seed=<hash> · unit=<refs|blocks> · corpora=[…] · n=<k>_`.
- **Seed** (`?seed=42`) makes the result reproducible across calls —
  useful for tests and for sharing "what cards did you draw?"
  snapshots.  Note: reproducibility is only within a corpus snapshot;
  a new ingest changes what's there to draw from.

### Cost / safety

- `cost_hint="free"`.  All compute is local PG + a single embedding call
  per blast-radius request (already amortised by `sentence-transformers`
  warm cache).
- No writes.  No external network.
- `?n` clamped to `[1, 20]`.  `?radius` clamped to `[0.0, 1.0]`.

### Wisdom corpus — `random:?corpus=wisdom`

Proverbs, chengyu, koans, stoic maxims, and similar short cultural
artefacts ship as a *normal corpus* in `acatome-store`, not as a new
kind.  Each entry is one ref; the embedded text is its meaning + gloss
in English; tradition / language / source live in metadata.  This
makes the entire library accessible through:

- **Uniform pick**: `random:?corpus=wisdom` — one entry at random.
- **Filtered pick**: `random:?corpus=wisdom&tradition=chengyu` — pick
  only from a tradition.
- **Blast-radius pick**: `random:<problem statement>?corpus=wisdom&n=3`
  — wisdom whose *meaning* sits near the problem in semantic space.
  This is the killer feature: the chengyu 朝三暮四 (morning-three,
  evening-four) embeds near "team unhappy with the new schedule
  despite same total hours" because both centre on perception-of-the-
  same-content.  Keyword search misses this; semantic search finds it.
- **Search**: `search(type='wisdom', query='conflict resolution')` —
  intentional lookup by topic.

Why this beats a separate `wisdom:` kind: a new kind would have to
re-implement the corpus loader, the random pick (uniform + blast),
the semantic search, the metadata filter, and the cross-corpus
integration — all of which `random:` and `search:` already do.  No
special access pattern justifies the duplication.

#### Corpus shape

Each entry, written as YAML for ingest:

```yaml
- slug: zhao-san-mu-si
  tradition: chengyu          # one of: chengyu, proverb-en, stoic, koan, …
  lang: zh
  original: "朝三暮四"
  pinyin: "zhāo sān mù sì"
  literal: "morning three, evening four"
  meaning: "Manipulating perception by rearranging the same total."
  gloss: |
    A keeper told his monkeys they'd get three nuts in the morning and
    four in the evening; they protested.  He switched to four-then-three;
    they were satisfied.  Same total, different framing.  Used today for
    shallow appeasement that doesn't change substance.
  tags: [perception, framing, manipulation, animal-fable]
  source: "戰國策"
```

The **embedded text** is `meaning + gloss` only — pure semantic
content.  Original phrase, pinyin, literal translation, and source
attribution live in metadata for display, but stay out of the
embedding space so they don't dilute neighbourhood matching.

#### When does the wisdom load?

Same way every other corpus loads in this stack: the user runs an
ingest command once.  No auto-load at install, no lazy load on first
call (would block the first agent request on embedding-model warmup).

```bash
# One-shot ingest from a YAML file
acatome-store ingest-wisdom path/to/wisdom.yaml
```

The script:

1. Adds `wisdom` to `acatome_store.models.CORPUS_SEEDS` if missing.
2. For each entry: creates a Ref (slug + title=`original` + corpus=wisdom),
   one Block (text = `meaning + gloss`, metadata = the rest of the
   YAML fields), and triggers the standard embedding pipeline.
3. Idempotent — re-running with the same YAML updates existing entries
   by slug, no duplicates.

precis-mcp ships a **starter YAML** at
`pips/packages/precis-mcp/data/wisdom-starter.yaml` (~100 hand-curated
entries: 50 chengyu, 30 proverbs, 20 stoic).  Users run the ingest
once after first installing precis-mcp.  The skill `iching-consult`
notes that wisdom may not be loaded and guides the agent to surface
the install hint if `random:?corpus=wisdom` returns "no hits."

Subsequent additions go through the same path — append entries to a
local YAML and re-run `ingest-wisdom`.  Or manually via
`put(type='memory', tags=['wisdom', '<tradition>'], …)` for one-offs,
though that lands in `memories` not `wisdom` — fine for personal
notes, less ideal for shareable cultural references.

#### Out of scope for v1

- Original-language semantic search (embedding the Chinese characters
  themselves).  Multilingual embeddings exist but the meaning + gloss
  approach is simpler and works.  Add later if needed.
- User-pluggable wisdom packs (e.g. lab-specific aphorisms).  Just
  append to the YAML and re-ingest.
- Auto-translation of original-language entries.  Curated paraphrases
  are deliberate, not a stopgap.

### Related / not-merged

`random:` deliberately does **not** subsume `rng:`.  They share the word
but nothing else: `rng:` is a stdlib-only stateless math primitive with
deterministic `?seed=` semantics; `random:` is a database-backed
content sampler whose results depend on what's been ingested.  Merging
would put two different cost/availability/test stories behind one URI
scheme.  Keep separate.

---

## Kind 2 — `rng:` (number generator)

**Stateless, read-only, free.  Pure Python `random` / `secrets` / `uuid`.**

Why a separate kind, not bolted onto `calc:`?  `calc:` is intentionally
deterministic and AST-sandboxed — adding randomness would muddy both its
testability and its security story.  `rng:` is its own one-tool kind.

Primary currency is **integers and ranges**.  Float is opt-in.  Default
call with no args returns a coin flip (int `0` or `1`).

| URI                         | Returns                                  |
|-----------------------------|------------------------------------------|
| `rng:`                      | Integer in `[0, 1]` — coin flip          |
| `rng:100`                   | Integer in `[0, 100]` inclusive          |
| `rng:1..6`                  | Integer in `[1, 6]` inclusive            |
| `rng:1..6x4`                | List of 4 integers in range              |
| `rng:float`                 | Float in `[0.0, 1.0)`                    |
| `rng:float/0..1`            | Float in range (inclusive low, exclusive high) |
| `rng:3d6`                   | Dice — three six-sided, returns rolls + sum |
| `rng:choice/red,green,blue` | Uniform pick from a comma list           |
| `rng:shuffle/a,b,c,d`       | Returns the list in random order         |
| `rng:uuid`                  | UUID4                                    |
| `rng:bytes/16`              | 16 random bytes, hex-encoded             |
| `rng:?seed=42/3d6`          | Seeded — same call → same result         |
| `rng:/help`                 | Onboarding skill inline                  |

Rationale for the default: agents reach for RNG to make a single
yes/no / branch decision far more often than they need a float.  `rng:`
as a coin flip is the highest-value terse call.  Float is one explicit
keyword away.  Ranges are always inclusive on both ends for ints
(matches dice / "pick a number between 1 and 6" intuition) and
`[lo, hi)` for floats (matches `random.uniform` / `np.random.rand`
convention so the result feeds cleanly into downstream math).

### Implementation notes

- Backed by `random.Random()` instances (one per call, optionally seeded).
- `rng:bytes/N` and `rng:uuid` use `secrets` / `uuid4` (CSPRNG) regardless
  of `?seed=` — seeding crypto bytes is a footgun.  The handler emits a
  warning hint if `?seed=` is combined with `bytes`/`uuid`.
- Output for dice / sample lists is markdown-formatted so the agent can
  read it back as a table.
- Footer: `_Generated locally by precis.rng · seed=<n>_` (or `seed=os` when
  unseeded).

---

## Kind 3 — `iching:` (64-archetype reframing tool, three layers)

**Stateless, read-only, free.  Bundled YAML data file.**

A reframing tool built on the I-Ching's 64-archetype space.  Each
hexagram carries **three intrinsic interpretation layers** — heritage,
modern systems, and cognitive — bundled together so the agent gets a
multi-perspective view in one read.  No changing-lines mechanism, no
separate "lens library" — the cognitive lens is *part of* each
hexagram.

The kind is named `iching:` because the *structure* (64 archetypes,
trigram composition, King Wen numbering) is genuinely from the
I-Ching.  The prose is original to precis and tuned for LLM
consumption: short, declarative, action-oriented.  No claim of
classical translation.

### URI surface

| URI                              | Behaviour                                |
|----------------------------------|------------------------------------------|
| `iching:`                        | Random hexagram, all three layers        |
| `iching:<n>`                     | King Wen number 1–64, all three layers   |
| `iching:?layer=cognitive`        | Random hexagram, only the cognitive layer |
| `iching:?layer=modern,cognitive` | Random hexagram, two layers (no heritage) |
| `iching:<n>?layer=iching`        | Specific hexagram, only heritage layer   |
| `iching:?seed=<x>`               | Seeded random — reproducible             |
| `iching:/cognitive`              | List all distinct cognitive concepts in the corpus |
| `iching:/cognitive/<slug>`       | All hexagrams sharing this cognitive concept (reverse index) |
| `iching:/types`                  | List cognitive `type` values (heuristic, principle, bias, fallacy) with counts |
| `iching:/help`                   | Onboarding skill inline                  |

Search is the second access pattern, served by the standard `search`
tool, not a special URI:

- `search(type='iching', query='cascading failure')` → hexagram 12
  (Stagnation / Failure Mode / Goodhart's Law).
- `search(type='iching', query='early-stage chaos')` → hexagram 3
  (Initial Difficulty / Bootstrap Phase / First Principles).
- Filter: `search(type='iching', query='…', grep='cognitive.type:bias')`
  to limit to hexagrams whose cognitive lens is a known bias.

### Three layers

Every hexagram entry has the same shape:

| Layer       | What it carries                                           |
|-------------|-----------------------------------------------------------|
| `iching`    | The heritage interpretation — name, idea, text.  Original Chinese name preserved as `hexagram.chinese` for display. |
| `modern`    | The systems / engineering reading — name, idea, text.    |
| `cognitive` | A named cognitive tool — name (e.g. `Pareto Principle`, `Steel Man`, `Survivorship Bias`), `type` ∈ {`heuristic`, `principle`, `bias`, `fallacy`}, idea, text. |

Default `iching:` returns all three.  Use `?layer=` to filter when
you want a terser response.

### Output shape (LLM-tuned)

Predictable structure, no decoration:

```
iching · 12 · 否 · binary 111000

heritage:
  name: Stagnation
  idea: Recognise blockages; withdraw strategically.
  text: Disconnected layers prevent flow.

modern:
  name: Failure Mode
  idea: Decouple and isolate.
  text: Separation prevents cascading failures.

cognitive:
  name: Goodhart's Law
  type: principle
  idea: Metrics distort systems.
  text: Misaligned optimisation causes stagnation.

trigrams: Heaven (111) over Earth (000)
```

Notes:

- One line header carries King Wen number, Chinese name, and binary.
  The agent can quote the binary as a stable identifier without
  reading the rest.
- Each layer is a labelled block with the same field order — `name`,
  `idea`, `text` (cognitive adds `type`).  Positional parsing is
  reliable.
- No yang/yin ASCII art.  The binary string is the machine-friendly
  representation; humans who want pictures can render them
  client-side.
- `trigrams:` line surfaces the compositional reading for agents that
  care about the structural argument (Heaven-over-Earth = blocked
  flow).

For `?layer=cognitive` the response is just one labelled block:

```
iching · 12 · 否 · cognitive

cognitive:
  name: Goodhart's Law
  type: principle
  idea: Metrics distort systems.
  text: Misaligned optimisation causes stagnation.
```

### Implementation

- **Data file**: ships as `pips/packages/precis-mcp/src/precis/data/iching.yaml`.
  64 entries, ~1200 lines, ~80 KB.  Format already validated:
  64 unique binaries, each `binary == upper.binary + lower.binary`,
  ids 1–64 complete.  Loaded once at handler initialisation.
- **YAML, not JSON**: `pyyaml` is already a precis dependency (skill
  handler reads SKILL.md frontmatter).  YAML's diff-friendliness and
  multi-line strings matter every time the file is edited; parse
  speed is irrelevant for a load-once file.
- **Random pick**: uniform across 64 by default.  `?seed=<int>` makes
  it reproducible (`random.Random(seed).choice(...)`).  No coin-throw
  simulation since there are no per-line texts to drive it.
- **Lookup**: by King Wen number `<n>` (`?n` parsed from URI path).
- **Reverse index**: `iching:/cognitive/<slug>` builds a one-shot
  index `{slug-of-cognitive.name → [hexagram-ids]}` at handler init.
  Slug is `name.lower().replace(' ', '-').replace("'", "")`.  Some
  cognitive concepts appear on multiple hexagrams (e.g. Pareto
  Principle on 9 and 11; Lindy Effect on 27, 32, 50; Inversion on 21
  and 39) — useful for "show me all the Pareto-flavoured archetypes"
  queries.
- **Search integration**: at handler init, register each hexagram as
  a Ref in the `iching` corpus with one Block per layer (three blocks
  per hexagram = 192 blocks).  Block metadata records `layer`,
  `cognitive.type`, and `cognitive.name` so search results can be
  filtered.  Embedding text is the layer's `idea + text` concatenated.
  Total embed cost: 192 × ~50 tokens ≈ 10K tokens, sub-second on a
  warm encoder.  Re-embed only when the data file mtime changes.
- **Validator**: ship a CI check at
  `pips/packages/precis-mcp/tests/test_iching_data.py` that asserts:
  64 entries, 64 unique binaries, every binary equals
  `upper.binary + lower.binary`, ids 1–64 unique and complete, every
  `cognitive.type` is in the allowed enum.  This catches AI-edit
  regressions before they ship.
- **Footer**: `_iching: 64-archetype reframing tool · data v<mtime>_`.
  No attribution to a specific translation since the prose is
  original.

### What this kind is *not*

- **Not divination.**  The output is a reframing prompt, full stop.
  The skill `iching-consult` is explicit about this: oracle as
  randomiser, not authority.
- **Not a classical translation.**  No Wilhelm/Baynes, no Legge,
  no claim of cultural fidelity beyond the structural inheritance
  (64 archetypes, trigram composition, King Wen numbering).
- **Not a separate "thinking tools" library.**  The cognitive lens
  is intrinsic to each hexagram.  If the user wants raw thinking
  tools without the I-Ching wrapper, that's a future kind to design
  separately, not a degradation of this one.

---

## Kind 4 — `clock:` (current time / date)

**Stateless, read-only, free.  Pure Python `datetime` / `zoneinfo`.**

Agents are LLMs — they don't know what time it is.  They also can't tell
you what day of the week it is, whether a deadline is tomorrow or three
weeks out, or whether to say "this morning" or "tonight" in a draft
reply.  `clock:` is the minimal fix: a current-time lookup with
timezone support, exposed via the standard precis kind surface.

**Current time**

| URI                         | Returns                                   |
|-----------------------------|-------------------------------------------|
| `clock:`                    | Default — UTC + local, ISO 8601 + human   |
| `clock:utc`                 | UTC only, ISO 8601                        |
| `clock:local`               | Server local time (whatever the host is in) |
| `clock:Europe/Dublin`       | Time in a specific IANA timezone          |
| `clock:America/New_York`    | Another timezone                          |
| `clock:unix`                | Unix epoch seconds (integer)              |
| `clock:unix/ms`             | Unix epoch milliseconds                   |
| `clock:date`                | Today's date in UTC (ISO `YYYY-MM-DD`)    |
| `clock:date/<tz>`           | Today's date in a specific timezone       |
| `clock:iso`                 | Alias of `clock:utc`                      |
| `clock:rfc3339`             | RFC 3339 with explicit offset             |
| `clock:?format=%Y-%m-%d %H:%M` | Custom strftime                        |

**Durations — "how long until" / "how long since" / "how long between"**

| URI                                  | Returns                                |
|--------------------------------------|----------------------------------------|
| `clock:until/2027-01-01`             | Days / hours / minutes from now to date |
| `clock:until/2026-12-25T18:00`       | …to a datetime (UTC assumed if naïve)  |
| `clock:until/2026-12-25T18:00/Europe/Dublin` | …in a specific timezone        |
| `clock:since/2025-01-01`             | Time elapsed since a past date         |
| `clock:between/2026-04-01/2026-12-31`| Duration between two points (either order) |
| `clock:until/easter-2027`            | Named-holiday shorthand (Phase A extra — see below) |

Both endpoints of `between/` accept ISO 8601 dates, datetimes, or Unix
epoch seconds.  Output is multi-resolution so the agent can pick the
useful one:

```
📅 duration to 2027-01-01T00:00:00Z

251 days   ·   36 weeks + 6 days
6,029 hours 7 minutes
21,705,420 seconds

That's Friday, in calendar week 53 of 2026.
```

For past/future asymmetry: `since/` is *always* positive (elapsed),
`until/` is positive if the target is future and explicitly prefixed
`-` if it's already past (with a hint: "target already passed").
`between/` is always positive and notes which endpoint is later.

**Named shorthands** (Phase A-extra, optional):

- `new-year`, `easter-YYYY`, `christmas`, `equinox-spring`,
  `solstice-winter` → resolve to the next occurrence after `now()`.
- `eoy`, `eow`, `eom`, `eoq` → end-of-year / week / month / quarter
  (for deadline reasoning).
- `tomorrow`, `next-monday`, `next-friday` → nearest weekday name.

Useful for the common case ("how long until the end of the quarter")
without the agent having to compute the target date itself.

**Zones + help**

| URI                         | Returns                                   |
|-----------------------------|-------------------------------------------|
| `clock:/zones`              | Common timezones + their current offsets  |
| `clock:/help`               | Onboarding skill inline                   |

### Date input format — ISO 8601 only, no ambiguity

`clock:until/`, `since/`, `between/` accept **ISO 8601 only**:

- `2027-01-01`                   — date
- `2027-01-01T18:30`             — datetime (UTC assumed if no offset)
- `2027-01-01T18:30+01:00`       — datetime with explicit offset
- `2027-01-01T18:30Z`            — datetime, explicit UTC
- `1830000000`                   — Unix epoch seconds (all-digits input)
- Named shorthands from the list above (`easter-2027`, `eoq`, …)

**Ambiguous formats are refused, not guessed.**  A call like
`clock:until/01/02/2027` returns an error:

```
✗ Ambiguous date: "01/02/2027" could be 1 Feb or 2 Jan.
  Use ISO 8601 (YYYY-MM-DD): try `clock:until/2027-01-02` or
  `clock:until/2027-02-01`.
```

Rationale: DMY vs MDY vs YMD is a locale guessing game that no
server-side heuristic can win cleanly, and silently picking one is a
subtle bug factory (the output will look plausible regardless of
which interpretation was wrong).  ISO 8601 is unambiguous, sortable,
and is what the rest of the precis surface already uses (ref slugs,
footer timestamps, memory dates).  A clear error with two
copy-paste-ready alternatives is faster than debugging a wrong
answer.

The handler detects ambiguity by checking for `/` or `.` separators
in positions consistent with DMY/MDY, and by refusing two-digit years
(`27` is also ambiguous between 1927 / 2027).

### Default output shape

```
🕒 Saturday, 25 April 2026 · 09:53 UTC · week 17 · day 115/365

UTC        2026-04-25T09:53:00Z
Local      2026-04-25T10:53:00+01:00  (Europe/Dublin, BST, Saturday)
Unix       1775988780

Use `clock:<tz>` for other timezones, `clock:until/<date>` for
durations, or `clock:/zones` for the list.
```

The human-readable weekday + month name lands at the top — the
single most useful fact for a model trying to answer "is this
Friday?" or "is tomorrow a weekend?"  Calendar week and day-of-year
are in the same line so a paper-deadline or quarter-end calculation
reads off one glance.

### Enum description shows current time + next useful durations

This is the feature that distinguishes `clock:` from "just call
`datetime.now()` in your code": the kind's `KindSpec.description`
rendered into the tool enum **includes the current time AND a few
ready-to-use durations** every time the MCP tool schema is built.
An agent browsing the available kinds sees something like:

```
clock — Current time + durations.  Now: Saturday 2026-04-25 09:53 UTC
        (week 17, day 115/365).  251d to new-year · 66d to eoq ·
        244d to christmas.  Use `get(id='clock:')` for detail,
        `clock:<tz>` for a timezone, or `clock:until/<ISO-date>` for
        a custom duration.
```

The durations shown are a small fixed rotation chosen for broad
utility: one long-horizon anchor (`new-year`), one quarterly rhythm
(`eoq` — end of current quarter), and one seasonal reference
(`christmas` outside December; `easter-YYYY` inside December).  All
three update live with the enum render.

This gives the agent a reasonable "now" baseline *and* a rough sense
of scale for the current time-of-year, without any tool call.
Minimal LLM-grounding primitive: no mystery about what day it is or
whether the deadline is "soon."

### Implementation

- Backed by `datetime.datetime.now(datetime.UTC)` and `zoneinfo.ZoneInfo`.
- No network, no external deps beyond stdlib.
- `KindSpec.description` upstream tweak: allow `description` to be
  either a `str` (current) or a `Callable[[], str]` evaluated lazily
  at tool-schema build time.  `clock:` uses a callable so the enum
  shows live time; all other kinds keep their static strings with no
  change.  One-line check in `RegisteredKind.description` property:

  ```python
  @property
  def description(self) -> str:
      d = self.spec.description
      return d() if callable(d) else d
  ```

- Footer: `_Clock: host=<hostname> tz=<local> now=<ISO UTC>_` — names
  the host so an agent that's routed across multiple precis processes
  can tell which one answered.
- `cost_hint="free"`.  Zero I/O.

### Out of scope for this kind

- Arbitrary date arithmetic (`monday-of-week-42 + 3 business days`,
  business-day calendars, holiday calendars beyond the handful listed
  in named shorthands).  That's a whole datetime library; `dateutil`
  in `calc:` covers the advanced cases via SymPy's unit system.
  `clock:` handles the 95% of "how long until" / "how long since"
  that the agent needs day-to-day.
- Calendar / event data.  A future `calendar:` kind would plug into
  ical / Google Calendar.  `clock:` is strictly "clock and
  stopwatch" — no event list, no RSVP state.
- Scheduling future reminders.  That's `todo:` territory.

---

## Skill 1 — `ideation`

`precis/skills/ideation/SKILL.md`.

```yaml
---
name: ideation
description: >
  Break out of a stuck problem by drawing 3 random items from the corpus
  (or the blast radius of the problem statement) and synthesising a
  non-obvious answer.  Includes a self-evaluation loop — if the
  synthesis is weak, redraw and retry, but keep the original problem
  statement fixed as the anchor.  Use when conventional search has
  plateaued.
user-invocable: true
argument-hint: [problem]
allowed-tools: [get, put]
applies-to: [random]
tags: [creativity, synthesis, brainstorm]
---
```

### Anchor-first discipline

Random draws pull the agent *away* from the problem.  Without a fixed
anchor, successive rounds drift into free-association — fun, but not
useful.  The anchor is the one-sentence problem statement from step 1:
it is written **once** and copied verbatim into every retry.  Never
rephrase it mid-loop; if the re-read of the anchor makes the original
phrasing feel wrong, that is itself the finding — stop ideating and
redefine the problem instead.

### Workflow

**Setup (run once):**

1. **Anchor.** Write the problem in one sentence.  Pin it — this is
   the invariant across all attempts.
2. **Success criteria.** In one more sentence, name what a "good"
   synthesis looks like.  (e.g. *"a testable hypothesis I hadn't
   considered"*, *"a dependency I was missing"*, *"a reframing that
   changes the first step"*.)  This is the yardstick used in step 5.

**Loop (repeat up to 3 times):**

3. **Draw — prefer distal.** Default call is uniform-random across the
   whole corpus: `get(id='random:?n=3')`.  The whole point of ideation
   is to pull ideas from *far away*.  Close-neighbour picks
   (`random:<anchor>?radius=0.3`) mostly return what the agent would
   have found via ordinary `search(…)` — they're not generative, they
   just re-state the problem in its own neighbourhood.  Reach for
   blast-radius (`random:<anchor>?radius=0.6`) only as a fallback
   when two wild rounds have failed relevance scoring, not as the
   first move.

   Rule of thumb: if a pick "obviously relates" to the anchor, the
   draw was probably too tight.  Surprise is the signal.

   **Optional: salt with wisdom.**  If two of the three picks are
   coming from the same corpus and the synthesis feels homogeneous,
   replace one pick with a wisdom draw:
   `get(id='random:?corpus=wisdom&n=1')` for fully wild, or
   `get(id='random:<anchor>?corpus=wisdom&n=1&radius=0.6')` for
   semantic-near.  Chengyu and proverbs carry compressed narrative
   frames that can crack open a synthesis the corpus alone won't.
   Don't over-rely on this — the wisdom corpus is small (~100 entries
   in v1) and the same proverb showing up twice means the loop is
   exhausted, not insightful.
4. **Force-synthesise.** For each pick, one sentence:
   *"What does this remind me of about the anchor?"*  Then one
   paragraph synthesis that uses **at least two** of the three picks
   and references the anchor verbatim.  No discarding picks.
5. **Self-evaluate.**  Score the synthesis against the success
   criteria from step 2 on three axes:

   - **Novelty** (1–5): would I have arrived here without the draws?
   - **Relevance** (1–5): does it still address the anchor, or has it
     drifted into an adjacent problem?
   - **Actionability** (1–5): is there a next step I could actually take?

   **Pass** if total ≥ 11 AND relevance ≥ 3.  **Fail** otherwise.
6. **Decide.**
   - *Pass:* stop.  `put(type='memory', text=<synthesis>, tags=['ideation', <topic>])`.
     Include the anchor sentence verbatim at the top of the memory.
   - *Fail:* log one sentence explaining *why* the synthesis missed
     (drifted / thin / obvious / picks too unrelated), then loop to
     step 3 with a fresh draw.  Keep the anchor unchanged.

**After 3 failed rounds:** stop.  The tool is not the right move for
this problem right now.  Record the anchor + the three failure notes
as a single memory tagged `['ideation', 'miss']` — future rounds on
the same topic benefit from knowing what didn't land.

### Guardrails

- **Never mutate the anchor during the loop.**  If you feel the urge,
  that's a signal to exit the skill, not to rephrase.
- **Never skip the self-evaluation.**  The loop is the point — a
  single round with no evaluation is just "draw three cards and call
  it done."
- **Relevance ≥ 3 is a hard floor.**  A highly novel but off-topic
  synthesis is a failure of this skill, not a success; pursue it
  separately if it's interesting, but don't count it here.

---

## Skill 2 — `iching-consult`

`precis/skills/iching-consult/SKILL.md`.

```yaml
---
name: iching-consult
description: >
  When stuck on a decision, draw an I-Ching archetype and use its
  three-layer interpretation (heritage / modern / cognitive) as a
  re-framing prompt.  Not for prediction; for forced perspective
  shift.  Pair with the wisdom corpus when extra cultural depth helps.
user-invocable: true
argument-hint: [question]
allowed-tools: [get]
applies-to: [iching]
tags: [reflection, decision, reframing]
---
```

### Workflow

1. **Articulate.**  State the question in one sentence.  The act of
   precise articulation is half the value of the skill.
2. **Draw.**  Two choices:
   - `get(id='iching:')` — uniform random across all 64.  Use when
     truly stuck; the archetype is genuinely unbiased.
   - `search(type='iching', query=<your question>)` — pick the
     archetype semantically nearest to the question.  Use when you
     want a *fitting* lens rather than a *random* one.  The skill
     loses some serendipity but gains relevance.
3. **Read all three layers in order.**  Heritage → modern → cognitive.
   Resist the urge to skip.  Each layer surfaces a different angle:
   - **Heritage** carries the archetypal feel — what kind of situation
     is this?
   - **Modern** carries the systems / engineering reading — what is
     it operationally?
   - **Cognitive** carries a *named* tool — Steel Man, Pareto, Inversion,
     Survivorship Bias — that you can apply directly.
4. **Apply the cognitive lens to the question.**  This is where the
   skill earns its keep.  Take the named tool from step 3 and run it
   on the original question:
   - If the lens is a *heuristic* (Steel Man, First Principles, OODA),
     execute it: produce the steel-man, decompose to first principles,
     run an Observe-Orient-Decide-Act loop.
   - If it's a *principle* (Pareto, Lindy, Goodhart's Law), check the
     question against the principle: where in this situation is the
     20%? what is being optimised that distorts what's measured?
   - If it's a *bias* or *fallacy* (Sunk Cost, Confirmation Bias,
     Survivorship Bias), audit the question for the bias's signature.
5. **Optional: pair with wisdom.**  If the cognitive lens lands but
   you want extra texture, draw one wisdom entry semantically near
   the question:
   `get(id='random:<question>?corpus=wisdom&n=1&radius=0.5')`.  One
   pithy proverb is enough; more dilutes.
6. **Write 3–5 sentences:**
   - What the lens forces you to consider that you weren't already.
   - Whether the framing changes the question itself.
   - One concrete next action (or the explicit decision *not* to act).
7. **The oracle is a randomiser, not an authority.**  If the reading
   feels forced, name that explicitly and keep the next-action you'd
   already arrived at.  This skill earns trust by being honest about
   misses.

### When this skill is the right tool

- You have already done the obvious analysis and are still stuck.
- The decision has multiple framings and you don't know which to use.
- You suspect a bias is influencing your judgement but can't name it.

### When it is not

- You need a fact, a calculation, or a calendar — use `web:`, `calc:`,
  or `clock:` instead.  The oracle has nothing to add to "what time
  is the meeting."
- The decision is already made and you're seeking validation —
  randomisers are a poor source of confirmation.

---

## Phasing

**Phase A** — `clock:` kind.  Stdlib-only, ~100 LOC + tests.  Ships
the `KindSpec.description`-as-callable upstream tweak (one-line property
change + a test that the enum shows live time).  Cheapest useful kind;
land first.

**Phase B** — `rng:` kind.  No new deps.  Tests its own seed mechanism
end-to-end.  ~150 LOC + tests.

**Phase C** — `random:` kind + `?tag=` filter + skill `ideation`.
Depends on `acatome-store`.  Add `max_distance` and tag-filter args
to `PgvectorIndex.search_text` (small upstream change).  ~250 LOC +
tests.

**Phase D** — `iching:` kind + skill `iching-consult`.  Move
`docs/iching.txt` → `src/precis/data/iching.yaml`.  Add validator test.
Register hexagrams as refs in the `iching` corpus at handler init so
`search(type='iching', …)` works.  ~300 LOC + tests + 80 KB data.

**Phase E (content, not code)** — `wisdom` corpus.  Author
`pips/packages/precis-mcp/data/wisdom-starter.yaml` with ~100 entries
(50 chengyu, 30 proverbs, 20 stoic).  Add `wisdom` to
`acatome_store.models.CORPUS_SEEDS`.  Add an `acatome-store
ingest-wisdom` CLI subcommand that reads the YAML, creates Refs +
Blocks + embeds.  Run the ingest once locally to populate the
default-cluster store.  Document for users in `iching-consult`'s
"may not be loaded" hint.

Phases A–D are independently mergeable.  `clock:` is the only one
that touches shared registry plumbing (the description-callable
change); B/C/D are purely additive.  Phase E is content authoring
and can happen in parallel with code work or after — it adds value
to `random:` but doesn't block any kind.

---

## Out of scope

- **Per-line I-Ching texts and changing-lines mechanism.**  The data
  file does not carry per-line interpretations.  Coin-throw simulation
  with old-yin/old-yang is dropped.
- **Persisting throws / picks to a journal kind.**  Agents can do
  that manually via `put(type='memory', …)` if they want a record.
- **Per-user RNG state.**  Stateless; if reproducibility is needed,
  pass `?seed=`.
- **Tarot, runes, bibliomancy.**  Easy to add later if the data file
  pattern proves itself — same handler shape, different YAML.
- **LLM-generated `iching:` commentary.**  The three-layer prose is
  the value; if the agent wants further synthesis it should pipe
  `iching:` output through `think:` separately.
- **Original-language semantic search for wisdom.**  The English
  meaning + gloss approach works fine for v1.  Multilingual
  embeddings can be added later if useful.
- **Pluggable lens / wisdom packs.**  No `~/.precis/iching-lenses/`
  or `~/.precis/wisdom/` overlay mechanism.  Cognitive lenses are
  intrinsic to hexagrams; wisdom packs are just YAML files re-ingested.

---

## Open questions

1. **Trigram-based recommendations.**  Should `iching:` expose a
   trigram-level access pattern — e.g. `iching:trigram/water/over/fire`
   to find the hexagram with that composition, or
   `iching:upper/water` to list the eight hexagrams with Water on
   top?  Useful for compositional reasoning but adds URI surface.
   Defer to v2; the binary-string lookup already covers the
   "I know what I want" case.
2. **Should `random:` accept multiple seed strings?**  E.g.
   `random:?seeds=a,b,c` returns the centroid neighbourhood.  Skip
   for v1; reconsider if `ideation` benefits.
3. **Wisdom corpus growth model.**  Once the starter ships, how does
   the library grow?  Curated PRs to the precis-mcp repo?  A separate
   repo of community submissions?  Each user's local YAML?  Probably
   start with curated PRs (small, high-quality) and let the community
   pattern emerge later.
4. **Cognitive-lens canonical slugs.**  Today the `cognitive.name`
   field is free text matched on equality — "Pareto Principle" must
   spell that exact way to be reverse-indexed.  Worth adding a
   normalised slug field per cognitive entry to harden against typos
   when the data file evolves?  Probably yes; one-line addition.
