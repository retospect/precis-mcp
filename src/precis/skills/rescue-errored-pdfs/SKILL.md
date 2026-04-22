---
name: rescue-errored-pdfs
description: >
  Recover PDFs stuck in acatome-extract's `paper-ingest/errors/` after
  failed metadata verification.  Triage true duplicates vs re-ingestable
  files, write `.meta.json` sidecars to bypass the title-match gate, and
  push them back through the watch pipeline.
user-invocable: true
argument-hint: [errors-dir]
allowed-tools: [get, put]
applies-to: [paper]
tags: [papers, ingestion, operations, recovery, workflow]
---

## When to Use

- User says "there are papers stuck in errors" or drops a path inside `paper-ingest/errors/`
- A paper failed watch-pipeline verification with `UnverifiedPaperError: Paper failed verification (Title mismatch: ...)` or `(no DOI/title match)`
- An `.acatome` bundle exists in `errors/` with an obviously-wrong slug (e.g. `ouigwe2013microsoft` for a Teghammar thesis, or `publishedonlinemarchspringersc...`)
- A paper was ingested previously with mangled metadata (author concatenation in slug, null DOI where one should exist)

## Why This Happens

`acatome-extract` runs a verification gate that scores the *metadata title* against the *first-pages text* of the PDF.  When Marker's extracted header text doesn't include the paper title verbatim — common for scanned proofs, theses, prepress acs_nn_nn-\* files, or papers where page 1 starts with author affiliations — the score drops below 80 and the paper lands in `errors/`.  The metadata is usually *correct*; the gate is just pessimistic.

## The Recovery Loop

### 1. Triage

For each PDF in `errors/`:

**(a) Is it a byte-duplicate of something already handled?**
```bash
python3 -c "import hashlib; print(hashlib.sha256(open('<pdf>','rb').read()).hexdigest()[:16])"
```
Compare against PDFs already in `~/.acatome/papers/` or `paper-ingest/completed/`.  Same hash → move to `errors/duplicates/`, delete the `.error.txt`.

**(b) Is the DOI already in the store?**
```bash
psql -h localhost -U acatome -d acatome \
  -c "SELECT slug, doi FROM refs WHERE doi='10.x/y';"
```
Hit → move PDF to `errors/duplicates/`.  Delete orphan `.acatome` stubs in `errors/`.

**(c) Is it already ingested but with bad metadata?**
Check `refs` by title fuzz.  If found with a mangled slug / null DOI / author concatenation, plan a surgical DB fix (see Step 5) instead of re-ingest.

**(d) Otherwise: re-ingest with sidecar.**

### 2. Inspect the Existing `.acatome` Stub

The watch pipeline already ran Marker, so the blocks are extracted — check what it captured:

```python
import gzip, json
with gzip.open('errors/<stem>.acatome', 'rt') as f:
    data = json.load(f)
h = data['header']
print(h.get('title'), h.get('doi'), h.get('year'))
print([a['name'] for a in h.get('authors', [])[:3]])
print('blocks:', len(data.get('blocks', [])))
```

- **Blocks > 0, metadata correct, only `verified=False`** → minimal sidecar (`{"verified": true}`) is enough.
- **Blocks > 0, metadata wrong** → write a full sidecar overriding title/author/doi/year.
- **Blocks == 0** → Marker extraction failed; sidecar + re-extract from scratch.

### 3. Write a `.meta.json` Sidecar

`acatome-extract` reads `<stem>.meta.json` next to each PDF.  Override keys:

```json
{
  "title": "Canonical title from the paper",
  "author": ["Surname, Given", "Surname2, Given2"],
  "doi": "10.x/y",
  "year": 2019,
  "journal": "Journal Name",
  "type": "article",
  "verified": true
}
```

**Critical rules:**

- **Always use `"Surname, Given"` format** in the `author` array.  The slug generator splits on the first comma; `"Albert G. Nasibulin"` (no comma) yields slug `albertgnasibulin2011...` instead of `nasibulin2011...`.
- **`verified: true`** bypasses the title-match gate.  Only set this when *you* have confirmed the metadata matches the PDF.
- **`type: "techreport"`** for theses and reports — skips the Crossref/S2 cascade (which won't find them anyway).  Other non-article types: `datasheet`, `manual`, `notes`, `other`.
- **Omit `author` when the DOI resolves via Crossref** — the resolver returns properly-formatted author names.  Only supply authors for DOIs Crossref doesn't index (e.g. `10.13140/RG.*` ResearchGate DOIs) or thesis/report types.
- **An empty string clears nothing; `null` clears a field.**  Use this to wipe a bad `s2_id` from an earlier lookup.

### 4. Clean Stale Files

Remove the failed leftovers so watch doesn't re-use them via the "shared bundle" fast-path (which would propagate the same bad metadata):

```bash
rm errors/<stem>.acatome errors/<stem>.error.txt
rm errors/<wrong-slug>.acatome   # the mis-slugged output bundle
```

### 4a. Decide the Re-trigger Path

There are two ways to push the PDF through the pipeline, and **the choice matters**:

**(A) Watch-based re-trigger** — works only when the PDF's SHA-256 hash is **not** in the current watch session's `seen_hashes`:

```bash
mkdir -p paper-ingest/retry
mv errors/<stem>.pdf errors/<stem>.meta.json paper-ingest/retry/
tail -f paper-ingest/ingest.log
```

Subdir names become tags automatically, so `retry/` tags these papers with `retry` — useful for later audit.

**The `seen_hashes` trap.** The watch process computes a SHA-256 of every PDF it processes — *including during the backfill pass that runs on startup*.  If a PDF was sitting in `paper-ingest/` root when watch started, backfill hashed it, tried to extract, failed verification, and moved it to `errors/` — **the hash is still in `seen_hashes` for the rest of that session**.  Moving the PDF back into any non-skip subdir triggers the hash-dedup gate instead of re-processing:

```
acatome.watch new: 380752.380786.pdf
acatome.watch   duplicate (hash): 380752.380786.pdf → errors/duplicates/
```

Diagnose by checking when the PDF was first seen this session:

```bash
grep "new: <stem>" paper-ingest/ingest.log
grep "backfill:" paper-ingest/ingest.log | tail -5
```

A `new:` entry predating the last `backfill:` line means the PDF is in `seen_hashes`; skip to path (B).

**(B) Direct CLI** — bypasses the watch entirely.  Always works, and is the right choice for previously-errored PDFs on a long-running watch:

```bash
# Stage PDF + sidecar outside the watch tree so watch ignores it
mkdir -p /tmp/acatome-rescue
mv paper-ingest/errors/<stem>.pdf /tmp/acatome-rescue/
mv paper-ingest/errors/<stem>.meta.json /tmp/acatome-rescue/

# Extract reads the sidecar, writes the bundle to ~/.acatome/papers/<first-letter>/
uv run acatome-extract extract /tmp/acatome-rescue/<stem>.pdf

# Enrich adds embeddings (sentence-transformers bge-m3)
uv run acatome-extract enrich ~/.acatome/papers/<l>/<slug>.acatome

# Ingest persists to postgres + pgvector
uv run acatome-store ingest ~/.acatome/papers/<l>/<slug>.acatome

# Filesystem hygiene — watch skips completed/, so no re-processing
mv /tmp/acatome-rescue/<stem>.pdf paper-ingest/completed/
rm /tmp/acatome-rescue/<stem>.meta.json
rmdir /tmp/acatome-rescue
```

**(C) Restart watch** — a heavier hammer.  Only useful if you have a batch of 5+ rescues and want watch's progress log.  Send SIGTERM to the watch process (not SIGKILL; it finishes the current PDF first) and let the supervisor restart it.  The fresh session starts with empty `seen_hashes` and backfill will pick up everything in non-skip dirs.

### 5. Post-process: Fix Slugs (if needed)

If the sidecar `author` list used `"Given Surname"` instead of `"Surname, Given"`, the resulting slug will include the full first-author name.  Fix it in-place:

```sql
UPDATE refs
   SET slug='nasibulin2011multifunctional',
       authors='[{"name":"Nasibulin, Albert G."}, ...]'
 WHERE id=3484;

UPDATE papers
   SET bundle_path='/Users/bots/.acatome/papers/n/nasibulin2011multifunctional.acatome'
 WHERE ref_id=3484;
```

Then move the bundle + PDF on disk to the new first-letter subdir, and rewrite `header.slug` and `header.authors` inside the gzipped bundle JSON so future re-ingests stay consistent.

### 6. Fixing an Already-Ingested-With-Bad-Metadata Paper

If a paper is already in the store with a mangled slug (e.g. `haoranwangdepartmentofcomputer2025make`) and you want to reuse the existing extract, you have two options:

**(a) Surgical DB update** (fast, keeps existing blocks + embeddings):
```sql
UPDATE refs SET slug='wang2025make',
    doi='10.13140/RG.2.2.32726.36160',
    authors='[{"name":"Wang, Haoran"},{"name":"Shu, Kai"}]'
 WHERE id=<ref_id>;
```

**(b) Full re-ingest**: `DELETE FROM refs WHERE id=<ref_id>;` (cascades to Paper + blocks + citations + notes), delete the orphan `.acatome` and `.pdf` from `~/.acatome/papers/`, then run the normal rescue loop above.  Cleaner, but re-runs Marker + embeddings.

Use (a) when the block-level content is fine and only the header is wrong.  Use (b) when Marker's output is suspect.

## Anti-patterns

- **Don't set `verified: true` without actually checking the PDF matches the metadata.**  The gate exists for a reason — papers with mismatched DOIs have landed in production stores this way.
- **Don't use `acatome-extract repair` on these.**  `repair` targets `anon*` / `*untitled*` bundles and rescues metadata *from the block text*.  If the text is also garbage (thesis front-matter, journal boilerplate), it'll rescue the wrong title.  Sidecars beat rescue when you already know the answer.
- **Don't trust an existing `.acatome` in `errors/` via shared-bundle fast-path.**  If the lookup cascade found a wrong DOI/title (which is *why* Marker gave up), reusing that header propagates the error.  Always delete the stale bundle before re-triggering.
- **Don't ingest from `errors/`.**  Watch explicitly skips `completed/`, `errors/`, and `errors/duplicates/`.  Files must be moved *out* of those subdirs to be picked up by path (A), or staged outside the watch tree entirely for path (B).
- **Don't assume moving to `retry/` will re-process a previously-errored PDF.**  If the current watch session's backfill ever hashed the PDF, path (A) will loop-move it into `errors/duplicates/`.  Use path (B) for anything that's been through `errors/` once already.

## Verification

After a rescue run, confirm:

```sql
SELECT id, slug, doi, year, title,
       (SELECT count(*) FROM blocks b WHERE b.ref_id=r.id) AS blocks
  FROM refs r
 WHERE id IN (<new-ref-ids>);
```

Each row should have a plausible slug, the expected DOI, and blocks > 0.  The `paper-ingest/errors/` directory should be empty except for `duplicates/` and any `REVIEW.md`.

## Example Sessions

### Session 1 — cold errors/ batch (path A works)

```
errors/
  03 s41557-019-0290-1.pdf        ← hash-dup of liu2019kinetic.pdf
  FULLTEXT01.pdf                  ← Teghammar 2013 thesis (no DOI)
  liu2019kinetic.pdf              ← header fine, just failed title gate
  multifunctional-*.pdf           ← Nasibulin 2011 ACS Nano (10.1021/nn200338r)
  anon2025untitled.pdf            ← Wang 2025 RG DOI, already ingested w/ bad slug
  towards*20260421161808.pdf      ← DOI already in store as weng2019towards
```

None of these hashes were in `seen_hashes` (the errored PDFs were moved to `errors/` in a *previous* watch session).  Path (A) worked: sidecars written, PDFs moved to `paper-ingest/retry/`, watch picked them up.

Result:
- 2 to `errors/duplicates/` (hash-dup + DOI-already-in-store)
- 4 to `completed/` with correct slugs: `liu2019kinetic`, `nasibulin2011multifunctional`, `teghammar2013biogas`, `wang2025make`

### Session 2 — hot-backfill batch (path B required)

```
errors/
  380752.380786.pdf                          ← Klauck et al 2001, STOC
  interactioninquantumcommunicat2001all.pdf  ← byte-dup of above
  CRBIOL_2004__327_5_409_0.pdf               ← Eisenstein 2004, CR Biologies
  rpurcell2004stalking.pdf                   ← byte-dup of above (lookup matched wrong paper!)
```

The backfill at watch startup (same session) had already hashed these four PDFs when they lived in `paper-ingest/` root, so `seen_hashes` contained all four hashes.  Moving the canonical pair into `retry/` tripped hash-dedup → watch moved them into `errors/duplicates/` without re-extracting.

Switched to path (B): staged the canonical PDF + sidecar in `/tmp/acatome-rescue/`, ran `acatome-extract extract → enrich`, `acatome-store ingest`, then moved the PDF to `completed/`.

Result:
- ref 3503: `klauck2001interaction`, 268 blocks
- ref 3504: `eisenstein2004proteins`, 132 blocks
- Byte-dup pair remained in `errors/duplicates/` correctly

## Related Skills

- `skill:handle-dropped-pdf` — the user-facing path for new submissions (not rescue)
- `skill:find-paper` — check the store before assuming a DOI needs ingestion
- `skill:quest-disambiguate` — when the DOI/metadata itself is ambiguous
