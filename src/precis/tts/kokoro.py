"""Kokoro TTS adapter — the local, permissive, per-language-native voice engine.

Implements the :class:`precis.export.audio.Synthesizer` seam via ``kokoro-onnx``
(the ``[tts]`` extra). Model + voices load from ``PRECIS_KOKORO_MODEL`` /
``PRECIS_KOKORO_VOICES`` (the on-disk ``kokoro-v1.0.onnx`` + ``voices-v1.0.bin``).
kokoro-onnx + the model files are heavy and host-specific (installed on the
inference node), so the import is lazy and this module is never touched on a
build without the extra.

**Mandarin / Japanese via misaki.** Kokoro's zh/ja voices were trained on the
**misaki** G2P, not espeak — so for ``lang in {cmn, ja}`` this routes text through
misaki to phonemes and feeds those to the model (``is_phonemes=True``), which is
the difference between native and rough output. misaki is optional: if it (or its
language extra) is absent, we fall back to the espeak path — functional, lower
quality — and never fail. The ``precis-tts`` image bakes ``misaki[zh,ja]`` in.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

#: Languages whose Kokoro voices want the misaki G2P (espeak phonemes mismatch
#: the training set). Keyed by our espeak lang code -> misaki submodule + class.
_MISAKI_LANGS: dict[str, tuple[str, str]] = {
    "cmn": ("zh", "ZHG2P"),
    "ja": ("ja", "JAG2P"),
}


class KokoroSynth:
    """A :class:`Synthesizer` backed by kokoro-onnx.

    ``speed`` is the one clean native prosody knob; ``voice``/``lang`` come per
    segment from the draft's voice score (UK voices want ``en-gb``, US ``en-us``,
    Mandarin ``cmn`` via misaki).
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
        self._g2p: dict[str, Any] = {}  # lang -> misaki G2P instance (cached)

    def _misaki_g2p(self, lang: str) -> Any | None:
        """The cached misaki G2P for a language, or ``None`` if misaki (or its
        language extra) isn't installed — the caller then uses the espeak path."""
        if lang in self._g2p:
            return self._g2p[lang]
        g2p: Any | None = None
        submod, cls = _MISAKI_LANGS[lang]
        try:
            import importlib

            mod = importlib.import_module(f"misaki.{submod}")
            g2p = getattr(mod, cls)()
        except Exception as exc:  # misaki absent or dict missing — fall back
            log.info(
                "misaki %s unavailable (%s); using espeak for %s", submod, exc, lang
            )
        self._g2p[lang] = g2p
        return g2p

    def synthesize(self, text: str, *, voice: str, lang: str) -> tuple[Any, int]:
        if lang in _MISAKI_LANGS:
            g2p = self._misaki_g2p(lang)
            if g2p is not None:
                try:
                    phonemes, _tokens = g2p(text)
                    return self._k.create(
                        phonemes, voice=voice, speed=self._speed, is_phonemes=True
                    )
                except Exception as exc:  # never fail a render on G2P trouble
                    log.warning("misaki %s G2P failed (%s); espeak fallback", lang, exc)
        return self._k.create(text, voice=voice, speed=self._speed, lang=lang)
