# Flashcard-cast — build prompt for a fresh session

Reto asked (2026-07-26) for a **separate flashcard briefing**, a sibling to the
morning reading cast + evening meditation cast. Paste the block below into a
fresh session to build it. Reuse the cast spine; do not rebuild it.

```
Build a FLASHCARD CAST — a standalone daily audio briefing focused entirely on
spaced-repetition review, as a sibling to the existing reading cast (morning brief)
and meditation cast (evening nidra). REUSE the cast spine; do not rebuild it.

Architecture to model on (read these first):
- Cast spine: src/precis/reading/cast_common.py (create_cast_draft, meta.cast/voice)
  → src/precis/workers/cast_audio.py (narrate → TTS → publish) → src/precis/audio_feed.py.
- Existing casts to mirror: src/precis/reading/briefing_cast.py (meta.cast='reading')
  and src/precis/reading/meditation.py (evening nidra).
- Trigger: a `level:recurring` todo in the prod DB (meta.schedule.cron,
  meta.executor='claude_inproc', meta.job_type=...) mints a claude_inproc job;
  the job_type lives in src/precis/workers/job_types/ (mirror reading_brief.py).
  These run on melchior only; cast_audio narrates on spark.
- Flashcard DATA source already exists: briefing_cast._lane_recall (briefing_cast.py:436)
  gathers anki leeches, card_forge-minted, escalated/new concepts; anki `meta.fields`
  (Text / Back Extra) carry the card bodies.

Build:
1. src/precis/reading/flashcard_cast.py — compose a spoken review script from today's
   due/leech anki cards. For each card, TEACH it (answer + reasoning + the common
   mistake + an adjacent hook), don't recite Q/A — mirror briefing_cast's depth-first
   _MORNING_CONTRACT teaching style. Compose via router DispatchClient (tier: BIG or
   FRONTIER for teaching quality). Write a dated draft via create_cast_draft with a
   distinct meta.cast='flashcard' + its own voice.
2. A flashcard_brief job_type in workers/job_types/, registered in the claude_inproc
   dispatch, mirroring reading_brief.
3. Confirm cast_audio's newest-cast-draft selector picks up meta.cast='flashcard'
   (make it cast-agnostic if needed). Decide: shared podcast feed vs a separate one.
4. Provide the put(kind='todo', level='recurring', meta.schedule.cron=...) call to seed
   the schedule (pick a time distinct from the 06:00 morning brief) — for Reto to run.
5. Tests mirroring the reading-cast tests (lane assembly, compose contract, draft creation).

Scope: a NEW sibling cast. Do NOT modify the reading or meditation casts. Confirm
tier/voice/schedule/feed with Reto if ambiguous.
```
