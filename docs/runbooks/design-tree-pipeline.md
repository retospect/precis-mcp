# Design-tree content pipeline — pre-fill, verify, merge

When you are adding or deepening nodes in the design knowledge graph
(`src/precis/data/design-tree/`, backlog `design-tree.md`, gr516958). The
same procedure fits any sourced-rules corpus. It produced 128 nodes and
~2,370 verified claims over three runs, 2026-10-25/26.

## Procedure

1. **Skeleton by hand.** Write the node names and levels, and have Reto
   review them. The three skeletons used are in
   [design-tree-pipeline/skeletons/](design-tree-pipeline/skeletons/).
2. **Pre-fill.** Send one templated question per node to
   `perplexity-reasoning` (or `perplexity-research`). Put the node name
   first in the template, so templated queries don't share a cache key.
   The answer is read through the kind. Don't dump it to files: gr517274
   (fixed, 881ef1bae) removed the reason the runs did, which was the 4 KB
   fold.
3. **Expand.** One Sonnet agent per batch of 5–9 nodes writes a record
   per node, with every number or hard rule as its own claim, in our own
   words.
4. **Verify.** A different Sonnet agent checks every claim at its source
   and assigns one verdict:
   - confirmed;
   - confirmed-alt (another source confirms it);
   - excerpt-only (seen only in a search excerpt);
   - contradicted;
   - unsupported;
   - unreachable.

   It also remaps wrong citations. Verifiers need local PDF text
   extraction, because WebFetch can't read compressed PDFs. Skipping this
   stage is not an option: it caught errors of 10–32x (examples in
   gr516958 comment 10).
5. **Merge.** Build the manifest. Map verdicts to `confidence`, apply the
   corrected values, and put machine-specific numbers into
   `capabilities.json` rows via `table_ref`. The design overrides used are
   `scripts/work3/overrides3.py` and `scripts/s2/merge.py`.
6. **Red list.** Reto reviews only the numbers a designer dimensions from
   that are corrected, shop-rule or unverified. Each "use X" becomes
   `shop-rule (Reto)`.
7. **Import.** Run `precis conventions import --manifest design-tree` on
   the dev DB, dogfood a walk, then prod.

Intermediate drafts and verdicts have no precis home yet (gr520907). Until
that lands, keep them in a worktree's `scratch/`, never `/tmp`, and
regenerate the red list from the merged manifest.

## Per-run detail (provenance)

- **Protocols.** In [protocols/](design-tree-pipeline/protocols/):
  - `pilot-PROTOCOL.md` holds the record format and the verdict
    definitions;
  - `run2-PROTOCOL.md` is the cheaper pipeline;
  - `run3-PROTOCOL.md` holds the process-node templates;
  - `s2-AGENT-BRIEF.md` is the desktop-FDM source re-check.
- **Metrics per batch:** [metrics/](design-tree-pipeline/metrics/).
- **Worked walks for builders:**
  [worked-walks.md](design-tree-pipeline/worked-walks.md). Check rule ids
  against the current manifests.
- **Scripts:** [scripts/](design-tree-pipeline/scripts/). They are kept as
  provenance and won't run: their input dirs are gone.

Run lessons:

- Name the makers' catalogue PDFs in the query (SKF/Schaeffler pages are
  JS-only; Parker returns 403).
- Use per-topic templates for non-geometry processes (coating, heat
  treatment). The DfM template drags answers toward machining tables.
- Budget by agent time: ~50–75k Sonnet tokens per node. Perplexity costs
  ~$0.02 per node.
