---
status: idea
pillar: memory-graph
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

**Also covers gr465468** (2026-10-10 triage): refs 464484 and 463425 chunk
to 0 and 7 chunks from ~100 KB cached payloads. These are likely
entitlement-preview PDFs from the same cohort. Inspect both payloads during
the requeue.
