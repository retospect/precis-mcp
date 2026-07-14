"""Kokoro TTS adapter — the local, permissive, per-language-native voice engine.

Implements the :class:`precis.export.audio.Synthesizer` seam via ``kokoro-onnx``
(the ``[tts]`` extra). Model + voices load from ``PRECIS_KOKORO_MODEL`` /
``PRECIS_KOKORO_VOICES`` (the on-disk ``kokoro-v1.0.onnx`` + ``voices-v1.0.bin``).
kokoro-onnx + the model files are heavy and host-specific (installed on the
inference node), so the import is lazy and this module is never touched on a
build without the extra.
"""

from __future__ import annotations

import os
from typing import Any


class KokoroSynth:
    """A :class:`Synthesizer` backed by kokoro-onnx.

    ``speed`` is the one clean native prosody knob; ``voice``/``lang`` come per
    segment from the draft's voice score (UK voices want ``en-gb``, US ``en-us``).
    """

    def __init__(
        self,
        *,
        model_path: str | None = None,
        voices_path: str | None = None,
        speed: float = 1.0,
    ) -> None:
        from kokoro_onnx import Kokoro  # lazy: [tts] extra only

        model = model_path or os.environ.get("PRECIS_KOKORO_MODEL")
        voices = voices_path or os.environ.get("PRECIS_KOKORO_VOICES")
        if not model or not voices:
            raise RuntimeError(
                "Kokoro needs PRECIS_KOKORO_MODEL + PRECIS_KOKORO_VOICES "
                "(paths to kokoro-v1.0.onnx / voices-v1.0.bin)"
            )
        self._k = Kokoro(model, voices)
        self._speed = speed

    def synthesize(self, text: str, *, voice: str, lang: str) -> tuple[Any, int]:
        return self._k.create(text, voice=voice, speed=self._speed, lang=lang)
