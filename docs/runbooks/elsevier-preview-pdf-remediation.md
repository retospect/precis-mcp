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
JOIN refs r ON r.ref_id = e.ref_id AND r.deleted_at IS NULL
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

## Execution boundary — must run on cluster infra

`PRECIS_ELSEVIER_API_KEY` lives in the DB-backed vault
(`docs/design/secrets-vault.md`); `agent_rw` (the only DSN reachable from a
dev laptop session) has **zero vault grants by design**. The reset SQL can
be prepared/reviewed from a dev session (as above, read-only until the
`BEGIN`/`COMMIT` block runs), but the actual fetch pass needs a real
worker's vault-capable DSN — run via a `cluster-admin` session against
melchior/caspar, watching the pass logs for the re-fetch.
