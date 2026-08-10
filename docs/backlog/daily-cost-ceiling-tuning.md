# PRECIS_DAILY_COST_CEILING sits at the noise floor — pick real numbers

Deploy-var tuning (Reto), not code: the deployed cap is $50 and an observed
ordinary day was $50.23, so a normal day parks the planner and (post-fix) the
reviewers. Two knobs, deliberately different currencies:
`PRECIS_DAILY_COST_CEILING` includes notional OAuth dollars (~93% of total) —
raise with real headroom; `PRECIS_BUDGET_DAILY_USD` is real money only
(~$4/day at its $20 default) — set explicitly.
`docs/reference/config-variables.md` had the relationship backwards and was
corrected. (The OAuth-lane quota gate itself shipped: `workers/quota_check.py`
populates the snapshot `budget/quota.py::evaluate` reads.)

test: a normal day's recorded 24 h spend sits comfortably under the cap.
