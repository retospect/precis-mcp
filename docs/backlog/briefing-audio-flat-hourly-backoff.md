# briefing_audio.py uses flat-hourly retry backoff — same latency class as cast_audio bug just fixed

## What and why

The session that shipped `66e7d9a2` fixed `src/precis/workers/cast_audio.py` so that:
1. A SIGTERM-collateral-killed TTS render no longer escalates the retry backoff.
2. Failures use exponential backoff from 2min capped at 60min.

The residual: `src/precis/workers/briefing_audio.py` (the *news* briefing audio path, distinct from the cast_audio path) has its own retry/backoff logic that is **flat-hourly**, not the same exponential-from-2min scheme. It's in the same latency class — a killed or failed render there can add up to an hour of latency per attempt, and it has no equivalent of the killed-vs-failed distinction that cast_audio now makes.

## Fix direction

Factor the cast_audio backoff policy (killed-render no-escalate + exponential 2min→60min via `_FAIL_BACKOFF_MINUTES` and the `SIGNAL_KILL_RETURNCODES` distinction in `src/precis/tts/render.py`) into something briefing_audio can share, rather than duplicating. Verify the current briefing_audio behavior by reading the file first — confirm the flat-hourly claim against the shipped code before acting.

## Owner

`src/precis/workers/briefing_audio.py`

## Priority

Medium — it's a latency/reliability paper-cut, not a correctness-critical or data-loss bug.
