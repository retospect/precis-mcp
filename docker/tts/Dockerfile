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
# Run  :  podman run --rm -v <scratch>/in:/work/in:ro -v <scratch>/out:/work/out \
#           precis-tts        # reads /work/in/segments.json -> /work/out/out.m4a
#
# Base is 3.11 (not 3.12): precis-mcp needs only >=3.11, and 3.11-slim is already
# cached on the compute node (shared with docker/aizynth), so the build skips the
# flaky spark→Docker Hub pull entirely.
FROM python:3.11-slim-bookworm

ARG PRECIS_REF=main
ARG KOKORO_MODEL_URL=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
ARG KOKORO_VOICES_URL=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# espeak-ng: non-zh/ja phonemizer; ffmpeg: encode; tini: reap the child;
# git+curl: pip-from-git + model download. misaki's JP/ZH dicts are pip wheels
# (fugashi/unidic-lite/jieba) — no apt.
RUN apt-get update \
 && apt-get install -y --no-install-recommends espeak-ng ffmpeg tini git curl \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      "precis-mcp[tts] @ git+https://github.com/retospect/precis-mcp@${PRECIS_REF}" \
      "misaki[zh,ja]"

# Warm the G2P dictionaries INTO the image so the container needs no network at
# run: pyopenjtalk fetches its ~23 MB openjtalk dict on first use (Japanese uses
# this backend, not the 500 MB-unidic cutlet default), and jieba builds its zh
# prefix dict. Without this the first zh/ja render would hit the network.
RUN python - <<'PY'
from misaki import ja, zh
zh.ZHG2P()("你好")
ja.JAG2P(version="pyopenjtalk")("こんにちは")
print("misaki zh/ja dictionaries warmed")
PY

# Bake the model files in (self-contained image).
RUN mkdir -p /models \
 && curl -fsSL "$KOKORO_MODEL_URL"  -o /models/kokoro-v1.0.onnx \
 && curl -fsSL "$KOKORO_VOICES_URL" -o /models/voices-v1.0.bin
ENV PRECIS_KOKORO_MODEL=/models/kokoro-v1.0.onnx \
    PRECIS_KOKORO_VOICES=/models/voices-v1.0.bin

COPY precis-tts-run /usr/local/bin/precis-tts-run
RUN chmod +x /usr/local/bin/precis-tts-run

WORKDIR /work
ENTRYPOINT ["tini", "--"]
CMD ["precis-tts-run"]
