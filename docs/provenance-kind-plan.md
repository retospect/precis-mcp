# `provenance` kind — retraction & amendment monitoring

**Status:** plan / not yet implemented
**Owner:** unassigned
**Last revised:** 2026-05-30

## Goal

Given one DOI or a list of DOIs (e.g. the references of a manuscript
about to be released), emit a triaged report on the *health* of each
paper: is it retracted, under expression of concern, corrected,
superseded, or clean? For a paper found in the local store, persist the
finding (status column, `STATUS:` tag, `retracted-by` link) so future
calls see it without re-fetching. Notices themselves become first-class
paper refs so `get(kind='paper', id=notice_slug)` works.

The use case is the agent / human preflight before submitting or
publishing: "here are 250 papers I cite; release in a week; flag
anything I need to deal with."

## Why this is small

The schema already carries `refs.retraction_status`, `retracted_at`,
`retraction_reason`, `retraction_url`, `retraction_checked_at`
(`0001_initial.sql:306-313`) — a thoughtful past designer anticipated
this. The relations table already has `cites`/`cited-by` and
`supersedes`/`superseded-by`. The work is: a new tool kind, a small
fetch+classifier module, a few new link relations, an RW reason cache,
and a CLI subcommand.

---

## Data sources

### Crossref — the master key

Since December 2023 Crossref is the consolidated source of retraction
data. They carry:

- **Publisher-reported notices** via `message.update-to` on the affected
  paper's record, and via `message.relation['is-corrected-by']` etc. on
  the inverse side.
- **The Retraction Watch Database (RWDB)**, distributed under CC-BY
  through Crossref Labs as part of the December 2023 RW→Crossref
  partnership.

So a single endpoint (`GET https://api.crossref.org/works/{doi}`) is
sufficient for the *what happened* signal. Reason taxonomy lives in the
RW columns of the same dataset.

### Retraction Watch — the reasons

Retraction Watch (retractionwatch.com, run by the Center for Scientific
Integrity) is no longer a separate API to hit; their dataset is
distributed via Crossref. They remain the authoritative source of the
*reason taxonomy* (>100 codes such as `+Falsification/Fabrication of
Data`, `+Author Unresponsive`, `+Concerns/Issues With Data`) — the
human-readable "why was this retracted" that Crossref's structural
fields don't carry. We pull this once a month and cache locally.

There is no separate `retraction.org` canonical source. Crossref is the
hub now.

### Optional: Semantic Scholar cross-check

Already in the codebase (`ingest/semantic_scholar.py`). Treat as
advisory only — log discrepancies, don't act on them.

---

## DOI verification (anti-hallucination)

LLM-generated bibliographies frequently contain DOIs that look right but
don't resolve. The tool must distinguish:

1. **Well-formed and resolvable** → proceed
2. **Well-formed but unknown to Crossref** → mark as `unknown`, surface
   in report; do *not* silently skip
3. **Malformed string** → reject with hint
4. **Resolvable but the agent's intent was different** → reverse-lookup
   from bibliographic hints

### Step 1: format validation

Regex (case-insensitive): `10\.\d{4,9}/[^\s]+`. Strip leading
`doi:`/`https://doi.org/` prefixes. Anything that doesn't match → return
`{status: 'malformed', hint: '...'}` without making an HTTP call.

### Step 2: existence check

`GET /works/{doi}` against Crossref:

- 200 → record exists; canonical metadata is the response
- 404 → record does not exist (`{status: 'unknown'}` in report)
- 429 / 5xx → retry with backoff, then `{status: 'check_failed'}`

This is the same call we'd make for retraction lookup, so verification
is free — no separate verification pass.

### Step 3: fuzzy resolution (opt-in)

When the agent passes a DOI that 404s *and* supplies bibliographic
hints (title / first author / year), try:

```
GET /works?query.bibliographic=<title>&query.author=<surname>&rows=5
```

Crossref returns ranked candidates with a `score` field. Take the top
hit if score ≥ 80 (empirically a strong match), surface the alternative
in the report:

```
🟢 corrected DOI
  Supplied: 10.x/typo-doi (not found)
  Resolved: 10.y/canonical-doi (Crossref score 94 · title matches)
  → using resolved DOI for the rest of the check
```

This is opt-in via `view='fuzzy'` on `get(kind='provenance', ...)` or
`--fuzzy` on the CLI. Off by default — too risky to silently rewrite
DOIs.

### Step 4: existence as a separate kind action

For "is this DOI real?" without the full provenance check:

```python
get(kind='provenance', id='10.x/foo', view='exists')
# → { status: 'ok' | 'unknown' | 'malformed', canonical_doi, title, ... }
```

Cheap and useful in its own right — agents constantly need this.

---

## Surface changes

### New tool kind: `provenance`

Stateless tool kind (sibling to `calc`, `patent`, `web`). Reads
Crossref + local RW cache, writes through to `refs.retraction_*` and
`links` when the DOI matches a local paper ref.

```python
get(kind='provenance', id='10.1038/s41586-021-03819-2')
get(kind='provenance', q='10.x/a, 10.x/b, 10.x/c')
get(kind='provenance', q='manuscript.bib', view='from-bibfile')
```

**Views:**

| view | output |
|------|--------|
| (default) | full triaged report grouped by severity, markdown |
| `blockers` | only 🔴/🟠 (the must-act list) |
| `exists`   | DOI existence check only (cheap) |
| `fuzzy`    | as default + opt-in fuzzy resolution for 404 DOIs |
| `json`     | structured payload for downstream tooling |
| `csv`      | flat row-per-DOI |

**Other kwargs:**

- `force=True` — bypass `retraction_checked_at` TTL (default 7d)
- `transitive=1` — depth of cite-walk (default 1; 0 disables)
- `ingest_notices=True` — auto-ingest notice DOIs as paper refs
  (default True; see "Notices as refs" below)

### `paper` kind gains `view='health'`

```python
get(kind='paper', id='smith2019gene', view='health')
```

Thin shim: resolves slug → DOI → calls into provenance. Lets agents
who already have a slug skip the DOI lookup.

### New link relations (migration `0002_provenance.sql`)

| slug | inverse | description |
|------|---------|-------------|
| `retracted-by` | `retracts` | Target retracts source |
| `retracts` | `retracted-by` | Source retracts target |
| `corrected-by` | `corrects` | Target corrects source |
| `corrects` | `corrected-by` | Source corrects target |
| `concern-raised-by` | `raises-concern-about` | EoC notice attached to source |
| `raises-concern-about` | `concern-raised-by` | Source raises EoC about target |

`supersedes` / `superseded-by` already exists — used for preprint →
journal version updates.

Update `precis/store/types.py:Relation` Literal to match.

### New closed-namespace tags

Extend the `STATUS:` closed namespace for paper refs:

- `STATUS:retracted`
- `STATUS:concern`     (expression of concern)
- `STATUS:corrected`   (one or more corrigenda)

Mutually exclusive within the namespace; dominance order:
`retracted > concern > corrected`. Closed-namespace tags enforce
one-per-target (`_tags_ops.py:151`).

### Notices as paper refs (decision 8: confirmed yes)

When provenance finds a notice DOI (`10.x/foo-r1` retracting
`10.x/foo`), the notice itself is a published article — a small one,
but a real DOI with metadata. We auto-ingest it as a paper ref so:

- `get(kind='paper', id=<notice-slug>)` works
- The `retracted-by` link has a real target on both ends
- The notice can be searched and cited like any other ref

**Scope of auto-ingest:**

- 🔴 retraction / withdrawal / removal notices: **always** auto-ingest
- 🟠 expression of concern notices: **always** auto-ingest
- 🟡 corrigendum / erratum: **only** if the notice DOI already exists
  in store OR `ingest_notices='all'` is passed (would explode ref count
  otherwise — corrigenda are extremely common)
- 🟢 addendum / clarification / new-version: only as `supersedes` link
  target if the new version is itself ingested through normal channels

The auto-ingest path is minimal: Crossref metadata only, no PDF fetch
(notices rarely have useful body content). Sets `provider='crossref'`
and tags `STATUS:notice` (new tag — flags the ref as a notice rather
than a primary paper, so search results can suppress it if asked).

### New provider

```sql
INSERT INTO providers (slug, description) VALUES
    ('retraction_watch', 'Retraction Watch dataset (CC-BY via Crossref)');
```

### CLI

```
precis jobs check-provenance \
    --doi 10.x/a --doi 10.x/b           # one or more --doi flags
    --refs manuscript.bib                # OR a bibtex file
    --slug-pattern 'smith*'              # OR live slugs from the store
    --since 7d                           # only recheck stale entries
    --transitive depth=1                 # cite-check depth (0 disables)
    --fuzzy                              # opt-in DOI fuzzy resolution
    --format md|json|csv
    --out preflight.md
```

Wires into `cli/main.py` next to `precis jobs watch-patent`.

### Skill cards (discovery surface)

- `data/skills/precis-provenance.md` — how to call the kind, what views
  mean, severity tiers
- `data/skills/precis-preflight.md` — the manuscript-release recipe

Without skills the kind is invisible to small-model agents.

---

## Architecture

```
src/precis/ingest/
    provenance.py          (NEW — Crossref update-to + RW lookup + classifier)
    crossref.py            (extend — add `fetch_update_to(doi)` and
                            `fuzzy_resolve(hints)` helpers)

src/precis/handlers/
    provenance.py          (NEW — KindSpec + get() returning Response)
    _provenance_report.py  (NEW — markdown/json/csv rendering)
    paper.py               (extend — add 'health' view)

src/precis/jobs/
    provenance_rw_sync.py  (NEW — monthly RW CSV refresh)

src/precis/cli/
    provenance.py          (NEW — `precis jobs check-provenance`)
    main.py                (register subcommand)

src/precis/migrations/
    0002_provenance.sql    (NEW)

src/precis/data/skills/
    precis-provenance.md   (NEW)
    precis-preflight.md    (NEW)
```

### Core flow for a single DOI

```
provenance.check_doi(doi, *, store, hub, fuzzy=False) -> ProvenanceResult:
    1. Validate DOI format. If malformed → return early.
    2. Look up local ref by DOI in ref_identifiers (may be absent; that's OK).
    3. GET /works/{doi} from Crossref (existing http cache, TTL=7d).
       - 404 + fuzzy=True + hints present → try fuzzy_resolve(hints),
         swap DOI if score ≥ 80.
       - 404 + no fuzzy → status='unknown', return.
    4. For each entry in message.update-to:
         - classify type → (severity, status enum)
         - GET /works/{notice_doi} for notice title/date
         - if severity ≥ 🟠 and ingest_notices: upsert notice as paper ref
    5. If local ref exists:
         - look up RW reason from provenance_rw_cache (keyed on paper_doi)
         - upsert refs.retraction_status (highest-severity status seen)
         - upsert links: ref --retracted-by--> notice_ref (when notice in store)
         - apply STATUS:{retracted,concern,corrected} closed tag
         - touch retraction_checked_at = now()
    6. If transitive depth > 0 and notices on the paper are clean:
         - pull cited DOIs from chunks via ingest/citations.py
         - for each cited DOI, repeat steps 1-5 at depth-1
         - only surface results with severity ≥ 🟠 (suppress corrections)
    7. Return ProvenanceResult.
```

### Concurrency / rate

- Crossref polite pool needs `mailto`; reuse `PRECIS_CROSSREF_MAILTO`.
- Batch path uses `asyncio.gather` with `Semaphore(8)`.
  ~250 DOIs → ~30s warm cache, 60-90s cold.
- HTTP responses go through `store/_cache_ops.py`:
  - `/works/{doi}` TTL = 7d
  - `/works?filter=update-to.DOI:...` TTL = 24h (reverse lookup is more dynamic)
  - `/works?query.bibliographic=...` TTL = 30d (fuzzy resolution rarely changes)

### Failure modes

| condition | behaviour |
|-----------|-----------|
| DOI malformed | status='malformed', no HTTP call |
| DOI 404 | status='unknown', surface in report |
| DOI 404 + fuzzy + hints | try resolve, fall back to 'unknown' |
| Crossref 429 | exponential backoff, surface remaining quota |
| Crossref 5xx | retry 3×, then status='check_failed' |
| RW cache stale (>45d) | still return Crossref data; mark `rw_freshness='stale'` |
| Notice DOI itself 404 | log warning, keep the parent finding without notice metadata |

---

## Schema delta (`0002_provenance.sql`)

```sql
-- 1. New link relations
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('retracted-by',          FALSE, 'retracts',              'Target retracts source'),
    ('retracts',              FALSE, 'retracted-by',          'Source retracts target'),
    ('corrected-by',          FALSE, 'corrects',              'Target corrects source'),
    ('corrects',              FALSE, 'corrected-by',          'Source corrects target'),
    ('concern-raised-by',     FALSE, 'raises-concern-about',  'EoC notice attached to source'),
    ('raises-concern-about',  FALSE, 'concern-raised-by',     'Source raises concern about target');

-- 2. New provider
INSERT INTO providers (slug, description) VALUES
    ('retraction_watch', 'Retraction Watch dataset (CC-BY via Crossref)');

-- 3. New kind
INSERT INTO kinds (slug, is_numeric, title, description) VALUES
    ('provenance', FALSE, 'Provenance / health check',
     'Check DOIs for retractions, expressions of concern, corrections, and version updates.');

-- 4. RW reason cache
CREATE TABLE provenance_rw_cache (
    record_id        BIGINT PRIMARY KEY,           -- RW dataset row id
    paper_doi        TEXT NOT NULL,
    notice_doi       TEXT,
    notice_type      TEXT NOT NULL,                -- 'Retraction' | 'Correction' | 'EoC' | ...
    reasons          TEXT[] NOT NULL DEFAULT '{}', -- RW reason codes (+Falsification, etc.)
    retraction_date  DATE,
    raw              JSONB NOT NULL DEFAULT '{}'::jsonb,
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX provenance_rw_paper_doi_idx ON provenance_rw_cache (paper_doi);

CREATE TABLE provenance_rw_sync (
    source_url        TEXT PRIMARY KEY,
    last_full_sync_at TIMESTAMPTZ,
    last_row_count    INT
);
```

No `refs` changes — columns already exist.

`STATUS:retracted`/`concern`/`corrected`/`notice` are added to the
closed-namespace allow list in `_tags_ops.py` (Python-side enforcement,
not DDL).

---

## Report shape (markdown view, canonical)

```
# Provenance check — 250 DOIs · 2026-05-30

Crossref freshness: 247/250 (3 unknown DOIs)
Retraction Watch dataset synced: 2026-05-12 (18 days ago)

## 🔴 Blockers (1)

### 10.1234/foo · Smith et al. 2019 · "Title"
- **Retracted** 2022-08-14 via notice [10.1234/foo-r1]
- Reason: data fabrication (+Falsification/Fabrication of Data)
- Action: drop the citation or replace the supporting argument

## 🟠 Review (3)

### 10.5678/bar · Doe 2023
- **Expression of Concern** issued 2024-03-10
- Concern: image duplication under investigation
- Action: re-read; check whether your claim depends on the contested bit

### 10.9999/baz · Lee 2022 (transitive)
- Itself clean, but **cites retracted** 10.x/retracted-thing at chunk #42
- Action: check whether the cited claim is load-bearing in baz's argument

## 🟡 Corrections (12)

| DOI | Notice | Type | Likely impact |
|---|---|---|---|
| 10.x/aaa | 10.x/aaa-c1 | corrigendum | affiliation — benign |
| 10.x/bbb | 10.x/bbb-c1 | erratum | corrected equation in §3 — READ |
| … | | | |

## 🟢 Info (4)

- 10.x/preprint → superseded by 10.y/journal (2025-11)
- (3 more) …

## Unknown (3)
- 10.x/typo-doi — Crossref returned 404 (try --fuzzy)
- 10.x/garbled — malformed (extra slash)
- 10.x/missing — Crossref returned 404
```

`json` returns the same data structured; `blockers` truncates to 🔴/🟠.

---

## Severity classification table

| Crossref `update_type` | `retraction_status` | severity |
|------------------------|---------------------|----------|
| `retraction`, `partial_retraction` | `retracted` | 🔴 blocker |
| `withdrawal`, `removal` | `retracted` | 🔴 blocker |
| `expression_of_concern` | `expression_of_concern` | 🟠 review |
| `correction`, `corrigendum`, `erratum` | `corrected` | 🟡 note |
| `addendum`, `clarification` | `corrected` | 🟢 info |
| `new_edition`, `new_version` | NULL | 🟢 info (use `supersedes` link) |

Cite-walk transitive findings inherit the severity of the cited paper's
notice but cap at 🟠 (a retracted citation never blocks the citing
paper itself; it's a "review and judge" signal).

---

## Phasing

| phase | scope | est. effort |
|-------|-------|-------------|
| **1** | Single-DOI Crossref path. DOI verification (steps 1-2). `handlers/provenance.py` minimal. Migration `0002`. Writes through to `refs.retraction_*` + links + STATUS tags. Notice auto-ingest for 🔴/🟠. Skill card `precis-provenance.md`. | ~1.5 days |
| **2** | Batch (`q='doi1,doi2'`), CLI (`precis jobs check-provenance`), `view='blockers'`, `view='json'`, async fan-out with semaphore. | ~0.5 day |
| **2.5** | Citation metadata verification (`view='verify'`). Token-set Jaccard against Crossref metadata. New report section "Metadata mismatch". | ~0.5 day |
| **3** | Retraction Watch cache. `jobs/provenance_rw_sync.py` monthly cron (wired into existing `precis maintenance run`). Join into report. | ~1 day |
| **4** | Transitive cite-check (depth=1). Reuse `ingest/citations.py`. Skill `precis-preflight.md`. | ~0.5 day |
| **5** | `paper` `view='health'` shim. Fuzzy DOI resolution (step 3 of verification). `view='exists'` shortcut. | ~0.5 day |

Total: ~4.5 days. Phase 1 alone is shippable and answers the original
ask for any DOI you can name.

---

## Phase 2.5: citation metadata verification

A real DOI pointing to a *different* paper than the bib claims is a
worse failure than a missing DOI — it ships silently and the
manuscript ends up citing work the author never read. LLM-generated
bibliographies do this constantly (right DOI, wrong title; surnames
swapped; year off). Same workflow trigger as the health check
("preflight before release"), same Crossref response already in hand
— so we bundle it into the `provenance` kind rather than spinning up
a parallel kind.

### Trigger

Only runs when the input carries bibliographic metadata:

- `.bib` file or DOI-per-line text file with `--verify` on the CLI
- `view='verify'` on the kind with a structured input
  `[{doi, title, authors, year}, ...]`

Bare `q='10.x/a,10.x/b'` skips verification — no metadata to check
against.

### Comparison pipeline

The goal is to be insensitive to formatting / transliteration noise
that varies between publishers and citation styles, while still
catching substantive differences. We diff each field separately, then
emit per-field scores and let the report-rendering model judge what's
"typo" vs "wrong paper."

**String normalisation (applied to title and author surnames):**

```
1. unicodedata.normalize('NFKD', s)              # decomposes diacritics,
                                                 # super/subscripts, ligatures,
                                                 # full-width forms, etc.
2. drop combining characters                     # Müller → Muller, naïve → naive
                                                 # also: H₂O → H2O, ﬁ → fi, ² → 2
3. lowercase
4. replace every non-alphanumeric char with ' '  # commas, colons, dashes,
                                                 # quotes (smart + straight),
                                                 # em/en-dashes, parens,
                                                 # markup ({}, \emph), …
5. collapse whitespace
6. tokenise (split on space)
7. drop stopwords {the, a, an, of, in, on, for, and, to}
```

NFKD does the heavy lifting: it handles diacritics, super/subscript
digits, ligatures, and Unicode compatibility forms in one call. We do
not need a hand-rolled table for `²→2`, `ﬁ→fi`, `½→1/2`, full-width
Latin, etc.

**German-phonetic alternative form** (applied only to author
surnames, because that's where it matters):

```
Replace ä→ae, ö→oe, ü→ue, ß→ss     # then run the pipeline again
```

This is a *second* normalised form, not a replacement. A surname
matches if either NFKD-strip *or* German-phonetic form matches between
the input and Crossref. Covers Müller↔Muller (NFKD) *and*
Müller↔Mueller (phonetic). Same applies to Schröder/Schroeder,
Weiß/Weiss, etc.

**Comparison metric:** token-set Jaccard, not sequence/Levenshtein.

```
jaccard(A, B) = |A ∩ B| / |A ∪ B|
```

Token-set Jaccard is invariant to:
- Word order ("Role of X in Y" ↔ "Y: the role of X")
- Subtitle separators (colon, em-dash, ` - `)
- Articles at start ("The role…" ↔ "Role…")
- Trailing punctuation, markup leakage
- Stopword presence

It still penalises a substantively different word, which is the
actual signal we want.

**Things we deliberately do not normalise:**

- Greek letters used semantically (`β-cell` vs `beta-cell`) — NFKD
  won't touch them; a hand-built mapping has too many edge cases
  (μ vs micro vs u, Σ vs Sigma vs sum). The token diff stays visible
  in the report; the model judges.
- Math symbols (≤, ±, ×) — surfaced as-is in the diff.
- Acronyms (`mRNA` vs `messenger RNA`) — surfaced as diff; model
  judges.

### Per-field thresholds

No hardcoded pass/fail thresholds in v1. The classifier emits raw
scores; the report-rendering model (Opus) applies common-sense
judgement. The JSON view always exposes the raw scores so deterministic
downstream tooling can apply its own rule.

| field | comparison | what's surfaced |
|-------|-----------|-----------------|
| **First-author surname** | normalised exact match (both NFKD and phonetic forms) | match / mismatch |
| **Title** | token-set Jaccard after normalisation | score 0.0-1.0 + the diff (added/removed/changed tokens) |
| **Year** | numeric, ±1 tolerance for online-first vs print | match / off-by-1 / mismatch |
| **Journal** | report-only, no gating | input vs Crossref string |
| **Page numbers** | report-only, no gating | input vs Crossref range |

### Report addition

New section in the markdown report between **🔴 Blockers** and **🟠
Review**, because a wrong-paper citation is the same hazard class as
a retraction:

```
## ⚠️ Metadata mismatch (2)

### 10.1038/nphys123  (you cited as "Smith 2019: Quantum X")
- Crossref says:   Jones, Lee, Park — "Quantum Y in Z systems", 2019
- Title token overlap: 0.40 (added: y, z, systems; removed: x)
- First-author surname: mismatch (Smith ≠ Jones)
- → likely wrong DOI — you may have meant a different paper

### 10.1234/foo  (you cited as "Müller 2020")
- Crossref says:   Mueller — same paper, year 2021 (online-first 2020)
- Surname: match (via German-phonetic normalisation)
- Year: off-by-1 (online-first vs print)
- → probably a citation-style difference, not an error
```

### Opt-in / opt-out

- `view='verify'` on the kind → runs verification alongside the health
  check
- `--verify` on the CLI → same
- Default off when the input is just DOIs (no metadata to verify against)
- Default off when `view='blockers'` or `view='exists'` is requested
  (those views are about health/existence, not citation hygiene)

### Acceptance criteria for Phase 2.5

- `Müller` matches `Mueller`, `Schröder` matches `Schroeder`
- `H₂O is essential` matches `H2O is essential` (Jaccard = 1.0)
- `"The role of beta cells in diabetes"` matches `"Role of β-cells in
  diabetes mellitus"` with score ≥ 0.6 and the diff identifies
  `beta-cells` vs `cells` and `mellitus` as the variable tokens
- A bib entry with DOI for paper A but title for paper B (no token
  overlap) is surfaced in the `Metadata mismatch` section, not silently
  passed
- The JSON view exposes per-field raw scores; nothing in the pipeline
  hardcodes a pass/fail threshold

---

## Acceptance criteria

- `get(kind='provenance', id='10.1038/nature05095')` returns the Hwang
  stem-cell retraction (well-known smoke test).
- `get(kind='provenance', q='<250 real DOIs>')` completes <90s cold,
  <30s warm.
- Re-running within 7d hits cache (no Crossref traffic, verifiable via
  the http cache hit counter).
- A retracted paper found locally gains `STATUS:retracted`, a
  `retracted-by` link to the notice ref, `refs.retraction_status =
  'retracted'`, and the notice DOI is itself fetchable via
  `get(kind='paper', id=<notice-slug>)`.
- Hallucinated DOI (`10.1234/this-is-fake-9999`) returns
  `status='unknown'` with a hint, not a silent success.
- `view='exists'` returns in <2s for a cached DOI, <5s cold.
- `view='json'` round-trips through a JSON parser; `view='csv'` opens
  in a spreadsheet.
- `get(kind='skill', id='precis-provenance')` resolves with usage
  examples.

---

## Resolved decisions

- **Notice DOIs as refs?** Yes. Auto-ingest 🔴/🟠 notices unconditionally;
  🟡 corrigenda only if already in store or `ingest_notices='all'`.
  Notices get `STATUS:notice` tag so search can suppress.
- **Crossref or Retraction Watch as primary?** Crossref. Since the
  December 2023 RW→Crossref partnership, Crossref is the consolidated
  source — they hold publisher-reported `update-to` data *and*
  distribute the RW dataset under CC-BY. RW remains the source of the
  reason taxonomy, fetched via Crossref Labs.
- **DOI verification before treating as canonical?** Yes — format
  validate, then existence-check via Crossref `/works/{doi}`. Fuzzy
  resolution opt-in via `--fuzzy` / `view='fuzzy'`.

## Deferred

- Slack/email alerting on `STATUS:retracted` becoming true on an
  existing local ref. User said on-demand, not push. Easy to wire into
  `precis maintenance` later via a daily sweep over
  `refs WHERE retraction_checked_at < now() - interval '30d'`.
- Per-publisher fallbacks. Some publishers (Elsevier, PLOS) bury
  retractions in non-standard fields. For v1 trust Crossref; revisit
  only on observed misses.
- Confidence weighting for fuzzy hits. v1 uses a fixed score≥80
  threshold; could later use chunk-side title cosine similarity to
  improve precision.

---

## Open issues

Items below need a decision (or confirmation) before or during Phase 1.
Proposed defaults are stated; flip them in code review if wrong.

### Blocking — resolve before coding

- **Notice slug generation.** The existing
  `ingest/crossref.py:_normalize()` heuristic (first-author + year +
  first-title-word) produces collision-prone garbage for retraction
  notices (authors are typically `Editors` or the journal name, titles
  are `Retraction of …`). **Proposed:** notice slugs are
  `<retracted-paper-slug>-r<n>` for retractions, `-c<n>` for corrections,
  `-e<n>` for EoCs (n = 1-based sequence when multiple). Slug
  construction lives in `ingest/provenance.py`, not in the generic
  Crossref normaliser, so the regular path is unaffected.

- **RW dataset fetch URL.** Retraction Watch data is distributed via
  Crossref since Dec 2023 but the access path isn't a single stable
  endpoint. **Proposed:** Phase 3 fetches the CSV mirror at
  `gitlab.com/crossref/retraction-watch-data` (stable, versioned,
  CC-BY documented in the repo). Document the URL and last-known-good
  schema in `provenance_rw_sync.py`. Phase 1 doesn't need this — RW
  reasons can be NULL in the report until Phase 3 lands.

- **Migration number.** Only `0001_initial.sql` exists on disk but
  `store/_links_ops.py` comments reference `0005_link_relations.sql`
  — implying older migrations were consolidated into `0001`.
  **Proposed:** confirm by checking `_migrations` table on a live DB;
  if 0001 is the latest applied, new migration is `0002_provenance.sql`.
  This is a 30-second check, not a real decision.

- **Bibtex parser dependency.** The CLI mentions
  `--refs manuscript.bib` but `bibtexparser` isn't currently a dep.
  **Proposed:** drop bib parsing from Phase 2; CLI accepts a
  DOI-per-line text file via `--refs preflight.txt`. Bibtex support
  can land later behind a `[bib]` extra if there's demand. Keeps the
  dependency surface honest.

### Important — resolve early but won't block start

- **Re-check cadence for already-retracted papers.** Plan says
  TTL=7d for everything. **Proposed:** TTL=7d for clean / corrected /
  EoC papers; TTL=30d once `retraction_status='retracted'` is set
  (reinstatements are rare; saves Crossref traffic on the long tail).

- **Multiple notices on one paper.** A paper can accumulate corrigenda
  *and* a retraction over time. `refs.retraction_status` stores only
  the dominant one. **Proposed:** the renderer iterates all `update-to`
  entries chronologically; the column carries the dominant status only
  as a query index. The `links` table is the full history (one row per
  notice). Confirm `_provenance_report.py` renders the full chronology
  in markdown, not just the dominant entry.

- **Concurrent writes.** Two agents call `provenance` on the same DOI
  simultaneously. **Proposed:** all writes use `INSERT … ON CONFLICT
  DO UPDATE`, idempotent under race. Need to verify the STATUS-tag
  upsert path in `_tags_ops.py:151` is also race-safe (the
  one-per-target invariant); if not, wrap the read-modify-write in a
  short transaction.

- **Non-DOI bib entries.** Older work / theses / books have no DOI.
  **Proposed:** the report lists these in a final "Unchecked (n)"
  section with the reason ("no DOI in input"), so users know the
  coverage gap. Silent drops are misleading for the preflight
  use case.

### Worth flagging — Opus applies judgement at report time

- **Aggregate severity for transitive cite hits.** A paper that cites
  one retracted source is 🟠; one that cites thirty is effectively 🔴.
  **Resolution:** no hardcoded threshold. The classifier emits the
  count and per-cite severity to the report; the model rendering the
  final markdown (Opus, in the agent loop) applies common-sense
  judgement — "this paper cites 17 retracted works, treat as blocker"
  vs "cites one retracted work peripherally, note only." The structured
  `json` view always exposes the raw counts so downstream tooling can
  apply its own rule if it wants determinism.

- **Stale local title after publisher relabel.** Some publishers
  retroactively prepend `(Retracted)` to the paper title. Our
  `refs.title` won't reflect that unless we re-fetch metadata. **Out
  of scope:** the `STATUS:retracted` tag carries the signal where it
  matters; the title is cosmetic.

- **Test fixtures for Crossref.** Recorded responses are needed for
  tests. **Proposed:** mirror whatever pattern `ingest/crossref.py`
  tests already use — confirm during Phase 1 implementation; copy don't
  invent.
