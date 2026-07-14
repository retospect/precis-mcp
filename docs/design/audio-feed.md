# Audio feed + producers — design-of-record & handoff

The "pipe audio to the phone" surface, and how to wire a **producer** onto it
(e.g. the news briefing → a daily audio brief). Built 2026-07-14; the feed +
the voice-draft producer are LIVE.

## The three layers

1. **Feed (delivery).** `precis.audio_feed` (pure) + `precis_web.routes.podcast`.
   - `publish_episode(dir, audio_path, *, episode_id, title, description,
     published_at, duration_seconds=None, source=None) -> Episode` — copies the
     audio in + writes a JSON sidecar. This is the **one call every producer
     makes.** CLI wrapper: `precis podcast add <audio> --title … --source … [--dir]`.
   - `precis_web` serves `GET /podcast/feed.xml` (RSS 2.0) + `GET /podcast/audio/{id}`.
   - **Dir is the shared NAS** (`finnmaccool:/Volume1/botshome/podcast`):
     `/opt/nas/botshome/podcast` on the Mac gateway (melchior serves it),
     `/nas/botshome/podcast` on spark (producers write it). So a producer on ANY
     node publishes and the gateway serves — no cross-host copy.
   - Config: `PRECIS_PODCAST_DIR`, `PRECIS_PODCAST_BASE_URL`
     (`https://melchior.tailded4cf.ts.net` — the Tailscale-serve origin).

2. **TTS (text → audio).** `precis.export.audio.Synthesizer` seam +
   `precis.tts.kokoro.KokoroSynth` (kokoro-onnx, the `[tts]` extra).
   `KokoroSynth().synthesize(text, *, voice, lang) -> (float32 samples, sr)`.
   Voices: `af_heart` (default) + af/am/bf/bm + other langs; `speed` is the one
   native knob; `lang` = `en-us`/`en-gb`/… per voice.

3. **Producer (content → text → audio → publish).** The voice-draft producer is
   the reference: `precis draft audio <slug> [--voice/--lang/--speed/--publish]`
   → narration layer (`precis.draft.narrate`) → Kokoro → m4a → `publish_episode`.

## The news-briefing producer (BUILT — the first automatic producer)

**Done** — `precis.workers.briefing_audio.run_briefing_audio`. It's the
reference for a *prose* (non-draft) producer, and shows the decoupling every
TTS producer needs.

- **Decoupled from the briefing *job*.** The news briefing runs in-process on
  the **agent** worker (melchior, `claude_inproc`) and persists a dated
  `briefing-<date>` `news` ref. TTS lives only on the `[tts]` host (spark), so
  the audio can't ride that job. Instead a separate pass on the TTS host reads
  the persisted ref and narrates it. **This is the pattern for any producer
  whose content is made where TTS isn't.**
- **Self-scheduling, no new cron.** It's a worker ref-pass
  (`--only briefing_audio`, gated `PRECIS_BRIEFING_AUDIO_ENABLED`, default-OFF)
  that fires off the *existence* of an un-narrated briefing — the system worker
  already loops on spark, so enabling the flag there is the whole install.
- **The path** (reusable pieces):
  1. Find the latest `briefing`-tagged `news` ref with no `meta.audio_episode_id`.
  2. Reconstruct its markdown from body chunks → `narrate.markdown_segments`
     (link → anchor text, headings → longer pause, URLs/markup dropped, lexicon
     applied). *Prose path*, distinct from the draft `render_narration`.
  3. `export.audio.synthesize_text(segments, wav, synth=KokoroSynth())` — the
     shared stitch loop (factored out of `export_audio`).
  4. `ffmpeg` WAV → m4a (falls back to publishing the WAV if ffmpeg is absent).
  5. `audio_feed.publish_episode(PODCAST_DIR, m4a, episode_id=f"news-{date}",
     …, source="news")`, then stamp `meta.audio_episode_id` on the ref
     (idempotency — a re-tick or a second TTS host can't double-publish).
- **Deploy:** on spark's **system** worker set `PRECIS_BRIEFING_AUDIO_ENABLED=1`
  + `PRECIS_PODCAST_DIR=/nas/botshome/podcast` (+ the Kokoro model env it already
  has). Manual smoke: `PRECIS_PODCAST_DIR=… precis worker --only briefing_audio
  --once`. A dry render (no publish/marker) is `run_briefing_audio(publish=False)`.

**Wiring the *next* producer** (a knowledge brief, "read me this paper"): copy
`briefing_audio` — build segments (`markdown_segments` for prose, or per-chunk
for a draft) → `synthesize_text` → m4a → `publish_episode`. Decouple TTS onto the
`[tts]` host exactly as above whenever content is produced elsewhere.

## Where it runs (the TTS host = spark)

spark (Linux/ARM64, GB10 GPU but CPU is plenty) is set up:
- `precis-mcp[tts]@main` in `/opt/precis/kokoro-venv` (also has kokoro-onnx +
  soundfile); model files at `/opt/precis/kokoro-venv/models/{kokoro-v1.0.onnx,
  voices-v1.0.bin}`; `espeak-ng` + `ffmpeg` apt-installed.
- Env a producer needs: `PRECIS_KOKORO_MODEL`, `PRECIS_KOKORO_VOICES`,
  `PRECIS_DATABASE_URL` (source it from the running worker's `/proc/<pid>/environ`
  — don't hardcode the DSN), `PRECIS_PODCAST_DIR=/nas/botshome/podcast`.

**Scheduling:** a `cron-tick` pass (same lane as the Discord news briefing) that
runs the producer once a morning. Keep the TTS engine behind the `Synthesizer`
seam so say→Kokoro→other is a swap.

## Consumption

Subscribe **on-device** (over Tailscale): Apple Podcasts *Add a Show by URL* /
Downcast → `https://melchior.tailded4cf.ts.net/podcast/feed.xml`. **NOT**
Overcast/Pocket Casts — they fetch feeds server-side (public internet) and can't
reach the tailnet. Phone must have Tailscale connected.

## Not-yet-done (see OPEN-ITEMS)

- **LaTeX → speech** for math-heavy drafts (`math_speech` mode).
- **Term/glossary pronunciation** — the personal + per-draft lexicon shipped
  (`narrate.resolve_lexicon`, skill `precis-audio-help`); still missing is a
  `pronunciation` attribute on the term registry (ADR 0052) and an inline
  per-occurrence override for homographs.

**Shipped since first draft:** the two-level pronunciation lexicon
(`resolve_lexicon`, personal `PRECIS_LEXICON_FILE` + per-draft
`meta.pronunciation`); `export.audio.synthesize_text` (the factored stitch loop
for non-draft producers); and the **news-briefing audio producer** above.
