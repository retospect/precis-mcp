# Quest loop safety

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Quest loop — anti-spin breaker for consecutive reaped ticks

_Grouped 2026-09-26; was `quest-loop-reap-spin-breaker`._

When every quest_tick a loop mints ends reaped/cancelled (the gr204309
pattern: 328 reaps for q164903 over 3 weeks, mint→claim→reap→re-mint,
zero successful ticks), `reconcile_quest_loops` re-mints forever with no
breaker — burning coordinator slots and flooding ref_events. The lease
root cause is fixed (coordinator keepalive + reaper evidence-of-life
guard, gr204309), but the loop still has no "N consecutive terminal
ticks without a completed slice → cool the quest + surface an alert"
circuit. Design first: what counts as a consecutive failure (loop-reaped
events? cancelled+failed?), how a cooled loop resumes (human tag? next
quest edit? timer?), and how it composes with anti-spin v2's existing
cooling. Owner `src/precis/quest/loop.py::reconcile_quest_loops`.

## quest: warn loudly when rubric_objectives reference measures nothing produces

_Grouped 2026-09-26; was `quest-rubric-unproducible-objectives-warning`._

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
