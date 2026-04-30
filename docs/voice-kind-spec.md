# `voice` — local speech ↔ text kind

> Status: **draft spec**. Not yet scheduled. Sized as a single phase, optional
> deps group (`voice`), no new MCP package. Hidden when deps are absent
> (same gating pattern as `markdown` without `PRECIS_MARKDOWN_ROOT`).

## Why

Agents on the cluster need two voice capabilities that don't exist yet:

1. **Transcription** of audio files (voice memos, meeting recordings,
   interviews) into searchable text. Today there's no way to ingest spoken
   knowledge into precis.
2. **Synthesis** of arbitrary text into playable audio so Asa can speak
   replies (Discord attachment, push-to-talk web UI, local `afplay`).

Both must run **locally** — no cloud round-trips, no API keys, no PII
leakage. The cluster has the hardware: melchior's M2 Ultra runs Kokoro on
CPU comfortably and Whisper-large-v3 via Metal; spark's GB10 runs
faster-whisper at 30×+ realtime.

A standalone `voice-mcp` package was considered and rejected — see
"Alternatives considered" below. A precis kind is the right shape because
transcripts are first-class searchable knowledge that should live next to
papers, markdown notes, and conversations.

## Mental model

A `voice` ref binds a transcript to an audio file. The two directions —
TTS (text in, audio out) and STT (audio in, text out) — map cleanly to
precis's existing `get`/`put` semantics:

| direction | verb  | input        | output                  | persistence    |
|-----------|-------|--------------|-------------------------|----------------|
| TTS       | `get` | `q=text`     | audio path + URL        | cache (TTL)    |
| STT       | `put` | `link=file:` | transcript ref slug     | durable ref    |

Synthesis is fully determined by `(text, voice, model, format)` — that's a
cache, not a memory. Transcription produces durable knowledge worth
embedding and indexing — that's a ref. The handler is a hybrid:
cache-backed on `get(q=...)`, ref-backed on `put(link=...)` and
`get(id=slug)`.

## Surface (4 verbs)

```python
# ── TTS: synthesize, cache-backed ────────────────────────────────────
get(kind='voice', q='Hello, world.')
get(kind='voice', q='Hello, world.', view='af_bella')
get(kind='voice', q='Hello.', view='af_bella:mp3')
# → "[audio: /opt/nfs/shared/voice/synth/hello-a3f9.wav, 1.2s]
#    URL: https://melchior.local/voice/synth/hello-a3f9.wav
#    Voice: af_bella · Model: kokoro-v1.0 · Cached"

# ── STT: transcribe and persist ──────────────────────────────────────
put(kind='voice', link='file:/path/to/recording.m4a')
put(kind='voice', link='file:/path/to/meeting.wav',
    tags=['speaker:reto', 'topic:precis'])
# → "voice/shipping-prototype-tomorrow-a3f9
#    47s · en · whisper-large-v3-turbo
#    > I think we should ship the prototype tomorrow…"

# ── Retrieve a stored transcript ─────────────────────────────────────
get(kind='voice', id='shipping-prototype-tomorrow-a3f9')
# → full transcript, segments with timestamps, audio path/URL, metadata

get(kind='voice', id='shipping-prototype-tomorrow-a3f9', view='segments')
# → [{start: 0.0, end: 4.2, text: "I think we should…"}, …]

# ── List ─────────────────────────────────────────────────────────────
get(kind='voice', id='/recent')
get(kind='voice', id='/recent?source=stt')
get(kind='voice', id='/voices')          # available TTS voices

# ── Search transcripts (free, via existing semantic search) ──────────
search(q='ship the prototype', kind='voice')

# ── Re-tag / annotate a transcript ───────────────────────────────────
put(kind='voice', id='shipping-prototype-tomorrow-a3f9',
    tags=['speaker:matthias'], mode='note',
    text='Recorded during 2026-04-28 sync.')

# ── Delete a transcript ──────────────────────────────────────────────
put(kind='voice', id='shipping-prototype-tomorrow-a3f9', mode='delete')
```

### Disambiguation rules

- `q=` present → TTS. `id=` ignored.
- `link=` present → STT. Always creates a new ref (or returns existing one
  if the audio sha256 matches a stored ref).
- `id=` only → ref retrieval.
- `id='/...'` → list view, like other kinds.

This mirrors how `math` already separates `q=` (compute) from `id=` (cached
ref retrieval). No new convention.

### `view=` for TTS

Format: `<voice>[:<format>]`. Examples:

- `view='af_bella'` — voice af_bella, default format (wav)
- `view='af_bella:mp3'` — mp3 output
- `view='am_adam:ogg'` — ogg/opus output

If `view` omitted → `PRECIS_VOICE_DEFAULT` voice, wav format.

### `view=` for STT retrieval

- (none) → human-readable transcript with metadata
- `'segments'` → JSON-ish list of `{start, end, text, speaker?}` for word/segment-level inspection
- `'audio'` → just the audio path + URL (no transcript)

## Storage

### TTS (cache, via `_cache_base.py`)

Cache key: `sha256(text + '|' + voice + '|' + model + '|' + format)[:16]`.

Cache entry meta:

```json
{
  "audio_path": "/opt/nfs/shared/voice/synth/<key>.wav",
  "audio_url":  "https://melchior.local/voice/synth/<key>.wav",
  "audio_sha256": "...",
  "duration_s": 1.2,
  "voice": "af_bella",
  "model": "kokoro-v1.0",
  "format": "wav",
  "text": "Hello, world.",
  "synth_ms": 340
}
```

TTL: 30 days (configurable). On expiry the file is pruned by the launchd
timer; cache row may persist with `audio_unavailable=true` so re-synth
recreates the file with the same key.

### STT (durable ref)

One ref per unique audio file (deduped by `audio_sha256`).

```
refs.kind        = 'voice'
refs.slug        = '<first-5-words-slugified>-<6-hex sha1 of audio>'
refs.title       = first 80 chars of transcript
refs.meta        = {
                     audio_path, audio_url, audio_sha256,
                     duration_s, language, model,
                     source: 'stt',
                     segments: [{start, end, text}, ...]   # full segments inline
                   }

blocks[0].text   = full transcript (one block, embedded)
blocks[0].pos    = 0
```

Embeddings: full transcript via the configured embedder (bge-m3) — same
path as `paper`/`markdown` blocks. Searchable from day one.

Tags (writable via `put(..., tags=[...])`):

- `source:stt` (closed-prefix, auto) — set on every STT ref
- `lang:<code>` (open, auto) — detected language
- `speaker:<name>` (open, user) — manually applied
- `topic:<x>` (open, user) — manually applied

### What's *not* stored

- Audio bytes never go into Postgres. Files live on filesystem.
- No `voice_segments` or `voice_audio_files` table. All metadata fits in
  `meta` JSONB. Same DRY principle as the `python` kind.

## Implementation

### Files

```
src/precis/handlers/voice.py                     # ~400 LOC, hybrid handler
src/precis/voice/__init__.py
src/precis/voice/stt.py                          # faster-whisper wrapper
src/precis/voice/tts.py                          # kokoro-onnx wrapper
src/precis/voice/output.py                       # path/URL helpers, hashing
src/precis/voice/voices.py                       # voice catalog
src/precis/data/skills/precis-voice-help.md
tests/test_voice_handler.py
tests/test_voice_stt.py                          # mocked backend
tests/test_voice_tts.py                          # mocked backend
```

### Optional dependency group

```toml
[project.optional-dependencies]
voice = [
  "faster-whisper>=1.0",
  "kokoro-onnx>=0.4",
  "soundfile>=0.12",
]
```

Same pattern as `flashcards`. Handler hidden via try-import in
`registry.py`. `[all]` pulls it in.

### Backends

- **STT**: `faster-whisper` with `large-v3-turbo` model. Lazy-loaded
  singleton (model loads on first call, ~1.5s, then resident).
  Auto-device: CUDA on Linux+GPU, Metal on Apple Silicon, CPU fallback.
- **TTS**: `kokoro-onnx` (82M params, CPU-only fine on M2 Ultra).
  Lazy-loaded. Voices baked into the ONNX file.

Models live at `PRECIS_VOICE_MODEL_DIR` (default
`~/.cache/precis/voice/models`). Downloaded on first use; bootstrap
script can pre-pull.

### Audio output dir + URL base

```
PRECIS_VOICE_OUTPUT_DIR=/opt/nfs/shared/voice         # cluster default
                                                      # or ~/.cache/precis/voice/out
PRECIS_VOICE_OUTPUT_URL_BASE=https://melchior.local/voice    # optional
```

Layout:

```
$VOICE_OUTPUT_DIR/
├── synth/<key>.{wav,mp3,ogg}     # TTS, TTL-pruned
└── stt/<sha256>.<ext>             # STT inputs (optional copy for replay)
```

If `URL_BASE` is set, every response includes both `audio_path` and
`audio_url`. Else only `audio_path`.

### Nginx serve config (one-liner addition)

```nginx
location /voice/ {
    alias /opt/nfs/shared/voice/;
    autoindex off;
    add_header Cache-Control "public, max-age=86400";
}
```

To be added in `roles/nginx/templates/cluster_proxy.conf.j2`.

### Cleanup

LaunchDaemon timer on melchior:

- `find $VOICE_OUTPUT_DIR/synth/ -mtime +$PRECIS_VOICE_TTL_DAYS -delete`
  (default 30 days)
- STT inputs are kept indefinitely (small, and the user explicitly chose
  to ingest them)

### Path-traversal safety

`put(link='file:...')` validates the path is absolute, exists, and is a
regular file. No relative paths, no symlinks followed. Audio files are
read but **not moved or modified** — the source path stays where the user
put it.

## Configuration summary

```bash
PRECIS_VOICE_MODEL_DIR=~/.cache/precis/voice/models
PRECIS_VOICE_OUTPUT_DIR=~/.cache/precis/voice/out
PRECIS_VOICE_OUTPUT_URL_BASE=                    # optional, enables URL mode
PRECIS_VOICE_DEFAULT=af_bella
PRECIS_VOICE_STT_MODEL=large-v3-turbo
PRECIS_VOICE_TTS_MODEL=kokoro-v1.0
PRECIS_VOICE_DEVICE=auto                          # auto|cuda|metal|cpu
PRECIS_VOICE_TTL_DAYS=30
```

Cluster MCP config snippet:

```json
"precis2": {
  "command": ".../precis-mcp/.venv/bin/precis",
  "args": ["serve"],
  "env": {
    "PRECIS_VOICE_OUTPUT_DIR": "/opt/nfs/shared/voice",
    "PRECIS_VOICE_OUTPUT_URL_BASE": "https://melchior.local/voice",
    "PRECIS_VOICE_DEVICE": "auto"
  }
}
```

## Skill doc

`src/precis/data/skills/precis-voice-help.md` covers:

- The `q=` (synth) vs `link=` (transcribe) vs `id=` (retrieve) trichotomy
- Voice catalog (output of `get(kind='voice', id='/voices')`)
- How to use transcripts: feed into `paper`/`markdown` workflows,
  cite `voice/<slug>` from a tex doc, etc.
- Discord delivery hint: pass the URL to Hermes, which posts it as a
  Discord file attachment (Discord renders an inline player for
  mp3/m4a/ogg/wav)

Surfaced via the synthesized `precis-help` skill index after registry
binding.

## Test coverage

- Handler unit tests with mocked `_synth` and `_transcribe` (no model
  load) — fast, run in CI
- Integration test gated on `PRECIS_VOICE_TEST_MODELS=1` env var that
  actually loads models and round-trips a tiny audio sample (run nightly
  or manually, not in CI)
- Path-traversal tests for `put(link=...)`
- Cache-key stability tests (same input → same key across processes)
- Format coverage (wav/mp3/ogg)
- Re-synth after TTL prune (cache row exists, file missing → regenerate)
- Dedup test: same audio file ingested twice → same ref slug

## Discord / Asa integration (out of scope but documented)

When Asa wants to speak a reply on Discord:

1. Asa calls `precis2.get(kind='voice', q='<reply text>')`
2. Response includes `audio_url`
3. Asa hands the URL to a tiny Hermes skill (~10 LOC) that does
   `requests.get(url)` and posts the bytes via `discord.py` `File()`
4. Discord renders an inline audio player

No voice-channel presence, no Opus encoding, no streaming. Push-to-talk
audio uploads from the user follow the inverse path: Hermes uploads the
attachment to a temp dir, calls `precis2.put(kind='voice',
link='file:/tmp/...')`, gets the transcript, feeds it into the chat
context.

These two ~10-line Hermes-side helpers are the entire "Asa voice" story.
They live in `roles/hermes/templates/skills/` (TBD), not here.

## Alternatives considered

### Standalone `voice-mcp` package

**Rejected.** Pros: reusable from non-precis contexts, isolates heavy
deps. Cons: voice transcripts *are* knowledge — they should be searchable
alongside papers and markdown. A separate MCP forces awkward two-step
flows ("transcribe with voice-mcp, then store in precis"). Optional deps
group solves the heavy-deps concern without the architectural split.

### Two kinds (`tts` + `stt`)

**Rejected.** Both directions describe the *same artifact* (transcript ↔
audio). Splitting them duplicates voice catalog, output dir, model
config, and the skill doc. The `q=` vs `link=` discriminator is already a
clear precis idiom (cf. `math`).

### Storing audio in Postgres bytea

**Rejected.** Bloats the DB, defeats nginx static-serve. Filesystem is
the right place for binary blobs. Postgres holds metadata + transcript +
embedding.

### Server-side voice cloning (XTTS-v2, F5-TTS)

**Out of scope for v1.** Kokoro covers "Asa speaks" with one neutral
voice well. Voice cloning adds GPU dependency, model size (~5GB), and
ethical/consent surface. Add later as `view='clone:<reference_audio>'`
if needed.

### Real-time streaming TTS

**Out of scope.** MCP is request/response. Streaming would need a
separate transport. The 200ms-ish synth latency for a typical reply is
acceptable for "click to play" UX.

## Open questions

1. **Speaker diarization for STT?** WhisperX adds it cheaply; bumps
   transcript quality for meetings. Defer to v2 unless first user case
   needs it.
2. **Auto-language detection vs forced language?** Whisper detects by
   default; forcing via `tags=['lang:de']` on `put` could be a
   resolver hint for ambiguous short clips.
3. **Audio chunking for very long files?** `large-v3-turbo` handles
   long-form fine; faster-whisper has built-in VAD chunking. Probably no
   action needed.
4. **Cache eviction policy.** TTL-only is fine for v1. LRU could come
   later if disk fills.
5. **Voice catalog source of truth.** Bake into `voices.py` constant for
   v1. If we later want voice cloning, this becomes a dynamic registry.

## Build order

1. `voice` handler scaffold + tests with mock backends
2. `_synth` path with kokoro-onnx — verify on melchior
3. `_transcribe` path with faster-whisper — verify against sample wav
4. Skill doc + `precis-voice-help.md`
5. Registry wiring + optional-deps gating
6. Nginx serve config in Ansible (`roles/nginx`)
7. LaunchDaemon TTL prune timer (`roles/precis_voice_cleanup` or fold
   into existing maintenance role)
8. End-to-end live test on cluster: Asa says "hello" via Discord
   attachment

Estimated size: ~1 phase, comparable to `flashcard` + the cache hookup
that `youtube` already exemplifies.
