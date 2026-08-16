# quest: warn loudly when rubric_objectives reference measures nothing produces

Found live on qu164903 (2026-08-16): its `meta.rubric_objectives` was switched
to the CHE electro axes (`span_at_Uopt`/`U_L_abs`/`P_side`) but **zero**
structures in all of prod have ever carried those keys — even today's
autocatpath-0.14.0 aggregates emit only `span`/`barrier`/`selectivity_margin`/
`poison_margin`/`trap_margin`. `pareto_split` requires every rubric key, so all
36 candidates (23 with trusted barriers) sat "awaiting a sim" and the frontier
rendered empty for days with no signal anywhere. `catalyst_seed.py`'s docstring
already names this failure mode ("declaring an objective nothing produces would
leave every candidate unevaluated") but nothing detects it at runtime.

Fix shape: at frontier-assembly (or tick-prompt) time, when an objective key is
present on ZERO candidates that have ≥1 other measure, surface a warning on the
frontier/leaderboard views and in the tick prompt ("objective `span_at_Uopt`
has never been measured on any candidate — frontier cannot populate"). Maybe
also `precis quest doctor`. Owner anchor: `precis.quest.frontier.pareto_split`
/ `quest_frontier`; test: quest with a never-produced rubric key renders the
warning in `view='frontier'` + tick prompt.
