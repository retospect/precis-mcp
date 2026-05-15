---
id: precis-doi-resolution
title: precis — collapse raw DOIs in text to paper slugs
status: active
tier: 2
floor: any
applies-to: cross-cutting (markdown / plaintext / tex; out-of-root dirs too)
last-updated: 2026-05-10
---

# precis-doi-resolution — collapse raw DOIs in text to paper slugs

When you ingest external prose (Perplexity reports, research dumps,
LLM chat exports, manuscript drafts, bibliography rough-cuts, …)
it usually contains DOIs in their raw form — `10.1093/sleep/zsag065`
or `https://doi.org/10.1038/s41531-025-01018-8`. Every DOI that
precis already knows can be replaced *in place* with its paper slug
in square brackets — `[lee2026could]`, `[nepozitek2025glymphatic]` —
giving you a citation-friendly, search-friendly, bib-generation-friendly
form. Unknown DOIs are left untouched (best-effort).

This is a destructive rewrite of the text on disk. It's idempotent
(re-running is a no-op since the DOIs are gone), but the original
DOI strings are not preserved unless you keep a backup outside the
target directory.

## When to run it

Run it **once per directory** of freshly-ingested prose, before any
downstream tool (LaTeX bib generation, agent prompts, search) touches
the files. Typical triggers:

- A new sub-directory under `content/.../...` got populated with
  Perplexity / Sonar-deep-research reports.
- You just landed a docx / markdown manuscript that cites by DOI.
- An agent wrote a research summary to `$PAPERS_REPORTS` and you
  want the slug form for the next pipeline stage.

## Workflow surface

### Today: workspace-side script

The current implementation lives in the workspace, *not* in the MCP
verb surface. From `pips/packages/precis-mcp/`:

```sh
# default: cwd, recurse, rewrite *.md and *.txt in place
./scripts/doilist convert-doi-to-slugs

# explicit dir + dry-run (counts only, no writes)
./scripts/doilist convert-doi-to-slugs --dry-run path/to/dir

# also rewrite .tex sources
./scripts/doilist convert-doi-to-slugs --ext md --ext txt --ext tex .
```

Output reports `replaced/seen` per file plus a final tally. Best to
run `--dry-run` first on unfamiliar directories — the bulk SQL-backed
slug map runs in well under a second, so iterating is cheap.

The script lives at
`pips/packages/precis-mcp/scripts/_doilist.py`; documented in
`pips/packages/precis-mcp/scripts/README.md` under
`### doilist → #### doilist convert-doi-to-slugs`.

### Tomorrow: precis edit mode (planned)

The intended long-term home is an `edit` mode on the file kinds:

```python
edit(kind='markdown',  id='content/foo/bar.md', mode='resolve-dois')
edit(kind='plaintext', id='notes/dump.txt',     mode='resolve-dois')
edit(kind='tex',       id='paper.tex',          mode='resolve-dois')
```

Once shipped, that's the agent-native call. **Until then**, prefer
the workspace script — it works on directories outside `PRECIS_ROOT`
(e.g. `content/techreport/...`) which the file kinds can't reach.

## What gets matched and replaced

- **Bare DOI** `10.x/y` → `[slug]`
- **URL-form DOI** `https://doi.org/10.x/y` (or `dx.doi.org`,
  `http://`) → `[slug]` (the URL prefix is consumed too — no broken
  links left behind).
- **arXiv DOI form** `10.48550/arXiv.<id>` → same slug as the bare
  arXiv id (resolved via the `ref_identifiers` cross-scheme alias
  index).

Trailing markdown formatting and a single sentence-punctuation char
are preserved on output:

| Input                             | Output                |
|-----------------------------------|-----------------------|
| `**DOI: 10.x/y**`                 | `**DOI: [slug]**`     |
| `See 10.x/y.`                     | `See [slug].`         |
| `Footnote 10.x/y[^9].`            | `Footnote [slug][^9].`|
| `In a list (10.x/y).`             | `In a list ([slug]).` |
| `` `10.x/y` ``                    | `` `[slug]` ``        |

URL-suffix junk is dropped (`.full`, `?utm=…`, `…`, `..`).

Unknown DOIs (not yet ingested in precis) are left **exactly** as-is.

## What it does *not* do

- It does **not** auto-ingest unknown DOIs. Pair with `doilist scan`
  (which writes a `dois_to_get.md` queue and optionally fetches OA
  PDFs via Unpaywall) when you want missing DOIs added to the
  corpus.
- It does **not** create `cites` links between the rewritten document
  and the cited papers. That's a separate workflow (see option D in
  `docs/future-integrations.md` if it lands).
- It does **not** rewrite bibtex (`.bib`) or LaTeX-style `\cite{...}`
  references — only DOI strings in prose.

## Related

- `precis-paper-help` — paper slugs are the canonical paper ID;
  every `get(kind='paper', id='10.x/y')` already does the same DOI →
  slug resolution at lookup time, so once a document carries `[slug]`
  forms the agent never needs to round-trip through a DOI again.
- `precis-files-help` — file-kind addressing and the `PRECIS_ROOT`
  boundary.
