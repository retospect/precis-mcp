# Finding chase — trace cited claims back to their primary source

**Status**: draft (pre-review), **revised 2026-05-31 for Path B + B-ii**
(`kind='citation'` shipped in `0007_citation_kind.sql`; this design
recasts findings as *chain heads* over those citation hops, with a
sibling-worker chase per ADR 0018).
**Owner**: `src/precis/handlers/finding.py` (new),
`src/precis/workers/chase.py` (new). Migration `0004_*.sql` already
applied; no further schema work needed.
**Predecessors**:
- [`storage-v2.md`](./storage-v2.md) — establishes refs / chunks /
  links / derived queue
- [`extract-once.md`](./extract-once.md) — establishes the stub-vs-real
  merge mechanic (`ref_identifiers` collapse on re-ingest)
**Related ADRs**:
- [0006 — tri-identifier scheme](../decisions/0006-tri-identifier-scheme.md)
  (we ride `cite_key` as the LaTeX/BibTeX handle)
- [0007 — derived queue, no jobs table](../decisions/0007-derived-queue-no-block-jobs.md)
- [0008 — drop slug, `cite_key` is canonical human form](../decisions/0008-drop-slug-identifier-normalisation.md)
- [0017 — derived-queue family + `artifact_kinds` registry](../decisions/0017-derived-queue-family.md)
  (substrate tables `ref_artifacts` + `artifact_kinds` are valid;
  §4 WorkerHandler refactor superseded — see 0018)
- [0018 — persistent discovery layer + sibling ref-level workers](../decisions/0018-persistent-discovery-layer.md)
  (the chase worker follows `run_paper_segments_pass`, not a base-class
  subclass)
**Shipped neighbour**:
- `kind='citation'` (migration `0007_citation_kind.sql`,
  `src/precis/handlers/citation.py`) — the *single-hop* verified
  claim → source quote primitive. Findings are *chain heads* over
  citations; see §"Relationship to `citation`" below.

## Problem

A document being authored by a client (an agent or a human via the
CLI) states a *quantitative or empirical claim* — "the device was
held at 2.4 kV for 30 s on Si/SiO₂ with a Cu top contact" — and finds
a supporting citation in the corpus, say **miller2020**. But when
we open miller2020 at the relevant page, the claim is itself
attributed to **fischer2013** with no further detail. The "real"
provenance — who actually measured it, under what conditions — is
one or more citation hops away, often outside the local corpus.

Today this dead-ends in two places:

1. The agent has no structured way to express *"my claim cites
   miller2020, but miller2020 cites fischer2013 for that same
   number"*. The only primitive is `put(kind='memory', ...)` plus
   manual `link(...)` calls, which gives a flat note with no
   built-in fulfilment workflow.
2. fischer2013 may not be in the local corpus. The only "fetch this
   for me" mechanism is appending its DOI to the plaintext file
   `request_doi.md` (`src/precis/handlers/paper.py:336-355`). No
   structured row, no worker pickup, no link back to the claim that
   asked for it.

The net effect: every chase is done by hand, the provenance chain is
not retrievable by future agents asking the *same* question, and the
work to ingest fischer2013 doesn't auto-resume the chase once the
PDF lands.

## Goal

Make the citation-chase a first-class, persistent, retrievable
artefact of the system. Specifically:

- The claim becomes a `finding` ref — chain head — embedded and
  searchable like any other ref. Carries setup context
  (`meta.scope`) so the same number under different setups produces
  distinct findings.
- Each hop of the chase is a row in `links` with relation
  `derived-from`, walked directly between ref/chunk endpoints —
  no per-hop record created by the worker (per Path B-ii — the
  shipped `kind='citation'` is reserved for *user / verifier
  subagent* records, not chase-bot records). The chain terminates
  when we hit a primary report (a paper that *measured* the value
  rather than re-citing it).
- Missing cited papers are materialised as **stub refs**: real
  `refs` rows with identifiers (DOI / arXiv / S2 id) and
  `pdf_sha256 IS NULL` — the column predicate alone identifies a
  stub. `precis stats` lists them; no tag needed (the `STATUS:`
  slot on paper refs is reserved for provenance state).
- When a stub's PDF lands, the merge happens automatically (DOI
  hits the same `ref_id` via `ref_identifiers`;
  chunks / embeddings / summaries flow through the existing derived
  queue).
- A **sibling worker** (`precis.workers.chase.run_finding_chase_pass`,
  modelled on `run_paper_segments_pass` per ADR 0018) advances
  findings whose chain is incomplete, one hop per pass.

The substrate tables introduced by ADR 0017 (`ref_artifacts`,
`artifact_kinds`) are *not* consumed by the chase — chase state
lives on the finding itself (`meta.chain`, `STATUS:tracing|established`).
The substrate remains a useful generic facility for future ref-level
work; the chase just doesn't need persistent per-pass queue state.

## Non-goals

- **Automatic PDF fetching.** Sci-Hub / Unpaywall / publisher
  login is out of scope. The chase worker creates stubs
  (`pdf_sha256 IS NULL`); the user (or a separate, opt-in fetcher
  worker) is responsible for getting the PDF into the inbox.
- **Bulk finding extraction from arbitrary documents.** A finding
  is created by an explicit caller (`put(kind='finding', …)`) —
  not by mining every paper for every numeric value.
- **Disagreement resolution / contradictions between chains.** If
  fischer2013 says 2.4 kV and a separate chain says 2.6 kV, we
  record both findings and the existing `contradicts` relation can
  link them; we do not pick a winner.
- **Re-running the chase when upstream papers change.** Once a
  chain terminates, it stays terminated. Manual re-trigger only.
- **Citing a finding externally.** Findings are *internal certainty
  records* — they are not a citable surface. They never appear in
  `\cite{}`. While the chase is in flight, the finding's `pub_id`
  is a *placeholder* the agent drops in text; at finalisation,
  `precis resolve` substitutes the primary paper's `cite_key`.
- **LLM passes during the chase.** No title canonicalisation, no
  mis-citation comparison, no scope enrichment. KISS, model-light.
  The chase walks the citation graph deterministically; LLM cost
  per finding ends up dominated by what the *agent* did at
  `put(...)` time, not by what the worker does.
- **Mis-citation detection as a worker.** The
  `relations.misattributes` / `misattributed-by` vocabulary stays
  in the schema as an **optional curator surface** (a human or a
  separate analysis tool can write a `misattributes` link by hand
  via `link(rel='misattributes', …)`). The chase worker does not
  generate them.
- **Auto-creating `citation` records from chase hops.** Per Path
  B-ii — the shipped `kind='citation'` is reserved for *verifier-
  subagent* records (user-written). The chase walks `links` +
  `chunks` directly and records the chain on the finding's
  `meta.chain`; it does not pollute the bibliography surface with
  bot-asserted citations.

## Relationship to `citation` (shipped)

Two ref kinds, two clear jobs:

| Kind | Shipped? | Role | Authored by | Lifecycle |
| --- | --- | --- | --- | --- |
| `citation` | yes (`0007`) | one verified claim → one source quote | user / verifier subagent | write-once |
| `finding` | this design | chain head: claim + setup + provenance chain to primary | user (initial); worker (chain extension) | `STATUS:tracing` → `STATUS:established` |

A finding is the *answer* the agent reaches for when searching
("what evidence do we have for X?"). A citation is the *atomic
evidence record* a writer attaches to a claim they are making
right now.

The chase walks the `links` graph directly; it does **not** create
citations as it goes (Path B-ii). The finding's
`meta.chain = [{ref_id, chunk_id?}, …]` records the walk;
`meta.primary_cite_key` and `meta.via_cite_keys` snapshot the
chain in cite_key form for cheap rendering. Citations remain
strictly user / verifier authored — when a writer pulls a finding
into a document and `precis resolve` substitutes
`[finding-pub_id]` → `\cite{primary_cite_key}`, the writer can
optionally create a backing `citation` record by hand with the
exact verbatim quote — but that's a separate step the writer
controls, not something the chase pre-populates.

## What a finding is

A finding is a **synthesised artefact**: the proven endpoint of a
chase, carrying the fact, its **setup context**, and the full
provenance trail down to the primary source. It is not "any claim
someone wrote down" — it is the *result* of grounding such a claim
against a primary report.

The setup context is load-bearing: *"2.4 kV held for 30 s"* alone
is not a finding. *"2.4 kV held for 30 s on Si/SiO₂ MOSCAPs with a
Cu top contact in N₂"* is. Two findings differ if their setup
differs, even when the bare number is identical. The skill (see
"Skill guidance" below) is responsible for not collapsing them.

Concretely a finding ref carries:

| Field | Shape | Source |
| --- | --- | --- |
| `refs.title` | short claim title — *"gate-bias 2.4 kV / 30 s on Si/SiO₂"* | author at `put` (no LLM rewrite) |
| `chunks` ord=0, kind=`finding_body` | claim + setup envelope as flowing prose (one chunk; setup folded in, no separate context chunk) | author at `put` |
| `card_combined` (ord=-1) | title + body + primary cite_key | computed; refreshed via DELETE+INSERT when the chain terminates (not an LLM pass — just text concat) |
| `refs.meta->'scope'` JSONB | structured slice of the setup for filtering (`{"electrode": "Cu", "ambient": "N2", ...}`) | author at `put` only — no LLM extraction |
| `refs.meta->'chain'` JSONB | ordered `[{ref_id, chunk_id?}, …]` of every hop the chase walked, primary last | chase worker, one append per pass |
| `refs.meta->'primary_cite_key'` | cite_key of the terminal ref | chase worker on termination |
| `refs.meta->'via_cite_keys'` | ordered cite_keys of intermediate refs (the begat chain, excluding primary) | chase worker on termination |
| `links --derived-from-->` chain | direct + indirect sources (mirror of `meta.chain` for graph queries) | chase worker, one row per hop |
| `STATUS:tracing` tag | in flight | initial state |
| `STATUS:established` tag | terminal — primary source identified | replaces `tracing` on termination |

> **Note on dormant 0004 vocab.** Migration `0004_finding_and_queue_family.sql`
> shipped `chunk_kinds.finding_context` as a separate chunk_kind. Path B
> folds setup prose into `finding_body` instead. The
> `chunk_kinds.finding_context` row stays in place (forward-only
> migrations; harmless dormant vocab; promote later if a real need
> for a separately-embeddable setup chunk surfaces).

The ref exists during the chase too — it has to, because the chain
is attached to it. But until `STATUS:established`, it's a *claim in
flight*, not a usable finding. Default
`search(kind='finding', ...)` filters to `:established`; in-flight
chases surface only via `search(kind='finding', q=..., status='tracing')`
or `precis stats`.

## Conceptual model

```
 ┌──────────────────────────────────────────────────────────────┐
 │  AGENT-AUTHORED DOCUMENT                                     │
 │  "...the device was held at 2.4 kV for 30 s [miller2020]..." │
 └─────────────────────────┬────────────────────────────────────┘
                           │  put(kind='finding', title='2.4kV/30s on Si/SiO2',
                           │       body='2.4 kV held for 30 s on Si/SiO2 MOSCAPs;
                           │             Cu top contact, N2 ambient, room temp.',
                           │       scope={'electrode':'Cu','ambient':'N2'},
                           │       cited_in='miller23a#42')
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  finding ref  (pub_id=ab12c3)                                │
 │  • one finding_body chunk (claim + setup as flowing prose)   │
 │  • meta.scope = {electrode:'Cu', ambient:'N2'}               │
 │  • meta.chain = [{ref_id: miller2020, chunk_id: <42>}]       │
 │  • tag STATUS:tracing                                        │
 │  • link  --derived-from-->  miller2020 chunk:42   (frontier) │
 └─────────────────────────┬────────────────────────────────────┘
                           │  chase worker (pass 1)
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  reads miller2020 chunk:42; detects "[12]" inline citation;  │
 │  S2 references list says [12] = fischer2013 (DOI x).         │
 │  fischer2013 not in corpus → create stub ref                 │
 │       (ref_identifiers: doi=x, s2=y; cite_key=fischer13;     │
 │        pub_id=k4j7m2)                                        │
 │  (stub-ness implied by pdf_sha256 IS NULL — no tag written)  │
 │  link finding --derived-from--> fischer2013   (ref-level)    │
 │  append to meta.chain (now 2 entries)                        │
 │  finding stays STATUS:tracing                                │
 └─────────────────────────┬────────────────────────────────────┘
                           │  user drops fischer2013.pdf into inbox
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  precis-watch → precis_add(). DOI on PDF matches the stub's  │
 │  ref_identifier → same ref_id. Stub upgrade:                 │
 │    • UPDATE refs.pdf_sha256 (was NULL, becomes the new hash) │
 │    • INSERT new (pdf_sha256, content_hash) ref_identifiers   │
 │    • extract blocks/chunks for the new PDF; embed via queue. │
 │  No tag to clear — the column predicate flipped itself.      │
 └─────────────────────────┬────────────────────────────────────┘
                           │  chase worker (pass 2)
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  re-claims the finding (target ref now has chunks).          │
 │  Locates the chunk in fischer2013 that states the value      │
 │  (lexical + ANN top-1 within fischer2013).                   │
 │  Frontier chunk has no further [N] cite → CHAIN TERMINATES:  │
 │    • link finding --derived-from--> fischer2013 chunk:N      │
 │    • meta.chain extended with {fischer2013, chunk_id:N}      │
 │    • meta.primary_cite_key = 'fischer13'                     │
 │    • meta.via_cite_keys = ['miller23a']                      │
 │    • re-emit card_combined (DELETE+INSERT at ord=-1)         │
 │    • tag finding STATUS:established; remove STATUS:tracing.  │
 │  No LLM call, no misattribution writeback.                   │
 └──────────────────────────────────────────────────────────────┘
```

## Derived-queue family (substrate present; chase doesn't use it)

ADR 0007 established the chunk-level derived queue
(`chunk_embeddings`, `chunk_summaries`).
[ADR 0017](../decisions/0017-derived-queue-family.md) introduced
the substrate for untyped per-ref / per-link / per-pdf derived
state (`*_artifacts` tables + `artifact_kinds` registry).
[ADR 0018](../decisions/0018-persistent-discovery-layer.md)
shipped the first ref-level *consumer* (`run_paper_segments_pass`)
and explicitly chose the **sibling-worker** pattern over the
parameterised `WorkerHandler` base class originally proposed in
ADR 0017 §4.

**Path B does the same.** The chase worker
(`precis.workers.chase.run_finding_chase_pass`) is a sibling
function modelled on `run_paper_segments_pass` — not a subclass
of anything, not a writer to `ref_artifacts`. Chase state lives
on the finding itself (`meta.chain`, the `STATUS:` tag, the
`derived-from` links). No per-pass queue rows; no
`chase_citation` / `resolve_citation:s2` artifacts.

> **Dormant 0004 vocab.** Migration `0004_finding_and_queue_family.sql`
> seeded `artifact_kinds` with `chase_citation` and
> `resolve_citation:s2` rows. Path B leaves them in place
> (forward-only migrations; harmless). They become live again only
> if a future redesign moves the chase to a queue-artifact model
> — not planned.

The substrate remains valuable for *other* ref-level work
(periodic retraction sweeps, link-level scoring, per-pdf OCR).
This design just doesn't consume it.

**Retraction-checking is NOT in this design.** The
[provenance kind](provenance-kind-plan.md) — **shipped Phases
1–6**, see `src/precis/handlers/provenance.py` and
`src/precis/ingest/provenance.py` — handles retraction / EoC /
correction state synchronously via `get(kind='provenance', ...)`,
writing through to `refs.retraction_*` columns plus `links`
directly. ADR 0017 originally listed `check_retraction:crossmark`
as a planned third artifact; that has been retracted (the
provenance tool owns the work; a future periodic-backfill
scanner artifact can register under the same family if real
demand surfaces).

## Schema impact

The v2 schema already supports almost everything. This migration is
additive — no ALTER on existing tables. Single file:
`0004_finding_and_queue_family.sql`.

### New ref kind `finding`

```sql
INSERT INTO kinds (slug, enabled, title, description) VALUES
    ('finding', TRUE, 'Finding',
     'A retrievable empirical claim with explicit setup context and '
     'a provenance chain back to its primary source.');
```

### `chunk_kinds` (shipped in 0004; one row used under Path B)

```sql
-- already in 0004_finding_and_queue_family.sql:
INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('finding_body',    FALSE, 'Finding claim text (the measured value)'),
    ('finding_context', FALSE, 'Finding setup envelope (instrument, electrode, ambient, ...)');
```

Path B uses **`finding_body` only** (one ord=0 chunk per finding,
carrying claim + setup as flowing prose). `finding_context` stays
in the vocabulary as dormant — promote later if a real need for a
separately-embeddable setup chunk surfaces (e.g. setup-only
search becomes a hot query).

Standard card variants (`card_combined`, `card_title`, `card_meta`)
apply; `card_authors`, `card_abstract`, `card_keywords` are skipped
for findings (no authors / abstract / RAKE-able body of academic
length).

### `relations` (mis-citation vocabulary — optional curator surface)

```sql
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('misattributes',    FALSE, 'misattributed-by',
        'Source chunk misrepresents what the target chunk actually says'),
    ('misattributed-by', FALSE, 'misattributes',
        'Source chunk is misrepresented by the target chunk');
```

Already shipped in migration 0004. **The chase worker does NOT
write these** under Path B (no LLM in the chase). Kept in the
schema as an opt-in **curator surface**: a human (or a separate
analysis tool) can flag mis-citation by hand:

```python
link(src='miller23a~42', dst='fischer13~17',
     rel='misattributes',
     meta={"src_says": "Cu foil", "dst_actual": "Cu top contact"})
```

Surfaced via `search(relation='misattributes')` or
`get(kind='paper', view='bibliography')` if the bibliography view
opts in to displaying them later.

### `actor` (audit trail)

```sql
INSERT INTO actors (slug, description) VALUES
    ('chase',
     'Citation-chase worker. Pins set_by on chase-created stubs, '
     'derived-from links, and card_combined re-emits.');
```

Every row the chase writes pins `set_by='chase'`. Audit query:
`SELECT * FROM links WHERE set_by='chase' ORDER BY created_at DESC`.

### Status tags (data, not schema)

Materialised on first use via the existing `tags` INSERT path — no
migration. All values apply only to `kind='finding'` refs; the
`STATUS:` namespace is closed (one value per target,
`_tags_ops.py:147-151`), so reserving it for finding state keeps
other ref kinds free to use the same namespace for their own
states (e.g. provenance's `STATUS:retracted` on papers).

- `STATUS:tracing` — finding whose provenance chain is not terminal.
- `STATUS:established` — finding whose chain terminates at a primary
  report; rendering substitution becomes safe.
- `STATUS:multi_candidate` — chase paused: the frontier chunk cites
  >1 reference and disambiguation requires user / LLM input.
- `STATUS:dead_chain` — chase blocked: the target ref was
  soft-deleted (`refs.deleted_at IS NOT NULL`).
- `STATUS:cycle` — chase aborted: chain revisited a ref already on
  it.

**Stubs are identified by a column predicate, not a tag.** A stub
paper is `refs.pdf_sha256 IS NULL AND EXISTS (ref_identifiers
WHERE ref_id = r.ref_id)` — the chase handler and `precis stats`
both read that predicate directly. No `STATUS:awaiting_pdf` tag is
written; the predicate flips itself the moment a PDF lands
(see §"Stub upgrade" below). Keeps the `STATUS:` slot on `paper`
refs free for provenance's retraction tags.

### `ref_artifacts` + `artifact_kinds` (shipped in 0004, unused by chase)

Per ADR 0017 — shape is shared with future `link_artifacts` /
`pdf_artifacts` / `chunk_artifacts`. Full SQL in ADR 0017 §§1–3.
**Path B does not write to these tables**; chase state lives on
the finding's `meta` JSONB and the `links` graph. The tables and
seed rows are kept as the generic substrate for future ref-level
work that needs persistent per-pass state.

## Identifiers and the `cited_in` micro-syntax

`cite_key` is the LaTeX / BibTeX canonical handle per ADR 0008
(`miller23a`, `fischer13`). Findings ride that:

- The finding's own `cite_key` is minted at `put` time
  (`make_cite_key` from author=`finding`, year=current).
- The finding's `pub_id` is the 6-char base32 hash; it doubles as
  the in-text placeholder while the chase is in flight (`[ab12c3]`).
- External handles (DOI, arXiv, S2 id) are stored in
  `ref_identifiers` like any other ref.

The `cited_in` argument grammar (block-level optional):

```
cited_in := <handle> [ '#' <anchor> ]
<handle> := <cite_key>          // canonical — what the user types
          | <pub_id>             // also accepted (6-char hash)
          | doi:...              // also accepted
          | arxiv:...            // also accepted
<anchor> := <ord>                // chunk by ord, e.g. 'miller23a#42'
          | 'p' <page>           // page only, worker resolves chunk
          | <none>               // ref-level, worker resolves chunk
```

Examples:
```
cited_in='fischer13'           # ref-level
cited_in='fischer13#3'         # chunk ord=3
cited_in='fischer13#p7'        # page 7
cited_in='doi:10.1234/xyz'     # via DOI
cited_in='ab12c3'              # via pub_id (programmatic callers)
```

Reference parser lives at `precis.handlers.finding._parse_cited_in`.
LaTeX export from `precis resolve` drops the `#<anchor>` part —
standard BibTeX is paper-level — and renders `\cite{<cite_key>}`.

## Worker plumbing

### `precis.workers.chase.run_finding_chase_pass` — sibling worker

Modelled on `precis.workers.segment_toc.run_paper_segments_pass`
(see ADR 0018 §"Worker"). Plain function, claims findings via the
same `LEFT JOIN … IS NULL` shape as the segment worker uses for
refs without segments, but the predicate here is "finding still
tracing":

```sql
SELECT r.ref_id
  FROM refs r
 WHERE r.kind = 'finding'
   AND r.deleted_at IS NULL
   AND EXISTS (
         SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
          WHERE rt.ref_id = r.ref_id
            AND t.namespace = 'STATUS'
            AND t.value = 'tracing'
       )
 ORDER BY r.ref_id
 LIMIT %s
   FOR UPDATE OF r SKIP LOCKED;
```

For each finding `F`:

**1. Pick the current frontier** from `F.meta.chain` (last entry).
If `meta.chain` is empty (shouldn't happen — `put` writes the
initial entry), abort with `STATUS:dead_chain`.

**2. Read the cited chunk.** If the frontier is chunk-scoped,
that's the chunk. If ref-scoped, locate the chunk in the target ref
by lexical + ANN search constrained to `ref_id = frontier`, top-1
with confidence threshold.

- If the target ref has no chunks yet (stub: `pdf_sha256 IS NULL`),
  do nothing this pass (finding stays `STATUS:tracing`). Next pass
  will re-check; the chase naturally resumes when the stub gets
  chunks. No queue state, no waiting flag.
- If the target ref is soft-deleted (`deleted_at IS NOT NULL`),
  tag `STATUS:dead_chain`. Chain preserved as-is.

**3. Decide: terminal or hop?**

- **Terminal** if the frontier chunk has no outgoing `cites` link
  AND no inline bibliographic reference pattern (see §"Inline
  citation detection" below). Run the **chain-snapshot pass**
  (§"Chain-snapshot pass" below); chain is established.
- **Hop** otherwise. Determine the next reference; resolve it to a
  `ref_id` (existing via `ref_identifiers`, or create a stub —
  see §"Stub creation"); append `{ref_id, chunk_id?}` to
  `F.meta.chain`; add link `F --derived-from--> next_ref`.

**4. Cycle protection.** `meta.chain` IS the cumulative visited
record (no separate `visited` array). Before appending, check the
candidate's `ref_id` against existing chain entries. Revisit →
tag `STATUS:cycle`; chain preserved as-is.

The worker is idempotent. Re-running on a finding tagged
`STATUS:established` short-circuits at the claim query (the predicate
demands `STATUS:tracing`).

### Inline citation detection (the hard problem)

Marker does not structurally capture inline citation markers — `[12]`
or `(Miller et al. 2020)` arrive as plain chunk text. The chase
extracts them in stages:

1. **Numbered-bracket form.** Regex `\[(\d+(?:,\s*\d+)*)\]` over the
   chunk text. Each captured number indexes into the source
   paper's S2 references list (`precis.ingest.citations.citations`
   returns references in order). 1 hit → use it.
2. **Author-year form.** Regex `\(([A-Z][a-zA-Z']+(?: et al\.?)?(?:
   and [A-Z][a-zA-Z']+)?),?\s*(\d{4})[a-z]?\)`. Fuzzy-match
   `(family, year)` against S2 references. 1 hit → use it.
3. **Marker `references` fallback.** When S2 returns no references
   (preprints S2 hasn't indexed, internal reports), parse the
   source paper's `chunks WHERE chunk_kind='references'` and match
   inline cites against the parsed bib entries.
4. **Multi-candidate.** If a chunk cites >1 reference (`[12,13]` or
   "(Miller 2020; Kumar 2018)"), attach **all** candidates as
   separate `derived-from` links with `meta={"candidate":true,
   "score":0.x}`. Tag finding `STATUS:multi_candidate`; chain
   stays unappended until disambiguated (user →
   `edit(kind='finding', id=..., pick_candidate='miller23a')`).
5. **No hits.** Tag finding `STATUS:dead_chain` with
   `meta.dead_reason = 'no_inline_cite'`. Chain preserved as far
   as it got; operator can resolve manually via `edit(...)`.

### Stub creation

The chase creates a stub ref when an inline citation resolves to a
paper not in the local corpus. Minimum identifier requirement: at
least one of `{DOI, arXiv id, S2 id}` plus a title. The stub:

- Gets a minted `pub_id` (per ADR 0006 / ADR 0008 — same path as
  any real ref) and a `cite_key`.
- Carries whatever external IDs S2 returned in `ref_identifiers`.
- Has `pdf_sha256 IS NULL` — **the column predicate IS the
  stub-state signal** (no tag).
- `set_by = 'chase'` so the audit query (`SELECT * FROM refs
  WHERE set_by='chase'`) surfaces every chase-created stub.

With **only** `(authors, year, title)` from a Marker-parsed
reference list (no external IDs), the chase fuzzy-matches against
`ref_identifiers (id_kind='cite_key')` via the trigram index first
— if it hits an existing ref, reuse. Else: tag the *finding*
`STATUS:dead_chain` with `meta.dead_reason = 'no_external_id'` and
do *not* mint a blind stub. The half-known reference would never
auto-merge with a real PDF and would clutter the corpus.

### Chain-snapshot pass (no LLM)

When step 3 reaches a terminal frontier, the chase worker runs a
small deterministic snapshot pass *before* flipping the status tag:

1. **`meta.primary_cite_key`** = cite_key of the terminal ref.
2. **`meta.via_cite_keys`** = ordered cite_keys of every ref on
   the chain between `F` and the terminal, excluding endpoints
   (the begat chain).
3. **Re-emit `card_combined`** as the concatenation
   `<title> [primary=<cite_key>; via=<cite_keys…>]`. `chunks
   (ref_id, ord)` is UNIQUE, so use
   `DELETE FROM chunks WHERE ref_id=$F AND ord=-1` then
   `INSERT INTO chunks …`. `ON DELETE CASCADE` clears the stale
   `chunk_embeddings` row; new `chunk_id` has no embedding row →
   derived queue re-embeds on next pass.

> **Append-only contract on `chunks`:** chunks are append-only
> except for `ord < 0` card variants. The card re-emit is the
> *only* path that mutates a chunk's text. Document this in
> `AGENTS.md` when this ships.

The snapshot pass is **pure text concat plus a DB write** — no
LLM call, no rewrite, no enrichment. Idempotent: re-running on
an established finding computes the same `primary` / `via` from
the chain and is a no-op write on `card_combined` if text matches.
The author's original `refs.title` and `meta.scope` are preserved
verbatim from `put()` time.

### Re-enqueue on chunk arrival

Once a stub gets its PDF and chunks land, findings that were
waiting on it should resume. **Polling, no triggers.**

The chase claim query selects on `STATUS:tracing` only (see the
SQL in §"`run_finding_chase_pass`" above). When the worker hits a
frontier whose target ref is a stub (`pdf_sha256 IS NULL`), it
**does nothing** that pass and leaves the finding `STATUS:tracing`.
The next pass re-tries; if the stub has chunks by then, advancement
resumes. No queue state, no "waiting" flag.

Cost: each `STATUS:tracing` finding is re-examined every pass even
when its stub is still pending. With a `--once` cadence per worker
run, and findings in flight bounded by hundreds (in any realistic
corpus), this is negligible. If it ever bites, add an exponential
backoff on `meta.last_pass_at`.

### Stub upgrade (multi-hash refs)

`precis_add` today (`src/precis/ingest/add.py:226-227`) hits an
existing ref via `probe_existing` and short-circuits with
`_hit_result_from_db` — *no write-back*. For findings to resume
their chase, the path needs to actually upgrade the matched ref
when the new ingest has bytes the existing row lacks. Spell out
the rule:

> **Reuse existing alias machinery.** `PaperToWrite.pdf_sha256_aliases`
> (`src/precis/ingest/db_writer.py:108-114`) already supports an
> arbitrary list of `pdf_sha256` rows per ref (provisioned by
> ADR 0014's PDF-metadata write-back to record the pre-patch
> hash). The insert loop at `db_writer.py:390-400` writes them
> as `ref_identifiers(id_kind='pdf_sha256', id_value=...)`
> rows with `ON CONFLICT DO NOTHING`. C7 should **wire the
> stub-upgrade branch into this existing path** rather than build
> a parallel one — the only new thing is the `UPDATE refs SET
> pdf_sha256 = ... WHERE pdf_sha256 IS NULL` branch.

- **Multiple hashes per ref are first-class.** A single paper can
  legitimately have multiple PDF representations: publisher version,
  author preprint, arXiv update, repository scan. Every distinct
  bytes-hash is a real fact about the ref — none should overwrite
  the others. The `pdfs` and `ref_identifiers` tables already
  support this (the `pdfs` PK is per-file, `ref_identifiers
  (id_kind='pdf_sha256')` is per-row).
- **`refs.pdf_sha256` is the *primary* / canonical PDF only.** It
  exists for cheap single-PDF reads (search hit cards, the `pdfs`
  FK join). Multiple-PDF refs surface every hash via
  `ref_identifiers`.

Upgrade rule on `precis_add` hit:

```text
on probe_existing hit (ref already in DB):
    INSERT INTO pdfs (pdf_sha256, content_hash, page_count, ...)
                ON CONFLICT (pdf_sha256) DO NOTHING
    INSERT INTO ref_identifiers (id_kind='pdf_sha256',
                                  id_value=<new hash>, ref_id=<existing>)
                ON CONFLICT DO NOTHING
    INSERT INTO ref_identifiers (id_kind='content_hash', ...) ON CONFLICT ...

    IF existing.pdf_sha256 IS NULL:                 -- stub upgrade
        UPDATE refs SET pdf_sha256 = <new>,
                        pdf_pages  = <new range or NULL>,
                        pdf_role   = <new role>,
                        updated_at = now()
                 WHERE ref_id = <existing>
        extract blocks/chunks from the new PDF;
        derived queue picks up embeddings/summaries automatically.
    ELSE:                                            -- alias of a known ref
        (refs.pdf_sha256 unchanged; new hash registered as alias)
        decision deferred to operator: extract blocks for the alias
        PDF too, or rely on the canonical PDF's chunks?
        v1: NO re-extract for aliases (chunks stay tied to the
        canonical PDF). A later "re-extract this alias" verb can
        land when demand surfaces. Document the limitation.
```

Idempotent under retry (every INSERT is ON CONFLICT). The
"stub upgrade" branch is what re-enables the chase: the column
predicate (`pdf_sha256 IS NULL`) that the chase reads flips to
non-NULL, and the next chase pass advances past that stub.

The chase handler does NOT need a post-write hook; it polls
naturally. The shipped provenance kind also benefits: a stub
that gets retracted before its PDF arrives can carry
`STATUS:retracted` freely (no `STATUS:awaiting_pdf` competing
for the slot).

### Stub enrichment — deferred

Earlier drafts proposed a separate `ResolveCitationHandler`
artifact to enrich stub refs in the background (full authors
list, abstract, citation count). Path B drops this entirely.
The chase populates the minimum needed for identity (one S2 call,
`title + DOI + year`) at stub-creation time; anything richer waits
for the actual PDF to land, at which point `precis_add` fills the
canonical metadata from CrossRef + embedded metadata more reliably
than S2 would.

If a real need for background enrichment surfaces later, register
a new artifact in `artifact_kinds(slug='resolve_citation:s2')`
(already seeded, dormant) and write a sibling worker — but defer
until measured.

## CLI / MCP surface

Minimum-change principle: piggyback on the seven-verb surface.

### `put(kind='finding', …)` — start a chase

```python
put(kind='finding',
    title='gate-bias 2.4 kV / 30 s on Si/SiO2',
    body='Device prep: 2.4 kV applied across the 50 nm gate oxide '
         'for 30 s on Si/SiO2 MOSCAPs with a Cu top contact '
         '(sputtered), N2 ambient, room temp, planar geometry.',
    scope={'electrode':'Cu','ambient':'N2','technique':'DC ramp',
           'geometry':'planar','substrate':'Si/SiO2'},
    cited_in='miller23a#42')
    → creates finding ref, one finding_body chunk (claim + setup
      as prose), initial `derived-from` link to miller23a chunk:42,
      meta.chain = [{ref_id: miller2020, chunk_id: <42>}],
      tag STATUS:tracing,
      returns pub_id (e.g. 'ab12c3')
```

`title` is the short claim title (one line, embeddable). `body` is
the claim text *plus* setup envelope as flowing prose (one chunk;
no separate `context=` argument). `scope` is the structured slice
of the same envelope in JSONB form, used for filtering and for
two-agents-collapse dedup. `cited_in` is the starting frontier of
the chase. The finding is **not yet usable** for `precis resolve`
substitution — it's `STATUS:tracing` until established.

### `search(kind='finding', q=...)` — find established facts

Default search filters to `STATUS:established`. TOON columns:

```
id     | title                                     | setup                            | primary_cite
-------+-------------------------------------------+----------------------------------+-------------
ab12c3 | gate-bias 2.4 kV / 30 s on Si/SiO2        | Cu/N2/DC ramp/planar             | fischer13
de45f6 | 0.1 mol/L NaCl in citrate buffer, pH 6.0  | wet etch/Ag/AgCl/22C             | lin98
```

`id` is the finding's `pub_id`. `setup` is a one-line render of
`meta.scope` (key=value joined with `/`). `primary_cite` is from
`meta.primary_cite_key`.

Status filter overrides: `status='tracing'`, `status='*'` (all
including in-flight), `status='dead_chain'` etc.

**Hybrid-search composition.** RRF (Reciprocal Rank Fusion) over
the lexical `chunks.tsv` and vector branches operates without
knowing the status tag; the status filter is **post-applied** to
the top-K results, with **1.5× over-fetch** at the RRF stage to
absorb the loss. Justification: filtering pre-RRF means joining on
`v_ref_tags_all` inside both retrieval branches, which the planner
handles poorly with HNSW. Post-filter is one extra JOIN on a
small K, cheap. `:established` will dominate the corpus once the
chase backfill stabilises, so the over-fetch waste is small.

### `get(kind='finding', id=<pub_id>)` — detail view (begat-style)

```
finding ab12c3   (cite_key: finding-ab12c3 — never goes in \cite{})
  title:   gate-bias 2.4 kV / 30 s on Si/SiO₂
  claim:   Device prep: 2.4 kV applied across the 50 nm gate oxide
           for 30 s.
  setup:   Cu top contact (sputtered), N₂ ambient, room temp,
           planar MOSCAP geometry.
  scope:   {electrode:'Cu', ambient:'N2', technique:'DC ramp',
            geometry:'planar', substrate:'Si/SiO2'}

  primary: fischer13 §3.2 — measured directly
           "We applied 2.4±0.05 kV across the 50 nm gate oxide for
            30 s in a flowing-N2 glove box, using a Cu top contact
            (sputtered)…"

  begat by:                              (oldest → newest)
    fischer13  — primary measurement
    miller23a  — cited fischer13 for this value

  found via: miller23a §2 (initial cite at finding put-time)

  notes (curator-flagged misattributions on the chain, if any):
    - miller23a says "Cu foil" — fischer13 says "Cu top contact
      (sputtered)". Material form mismatch (severity: material).
      ↑ written by hand via link(rel='misattributes'); not
        auto-generated by the chase (Path B).

  status: STATUS:established (chain snapshotted 2026-05-30 by chase)
```

`begat by` is the chain rendered oldest → newest (like the OT
genealogy). `notes` lists every `misattributes` link the chase
encountered along the chain. For `STATUS:tracing` findings, the
chain is shown with the current frontier marked and any stub refs
(`pdf_sha256 IS NULL`) flagged.

### NO `cite(kind='finding', ...)`

Findings are not externally citable. The verb is **not implemented
for `kind='finding'`** — calling it returns the standard "kind
does not support cite" error from the protocol surface. The
finding's role in text is the placeholder-substitution flow below.

### `precis resolve <text|file>` — substitute placeholders

```
precis resolve <input> [--format plain|markdown|latex]
                       [--strict]     # fail if any finding is in-flight
                       [--keep-id]    # keep <pub_id> alongside cite_key
                       [--ascii]      # ASCII-only in-flight marker
```

Reads input, finds `[<pub_id>]` tokens, looks up each finding:

- `STATUS:established` → substitute the primary paper's
  `cite_key`. Plain: `[fischer13]`. Markdown: `[fischer13]`.
  LaTeX: `\cite{fischer13}`.
- `STATUS:tracing` (and similar in-flight states) → leave the
  token, emit warning to stderr unless `--strict` (then exit 3).
  Rendering markers below.
- `STATUS:dead_chain` / `STATUS:cycle` / etc. → fail unless
  `--keep-id`, then emit `[ab12c3 (✗)]` with the failure reason.

`--keep-id` keeps both: plain `[fischer13 (ab12c3)]`; LaTeX
`\cite{fischer13}\,\mbox{\textsuperscript{[ab12c3]}}`.

### In-flight render markers

UTF-8 LaTeX (XeLaTeX / LuaLaTeX) is the assumed default. The
container images already use a UTF-8-capable engine; document
"`precis resolve --format latex` output requires xelatex or
lualatex" in the user-facing docs.

| Format | Established | In flight |
| --- | --- | --- |
| plain | `[fischer13]` | `[ab12c3 ⏳]` |
| markdown | `[fischer13]` | `[ab12c3 ⏳]` |
| LaTeX (default) | `\cite{fischer13}` | `\cite{ab12c3}\,\textsuperscript{⏳}` |
| LaTeX `--ascii` | `\cite{fischer13}` | `\cite{ab12c3}\,\textsuperscript{*}` (footnote `*` = in-flight finding) |

The `\cite{ab12c3}` reference will trip BibTeX if the bib file
doesn't carry an entry for the pub_id. `precis resolve` for LaTeX
also emits a stub `.bib` snippet for any in-flight `pub_id` so the
document compiles — the entry is replaced with the real one at the
next `precis resolve` pass after grounding.

### `cite(kind='paper')` — surfaces rooted/supported findings

When the paper detail is rendered (`get(kind='paper', id=...)`),
add a one-liner derived from the chain graph:

```
  findings: 8 rooted here, 4 supported
```

- **rooted** = `meta.primary_cite_key = <this paper's cite_key>`
  AND `STATUS:established`.
- **supported** = `<this paper's cite_key> ∈ meta.via_cite_keys`
  AND `STATUS:established`.

Click-through queries:
`search(kind='finding', primary=<pub_id>, status='established')`
and `search(kind='finding', via=<pub_id>, status='established')`.
Counts established-only by intent — in-flight findings are noise.

### Worker CLI

`precis worker` gains one `--only` choice: `chase`. Runs by default
alongside existing handlers. Following the sibling-worker pattern
of `--only segments` (ADR 0018), the chase worker is registered
directly in `precis/cli/worker.py` and does not flow through the
`artifact_kinds` registry or `precis worker --status` aggregation.
Operator-visible progress comes from `precis stats --findings`
(counts by `STATUS:tracing|established|dead_chain|cycle|multi_candidate`).

### Stub visibility

`precis stats` (per `storage-v2.md:651`) gains a `--stubs`
section that runs `SELECT count(*) FROM refs WHERE pdf_sha256
IS NULL AND kind = 'paper' AND deleted_at IS NULL` plus a
grouped breakdown by `meta.scope` if present. The operator sees
the fetch backlog without a tag scan.
`request_doi.md` (the plaintext-file hack at `paper.py:336-355`)
is **deprecated, not deleted** in this release — the empty-search
DOI suggestion is updated to point at `put(kind='finding', ...)`
or a future `precis request-paper <doi>`. Remove the plaintext
path in the *next* release.

## Paper deletion (soft only)

`delete(kind='paper', id=...)` is **soft-delete only**: sets
`refs.deleted_at = now()`. The schema already supports it
(`0001_initial.sql:322`, `refs_alive_idx WHERE deleted_at IS NULL`).
Hard delete is not exposed from the CLI / MCP surface — recovery
of a hard-deleted paper that's referenced by an established
finding's chain is impossible, and the surface should not make
that mistake easy.

Behaviour:
- Soft-deleted papers are excluded from `search` (already
  partial-indexed for it).
- The chase treats `deleted_at IS NOT NULL` as a dead-end: existing
  links are preserved; in-flight findings hitting a deleted target
  get `STATUS:dead_chain`. Established findings whose primary was
  soft-deleted retain their `meta.primary_cite_key` (they're
  historical record) and pick up `STATUS:primary_deleted` for
  operator visibility.
- True wipe is a DB-admin operation
  (`DELETE FROM refs WHERE deleted_at < now() - interval '90 days'`),
  not a verb. The `90 days` window means a misclick is recoverable.

Implication for stubs: soft-delete works on stubs too. Deleting a
stub orphans any in-flight chase whose chain pointed at it; the
chase pass writes `STATUS:dead_chain` on next claim. No special
guard required.

## Skill guidance (`finding-help` skill, MUST search before create)

Distilled from `src/precis/data/skills/precis-memory-help.md`
style. Ships at `src/precis/data/skills/finding-help.md`.

```markdown
## When to create a finding

A finding is a quantitative or empirical claim whose **setup
context** matters to anyone re-using it. Create one when you find
yourself writing *"X = 2.4 kV"*, *"the experiment used 0.1 mol/L
NaCl"*, *"only 12% of patients responded"* — anything where the
next reader will sooner or later ask "says who, under what
conditions, and how was it measured?"

### Before creating, ALWAYS search

    search(kind='finding', q='2.4 kV gate dielectric 30 s')

Read the setup column of every hit. If one matches your setup
(same instrument / electrode / ambient / technique), **reuse its
pub_id** — append your context as a linked memory rather than
spawning a parallel chase. Alternate setups (Cu vs Ag electrode,
N2 vs Ar ambient) need different findings, even when the bare
number is identical.

### DO NOT create findings for

- Opinions or qualitative impressions ("the figure is striking",
  "the result is robust").
- Definitions or terminology ("we call this the gate-bias regime").
- Claims without a measurable quantity ("the device worked well").
- Speculation, hypothesis, or proposed future work.
- Claims you are stating *for the first time* — those are
  findings of the document you're writing. Make them citable by
  publishing them, not by recording them.

### After creating

The chase runs asynchronously. Use the returned `pub_id` as a
placeholder in your text — `[ab12c3]`. When you're ready to
finalise the document, run:

    precis resolve <document> --format latex --strict

The placeholder will be replaced with `\cite{<primary>}` once the
chase establishes the finding. `--strict` makes the command fail
if anything is still in flight — useful for CI gates on a
manuscript.
```

## Schema / API / Ingest / Performance thresholds

Against `docs/conventions/thresholds.md`:

- **Schema**: adds new tables (`ref_artifacts`, `artifact_kinds`),
  a new kind row, two new chunk_kind rows, two new relations, one
  new actor row. No ALTER on existing tables. No column drops or
  renames. No dim changes. ✅
- **API**: no removed flags. New `put(kind='finding', ...)` keys
  (`title`/`body`/`scope`/`cited_in` — no `context=`, folded
  into `body`). New `precis resolve` subcommand. Existing
  seven-verb surface unchanged. ✅
- **Ingest**: `pdf_sha256` / `cite_key` / `pub_id` rules unchanged.
  Stub creation uses the same `make_pub_id` / `make_cite_key`
  paths as fresh ingest. ✅
- **Performance**: chase worker makes 0–1 S2 calls per hop, **zero
  LLM calls** (Path B drops the synthesis-pass rewrite and the
  mis-citation comparison). No new global model load. ✅
- **Cross-package**: no new top-level dep. S2 is already vendored
  (`semanticscholar` via `precis.ingest.citations`). No new LLM
  client call from the chase path. ✅

No threshold trips. Proceeding without checkpoint.

## Open questions

The following are settled (listed for the record):

- **Finding-as-ref vs. finding-as-chunk** — ref (own `pub_id`,
  own tags, own verification, can carry meta.scope).
- **Citation source S2 vs Marker** — S2 first, Marker fallback.
- **Stub identifier minimum** — at least one of {DOI, arXiv, S2 id};
  trigram fuzzy-match fallback before minting; unresolvable hops
  abort the hop rather than mint a blind stub.
- **Max hop cap** — none. Cycle protection handles pathological
  chains; long-but-progressing chains are fine.
- **Title canonicalisation at synthesis** — allowed (LLM
  high-model with RAG); old title was embedded once, new title
  re-embeds via card re-emit.
- **Finding → finding chains** — disallowed in this design. A
  finding's `derived-from` chain terminates at a paper, never at
  another finding. Combining findings into logical inferences is a
  separate feature for a future skill.
- **Mis-citation as a relation, not a separate table** — uses the
  new `misattributes` relation, no new table.

Genuinely open:

1. **Verification gate on `STATUS:established`.** Two positions:

   - *Lenient* — chase always establishes; verification is a
     separate optional badge (existing `human_verified_at`).
   - *Strict* — `STATUS:established` requires every ref on the
     chain to have `human_verified_at IS NOT NULL`.

   Recommendation: lenient. Verification is a separate axis (the
   `human_verified_at` flag already exists and is unused by any
   gate; adopting it as a chase prerequisite would push the entire
   `:established` corpus to zero on day one). A future
   `STATUS:verified` tag, set by `precis verify <finding>`, can
   layer on top.

2. **Multi-candidate disambiguation UI.** When the chase pauses
   at a chunk citing multiple references, the candidates are all
   linked with `meta={"candidate":true}` and the finding tags
   `STATUS:multi_candidate`. How does the user pick? Two options:

   - `edit(kind='finding', id=..., pick_candidate='miller23a')` —
     promotes one link to non-candidate, deletes the others.
   - A future `precis disambiguate` interactive command.

   Recommendation: ship `edit(..., pick_candidate=...)` first; add
   the interactive command if a real workflow surfaces.

3. **Re-running a chase after upstream changes.** A retracted
   target ref should ideally re-grade the finding's `:established`
   status. Today: manual re-trigger only (`precis chase --refresh
   <pub_id>`). Retraction propagation is queued for "after links
   are populated" (`storage-v2.md:614-617`); chase chains become an
   input to that work.

## LLM hooks (`claude -p`, default-off)

C5 ships **three optional LLM-driven enhancement points** that
default off (deterministic chase only) and turn on per-worker via
`PRECIS_CHASE_LLM=1` or `precis worker --only chase --with-llm`.
The helper is the project-wide
[`precis.utils.claude_p`](../../src/precis/utils/claude_p.py)
(shipped this branch) — subprocess-based, mock-friendly,
cost-capped, JSON-block-parsing wrapper around `claude -p`.

| Hook | Default behaviour | With `--with-llm` |
| --- | --- | --- |
| `_disambiguate_candidates` | tag `STATUS:multi_candidate`; wait for user | LLM reads the chunk + candidate bib entries, picks the most plausible target (returns `pick_index` or `null`) |
| `_locate_chunk_in_target` | top-1 lexical+ANN; flag low confidence | LLM confirms ANN's pick or proposes an alternate ord; lets the chase skip the wrong-chunk failure mode |
| `_verify_support_with_caveats` | no-op | LLM reads target chunk + claim + scope; returns `{supports, support_reason, caveats[], cited_others[], terminal}`. Caveats land on `meta.chain[k].caveats`; `cited_others` surfaces as inline cites the chase can follow (but does **not** auto-spawn sibling findings — see below) |

**Caveats compound up to the finding.** Each hop's verification
JSON is stored in `meta.chain[k].verification`; the finding's
top-level `meta.caveats` aggregates non-empty caveats across hops
(union with provenance). Rendering in `get(kind='finding')` and
in `precis resolve` surfaces them prominently:

```
finding ab12c3
  ...
  primary: fischer13
  caveats:
    - only for 50 nm SiO2 thickness (from fischer13)
    - room temp only (from miller23a §3)
  ...
```

**Caveats that reference further cites do NOT auto-spawn sibling
findings.** Path B-ii sticks: the caveat is recorded with its
`cited_others` token, and the user can spawn a sibling finding by
hand (`put(kind='finding', body='...thinner films', scope=...,
cited_in='lin98')`) to chase that specific qualification.
Rationale: auto-branching is exponential and noisy; the user knows
which qualifications matter for their document.

**Prompts live alongside the worker** as module-level constants
(`precis/workers/chase.py::_PROMPT_VERIFY`, etc.) so they version
with the code rather than as separate template files. Each prompt
ends with a JSON-shape hint; `_parse_last_json_block` in the
helper grabs the rightmost balanced block from stdout.

## Future: `claude -p` at ingest time (queued, NOT in this branch)

The same `claude_p.call_claude_p` helper is the obvious tool for
LLM-driven enhancements at ingest time. Three candidates, in
priority order:

1. **Structured fact extraction (`paper_facts` table)** — path-3
   from ADR 0018 (the "tables curveball" discussion, task #63).
   Per body chunk, extract `(value, unit, claim, conditions)` rows
   that complement the existing `chunks.numerics` lexical index.
   Feeds findings (gives the chase a strong "is this the primary
   measurement?" hint) and the agent's structured-query surface.
   **Queue Q1**.
2. **LLM-driven abstract / TL;DR per paper** — incrementally
   better search-card text than RAKE keywords. Not blocking.
   **Queue Q2**.
3. **Setup-context extraction from methods sections** — enables
   scope-filtered search without the agent supplying `scope=` at
   finding-create time. **Queue Q3**.

None ship in this branch. Defer Q1 until C5 lands and we observe
where the deterministic chase actually struggles; if the failure
mode is "is this chunk the primary measurement?" then Q1 helps
directly, and re-uses the same helper.

## Test plan

1. **Unit — `run_finding_chase_pass` algorithm.**
   - Finding with `cited_in` pointing at an in-corpus chunk that
     has no inline cite → terminal on first pass, `STATUS:established`,
     chain-snapshot pass populates `meta.primary_cite_key` /
     `meta.via_cite_keys`, card_combined re-emitted.
   - Finding pointing at a chunk with a `[12]` inline cite
     resolving to a stub → does-nothing pass, finding stays
     `STATUS:tracing`, `meta.chain` unchanged.
   - After stub gets chunks (fixture: directly INSERT chunks),
     re-run → advances (terminal or one more hop) and appends
     to `meta.chain`.
   - Cycle: chain that revisits a ref → `STATUS:cycle`, chain
     preserved as-is.
   - Multi-candidate: chunk citing `[12,13]` → both candidates
     attached as `derived-from` links with `meta.candidate=true`,
     `STATUS:multi_candidate`, no append to `meta.chain`.
   - Dead chain: target ref `deleted_at IS NOT NULL` →
     `STATUS:dead_chain` with `meta.dead_reason='target_deleted'`.
   - Dead chain: no inline cite found → `STATUS:dead_chain` with
     `meta.dead_reason='no_inline_cite'`.
   - Dead chain: candidate has no external id → `STATUS:dead_chain`
     with `meta.dead_reason='no_external_id'`; no stub minted.
2. **Unit — chain-snapshot pass card re-emit.**
   - Confirm DELETE+INSERT pattern; old `chunk_id` is gone, new
     `chunk_id` exists at same `ord=-1`, stale embedding row gone
     (cascaded), new chunk re-embeds on next embed worker pass.
   - No LLM call made (snapshot is pure text concat).
3. _(No mis-citation worker test — chase doesn't write
   `misattributes` under Path B. Manual curator-write integration
   test stays in the general link-CRUD suite.)_
4. _(No `ResolveCitationHandler` test — handler dropped per
   Path B.)_
5. **Integration — `precis worker --only chase` end-to-end.**
   Seed two papers (miller23a cites fischer13 in chunk:42), create
   finding, run worker, observe terminal state and chain.
6. **Integration — `precis_add` upgrades a stub.**
   Create a stub for known DOI (`pdf_sha256 IS NULL`); drop
   matching PDF; assert `ref_id` unchanged; assert `refs.pdf_sha256`
   is now the new hash; assert new `(pdf_sha256, content_hash)` rows
   in `ref_identifiers`; assert chase handler advances the dependent
   finding on the next pass.
6a. **Integration — `precis_add` registers an alias hash.**
   Take an existing ref with a `pdf_sha256` already set; drop a
   second PDF that resolves to the same DOI; assert
   `refs.pdf_sha256` UNCHANGED; assert new
   `ref_identifiers(id_kind='pdf_sha256')` row added; assert no
   block re-extract was triggered for the alias PDF (v1 contract).
7. **MCP surface — `put(kind='finding', ...)` round-trip.**
   Create, `get`, `search`. Assert `cite(kind='finding', ...)`
   raises "kind does not support cite".
8. **CLI — `precis resolve`.**
   - Plain: established findings substitute cite_key; in-flight
     emit warning.
   - LaTeX: `\cite{...}` substitution; in-flight renders with
     `⏳` (default) or `*` (`--ascii`).
   - `--strict`: in-flight findings cause non-zero exit.
9. **Regression — `cite(kind='paper')` includes rooted/supported
   counts** for established findings only.
10. **Regression — `request_doi.md` deprecation warning** fires
    on the empty-search DOI path; suggested follow-up is
    `put(kind='finding', ...)`.

## Implementation order

C-step naming convention matches `storage-v2.md` so commit
messages and the plan stay in sync.

- **C0** ✅ This design doc + ADR 0017 (with §4 marked superseded
  by ADR 0018). Path-B revision: 2026-05-31.
- **C1** ✅ Migration `0004_finding_and_queue_family.sql` — applied
  2026-05-30. `chunk_kinds.finding_context`, the two
  `artifact_kinds` seed rows (`chase_citation`,
  `resolve_citation:s2`), and the `relations.misattributes` pair
  are **dormant under Path B** (kept in place per forward-only
  migration rule; harmless).
- **C2** ✅ `precis.identity.make_finding_paper_id(body_text, scope,
  initial_cite_pub_id)` — deterministic; same skill input →
  same `pub_id`. 12 unit tests passing.
- **C3** `precis.handlers.finding` — `NumericRefHandler` subclass,
  `put(title, body, scope, cited_in)`, no `cite` support, card
  variants, search wiring with status post-filter,
  `_parse_cited_in` reference implementation. Models on
  `CitationHandler` (shipped) for shape. ~150 LoC.
- **C4** ~~`WorkerHandler` refactor~~ — **dropped per ADR 0018.**
  The chase worker is a sibling function (see C5).
- **C5** `precis.workers.chase.run_finding_chase_pass` — sibling
  worker modelled on `precis.workers.segment_toc.run_paper_segments_pass`.
  Includes: claim query (`STATUS:tracing` finder), frontier
  selection from `meta.chain`, inline-citation detection (staged
  regex → S2 → Marker references → multi-candidate),
  stub creation with the `set_by='chase'` audit marker, cycle
  protection via `meta.chain` membership check, dead-chain /
  multi-candidate tagging, chain-snapshot pass at termination
  (no LLM). Registered in `precis/cli/worker.py` under
  `--only chase`. ~200 LoC.
- **C6** ~~`ChaseCitationHandler`~~ — merged into C5.
- **C7** `precis_add` stub-upgrade + multi-hash alias path —
  reuse the existing `PaperToWrite.pdf_sha256_aliases` machinery
  in `src/precis/ingest/db_writer.py:108-114, 390-400` (the
  alias-as-ref_identifier-row insert is already idempotent
  `ON CONFLICT DO NOTHING`). On `probe_existing` hit: ADD the
  new `(pdf_sha256, content_hash)` rows via that path; if
  `existing.pdf_sha256 IS NULL`, ALSO `UPDATE refs SET
  pdf_sha256 = ... WHERE ref_id = ...` and extract chunks for
  the new PDF. (No tag work — column predicate flips itself.)
  See §"Stub upgrade" for the rule.
- **C8** `precis resolve` CLI subcommand + `finding-help.md`
  skill doc. UTF-8 + ASCII LaTeX render markers.
- **C9** Light deprecation pass — `paper.py` empty-search DOI
  hint suggests `put(kind='finding', ...)` (plus the existing
  citation/bibliography surfaces). `request_doi.md` retained
  with a deprecation banner. A "findings rooted/supported here"
  line on `get(kind='paper', view='bibliography')` is opt-in:
  fold only if the existing bibliography view feels too narrow
  once findings start landing.
- **C10** CHANGELOG entry + minor version bump. README updated
  with the `--only chase` worker flag and the `precis resolve`
  command.

Each step ships its own commit with tests. C1 ran
`precis migrate --dry-run` against the live DB (verified ✅).

## Risk

- **S2 outage.** Chase progress halts; affected findings stay
  `STATUS:tracing` indefinitely and re-attempt on next worker
  pass. No corruption; no per-pass failure rows to clean up.
- **Stub explosion.** A single chase could create several stubs
  over multiple hops. Mitigation: `precis stats --stubs` surfaces
  the backlog (column-predicate query, see §"Stub visibility");
  a future opt-in fetcher worker (Unpaywall + IA fallback) drains
  it. Stubs cost ~1 KB each.
- **Wrong chunk localisation.** Top-1 lexical-ANN within a target
  paper might pick a related-but-wrong chunk. Mitigation: keep the
  confidence score in the appended `meta.chain` entry
  (`{ref_id, chunk_id, confidence}`); render the chain with a
  visible flag when confidence < threshold; agents can re-anchor
  via `edit(kind='finding', id=..., anchor='<target>#<ord>')`.
- **Placeholder leak into published documents.** Authors might
  forget `precis resolve --strict` and ship `[ab12c3]` in a
  manuscript. Mitigation: skill doc emphasises `--strict` in CI;
  the in-flight render marker (⏳ or `*` footnote) is deliberately
  visible during proof-reading.
- **Per-pass cost of re-examining tracing findings stuck on
  stubs.** Bounded by (findings-in-flight × pass-interval); see
  §"Re-enqueue on chunk arrival". Add backoff only if measured.

## Definition of done

(Audited 2026-06-05 against `v8.1.0 — finding-chase` CHANGELOG entry;
gaps closed same day in a follow-up commit.)

- [x] `0004_finding_and_queue_family.sql` applied to live DB
      (`9d7e85f170f7` checksum). Vocabulary present and dormant
      where Path B doesn't consume it (`chunk_kinds.finding_context`,
      two `artifact_kinds` seed rows, `relations.misattributes` pair).
- [x] `make_finding_paper_id` shipped with 12 tests (C2).
- [x] `put(kind='finding', ...)` creates ref + one `finding_body`
      chunk (claim + setup as prose) + initial `derived-from`
      link in one transaction. (`handlers/finding.py` lines
      244–303 — single `with store.tx() as conn:` wraps every
      write; `UniqueViolation` on `pub_id` rolls back cleanly.)
- [x] `precis worker --only chase` advances findings by one hop
      per pass; `--once` mode is deterministic for the test suite.
      (`cli/worker.py` registers the `--only chase` choice;
      `workers/chase.py:run_finding_chase_pass` is the entry.)
- [x] Stubs are identified by `pdf_sha256 IS NULL` (no tag).
      `precis_add` on a matching PDF merges into the same
      `ref_id`, UPDATEs `refs.pdf_sha256` when it was NULL, and
      ADDs `(pdf_sha256, content_hash)` rows to `ref_identifiers`
      either way (multi-hash alias support). (`ingest/add.py:230`
      stub upgrade comment + `ingest/db_writer.py:476–516` alias path.)
- [x] `search(kind='finding', q=...)` returns hits filtered to
      `:established` by default, TOON shape
      `id | title | setup | primary_cite`. (`FindingHandler.search`
      override added 2026-06-05; `status=` shorthand desugars to
      a STATUS-tag filter unioned with the caller's `tags=`;
      empty-q path falls back to a recency-ordered list; rendering
      uses `render_agent_table` for the TOON shape. Closed-vocab
      `_CLOSED_VOCAB['STATUS']` extended to include the
      chase-workflow values so filter-time validation accepts
      them.)
- [x] `get(kind='finding', id=...)` renders the begat chain plus
      any user-curated misattribution links on the chain.
      (`handlers/finding.py:_render_one` surfaces outbound
      `misattributes` edges under a `misattributed via:` block
      alongside the begat chain. Added 2026-06-05; test
      `tests/test_finding.py::TestRoundTrip::test_get_renders_misattribution_links`.)
- [x] `cite(kind='finding', ...)` raises "kind does not support
      cite". (`handlers/finding.py:349–370` — `raise Unsupported`
      with the `precis resolve` next-hint.)
- [x] `precis resolve` substitutes established findings;
      `--strict` exits non-zero on in-flight; LaTeX output uses
      UTF-8 by default with ASCII fallback. (`cli/resolve.py:97`
      `--strict`, line 110 `--ascii`, line 188 `sys.exit(3)`.)
- [x] `precis stats --findings` summarises counts per `STATUS:`
      value; `precis stats --stubs` summarises stub backlog.
      (`cli/stats.py` added 2026-06-05. Default prints both
      sections; each flag isolates one. `precis stubs` remains
      the row-level lister; `precis stats --stubs` is the
      aggregate count for "how big is the backlog?" without
      dumping it. JSON / TOON / table output via the shared
      ``add_format_argument`` plumbing.)
- [x] `finding-help` skill installed under
      `src/precis/data/skills/precis-finding-help.md` (236
      lines, last-updated 2026-06-01).
- [x] Tests cover: terminal, stub-waiting (does-nothing pass),
      hop, cycle (revisit), dead-chain (no inline cite / no
      external id / target deleted), multi-candidate, card re-emit
      at chain termination.
      (`tests/workers/test_chase.py` added 2026-06-05 — 9 scenario
      tests against `run_finding_chase_pass` with
      `_load_s2_references` mocked. Plus a `test_finding.py`
      `TestSearch` class covering the new search override:
      default-established filter, status overrides, `status='*'`,
      TOON shape, recency fallback, BadInput on empty input.)
- [x] CHANGELOG entry + minor version bump
      (`v8.1.0 — finding-chase + OA fetcher cascade + event
      log (2026-06-01)`).

### Audit close-out (2026-06-05)

All gaps from the 2026-06-05 audit landed the same day:

* `FindingHandler.search` override + `_CLOSED_VOCAB['STATUS']`
  expansion to cover the chase-workflow values.
* `_render_one` surfaces `misattributes` outbound edges.
* `precis stats` subcommand with `--findings` / `--stubs` flags.
* `tests/workers/test_chase.py` — 9 scenario tests.

The genuinely open items now live under §"Open questions" (lenient
vs. strict verification gate, multi-candidate disambiguation UI,
re-running a chase after upstream retraction). Those are design
discussions, not DoD gaps.
