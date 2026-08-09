# Patent authoring — the freedom-to-operate writing loop

> The **dynamic authoring loop** on top of the static patent genre in
> [`patent-drafting-merge.md`](patent-drafting-merge.md) (what a patent
> draft *is*). This loop: sweep prior art → ingest → sync description →
> draft claims against a freedom-to-operate view → log scoping
> decisions.

Shipped portion: see the `workers/patent_digest.py` and
`handlers/_patent_claims.py` module docstrings; full proposal + slice
ledger in git history. Live: claim-chunk marking at ingest
(`view='claims'`/`'description'`), `doc_type=patent` as a first-class
`Workspace` field, the comprehensive claims-digest working set
(auto-refreshed before prompt assembly on patent ticks), the
scoping-decision ledger via the `plan` kind (auto-injected outline),
the patent planner branch (sweep→ingest→sync→claim→log), LaTeX + docx
export genre switch (in-text per-authority patent cites, no
bibliography; IDS stays a separate view), and claim-family grouping in
document order.

## Decided constraints

- The loop lives on `plan_tick`, keyed by `doc_type=patent` — never a
  new coroutine (reuses guardrails, cost caps, child/yield, live
  working-set injection).
- Prior-art pull is agent-driven with a per-tick budget; the tick
  reports what it ingested.
- The claims view is **comprehensive, not a decaying fisheye** —
  independent claims are never dropped; dependents compress under
  budget.
- The ledger is retained reasoning that never exports (`plan`); the
  others'-claims set is retained in `meta.working_set`.

## Open scope

- **Visual claim-family tree render** (nested dependents under their
  independent — needs a custom render surface) and the **interactive
  web claims view** (`/patent/<slug>`; the same working set feeds
  both). Owner: `precis_web/routes/`.
- **Backfill the ~101 already-ingested patents** with claim markers —
  needs raw-XML-on-disk (or OPS re-fetch) on the cluster; new sweeps
  self-mark, so completeness-only.
- **End-to-end validation** on a real `doc_type=patent` draft: sweep +
  ingest prior art (needs `PRECIS_PATENT_RAW_ROOT` + EPO OPS on the
  executor) → iterate description → claims with the FTO working_set →
  scoping decision → export. Watch the patent-ingest gate on the agent
  host + surname extraction on non-comma bylines.
- **Reto wants:** run the drafting mostly on local models; prep/check
  the panel screw holder device; find/add the supplemental filing
  documents so EU/US/CN filing gets pushbutton at reasonable cost.

## Open questions

1. **`patent_ingested` wait-gate** — build it, or accept the
   ~1-minute embed lag and rely on raw claim text in-tick? (Leaning:
   skip until the lag actually bites.)
2. **Dependent-claim compression** past budget — per-patent gist, or
   independents-only with a count marker? Must never imply
   completeness it doesn't have.
3. **US vs EP claim register** — one `doc_type` with a jurisdiction
   sub-flag, or two? (Citation strings are already per-authority in
   `export/_patent_cite.py`.)
4. **Ledger → issues** — should a blocked-scope decision optionally
   open an ADR-0037 §3b issue to the inventor ("we scoped around
   US…claim 7 — accept the narrowing?") rather than only logging?
