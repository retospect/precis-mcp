# Literature-only quests: set the compute-lane switch + trim the prompt

> Mechanism shipped 2026-08-28: `meta.compute_lane == "off"` makes every
> armed tick reason-only — `workers/job_types/quest_tick.py::
> _quest_compute_enabled`, read fresh each slice, threads
> `run_quest_tick(compute=False)`. That kills the real cost (no candidate
> materialisation, no relax/autocatpath dispatch, no searches). Remaining:

## 1. Backfill the key on declared-literature quests

Quest 202469's dossier declares "no compute lane, no simulations" in prose;
nothing set `meta.compute_lane`. Set it to `"off"` there (and on any other
quest whose dossier declares a mode) — the switch only exists where an
operator flips it.

## 2. Reason-only prompt still solicits proposals

With compute off the tick prompt still carries the full proposal menu
(`add_atom_site` etc.), so the model may spend tick tokens writing proposals
that are never materialised. Harmless to the cluster, wasteful to the tick
budget. Gate the proposal-menu section of the prompt on the same switch —
in `precis.quest.tick`, before the proposal step, per the original analysis.

## Verify

A `compute_lane=off` quest should log no `rejected proposal` observations and
mint no `relax` / `autocatpath` jobs:

```
ssh spark "grep -c 'rejected proposal' /var/log/precis-worker.log"
```
