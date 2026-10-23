# precis-tts — the audio / text-to-speech tool image

A per-tool container (sibling of `docker/aizynth`) that renders a **voice-score**
to an audio file. Follows the established dockerized-tool pattern: **code + env +
models** baked, built **on the `tts_render` topology node** (melchior — `docker
build` via colima; `podman build` on a Linux node), driven by a **one-shot
`docker`/`podman run` per episode** that stages input/output over a bind mount.
The precis worker never needs the `[tts]` extra — it shells out.

## What's inside

- **Kokoro** (`kokoro-onnx`) — all 9 languages, Apache, CPU-fine.
- **misaki[zh,ja]** — the G2P Kokoro's Mandarin/Japanese voices were *trained on*
  (espeak phonemes mismatch → rough zh/ja). `KokoroSynth` routes `lang ∈ {cmn,ja}`
  through misaki (`is_phonemes=True`) and falls back to espeak if misaki is absent.
- **espeak-ng** — phonemizer for the other languages. **ffmpeg** — the one mp3
  encode after stitching (mp3 plays everywhere, incl. Apple/iOS, so a shared
  enclosure just works).
- **Baked model files** (~330 MB) — self-contained, no NAS mount at run (the
  chosen trade vs aizynth's NAS-mounted models).

## Build (on the `tts_render` node)

    docker build -t precis-tts:<sha> --build-arg PRECIS_REF=<sha> docker/tts
    # (podman build … on a Linux node)

`PRECIS_REF` pins the precis-mcp version installed into the image; tag the image
with the same sha (the digest is the natural cache key, per the aizynth pattern).

Normally you never run this by hand: `redeploy-precis.yml` imports
`playbooks/45-tts.yml` with `tts_precis_ref` pinned to the deploy sha, so every
`scripts/deploy` rebuilds the image in lockstep with the venvs. The Dockerfile
keeps everything sha-independent (deps, misaki dicts, models) in layers above
the pinned install, so that per-deploy rebuild is one pip install.

## Run (one-shot per episode)

    docker run --rm \
      -v <scratch>/in:/work/in:ro -v <scratch>/out:/work/out \
      precis-tts:<sha>
    # reads /work/in/segments.json -> writes /work/out/out.mp3 (+ result.json)

`segments.json` is the voice-score the worker builds with `render_narration`
(draft) or `markdown_segments` (briefing):

    {"segments": [{"text": "Hello.",  "voice": "af_heart",    "lang": "en-us", "kind": "para"},
                  {"text": "你好世界", "voice": "zf_xiaoxiao", "lang": "cmn",   "kind": "para"}],
     "speed": 1.0}

## Smoke-test cn/jp by ear (before wiring)

Because the shim is self-contained, you can hear misaki quality directly:

    mkdir -p /tmp/tts/in /tmp/tts/out
    cat > /tmp/tts/in/segments.json <<'EOF'
    {"segments":[{"text":"你好世界，这是一个测试。","voice":"zf_xiaoxiao","lang":"cmn","kind":"para"},
                 {"text":"こんにちは、これはテストです。","voice":"jf_alpha","lang":"ja","kind":"para"}]}
    EOF
    docker run --rm -v /tmp/tts/in:/work/in:ro -v /tmp/tts/out:/work/out precis-tts:<sha>
    # play /tmp/tts/out/out.mp3

## Mix-and-match (future)

The `Synthesizer` seam + per-segment stitch make a **`lang → engine` router**
natural: route `cmn`/`ja` to a heavier engine (MeloTTS, VOICEVOX, CosyVoice) if
misaki isn't good enough, keep the rest on Kokoro, stitch the outputs. `_stitch`
already **resamples every segment up to a common sample rate** before concat, so
mixing 24 kHz + 44.1 kHz engines is safe. Add engines inside this image (or as
sidecars) and register them per-lang.

## Wiring (landed)

1. **precis-side driver** — `render_via_container(segments, out_audio, *, image,
   scratch)` in `src/precis/tts/render.py` writes `segments.json`, runs
   `docker run precis-tts`, returns the audio; `briefing_audio` / `cast_audio` /
   `precis draft audio` prefer it when `PRECIS_TTS_IMAGE` is set (else the
   in-process `KokoroSynth`).
2. **`roles/tts`** (cluster) — topology-driven (`precis_capabilities.tts_render`
   in the overlay's `topology.yml`, currently melchior): ensures the container
   runtime, builds the image (flag-gated), worker env (`PRECIS_TTS_IMAGE`).
   Works on Linux (systemd drop-in) and the macOS gateway (colima docker).
