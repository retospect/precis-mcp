# Triage the draft-backfill claim hubs — reground / retire / merge

When a whole-draft backfill pass lands (`taproot_backfill`, the `[pa]`→`[pc]`
re-ground + `[pc]`→`[fi]` promote), some hubs come out wrong in two *different*
ways, and the remedies are opposite:

- the hub **states a real claim** but the evidence edge is grounded on a
  paper's title/author front-matter block → **re-ground**, don't retire;
- the hub **states nothing** — a text-about-text gloss ("The passage
  describes…") or a sentence fragment ("subsequently into flexible transparent
  conductors…") → **retire**, don't go looking for support.

Telling them apart by eye does not scale. This runbook is the mechanical split.

## 1. Dump the edges

Read-only. `origin=draft-backfill` is the fingerprint `apply_chunk` stamps on
every evidence edge it mints.

```sql
COPY (SELECT json_agg(row_to_json(t)) FROM (
  SELECT l.link_id, l.dst_ref_id AS hub_ref_id, r.title AS claim,
         l.meta->>'source_handle' AS source_handle,
         l.meta->>'draft_chunk'   AS draft_chunk,
         c.ord, c.text AS chunk_text
  FROM links l
  JOIN refs   r ON r.ref_id = l.dst_ref_id
  JOIN chunks c ON c.chunk_id = (substring(l.meta->>'source_handle' from 3))::bigint
  WHERE l.meta->>'origin' = 'draft-backfill'
    AND r.deleted_at IS NULL
    AND c.retired_at IS NULL
) t) TO STDOUT
```

`scripts/prod-psql "<the above>" > /tmp/triage.json`. COPY escapes backslashes
and newlines, so un-escape before `json.loads`.

Narrow to one draft with `AND l.meta->>'draft_chunk' = ANY(…)`, or to one run
with `AND l.created_at > …`.

## 2. Bucket

Two independent axes, one Python predicate and three regexes:

| axis | signal | source |
|---|---|---|
| grounding | `taproot.grounding.has_grounding_prose(chunk_text)` | the gate itself — reuse it, don't re-implement |
| claim: meta | `^(the\|this) (passage\|chunk\|text\|excerpt\|section\|paper)\b` | text-about-text |
| claim: fragment | starts lowercase, or with `subsequently\|and\|which\|where\|then\|later\|also` | cite-group segmentation cut mid-sentence |
| claim: anaphoric | `^(the same (group\|authors\|team)\|they\|these authors)\b` | narrative subject; the world-claim twin usually already exists |

Bucketing:

- **RETIRE** — claim is meta or fragment. Grounding is irrelevant: there is
  nothing to support. Retire the hub; the draft prose keeps its `[pa]`/`[pc]`.
- **MERGE?** — claim is anaphoric. Find the world-claim twin (same content,
  proper subject) and merge; these are near-duplicates that defeated
  `block()`'s ANN because a narrative restatement embeds differently. Feed them
  to `docs/backlog/claim-hub-dedup-sweep.md`.
- **REGROUND** — claim is assertive but `has_grounding_prose` is False. Run
  `precis taproot repair-evidence --cohort prose-less --draft <dr>` (dry-run by
  default; `--apply` to write). It re-verifies the hub's claim against **only**
  the source the edge already names, so the passage is found in the paper the
  edge asserts — which is what you want here: the title matched precisely
  because the claim restates that paper's own result, so its body almost always
  carries it. A source with no supporting passage is recorded and *nothing* is
  written — neither the edge nor the claim.
- **ok** — everything else.

## 3. Measured baseline (2026-08-25, before any remediation)

62 edges / 57 distinct hubs across the nanobud and Parkinson drafts:

| bucket | n |
|---|---|
| RETIRE | 4 |
| MERGE? | 0 |
| REGROUND | 5 |
| ok | 53 |

Re-run after a remediation pass; the buckets are the measurement.

Two things that baseline showed, worth carrying forward:

- **`ord` is not the signal.** Six of the low-`ord` groundings are abstracts,
  which are fine evidence, and four are numeric tables, which ground numeric
  claims well. Only a *prose* test separates them — which is why
  `_has_grounding_prose` accepts a table and rejects a title that happens to be
  a full sentence (a title carries no terminator).
- **The fragment class is wider than the title-grounded class.** `fi245753`
  ("dopaminergic degeneration diminishes ventilatory drive") is a fragment
  grounded on perfectly good body prose. Bad grounding and empty claim
  correlate, but neither implies the other — which is why the buckets are two
  axes, not one. → `docs/backlog/taproot-backfill-fragment-claims.md`

## 4. Related

- The grounding gate: gripe 245842, `precis.taproot.grounding.has_grounding_prose`
  — enforced in `backfill` (both cite arms), in
  `reground.candidate_passages` (so re-grounding, chase and evidence repair
  never offer a title page), and selectable as `repair-evidence --cohort
  prose-less`.
- The fragment cause: `docs/backlog/taproot-backfill-fragment-claims.md`.
- The evidence-side mirror (a paper's lit-review paragraph accepted as
  evidence): `docs/backlog/taproot-evidence-section-gating.md`.
