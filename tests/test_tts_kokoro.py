"""KokoroSynth's language routing — misaki G2P for cmn/ja, espeak for the rest,
graceful fallback when misaki is absent or errors. Pure: bypasses __init__ (which
would import kokoro-onnx) and drives the routing over a fake model.
"""

from __future__ import annotations

from typing import Any

from precis.tts.kokoro import KokoroSynth


class _FakeK:
    """Records the create() calls the router makes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(
        self,
        text: str,
        *,
        voice: str,
        speed: float,
        lang: str | None = None,
        is_phonemes: bool = False,
    ) -> tuple[list[float], int]:
        self.calls.append(
            {"text": text, "voice": voice, "lang": lang, "is_phonemes": is_phonemes}
        )
        return [0.0], 24000


def _synth() -> KokoroSynth:
    s = object.__new__(KokoroSynth)  # skip __init__ (no kokoro-onnx on host)
    s._k = _FakeK()  # type: ignore[attr-defined, assignment]
    s._speed = 1.0  # type: ignore[attr-defined]
    s._g2p = {}  # type: ignore[attr-defined]
    return s


def test_english_uses_espeak_path():
    s = _synth()
    s.synthesize("hello", voice="af_heart", lang="en-us")
    c = s._k.calls[0]  # type: ignore[attr-defined]
    assert c["text"] == "hello" and c["lang"] == "en-us" and c["is_phonemes"] is False


def test_mandarin_routes_through_misaki(monkeypatch):
    s = _synth()
    monkeypatch.setattr(s, "_misaki_g2p", lambda lang: lambda t: ("PHON↓", None))
    s.synthesize("你好", voice="zf_xiaoxiao", lang="cmn")
    c = s._k.calls[0]  # type: ignore[attr-defined]
    # phonemes fed to the model with is_phonemes=True, no lang
    assert c["text"] == "PHON↓" and c["is_phonemes"] is True and c["lang"] is None


def test_falls_back_to_espeak_when_misaki_absent(monkeypatch):
    s = _synth()
    monkeypatch.setattr(s, "_misaki_g2p", lambda lang: None)  # not installed
    s.synthesize("你好", voice="zf_xiaoxiao", lang="cmn")
    c = s._k.calls[0]  # type: ignore[attr-defined]
    assert c["text"] == "你好" and c["lang"] == "cmn" and c["is_phonemes"] is False


def test_g2p_error_falls_back_never_raises(monkeypatch):
    s = _synth()

    def _boom(_t):
        raise RuntimeError("g2p exploded")

    monkeypatch.setattr(s, "_misaki_g2p", lambda lang: _boom)
    s.synthesize("こんにちは", voice="jf_alpha", lang="ja")  # must not raise
    c = s._k.calls[0]  # type: ignore[attr-defined]
    assert c["lang"] == "ja" and c["is_phonemes"] is False  # espeak fallback


def test_unspeakable_text_returns_silence_without_calling_create():
    # A "---" block (or other punctuation-only text that slipped through
    # narrate.py's filter) has nothing for Kokoro to say — return silence
    # instead of handing it to the model, where it phonemizes to zero
    # batches and np.concatenate dies (the cast_audio crash-loop).
    s = _synth()
    samples, sr = s.synthesize("---", voice="af_heart", lang="en-us")
    assert sr == 24000
    assert len(samples) == 0
    assert s._k.calls == []  # type: ignore[attr-defined]


def test_kokoro_empty_concatenate_error_returns_silence():
    # The model itself raises this ValueError for text it can't phonemize —
    # the belt-and-suspenders case for text that passes the pre-check (has a
    # letter/digit) but kokoro-onnx still can't produce a batch for
    # (np.concatenate on zero batches). The synth must return empty audio
    # instead of propagating the crash.
    s = _synth()

    def _boom(*_a, **_kw):
        raise ValueError("need at least one array to concatenate")

    s._k.create = _boom  # type: ignore[method-assign]
    samples, sr = s.synthesize("hello", voice="af_heart", lang="en-us")
    assert sr == 24000
    assert len(samples) == 0


def test_kokoro_other_value_error_still_raises():
    s = _synth()

    def _boom(*_a, **_kw):
        raise ValueError("some other kokoro failure")

    s._k.create = _boom  # type: ignore[method-assign]
    try:
        s.synthesize("hello", voice="af_heart", lang="en-us")
        raise AssertionError("expected ValueError to propagate")
    except ValueError as exc:
        assert "some other kokoro failure" in str(exc)
