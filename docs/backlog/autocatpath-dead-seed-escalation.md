# A terminally failed seed subtree never escalates to the retry lane

A seed job that exhausts its attempt cap leaves its seed todo's
`child_job_succeeded` auto_check permanently unsatisfied, so `T_agg` never
becomes dispatchable and no `autocatpath_aggregate` job exists — the ADR 0064
§C retry lane (`src/precis/quest/compute.py::harvest_measures` watches
explore/aggregate jobs) sees nothing; the candidate waits forever with no
retry, gripe, or rule-out. Decide the escalation signal for a dead seed
subtree: treat "all seed todos terminal but not all done" as a failed barrier
eval feeding the §C ladder, or a nursery detector on aged undispatchable
`T_agg` trees. Needs design.
