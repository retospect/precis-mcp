# Duplicate paper handling — plan

Status: **Phases 1–3 implemented** (Phases 1–2 2026-06-19; Phase 3 —
title-similarity for id-less stubs — 2026-07-04).
Written after the `fix-metadata` remediation surfaced 30 duplicate paper
refs (a junk-metadata copy of a paper whose canonical version already
exists). This describes how dups arise, what guards against them, and the
phased handling — without the foot-guns of the (now-deleted)
`dedupe-papers` command.

Implementation: the shared merge primitive + survivor rule live in
`src/precis/ingest/dedup.py` (`merge_duplicate`, `pick_survivor`).
Phase 1 is wired into `fix-metadata` (`ingest/remediate.remediate_one`);
Phases 2–3 are `precis reconcile-duplicates` (`cli/reconcile.py` →
`dedup.reconcile_by_pdf_sha256` + `reconcile_by_doi_case` +
`reconcile_by_title_similarity`), also run on a cadence by the
`paper_reconcile` worker pass (`workers/paper_reconcile.py`). The
title-only-stub *prevention* guard lives in `Store.upsert_stub_paper`.
Owner lookup uses `Store.identifier_owner` (normalises identically to
`set_ref_identifier`).

## How duplicates arise

Ingest dedups via `db_writer.probe_existing`, which matches on
`pdf_sha256 / doi / arxiv / s2 / content_hash / paper_id`. A second copy
of the same paper therefore slips through **only** when it shares *none*
of those with the existing ref:

- It's a **different file** (different `pdf_sha256`) — a different scan,
  a re-typeset reprint, a publisher vs preprint PDF.
- It **resolved no DOI/arXiv** at ingest (junk embedded metadata, S2
  miss) — so the `doi`/`arxiv` probes can't link it to the twin.
- Its **`content_hash` differs** — different OCR / extraction of the same
  text hashes differently.

The 2026-06-19 root-cause fix (better title/author/DOI resolution at
ingest) shrinks this set — more re-ingests now resolve a DOI and collide
on the `doi` probe — but does **not** eliminate it. A DOI-less paper
arriving as two distinct files with differing OCR can still dup. Those
land in the `needs-triage` bucket (no recoverable metadata), which is
exactly where residual dups hide.

So: **fewer dups, not zero. A periodic reconciliation is the safety net.**

## What already exists

- **`probe_existing`** — the ingest-time guard above.
- **Zombie-stub reconciliation** (`ingest/add._reconcile_orphan_stub`):
  when a freshly fetched PDF content-dedups against a *different* ref than
  the chase stub it was fetched for, `precis_add` folds the orphan stub
  into the survivor — migrates identifiers + graph edges, records a
  `supersedes` edge + `meta.superseded_by`, soft-deletes the stub. This
  is the template for "merge a duplicate into a survivor" we should reuse.
- **`fix-metadata` dup *detection***: when a suspect's re-derived DOI is
  already owned by another live ref, `set_ref_identifier` raises and the
  paper is reported `no_change` (the conflict is the detection signal).
- The old **`precis jobs dedupe-papers` was deleted** — stale v2 schema
  and a wrong "keep lowest `ref_id`, hard-delete the rest" survivor rule
  that would delete the canonical in favour of the junk copy.

## Survivor selection rule (the important bit)

Never "keep lowest id". When collapsing a dup group, keep the ref with,
in priority order:

1. a resolved **DOI / arXiv** identifier,
2. a **non-junk title** (`not is_garbage_title` and non-empty),
3. the **most authors**,
4. **lowest `ref_id`** only as a final tiebreak.

The loser is **soft-deleted** (reversible `deleted_at`) with an audit
`ref_event` (`event='soft_deleted_duplicate'`, payload
`{duplicate_of: <survivor>}`) — matching how the 30 were handled by hand.
Identifiers + graph edges migrate to the survivor first (reuse
`_reconcile_orphan_stub`'s migration), and a `supersedes` edge +
`meta.superseded_by` are recorded so the merge is auditable and
`probe_existing` (which already filters soft-deleted refs) won't
resurrect it.

## Phased plan

### Phase 1 — fold dup-resolution into `fix-metadata` (DONE)
On the DOI-conflict path (today `no_change`), when the suspect is junk
(empty/garbage title) and the conflicting canonical is alive with good
metadata, **soft-delete the suspect as a duplicate** instead of erroring:
migrate identifiers/edges to the canonical, record the `supersedes` edge +
audit event, soft-delete. This auto-handles the exact 30-dup class found
on 2026-06-19 — no separate command, no manual SQL. Gated behind
`--apply`; the dry-run reports `dup -> canonical` lines.

### Phase 2 — reconciliation sweep (DONE — `precis reconcile-duplicates`)
A maintenance pass (candidate home: a `precis maintenance` phase or a new
`reconcile-duplicates` job) that, over **live** paper refs, groups by
shared `pdf_sha256` then `doi`/`arxiv` (v2-correct: read from
`ref_identifiers`, key on `ref_id`), applies the survivor rule above, and
soft-deletes losers with the audit trail. Dry-run by default. This catches
dups that Phase 1 misses because neither copy was ever a `fix-metadata`
suspect (e.g. two good-metadata ingests of the same paper from different
files that share a `pdf_sha256` but were inserted in a race).

### Phase 3 — near-duplicate detection (DONE for the id-less-stub class)
Same paper, no shared `pdf_sha256`/`doi`/`content_hash`. The material case
turned out to be **id-less title-only stubs**: an LLM (the `dream` actor)
references a paper we already hold *by title alone* via
`put(kind='paper', title=…)`; with no external identifier to collapse on,
`upsert_stub_paper` mints a fresh stub whose title-derived cite_key
(`attention17`, off the title's first word) structurally can't collide
with the held paper's author key (`vaswani17`). Two-sided fix:

- **Prevent (mint-time guard).** `upsert_stub_paper` now fuzzy-matches a
  title-only acquire against *held* papers (trigram title ≥ 0.85, year
  within 1) and returns the held ref instead of inserting — so a
  title-only re-acquire is idempotent like the identifier path.
- **Clean up (reconcile).** `dedup.reconcile_by_title_similarity` folds
  existing leaked stubs into the held paper via `merge_duplicate`,
  **auto-merging only the high-confidence band** (sim ≥ 0.85 + compatible
  year); the ambiguous band (0.6–0.85, or high-sim/year-mismatch) is
  surfaced as `TitleMatchReview` for human review, never auto-deleted —
  precision over recall, as planned. Wired into `precis
  reconcile-duplicates` and run on a 24h cadence by the `paper_reconcile`
  worker pass (single-runner advisory lock + `app_state` throttle).

Still open (lower priority): near-dup detection across two *held* copies
with different OCR (SimHash/MinHash over body text) — not yet material.

Adjacent leak worth its own pass: ~100 id-less title-only stubs that are
genuinely wanted (not dups) can't be chased by `fetch_oa` (no DOI/arXiv)
and never gain a dedup key. A title→identifier resolution pass (S2 title
search) would make them chaseable *and* dedup-eligible. Filed, not built.

## Deterministic hygiene heals (paper_hygiene.py)

Alongside the merges, the `paper_reconcile` pass runs three judgment-free,
network-free DB repairs (`src/precis/ingest/paper_hygiene.py`) that clean
*legacy residue* left by bugs the current code no longer produces. Each is
dry-run-able and idempotent:

- **Card drift** — a paper whose title was repaired but whose embedded
  `card_combined` search chunk was never rebuilt, so search still matches
  the stale text. All current write paths (`edit`, `fix-metadata`) call
  `rewrite_cards`; this heals history (`heal_drifted_cards`, verified
  punctuation-insensitively so a formatting variant isn't churned).
- **Superseded chains** — `meta.superseded_by` pointing at another retired
  ref instead of the final live survivor (the "stub points at a stub"
  dereference); `collapse_superseded_chains` rewrites it one-hop.
- **Dangling links** — a non-`supersedes` edge still pointing at a
  soft-deleted paper; `migrate_dangling_paper_links` repoints it to the
  survivor (dropping self-loop/duplicate collisions). The `supersedes`
  audit edge is deliberately left pointing at the retired ref.

## Bucket B — metadata re-resolution (built: `precis resolve-metadata`)

The `needs-triage` cohort (junk/empty titles Marker produced, and id-less
"wanted" stubs) needs *authoritative* metadata. `fix-metadata`/remediate
re-derives from a PDF on disk, so a missing/garbled PDF or a no-DOI paper
dead-ends in triage. `ingest/metadata_resolve.py` resolves **PDF-free**
from what we already hold:

- **Track 1 — stored DOI → Crossref** (`lookup_crossref`): authoritative;
  auto-applies unless Crossref's own title is junk (book front-matter → the
  discard lane).
- **Track 2 — no DOI → Semantic Scholar title search** (`lookup_s2`, query
  title from `refs.title` when usable else the first line of chunk 0):
  recovers a DOI + canonical metadata, **gated on trigram title similarity
  (≥0.85 auto, [0.6,0.85) review) + year compatibility**. The recovered
  DOI is the prize — it makes the paper citable, fetchable, and
  identifier-dedup-eligible (a recovered DOI already owned by another live
  ref → review as a likely duplicate, never a blind overwrite).

`apply_resolution` writes title/year/authors + `meta.abstract` + **journal**
(a gap remediate left), attaches the DOI/arXiv, `rewrite_cards`, and drops
the `needs-triage` tag. Auto verdicts apply; review + discard lanes print
for a human. CLI `precis resolve-metadata` (dry-run default, `--apply`);
network-bound so it runs on-cluster. Book front-matter / editorial
fragments and held-flag-without-chunks partial ingests are **flagged for
review, never auto-deleted**. A standing worker pass over future id-less
stubs is the planned next step (the CLI proves the resolution first).

## Non-goals
- No hard-delete anywhere — soft-delete + audit only, so any
  mis-merge is reversible.
- No "lowest-id wins" rule, ever (the deleted command's bug).
- No auto-merge of an **id-bearing** stub on title similarity alone — an
  authoritative identifier asserts distinctness; those are review-only.
