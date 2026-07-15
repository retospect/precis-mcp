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
FROM python:3.12-slim-bookworm

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
