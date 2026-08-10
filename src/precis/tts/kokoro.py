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
import re
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

#: kokoro-onnx's fixed output sample rate (24 kHz) — used for the empty-audio
#: fallback tuple so a skipped/failed segment still matches the
#: :class:`~precis.export.audio.Synthesizer` return shape.
_KOKORO_SR = 24000

#: Any Unicode letter or digit. Text with none of these (a "---" rule, bare
#: punctuation left after narrate.py's markup stripping) has nothing for
#: Kokoro to say — phonemizing it yields zero batches and
#: ``np.concatenate`` dies with "need at least one array to concatenate".
_HAS_VOICE = re.compile(r"[^\W_]")

#: Sentence-ending punctuation followed by whitespace — the preferred split
#: point when a batch overflows kokoro-onnx's fixed 510-phoneme/token limit
#: (splitting mid-sentence reads worse than splitting between sentences).
_SENTENCE_BREAK = re.compile(r"[.!?。！？]\s+")

#: Any whitespace run — the fallback split point when there's no sentence
#: boundary to use.
_WHITESPACE = re.compile(r"\s+")


def _split_point(payload: str) -> int | None:
    """The index nearest ``payload``'s midpoint to split on: a sentence
    boundary if one exists, else any whitespace, else ``None`` (a single
    giant token — unsplittable). A match flush against either end (e.g. a
    trailing ". ") is excluded — it would hand ``payload`` right back
    unshrunk on one side and recurse forever."""
    mid = len(payload) / 2
    for pattern in (_SENTENCE_BREAK, _WHITESPACE):
        ends = [
            m.end() for m in pattern.finditer(payload) if 0 < m.end() < len(payload)
        ]
        if ends:
            return min(ends, key=lambda i: abs(i - mid))
    return None


def _create_with_split(
    create_fn: Callable[[str], tuple[Any, int]], payload: str
) -> tuple[Any, int]:
    """Call ``create_fn(payload)``; on kokoro-onnx's 510-phoneme/token
    boundary ``IndexError`` (``voice = voice[len(tokens)]`` in
    ``_create_audio``), split ``payload`` near its midpoint and recurse on
    each half, concatenating the resulting audio (recursion handles a half
    that's still too long). Unsplittable input (no whitespace at all) logs a
    warning and returns silence — matching the existing "no audio batches"
    ``ValueError`` fallback below: one cursed segment must not kill the whole
    episode."""
    try:
        return create_fn(payload)
    except IndexError:
        import numpy as np

        split = _split_point(payload)
        if split is None:
            log.warning(
                "Kokoro hit the 510-phoneme boundary on unsplittable input "
                "%r; returning silence",
                payload[:80],
            )
            return np.zeros(0, dtype=np.float32), _KOKORO_SR
        left, sr = _create_with_split(create_fn, payload[:split])
        right, _sr = _create_with_split(create_fn, payload[split:])
        return np.concatenate([left, right]), sr


#: Languages whose Kokoro voices want the misaki G2P (espeak phonemes mismatch
#: the training set). Keyed by our espeak lang code -> (misaki submodule, class,
#: init kwargs). Japanese uses the ``pyopenjtalk`` backend, which ships its own
#: dictionary (~23 MB, baked in the image) — the default ``cutlet`` backend needs
#: a ~500 MB unidic download and errors ("Failed initializing MeCab") without it.
_MISAKI_LANGS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "cmn": ("zh", "ZHG2P", {}),
    "ja": ("ja", "JAG2P", {"version": "pyopenjtalk"}),
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
        submod, cls, kwargs = _MISAKI_LANGS[lang]
        try:
            import importlib

            mod = importlib.import_module(f"misaki.{submod}")
            g2p = getattr(mod, cls)(**kwargs)
        except Exception as exc:  # misaki absent or dict missing — fall back
            log.info(
                "misaki %s unavailable (%s); using espeak for %s", submod, exc, lang
            )
        self._g2p[lang] = g2p
        return g2p

    def synthesize(self, text: str, *, voice: str, lang: str) -> tuple[Any, int]:
        import numpy as np

        if not _HAS_VOICE.search(text):
            log.warning("synthesize() got unspeakable text %r; returning silence", text)
            return np.zeros(0, dtype=np.float32), _KOKORO_SR
        if lang in _MISAKI_LANGS:
            g2p = self._misaki_g2p(lang)
            if g2p is not None:
                try:
                    phonemes, _tokens = g2p(text)
                    return _create_with_split(
                        lambda p: self._k.create(
                            p, voice=voice, speed=self._speed, is_phonemes=True
                        ),
                        phonemes,
                    )
                except Exception as exc:  # never fail a render on G2P trouble
                    log.warning("misaki %s G2P failed (%s); espeak fallback", lang, exc)
        try:
            return _create_with_split(
                lambda t: self._k.create(t, voice=voice, speed=self._speed, lang=lang),
                text,
            )
        except ValueError as exc:
            if "at least one array" not in str(exc):
                raise
            log.warning(
                "Kokoro produced no audio batches for %r; returning silence",
                text[:80],
            )
            return np.zeros(0, dtype=np.float32), _KOKORO_SR
