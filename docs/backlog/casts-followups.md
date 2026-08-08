# Daily audio casts — follow-ups

Reading-brief + nidra casts are live; these polish/feature follow-ups remain.

- Length calibration: the morning brief lands ~13 min vs a 20-min target
  despite the word-count contract line — segment it (nidra's per-segment
  budget hits its 45-min target) or add a stronger length floor.
- Wire the quest lane (`briefing_cast._lane_quest` is a stub; surface
  per-quest momentum + recent deeds; nidra could bias toward active-quest
  concepts) (td161129).
- Booklet lane: upgrade past the "where you left off" interim once
  reading-prep slice 2 lands; can migrate onto `refs.last_viewed_at`.
- Add a cluster-status lane (Reto want; today only open-alerts leak in).
- Hygiene: `meta.no_index` and/or retention GC for daily cast drafts; remove
  leftover test drafts (cast-nidra-test-546c21, nidra-test-546c21).
- TTS normalization: decide whether casts keep authoring TTS-friendly text
  ("Mof", "thousandfivehundred") or write normally and pipe through a
  code/LLM filter; is there a chemistry-to-IPA helper? (Reto)
Owner `src/precis/reading/`, `src/precis/workers/cast_audio.py`.
