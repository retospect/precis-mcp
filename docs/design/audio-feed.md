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

## Wiring a NEW producer (e.g. the news briefing)

The briefing is **plain text**, not a draft, so you don't want the draft
narration layer. The path is:

1. Get the briefing text (the daily-news pass already produces it for Discord).
2. **Synthesize.** Split into a few segments (paragraphs) and call
   `KokoroSynth().synthesize(seg, voice="af_heart", lang="en-us")` per segment;
   concatenate the float32 arrays with short silence gaps; `soundfile.write` a
   WAV. (Recommended: add a tiny `precis.export.audio.synthesize_text(text,
   synth, out_wav)` helper so producers don't re-roll the concat — the
   draft path's stitching in `export_audio` is the template; factor the
   segment-loop out.)
3. **WAV → m4a:** `ffmpeg -y -i x.wav -c:a aac -b:a 96k x.m4a`.
4. **Publish:** `audio_feed.publish_episode(PODCAST_DIR, "x.m4a",
   episode_id=f"news-{YYYYMMDD}", title=…, description=…, published_at=now,
   duration_seconds=…, source="news")` — or shell `precis podcast add`.

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
- **Pronunciation lexicon** is a *code hook* only (`render_narration(lexicon=…)`
  honors a dict) — no storage/authoring/skill yet. Home: a `pronunciation`
  attribute on the term registry (ADR 0052) + a personal lexicon + a skill.
- `synthesize_text` helper for non-draft producers (factor out of `export_audio`).
