---
name: find-paper
description: >
  Acquire a scientific paper given a DOI, arXiv id, or title.  Use when
  a user asks "can you get this paper", drops a citation, mentions a
  DOI, or when you encounter a `[@key]` reference that isn't in the
  precis-papers library yet.
user-invocable: true
argument-hint: [doi, arxiv-id, title]
allowed-tools: [get, put, search]
applies-to: [quest, paper]
kind-onboarding: quest
tags: [papers, research, workflow]
---

## When to Use

- User drops a DOI: "can you get 10.1021/jacs.2c01234"
- User drops an arXiv id or URL: "check arxiv:2301.12345"
- User pastes a citation: "Smith et al., JACS 146 (2024) 12345"
- You encounter `\cite{smith2024jacs}` or `[@smith2024jacs]` while editing a `.tex` / `.md` doc and the bib entry is undefined
- You need to quote a paper in a response and don't yet have it

## The three-step loop

1. **Check precis-papers first** — *always*.  Never submit a quest for a paper we already have.

   ```
   search(type='paper', query='<authors> <year> <keyword>')
   ```

   or if you already have a DOI:

   ```
   get(type='paper', id='doi:10.x/y')
   ```

   A hit means `status: ingested`, `found_in_store`, or an acatome slug you can cite directly.

2. **If absent, submit a quest.** (Phase 12b; read-only in 12a — write submits go through the CLI for now: `acatome-quest submit --doi 10.x/y`.)

   ```
   put(type='quest', text='10.1021/jacs.2c01234')
   ```

   Or with richer context:

   ```
   put(type='quest', ref={doi: '10.x/y', title: '…'}, source={document: 'ch02.tex', line: 147})
   ```

   The handler returns the full request card with a status:
   - `queued` → runner will fetch it; come back in a few minutes
   - `found_in_store` → acatome already had it; use the resolved slug
   - `needs_user` → disambiguation or bad DOI; see `skill:quest-disambiguate`

3. **Track outcome.**

   ```
   get(id='quest:<short-uuid>')
   ```

   Until `status: ingested`, **do not fabricate quotes from title or abstract.** Cite the DOI only; wait for the PDF to land.

## Anti-patterns

- **Don't submit twice.** `put(type='quest', ...)` is idempotent on DOI for open requests, but submitting the same DOI from different agents floods the resolver and hits per-agent rate limits (default 50 open).
- **Don't cite before ingestion.** A `needs_user` or `fetching` request has resolved metadata but no body text.  Citing the DOI is honest; fabricating "as Smith et al. argue" is not.
- **Don't treat title-only submissions as exact.** Resolver returns *candidates*; pick explicitly via `put(mode='confirm', choice=<n>)` rather than assuming candidate 0.

## Handling the three submission shapes

**DOI (highest confidence):**
```
put(type='quest', text='10.1021/jacs.2c01234')
```

**arXiv id:**
```
put(type='quest', text='arxiv:2301.12345')
```

**Title + authors (lowest confidence, often returns candidates):**
```
put(type='quest', ref={title: 'Metal-organic frameworks for...', authors: ['Smith'], year: 2024})
```
→ likely lands in `needs_user` with 2–5 candidates; proceed to `skill:quest-disambiguate`.

**Free-form citation string:**
```
put(type='quest', ref={raw: 'Smith, J. et al. JACS 146 (2024) 12345-12350. doi:10.x/y'})
```
→ resolver extracts DOI from the `raw` field.

## How to surface status to the user

Don't dump the raw quest card unless asked.  Summarise:

- `ingested` / `found_in_store`: "got it — see paper:smith2024jacs" (and then use the slug)
- `queued` / `fetching`: "fetching paper:<likely-slug> now — try again in ~2min"
- `needs_user`: "that paper needs disambiguation: `<N>` candidates.  Want me to walk through them?"
- `failed` / `extract_failed`: "couldn't retrieve — OA not available.  Want to try a different source or drop a PDF?"

## Related skills

- `skill:quest-disambiguate` — when status is `needs_user`
- `skill:handle-dropped-pdf` — user pastes a PDF URL or attaches one
