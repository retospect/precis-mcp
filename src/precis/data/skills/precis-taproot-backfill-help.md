---
id: precis-taproot-backfill-help
title: precis-taproot-backfill-help — convert a draft's [pc]/[pa] cites into hub cites
summary: batch-convert a draft's legacy [pc<id>]/[pa<id>] cites into hub [fi<id>] cites via the taproot_backfill job — scope, chunk-grounding, and the whole-paper [pa] re-ground arm
applies-to: put(kind='job', job_type='taproot_backfill') for draft backfill; precis taproot backfill (CLI equivalent)
status: active
---

# precis-taproot-backfill-help — convert a draft's [pc]/[pa] cites into hub cites

See [[precis-taproot-help]] for what a claim hub is, `fi<id>` vs
`pub_id`, and how citing `[fi<id>]` resolves.

## Turn a draft's [pc<id>] cites into a hub cite

Most legacy prose cites raw paper chunks (`[pc<id>]`), written before claim
hubs existed. Convert a draft scope's `[pc<id>]` (and `[pa<id>]`, below) cites
into hub `[fi<id>]` cites by **enqueuing a `taproot_backfill` job**. The
cascade is LLM-heavy (`extract → block → dedup_judge → place`) and by
design runs on the cluster worker — **never in the MCP process**; the
verb only mints the job:

```python
# Canonical: write the intent as a todo; the dispatch worker mints the job.
put(
    kind="todo",
    text="taproot backfill my-draft-slug",
    meta={
        "executor": "claude_inproc",
        "job_type": "taproot_backfill",
        "params": {"scope": "my-draft-slug"},
    },
)
# → the dispatch worker mints the taproot_backfill job under it (one tick).
# Ad-hoc submit skips the intent layer — parent on the draft's numeric ref_id
# (its subject ref, ADR 0044) or a todo's; parent_id is an int, not a slug:
#   put(kind="job", parent_id=<draft ref_id>, job_type="taproot_backfill",
#       params={"scope": "my-draft-slug"})
get(kind="job", id="jo<id>")  # poll: job_event stream + [pc]→[fi] as it runs
```

`params.scope` is a draft slug (every body chunk), a `dc<id>` heading
(its section), or a `dc<id>` leaf (one chunk); `params.ref_level`
(default false) controls the `[pa]` arm (below). The job runs
**serially and checkpointed** on the melchior agent worker: one chunk
at a time (so hub convergence sees a stable committed set — no
parallel near-duplicate race), progress in `meta.done_chunk_ids`, a
re-claim resumes where it left off. **No preview** — the prose rewrite
is a DELETE+INSERT through the draft edit door (embeddings re-run), so
the chunk history is the undo if a conversion is wrong. The CLI form
runs the same cascade in a shell / batch context:

```bash
precis taproot backfill --chunk dc1652005 --apply   # one chunk / section
precis taproot backfill --draft my-draft-slug       # every body chunk in a draft
```

It anchors on the `[pc<id>]` markers (the citation grouping picks the
claim span — not a sentence split you pick yourself): each cite's
preceding prose is the claim span, and adjacent pc-cites (`[pc1][pc2]`)
grounding one span collapse to **one** written cite. Each span runs the
full canonicalizer cascade (`extract_claim → block → dedup_judge →
place → apply_extraction`): a span bundling more than one atomic claim
splits into several atom hubs (each with its own evidence edge) plus a
non-evidence **compound** hub `conjunct-of`-linked to them (see
[[precis-taproot-help]]'s "The evidence model" section) — either way
the rewrite target is **one** `[fi<hub>]` (the compound when one
landed, else the lone atom), so a citer sees no change. A risky merge
files a review `todo` and leaves the `[pc…]` untouched; a
pointer-only span (no groundable claim) is left as-is. It is
**on-demand, per draft or section** — not a corpus sweep — and
idempotent: a re-run finds no `[pc…]` left to convert.

**Whole-paper `[pa<id>]` cites (the `[pa]` arm).** The same command also
recognizes bare whole-paper `[pa<id>]` cites (kept in their own groups — a
`[pa]` and a `[pc]` never fold together). Each is classified by whether its
paper is fetched:

- a **stub** `[pa]` (an un-fetched paper, 0 body chunks) is **skipped**
  (`stub-fetch-first`) — no passage to ground an edge, and an unread
  paper is never minted as evidence. Fetch the paper first, then re-ground.
- a **fetched** `[pa]` is **re-grounded** by default: a locate (lexical pick
  + a Tier.MEDIUM confirm) finds the supporting passage and rewrites the
  token `[pa<id>]`→`[pc<chunk>]` (action `reground`), which the existing
  `[pc]` path then promotes to a **chunk-grounded** hub on a later run
  (two-step; no hub minted by the re-ground itself). No passage found →
  `reground-nomatch`, left `[pa]`, no write. Pass `--ref-level` to instead
  promote it whole-paper: mints a **ref-level (ungrounded)** evidence edge
  and rewrites `[pa]`→`[fi<hub>]` directly — for claims with no single
  grounding passage (e.g. "X is a landmark result"); `job_summary` reports
  the `ref-level/ungrounded` count. A contiguous multi-paper `[pa1][pa2]`
  run re-grounds all-or-nothing: any supporter failing to locate leaves
  the whole run untouched (never erase a token).

```python
# the [pa] arm rides the same job; ref_level=True promotes a fetched [pa]
# whole-paper instead of re-grounding it to a [pc] passage
put(
    kind="todo",
    text="taproot backfill dc1652005 (ref-level)",
    meta={
        "executor": "claude_inproc",
        "job_type": "taproot_backfill",
        "params": {"scope": "dc1652005", "ref_level": True},
    },
)
```

CLI equivalent: `precis taproot backfill --chunk dc1652005 --apply [--ref-level]`.

## See also

```python
get(kind="skill", id="precis-taproot-help")  # what a hub is; citing [fi<id>]
get(
    kind="skill", id="precis-taproot-mint-help"
)  # admissibility rubric the extraction cascade applies
get(
    kind="skill", id="precis-draft-help"
)  # draft chunk model, the edit door the rewrite uses
get(kind="skill", id="precis-citation-help")  # the inline [pc<id>] cite, write side
```
