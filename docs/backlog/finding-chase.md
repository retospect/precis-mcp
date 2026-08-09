# Finding chase — trace cited claims back to their primary source

Shipped portion: see the `precis.workers` package docstring and
`src/precis/handlers/finding.py` / `src/precis/workers/chase.py`
module docstrings; full design + shipped-slice ledger in git history
(`v8.1.0 — finding-chase`, C0–C10 audited done 2026-06-05). Live:
`kind='finding'` (deterministic `make_finding_paper_id` dedup),
the `--only chase` sibling worker (hop advance, stubs by
`pdf_sha256 IS NULL`, cycle/dead-chain/multi-candidate states,
chain-snapshot card re-emit), `precis resolve` / `precis stats
--findings|--stubs`, LLM hooks behind `PRECIS_CHASE_LLM=1`,
retraction propagation (`Store.set_retraction_status`), and the
`edit(..., pick_candidate=...)` disambiguation verb. Related ADRs:
0017, 0018. Skill: `precis-finding-help`.

Owner anchors: `src/precis/handlers/finding.py`,
`src/precis/workers/chase.py`, `src/precis/identity.py::make_finding_paper_id`.

## Open scope

1. **Verification gate on `STATUS:established`** — lenient vs
   strict. Recommendation (undecided): lenient — chase always
   establishes; verification stays a separate axis
   (`human_verified_at`), and a future `STATUS:verified` tag set by
   `precis verify <finding>` can layer on top. Strict (every chain
   ref human-verified) would zero the `:established` corpus on day
   one.
2. **`claude -p` at ingest time** (queued, deliberately NOT shipped
   with chase; defer until the deterministic chase's failure modes
   are observed):
   - Q1 — structured fact extraction (`paper_facts` table; ADR 0018
     path-3): per body chunk, `(value, unit, claim, conditions)`
     rows complementing `chunks.numerics`.
   - Q2 — LLM abstract / TL;DR per paper (better card text than
     RAKE).
   - Q3 — setup-context extraction from methods sections
     (scope-filtered search without caller-supplied `scope=`).
3. **Generic retraction query** — "papers I cite that have been
   retracted" over the `links` graph (broader than the shipped
   finding-chain propagation; useful for memo/quest kinds).
4. **Stub enrichment — deferred until measured.** Do NOT build a
   background enrichment worker preemptively: stub identity needs
   only `title + DOI + year` (one S2 call); richer metadata arrives
   more reliably via `precis_add` when the PDF lands. If a real
   need surfaces, register against the dormant
   `artifact_kinds(slug='resolve_citation:s2')` seed.
5. **Taproot-bridge residual (live watch item).** The
   forward-bridge pilot (`workers/chase.py::_taproot_bridge`) is
   LIVE on melchior's system worker; fingerprint = `links` rows with
   `set_by='chase'` (baseline 0). It only fires when a finding
   establishes — with zero `STATUS:tracing` inflow it produces
   nothing, and canonical claim hubs are excluded from the outbound
   chase, so evidence-empty hubs are NOT self-filled (needs the
   taproot backfill — see `taproot.md`). Disable: flip the
   `precis_worker_taproot_chase` host_var + redeploy.

## Decided constraints (keep — they bound future work)

- Finding → finding chains are disallowed: a `derived-from` chain
  terminates at a paper. Combining findings into inferences is a
  separate future skill.
- Caveats with `cited_others` do NOT auto-spawn sibling findings
  (auto-branching is exponential and noisy; the user spawns by
  hand).
- No max hop cap — cycle protection covers pathological chains.
- Mis-citation is a `misattributes` relation, not a table.
- Retraction *checking* is out of scope for chase (provenance
  worker owns it); chase only re-grades on propagation.
