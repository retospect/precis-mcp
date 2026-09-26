# Cast followups

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Daily audio casts — follow-ups

_Grouped 2026-09-26; was `casts-followups`._

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

## TTS render is collateral damage of any worker restart

_Grouped 2026-09-26; was `cast-render-detach`._

The TTS container runs inside precis-worker.service's cgroup, so a deploy or
jetsam cull SIGTERMs a ~10-minute render mid-flight (exit 143). Exponential
backoff (shipped) makes that cheap, not absent. If episodes keep getting
lost, detach the render (`systemd-run --scope` or `docker run -d` + poll) so
it survives its parent — deliberately not done yet to avoid a supervision
path to get wrong. Owner `src/precis/tts/render.py`.

## LaTeX → speech for voice drafts

_Grouped 2026-09-26; was `latex-speech-narration`._

`speakable()` skips math (a spoken "equation" cue, drops inline $…$) — weak
for math-heavy drafts. Add math_speech ∈ {skip, brief, full}: v1 = a
pure-Python heuristic (^ → "to the power of", \frac → "over", greek,
operators); accessibility-grade = MathSpeak/ClearSpeak via the Speech Rule
Engine over MathML (latex2mathml in hand; MathML→speech is a node shell-out);
per-equation author override (pronunciation-lexicon pattern). Default stays
brief. Owner `src/precis/draft/narrate.py`.
