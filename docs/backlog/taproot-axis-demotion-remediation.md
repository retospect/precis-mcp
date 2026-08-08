# axis:taproot demotion — fleet remediation + secondary defects

The demotion race is fixed (04628e8c); remaining: per-claim triage of the 8
demoted DNA-origami/nanozyme findings (fi176420 176447 176451 176820 176871
177633 178235 178237) — restore `TAPROOT:claim` where the rubric passes
(fi176451's mechanism claim clearly does), leave demoted + de-cite where
meta-prose; check which draft cites them first. Secondary defect gr191953:
`src/precis/taproot/backfill.py::apply_chunk` rewrites prose to an `[fi]`
cite even when `attach_evidence` raised. Deeper options still open: stop the
axis pass labeling active-lifecycle findings, or wire classification → real
`mint_hub`. Owner `src/precis/workers/axis_pass.py::_claim_ref`.

test: run_axis_pass(axis_id='taproot') over a live TAPROOT:claim hub skips
it, not reclassifies.
