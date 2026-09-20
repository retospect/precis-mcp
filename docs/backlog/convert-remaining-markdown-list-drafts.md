---
status: ready
title: Convert the remaining 20 drafts' markdown bullet paragraphs to structured lists
prio: normal
---

# Convert the remaining 20 drafts' markdown bullet paragraphs to structured lists

## Motivation / why

Reto, 2026-09-20: a PDF export of `norr-her-survey` rendered its nested
bullet lists as run-on prose with literal hyphens, and the Word export
flattened them the same way. Cause: the list lived as markdown `- ` text
inside `paragraph` chunks. Only the web reader renders that — the LaTeX
and docx exports build lists from `ulist`/`olist` containers with `item`
children (migration 0037), so bullet text reached them as one line.

`precis.draft.mdlist` now parses bullets at the write door, so *new*
prose can't land in the broken shape, and `precis convert-draft-lists`
backfills existing drafts. `norr-her-survey` was converted on 2026-09-20
(76 paragraphs → 1729 items; export verified: 205 balanced `itemize`,
1600 `List Bullet 2` + 129 `List Bullet`, 0 warnings). The rest of the
corpus was deliberately left for a separate pass.

## In scope

Run the existing backfill over the other drafts:

```
scripts/prod-precis convert-draft-lists            # dry-run, all drafts
scripts/prod-precis convert-draft-lists --commit
```

At the time of writing: **68 paragraphs across 20 drafts, ~958 items**.
Re-measure before running — the numbers move as drafts are written.

Dry-run first and read the per-chunk lines. A paragraph the parser
declines stays a paragraph, which is the safe outcome; a paragraph it
converts that was really prose is reversible with
`edit(kind='draft', id='dc<container>', list_kind='normal')`.

## Explicitly NOT in scope

- Changing the parser or its guards (two-bullet minimum, all-lines rule).
  If a draft in the tail needs a looser rule, file that separately —
  loosening it re-opens the false-positive class the guards close.
- Paper/patent ingest chunks. This is drafts only.
- Deciding per-draft whether a list *should* be a list. The parser's
  answer is the answer; a human override is `list_kind='normal'` after.

## Acceptance criteria

- `scripts/prod-precis convert-draft-lists` (dry-run, no `--draft`)
  reports 0 remaining candidates.
- Spot-check one converted draft's export end to end: `precis draft
  export <slug>` produces balanced nested `itemize`, and the item count
  in `main.tex` matches `SELECT count(*) … chunk_kind='item'` for that
  ref.
- Any paragraph the operator judged wrongly converted is dissolved back
  with `list_kind='normal'`, and noted here before the file is deleted.

## Target + blast radius

`precis.cli.convert_draft_lists` (no code change expected — this is a
run, not a build), prod `chunks` rows for the affected drafts, and the
embedding/summary cascade those rows trigger. Each converted paragraph
fans out to one container plus one item per bullet, so the cascade
re-runs over roughly 14× the chunk count; expect a window where search
over a just-converted draft is patchy.

## Open questions / decisions log

- 2026-09-20: scoped to `norr-her-survey` first on Reto's call, to verify
  the export before touching the rest. Verified; the tail is this item.
