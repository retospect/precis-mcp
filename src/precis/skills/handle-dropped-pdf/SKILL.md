---
name: handle-dropped-pdf
description: >
  Handle a PDF the user has provided directly — a Discord attachment, a
  URL, or a file path.  Attach it to an existing quest (preferred) or
  create a new request with the PDF pre-loaded.
user-invocable: true
allowed-tools: [get, put]
applies-to: [quest]
tags: [papers, ingestion, workflow]
---

## When to Use

- User says "here's the PDF" and drops a Discord attachment, a URL, or a file path
- A `/failed` quest could be rescued if the user can provide the PDF directly
- You've tried `skill:find-paper` and the runner can't fetch (not OA, paywalled, or OA-source rate-limited)

## Two code paths

**URL path (agent-facing)** — when the user drops an HTTP(S) URL:

```
put(id='quest:<short-uuid>', mode='file', url='https://...')
```

or, to create a new request with the PDF attached:

```
put(type='quest', mode='file', url='https://...',
    ref={doi: '10.x/y'})
```

**File path (CLI-only)** — when the user has the file on disk:

The agent *does not pass base64 or file bytes through MCP.*  Ask the user to run:

```
acatome-quest submit-file --path /local/path/to/paper.pdf \
                          --request-id <short-uuid>
```

or to create a new request:

```
acatome-quest submit-file --path /local/path/to/paper.pdf \
                          --doi 10.x/y
```

The file-path CLI route was kept out of MCP on purpose — base64 over MCP is a 33% size tax for no benefit, and filesystem access is already the user's to give.

## Validation (what the handler enforces for you)

The runner validates before accepting:

- **Magic bytes**: must start with `%PDF-`.  HTML error pages (common when a Discord CDN URL has expired) are rejected with a friendly error asking for a fresh link.
- **Size cap**: default 50 MB (override via `QUEST_MAX_PDF_SIZE` env var on the runner side).
- **Fresh request status**: refuses `ingested`, `found_in_store`, `cancelled`.  Accepts `failed`, `extract_failed`, `needs_user`, `queued`.

## Expected outcomes

After `put(mode='file', ...)` lands:

- Status flips to `ingesting` immediately
- Runner writes the PDF to the extractor's inbox
- `acatome-extract` chews on it (seconds to minutes)
- Reconciler closes the request when the extracted paper's DOI matches

**If the DOI matches the attached PDF** → status `ingested`; the paper is now available via `get(type='paper', id='<slug>')`.

**If the DOI doesn't match** → reconciler attaches a `pdf_mismatch` misconception instead of closing.  Means the user dropped the wrong file; go to `skill:quest-disambiguate`.

## Handling common Discord failures

- **Short-lived CDN URLs**: Discord attachment URLs expire (usually after 24h).  A rejected URL with `not a PDF (missing %PDF- magic bytes)` often means the user reshared an old link; ask for a fresh upload.
- **403 Forbidden**: the CDN signed URL is stale.  Ask for a re-attach.
- **SSL error / timeout**: Discord's CDN occasionally flakes.  Retry once; if it fails again, ask for the file path instead.

## Rules

- **One file per request.**  A second `put(mode='file')` on the same request replaces the previous PDF (only meaningful when the first one failed validation or turned out to be wrong).
- **Don't invent a DOI.**  If the user drops a PDF without a DOI and you can't extract one from the first page, let the extractor do DOI discovery — don't guess.
- **Surface the outcome, then stop.**  "Ingesting paper from Discord upload — should appear as `paper:<likely-slug>` in a minute or two" is enough.  Don't poll the status view in a loop; the user can check.

## Related skills

- `skill:find-paper` — when the user hasn't dropped a PDF yet
- `skill:quest-disambiguate` — when the PDF caused a `pdf_mismatch` flag
