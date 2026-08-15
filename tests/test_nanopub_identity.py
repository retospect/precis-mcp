"""Nanopub slice 1/2 identity layer — AIDA canonicalization, snip
normalization, the publish state machine. Pure units, no DB."""

from __future__ import annotations

import pytest

from precis.nanopub import aida, snip, state

# ── aida ────────────────────────────────────────────────────────────────


def test_canonical_sentence_folds_whitespace_and_adds_period() -> None:
    assert aida.canonical_sentence("  Flexible   MOFs\nbend. ") == "Flexible MOFs bend."
    assert aida.canonical_sentence("Does it bend?") == "Does it bend?"
    assert aida.canonical_sentence("It bends") == "It bends."


def test_canonical_sentence_is_idempotent() -> None:
    once = aida.canonical_sentence("It  bends")
    assert aida.canonical_sentence(once) == once


def test_canonical_sentence_rejects_empty() -> None:
    with pytest.raises(ValueError):
        aida.canonical_sentence("   ")


def test_aida_uri_uses_percent20_never_plus() -> None:
    uri = aida.aida_uri("It bends")
    assert uri == "http://purl.org/aida/It%20bends."
    assert "+" not in uri


def test_parse_aida_uri_converges_both_live_encodings() -> None:
    # %20 and + both appear for spaces in the live corpus — different
    # URIs for identical sentences; the lenient parse folds them.
    a = aida.parse_aida_uri("http://purl.org/aida/It%20bends.")
    b = aida.parse_aida_uri("http://purl.org/aida/It+bends.")
    assert a == b == "It bends."
    assert aida.parse_aida_uri("https://example.org/notaida") is None


def test_same_sentence_same_uri_after_whitespace_variance() -> None:
    assert aida.aida_uri("It  bends.") == aida.aida_uri("It bends.")


# ── snip ────────────────────────────────────────────────────────────────


def test_normalize_text_strips_soft_hyphens_and_ligatures() -> None:
    assert snip.normalize_text("direc­tions") == "directions"
    assert snip.normalize_text("ﬁnding œuvre") == "finding oeuvre"
    assert snip.normalize_text("A  B\tC") == "a b c"


def test_make_snip_is_lowercase_ascii_tokens() -> None:
    s = snip.make_snip("This anisotropy can reach a 400:1 ratio, in stark contrast")
    assert snip.is_valid_snip(s)
    assert s == "this anisotropy can reach a 400 1 ratio"


def test_is_valid_snip_rejects_bad_forms() -> None:
    assert not snip.is_valid_snip("")
    assert not snip.is_valid_snip("Upper case")
    assert not snip.is_valid_snip("double  space")
    assert not snip.is_valid_snip("punct,uation")
    assert snip.is_valid_snip("mil-53 anisotropy 400")


def test_count_matches_is_token_bounded() -> None:
    # "ratio 400" must not match inside "ratio 4000".
    assert snip.count_matches("ratio 400", ["a ratio 4000 case"]) == 0
    assert snip.count_matches("ratio 400", ["a ratio 400 case"]) == 1
    assert snip.count_matches("ratio 400", ["ratio 400 here", "ratio 400 there"]) == 2


def test_count_matches_across_hyphenation_artifacts() -> None:
    # PDF soft-hyphen line break in the haystack still matches.
    assert snip.count_matches("weakest directions", ["weakest direc­tions"]) == 1


def test_contains_verbatim_keeps_meaningful_punctuation() -> None:
    chunk = "moduli span 2.30 GPa in this family"
    assert not snip.contains_verbatim("2–30 GPa", chunk)
    assert snip.contains_verbatim("2.30 GPa", chunk)
    # Extraction artifacts (case, ligature, whitespace) still fold.
    assert snip.contains_verbatim("stiFFness proﬁle", "the stiffness profile is")


# ── state machine ───────────────────────────────────────────────────────


def test_forward_path_is_legal() -> None:
    import itertools

    path = ["candidate", "reviewed", "signed", "anchored", "published", "superseded"]
    for a, b in itertools.pairwise(path):
        state.check_transition(a, b)


def test_rejected_branches_off_reviewed_only() -> None:
    state.check_transition("reviewed", "rejected")
    for other in ("candidate", "signed", "anchored", "published"):
        if other == "reviewed":
            continue
        with pytest.raises(ValueError):
            state.check_transition(other, "rejected")


def test_no_backward_flips_from_anchored_on() -> None:
    for frozen in ("anchored", "published"):
        for back in ("candidate", "reviewed", "signed"):
            with pytest.raises(ValueError):
                state.check_transition(frozen, back)


def test_terminal_states_allow_nothing() -> None:
    for terminal in state.TERMINAL:
        for target in state.STATES:
            with pytest.raises(ValueError):
                state.check_transition(terminal, target)


def test_pre_anchor_reopens_are_legal() -> None:
    state.check_transition("reviewed", "candidate")
    state.check_transition("signed", "candidate")
    state.check_transition("signed", "reviewed")  # dependency-dirty flip
