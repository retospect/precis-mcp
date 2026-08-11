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
    s._k = _FakeK()  # type: ignore[assignment]
    s._speed = 1.0
    s._g2p = {}
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


class _FakeSplitK:
    """Raises IndexError (kokoro-onnx's 510-phoneme boundary bug) for any
    input longer than ``max_len`` characters; otherwise returns an array
    whose length equals the input length, so a concatenated result proves
    every leaf actually got rendered (no dropped text)."""

    def __init__(self, max_len: int) -> None:
        self.max_len = max_len
        self.calls: list[str] = []

    def create(
        self,
        text: str,
        *,
        voice: str,
        speed: float,
        lang: str | None = None,
        is_phonemes: bool = False,
    ) -> tuple[list[float], int]:
        self.calls.append(text)
        if len(text) > self.max_len:
            raise IndexError("index 510 is out of bounds for axis 0 with size 510")
        return [1.0] * len(text), 24000


def test_long_text_split_on_indexerror_returns_concatenated_audio():
    s = _synth()
    s._k = _FakeSplitK(max_len=20)  # type: ignore[assignment]
    text = (
        "This is sentence one. This is sentence two, which is also fairly "
        "long and needs another split."
    )
    samples, sr = s.synthesize(text, voice="af_heart", lang="en-us")
    assert sr == 24000
    # every character of the original text made it into some leaf call — the
    # split partitions the string exactly, nothing dropped or duplicated
    assert len(samples) == len(text)
    # actually split, not a single lucky call
    assert len(s._k.calls) > 1  # type: ignore[attr-defined]


def test_unsplittable_text_returns_silence_without_raising():
    s = _synth()
    s._k = _FakeSplitK(max_len=5)  # type: ignore[assignment]
    samples, sr = s.synthesize(
        "supercalifragilisticexpialidocious", voice="af_heart", lang="en-us"
    )
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
