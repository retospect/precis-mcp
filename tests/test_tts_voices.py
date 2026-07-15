"""The Kokoro voice catalogue + narration resolver (pure — no TTS toolchain)."""

from __future__ import annotations

import pytest

from precis.tts import voices


def test_catalogue_shape():
    assert len(voices.VOICES) == 54
    assert {
        "af_heart",
        "ff_siwis",
        "zf_xiaoxiao",
        "jf_alpha",
        "if_sara",
    } <= voices.VOICES
    # Chinese is cmn, not zh (the espeak identifier — zh errors on the backend).
    assert "cmn" in voices.LANGS and "zh" not in voices.LANGS
    assert {"en-us", "en-gb", "fr-fr", "it", "ja", "es", "hi", "pt-br"} <= voices.LANGS


def test_lang_for_voice():
    assert voices.lang_for_voice("ff_siwis") == "fr-fr"
    assert voices.lang_for_voice("zf_xiaoxiao") == "cmn"
    assert voices.lang_for_voice("af_heart") == "en-us"
    assert voices.lang_for_voice("bogus") is None


def test_default_voice_for_lang():
    assert voices.default_voice_for_lang("cmn") == "zf_xiaoxiao"
    assert voices.default_voice_for_lang("fr-fr") == "ff_siwis"
    assert voices.default_voice_for_lang("de") is None  # German not in v1.0


def test_resolve_infers_lang_from_voice():
    assert voices.resolve("ff_siwis", None) == ("ff_siwis", "fr-fr")
    assert voices.resolve("jf_alpha", None) == ("jf_alpha", "ja")


def test_resolve_picks_default_voice_from_lang():
    assert voices.resolve(None, "cmn") == ("zf_xiaoxiao", "cmn")
    assert voices.resolve(None, "it") == ("if_sara", "it")


def test_resolve_both_must_agree():
    assert voices.resolve("if_sara", "it") == ("if_sara", "it")
    with pytest.raises(ValueError, match="speaks it, not"):
        voices.resolve("if_sara", "fr-fr")  # Italian voice, French lang → mismatch


def test_resolve_rejects_unknowns_with_hints():
    with pytest.raises(ValueError, match="unknown voice"):
        voices.resolve("af_nope", None)
    with pytest.raises(ValueError, match="'cmn', not 'zh'"):
        voices.resolve(None, "zh")  # the classic Chinese-code mistake
    with pytest.raises(ValueError, match="voice= and/or lang="):
        voices.resolve(None, None)
