"""Deterministic number verbalization — the code-owned pronunciation layer
that replaced the old "spell numbers out in the draft" prompt rule. Pure, no
TTS."""

from __future__ import annotations

from precis.draft.verbalize import verbalize_numbers

# ── ISO dates (must beat the range rule) ────────────────────────────────


def test_iso_date():
    assert verbalize_numbers("2026-07-22") == "the twenty-second of July"


def test_iso_date_not_mangled_into_a_range():
    # Before the date rule existed this looked like a huge-number range.
    out = verbalize_numbers("meet on 2026-07-22 please")
    assert "to" not in out.split()
    assert out == "meet on the twenty-second of July please"


# ── times ────────────────────────────────────────────────────────────────


def test_time_on_the_half_hour():
    assert verbalize_numbers("14:30") == "two thirty"


def test_time_on_the_hour():
    assert verbalize_numbers("09:00") == "nine o'clock"


def test_time_single_digit_minute():
    assert verbalize_numbers("9:05") == "nine oh five"


# ── ranges (hyphen / en-dash / em-dash) ─────────────────────────────────


def test_range_hyphen():
    assert verbalize_numbers("2-4") == "two to four"


def test_range_en_dash():
    assert verbalize_numbers("10–20") == "ten to twenty"


def test_range_em_dash():
    assert verbalize_numbers("10—20") == "ten to twenty"


# ── currency ─────────────────────────────────────────────────────────────


def test_currency_decimal():
    assert verbalize_numbers("$1.2") == "one point two dollars"


def test_currency_plain():
    assert verbalize_numbers("$500") == "five hundred dollars"


def test_currency_scale_suffix():
    assert verbalize_numbers("$1.2M") == "one point two million dollars"
    assert verbalize_numbers("$3K") == "three thousand dollars"
    assert verbalize_numbers("$2B") == "two billion dollars"


# ── percent ──────────────────────────────────────────────────────────────


def test_percent():
    assert verbalize_numbers("45%") == "forty-five percent"


# ── ordinals ─────────────────────────────────────────────────────────────


def test_ordinals():
    assert verbalize_numbers("1st") == "first"
    assert verbalize_numbers("2nd") == "second"
    assert verbalize_numbers("3rd") == "third"
    assert verbalize_numbers("21st") == "twenty-first"


# ── decimals ─────────────────────────────────────────────────────────────


def test_decimal():
    assert verbalize_numbers("2.3") == "two point three"


# ── years (must win over the plain-integer rule) ────────────────────────


def test_year_recent():
    assert verbalize_numbers("2026") == "twenty twenty-six"


def test_year_historical():
    assert verbalize_numbers("1984") == "nineteen eighty-four"


def test_year_beats_plain_integer_in_range():
    # A bare 4-digit number inside 1100-2199 reads as a year ("fifteen
    # hundred"), not the cardinal "one thousand five hundred" — this is the
    # rule the docstring calls out as "must win over the plain-integer rule".
    out = verbalize_numbers("in 1500 something happened")
    assert out == "in fifteen hundred something happened"
    assert "one thousand" not in out


# ── thousands separators / plain integers ───────────────────────────────


def test_thousands_separator():
    assert verbalize_numbers("1,000") == "one thousand"


def test_thousands_separator_larger():
    assert verbalize_numbers("12,345") == "twelve thousand three hundred forty-five"


def test_plain_integer_outside_year_range():
    # 9999 is well outside the 1100-2199 year window, so it reads as a plain
    # cardinal, not a year.
    assert verbalize_numbers("9999") == "nine thousand nine hundred ninety-nine"


def test_plain_integer_small():
    assert verbalize_numbers("12") == "twelve"


# ── guard rail: never touch a digit adjacent to a letter ───────────────


def test_guard_chemical_formula():
    assert verbalize_numbers("H2O") == "H2O"
    assert verbalize_numbers("Fe3O4") == "Fe3O4"


def test_guard_model_names():
    assert verbalize_numbers("bge-m3") == "bge-m3"
    assert verbalize_numbers("Qwen3") == "Qwen3"
    assert verbalize_numbers("GPT-4") == "GPT-4"


def test_guard_disease_name():
    assert verbalize_numbers("COVID-19") == "COVID-19"


def test_guard_voice_id_no_digits_untouched():
    assert verbalize_numbers("af_nicole") == "af_nicole"


def test_guard_in_running_prose():
    # The whole point: numbers embedded among identifiers convert, the
    # identifiers themselves don't.
    out = verbalize_numbers("bge-m3 embeds 1,000 chunks on GPT-4 hardware")
    assert "bge-m3" in out
    assert "GPT-4" in out
    assert "one thousand" in out


# ── non-English passthrough ─────────────────────────────────────────────


def test_non_english_lang_is_a_noop():
    text = "猫は2匹います"
    assert verbalize_numbers(text, lang="ja") == text
    assert verbalize_numbers("1,000", lang="cmn") == "1,000"
    assert verbalize_numbers("2.3", lang="fr-fr") == "2.3"


def test_english_variants_both_transform():
    assert verbalize_numbers("45%", lang="en-us") == "forty-five percent"
    assert verbalize_numbers("45%", lang="en-gb") == "forty-five percent"


# ── idempotency ──────────────────────────────────────────────────────────


def test_idempotent_on_already_converted_text():
    once = verbalize_numbers("It costs $1.2M and takes 2-4 weeks, on 2026-07-22.")
    twice = verbalize_numbers(once)
    assert once == twice


def test_idempotent_on_untouched_identifiers():
    text = "af_nicole reads bge-m3 embeddings for Qwen3"
    once = verbalize_numbers(text)
    assert once == text
    assert verbalize_numbers(once) == text


# ── fractions ────────────────────────────────────────────────────────────


def test_simple_fractions_read_as_words():
    # The live case: "one silver adatom at 1/9 coverage" in a catalysis brief.
    assert verbalize_numbers("at 1/9 coverage") == "at one ninth coverage"
    assert verbalize_numbers("2/3 of the slab") == "two thirds of the slab"
    assert verbalize_numbers("1/2 done") == "one half done"
    assert verbalize_numbers("3/4 full") == "three fourths full"


def test_halves_are_not_ordinal_seconds():
    """/2 is the one denominator English doesn't name by its ordinal."""
    assert verbalize_numbers("1/2 done") == "one half done"
    assert "second" not in verbalize_numbers("1/2 and 5/2")
    # An improper fraction declines the fraction rule, but the digits still
    # must not reach the synth — they fall through to the plain-integer rule.
    assert verbalize_numbers("covers 3/2") == "covers three/two"


def test_fraction_rule_leaves_dates_and_ratios_alone():
    # Denominator out of range, improper, or part of a longer slash run: the
    # fraction rule must decline rather than half-convert a date.
    assert verbalize_numbers("16/9 aspect").startswith("sixteen/nine")
    assert verbalize_numbers("7/22 date").startswith("seven/twenty-two")
    assert "ninth" not in verbalize_numbers("1/9/2026 stamp")
