---
status: active
pillar: platform
---
# R15 — total ref reader coverage (gr469093)

Approved scope: registered kinds must never fail detail navigation because a
separate browse allowlist forgot them. Confirmed origin is Drive with every
kind selected, `sort=created`, `state=all`, `paper_chunks=both`: ORCID469065
and agentlog469123. This is not a Return/Showcase or kind-taxonomy change.

## Contract

- Existing matching live refs resolve regardless of which optional handlers
  loaded in this web process. Native readers redirect with 303; other kinds
  render the generic detail template. Keep deleted-ref tombstones and wrong
  kind/nonexistent-ref rejection. The curated browse menu is not a detail gate.
- One shared kind-to-reader URL helper supplies Drive rows (including folder
  children), `/r/`, previews/tag pivots and `/refs` redirects. Slug readers
  need a stored slug; absent one, fall back to generic detail without loops.
  Preserve paper chunk/page jumps and claim-hub finding redirects.
- File-backed Markdown/plaintext/TeX render stored chunks: handler get can
  retire a stored ref absent from this process's filesystem. Repeated opens
  must remain 200 and retain the stored snapshot.
- ORCID shows stored name, validated ORCID iD with outbound orcid.org link,
  and live papers on `authored`/inverse `authored-by` links. No upstream lookup/refresh,
  credential probe, schema change or production write is needed to view it.
- Inventory production live counts via read-only `refs.retired_at IS NULL`
  grouping. Coordinator supplied affected kinds: orcid news agentlog
  semanticscholar draft tex taxon se llm folder concept wikipedia figure cfp
  markdown cad mermaid plaintext plan protein make estimate. Counts pending
  coordinator read, not inferred from historical totals.

## Focused acceptance

1. Every registered kind with a fixture ref: GET `/refs/kind/id` never 400;
   native redirects land on the existing reader, generic detail returns 200.
2. Every Drive-offered kind, including all22 affected kinds: rendered row URL
   reaches 200 with a valid reader fixture. Also verify `/r/` and tag pivots
   use the same target; slug escaping and absent-slug fallback covered.
3. ORCID fixture name/iD/outbound link and authored paper link render. Deleted
   papers omitted; no runtime get/upstream fetch for ORCID page.
4. Existing cache no-fetch, tombstone, finding claim and paper-jump checks stay
   passing. Fresh Chromium follows actual all-kind Drive row to ORCID and
   agentlog on the isolated dev app/DB; no console errors.

No full-suite scheduling, release metadata bump or integration here; coordinator
owns those gates. R14 sort/kind branches and scratch evidence stay immutable.

## Verification to date

483 focused Drive/ref/resolver/tag/backlink tests passed on the final source;
scoped container types (8 files) and Ruff/format (9 files) passed. The real-store
inverse test caught `fetch_refs_by_ids` including deleted refs by default;
the person view explicitly requests live refs only. The focused registry walk
also proves repeated file-detail opens preserve stored snapshots. Actual
Chromium dev navigation and production-count receipt are recorded in the
shared ready inbox after their checks; production/authenticated deployment
and full release gate remain coordinator-owned.
