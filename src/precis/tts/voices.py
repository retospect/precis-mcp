"""Kokoro voice catalogue — ground truth for per-chunk narration routing.

Grounded in the deployed ``voices-v1.0.bin`` (54 voices, verified 2026-07-15). Pure
data + helpers, **no kokoro import**, so the draft handler can validate a chunk's
``voice`` / ``lang`` against it and a typo fails loudly instead of silently
falling back to the default voice.

A voice name is ``<lang><gender>_<name>``; its first letter picks the language and
that language's espeak ``lang`` code. **Chinese is ``cmn``** (the espeak
identifier) — ``zh`` errors on the backend. Good Mandarin / Japanese output also
wants the misaki G2P (a synth-side upgrade); the *voice* routing here is
independent of that.
"""

from __future__ import annotations

#: language prefix (a voice name's first letter) -> (espeak lang code, default voice)
_LANGS: dict[str, tuple[str, str]] = {
    "a": ("en-us", "af_heart"),
    "b": ("en-gb", "bf_emma"),
    "e": ("es", "ef_dora"),
    "f": ("fr-fr", "ff_siwis"),
    "h": ("hi", "hf_alpha"),
    "i": ("it", "if_sara"),
    "j": ("ja", "jf_alpha"),
    "p": ("pt-br", "pf_dora"),
    "z": ("cmn", "zf_xiaoxiao"),
}

#: every voice in the deployed model, grouped by language prefix.
_VOICES_BY_PREFIX: dict[str, tuple[str, ...]] = {
    "a": (
        "af_alloy",
        "af_aoede",
        "af_bella",
        "af_heart",
        "af_jessica",
        "af_kore",
        "af_nicole",
        "af_nova",
        "af_river",
        "af_sarah",
        "af_sky",
        "am_adam",
        "am_echo",
        "am_eric",
        "am_fenrir",
        "am_liam",
        "am_michael",
        "am_onyx",
        "am_puck",
        "am_santa",
    ),
    "b": (
        "bf_alice",
        "bf_emma",
        "bf_isabella",
        "bf_lily",
        "bm_daniel",
        "bm_fable",
        "bm_george",
        "bm_lewis",
    ),
    "e": ("ef_dora", "em_alex", "em_santa"),
    "f": ("ff_siwis",),
    "h": ("hf_alpha", "hf_beta", "hm_omega", "hm_psi"),
    "i": ("if_sara", "im_nicola"),
    "j": ("jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo"),
    "p": ("pf_dora", "pm_alex", "pm_santa"),
    "z": (
        "zf_xiaobei",
        "zf_xiaoni",
        "zf_xiaoxiao",
        "zf_xiaoyi",
        "zm_yunjian",
        "zm_yunxi",
        "zm_yunxia",
        "zm_yunyang",
    ),
}

#: every known voice name.
VOICES: frozenset[str] = frozenset(v for vs in _VOICES_BY_PREFIX.values() for v in vs)
#: every supported espeak lang code.
LANGS: frozenset[str] = frozenset(code for code, _ in _LANGS.values())


def lang_for_voice(voice: str) -> str | None:
    """The espeak ``lang`` code a voice speaks, or ``None`` if the voice is unknown."""
    return _LANGS[voice[0]][0] if voice in VOICES else None


def default_voice_for_lang(lang: str) -> str | None:
    """A canonical voice for a language code, or ``None`` if unsupported."""
    for code, default in _LANGS.values():
        if code == lang:
            return default
    return None


def voices_for_lang(lang: str) -> tuple[str, ...]:
    """Every voice available for a language code (empty if unsupported)."""
    for pfx, (code, _default) in _LANGS.items():
        if code == lang:
            return _VOICES_BY_PREFIX[pfx]
    return ()


def resolve(voice: str | None, lang: str | None) -> tuple[str, str]:
    """Resolve a ``(voice, lang)`` narration pair — fill one from the other and
    validate both against the catalogue.

    - only ``voice`` → ``lang`` inferred from it;
    - only ``lang`` → its default voice;
    - both → they must agree (a voice must speak the given language, else the
      text is phonemized in one language and spoken by another — garbage).

    Raises :class:`ValueError` (the handler wraps it as ``BadInput``) with a
    catalogue-aware hint on an unknown voice / lang or a mismatch."""
    if voice is None and lang is None:
        raise ValueError("give voice= and/or lang=")
    if voice is not None and voice not in VOICES:
        raise ValueError(
            f"unknown voice {voice!r} — e.g. af_heart (en-us), ff_siwis (fr-fr), "
            f"zf_xiaoxiao (cmn), jf_alpha (ja), if_sara (it)"
        )
    if lang is not None and lang not in LANGS:
        raise ValueError(
            f"unknown lang {lang!r} — supported: {', '.join(sorted(LANGS))} "
            f"(Chinese is 'cmn', not 'zh')"
        )
    if voice is not None and lang is None:
        lang = lang_for_voice(voice)
    elif lang is not None and voice is None:
        voice = default_voice_for_lang(lang)
    assert voice is not None and lang is not None
    vlang = lang_for_voice(voice)
    if vlang != lang:
        others = ", ".join(voices_for_lang(lang)[:3])
        raise ValueError(
            f"voice {voice!r} speaks {vlang}, not {lang!r} — pick a {lang} voice "
            f"({others}…) or drop lang= to infer it"
        )
    return voice, lang


__all__ = [
    "LANGS",
    "VOICES",
    "default_voice_for_lang",
    "lang_for_voice",
    "resolve",
    "voices_for_lang",
]
