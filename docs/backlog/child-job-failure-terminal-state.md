# Failed child jobs park their auto_check parent forever

`src/precis/workers/auto_check_evaluators/child_job_succeeded.py::evaluate`
has no failed-child verdict (pending forever), and
`src/precis/handlers/_job_bubble.py::bubble_job_failure` retries only
`swept:claim-orphaned` — every other failure latches `child-failed:<job_id>`
permanently. 102 of the 110 stuck auto_check leaves trace here (gr192371
counts 18 — reconcile scopes before closing either). Decided (Reto): retries
first (`swept:wall-timeout` joins the retry-eligible set, bounded), then a
visible non-success terminal state (evaluator third verdict + a terminal
auto_check status surfaced in the attention view) as backstop — never terminal
without retries. Do NOT fix at the visibility layer alone (masking risk).
Blast radius: any `child_job_succeeded` auto_check, precis-dft's `gpaw_relax`
included. Related ops: un-deadlock the 07-25 parents stuck on dead children;
gr187627 (`autocatpath_aggregate` still on the legacy blocking dispatch).

test: regression on `child_job_succeeded` evaluation with a failed child.
