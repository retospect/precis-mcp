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

Quest 202469's narrative chunk `2856653` was last written **2026-08-14 00:32
UTC**, 51 minutes before the fix commit existed. Every tick since has been
queued behind cluster load; as of 19:31 all four quest loops sat `queued` with
none running, spark being saturated with `autocatpath_seed`/`_aggregate` work.
Nothing is broken — it is contention.

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
3. **The ledger survived** — the pinned `ledger-node` chunks (2904088–2904117 on
   this dossier) are still present and were not flattened back into the
   narrative by the rewrite.

The rendered page is
`https://melchior.tailded4cf.ts.net/smartdraft/quest-202469-dossier`; reading
the chunk text directly is the more reliable check.

If any of the three fails, the defect is in `precis/quest/dossier.py`'s rewrite
prompt/gate, not in the draft renderer — the renderer's behaviour here is
intended.

## Related

- `docs/backlog/dossier-present-tense-refinement.md` — the unbuilt redesign this
  fix is a precondition for.
- `docs/backlog/quest-candidate-proposals-die-on-slab-op-validation.md` — found
  while waiting for this tick.
