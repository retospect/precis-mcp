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

### Step 3: candidate hint (opt-in; never substitutes)

**Important: we deliberately do NOT auto-resolve 404 DOIs to a
different DOI based on text similarity.** A DOI is an assertion of
identity; silently swapping it for a same-titled paper is exactly
the kind of citation-hygiene failure the rest of `provenance` exists
to *detect*. We considered this in an earlier revision of the plan
and dropped it.

What we *do* support, when a 404 DOI is paired with bibliographic
metadata in a ``BibEntry``: surface candidate Crossref matches as an
informational hint, without acting on them. The report shows:

```
⚪ Unknown DOI (1)
- **#47** · `10.1234/typo` — Crossref returned 404
  Bibliographic hints suggest possible matches:
    - 10.5678/foo (score 94) — "Quantum error correction…" Smith 2019
    - 10.5678/bar (score 81) — "Similar title…"            Smith 2020
  Action: verify which (if any) is the citation you meant.
```

No auto-substitution. The candidate DOIs are NOT silently health-
checked, NOT written through to the store, NOT matched against the
notice graph. The caller (human or LLM) decides what to do.

Implementation cheap: when ``BibEntry`` hints are present and a DOI
returns 404, issue one ``/works?query.bibliographic=…&query.author=…
&rows=5`` call and emit the top results into the report's "Unknown
DOI" section as text. Same opt-in shape as Phase 2.5 metadata
verification — only fires when bibliographic hints exist.

**Why this is safe but the previous "fuzzy resolution" wasn't:**

| concern | "auto-resolve" (rejected) | "candidate hint" (Phase 5) |
|---|---|---|
| Wrong-paper substitution | silent | impossible — never substitutes |
| Threshold tuning | load-bearing on score≥80 | irrelevant — all candidates shown |
| Hallucinated DOI failure mode | masked (fuzzy "finds" something) | preserved (status='unknown') |
| Stale Crossref index | gives confidently-wrong answer | gives hint, marked as such |

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
| `json`     | structured payload for downstream tooling |
| `csv`      | flat row-per-DOI |

Earlier revisions of the plan included a ``view='fuzzy'`` that would
auto-substitute 404 DOIs with their nearest-title-match. Removed —
silent identifier substitution is the failure mode the rest of
`provenance` is designed to *detect*. The salvageable subset (surface
candidate matches as an informational hint, never substitute) lives
in Phase 5 / Step 3 above, attached to the `Unknown DOI` report
section rather than as its own view.

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
    --suggest-candidates                 # for 404 DOIs with bib hints,
                                         # show possible matches (no
                                         # substitution; advisory only)
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
                            `candidate_search(hints)` helpers — the latter
                            returns Crossref ranked matches as hints only,
                            never used to substitute a 404 DOI)

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
provenance.check_doi(doi, *, store, hub, suggest_candidates=False) -> ProvenanceResult:
    1. Validate DOI format. If malformed → return early.
    2. Look up local ref by DOI in ref_identifiers (may be absent; that's OK).
    3. GET /works/{doi} from Crossref (existing http cache, TTL=7d).
       - 404 + suggest_candidates=True + BibEntry hints present →
         call candidate_search(hints), attach top-N to result.candidate_dois
         (advisory only; the queried DOI's status STAYS `unknown`, no
         substitution happens, candidates are not health-checked).
       - 404 otherwise → status='unknown', return.
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
  - `/works?query.bibliographic=...` TTL = 30d (candidate-hint queries rarely change)

### Failure modes

| condition | behaviour |
|-----------|-----------|
| DOI malformed | status='malformed', no HTTP call |
| DOI 404 | status='unknown', surface in report |
| DOI 404 + suggest_candidates + hints | status STAYS unknown; candidate hint list attached for human review |
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
- 10.x/typo-doi — Crossref returned 404
  Possible matches (run with --suggest-candidates):
    - 10.5678/foo (score 94) — "Quantum widgets…"
    - 10.5678/bar (score 81) — "Similar widgets…"
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
| **3.5** | Numbered-result rendering across all views, matching the project's standardised LLM-output convention (`utils/search_merge.py:208`). | ~0.5 day |
| **4** | Transitive cite-check (depth=1). Reuse `ingest/citations.py`. Skill `precis-preflight.md`. | ~0.5 day |
| **5** | `paper` `view='health'` shim. Candidate-DOI hint for 404s with bib metadata (read-only; never substitutes). `view='exists'` shortcut. | ~0.5 day |

Total: ~5 days. Phase 1 alone is shippable and answers the original
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

## Phase 3.5: numbered-result rendering (standardised LLM output)

### Motivation

The Phase 2 batch renderer groups results by severity. That reads well
to a human but creates an off-by-one hazard for an LLM: when 250 DOIs
go in and a severity-sorted report comes out, the model can't easily
say *"the 47th paper in your bib is retracted"* — the visual grouping
breaks the input-order correspondence.

The codebase already has a standardised solution for this in
`utils/search_merge.py:208`: every rendered hit gets a 1-based index
via `enumerate(rendered, 1)` so downstream consumers (LLM or human)
can quote `result #7` unambiguously. Provenance reports need the same
discipline.

### Scope

Apply to all three views (`default`, `blockers`, `json`). The input
position — *not* the position within a severity group — is the
authoritative index. A 🔴 blocker at input line 47 reads as
`#47` in every view; the blockers view simply hides the in-between
entries, but never renumbers.

**Default view (markdown):**

```
## 🔴 Blocker (1)
- **#47** · `10.5678/bad` — B 2022 · _Bad paper_
  - 🔴 **retraction** · 2022-08-14 · notice DOI: `10.5678/bad-r1`

## 🟢 Clean (1)
- **#1**   · `10.1234/clean` — A 2020 · _Clean paper_
- **#3**   · `10.9999/eoc`   — — · _Concerned_ (now 🟠, see above)
```

**Blockers view:** same numbering, but only 🔴/🟠 entries appear.
The suppression note becomes informational only; the LLM can still
reference `#47` from the surrounding prose without re-counting.

```
_view='blockers' — entries #1 and #3 hidden (🟡/🟢)._
## 🔴 Blocker (1)
- **#47** · `10.5678/bad` …
```

**JSON view:** add an `input_index` field (1-based) on each result
object, alongside the existing fields. Downstream tooling parsing
the JSON can apply its own severity rule without losing the bib-line
mapping.

```json
{
  "count": 250,
  "results": [
    {"input_index": 1, "doi": "10.1234/clean", "status": "ok", ...},
    {"input_index": 2, "doi": "10.5678/bad", "status": "ok",
     "overall_severity": "blocker", ...},
    ...
  ]
}
```

### Implementation

- Thread `input_index` through `check_dois`: assign at the point where
  the result list is built (currently in `_provenance/check_dois`
  results slot index). Cheapest: add an `input_index: int` field to
  `ProvenanceResult` (default `0`), populated by the batch wrapper.
- `render_batch` reads `.input_index` instead of recomputing from
  enumeration order; the severity-grouped sections lose nothing
  because they render the index alongside the DOI.
- `_provenance_report._render_per_doi_block` prefixes each block with
  `**#{r.input_index}**`.
- Single-DOI path (`render_single`) doesn't show the index — there's
  only one result, the ambiguity doesn't exist.

### Acceptance criteria for Phase 3.5

- `check_dois(["10.x/a", "10.x/b", "10.x/c"])` returns three results
  with `input_index in (1, 2, 3)` regardless of completion order from
  the thread pool.
- A model can reproduce the original input order from the JSON view
  via `sorted(results, key=lambda r: r["input_index"])`.
- Re-running `view='blockers'` with the same input never renumbers
  surviving entries — `#47` in the default view is `#47` in the
  blockers view.

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
  validate, then existence-check via Crossref `/works/{doi}`. 404
  surfaces as `status='unknown'`; we do *not* auto-substitute a
  different DOI based on title similarity. See "Rejected: fuzzy
  DOI auto-resolution" below.
- **What to do with 404 DOIs that carry bib metadata?**
  Phase 5 emits Crossref candidate matches as an *advisory hint*
  attached to the unknown-DOI report section. The supplied DOI's
  status stays `unknown`; candidates are not health-checked, not
  written through, not matched against the notice graph. The
  caller decides whether any of them is the intended citation.

## Rejected: fuzzy DOI auto-resolution

An earlier plan revision had a ``view='fuzzy'`` that would auto-
substitute a 404 DOI with its nearest Crossref title-match above a
``score≥80`` threshold. **Dropped.** Reasons:

1. **A DOI is an identity assertion.** Silently swapping it for a
   same-titled paper is exactly the citation-hygiene failure the
   rest of `provenance` exists to detect. Phase 2.5 flags wrong-
   paper citations *loudly*; an auto-resolve path would do it
   *silently* — internally inconsistent.
2. **Score≥80 is structurally unsound.** Crossref's scoring is
   opaque and query-dependent. The same threshold means "exact
   match" for one query and "shared four content words" for
   another. Tuning a single threshold is impossible.
3. **Wrong fix for the dominant failure mode.** When an LLM
   hallucinates a DOI, fuzzy resolution "finds" something plausible
   and presents it as the fix — confidently masking the
   hallucination instead of surfacing it.
4. **The candidate-hint subset (Phase 5) preserves the useful
   signal** (Crossref's ranked matches when bib hints exist)
   *without* the silent substitution.

## Deferred

- Slack/email alerting on `STATUS:retracted` becoming true on an
  existing local ref. User said on-demand, not push. Easy to wire into
  `precis maintenance` later via a daily sweep over
  `refs WHERE retraction_checked_at < now() - interval '30d'`.
- Per-publisher fallbacks. Some publishers (Elsevier, PLOS) bury
  retractions in non-standard fields. For v1 trust Crossref; revisit
  only on observed misses.
- Ranking signal for candidate hints (Phase 5). v1 just shows the
  Crossref ``/works?query.bibliographic=...`` ranked list. Could
  later cross-reference with chunk-side title cosine similarity to
  surface better candidates first — but only as a display order
  change, never as an auto-substitution rule.

---

## Resolved planning decisions

All items below were proposed defaults in earlier revisions of this
doc; they have since been confirmed against the code and are now part
of the Phase 1 spec.

### Schema / wiring

- **Migration number → `0002_provenance.sql`.** `0001_initial.sql` is
  the only on-disk migration; `store/migrate.py` applies in sorted
  filename order. Earlier comments in `_links_ops.py` referencing
  `0005_link_relations.sql` are from a pre-consolidation history; the
  current state has everything in `0001`.

- **Notice slug rule → `<paper-slug>-r<n>` / `-c<n>` / `-e<n>`.**
  Retraction = `r`, correction = `c`, EoC = `e`; `n` is 1-based when a
  paper has multiple notices of the same kind. Slug construction
  lives in `ingest/provenance.py` — not in the generic
  `ingest/crossref.py:_normalize()` path, which keeps producing
  first-author+year+title-word slugs for primary papers. The existing
  Crossref normaliser is *not* re-used for notices because its
  heuristic mangles them (authors are typically `Editors` or a journal
  name; titles are `Retraction of …` — would collide constantly).

- **CLI bib input → plain-text DOI-per-line, not bibtex.**
  `bibtexparser` is not currently in `pyproject.toml`; adding it would
  be a new dep for marginal value. CLI takes `--refs preflight.txt`
  (one DOI per line, blank lines and `#` comments allowed). Bibtex
  support can land later behind a `[bib]` extra if anyone asks. The
  Phase 2.5 metadata-verify path takes structured input via the kind
  API (`view='verify'` with `[{doi,title,authors,year},...]`) rather
  than parsing bib syntax.

- **STATUS-tag concurrent writes → safe as-is.**
  `_tags_ops.py:135-202` does `DELETE ... WHERE namespace=X` followed
  by `INSERT ... ON CONFLICT DO NOTHING`. Two concurrent provenance
  checks on the same DOI compute the same STATUS value (deterministic
  from the Crossref response), so last-write-wins is harmless and the
  INSERT is idempotent. No explicit transaction wrapping needed in
  Phase 1.

### Behaviour / report shape

- **Re-check cadence → TTL=7d clean / TTL=30d once
  `retraction_status='retracted'`.** Reinstatements are rare; this
  saves Crossref traffic on the long tail of known-retracted papers.
  Implemented as a single `interval` lookup in the cache layer keyed
  on the current `retraction_status` column.

- **Multiple notices on one paper → full chronology in the report,
  dominant status only in the column.** The `refs.retraction_status`
  column is a query-index summary; the `links` table is the full
  history (one `retracted-by` / `corrected-by` / `concern-raised-by`
  row per notice). `_provenance_report.py` iterates all `update-to`
  entries in date order when rendering markdown; the structured `json`
  view emits the full list too.

- **Non-DOI input → "Unchecked (n)" section in the report.** Lines
  in `--refs` that don't parse as a DOI (comments, blanks, free-form
  text, no-DOI entries) are surfaced in a final `## Unchecked (n)`
  section with the reason ("no DOI on line 42", "malformed DOI", …),
  so the user sees the coverage gap rather than silently shipping
  with unchecked citations.

- **Aggregate severity for transitive cite hits → no hardcoded
  threshold; model judges at render time.** The classifier emits raw
  per-cite findings (count + severity per cited retraction); the
  report-rendering model (Opus in the agent loop) applies
  common-sense judgement — "cites 17 retracted works, treat as
  blocker" vs "cites one peripherally, note only." The `json` view
  always exposes the raw counts so deterministic downstream tooling
  can apply its own rule.

### Implementation notes

- **Test fixtures → hand-rolled dict + `unittest.mock.patch`.**
  `tests/ingest/conftest.py` exposes `sample_crossref_response`;
  `tests/ingest/test_crossref.py` patches the `Crossref` client with
  `@patch("precis.ingest.crossref.Crossref")`. Phase 1 follows the
  same pattern — no VCR / cassette infra to add. Capture a few real
  Crossref responses (retracted paper, EoC paper, clean paper, 404)
  by hand, save them as dict fixtures in
  `tests/ingest/conftest.py`.

## Resolved Phase 3 source (confirmed 2026-05-30)

- **Retraction Watch dataset fetch.** Two viable, currently-live
  sources documented by Crossref:

  - **Primary — Crossref Labs API:**
    ``https://api.labs.crossref.org/data/retractionwatch?<email>`` —
    note the unusual query format (email is the bare query string,
    not ``?mailto=...``). CSV, ~40 MB, daily updates. Documented at
    https://www.crossref.org/labs/retraction-watch/.

    **Caveat:** Crossref labels this Labs/experimental:
    *"They may disappear without warning and/or perform erratically.
    We plan to model and support it via our REST API in future."*

  - **Secondary — GitLab mirror:**
    ``gitlab.com/crossref/retraction-watch-data`` —
    Crossref-hosted, established September 2024 with daily updates
    (475+ commits as of May 2026). Same data, more stable URL.

  **Phase 3 implementation pattern:** try Labs API first (smaller pin,
  official endpoint), fall back to GitLab on 404 / connection error.
  Log clearly which source served the data so a future Crossref REST
  migration is detectable from the sync job logs.

## Deferred / out of scope

- **Stale local title after publisher relabel.** Some publishers
  retroactively prepend `(Retracted)` to the paper title. Our
  `refs.title` won't reflect that unless we re-fetch metadata. The
  `STATUS:retracted` tag carries the signal where it matters; the
  title is cosmetic. Out of scope.
