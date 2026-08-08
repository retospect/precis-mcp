# Re-measure plan_tick after the 57837fa4 prompt cut (win is n=1)

The 40% request_chars cut is confirmed; the performance claim rests on one
post-cut tick (36,395 chars / 6 turns / 25.6 s). Re-run once ~20 post-cut
local ticks accumulate, against the pre-cut healthy baseline (n=8: ~58k
chars, 8.9 turns, ~55 s). Two traps that already bit: don't average over
60-turn exhausted runs (report the median), and don't read a
deploy-straddling window as post-cut (filter `ts` past the deploy or
`request_chars < 45000`). The "planning uses a few % of the spark pair"
capacity estimate moves with this. Mechanical measurement.
