---
name: quest-disambiguate
description: >
  Act on a quest in `needs_user` status.  The resolver found multiple
  candidates, or flagged a misconception (DOI-title mismatch, retraction,
  duplicate-of).  This skill walks through confirm / repoint / flag /
  cancel decisions and the rules for each.
user-invocable: true
allowed-tools: [get, put]
applies-to: [quest]
tags: [papers, disambiguation, workflow]
---

## When to Use

- A `/needs-user` notification appeared at session start
- A `put(type='quest', ...)` call just returned `status: needs_user`
- An error or hint points you at `skill:quest-disambiguate`

## Get the current state

```
get(id='quest:<short-uuid>')              # the card
get(id='quest:<short-uuid>/candidates')   # disambiguation options (if any)
get(id='quest:<short-uuid>/misconceptions')  # flags
```

## The four decisions

### 1. Confirm one of the candidates

When the resolver returns `N > 1` candidates and you can tell which is correct (e.g. from title, authors, DOI prefix, or matching the user's citation context):

```
put(id='quest:<short-uuid>', mode='confirm', choice=<n>)
```

`<n>` is the 0-indexed position in the candidate list.  The request moves to `queued` with the picked ref.

**Don't guess.**  If all candidates look plausible and you can't verify, ask the user.  A wrong `confirm` silently fetches the wrong paper and the mismatch only surfaces at reconciliation time (sometimes never).

### 2. Repoint to a corrected DOI

When the input DOI was wrong (typo, mismatched with title, retracted paper) but you know the *right* one:

```
put(id='quest:<short-uuid>', mode='repoint', doi='10.x/corrected')
```

Triggers re-resolution.  The new DOI replaces the old; the request status flips back to `queued` or back to `needs_user` if the new DOI also has problems.

### 3. Flag a misconception

When you want to attach a finding without changing the request's fate — e.g. "this paper was retracted" or "this is a preprint of a later journal version":

```
put(id='quest:<short-uuid>', mode='flag', code='retracted',
    evidence='Retraction Watch, 2024-06-12, fabricated data')
```

Valid codes and their meanings:

| Code | Severity | When to use |
|---|---|---|
| `doi_invalid` | major | DOI doesn't resolve to anything |
| `doi_truncated` | major | DOI looks cut off (e.g. ends in `10.1021`) |
| `doi_title_mismatch` | **critical** | The DOI resolves, but the title doesn't match the input title |
| `title_not_found` | **critical** | Title-only submission returned no candidates |
| `duplicate_of` | minor | We already have this paper under a different request/slug |
| `retracted` | **critical** | Paper has been retracted (cite with a retraction notice or don't cite) |
| `preprint_of` | info | This is an arXiv preprint; prefer the journal version |
| `pdf_mismatch` | **critical** | User-dropped PDF turned out to be the wrong paper |

Flags are *accumulative* — a request can carry several.  Severity drives the colour coding in `/misconceptions` views.

### 4. Cancel

When the paper can't be resolved and isn't worth further effort:

```
put(id='quest:<short-uuid>', mode='cancel')
```

Terminal.  Prefer `flag` over `cancel` when the reason might matter to future reviewers.

## Misconception-driven playbook

- **`doi_invalid` / `doi_truncated`**: ask the user for the full DOI, or `repoint` if you can find it from context.
- **`doi_title_mismatch`** *(critical)*: almost always the DOI is correct and the title was wrong (or vice versa).  Trust the DOI; `repoint` with the matching DOI if you can find it, else `flag` and ask the user.
- **`title_not_found`** *(critical)*: resolver saw zero hits on the title.  Try a shorter query (drop subtitle), different authors, or `cancel` if the paper doesn't exist.
- **`retracted`** *(critical)*: don't use the paper.  `flag` so the retraction is on-record, then either `cancel` the request or proceed knowing it's a retraction-notice citation only.
- **`duplicate_of`**: link the new request to the existing slug; `cancel` the duplicate with a note pointing to the original.
- **`preprint_of`**: usually fine to keep the preprint, but add a pointer to the published version if the user will cite in a paper.
- **`pdf_mismatch`**: the runner attached a PDF but reconciliation found it was the wrong paper.  User dropped a stale/wrong file; ask for the correct one.

## Surface the outcome

After acting on a `needs_user` quest, re-fetch the card and tell the user what changed:

```
get(id='quest:<short-uuid>')
```

One-line summary: `"confirmed candidate 2 → status queued"` or `"flagged as retracted; not proceeding"`.

## Related skills

- `skill:find-paper` — how the quest was created in the first place
- `skill:handle-dropped-pdf` — when the user attaches a PDF in response
