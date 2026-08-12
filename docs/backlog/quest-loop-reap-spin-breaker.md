# Quest loop — anti-spin breaker for consecutive reaped ticks

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
