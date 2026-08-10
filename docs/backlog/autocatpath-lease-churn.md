# autocatpath ~2.5 h re-lease churn — needs cluster-log evidence

PRECIS_AUTOCATPATH_WALL_SECONDS wiring is confirmed correct end-to-end
(regression in tests/test_quest_compute.py), so the observed re-lease churn
is NOT a dropped value. Get live cluster-log evidence (contention? runs
genuinely outliving a correctly-applied 2.5 h lease?) before raising the
default — don't guess a new number.

Related: autocatpath-seed-wall-overruns.md.
