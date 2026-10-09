# Elsevier preview-PDF remediation

## Background

gr162364/gr162363 (2026-07-17): `fetcher:elsevier` fetches returned Elsevier's
entitlement-limited **preview PDF** (large payload, single rendered page)
instead of the full article, and the old pipeline ingested it as if it were
complete — no error, no chunk-count sanity check. Root-caused and fixed:

- **Code fix**: `c838c8e9` (XML markup leg + truncation alert),
  `7f3db0cb` (gr161905 — the markup-vs-PDF ingest race that made a safe
  re-fetch possible without a duplicate-body risk). Both are on `main`.
- **What's left**: the papers ingested *before* the fix still carry the
  truncated body. This runbook is the reset + re-fetch procedure for those.

## Scoping query (regenerate — do not trust a stale ref_id list)

The reliable signature is **Marker's own extracted page range**
(`refs.pdf_pages`), not chunk count — chunk count varies too much across
legitimately short papers to threshold cleanly, but a genuinely single-page
`pdf_pages` range against a large cached payload is Marker directly telling
you it only ever saw page 1. Validated against the reference incident
(`ref_id=162036`, the paper gr162363 was filed against): `pdf_pages=[0,1)`,
`size_bytes=647277`.

```sql
-- Count + list of currently-affected refs. Re-run this fresh each time —
-- prod state changes (new fetches, prior remediation rounds) so a cached
-- list from an earlier session WILL go stale. Read-only; safe on agent_rw.
WITH elsevier_fetch AS (
  SELECT DISTINCT ON (ref_id) ref_id, ts, (payload->>'size_bytes')::bigint AS size_bytes
  FROM ref_events
  WHERE source = 'fetcher:elsevier' AND event = 'fetch_ok'
  ORDER BY ref_id, ts DESC
)
SELECT r.ref_id, r.title, e.size_bytes, r.pdf_pages
FROM elsevier_fetch e
JOIN refs r ON r.ref_id = e.ref_id AND r.retired_at IS NULL
WHERE (upper(r.pdf_pages) - lower(r.pdf_pages)) <= 2
  AND e.size_bytes > 100000
ORDER BY e.size_bytes DESC;
```

**2026-07-23 snapshot: ~2,796 refs matched** (not the 224 an earlier pass
logged — that figure came from an unpersisted query and could not be
reconciled; this signature is validated against the known-bad reference
paper and trusted over it). Treat any count as a snapshot, not a target —
re-run before acting at scale.

## Reset procedure (per ref_id, or a batch of ref_ids)

Chunk delete alone is not enough — `claim_stubs_to_fetch` selects on
`refs.pdf_sha256 IS NULL`, and a bare pdf_sha256 reset alone leaves the stub
sitting in the exponential fetch-backoff window keyed on `ref_events` history
(see `fetch_oa.py::claim_stubs_to_fetch`), so it would not actually retry
promptly. The reset must also clear that backoff — same convention as
`paper_hygiene.requeue_stranded_fetches`'s stranded-fetch heal (delete the
`fetcher:%` history, stamp `meta.oa_requeued` so it jumps the queue):

```sql
BEGIN;

DELETE FROM chunks WHERE ref_id = ANY(%(ref_ids)s) AND ord >= 0;

UPDATE refs
   SET pdf_sha256 = NULL, pdf_pages = NULL, pdf_role = NULL
 WHERE ref_id = ANY(%(ref_ids)s);

-- Hygiene: drop stale hash identifiers pointing at the truncated PDF
-- being replaced. Not confirmed as the cause of the 2026-07-23 pilot's
-- truncated-worse result (forensics point at gripe:170349 instead), but
-- these rows are genuinely dangling once the reset above clears the ref's
-- own pdf_sha256, so clear them regardless.
DELETE FROM ref_identifiers
 WHERE ref_id = ANY(%(ref_ids)s) AND id_kind IN ('pdf_sha256', 'content_hash');

DELETE FROM ref_events
 WHERE ref_id = ANY(%(ref_ids)s) AND source LIKE 'fetcher:%%';

UPDATE refs
   SET meta = meta || jsonb_build_object(
         'oa_requeued', jsonb_build_object(
           'at', now()::text,
           'reason', 'elsevier-preview-pdf-remediation'
         )
       )
 WHERE ref_id = ANY(%(ref_ids)s);

COMMIT;
```

Then, with `PRECIS_FETCH_MARKUP=1` set (so the OA-fetch cascade takes the
Elsevier XML markup leg instead of the plain PDF leg), the next fetch pass
re-acquires these stubs and — per the gr161905 fix — the companion PDF is
tagged `printable_only` so Marker never re-truncates the body.

## Pilot (5 refs, chosen from the 2026-07-23 snapshot)

`162036` (the reference incident itself — best sanity check), `58457`,
`165559`, `39798`, `168074`. Run the reset SQL against just these five,
watch one fetch pass recover full text, verify (chunk count back to a
normal range, `pdf_pages` spans the real document, abstract no longer
truncated mid-sentence) before scaling to the rest.

**2026-07-23 pilot run result: BLOCKED, do not scale.** Reset+fetch (the
SQL above, `PRECIS_FETCH_MARKUP=1`) worked exactly as documented — all 5
stubs re-fetched larger companion PDFs. But downstream ingest recovered
full text for **none** of the 5; 4 ended up more truncated than before the
reset and one (`168074`) stayed empty. Root cause **confirmed** via
forensics (sidecar files + watch-log sequence): a markup-companion-PDF
sidecar race — `elsevier_xml`'s `"no <body>"` parse-failure recovery path
(gr161905, `add.py:_recover_markup_parse_failure`) gives up permanently if
the companion PDF's filename hasn't yet been linked onto the markup
sidecar. `168074`'s companion PDF landed in the shared inbox but was never
imported because of exactly this race. Filed as its own actionable item,
fix direction included: `gripe:170349`.

Two secondary findings, neither the primary driver: spark's Marker OCR was
separately broken (nvidia docker runtime never configured) — **fixed and
verified 2026-07-23**. `ref_identifiers` stale rows (`id_kind IN
('pdf_sha256','content_hash')`) do survive the reset SQL, but forensics
show they are **not** what caused the truncated results.

**2026-07-24: `gripe:170349` fixed** (worktree `sharded-crunching-kite`, not
yet shipped to `main`). `_run_markup_cascade` (`src/precis/workers/
fetch_oa.py`) now stages the markup trigger + its sidecar under
`inbox_dir/.staging/` — unwatched (`watch.py::_MANAGED_DIRS`) — instead of
writing directly into the watched inbox. `_publish_markup_trigger` replaces
`_link_markup_companion`: it rewrites the sidecar with the now-known
`companion_pdf` and `os.replace()`s both files into the real inbox path in
one atomic step, only after the companion PDF's fate is known. The trigger
is never watcher-visible with an incomplete sidecar.
`scripts/test tests/workers/test_fetch_oa.py -k Markup` (11 passed) +
`scripts/test --impacted` (472 passed) + ruff/mypy clean.

Do not run the reset SQL against the full ~2,796-ref population until this
fix has **shipped** and a re-run of the 5-ref pilot recovers full text
end-to-end.

## Execution boundary — must run on cluster infra

`PRECIS_ELSEVIER_API_KEY` lives in the DB-backed vault
(`src/precis/secrets.py`, ADR 0055); `agent_rw` (the only DSN reachable from a
dev laptop session) has **zero vault grants by design**. The reset SQL can
be prepared/reviewed from a dev session (as above, read-only until the
`BEGIN`/`COMMIT` block runs), but the actual fetch pass needs a real
worker's vault-capable DSN — run via a `cluster-admin` session against
melchior/caspar, watching the pass logs for the re-fetch.

## Front-matter-only preview PDFs — `precis markup-backfill` (gripe 372781)

**Symptom.** Elsevier's Article Retrieval API can return a well-formed,
complete `%PDF-` that is only the entitlement-limited preview page (title,
affiliations, abstract, first intro paragraphs, printed footer) with no
error. It ingests silently as ~8 body chunks. Example: pa167977 (fetched
2026-07-22Z, before the markup-first XML leg existed; gr372863).

**Tooling (on main, deployed 2026-09-25Z).** Markup ingest outcomes are
journalled as `markup:<fmt>` ref_events (`markup_ingested` /
`markup_parse_failed`). `precis markup-backfill`
(`src/precis/cli/markup_backfill.py`) = detector + one-shot
`meta.markup_refetch` pin + `stub_predicate_sql` escape hatch + gated
DELETE+INSERT body replace (only at >=3x and >=30 chunks). Deliberately
**not** wired into `paper_reconcile` — Reto wants to eyeball the dry-run
first; wiring it in is the follow-up.

**Next step (not yet run).**
1. Dry-run `precis markup-backfill`. Expect ~1641 rows (prod 2026-09-20Z
   funnel: 3726 Elsevier-PDF-only → 2717 thin → 1659 with the footer
   signature → 1641 without a reference list). The docstring's "~7800" is an
   estimate of the affected population, not the detector yield. Re-measure;
   the corpus has grown.
2. `--apply`, then watch for `markup_backfill` / `body_replaced` events.

**Detector caveat.** The "no reference list" filter trims only 18 of 1659;
the footer signature does the discrimination. The real safety net is the
growth guard at replacement time.

**Fragile config.** `PRECIS_FETCH_MARKUP` is set on melchior only (overlay
`host_vars`, gitignored). Harmless only while melchior claims essentially all
OA-fetch work — if melchior drains, every Elsevier paper silently reverts to
preview-PDF ingest, and nothing in the repo shows it.

**Open.** Gripe 372785: extend past Elsevier (Wiley TDM next). Do not widen
to "any thin PDF body" — each publisher needs its own validated footer
signature.

## Bodiless PDFs — `precis bodiless-heal` (gripe 453860)

**Symptom.** A live paper with `pdf_sha256` set and no body chunk
(`ord >= 0`). ~950 on prod (2026-10-02): 814 Elsevier not-entitled (the
preview above, but bodiless rather than thin), 65 readable PDFs that never
ran through Marker, 43 corrupt, 20 missing on disk, 5 scanned. Counted by
`precis stats` (bodiless-pdf section) and the `/status` "PDF-no-body" label.

**Tooling.** `paper_hygiene.heal_bodiless_pdfs` judges each candidate
once and journals the verdict as a `ref_events` row,
`source='heal:bodiless'`, `event` ∈ `preview` / `missing_file` /
`unreadable` / `extracted`, `payload.pdf_sha256` = the sha judged (a
re-fetch that lands a different file is judged afresh). Elsevier-fetched
candidates get `preview` and are never re-extracted — that would mint a
preview *body*. Readable, locally held PDFs are re-extracted (Marker in a
killable subprocess) through the ordinary stub-upgrade write path. Wired
into `paper_reconcile` with a per-pass Marker cap
(`PRECIS_BODILESS_HEAL_EXTRACT_PER_PASS`, default 5; `0` = judge only).

**Operator run.** `precis bodiless-heal` (dry-run: prints the verdict each
candidate would get) then `precis bodiless-heal --apply` on a node that
mounts the corpus — a file only another node holds is reported `deferred`
and left for that node's pass. Read verdicts back with
`SELECT event, count(*) FROM ref_events WHERE source = 'heal:bodiless' GROUP BY 1`.
