# `precis fix-metadata`: junk-metadata remediation, needs-triage, duplicates

**When.** Local papers were ingested with junk/empty titles ("No Job Name",
`anon…` slugs, initials-only authors), or you are working the `needs-triage`
queue, or handling duplicate refs.

## The tool

`precis fix-metadata` (`src/precis/ingest/remediate.py` +
`src/precis/cli/fix_metadata.py`) re-derives metadata for such papers. Run it
via the in-repo ansible runner `deploy/run-fix-metadata.yml`, which injects the
prod DSN and the Semantic Scholar key from the vault into caspar's env (secrets
never hit the transcript):

    ansible-playbook run-fix-metadata.yml                              # dry-run
    ansible-playbook run-fix-metadata.yml -e '{"fixmeta_args":"--apply"}'
    # --retry-triaged re-attempts needs-triage papers

First full run against prod (2026-06-19Z): 336 suspects → **195 fixed**,
**110 tagged `needs-triage`**, **30 duplicates soft-deleted**, 1 no-pdf.

## Working the needs-triage queue

- **Triage UI:** the **Triage** nav tab → `/papers/triage` lists `needs-triage`
  papers. A paper's detail page has a paste-title → S2 panel that pre-fills the
  edit form; Save applies and clears the tag. The triaged papers keep their junk
  titles until worked through this queue.
- `PaperHandler.edit` rewrites `card_*` chunks on metadata change (shared
  `ingest/cards.rewrite_cards`).

## Duplicate handling (`src/precis/ingest/dedup.py`)

- `merge_duplicate` + `pick_survivor`: survivor = DOI → non-junk title → most
  authors → lowest id — **never lowest-id alone** (the deleted `dedupe-papers`
  CLI used keep-lowest-id on a stale v2 schema). `Store.identifier_owner` backs
  it.
- Phase 1: `fix-metadata` folds a re-derived-DOI duplicate into its canonical
  (`action="deduped"`).
- Phase 2: `precis reconcile-duplicates` collapses `pdf_sha256`-shared refs;
  **runs nightly 04:30 on caspar** via the cluster `precis_reconcile` launchd
  role (playbook 40; runner `deploy/run-reconcile.yml`).
- All merges soft-delete + audit (`ref_events` `duplicate_merged` /
  `soft_deleted_duplicate`, a `supersedes` edge, `meta.superseded_by`).
- Phase 3 (fuzzy near-dup) is still planned.

**No hard guarantee against new dups.** Ingest `probe_existing` dedups on
`pdf_sha256` / doi / arxiv / s2 / `content_hash` / `paper_id`, and resolving
more DOIs at ingest makes more re-ingests collide — but the same paper as two
different files with no resolvable DOI and differing OCR can still duplicate
(it lands in needs-triage). Prod access: [`prod-db-access`](./prod-db-access.md).
Deploy: [`cluster-deploy`](./cluster-deploy.md).
