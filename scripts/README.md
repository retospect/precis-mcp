# precis-mcp / scripts

Operator utilities that wrap the precis-mcp package without going
through the `precis` CLI. Each command is a thin Bash wrapper that
picks an interpreter in this order:

1. **Shared workspace venv** at `pips/.venv` — the default since the
   single-venv migration. The workspace root's `dev` dependency group
   pulls `precis-mcp[paper]` (acatome-extract + sentence-transformers)
   into this venv, so no per-package venv is required.
2. **Legacy per-package venv** at `packages/precis-mcp/.venv` — kept
   as a fallback for checkouts that pre-date the migration.
3. **`uv run --project <workspace>`** — last resort, on-demand sync.

The wrapper resolves these paths itself so scripts work regardless of
the caller's CWD or shell.

## Environment

Every wrapper sets sensible defaults if not already exported:

- `PRECIS_DATABASE_URL` — defaults to
  `postgresql://acatome:acatome@127.0.0.1:5432/precis`
  (the canonical local v2 database).
- `PRECIS_EMBEDDER` — defaults to `bge-m3` (loads `BAAI/bge-m3` via
  `sentence-transformers`).

Override either by exporting the variable before invoking the script.

## Commands

### `paper-count`

Print counts of paper refs and (optionally) breakdowns.

```sh
./scripts/paper-count                      # papers + total blocks
./scripts/paper-count --by-kind            # all kinds, sorted by count
./scripts/paper-count --by-provider        # paper rows per provider
./scripts/paper-count --recent             # 10 most recent papers
./scripts/paper-count --recent 50          # custom N
```

### `paper-monitor-ingest-dir`

Watch a directory for new top-level `*.pdf` files. For each one:

1. Run `acatome-extract` → produces a `.acatome` bundle.
2. Insert the bundle via `Store.ingest_bundle(...)` (idempotent on
   DOI / pdf_hash / arxiv_id).
3. On success: move the PDF + bundle into `<dir>/completed/`.
4. On failure: move the PDF into `<dir>/errors/` alongside a
   `<stem>.error.log` traceback.

```sh
# default watch dir = /Users/bots/Documents/cluster/paper-ingest
./scripts/paper-monitor-ingest-dir
./scripts/paper-monitor-ingest-dir --once          # one sweep, no loop
./scripts/paper-monitor-ingest-dir --interval 30   # poll every 30 s
./scripts/paper-monitor-ingest-dir --tag review-queue --tag urgent
./scripts/paper-monitor-ingest-dir --no-verify     # skip metadata cross-check
./scripts/paper-monitor-ingest-dir --dir /some/other/inbox
```

`Ctrl+C` (or `SIGTERM`) drains the current PDF and exits cleanly.

### `find-citing-papers`

For every paper in precis with a DOI or arXiv id, fetch the citation
list from Semantic Scholar, filter by date window and relevance, dedup
against the corpus, and write a markdown report plus a JSONL feed of
unique citing papers.

```sh
./scripts/find-citing-papers                              # last 180 days, default relevance gate
./scripts/find-citing-papers --since 2026-02-01           # explicit window start
./scripts/find-citing-papers --until 2026-07-31           # explicit window end
./scripts/find-citing-papers --influential-only           # require S2 isInfluential=True
./scripts/find-citing-papers --keep-background            # keep background-only intents
./scripts/find-citing-papers --limit 100                  # only the N most recently ingested source papers
./scripts/find-citing-papers --slug-prefix abazari        # filter source corpus by slug
./scripts/find-citing-papers --no-fetch                   # aggregate from existing cache only
./scripts/find-citing-papers --force                      # ignore cache, refetch every paper
```

**Noise reduction** (the full corpus returns ~900k unique citing
papers — these flags are how you make a digestible report):

```sh
# Co-citation density: drop papers that cite fewer than N of ours.
# Strongest, cheapest signal. 909k → 212k @ 2; → 25k @ 5; → ~3k @ 10.
./scripts/find-citing-papers --no-fetch --min-co-cites 5

# Drop fresh preprints that haven't been cited yet (signal: traction).
./scripts/find-citing-papers --no-fetch --min-citing-citations 5

# Hard cap on output: top N after sort.
./scripts/find-citing-papers --no-fetch --top-n 200

# bge-m3 cosine rerank (the gold standard relevance gate). Loads
# the embedder once, embeds source corpus + surviving citing papers'
# title+abstract, drops anything below cosine similarity threshold.
# Adds ~80-100s per ~2.5k surviving citing papers on Apple Silicon.
./scripts/find-citing-papers --no-fetch --min-co-cites 5 --min-similarity 0.55

# Per-source-top-K: emit top K most-recent citations PER OUR paper
# (separate output mode, useful for "what's new for paper X" digests).
./scripts/find-citing-papers --no-fetch --per-source-top 5

# Recommended starting digest combo:
./scripts/find-citing-papers --no-fetch \
    --since 2026-01-01 \
    --min-co-cites 3 \
    --min-citing-citations 1 \
    --min-similarity 0.55 \
    --top-n 200
```

Sort precedence in global mode: co-citations DESC, similarity DESC
(when computed), publication date DESC, title.

Per-paper results are cached as JSON under
`paper-ingest/.citing-papers-cache/<slug>.json` (override with
`PRECIS_CITING_CACHE_DIR` or `--cache-dir`) so the sweep is
**resumable** — re-runs reuse cache files unless `--force` is passed.

Reads `SEMANTIC_SCHOLAR_API_KEY` from the environment to raise S2's
free-tier rate limit. A full sweep on ~4k papers takes 1–3 hours
depending on rate-limit hits and how heavily-cited each source is.

The default markdown report goes to
`paper-ingest/citing-papers-<UTC-timestamp>.md`; the JSONL feed
sits next to it with `.jsonl` extension and is shaped for downstream
ingest (e.g. piping through `acatome-quest submit`).

### `enrich-paper-identifiers`

One-shot sweep that walks every live paper ref and fully populates
`ref_identifiers` from Semantic Scholar's `externalIds` cluster
(DOI / ArXiv / PubMed / PubMedCentralID / MAG / DBLP / CorpusId /
OpenAlex). Migration `0009_ref_identifiers.sql` backfilled the four
canonical schemes (DOI, arXiv id, S2 paperId, pdf_hash) from
existing meta JSON; this sweep adds everything else by re-asking S2.

After the sweep, `doilist scan` sees the maximum-coverage alias
index — sources/ DOIs that match ANY known identifier of any
ingested paper get caught, not just the canonical four.

```sh
./scripts/enrich-paper-identifiers --dry-run --limit 5  # sanity check
./scripts/enrich-paper-identifiers                       # full sweep
./scripts/enrich-paper-identifiers --limit 500           # first 500 only
./scripts/enrich-paper-identifiers --re-enrich           # ignore the
                                                         # `s2-enriched`
                                                         # tag and re-query
```

Idempotent via the `s2-enriched` open tag attached to each ref on
completion. Re-runs of the script skip already-tagged refs unless
`--re-enrich` is passed. Failures are logged but don't abort the
sweep — re-run later to pick them up.

Reads `SEMANTIC_SCHOLAR_API_KEY` from the environment to raise S2's
free-tier rate limit. Without it the sweep still runs but is slower
and more likely to hit 429 errors. Fixed soft delay of 0.4s between
calls keeps us well under any S2 rate cap.

Estimated wall-clock: ~2.5s per paper, so ~100–175 min for a 4k-paper
corpus (S2 latency dominates the inter-call sleep).

### `doilist`

DOI triage / fetching / rewriting. Subcommands:

- `doilist scan` — extract DOIs from `sources/`, dedupe against
  precis (across every alias scheme), validate via doi.org, write
  `dois_to_get.md` plus `.doi_status.json`. `--download` to also
  fetch the resulting OA PDFs via Unpaywall.
- `doilist download` — fetch PDFs for the queue file, slowly.
- `doilist recheck` — re-clean and re-validate previously-invalid
  DOIs.
- `doilist skip <doi> --reason <tag>` / `doilist unskip <doi>` —
  mark DOIs as retracted / paywalled / out-of-scope without dropping
  them from the cache.
- `doilist convert-doi-to-slugs <dir>` — see below.

#### `doilist convert-doi-to-slugs`

Walk a directory and rewrite known DOIs to `[slug]` in text files,
in place. Best-effort: any DOI not in precis is left untouched.

Useful for the perplexity-summary triage workflow: drop a directory
of summaries into a paper outline, then collapse every DOI string
into the precis paper slug so downstream tools (LaTeX bib generation,
agent prompts, search) can work with native identifiers instead of
opaque DOI URLs.

```sh
# default: cwd, recurse, rewrite *.md and *.txt in place
./scripts/doilist convert-doi-to-slugs

# explicit directory + dry-run (counts only, no writes)
./scripts/doilist convert-doi-to-slugs --dry-run path/to/dir

# also rewrite .tex sources
./scripts/doilist convert-doi-to-slugs --ext md --ext txt --ext tex .
```

Both URL-form (`https://doi.org/10.x/y`) and bare (`10.x/y`) DOIs are
matched; URL prefixes are consumed alongside the DOI so no broken
links remain. arXiv DOI form (`10.48550/arXiv.<id>`) maps to the
same slug as the bare arXiv id.

One bulk SQL JOIN over `ref_identifiers × refs (kind='paper')` builds
the slug map at startup and is reused across all files — typical
runtime is well under a second for a directory of markdown notes.

## Layout

```
scripts/
  _common.py                          # shared store/embedder helpers
  _paper_count.py                     # python impl
  _paper_monitor_ingest_dir.py        # python impl
  _find_citing_papers.py              # python impl
  _enrich_paper_identifiers.py        # python impl
  _doilist.py                         # python impl
  paper-count                         # bash wrapper
  paper-monitor-ingest-dir            # bash wrapper
  find-citing-papers                  # bash wrapper
  enrich-paper-identifiers            # bash wrapper
  doilist                             # bash wrapper
  README.md
```

The leading-underscore Python files are private impls — invoke the
wrappers, not the `.py` files directly.
