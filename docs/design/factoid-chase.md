# Factoid chase — trace cited claims back to their source

**Status**: draft (pre-review)
**Owner**: `src/precis/workers/`, `src/precis/handlers/factoid.py` (new),
new migration on top of `0001_initial.sql`
**Predecessors**:
- [`storage-v2.md`](./storage-v2.md) — establishes refs/chunks/links/derived-queue
- [`extract-once.md`](./extract-once.md) — establishes the stub-vs-real merge
  mechanic (`ref_identifiers` collapse on re-ingest)
**Related ADRs**:
- 0007 — derived queue (we extend the handler base to ref-level claims)
- 0008 — identifier scheme (stub refs use `ref_identifiers` like any other
  ref; no special "stub" identifier kind needed)

## Problem

A document being authored by a client (an agent or a human via the CLI)
states a *numerical or empirical claim* — "the device was held at 2.4 kV
for 30 s" — and finds a supporting citation in the corpus, say
**miller2020**. But when we open miller2020 at the relevant page, the
claim is itself attributed to **fischer2013** with no further detail.
The "real" provenance — who actually measured it, under what conditions
— is one or more citation hops away, often outside the local corpus.

Today this dead-ends in two places:

1. The agent has no structured way to express *"my claim cites miller2020,
   but miller2020 cites fischer2013 for that same number"*. The only
   primitive is `put(kind='memory', ...)` plus manual `link(...)` calls,
   which gives a flat note with no built-in fulfilment workflow.
2. fischer2013 may not be in the local corpus. The only "fetch this for
   me" mechanism is appending its DOI to the plaintext file
   `request_doi.md` (`src/precis/handlers/paper.py:336-355`). No
   structured row, no worker pickup, no link back to the claim that
   asked for it.

The net effect: every chase is done by hand, the provenance chain is
not retrievable by future agents asking the *same* question, and the
work to ingest fischer2013 doesn't auto-resume the chase once the PDF
lands.

## Goal

Make the citation-chase a first-class, persistent, retrievable artefact
of the system. Specifically:

- The claim becomes a `factoid` ref, embedded and searchable like any
  other ref.
- Each hop of the chase is a row in `links` with relation `cites` /
  `derived-from`, terminating when we hit a primary report (a paper
  that *measured* the value rather than re-citing it).
- Missing cited papers are materialised as **stub refs**: real
  `refs` rows with identifiers (DOI/arXiv/S2-id) and no PDF, tagged
  `STATUS:awaiting_pdf` so the operator can see what's needed.
- When a stub's PDF lands, the merge happens automatically (DOI hits
  the same `ref_id` via `ref_identifiers`; chunks/embeddings/summaries
  flow through the existing derived queue).
- A worker handler `chase_citation` claims factoids whose chain is
  incomplete and pushes the chase one hop forward each pass.

Crucially the worker handler is **generally applicable**: it is not
tied to the "factoid" kind. The same plumbing — claim a ref-level row
where some artifact is missing, do work, write the result — supports
`resolve_citation:s2`, `check_retraction:crossmark`
([`storage-v2.md:602`](./storage-v2.md)), and any future ref-scoped
artifact. This is the ref-level shape that ADR 0007 anticipated but
did not yet generalise into the worker base class.

## Non-goals

- **Automatic PDF fetching.** Sci-Hub / Unpaywall / publisher login is
  out of scope. The chase worker creates stubs and tags them
  `awaiting_pdf`; the user (or a separate, opt-in fetcher worker) is
  responsible for getting the PDF into the inbox.
- **Multi-claim factoid extraction from arbitrary documents.** A
  factoid is created by an explicit caller (`put(kind='factoid', …)`)
  or by the chase worker recursing — not by mining every paper for
  every numeric value.
- **Disagreement resolution / contradictions.** If fischer2013 says 2.4
  kV and a separate chain says 2.6 kV, we record both and surface the
  `contradicts` relation; we do not pick a winner.
- **Re-running the chase when upstream papers change.** Once a chain
  terminates, it stays terminated. Manual re-trigger only.

## What a factoid is

A factoid is a **synthesized artifact**: the proven endpoint of a
chase, carrying the fact itself plus the full provenance trail down
to the primary source. It is not "any claim someone wrote down" — it
is the *result* of grounding such a claim against a primary report.

Concretely a factoid ref carries:

| Field | Shape | Source |
| --- | --- | --- |
| `refs.title` | short fact title — *"2.4 kV held for 30 s"* | author-supplied at `put`; may be canonicalised by the synthesis pass once grounded |
| `factoid_body` chunk (ord=0) | detailed info — units, context, conditions, scope | author-supplied at `put` |
| `card_combined` (ord=-1) | title + detail + primary cite_key | computed |
| `meta.primary_cite_key` | the cite_key of the primary source | filled by the synthesis pass at chase termination |
| `meta.via_cite_keys` | ordered list of intermediate cite_keys | filled by the synthesis pass |
| `links --derived-from-->` chain | direct + indirect sources | written by the chase handler one hop at a time |
| `STATUS:chasing` tag | in flight | initial state |
| `STATUS:grounded` tag | terminal — primary source identified | replaces `chasing` at synthesis |

The ref exists during the chase too — it has to, because the chain is
attached to it. But until `STATUS:grounded`, it's a *claim in flight*,
not a usable factoid. Default `search(kind='factoid', ...)` filters
to grounded; in-flight chases surface only via
`search(kind='factoid', q=..., status='chasing')` or `precis stats`.

## Conceptual model

```
 ┌──────────────────────────────────────────────────────────────┐
 │  AGENT-AUTHORED DOCUMENT                                     │
 │  "...the device was held at 2.4 kV for 30 s [miller2020]..." │
 └─────────────────────────┬────────────────────────────────────┘
                           │  put(kind='factoid', text='2.4 kV …',
                           │       cited_in='miller2020#chunk:42')
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  factoid ref (pub_id=ab12c3, cite_key=factoid-…)             │
 │  • chunk_kind='factoid_body' — the claim text                │
 │  • tag STATUS:chasing                                        │
 │  • link  --supports-->  miller2020 chunk:42  (initial cite)  │
 └─────────────────────────┬────────────────────────────────────┘
                           │  chase_citation worker (pass 1)
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  reads miller2020 chunk:42, finds it cites [12] fischer2013  │
 │  fischer2013 not in corpus → create stub ref + identifier    │
 │  link factoid --derived-from--> fischer2013 (ref-level)      │
 │  tag fischer2013 STATUS:awaiting_pdf                         │
 │  factoid stays STATUS:chasing                                │
 └─────────────────────────┬────────────────────────────────────┘
                           │  user drops fischer2013.pdf into inbox
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  precis-watch picks up the PDF; precis_add() runs.           │
 │  DOI on the PDF matches the stub's ref_identifier → same     │
 │  ref_id. Stub gets pdf_sha256, chunks, embeddings.           │
 │  Tag STATUS:awaiting_pdf removed by the precis_add hook.     │
 └─────────────────────────┬────────────────────────────────────┘
                           │  chase_citation worker (pass 2)
                           ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  re-claims the factoid (target ref now has chunks).          │
 │  Locates the chunk in fischer2013 that states the value.     │
 │  No further citation in that chunk → mark chain terminal:    │
 │  link factoid --derived-from--> fischer2013 chunk:N          │
 │       (now chunk-scoped, replacing the ref-level edge)       │
 │  tag factoid STATUS:grounded; remove STATUS:chasing.         │
 └──────────────────────────────────────────────────────────────┘
```

The factoid is now retrievable by anyone:
`search(kind='factoid', q='2.4 kV')` → hit; `cite(kind='factoid', id=...)`
returns the claim text plus the cite-chain it grounds in.

## Schema impact

The v2 schema already supports almost everything. Two small additions
plus one new chunk_kind, in a new migration on top of `0001_initial.sql`.

### New ref kind `factoid`

```sql
INSERT INTO kinds (slug, enabled, title, description) VALUES
    ('factoid', TRUE, 'Factoid',
     'A retrievable empirical claim with an explicit provenance chain '
     'back to its primary source.');
```

### New `chunk_kind` `factoid_body`

```sql
INSERT INTO chunk_kinds (slug, is_card, description) VALUES
    ('factoid_body', FALSE, 'Factoid claim text');
```

The factoid is a numeric-id ref (like memory/todo/gripe), `ord=0` body
chunk of kind `factoid_body`. Card variants follow the standard scheme
(`card_combined`, `card_title`, …) so default search picks it up.

### Status tags (data, not schema)

Three values in the `STATUS` namespace, materialised on first use via
the existing `tags.insert ON CONFLICT` path — no migration:

- `STATUS:awaiting_pdf` — stub ref, identifiers known, no PDF yet.
- `STATUS:chasing` — factoid whose provenance chain is not terminal.
- `STATUS:grounded` — factoid whose chain terminates at a primary report.

### Ref-level derived-artifact table (queue substrate)

ADR 0007 establishes the chunk-level derived queue (`chunk_embeddings`,
`chunk_summaries`). The chase worker needs the same shape at ref-level
so "this factoid lacks a chase pass" is one `LEFT JOIN` away.

```sql
CREATE TABLE ref_artifacts (
    ref_id      BIGINT  NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
    artifact    TEXT    NOT NULL,
        -- 'chase_citation' | 'resolve_citation:s2'
        -- | 'check_retraction:crossmark' | ...
    payload     JSONB,                       -- handler-specific output
    status      TEXT    NOT NULL DEFAULT 'ok'
                CHECK (status IN ('ok', 'failed')),
    attempts    INT     NOT NULL DEFAULT 1,
    last_error  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, artifact)
);
CREATE INDEX ref_artifacts_failed_idx
    ON ref_artifacts (ref_id, artifact)
    WHERE status = 'failed';
```

The pre-existing retraction columns on `refs` (`retraction_checked_at`,
`retraction_status`, …) stay where they are; only *new* ref-level
artifacts use `ref_artifacts`. Reason: retraction state is hot-read
(`v_refs` exposes it directly, search filters on it); a join through
`ref_artifacts` for every search hit is unnecessary. The new artifact
kinds are cold-read by comparison.

## Worker plumbing

### Refactor `WorkerHandler` to be claim-shape parametric

Today `precis.workers.base.WorkerHandler.claim_batch` is hardcoded
to chunks (`src/precis/workers/base.py:88-113`). Introduce two
subclasses sharing the failure-marker / status code:

```
WorkerHandler                 (abstract; failure marker, status)
├── ChunkWorkerHandler        (today's body — claim over chunks)
│   ├── EmbedHandler          (unchanged)
│   └── RakeLemmaHandler      (unchanged)
└── RefWorkerHandler          (new — claim over refs)
    ├── ResolveCitationHandler  (calls S2; fills title/authors/year/aliases on a stub)
    └── ChaseCitationHandler    (does one chase hop)
```

`RefWorkerHandler.claim_batch` is the same `LEFT JOIN` pattern with
`chunks` swapped for `refs` and the predicate optionally filtered by
tag or kind (e.g. `WHERE r.kind = 'factoid'` for the chase handler).
`write_ok` / `write_failed` write to `ref_artifacts` instead of
`chunk_embeddings`. Approx. 80 LoC of refactor, no behaviour change
for existing handlers.

### `ChaseCitationHandler` — the chase, one hop per claim

Algorithm for one factoid `F`:

1. **Pick the current frontier.** The frontier of `F` is the most
   recent outgoing `derived-from` or `supports` link from `F` (or
   from a chunk reached via the chain). If no such link exists, the
   frontier is the initial citation passed at create-time.
2. **Read the cited chunk.** If the frontier is chunk-scoped, that's
   the chunk. If ref-scoped, locate the chunk in the target ref that
   the factoid text refers to (lexical + ANN search constrained to
   `ref_id = frontier`, top-1).
   - If the target ref has no chunks yet (stub), **emit a no-op
     `ok` row** with `payload={"waiting": "stub_pdf"}` and return.
     The chase resumes when the PDF lands (see *re-enqueue on chunk
     arrival* below).
3. **Decide: terminal or punt?**
   - **Terminal** if the chunk has no outgoing `cites` link AND no
     inline bibliographic reference pattern (regex over the chunk
     text). Add `derived-from F → frontier_chunk` (chunk-scoped),
     then run the **synthesis pass** (below), set `STATUS:grounded`,
     clear `STATUS:chasing`, done.
   - **Punt** otherwise. Determine the next reference:
     a. Read the source paper's S2 reference list
        (`precis.ingest.citations.citations(paper_id)` — already
        wraps S2 with retries).
        Pick the reference cited at the frontier chunk's position.
        Fallback: parse the Marker-extracted `references` chunk of
        the source paper (`chunks WHERE chunk_kind='references'`)
        and match by inline citation key.
     b. Resolve the next reference to a `ref_id`:
        - If its DOI/arXiv/S2-id hits `ref_identifiers`, use that
          `ref_id`.
        - Otherwise create a **stub ref**: insert `refs` row with
          `kind='paper'`, `title` from S2, `pdf_sha256=NULL`, plus
          `ref_identifiers` rows for whatever IDs S2 returned, plus
          tag `STATUS:awaiting_pdf`. Mint `pub_id` + `cite_key` as
          per the standard identity rules. Add to
          `ref_artifacts(artifact='resolve_citation:s2', status='ok')`
          so the resolve handler doesn't re-run on it.
        - Add link `F --derived-from--> next_ref` (ref-scoped — chunk
          unknown until the PDF lands).
4. **Write the chase result** to `ref_artifacts(artifact='chase_citation')`:
   `payload={"frontier": ..., "next": ..., "terminal": bool}`.

The handler is idempotent. Re-running on a factoid whose chain is
terminal short-circuits at step 3a's first check.

### Synthesis pass (only at termination)

When step 3 reaches a terminal frontier, the chase handler runs a
small synthesis pass *before* flipping the status tag:

1. Resolve `meta.primary_cite_key` = cite_key of the terminal ref.
2. Resolve `meta.via_cite_keys` = ordered cite_keys of every ref on
   the chain between F and the terminal, excluding endpoints.
3. Re-emit the `card_combined` chunk to include
   `<title> [primary=<cite_key>; via=<cite_keys…>]`. The card
   re-emits trigger a re-embed via the existing derived queue —
   the factoid becomes discoverable by queries that hit the
   primary-source's terminology, not just the original phrasing.
4. *(Optional, deferred)* Canonicalise `refs.title` from the primary
   source's wording. Kept off in v1 because LLM rewrites are a
   silent-edit risk; revisit if the original phrasings drift from
   the primary terminology enough to harm search.

Synthesis is idempotent: re-running computes the same `primary` /
`via` from the chain and the `card_combined` is regenerated only if
its text changed.

### Re-enqueue on chunk arrival

Once a stub gets its PDF and chunks land, the factoids that were
waiting on it should resume. Two options:

- **Polling (chosen).** The chase handler's claim query is
  `LEFT JOIN ref_artifacts ... WHERE status IS NULL OR
  payload->>'waiting' = 'stub_pdf' AND target_ref_has_chunks`.
  Waiting rows are kept claimable until the target materialises.
  No trigger, no callbacks, falls out of the existing derived-queue
  shape.
- **Trigger.** A Postgres `AFTER INSERT` trigger on `chunks` clears
  the waiting row. Rejected: triggers are invisible operationally
  and the polling cost is bounded (factoids in flight × handler
  interval).

### `ResolveCitationHandler` — fill stub metadata

The stub-creation path in `ChaseCitationHandler` does the *minimum*
needed for identity (one S2 call, set title + DOI + year). The
resolve handler is a *separate*, opt-in artifact that enriches a
stub more thoroughly: full authors list, abstract, citation count,
S2 references list, etc. Two passes keep the chase fast and the
enrichment async.

Claim shape: `refs LEFT JOIN ref_artifacts WHERE artifact='resolve_citation:s2'
IS NULL AND pdf_sha256 IS NULL`. Stops itself once the PDF lands
(at which point precis_add fills the same fields from CrossRef +
embedded metadata, more reliably than S2).

## CLI / MCP surface

Minimum-change principle: piggyback on the seven-verb surface.

### `put(kind='factoid', …)` — start a chase

```
put(kind='factoid',
    title='2.4 kV held for 30 s',
    text='Device prep: 2.4 kV applied across the gate dielectric for '
         '30 s at room temp, prior to step <N>.',
    cited_in='miller2020#chunk:42')
    → creates factoid ref, initial `supports` link to miller2020 chunk:42,
      tag STATUS:chasing, returns pub_id
```

`title` is the short fact title (one line, embeddable). `text` is the
detailed body (units, context, conditions). `cited_in` is the
starting frontier of the chase. The factoid is **not yet a usable
factoid** — it's `STATUS:chasing` until grounded.

### `search(kind='factoid', q=...)` — find grounded facts

Default search filters to `STATUS:grounded`. The TOON rendering uses
the columns you specified:

```
id     | title                                | primary_cite
-------+--------------------------------------+--------------
ab12c3 | 2.4 kV held for 30 s [device prep]   | fischer2013
de45f6 | 0.1 mol/L NaCl in citrate buffer     | lin1998
```

`id` is the factoid's 6-char `pub_id` (the "hashed slug"). `primary_cite`
is the cite_key of the terminal ref from `meta.primary_cite_key`.
Override the status filter with `search(kind='factoid', q=..., status='chasing')`
or `status='*'` to see in-flight chases.

### `get(kind='factoid', id=<pub_id>)` — detail view

```
factoid ab12c3
  title: 2.4 kV held for 30 s [device prep]
  detail: Device prep: 2.4 kV applied across the gate dielectric for
          30 s at room temp, prior to step <N>.
  primary: fischer2013 (10.1234/xyz)  [pub_id k4j7m2]
  via:
    - miller2020 (10.5678/abc) — chunk:42 — initial citation
    - kumar2018  (10.9012/def) — chunk:17 — secondary hop
  grounded: 2026-05-29  by  chase_citation:s2
```

`primary` and `via` are rendered from the chain in `links`. The
status line at the bottom shows when the chase terminated and which
handler did it. For `STATUS:chasing` factoids, the chain is shown
with the in-flight frontier marked and `STATUS:awaiting_pdf` stubs
flagged.

### `cite(kind='factoid', id=<pub_id>)`

Returns the factoid's own `cite_key` followed by the cite_keys of every
ref on the chain, primary first, in TOON. Callers that want just the
primary use `cite(... want='primary')`. (Same as you'd find with `s2 id`
on a stub — any of S2 id, our `pub_id`, our `cite_key` work as input
handles, per ADR 0008.)

No new verbs. The factoid kind handler is a `NumericRefHandler`
subclass like memory/todo/gripe (`src/precis/handlers/memory.py:26`)
plus the `cited_in` argument on `put(...)`.

### Worker CLI

`precis worker` gains two `--only` choices: `chase` and `resolve`.
Both run by default. `precis worker --status` already iterates
registered handlers and prints `(total | ok | failed | pending)`
per artifact — works as-is once the two new handlers register
themselves.

### Stub visibility

`precis stats` already exists (per `storage-v2.md:651`); add a
section for `STATUS:awaiting_pdf` refs so the operator sees the
fetch backlog. `request_doi.md` (the plaintext-file hack at
`paper.py:336-355`) is **deprecated, not deleted** — the empty-search
DOI suggestion is updated to point at `put(kind='factoid', ...)` or
to a future `precis request-paper <doi>` command. We delete the
plaintext path in a follow-up once `precis stats` is the established
backlog view.

## Paper-gated edits

You asked: do we have them already? **No.** The schema carries
`refs.human_verified_at` + `_by` + `_note`
(`src/precis/store/types.py:113-115`) but no handler reads them as a
gate; `precis verify` is in `storage-v2.md:573-577` but unimplemented.

For *this* feature, the relevant question is: do we let an unverified
ref be a source of a chase chain? Two positions:

- **Lenient** (default): no gate. The chase populates whatever it finds.
  Risk: garbage propagates through provenance chains. Mitigation:
  every link records `set_by` (actor) so it's traceable; users can
  curate by deleting suspect chains.
- **Strict**: a factoid cannot be marked `STATUS:grounded` unless every
  ref on its chain has `human_verified_at IS NOT NULL`. The chain
  still builds; just the grounded badge requires human sign-off.

Recommendation: **lenient for chasing, strict for the grounded badge.**
The chase is a "this is what the corpus says"; the badge is "and a human
double-checked the corpus". Adds ~5 lines in the chase handler and a
read-side filter in the factoid kind handler.

Implementing `precis verify` is a separate, small piece of work; it's
out of scope for this design but I'd land it in the same release so
the `STATUS:grounded` upgrade path is usable on day one.

## Schema / API / Ingest / Performance thresholds

Against `docs/conventions/thresholds.md`:

- **Schema**: adds a new table (`ref_artifacts`), a new kind row, a new
  chunk_kind row. No ALTER on existing tables. No column drops or
  renames. No dim changes. ✅ (No threshold trip — new tables are
  additive.)
- **API**: no removed flags, no MCP response shape changes, seven-verb
  surface unchanged. ✅
- **Ingest**: `pdf_sha256` / `cite_key` / `pub_id` rules unchanged.
  Stub creation uses the same `make_pub_id` / `make_cite_key` paths
  as fresh ingest. ✅
- **Performance**: chase handler makes one S2 call per hop (bounded by
  `chunks_per_factoid × hops_per_chain` — measured tens, not
  thousands). No new global model load. ✅
- **Cross-package**: no new top-level dep. S2 is already vendored
  (`semanticscholar` via `precis.ingest.citations`). ✅

No threshold trips. Proceeding without checkpoint.

## Open questions

1. **Factoid as ref vs. factoid as chunk on an "umbrella" ref.**
   **Settled: ref.** A factoid is the synthesised result of a chase,
   carrying its own primary cite and via-chain. It needs its own
   `pub_id`/`cite_key` so it's citable from outside; its own tags +
   verification; and the ability to be the *source* of another
   factoid (transitive grounding). The chunk-on-umbrella model
   collapses these into one row and loses retrievability.
   Counter-argument: more rows, more vacuum work if factoid count
   explodes. Mitigation: a soft-delete `STATUS:stale` tag and a
   future GC pass; defer until measured.

2. **Citation source: S2 first vs. Marker-references first.**
   Chosen: S2 first, Marker fallback. S2 has the explicit graph,
   typed (DOI/arXiv/S2-id); Marker `references` chunks are free-text
   and require regex/LLM parsing per paper.
   When S2 misses (preprints S2 hasn't indexed, internal reports,
   patents): fall back to Marker. The handler logs `payload={"source":
   "marker_fallback"}` for observability.

3. **Locating the cited chunk within the target paper.**
   Chosen for v1: lexical + ANN search constrained by `ref_id`,
   top-1, with a confidence threshold; below threshold, emit
   `payload={"low_confidence": true}` and stop. LLM-assisted
   localisation is a v2 option (one additional handler:
   `locate_claim_chunk:gpt-4-mini`, gated on cost).

4. **Stub identifier minimum.** What's the smallest set of fields
   that justifies creating a stub? Chosen: **at least one identifier
   from {DOI, arXiv, S2 id}** plus a title. The stub gets a minted
   `pub_id` (our hashed slug) and `cite_key` like any other ref;
   the S2 id is stored as a `ref_identifiers (id_kind='s2')` row so
   the factoid can be cited by either handle (the S2 id while
   pre-PDF, the cite_key once we know the authors firm enough to
   mint it cleanly).
   With only `(authors, year, title)` from a parsed reference list
   and no external ID at all, fuzzy-match against
   `ref_identifiers (id_kind='cite_key')` via the trigram index
   (`ref_identifiers_cite_key_trgm_idx`) before creating a new
   stub — avoids duplicate stubs for the same paper. If even that
   fails, **skip the hop and write `payload={"unresolvable": ...}`**
   rather than minting a half-blind stub; the chain is recorded as
   far as it got and the operator can resolve manually.

5. **Re-running a chase after the upstream changes.** A retracted
   target ref should ideally retract the factoid's `grounded`
   status. Chosen for v1: no — manual re-trigger only
   (`precis chase --refresh <pub_id>` to be added if demand
   materialises). The retraction propagation work is already
   queued for "after links are populated"
   ([`storage-v2.md:614-617`](./storage-v2.md)); chase chains
   become an input to that work.

6. **Cost gate on S2.** S2 has a generous unauthenticated rate
   limit but it does throttle. The chase handler should respect
   `tenacity` backoff (already done in
   `precis.ingest.citations.citations`). For high-volume chases we
   add `SEMANTIC_SCHOLAR_API_KEY` plumbing through `_build_handlers`
   in `cli/worker.py` — same shape as the existing `--s2-api-key`
   flag on `precis add`.

7. **`request_doi.md` deprecation.** Today's empty-search DOI hit
   (`paper.py:336-355`) tells the agent to append a DOI to a
   plaintext file. After this lands, the same path can offer
   `put(kind='factoid', ...)` plus a new `precis request-paper
   <doi>` shortcut. Question: do we keep the plaintext file as a
   compatibility surface for one release, or hard-cut? Default:
   keep one release, log a deprecation warning, remove next.

## Test plan

1. **Unit — `ChaseCitationHandler` algorithm.**
   - Factoid with `cited_in` pointing at an in-corpus chunk that
     has no outgoing `cites` → terminal on first pass, `STATUS:grounded`.
   - Factoid pointing at a chunk with a `cites` link to a stub →
     `payload={"waiting": "stub_pdf"}`, factoid stays `STATUS:chasing`.
   - After stub gets chunks (fixture: directly INSERT chunks for the
     stub), re-run → completes (terminal or one more hop).
   - Cycle protection: a chain that revisits a ref already on its
     chain raises `BadInput` and writes `status='failed'`.
2. **Unit — `ResolveCitationHandler`.**
   - Stub with only DOI → S2 mock returns full metadata → fields
     fill, `STATUS:awaiting_pdf` retained until PDF arrives.
   - S2 miss → `status='failed'`, `last_error` captures the cause.
3. **Integration — `precis worker --only chase` end-to-end.**
   Seed two papers (one with a `cites` link to the other), create
   one factoid, run worker once, observe terminal state on the
   factoid + correct `derived-from` chain.
4. **Integration — `precis_add` clears `STATUS:awaiting_pdf`.**
   Create a stub for a known DOI; drop the matching PDF on
   `precis_add` path; assert the same `ref_id`, that the tag is
   removed, that the chase handler re-claims the dependent factoid.
5. **MCP surface — `put(kind='factoid', ...)` round-trip.**
   Create, `get`, `search`, `cite` — full TOON output asserted.
6. **Regression — `request_doi.md` deprecation warning fires** on
   the empty-search DOI path; suggested follow-up call is
   `put(kind='factoid', ...)`.

## Implementation order

Same B-step naming convention as `storage-v2.md` so commit messages
match plan.

- **C0** This design doc lands; ADR follow-up if any open question
  surfaces a substantive choice during review.
- **C1** New migration `0002_factoid_and_ref_artifacts.sql`:
  - `ref_artifacts` table
  - kind `factoid`
  - chunk_kind `factoid_body`
  - update PUML diagram (`docs/design/schema-v2.puml`)
- **C2** `precis.identity` extension — `make_factoid_paper_id()`
  (synthetic, derived from `(claim_text, initial_cite_pub_id)` so
  re-creating the same factoid from the same client produces the
  same `pub_id`).
- **C3** `precis.handlers.factoid` — NumericRefHandler subclass,
  `put(... cited_in=...)` shape, card variants, search wiring.
- **C4** Worker base refactor — split `WorkerHandler` into
  `ChunkWorkerHandler` + `RefWorkerHandler`, no behavioural change
  for `EmbedHandler` / `RakeLemmaHandler`.
- **C5** `ResolveCitationHandler` — stub-fill, S2 backed,
  registered in `precis.workers.runner`.
- **C6** `ChaseCitationHandler` — the chase logic itself. Largest
  step; gated behind unit-test coverage on every branch listed
  above.
- **C7** `precis_add` post-write hook to clear `STATUS:awaiting_pdf`
  when the merged `ref_id` matches a stub's identifier.
- **C8** Deprecation pass — `paper.py` empty-search DOI hint
  updated to suggest the factoid flow; `request_doi.md` retained
  with a deprecation banner.
- **C9** CHANGELOG entry + version bump. New CLI `--only chase` /
  `--only resolve` documented in README. ADR for any
  non-obvious trade-off settled during C4-C6.

Each step ships its own commit with tests. C1 runs
`precis migrate --dry-run` against a fresh DB.

## Risk

- **Chase loops.** A pathological corpus could have circular
  citation chains (paper A cites paper B cites paper A). Mitigation:
  the chase handler maintains an in-payload `visited: [ref_id, …]`
  set; revisit raises `BadInput`, written as `status='failed'`. Loop
  size is bounded by the chain length we're willing to record.
- **S2 outage.** Chase progress halts but no data is corrupted —
  failed rows accumulate, operator deletes them after S2 recovers.
  ADR 0007's "no automatic retry" rule applies.
- **Stub explosion.** A single chase could create dozens of stubs.
  Mitigation: `precis stats --awaiting-pdf` surfaces the backlog;
  a future opt-in fetcher worker (Unpaywall + IA fallback) drains
  it. Stubs cost ~1 KB each so the storage cost is negligible
  even at thousands.
- **Wrong chunk localisation.** The lexical-ANN top-1 might pick a
  related-but-wrong chunk in the target paper. Mitigation: confidence
  threshold + `low_confidence` payload flag; agents reading the
  chain see the flag and can re-anchor manually
  (`edit(kind='factoid', id=..., anchor='<target_pub_id>#chunk:N')`).
- **Verification overhead.** If we ship strict `STATUS:grounded`
  and the user verifies few refs, no chains terminate. Mitigation:
  the badge is opt-in (it adorns; absence is not failure).

## Definition of done

- [ ] `0002_factoid_and_ref_artifacts.sql` applies cleanly; PUML
      diagram updated.
- [ ] `put(kind='factoid', text=..., cited_in=...)` creates a
      ref + initial chunk + initial link in one transaction.
- [ ] `precis worker --only chase` advances factoids by one hop
      per pass; `--once` mode is deterministic for the test suite.
- [ ] Stubs get a `STATUS:awaiting_pdf` tag and a minimal
      `ref_identifiers` row; `precis_add` on the matching PDF
      merges into the same `ref_id` and clears the tag.
- [ ] `search(kind='factoid', q=...)` returns hits with their
      provenance chain inlined in the TOON `cite` field.
- [ ] `precis worker --status` shows `chase_citation` and
      `resolve_citation:s2` alongside the existing handlers.
- [ ] Tests cover terminal, waiting, punt, cycle, and S2-failure
      paths.
- [ ] No existing test in the chunk-level handler suite changes
      behaviour (refactor is shape-preserving).
- [ ] CHANGELOG entry + minor version bump.
