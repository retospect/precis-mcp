---
status: idea
pillar: memory-graph
title: Elsevier entitlement-preview PDFs ground papers and findings as if complete
---

# Elsevier preview-PDF remediation (~2,796 prod papers)

Signature: `refs.pdf_pages` single-page range against a >100 KB payload —
methodology + regeneratable scoping query:
`docs/runbooks/elsevier-preview-pdf-remediation.md` (treat the old 224 count
as stale). The code blockers are fixed (markup/companion-PDF sidecar races
gr170349 / gr161905); next: re-run the 5-ref pilot with the staged-publish
fix deployed, then scale via the runbook's reset SQL (known incomplete:
ref_identifiers cleanup). Must run on cluster infra — the Elsevier key lives
in the vault, which agent_rw can't read by design. Ops.

Bundled in the 2026-10-11 gripe triage: the three gripes below are the same
cohort seen from three sides.

## 1. Under-chunked refs 464484 and 463425 (gr465468)

Both chunk to 0 and 7 chunks from ~100 KB cached payloads, so they are
likely entitlement-preview PDFs from the same cohort. Still under-chunked
on 2026-10-11. Fix direction: inspect both cached payloads during the
requeue; if the payload is an entitlement preview, mark the ref incomplete
rather than leaving it looking ingested.

Who: ops (the requeue is cluster-side).

## 2. pa167977 is a one-page preview and grounds two finding hubs (gr372863)

pa167977 is still a one-page preview PDF, yet two finding hubs rest on it.

- Ops: re-fetch it through
  `docs/runbooks/elsevier-preview-pdf-remediation.md`, then re-check the two
  hubs' evidence against the full text. (The read-path and evidence-attach
  one-page flag shipped; existing edges are not back-stamped, so these two
  hubs show it only on their next attach.)

Who: ops for the re-fetch.

## 3. Front-matter-only backfiller exists only for Elsevier (gr372785)

The detector for PDFs whose body is just front matter / a truncated preview
is Elsevier-specific. Other publishers produce the same thin bodies and
nothing finds them. Fix direction: measure thin-body rates per publisher
first (read-only corpus query), then add a validated publisher footer
signature, Wiley first. Owner `src/precis/workers/fetch_oa.py`. Validate each
signature against known-complete papers before it triggers a refetch.

Who: agent-code (measure, then signature); ops runs the resulting requeue.
