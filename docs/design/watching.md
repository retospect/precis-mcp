# Watching — citation-forward corpus growth

The **watcher** is the second *attention actor* over the salience field
the dreamer already maintains (`docs/design/dreaming.md`). It grows the
corpus along the citation graph of the papers we actually engage with —
no hand-picked seeds, no second scorer.

## One salience field, two attention loops

A chunk's salience is `last_seen − last_<actor>` (knob-free argmax; see
dreaming.md §Target selection). `last_seen` is heated on every external
access (`bump_salience`); each actor rotates on its **own** stamp so the
loops don't cool each other:

| | Dreamer | Watcher |
|---|---|---|
| selects | most-due salient **chunk** | most-due salient **papers** |
| action | internal synthesis | poll S2 forward-citations |
| heat | `bump_salience` | **same** `bump_salience` |
| rotate | `last_dreamt` | `last_watched` |
| select | `select_salient("dream", …)` | `select_salient("watch", …)` |
| self-heat guard | `as_background_actor` | **same** `as_background_actor` |

The shared self-heat suppression (`store/_salience.py`) is a
**correctness invariant**, not just DRY: the watcher reads the corpus to
pick seeds and would heat what it watches into an echo chamber without
it — the identical failure the dreamer's suppression exists to prevent.

Storage: `chunks.last_watched` (migration `0024_watching.sql`), an exact
mirror of `last_dreamt`. Adding a third actor is one column + one row in
`_ATTENTION_COLUMNS`; if it ever happens, collapse the per-actor columns
into an `attended JSONB {actor: ts}` map (zero-migration thereafter).

## The watch pass (`workers/watch_poll.py`)

Deterministic, no LLM. Each pass, under `as_background_actor("watch")`:

1. `select_salient("watch", kinds=("paper",), limit=N)` — the top-N
   most-due salient papers. Engaged-with papers (read, cited, searched)
   heat up and rise; untouched ones cool and age out. Fully automagic
   and self-maintaining.
2. For each seed, fetch **forward-citations** (papers that cite it) from
   Semantic Scholar via `ingest/citations.py` (`cited_by`).
3. Mint each new citing paper as a metadata-only **stub**
   (`upsert_stub_paper`, `pdf_sha256 IS NULL`) — idempotent, so
   already-held/already-discovered papers are a no-op. New stubs are
   tagged `source:semantic-scholar` + `discovered-via:cite:<seed>`.
4. Rotate the seed (`touch_attended("watch", …)`) so a different salient
   paper tops the next pass.

## Acquisition is not the watcher's job

A minted stub carries a DOI/arXiv/S2 id, so the existing `fetch_oa`
worker auto-claims it and does **OA-gated** acquisition: an open-access
copy is fetched, a paywalled one stays a discovered stub. That is
exactly "get only if automatically gettable, otherwise auto-discovered;
fetch on demand" — for free, by reusing the existing pipeline.

## Cadence & relevance

* **Cadence:** `watch_poll` is *not* in `system_passes`/`agent_passes` —
  it makes external S2 calls and must run on a schedule, via
  `precis worker --only watch_poll` from a dedicated low-frequency cron
  (mirror the dream LaunchDaemon).
* **Relevance gate (v1):** a per-seed cap (`max_per_seed`); overflow is
  logged, never silently dropped.
* **Relevance gate (follow-up):** embedding-similarity of each citing
  paper's abstract to the seed/active-corpus centroid (reusing
  `chunk_embeddings`/HNSW + the dreamer's cosine "spark" sampler). Needs
  on-the-fly embedding of external abstracts; deferred.

## Deferred

* **Embedding relevance gate** (above).
* **`watch` kind** — explicit/cold-start watches (a stored topic or RSS
  watch, e.g. seeding the 6 openclaw topics before the corpus is rich
  enough for citation-forward to self-seed). The salience-derived
  cite-forward loop needs no stored watch refs, so v1 ships without it.
* **Self-tuning:** promote/demote seeds by whether their discovered
  stubs get *used* (acquired, cited) — the usage signal already exists.
