---
status: ready
pillar: platform
---

# R15 SI discovery: HTML landing pages, publisher links, retry evidence

## Intake and verified premise

Base: origin/main `0bf2ca6f28c89b420109a421d9acd71d532e4ee3`.
- gr469080: `si_discovery.default_fetch` advertises JSON first on DOI landing
  requests; `discover` unconditionally passes that response to HTML parsing.
- gr469081: no Springer MOESM/MDPI s1/AIP/APS explicit patterns; current
  non-PDF skips keep URL but no reason and the paper overview omits them.
- gr465931 (existing, reused): connect/TLS stalls. Main already contains
  all-address SSRF validation and sequential TCP/TLS fallback under a 2T
  budget. Diagnose normal network requests/DNS; do not assume IPv6 cause.
- Native logs for pa207597/pa41972/pa175774/pa160708/pa260875 show earlier
  blocked checks followed by a clean latest si_none. Latest misses=[] is
  correct; cumulative deadline_retries is history. A distinct late-claim
  branch rearms without any outcome/event until cap: fix that lost evidence.

Native Python search/default_fetch source used first. Indexed root `/app`
is distinct from this task worktree; local source reads verify the branch.

## Acceptance / boundaries

1. Landing DOI fetch sends text/html; Figshare/Crossref/handle API requests
   send JSON. Offline transport regression fails first. 403/challenges
   remain misses, never bypassed.
2. Add only supplement URL patterns backed by a real retrieved primary
   landing-page fixture, starting Springer Nature. Do not guess other
   publishers' URLs. Unsupported zip/xlsx/cif candidates have a named
   non_pdf skip plus URL, visible in paper overview. No non-PDF download or
   ingest. Fixture parser and worker/view regressions fail first.
3. Preserve late-claim deadline miss + event on every rearm, including
   cap; tests fail first. Diagnose pinned family/address and fallback from
   bounded read-only requests. No SSRF relaxation. If network hardening
   needs safe_fetch.py, route its exact guarded command/decision to
   coordinator before changes; do not silently widen scope.

Focused tests/scripts/test, Ruff, scoped typecheck; independent Codex
review and normal coordinator land. No migration, model/provider/compute
jobs or production acquisition before verified deployment. Version/full
suite/release remain coordinator integration gates.

## After deployment

Bind verified runtime SHA, then explicitly re-request exactly the 20
orchestrator-designated empty papers via supported fetch-si. Report input
handles, before/after, found/fetched by publisher in
inbox/ingest-fetch-si-ready.md. Need exact cohort handles from coordinator;
never infer a broad campaign or equate local branch with deployment.

## Branch evidence

Five regressions failed before source edits: wrong Accept, missing MOESM
source-data file, absent late-claim miss, unnamed non-PDF skip, missing UI
skip status. Focused suite then passed 137 tests, including existing strict
SSRF/fallback tests; scoped typecheck passed 8 files; Ruff/diff checks pass.

Real anchor-only fixture: Nature `s41467-023-40259-0`, retrieved 2026-10-05
through ordinary pinned safe_get, supplies SI PDF, peer-review PDF and
source-data XLSX at media.springernature.com's Springer ESM mirror. Parser
retains SI/source data, excludes peer review. MDPI supplied page is HTTP403,
AIP supplied DOI leads to Cloudflare403; no bypass or invented patterns.
Supplied APS page is HTML200 with no supplement links. MDPI/AIP/APS
extensions therefore remain pending accessible representative fixtures.

Normal pinned network probe: Figshare/Crossref returned JSON200 quickly;
DOI HTML request resolved to Springer HTML200 in14.801s. All DNS addresses
and dials were IPv4. The first Springer TCP connect took0.008s, TLS stalled
until10s and logged fallback; next validated address succeeded. Main already
handles this path safely; no safe_fetch.py change is warranted from this
sample. Intermittent TLS egress/endpoint stalls remain an operational issue,
not a demonstrated IPv6 selection bug. Late-claim outcome loss is corrected;
prior-to-latest misses for the five named papers remain in event history.

Non-PDF ingestion is a sensible separate follow-up for typed scientific
data (CIF structures/XLSX tables), with bounded ZIP inspection and format
specific provenance/security rules; never feed arbitrary archives to PDF
extraction. This slice only discovers and reports their URLs/skips.
