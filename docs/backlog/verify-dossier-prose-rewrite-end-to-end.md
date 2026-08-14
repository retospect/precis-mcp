# Verify the dossier prose rewrite end-to-end (deployed, never observed)

> Delete this file once the check below passes. It exists only because the fix
> shipped and deployed without anyone reading a dossier written by the new code.

The "dossier writes draft chunks not markdown" fix is live fleet-wide
(deployed 2026-08-14, verified at melchior's venv `direct_url.json`). Dossier
202546's 23 stranded chunks are retired and the one-body-chunk contract is
restored. **What has never happened is a tick landing on the new code**, so the
user-visible complaint — raw markdown in the rendered draft — is unconfirmed
either way.

## Why it didn't get verified

Quest 202469's narrative chunk `2856653` (ord 6) was last written **2026-08-14
00:32 UTC**, 51 minutes before the fix commit existed.

An earlier version of this file blamed cluster contention. That was wrong, and
worth recording because the correction points at a second defect. Quest 202469
*does* tick — it wrote 30 chunks to this dossier at 07:40 on 2026-08-14, under
the deployed fix. What it has not done since 00:32 is re-run the **narrative
rewrite** specifically, nor written a `quest_log` entry (its three sibling
quests — 202467, 202468, 164903 — log every couple of hours, so this is
quest-specific, not a dead lane).

So the tick reaches the dossier and stops short of the prose step. Why is
unknown; it is the thing to chase if the check below stays unrunnable.

## The check

```sql
SELECT max(ts) FROM chunk_events WHERE chunk_id = 2856653;
```

Anything after `2026-08-14 00:32` is new-code output. Then read the text:

```sql
SELECT text FROM chunks WHERE chunk_id = 2856653;
```

Three things decide it:

1. **No block markdown in prose** — no literal `##` / `###` headings, no `-` or
   `*` bullet lines, no fences or pipe tables. These have no renderer by design;
   the inline subset (`**bold**`, `*italic*`, backticks, `$…$` KaTeX) is fine.
2. **Structure refs linkify** — `[st201901]` in square brackets, not `(st…)` in
   parentheses. Parentheses do not linkify, so a parenthesised ref renders as
   dead text.
3. **The ledger survived** — chunks 2904088–2904117 are still present and were
   not flattened back into the narrative by the rewrite. Note these are *not*
   pinned, contrary to what this file claimed before: they are plain
   `chunk_kind='paragraph'` rows with empty `meta`, which is its own defect —
   see `docs/backlog/dossier-ledger-nodes-land-as-unpinned-body-chunks.md`. If
   that one is fixed first, re-derive the id range before running this check.

The rendered page is
`https://melchior.tailded4cf.ts.net/smartdraft/quest-202469-dossier`; reading
the chunk text directly is the more reliable check.

If any of the three fails, the defect is in `precis/quest/dossier.py`'s rewrite
prompt/gate, not in the draft renderer — the renderer's behaviour here is
intended.

## Related

- `docs/backlog/dossier-present-tense-refinement.md` — the unbuilt redesign this
  fix is a precondition for.
- `docs/backlog/quest-compute-lane-runs-on-a-literature-only-quest.md` — found
  while waiting for this tick.
