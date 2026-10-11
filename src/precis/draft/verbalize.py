"""Deterministic number verbalization for the narration (TTS) path.

Kokoro/espeak mispronounces raw digits — ``"1,000"`` splits on the comma and
comes out "one zero zero zero". The old mitigation was a **prompt** rule
(``precis-voice`` rule 6) asking the composing LLM to spell numbers out in the
draft text itself — unreliable, and it forced the on-page draft to read "five
hundred thousand" instead of "500,000". The contract now: **the draft keeps
numerals** (good on the page), and **this module spells them out** at
audio-conversion time. Code owns pronunciation; the prompt keeps owning
editorial precision (rounding to two significant figures) — this module does
no rounding, it only decides how a numeral *sounds*.

:func:`verbalize_numbers` is called per :func:`~precis.draft.narrate.split_by_script`
span, after that split has resolved the span's language — that's the only
point a Japanese/Mandarin run can be told apart from an English one, so a CJK
span is left untouched rather than being run through English number words.

Pure function, no I/O, no store access, no TTS import — unit-testable without
a model, like the rest of :mod:`precis.draft.narrate`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, timedelta
from functools import lru_cache
from importlib import resources

from num2words import num2words

#: espeak lang-code prefix -> num2words lang code. v1 wires up English only;
#: a later language is just another entry here (the tokenizing/skip rules
#: below are ours, num2words does the actual word-building per language).
_LANG_MAP: dict[str, str] = {"en": "en"}


def _num2words_lang(lang: str) -> str | None:
    """The num2words lang code for an espeak ``lang`` (e.g. ``en-us``,
    ``en-gb``), or ``None`` if this module doesn't verbalize that language yet."""
    for prefix, code in _LANG_MAP.items():
        if lang.startswith(prefix):
            return code
    return None


#: Characters that keep a digit "inside a word" for guard purposes: ASCII
#: letters/digits plus the hyphen/underscore connectors identifiers use
#: (``bge-m3``, ``COVID-19``, ``af_nicole``). NOT ``.``/``,``/``:``/``%``/``$``
#: — those are the separators our own number patterns consume deliberately.
_TOKEN_CHAR = re.compile(r"[A-Za-z0-9_-]")


def _protected(s: str, start: int, end: int) -> bool:
    """True if the identifier-ish run touching (but outside) ``s[start:end]``
    contains an ASCII letter — i.e. this match sits directly against a word
    like ``bge-m3``, ``Qwen3``, ``GPT-4``, ``COVID-19``, not standing alone as
    a number. Extends outward through the connector chars so a letter one hop
    across a hyphen (``GPT-4``'s ``G``) still trips it, not just the single
    immediately-adjacent character.

    Only the *surrounding* context counts, never the match's own text — a
    pattern is free to deliberately consume adjacent letters itself (the
    ordinal ``st``/``nd``/``rd``/``th`` suffix, the currency ``K``/``M``/``B``
    scale letter); those aren't "adjacent", they're part of the number. The
    load-bearing guard is a digit next to a letter it did *not* itself claim."""
    i = start
    while i > 0 and _TOKEN_CHAR.match(s[i - 1]):
        i -= 1
    j = end
    while j < len(s) and _TOKEN_CHAR.match(s[j]):
        j += 1
    context = s[i:start] + s[end:j]
    return any(c.isascii() and c.isalpha() for c in context)


def _sub_guarded(
    pattern: re.Pattern[str], repl: Callable[[re.Match[str]], str], text: str
) -> str:
    """``pattern.sub(repl, text)``, but a match touching a letter-adjacent
    identifier passes through unchanged instead of being converted."""

    def _cb(m: re.Match[str]) -> str:
        if _protected(m.string, m.start(), m.end()):
            return m.group(0)
        return repl(m)

    return pattern.sub(_cb, text)


def _clean(words: str) -> str:
    """num2words' English cardinals default to the British "one thousand,
    four hundred and eighty-two" shape (comma + "and"); the spoken form we
    want is the flatter "one thousand four hundred eighty-two" — drop both."""
    return words.replace(",", "").replace(" and ", " ")


# ── the patterns, in match-priority order (earlier must run first so a
# later, looser pattern doesn't re-carve an already-claimed span) ──────────

# ISO date: 2026-07-22. Must run before _RANGE, else "2026-07-22" reads as a
# range between two giant numbers instead of a date.
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# 24-hour time: 14:30, 09:00.
_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

# A simple proper fraction: 1/9, 2/3, 3/4. Left alone it reads as "one slash
# nine" or just "one nine" — and coverage fractions are ordinary prose in the
# science casts ("one silver adatom at 1/9 coverage"). Deliberately narrow:
# denominator 2-12 and numerator < denominator, so a US-style date (7/22) or a
# ratio (16/9) doesn't get dragged in, and the lookarounds reject anything in a
# longer slash run (1/9/2026) rather than half-converting a date.
_FRACTION = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")

# A dash between two numbers reads as "to"; unqualified it's "minus".
# Includes the plain hyphen and both dash flavours (en/em).
_RANGE = re.compile(r"\b(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\b")

# $1.2 / $500 / $1.2M / £2bn / €3.5 million (symbol + optional scale). The
# scale is a K/M/B/T letter, a bn/mn/tn abbreviation, or a spelled word; all
# case-insensitive, but only directly after a currency symbol, so a bare "5m"
# (metres) elsewhere is untouched.
_CURRENCY = re.compile(
    r"([$£€])(\d+(?:\.\d+)?)"
    r"(?:\s?(thousand|million|billion|trillion|bn|mn|tn|[kmbt]))?\b",
    re.IGNORECASE,
)
_CURRENCY_NAMES = {
    "$": ("dollar", "dollars"),
    "£": ("pound", "pounds"),
    "€": ("euro", "euros"),
}
_CURRENCY_SCALES = {
    "k": "thousand",
    "thousand": "thousand",
    "m": "million",
    "mn": "million",
    "million": "million",
    "b": "billion",
    "bn": "billion",
    "billion": "billion",
    "t": "trillion",
    "tn": "trillion",
    "trillion": "trillion",
}

# 45%
_PERCENT = re.compile(r"\b(\d+(?:\.\d+)?)%")

# 1st, 2nd, 3rd, 21st
_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b")

# 2.3 (plain decimal — currency/percent above already claimed their own).
_DECIMAL = re.compile(r"\b(\d+)\.(\d+)\b")

# A bare 4-digit integer that reads naturally as a year (num2words' own
# first-two/last-two split): 1100-2199 covers the ordinary historical +
# near-future span this draft prose actually uses. Must win over the
# plain-integer rule below, or "2026" reads as "two thousand twenty-six"
# instead of "twenty twenty-six".
_YEAR = re.compile(r"\b(1[1-9]\d{2}|2[01]\d{2})\b")

# 1,000 / 12,345 — thousands-separated, read as one number, not the comma's
# literal digit groups.
_THOUSANDS = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")

# Whatever's left: a bare digit run.
_PLAIN_INT = re.compile(r"\b\d+\b")

_MONTHS = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


def verbalize_numbers(text: str, *, lang: str = "en") -> str:
    """Rewrite numeric surface forms in ``text`` into spoken words, for the
    given espeak ``lang`` code (as used in :mod:`precis.tts.voices`).

    v1 only transforms English (``lang`` starting with ``"en"``); every other
    language is returned unchanged — a Japanese/Mandarin span must not be
    turned into English words. Adding a language later is a ``_LANG_MAP``
    entry plus (if its date/time/currency shape differs) a per-language
    branch in the callbacks below; the tokenizing/skip rules stay the same.

    Idempotent-ish: the output never contains digits (for the language it
    transforms), so a second pass is a no-op.
    """
    n2w_lang = _num2words_lang(lang)
    if n2w_lang is None:
        return text

    def words(value: float | int) -> str:
        return _clean(str(num2words(value, lang=n2w_lang)))

    def numeric_words(num_str: str) -> str:
        value: float | int = float(num_str) if "." in num_str else int(num_str)
        return words(value)

    def ordinal_words(n: int) -> str:
        return _clean(str(num2words(n, lang=n2w_lang, to="ordinal")))

    def year_words(n: int) -> str:
        return _clean(str(num2words(n, lang=n2w_lang, to="year")))

    def _date_repl(m: re.Match[str]) -> str:
        mo, d = int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return m.group(0)
        return f"the {ordinal_words(d)} of {_MONTHS[mo]}"

    def _time_repl(m: re.Match[str]) -> str:
        h, minute = int(m.group(1)), int(m.group(2))
        hour12 = h % 12 or 12
        hour_word = words(hour12)
        if minute == 0:
            return f"{hour_word} o'clock"
        if minute < 10:
            return f"{hour_word} oh {words(minute)}"
        return f"{hour_word} {words(minute)}"

    def _fraction_repl(m: re.Match[str]) -> str:
        num, den = int(m.group(1)), int(m.group(2))
        if not (2 <= den <= 12 and 0 < num < den):
            return m.group(0)
        # "two seconds" would be the ordinal's answer for /2 — halves are the
        # one denominator English doesn't name by its ordinal.
        name = "half" if den == 2 else ordinal_words(den)
        if num > 1:
            name = "halves" if den == 2 else f"{name}s"
        return f"{words(num)} {name}"

    def _range_repl(m: re.Match[str]) -> str:
        return f"{numeric_words(m.group(1))} to {numeric_words(m.group(2))}"

    def _currency_repl(m: re.Match[str]) -> str:
        singular, plural = _CURRENCY_NAMES[m.group(1)]
        raw, suffix = m.group(2), m.group(3)
        amount = numeric_words(raw)
        if suffix:
            return f"{amount} {_CURRENCY_SCALES[suffix.lower()]} {plural}"
        return f"{amount} {singular if float(raw) == 1 else plural}"

    def _percent_repl(m: re.Match[str]) -> str:
        return f"{numeric_words(m.group(1))} percent"

    def _ordinal_repl(m: re.Match[str]) -> str:
        return ordinal_words(int(m.group(1)))

    def _decimal_repl(m: re.Match[str]) -> str:
        return numeric_words(f"{m.group(1)}.{m.group(2)}")

    def _year_repl(m: re.Match[str]) -> str:
        return year_words(int(m.group(0)))

    def _thousands_repl(m: re.Match[str]) -> str:
        return words(int(m.group(0).replace(",", "")))

    def _plain_int_repl(m: re.Match[str]) -> str:
        return words(int(m.group(0)))

    t = text
    t = _sub_guarded(_ISO_DATE, _date_repl, t)
    t = _sub_guarded(_TIME, _time_repl, t)
    t = _sub_guarded(_FRACTION, _fraction_repl, t)
    t = _sub_guarded(_RANGE, _range_repl, t)
    t = _sub_guarded(_CURRENCY, _currency_repl, t)
    t = _sub_guarded(_PERCENT, _percent_repl, t)
    t = _sub_guarded(_ORDINAL, _ordinal_repl, t)
    t = _sub_guarded(_DECIMAL, _decimal_repl, t)
    t = _sub_guarded(_YEAR, _year_repl, t)
    t = _sub_guarded(_THOUSANDS, _thousands_repl, t)
    t = _sub_guarded(_PLAIN_INT, _plain_int_repl, t)
    return t


# ── abbreviations (data: precis/data/tts_lexicon.json) ──────────────────────


@lru_cache(maxsize=1)
def _lexicon() -> dict[str, dict]:
    """The packaged abbreviation table: ``letters`` (set), ``words``
    (token -> respelling), ``expand`` (surface -> spoken phrase)."""
    raw = json.loads(
        resources.files("precis.data")
        .joinpath("tts_lexicon.json")
        .read_text(encoding="utf-8")
    )
    return {
        "letters": {k: True for k in raw["letters"]},
        "words": dict(raw["words"]),
        "expand": dict(raw["expand"]),
    }


# Roman numerals read as numerals ("World War II"), never as letters.
_ROMAN = re.compile(r"^[IVXLCDM]+$")
# An all-caps token of 2-6 letters standing alone (no digit/hyphen/underscore
# glued on: GPT-4, COVID-19, MOF-74 keep their own handling).
_CAPS_TOKEN = re.compile(r"(?<![\w-])[A-Z]{2,6}(?![\w-])")


def _spell(token: str) -> str:
    return " ".join(token)


def expand_abbreviations(text: str, *, lang: str = "en") -> str:
    """Rewrite abbreviations for speech per the packaged lexicon: expansions
    ("e.g." -> "for example"), word readings ("NASA" -> "Nasa") and letter
    spelling ("DFT" -> "D F T"). Unknown all-caps tokens of 2-6 letters
    default to letters unless they sit in a run of capitalised words (a
    shouted heading reads as words, not as initials). Chemical formulas and
    mixed-case tokens (MoS2, WSe2) are never touched. English only."""
    if _num2words_lang(lang) is None:
        return text
    lex = _lexicon()
    expand = lex["expand"]
    if expand:
        alts = "|".join(re.escape(k) for k in sorted(expand, key=len, reverse=True))
        pat = re.compile(rf"(?<![\w.])(?:{alts})(?!\w)")
        text = pat.sub(lambda m: expand[m.group(0)], text)

    def _caps(m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok in lex["words"]:
            return lex["words"][tok]
        if tok in lex["letters"]:
            return _spell(tok)
        if _ROMAN.match(tok):
            return tok
        before = m.string[: m.start()].rstrip(" ")
        after = m.string[m.end() :].lstrip(" ")
        prev_w = re.search(r"([A-Za-z]+)$", before)
        next_w = re.match(r"([A-Za-z]+)", after)
        for w in (prev_w, next_w):
            if w and len(w.group(1)) >= 2 and w.group(1).isupper():
                return tok
        return _spell(tok)

    return _CAPS_TOKEN.sub(_caps, text)


# ── temporal phrases anchored to the episode date ───────────────────────────

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_PREPS = r"(?:since|until|till|by|from|before|after|through|to|in|during|over|for)"
_TEMPORAL = re.compile(
    rf"(?:(?P<prep>\b{_PREPS})\s+)?"
    r"\b(?P<phrase>today|yesterday|tomorrow|this week|last week)\b(?![\'’]s)",
    re.IGNORECASE,
)


def anchor_temporal(text: str, episode_date: date | None, *, lang: str = "en") -> str:
    """Rewrite relative time words to absolute dates for an episode heard
    later than it was written: "today" -> "on Saturday the eleventh of
    October", "last week" -> "the week of Monday the twenty-ninth of
    September". Possessives ("today's") are left alone. With no
    ``episode_date`` (or a non-English ``lang``) the text is unchanged."""
    if episode_date is None or _num2words_lang(lang) is None:
        return text
    n2w = _num2words_lang(lang)

    def day(d: date) -> str:
        ordinal = _clean(str(num2words(d.day, lang=n2w, to="ordinal")))
        return f"{_WEEKDAYS[d.weekday()]} the {ordinal} of {_MONTHS[d.month]}"

    monday = episode_date - timedelta(days=episode_date.weekday())

    def _repl(m: re.Match[str]) -> str:
        phrase = m.group("phrase").lower()
        prep = m.group("prep")
        if phrase.endswith("week"):
            anchor = monday - timedelta(days=7 if phrase == "last week" else 0)
            body = f"the week of {day(anchor)}"
            lead = prep
        else:
            delta = {"today": 0, "yesterday": -1, "tomorrow": 1}[phrase]
            body = day(episode_date + timedelta(days=delta))
            # "since yesterday" keeps its preposition; a bare adverb needs "on".
            lead = prep if prep else "on"
        out = f"{lead} {body}" if lead else body
        # Keep the capital of a sentence-initial "Today".
        if not prep and m.group(0)[0].isupper():
            out = out[0].upper() + out[1:]
        return out

    return _TEMPORAL.sub(_repl, text)


def verbalize_text(
    text: str, *, lang: str = "en", episode_date: date | None = None
) -> str:
    """The full TTS-time pass: abbreviations, then temporal anchoring (when
    ``episode_date`` is known), then numbers — in that order so the words
    those steps emit are final and the date digits-free text is not re-read."""
    t = expand_abbreviations(text, lang=lang)
    t = anchor_temporal(t, episode_date, lang=lang)
    return verbalize_numbers(t, lang=lang)


__all__ = [
    "anchor_temporal",
    "expand_abbreviations",
    "verbalize_numbers",
    "verbalize_text",
]
