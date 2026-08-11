# precis-tts — the audio / text-to-speech tool image (one-shot per episode).
#
# Sibling of docker/aizynth: a per-tool image the compute node builds and the
# worker drives with a one-shot `podman run` per job. Bundles:
#   - Kokoro (kokoro-onnx) — all 9 languages, Apache-licensed, CPU-fine;
#   - misaki[zh,ja]        — the G2P Kokoro's Mandarin/Japanese voices were
#                            TRAINED on (espeak phonemes mismatch → rough zh/ja),
#                            so cn/jp come out native;
#   - espeak-ng            — phonemizer for the other languages;
#   - ffmpeg               — the WAV -> m4a encode (once, after stitching).
#
# Per the "bake the models" decision the ~330 MB Kokoro model files are baked in
# (self-contained image, no NAS mount at run) — unlike aizynth, which mounts its
# models read-only. Build ON the compute node (spark), never shipped as a tarball.
#
# Build:  podman build -t precis-tts:<sha> --build-arg PRECIS_REF=<sha> docker/tts
#
# Layer order is deliberate: everything expensive and sha-independent (apt,
# the dep tree, the misaki dict warmups, the ~330 MB model downloads) comes
# BEFORE the sha-pinned precis install, so a rebuild at a new PRECIS_REF —
# which redeploy-precis.yml now does on every deploy — re-runs only that one
# pip install. Keep new layers on the right side of that line.
# Run  :  podman run --rm -v <scratch>/in:/work/in:ro -v <scratch>/out:/work/out \
#           precis-tts        # reads /work/in/segments.json -> /work/out/out.m4a
#
# Base is 3.12: precis-mcp requires >=3.12 (numpy 2.5 dropped 3.11). This image
# installs the precis-mcp[tts] package, so its base must satisfy requires-python
# — the standalone aizynth/normalizer tool images (which don't install precis)
# stay on 3.11-slim.
FROM python:3.12-slim-bookworm

ARG KOKORO_MODEL_URL=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
ARG KOKORO_VOICES_URL=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# espeak-ng: non-zh/ja phonemizer; ffmpeg: encode; tini: reap the child;
# git+curl: pip-from-git + model download. build-essential+cmake: pyopenjtalk
# (misaki[ja]'s Japanese G2P) + mojimoji are C/C++ extensions built from sdist
# (no aarch64 wheels), so they need a compiler + cmake at install time.
# Timeouts + retries so spark's flaky external link can't hang a fetch forever
# (a stalled apt connection wedged the build for an hour with no timeout).
RUN apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 update \
 && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 install -y \
      --no-install-recommends \
      espeak-ng ffmpeg tini git curl build-essential cmake \
 && rm -rf /var/lib/apt/lists/*

# Warm layer: the TTS engine + G2P + the full precis dep tree at a FLOATING
# ref — deliberately `@main`, NOT the build-arg, so this layer's cache
# survives sha bumps. It may go stale (fine: it exists only to keep the deps,
# dicts, and models cached); the sha-pinned install at the bottom is what the
# render engine actually runs.
RUN pip install --no-cache-dir \
      "precis-mcp[tts] @ git+https://github.com/retospect/precis-mcp@main" \
      "misaki[zh,ja]"

# Bake the G2P dictionaries INTO the image so a render needs no network: the ja
# call downloads pyopenjtalk's ~23 MB openjtalk dict (Japanese uses this backend,
# not the 500 MB-unidic cutlet default); the zh call builds jieba's prefix dict.
# Single-line RUNs (not a heredoc — the legacy builder we build under, with
# DOCKER_BUILDKIT=0, does NOT execute RUN heredocs, so a heredoc warmup silently
# no-ops and the dict re-downloads on every render).
RUN python -c "from misaki import zh; zh.ZHG2P()('你好世界')"
RUN python -c "from misaki import ja; ja.JAG2P(version='pyopenjtalk')('こんにちは世界')"

# Bake the model files in (self-contained image). --retry + --connect-timeout so
# a stalled GitHub CDN connection fails fast + retries instead of hanging.
RUN mkdir -p /models \
 && curl -fsSL --connect-timeout 30 --retry 5 --retry-delay 5 --retry-connrefused \
      "$KOKORO_MODEL_URL"  -o /models/kokoro-v1.0.onnx \
 && curl -fsSL --connect-timeout 30 --retry 5 --retry-delay 5 --retry-connrefused \
      "$KOKORO_VOICES_URL" -o /models/voices-v1.0.bin
ENV PRECIS_KOKORO_MODEL=/models/kokoro-v1.0.onnx \
    PRECIS_KOKORO_VOICES=/models/voices-v1.0.bin

# Pin the render engine LAST. ARG is declared here (not at the top) so only
# this layer keys on it: bumping the sha re-runs just this pip install (deps
# already satisfied by the warm layer above; genuinely new deps get fetched),
# not the dict warmups or the model downloads. pip reinstalls precis-mcp
# whenever the direct URL's sha differs from the warm layer's (PEP 610
# direct_url comparison), so a same-version bump still lands.
ARG PRECIS_REF=main
RUN pip install --no-cache-dir \
      "precis-mcp[tts] @ git+https://github.com/retospect/precis-mcp@${PRECIS_REF}"

COPY precis-tts-run /usr/local/bin/precis-tts-run
RUN chmod +x /usr/local/bin/precis-tts-run

WORKDIR /work
ENTRYPOINT ["tini", "--"]
CMD ["precis-tts-run"]
