# Duplicate paper handling — plan

Status: **plan** (2026-06-19). Written after the `fix-metadata`
remediation surfaced 30 duplicate paper refs (a junk-metadata copy of a
paper whose canonical version already exists). This sketches how dups
arise, what already guards against them, and a phased plan to handle the
residual cases without the foot-guns of the (now-deleted) `dedupe-papers`
command.

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

### Phase 1 — fold dup-resolution into `fix-metadata` (low effort, high value)
On the DOI-conflict path (today `no_change`), when the suspect is junk
(empty/garbage title) and the conflicting canonical is alive with good
metadata, **soft-delete the suspect as a duplicate** instead of erroring:
migrate identifiers/edges to the canonical, record the `supersedes` edge +
audit event, soft-delete. This auto-handles the exact 30-dup class found
on 2026-06-19 — no separate command, no manual SQL. Gated behind
`--apply`; the dry-run reports `dup -> canonical` lines.

### Phase 2 — periodic reconciliation sweep (the safety net)
A maintenance pass (candidate home: a `precis maintenance` phase or a new
`reconcile-duplicates` job) that, over **live** paper refs, groups by
shared `pdf_sha256` then `doi`/`arxiv` (v2-correct: read from
`ref_identifiers`, key on `ref_id`), applies the survivor rule above, and
soft-deletes losers with the audit trail. Dry-run by default. This catches
dups that Phase 1 misses because neither copy was ever a `fix-metadata`
suspect (e.g. two good-metadata ingests of the same paper from different
files that share a `pdf_sha256` but were inserted in a race).

### Phase 3 — near-duplicate detection (harder, later)
Same paper, different files, **no** shared `pdf_sha256`/`doi`/`content_hash`
(different OCR). Needs fuzzy matching — title + author + year similarity,
or a SimHash/MinHash over body text. Out of scope until Phases 1–2 show
this class is material. Surface candidates for human review rather than
auto-deleting (precision matters more than recall here).

## Non-goals
- No hard-delete anywhere — soft-delete + audit only, so any
  mis-merge is reversible.
- No "lowest-id wins" rule, ever (the deleted command's bug).
