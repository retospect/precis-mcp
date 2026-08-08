# topic-dossiers

## Residuals (from OPEN-ITEMS)

- Weave-quest creation flow: nothing creates one end-to-end (mint quest +
  ensure_dossier + topic: tag + mark_weave_quest); the ADR-0060
  synthesis-tick path is the home.
- Weave v1 refinements: multi-place (top-1 only); claim-clustering dedup;
  review-todos parented on the quest lack a level:strategic ancestor.
- Stamp `topic:<slug>` on the dossier draft at creation/quest-binding — until
  then `view='integration'` gap lists need a manual tag.
- Synthesis tick body for topic-quests (`workers/job_types/quest_tick.py`):
  harvest unintegrated papers → merge into the dossier → log → link; decide
  whether noxrr adopts it or stays active-search-driven.
- Weekly digest cast + a quiet daily-brief lane. Reto's open design musings:
  weekly "new papers" front-matter vs a running changelog vs an
  eye-focus-like view of paras touched in the last week (he likes the
  hierarchical view).
- Rungs 7 (weekly/deep review + batching) and 8 (freshness + digest +
  contradiction/re-org) remain design-of-record.
