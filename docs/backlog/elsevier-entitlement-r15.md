---
status: ready
pillar: platform
---
# gr470193 — reject Elsevier previews before success; explicit cohort backfill

Base origin/main9893059498a9a5252c17e91d2374a637de2e0522. Native
Python search/source first; indexed /app is distinct from task tree. Local
owning code confirms XML is logged successful without parse, PDF only checks
magic, and parse_elsevier traverses serial-item wrappers. The synthetic abstract
fixture already fails parsing; the defect is logging success before parsing.
Existing markup_refetch + oa_requeued pin supports nondestructive re-fetch.

Acceptance: synthetic abstract-only XML fails before source fix. Require
real body blocks in Elsevier XML, gate both XML and PDF Elsevier legs before
fetch_ok (PDF uses same endpoint XML preflight); entitlement misses continue
cascade and publish no preview file. No character-length cutoff or OA=false
rejection of entitled full text. Preserve full short articles. Do not weaken
SSRF/cert handling; no live keys/providers/jobs in development.

Probe: fixed known-OA Article Retrieval DOI10.1016/j.heliyon.2019.e03087
(official Heliyon page identifies OA), instead of ScienceDirect Search.
CORE429 => rate limited with code; other unknowns preserve HTTP code.

Backfill: registered deterministic job, explicit ref_ids, dry_run default,
expected_count guard. Stamp markup_refetch/oa_requeued using existing seams;
retain bodies/hashes/events until valid replacement ingests. No broad
short-paper heuristic. Request frozen2007IDs from coordinator; no production
execution before validated deployed fix. Baseline supplied:3312success events,
3117distinct papers,2007under5000chars,705over20000; aftercounts NOT_RUN until
real replay. Unit DB cohort tests prove idempotency and normal claimability.

Focused scripts/test, Ruff/types, independent review, scoped branch commit;
normal coordinator integration owns fullsuite/version/deploy gates.
