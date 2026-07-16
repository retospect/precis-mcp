# Markup-first ingest — JATS/LaTeX/HTML before PDF+OCR

Status: draft
Owner: reto

## Problem

Every paper enters the corpus through Marker OCR on a PDF
(`src/precis/ingest/pipeline.py::extract_paper`), even when the
publisher serves structured full text (JATS XML, LaTeX source,
publisher HTML) over a free or already-keyed API. Marker is the
expensive, lossy leg: GPU minutes per paper, heuristic section
detection (`_retag_references`), mojibake repair, caption matching,
page-assignment guessing. JATS hands us every one of those facts
explicitly — `<sec>`, `<title>`, `<p>`, `<table-wrap>`, `<fig>`,
`<disp-formula>`, `<ref-list>` — for free.

## Goals

- When structured markup is available via API, use it as the **body
  chunk source** instead of Marker output.
- Still acquire the PDF as the **printable** artefact: `pdf_sha256`
  identity, `pdf_storage_path`, page ranges — but skip OCR entirely
  when markup already produced the chunks.
- No schema change. No behaviour change for markup-less papers.

## Non-goals

- Upgrading already-PDF-ingested refs to markup chunks. Body chunks
  are append-only (AGENTS.md §Don'ts); a supersede/re-ingest story is
  a separate ADR. Markup-first applies to **new ingests and stubs**
  (`pdf_sha256 IS NULL`) only.
- Parsing publisher HTML in v1. JATS + LaTeX cover the bulk; HTML is
  a follow-up leg behind the same producer interface.
- Rendering our own printable from markup. The publisher PDF is the
  printable; if no PDF exists (rare for markup hits), the ref simply
  keeps `pdf_sha256 = NULL` as today.

## Provider matrix (markup legs)

Ordered like the existing PDF cascade in
`src/precis/workers/fetch_oa.py`: deterministic/keyed first,
aggregators after.

| # | Leg | Format | Auth | Route |
|---|-----|--------|------|-------|
| 1 | PLOS pattern | JATS | none | `journals.plos.org/plosone/article/file?id={DOI}&type=manuscript` — deterministic, joins the existing publisher-pattern leg |
| 2 | Elsevier | Elsevier DTD XML | `PRECIS_ELSEVIER_API_KEY` | same Article Retrieval call as today with `httpAccept: text/xml` |
| 3 | Springer Nature | JATS/A++ | `PRECIS_SPRINGER_API_KEY` (new, optional) | OA full-text XML API |
| 4 | Europe PMC | JATS | none | DOI→PMCID (leg already resolves this), then `/{PMCID}/fullTextXML`. Biggest single win — whole PMC OA subset |
| 5 | Crossref TDM | varies (XML) | polite `mailto` | `works.link[]` entries with `content-type` in (`application/xml`, `text/xml`) — we already fetch the works record |
| 6 | arXiv HTML | LaTeXML HTML | none | `arxiv.org/html/{id}` — official arXiv HTML (2023+, LaTeXML-rendered, ~75% error-free rising to 90%). Structured `<section>`/`<figure>`/MathML. **Preferred arXiv route** |
| 7 | arXiv source | LaTeX | none | `arxiv.org/e-print/{id}` tarball — fallback when no HTML rendering exists (older papers). Flatten-and-chunk (see §2) |

S2 `externalIds` already lands `PubMedCentralID` at ingest
(`src/precis/ingest/semantic_scholar.py::_normalize`), so leg 4 is
often a single GET.

**arXiv preference order** (confirmed against arXiv help docs, Jul
2026): `arxiv.org/html` (official LaTeXML — *not* the third-party
ar5iv frontend, which is outside arXiv governance; ar5iv is a
last-ditch fallback for pre-2023 papers only) → raw `.tex` tarball →
PDF. The PDF (`arxiv.org/pdf/{id}.pdf`, the existing `_try_arxiv`
leg) is always fetched as the printable regardless of which markup
leg won.

## Design

### 1. `fetch_oa`: markup cascade before PDF cascade

Two passes per stub, one shared event trail:

1. **Markup pass** — legs above, stop on first hit. Emit
   `fetch_ok` with `payload.format ∈ {jats, elsevier_xml, arxiv_html,
   latex}` and `source='fetcher:<leg>_xml'`. Gated by
   `PRECIS_FETCH_MARKUP` (default-off until the backlog is exercised).
2. **PDF pass** — the existing ten-leg cascade, run **regardless of
   the markup outcome**:
   - markup hit → PDF is fetched as the printable companion;
   - markup miss → PDF is the chunk source exactly as today.

On a markup hit the worker drops a **bundle** into the watch inbox:

```
inbox/papers/<...>/foo.jats.xml                     # chunk source
inbox/papers/<...>/foo.pdf                          # printable, when the PDF pass hit
inbox/papers/<...>/foo.jats.xml.precis-fetch.json   # existing sidecar, extended
```

**Sidecar: extend the existing `FetchSidecar`, do not invent a new
file.** `src/precis/ingest/fetch_sidecar.py` already writes
`<file>.precis-fetch.json` carrying `ref_id` + `identifiers`
(incl. DOI) + `source`, and the watcher already reads it into
`fold_ref_id` (`cli/watch.py::process_pdf`). We add two fields to the
`FetchSidecar` dataclass + JSON payload:

- `source_format`: `"jats" | "elsevier_xml" | "latex" | "pdf"` — tells
  the watcher which producer to build (`MarkupInput` vs `PdfInput`).
- `companion_pdf`: filename of the same-stem printable when the PDF
  pass also hit, else absent.

The sidecar is written **next to the trigger file** (the markup file
when markup won; the PDF otherwise). The DOI lives in `identifiers`
already, so identity never depends on parsing the markup header, and
`fold_ref_id` folds into the right stub exactly as on the PDF path.
`read_sidecar` gains the two fields with back-compat defaults
(`source_format="pdf"`, `companion_pdf=None`) so existing PDF-only
sidecars decode unchanged. Miss/failure vocabulary (`no_oa_version`,
`fetch_failed`, …) is reused verbatim per leg.

### 2. New producer: `extract_paper_from_markup`

New module `src/precis/ingest/markup.py`, sibling of
`extract_paper`:

```
extract_paper_from_markup(
    markup_path: Path,
    *,
    fmt: Literal["jats", "elsevier_xml", "arxiv_html", "latex"],
    pdf_path: Path | None = None,
    sidecar: FetchSidecar | None = None,
) -> PaperToWrite
```

One chunk per source element, mirroring `_blocks_to_chunks`. Prose is
size-bounded with `text_chunker.split_text`, tables with `split_table`,
and everything runs through `enforce_hard_max` (the same utilities the
Marker path uses) so no chunk exceeds the embedder ceiling. Structural
units (caption / equation / figure / heading) stay whole (hard-capped
only). `ord` starts at 0 and increments per emitted chunk; cards use
`_build_cards` unchanged.

- **JATS** (lxml, already a dev extra): `<front>` → title / authors /
  abstract / ids; `<body>//<sec>` walk → real `section_path`;
  element→`chunk_kind` map is mechanical: `<p>`→`paragraph`,
  `<table-wrap>`→`table`, `<fig>`+`<caption>`→`figure`/`caption`,
  `<disp-formula>`→`equation`, `<ref-list>`→`references`. No
  `_retag_references`, no mojibake repair, no page-assignment
  heuristics.
- **arXiv HTML / ar5iv** (LaTeXML output): same element walk as JATS
  against the HTML tree — `<section>`→section_path, `<figure>` +
  `<figcaption>`→figure/caption, MathML `<math>`→equation, the
  bibliography `<ul class="ltx_biblist">`→references. Structurally
  JATS-class, so it shares the JATS mapper with a thin selector
  adapter.
- **Elsevier XML**: thin adapter mapping their DTD onto the same
  intermediate before the shared mapper.
- **LaTeX — flatten-and-chunk, no structural parser.** We do *not*
  reconstruct floats/tables/captions. Confirmed against arXiv
  conventions:
  - **Main file**: honor a `00README`/`00README.XXX` `toplevelfile`
    directive if present; else the `.tex` file(s) containing
    `\documentclass` (arXiv's own heuristic). Multiple top-level
    files concatenate alphanumerically.
  - **Follow `\input`/`\include`** from the main file (resolved from
    the tarball root, per arXiv's root-compilation rule) to assemble
    the document in rough reading order.
  - **Skip `anc/`** entirely — arXiv's ancillary dir is not article
    text and arXiv itself does not index it. Skip build cruft
    (`.aux`/`.log`/`.toc`/…).
  - **References come pre-separated as `.bbl`** (arXiv historically
    does not run BibTeX; authors bundle `foo.bbl` ↔ `foo.tex`). Emit
    the `.bbl` (or `thebibliography` env if inlined) as
    `chunk_kind='references'` — the one classification that matters,
    for free. `.ind`/`.gls`/`.nls` arrive pre-rendered too.
  - **Strip** comments (`%`), preamble, and non-rendering blocks
    (`\iffalse`…). Track `\section`/`\subsection` while streaming for
    `section_path`. Native LaTeX math is kept as text (good for
    embeddings — no OCR mangling).
  - **Macro-density gate**: if stripped text is dominated by
    unexpanded backslash-macro tokens (> threshold), bail to OCR on
    the companion PDF. This is the *only* tex→OCR fallback trigger;
    flatten-and-chunk otherwise cannot fail to "parse" (worst case:
    noisy text, which the embedder tolerates).
- **Numerics / cards**: reuse `extract_numerics`, `_build_cards`,
  `make_content_hash` from the existing pipeline unchanged.
- **Printable attach**: when `pdf_path` is given, read bytes, compute
  `make_pdf_sha256`, set `pdf_sha256` / `pdf_size_bytes` /
  `pdf_storage_path` / `pdf_role='main'` on the same `PaperToWrite`.
  **Marker is never invoked.** `pdf_pages_*` come from a cheap fitz
  page count (no text extraction). `pdf_role` keeps its sealed CHECK
  vocabulary — the printable *is* the main PDF; what changed is the
  chunk source, recorded in meta (below).

Parse failure at any point → log at ERROR with the ref identity +
format + cause, then fall back to `extract_paper` on the companion
PDF (or leave the stub for the next PDF-only pass). Markup-first must
never lose a paper we could have OCR'd.

### 3. Provenance

`refs.meta` gains two keys, set by the producer:

- `source_format`: `"jats" | "elsevier_xml" | "arxiv_html" | "latex"
  | "pdf"` (`"pdf"` implied/absent for the legacy path).
- `markup_source_url`: the URL the markup came from (audit).

No migration; `meta` is already JSONB.

### 4. Watcher routing

The watcher is PDF-hardwired today (`_is_pdf`, `backfill` globs
`*.pdf`, `process_pdf(pdf)`, the batch subprocess). Generalize
**without forking the pipeline** — `precis_add` already dispatches on
the input dataclass, so the watcher only builds the right input:

- **`_is_pdf` → `_is_ingestable`**: recognize `.pdf`, `.xml`, `.tex`,
  `.tar.gz` (sidecars end in `.json`, still excluded).
- **`backfill`**: glob the ingestable-suffix set, not just `*.pdf`.
- **`process_pdf` → `process_file`**: after settle, a classifier
  builds `MarkupInput` (markup suffix, or sidecar `source_format !=
  pdf`) or `PdfInput` (else). The batch subprocess generalizes the
  same way.
- **Same-stem-sibling rule (the bundle interlock, existence not
  timing)**: a `.pdf` that has a same-stem markup sibling is
  **skipped as a standalone trigger** — the markup path consumes it
  as `companion_pdf`. A lone `.pdf` ingests exactly as today. This
  avoids the double-ingest race with no new debounce logic.

`tagging/` segment semantics apply identically. New `MarkupInput`
dataclass in `add.py` carries `markup_path`, `fmt`, `companion_pdf`,
`extra_tags`, `fold_ref_id`, `as_kind`.

**Claim key (multi-host interlock).** `precis_add`/`process_pdf`
claim on `pdf_sha256` today. Generalize to **claim on the
chunk-source hash**: `markup_sha256` (sha256 of the markup bytes,
computed like `make_pdf_sha256`) when markup is the body source,
`pdf_sha256` otherwise. `Claim` already accepts any hex string
(`claim._key_for` reads the leading 16 hex chars), so this needs
**no change to `Claim`** — only which hash the `MarkupInput` arm
passes. The sidecar `ref_id` is *not* the claim key (it would
serialize unrelated hosts and is absent for manual drops).

### 5. `db_writer`: attach-only upgrade guard

Today `register_aliases_and_maybe_upgrade` upgrades a
`pdf_sha256 IS NULL` ref by setting the hash **and writing the
pipeline's chunks**. Two cases now exist for an identifier hit:

- ref has **no body chunks** (metadata-only stub) → current
  behaviour, unchanged.
- ref **has body chunks** (markup-ingested) and a PDF arrives later →
  **attach-only**: set `pdf_sha256` / `pdf_pages` / `pdf_role`, write
  **no chunks**. Guard: `EXISTS (SELECT 1 FROM chunks WHERE ref_id =
  … AND ord >= 0)`, checked in the existing stub-upgrade branch of
  `register_aliases_and_maybe_upgrade` right before the chunk insert.
  This keeps append-only intact and makes a re-dropped publisher PDF
  a cheap printable/metadata attach instead of a duplicate Marker
  run. Policy encoded: **markup chunks win over Marker chunks**; a
  manual override is a later supersede ADR.
- **PDF-first edge**: if the PDF genuinely arrives before the markup
  (odd manual drop), the PDF's Marker chunks are the body and the
  later markup is a no-op — same append-only punt.

### 6. Idempotency

- Re-running the markup ingest: DOI in the sidecar →
  `ref_identifiers` probe hits → `inserted=False`, as today.
- `content_hash` over the markup-derived body text detects
  same-paper-different-format collisions.
- PDF dropped before its markup twin: PDF path wins (chunks are
  Marker's), later markup arrival is a no-op per the append-only
  non-goal. Acceptable: the fetch worker controls ordering for the
  stub population, and it always drops markup first.

## Testing

- Fixture JATS (small OA paper) + fixture LaTeX under
  `tests/fixtures/markup/`; golden `ChunkToWrite` assertions for
  section_path, chunk_kind mapping, references tagging, numerics.
- `fetch_oa` markup-leg tests mirror the existing leg tests
  (respx/httpx mock): hit, miss, malformed-XML fallback ordering.
- Watcher routing test: bundle drop → one ref, chunks from XML,
  `pdf_sha256` set, `meta.source_format='jats'`, Marker never called
  (assert via monkeypatched `extract_blocks_marker`).
- Attach-only test: markup-ingested ref + later PDF drop → hash set,
  chunk count unchanged.
- Full suite in the dev container (`scripts/dev pytest`).

## Open questions

- ~~Springer key: worth requesting now?~~ Resolved: operator provides
  a key (portal: https://dev.springernature.com, Open Access API,
  `/openaccess/jats?q=doi:{DOI}`). Leg 3 lands in v1; the leg is a
  silent no-op when `PRECIS_SPRINGER_API_KEY` is unset, same pattern
  as the Elsevier/Wiley PDF legs.
- ~~LaTeX in v1 or v1.1?~~ Resolved: **arXiv HTML in v1** (JATS-class,
  cheap); **raw `.tex` via flatten-and-chunk in v1** too (§2) since
  the `.bbl`/`anc/` conventions make it robust and it cannot fail to
  parse; heavier structural parsing is explicitly out of scope.
- Should `search`/`get` surface `source_format` in paper views so the
  operator can see which refs got the good path? (Cheap; probably
  yes, as part of the meta rendering that already exists.) **Lean:
  yes**, in the meta block.

## Rollout

1. Producer + fixtures (pure, no network) — `markup.py`, tests.
2. Watcher routing + attach-only guard in `db_writer`.
3. `fetch_oa` markup legs, feature-flagged
   (`PRECIS_FETCH_MARKUP=1` initially, default-on once the stub
   backlog has been exercised).
4. ADR documenting the append-only punt for existing refs.
