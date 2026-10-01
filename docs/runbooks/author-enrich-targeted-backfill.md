# Draft bibliography has mixed author styles — targeted `paper_meta_enrich`

**Symptom.** An old draft's bibliography shows inconsistent author styles
(mixed Semantic Scholar / Crossref forms).

**Cause (not a bug).** The `paper_meta_enrich` worker pass (Crossref author
re-resolve) selects `meta->>'authors_resolved_at' IS NULL` **newest-first**,
50 refs per 6 h. The unstamped backlog was ~30,700 papers on 2026-08-28Z, so
low-ref-id (old) papers effectively never resolve on cadence. Check
`meta->>'authors_source'` and the stamp before inventing lint.

## Targeted fix

1. Build `(ref_id, doi)` pairs for the draft's cite closure from `links`:
   papers the draft `cites` ∪ papers cited by its cited findings
   (`cites` / `derived-from`).
2. Run `precis.ingest.paper_meta_enrich.enrich_paper` per ref against prod
   from the controller. DSN and `host.docker.internal` → `127.0.0.1` rewrite
   as in [`local-prod-draft-export`](./local-prod-draft-export.md); run under
   `uv run --with "habanero>=2.0"` (habanero lives in the heavy `paper`
   extra).
3. Crossref intermittently fails a first attempt: those refs land
   `authors_source=heuristic` **and still get stamped**. Re-running
   `enrich_paper` for just those is safe (it does not check the stamp);
   a 29-paper closure went fully green with 7/7 on retry.
4. `human_verified_at` refs keep their authors.

Systemic follow-up shipped 2026-08-29Z: draft-hygiene warns on a stale
bibliography. Reuse the closure SQL + driver pattern for any draft-level
"authors look inconsistent" complaint.
