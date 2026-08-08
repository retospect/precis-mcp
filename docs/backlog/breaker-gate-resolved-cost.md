# Budget breaker: gate on resolved transport cost, not tier band

`bands._TIER_BANDS[SMALL]=FREE`, so `breaker.gate_tier` never gates SMALL —
but all-remote SMALL resolves to a paid OpenRouter model at the highest
volume of any tier (~6.7k calls/24 h): a tripped cap pauses
BIG/MEDIUM/FRONTIER while SMALL keeps spending. Symmetric to the shipped
e6e02d7a fix (paid-band tier on a free-local rung exempt). Clean fix: drop
`is_paid(tier)` as the gate determinant; pass
`local=not _rung_is_cloud(rung0)` as the sole signal. Its own cycle — it
removes the is_paid assumption baked into bands.py/breaker.py; spend-check
first whether SMALL's remote $ is actually material. Owner
`src/precis/budget/bands.py`, `breaker.py` + `src/precis/utils/llm/router.py`.
