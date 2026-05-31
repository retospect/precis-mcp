# Finding chase — trace cited claims back to their primary source

**Status**: draft (pre-review)
**Owner**: `src/precis/workers/`, `src/precis/handlers/finding.py` (new),
new migration on top of `0001_initial.sql`
**Predecessors**:
- [`storage-v2.md`](./storage-v2.md) — establishes refs / chunks /
  links / derived queue
- [`extract-once.md`](./extract-once.md) — establishes the stub-vs-real
  merge mechanic (`ref_identifiers` collapse on re-ingest)
**Related ADRs**:
- [0006 — tri-identifier scheme](../decisions/0006-tri-identifier-scheme.md)
  (we ride `cite_key` as the LaTeX/BibTeX handle)
- [0007 — derived queue, no jobs table](../decisions/0007-derived-queue-no-block-jobs.md)
  (extended to ref-level in 0017)
- [0008 — drop slug, `cite_key` is canonical human form](../decisions/0008-drop-slug-identifier-normalisation.md)
- [0017 — derived-queue family + `artifact_kinds` registry](../decisions/0017-derived-queue-family.md)
  (the queue substrate this design depends on)

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

- The claim becomes a `finding` ref, embedded and searchable like
  any other ref.
- Each hop of the chase is a row in `links` with relation
  `derived-from`, terminating when we hit a primary report (a paper
  that *measured* the value rather than re-citing it).
- Missing cited papers are materialised as **stub refs**: real
  `refs` rows with identifiers (DOI / arXiv / S2 id) and
  `pdf_sha256 IS NULL` — the column predicate alone identifies a
  stub. `precis stats` lists them; no tag needed (the `STATUS:`
  slot on paper refs is reserved for provenance state).
- When a stub's PDF lands, the merge happens automatically (DOI
  hits the same `ref_id` via `ref_identifiers`;
  chunks / embeddings / summaries flow through the existing derived
  queue).
- A worker handler `chase_citation` claims findings whose chain is
  incomplete and pushes the chase one hop forward each pass.
- A second worker handler also flags **misattributions** (paper A
  says paper B reported X, paper B actually reported Y).

Crucially the worker plumbing is **generally applicable** beyond
findings — the chase, retraction checks, S2 stub-fill, and any
future ref-level artifact share one substrate. The substrate is
formalised in [ADR 0017](../decisions/0017-derived-queue-family.md);
this design is the first user of it.

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
| `refs.title` | short claim title — *"gate-bias 2.4 kV / 30 s on Si/SiO₂"* | author at `put`; canonicalised at synthesis (LLM, high-model, RAG) |
| `chunks` ord=0, kind=`finding_body` | the claim text incl. measurement value | author at `put` |
| `chunks` ord=1, kind=`finding_context` | the setup envelope (instrument, electrode, ambient, technique, geometry, scope) | author at `put` |
| `card_combined` (ord=-1) | title + body + context + primary cite_key | computed; re-emitted at synthesis |
| `refs.meta->'scope'` JSONB | structured slice of the setup for filtering (`{"electrode": "Cu", "ambient": "N2", ...}`) | author at `put` *and* LLM-extracted at synthesis |
| `refs.meta->'primary_cite_key'` | cite_key of the terminal ref | synthesis pass |
| `refs.meta->'via_cite_keys'` | ordered cite_keys of intermediate refs (the begat chain) | synthesis pass |
| `links --derived-from-->` chain | direct + indirect sources | written by the chase handler one hop at a time |
| `STATUS:tracing` tag | in flight | initial state |
| `STATUS:established` tag | terminal — primary source identified | replaces `tracing` at synthesis |

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
                           │       body='...', context='Cu top contact, N2 ambient',
                           │       scope={'electrode':'Cu','ambient':'N2'},
                           │       cited_in='miller2020#42')
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  finding ref  (pub_id=ab12c3)                                │
 │  • chunk_kind='finding_body'    — the claim                  │
 │  • chunk_kind='finding_context' — the setup                  │
 │  • meta.scope = {electrode:'Cu', ambient:'N2'}               │
 │  • tag STATUS:tracing                                        │
 │  • link  --derived-from-->  miller2020 chunk:42   (frontier) │
 └─────────────────────────┬────────────────────────────────────┘
                           │  chase_citation worker (pass 1)
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  reads miller2020 chunk:42; detects "[12]" inline citation;  │
 │  S2 references list says [12] = fischer2013 (DOI x).         │
 │  fischer2013 not in corpus → create stub ref                 │
 │       (ref_identifiers: doi=x, s2=y; cite_key=fischer13;     │
 │        pub_id=k4j7m2)                                        │
 │  (stub-ness implied by pdf_sha256 IS NULL — no tag written)  │
 │  link finding --derived-from--> fischer2013   (ref-level)    │
 │  finding stays STATUS:tracing                                │
 │  payload = {state:'hopped', from:..., to:..., visited:[...]} │
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
                           │  chase_citation worker (pass 2)
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  re-claims the finding (target ref now has chunks).          │
 │  Locates the chunk in fischer2013 that states the value.     │
 │  Compares miller2020's wording vs fischer2013's: detects     │
 │    "Cu foil" (miller) vs "Cu top contact, sputtered"         │
 │    (fischer). Writes misattributes link with diff.           │
 │  Frontier chunk has no further [N] cite → SYNTHESIS:         │
 │    • link finding --derived-from--> fischer2013 chunk:N      │
 │    • meta.primary_cite_key = 'fischer13'                     │
 │    • meta.via_cite_keys = ['miller20']                       │
 │    • LLM (high-model, RAG) canonicalises title               │
 │    • re-emit card_combined (DELETE+INSERT at ord=-1)         │
 │    • tag finding STATUS:established; remove STATUS:tracing.  │
 └──────────────────────────────────────────────────────────────┘
```

## Derived-queue family (the substrate)

ADR 0007 established the chunk-level derived queue
(`chunk_embeddings`, `chunk_summaries`).
[ADR 0017](../decisions/0017-derived-queue-family.md) generalises
that into a family: same shape across chunk / ref / link / pdf
targets, typed outputs for indexable values, untyped JSONB outputs
for handler results, one `artifact_kinds` registry, one parameterised
`WorkerHandler` base class.

This design is the first user. Two new artifacts register here:

| Artifact slug | Target | Storage | Output table | What it does |
| --- | --- | --- | --- | --- |
| `chase_citation` | ref | untyped | `ref_artifacts` | one chase hop per pass |
| `resolve_citation:s2` | ref | untyped | `ref_artifacts` | fill stub metadata via S2 |

The `ref_artifacts` table is created by this migration; the
`artifact_kinds` registry and the `WorkerHandler` refactor land in
the same migration per ADR 0017. Read ADR 0017 for the shape; this
design only specifies the *handlers*.

**Retraction-checking is NOT a queue artifact in this design.** The
[provenance kind](../provenance-kind-plan.md) — **shipped Phases
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

### New `chunk_kinds`

```sql
INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('finding_body',    FALSE, 'Finding claim text (the measured value)'),
    ('finding_context', FALSE, 'Finding setup envelope (instrument, electrode, ambient, ...)');
```

Standard card variants (`card_combined`, `card_title`, `card_meta`)
apply; `card_authors`, `card_abstract`, `card_keywords` are skipped
for findings (no authors / abstract / RAKE-able body of academic
length).

### New `relations` (mis-citation flagging)

```sql
INSERT INTO relations (slug, is_symmetric, inverse_slug, description) VALUES
    ('misattributes',    FALSE, 'misattributed-by',
        'Source chunk misrepresents what the target chunk actually says'),
    ('misattributed-by', FALSE, 'misattributes',
        'Source chunk is misrepresented by the target chunk');
```

Used outside findings too: any chunk-to-chunk misrepresentation
detected by any tool can land here, surfaced via
`search(relation='misattributes')`. The chase worker is its first
producer.

### New `actor` (audit trail)

```sql
INSERT INTO actors (slug, title, description) VALUES
    ('chase', 'Citation-chase worker',
     'Automated worker that traces findings to primary sources and '
     'flags misattributions encountered along the chain.');
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

### `ref_artifacts` table

Per ADR 0017 — shape is shared with future `link_artifacts` /
`pdf_artifacts` / `chunk_artifacts`. Full SQL in ADR 0017 §1.

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

### `ChaseCitationHandler` — one hop per claim

Subclasses the parameterised `WorkerHandler` from ADR 0017. Claims
findings (`refs.kind='finding'`) lacking a terminal
`ref_artifacts(artifact='chase_citation', payload->>'state' = 'anchored')`
row. For each finding `F`:

**1. Pick the current frontier.** Most recent outgoing
`derived-from` link from `F` (or the chunk reached via the chain).
If no such link exists, the frontier is the initial `cited_in`
passed at create-time.

**2. Read the cited chunk.** If the frontier is chunk-scoped,
that's the chunk. If ref-scoped, locate the chunk in the target ref
that the finding text refers to (lexical + ANN search constrained
to `ref_id = frontier`, top-1 with confidence threshold).

- If the target ref has no chunks yet (stub), emit
  `payload={"state":"waiting_pdf", "blocker_ref_id": <target>}`
  and return. The chase resumes when the PDF lands (see
  *Re-enqueue on chunk arrival* below).
- If the target ref is soft-deleted (`refs.deleted_at IS NOT NULL`),
  emit `payload={"state":"unresolvable","reason":"target_deleted"}`
  and tag finding `STATUS:dead_chain`. Chain preserved as-is.

**3. Decide: terminal or hop?**

- **Terminal** if the frontier chunk has no outgoing `cites` link
  AND no inline bibliographic reference pattern (see §"Inline
  citation detection" below). Run the **synthesis pass**
  (§"Synthesis pass" below); chain is grounded.
- **Hop** otherwise. Determine the next reference (§"Inline
  citation detection"); resolve it to a `ref_id` (existing ref via
  `ref_identifiers`, or create a stub — see §"Stub creation");
  add link `F --derived-from--> next_ref`; write
  `payload={"state":"hopped","from":...,"to":...,"visited":[...]}`.

**4. Mis-citation check** (every hop, not just terminal). Compare
the frontier chunk's wording about the target with the target
chunk's actual content. If a substantive mismatch (LLM call against
both texts, threshold-gated), write a `misattributes` link with
`meta={"src_says": "...", "dst_actual": "...", "severity": "..."}`.
This is independent of whether the hop continues or terminates.

**5. Cycle protection.** Maintain `payload.visited: [ref_id, ...]`
across passes (read → check membership → append → write).
Revisit → emit
`payload={"state":"unresolvable","reason":"cycle","visited":[...]}`
and tag finding `STATUS:cycle`.

The handler is idempotent. Re-running on a finding whose chain is
`anchored` short-circuits at step 3a's first check.

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
   "score":0.x}`. Chase pauses at the finding with
   `STATUS:multi_candidate` until disambiguated (user → `edit(...,
   pick_candidate='miller20')`, or a later LLM-disambiguation
   handler).
5. **No hits.** Emit
   `payload={"state":"unresolvable","reason":"no_inline_cite"}`.
   Chain preserved as far as it got; operator can resolve manually.

### Stub creation

The chase creates a stub ref when an inline citation resolves to a
paper not in the local corpus. Minimum identifier requirement: at
least one of `{DOI, arXiv id, S2 id}` plus a title. The stub:

- Gets a minted `pub_id` (per ADR 0006 / ADR 0008 — same path as
  any real ref) and a `cite_key`.
- Carries whatever external IDs S2 returned in `ref_identifiers`.
- Has `pdf_sha256 IS NULL` — **the column predicate IS the
  stub-state signal** (no tag).
- Gets an `ok` row in `ref_artifacts(artifact='resolve_citation:s2')`
  so the resolve handler doesn't re-run on it (the chase already
  populated the minimum).

With **only** `(authors, year, title)` from a Marker-parsed
reference list (no external IDs), the chase fuzzy-matches against
`ref_identifiers (id_kind='cite_key')` via the trigram index first
— if it hits an existing ref, reuse. Else: emit
`payload={"state":"unresolvable","reason":"no_external_id"}` and
do *not* mint a blind stub. The half-known reference would never
auto-merge with a real PDF and would clutter the corpus.

### Synthesis pass

When step 3 reaches a terminal frontier, the chase handler runs the
synthesis pass *before* flipping the status tag:

1. **`meta.primary_cite_key`** = cite_key of the terminal ref.
2. **`meta.via_cite_keys`** = ordered cite_keys of every ref on
   the chain between `F` and the terminal, excluding endpoints
   (the begat chain).
3. **LLM title canonicalisation** (high-model, RAG over the primary
   chunk). If the canonicalised title differs from
   `refs.title`, update it. Old title was embedded once; new title
   gets re-embedded automatically via the chunk re-emit (below).
4. **LLM `meta.scope` enrichment.** Same model call extracts any
   setup terms from the primary chunk not already in
   `meta.scope`. Merge into the JSONB.
5. **Re-emit `card_combined`.** `chunks (ref_id, ord)` is UNIQUE;
   we use `DELETE FROM chunks WHERE ref_id=$F AND ord=-1` followed
   by `INSERT INTO chunks ...`. `ON DELETE CASCADE` clears the
   stale `chunk_embeddings` row; new `chunk_id` has no embedding
   row → derived queue re-embeds on next pass.

> **Append-only contract on `chunks`:** chunks are append-only
> except for `ord < 0` card variants written by registered
> synthesis passes. The card re-emit is the *only* path that
> mutates a chunk's text. Document this in `AGENTS.md` when this
> ships.

Synthesis is idempotent: re-running computes the same `primary` /
`via` from the chain. The card_combined re-emit no-ops if the
text didn't change (delete + re-insert with same text — `chunk_id`
churns but is invisible to consumers).

### Re-enqueue on chunk arrival

Once a stub gets its PDF and chunks land, findings that were
waiting on it should resume. **Polling, no triggers.**

The chase handler's claim query treats `state='waiting_pdf'` rows
as still claimable:

```sql
... LEFT JOIN ref_artifacts o
       ON o.ref_id = r.ref_id AND o.artifact = 'chase_citation'
 WHERE (o.ref_id IS NULL                          -- never run
    OR  o.payload->>'state' = 'waiting_pdf')      -- waiting on stub
   AND r.kind = 'finding'
   AND r.deleted_at IS NULL
```

When a stub gets chunks, the next pass's chase re-runs on the
dependent finding and advances. Polling cost is bounded by
(findings in flight) × (handler interval), measured in hundreds
not millions.

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

### `ResolveCitationHandler` — enrich stubs in the background

Separate, opt-in handler. The chase populates the *minimum* needed
for identity (one S2 call, set title + DOI + year). The resolve
handler enriches a stub more thoroughly: full authors list,
abstract, citation count, S2 references list, etc.

Claim shape:
```sql
... FROM refs r LEFT JOIN ref_artifacts o
    ON o.ref_id = r.ref_id AND o.artifact = 'resolve_citation:s2'
   WHERE o.ref_id IS NULL
     AND r.pdf_sha256 IS NULL
     AND r.deleted_at IS NULL
```

Stops itself once the PDF lands (at which point `precis_add` fills
the same fields from CrossRef + embedded metadata, more reliably
than S2).

## CLI / MCP surface

Minimum-change principle: piggyback on the seven-verb surface.

### `put(kind='finding', …)` — start a chase

```python
put(kind='finding',
    title='gate-bias 2.4 kV / 30 s on Si/SiO2',
    body='Device prep: 2.4 kV applied across the 50 nm gate oxide '
         'for 30 s.',
    context='Cu top contact (sputtered), N2 ambient, room temp, '
            'planar MOSCAP geometry.',
    scope={'electrode':'Cu','ambient':'N2','technique':'DC ramp',
           'geometry':'planar','substrate':'Si/SiO2'},
    cited_in='miller23a#42')
    → creates finding ref, body + context chunks, initial
      `derived-from` link to miller23a chunk:42, tag STATUS:tracing,
      returns pub_id (e.g. 'ab12c3')
```

`title` is the short claim title (one line, embeddable). `body` is
the claim text. `context` is the setup envelope (also a chunk).
`scope` is the structured slice (JSONB, used for filtering).
`cited_in` is the starting frontier of the chase. The finding is
**not yet usable** for `precis resolve` substitution — it's
`STATUS:tracing` until established.

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

  notes (misattribution flags on the chain):
    - miller23a says "Cu foil" — fischer13 says "Cu top contact
      (sputtered)". Material form mismatch (severity: material).

  status: STATUS:established (synthesised 2026-05-30 by chase)
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

`precis worker` gains two `--only` choices: `chase` and `resolve`.
Both run by default. `precis worker --status` iterates registered
artifacts (`artifact_kinds` per ADR 0017) and prints `(total |
ok | failed | pending)` per artifact — the two new handlers
appear alongside existing `embed:bge-m3` / `summarize:rake-lemma`.

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
  (`title`/`body`/`context`/`scope`/`cited_in`). New
  `precis resolve` subcommand. Existing seven-verb surface
  unchanged. ✅
- **Ingest**: `pdf_sha256` / `cite_key` / `pub_id` rules unchanged.
  Stub creation uses the same `make_pub_id` / `make_cite_key`
  paths as fresh ingest. ✅
- **Performance**: chase handler makes 0–1 S2 calls per hop, 0–1
  LLM calls (one per misattribution check, one per synthesis pass).
  No new global model load. ✅
- **Cross-package**: no new top-level dep. S2 is already vendored
  (`semanticscholar` via `precis.ingest.citations`). LLM call goes
  through whichever client is already configured. ✅

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

## Test plan

1. **Unit — `ChaseCitationHandler` algorithm.**
   - Finding with `cited_in` pointing at an in-corpus chunk that
     has no inline cite → terminal on first pass, `STATUS:established`,
     synthesis pass populates `meta.primary_cite_key` /
     `meta.via_cite_keys`, card_combined re-emitted.
   - Finding pointing at a chunk with a `[12]` inline cite
     resolving to a stub → `state=waiting_pdf`, finding stays
     `STATUS:tracing`.
   - After stub gets chunks (fixture: directly INSERT chunks),
     re-run → advances (terminal or one more hop).
   - Cycle: chain that revisits a ref → `STATUS:cycle`, payload
     `reason=cycle`.
   - Multi-candidate: chunk citing `[12,13]` → both candidates
     attached, `STATUS:multi_candidate`.
   - Dead chain: target ref `deleted_at IS NOT NULL` →
     `STATUS:dead_chain`.
2. **Unit — mis-citation detection.**
   - LLM mock returns "material mismatch" diff → `misattributes`
     link written with `meta.diff` populated.
   - LLM returns "no mismatch" → no link written.
3. **Unit — `ResolveCitationHandler`.**
   - Stub with only DOI → S2 mock returns full metadata → fields
     fill on `refs`; `pdf_sha256` still NULL (stub-state predicate
     holds) until PDF arrives.
   - S2 miss → `status='failed'`, `last_error` captures cause.
4. **Unit — synthesis pass card re-emit.**
   - Confirm DELETE+INSERT pattern; old `chunk_id` is gone, new
     `chunk_id` exists at same `ord=-1`, stale embedding row gone
     (cascaded), new chunk re-embeds on next worker pass.
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

- **C0** This design doc + ADR 0017 land. ✅ in flight
- **C1** Migration `0004_finding_and_queue_family.sql`:
  - `artifact_kinds` registry + initial inserts
  - `ref_artifacts` table
  - `kinds (finding, …)`
  - `chunk_kinds (finding_body, finding_context)`
  - `relations (misattributes, misattributed-by)`
  - `actors (chase)`
  - Update PUML diagram (`docs/design/schema-v2.puml`)
- **C2** `precis.identity` extension —
  `make_finding_paper_id(body_text, scope, initial_cite_pub_id)`
  (deterministic; same skill input → same `pub_id`).
- **C3** `precis.handlers.finding` — `NumericRefHandler` subclass,
  `put(... title, body, context, scope, cited_in)`, no `cite`
  support, card variants, search wiring with status post-filter.
  `_parse_cited_in` reference implementation.
- **C4** `WorkerHandler` refactor per ADR 0017 — descriptor-based
  base class; existing `EmbedHandler` / `RakeLemmaHandler`
  unchanged in behaviour; runner reads `artifact_kinds`.
- **C5** `ResolveCitationHandler` — S2-backed, registered in
  `precis.workers.runner`.
- **C6** `ChaseCitationHandler` — chase logic, inline-citation
  detection, mis-citation flagging, synthesis pass with LLM
  canonicalisation. Largest step; gated on unit-test coverage of
  every branch above.
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
- **C8** `precis resolve` CLI subcommand + skill doc
  (`finding-help.md`).
- **C9** Deprecation pass — `paper.py` empty-search DOI hint
  updated; `request_doi.md` retained with a deprecation banner.
  `cite(kind='paper')` rooted/supported counts.
- **C10** CHANGELOG entry + minor version bump. README updated
  with the `--only chase` / `--only resolve` worker flags.

Each step ships its own commit with tests. C1 runs
`precis migrate --dry-run` against a fresh DB.

## Risk

- **Mis-citation false positives.** LLM-based comparison of
  citing vs. cited wording can flag harmless paraphrases.
  Mitigation: severity field on `misattributes.meta`; render only
  `severity in ('material','factual')` by default; surface
  `paraphrase` severity in `precis stats --misattributions`.
- **S2 outage.** Chase progress halts but no data is corrupted —
  failed rows accumulate, operator deletes them after S2 recovers.
  ADR 0007's "no automatic retry" rule applies.
- **Stub explosion.** A single chase could create dozens of stubs
  over multiple hops. Mitigation: `precis stats --stubs` surfaces
  the backlog (column-predicate query, see §"Stub visibility");
  a future opt-in fetcher worker (Unpaywall + IA fallback) drains
  it. Stubs cost ~1 KB each.
- **Wrong chunk localisation.** Top-1 lexical-ANN within a paper
  might pick a related-but-wrong chunk. Mitigation: confidence
  threshold + `low_confidence` payload flag; agents reading the
  chain see the flag and can re-anchor manually
  (`edit(kind='finding', id=..., anchor='<target_pub_id>#<ord>')`).
- **LLM cost on synthesis.** One LLM call per established finding
  (canonicalisation + scope enrichment). At thousands of findings
  per month this is real. Mitigation: gate behind a config flag
  (`PRECIS_FINDING_LLM_REWRITE=1` to enable; off by default until
  measured).
- **Placeholder leak into published documents.** Authors might
  forget `precis resolve --strict` and ship `[ab12c3]` in a
  manuscript. Mitigation: skill doc emphasises `--strict` in CI;
  the in-flight render marker (⏳ or `*` footnote) is deliberately
  visible during proof-reading.

## Definition of done

- [ ] `0004_finding_and_queue_family.sql` applies cleanly to a
      fresh DB; PUML diagram updated.
- [ ] `artifact_kinds` registry seeded with `embed:bge-m3`,
      `summarize:rake-lemma`, `chase_citation`,
      `resolve_citation:s2`. (Retraction work is owned by the
      shipped provenance kind, not a queue artifact.)
- [ ] `WorkerHandler` refactor preserves behaviour for
      `EmbedHandler` / `RakeLemmaHandler` (existing tests
      unchanged).
- [ ] `put(kind='finding', ...)` creates ref + body chunk +
      context chunk + initial `derived-from` link in one
      transaction.
- [ ] `precis worker --only chase` advances findings by one hop
      per pass; `--once` mode is deterministic for the test suite.
- [ ] Stubs are identified by `pdf_sha256 IS NULL` (no tag).
      `precis_add` on a matching PDF merges into the same
      `ref_id`, UPDATEs `refs.pdf_sha256` when it was NULL, and
      ADDs `(pdf_sha256, content_hash)` rows to `ref_identifiers`
      either way (multi-hash alias support).
- [ ] `search(kind='finding', q=...)` returns hits filtered to
      `:established` by default, TOON shape `id | title | setup
      | primary_cite`.
- [ ] `get(kind='finding', id=...)` renders the begat chain plus
      any misattribution notes.
- [ ] `cite(kind='finding', ...)` raises "kind does not support
      cite".
- [ ] `cite(kind='paper', ...)` includes a `findings: N rooted, M
      supported` line.
- [ ] `precis resolve` substitutes established findings;
      `--strict` exits non-zero on in-flight; LaTeX output uses
      UTF-8 by default with ASCII fallback.
- [ ] `precis worker --status` shows the two new artifacts
      (`chase_citation`, `resolve_citation:s2`).
- [ ] Mis-citation links written by the chase carry severity +
      diff in `meta`.
- [ ] `finding-help` skill installed under
      `src/precis/data/skills/`.
- [ ] Tests cover terminal, waiting, hop, cycle, dead-chain,
      multi-candidate, mis-citation, synthesis-card-re-emit paths.
- [ ] CHANGELOG entry + minor version bump.
