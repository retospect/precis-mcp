"""Tests for ``precis.identity``.

Pure-function tests; no DB, no I/O. Cover normalisation, hashes, and
the three primary identifiers (paper_id, pub_id, cite_key) plus the
opaque chunk handle (node_id).

Stability assertions ("regression pins") catch silent algorithm
changes — the values below are produced by the locked formulas and
must not change without an ADR.
"""

from __future__ import annotations

import re
import string

import pytest

from precis.identity import (
    PLACEHOLDER_TITLE,
    is_placeholder_title,
    make_cite_key,
    make_content_hash,
    make_finding_paper_id,
    make_node_id,
    make_paper_id,
    make_pdf_sha256,
    make_pub_id,
    normalize_arxiv,
    normalize_doi,
    normalize_text_for_hash,
)

# ---------------------------------------------------------------------------
# normalize_doi
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("10.1234/x", "10.1234/x"),
        ("10.1234/X", "10.1234/x"),
        ("10.1234/Foo.Bar", "10.1234/foo.bar"),
        ("doi:10.1234/x", "10.1234/x"),
        ("DOI:10.1234/X", "10.1234/x"),
        ("https://doi.org/10.1234/x", "10.1234/x"),
        ("http://doi.org/10.1234/x", "10.1234/x"),
        ("https://dx.doi.org/10.1234/x", "10.1234/x"),
        ("http://dx.doi.org/10.1234/x", "10.1234/x"),
        ("doi.org/10.1234/x", "10.1234/x"),
        ("  10.1234/x  ", "10.1234/x"),
    ],
)
def test_normalize_doi(raw: str | None, expected: str | None) -> None:
    assert normalize_doi(raw) == expected


# ---------------------------------------------------------------------------
# normalize_arxiv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("2301.12345", "2301.12345"),
        ("2301.12345v1", "2301.12345"),
        ("2301.12345v3", "2301.12345"),
        ("2301.12345v42", "2301.12345"),
        ("arXiv:2301.12345", "2301.12345"),
        ("arxiv:2301.12345", "2301.12345"),
        ("arxiv:2301.12345v2", "2301.12345"),
        ("https://arxiv.org/abs/2301.12345", "2301.12345"),
        ("https://arxiv.org/abs/2301.12345v3", "2301.12345"),
        ("http://arxiv.org/abs/2301.12345", "2301.12345"),
        ("arxiv.org/abs/2301.12345", "2301.12345"),
        ("https://arxiv.org/pdf/2301.12345", "2301.12345"),
        ("https://arxiv.org/pdf/2301.12345.pdf", "2301.12345"),
        ("https://arxiv.org/pdf/2301.12345v3.pdf", "2301.12345"),
        ("2301.12345#abstract", "2301.12345"),
        ("2301.12345?context=foo", "2301.12345"),
        # Old-style: archive prefix preserved (case included) and slash kept.
        ("cs.LG/0501001", "cs.LG/0501001"),
        ("cs.LG/0501001v2", "cs.LG/0501001"),
        ("hep-th/9901001", "hep-th/9901001"),
        ("arXiv:cs.LG/0501001", "cs.LG/0501001"),
    ],
)
def test_normalize_arxiv(raw: str | None, expected: str | None) -> None:
    assert normalize_arxiv(raw) == expected


# ---------------------------------------------------------------------------
# make_pdf_sha256
# ---------------------------------------------------------------------------


def test_pdf_sha256_empty() -> None:
    # Well-known SHA-256 of empty input.
    assert (
        make_pdf_sha256(b"")
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_pdf_sha256_hello() -> None:
    # Standard reference value.
    assert (
        make_pdf_sha256(b"hello")
        == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_pdf_sha256_length_and_charset() -> None:
    h = make_pdf_sha256(b"some payload")
    assert len(h) == 64
    assert re.fullmatch(r"[0-9a-f]+", h)


def test_pdf_sha256_deterministic() -> None:
    payload = b"\x00\x01\x02\x03\x04"
    assert make_pdf_sha256(payload) == make_pdf_sha256(payload)


# ---------------------------------------------------------------------------
# normalize_text_for_hash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("hello", "hello"),
        ("Hello", "hello"),
        ("Hello   World", "hello world"),
        ("  hello world  ", "hello world"),
        ("hello\n\nworld", "hello world"),
        ("hello\tworld", "hello world"),
        ("hello\r\nworld", "hello world"),
        # NFKD ligature decomposition: U+FB01 (ﬁ) → "fi".
        ("\ufb01x", "fix"),
        # NFKD compatibility decomposition leaves combining marks
        # in place (the unicodedata category is Mn). 'café' (NFC,
        # U+00E9) → 'e' + U+0301; we don't strip the mark here.
        ("\u00e9", "\u0065\u0301"),
    ],
)
def test_normalize_text_for_hash(raw: str | None, expected: str) -> None:
    assert normalize_text_for_hash(raw) == expected


def test_normalize_text_for_hash_idempotent() -> None:
    samples = ["Hello   World", "  spaced  ", "\ufb01x", ""]
    for s in samples:
        once = normalize_text_for_hash(s)
        twice = normalize_text_for_hash(once)
        assert once == twice


# ---------------------------------------------------------------------------
# make_content_hash
# ---------------------------------------------------------------------------


def test_content_hash_empty_matches_sha256_empty() -> None:
    # Empty / whitespace / None all canonicalise to "" and hash the same.
    expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert make_content_hash("") == expected
    assert make_content_hash(None) == expected
    assert make_content_hash("   ") == expected


def test_content_hash_whitespace_invariance() -> None:
    """Whitespace differences within the same content fold together."""
    base = make_content_hash("hello world")
    assert make_content_hash("Hello World") == base
    assert make_content_hash(" hello world ") == base
    assert make_content_hash("hello\n\nworld") == base
    assert make_content_hash("hello\tworld") == base


def test_content_hash_distinguishes_genuinely_different_content() -> None:
    a = make_content_hash("we propose method X")
    b = make_content_hash("we do not propose method X")
    assert a != b


def test_content_hash_known_value() -> None:
    # Regression pin: locked formula → locked output.
    assert (
        make_content_hash("Hello World")
        == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


def test_content_hash_length_and_charset() -> None:
    h = make_content_hash("some text")
    assert len(h) == 64
    assert re.fullmatch(r"[0-9a-f]+", h)


# ---------------------------------------------------------------------------
# make_paper_id
# ---------------------------------------------------------------------------


def test_paper_id_arxiv_only() -> None:
    assert make_paper_id(arxiv="2301.12345") == "arxiv:2301.12345"


def test_paper_id_doi_only() -> None:
    assert make_paper_id(doi="10.1234/x") == "doi:10.1234/x"


def test_paper_id_sha256_only() -> None:
    assert make_paper_id(pdf_sha256="ABCD" + "0" * 60) == "sha256:abcd" + "0" * 60


def test_paper_id_priority_arxiv_beats_others() -> None:
    pid = make_paper_id(
        arxiv="2301.12345",
        doi="10.1234/x",
        pdf_sha256="00" * 32,
    )
    assert pid == "arxiv:2301.12345"


def test_paper_id_priority_doi_beats_sha256() -> None:
    pid = make_paper_id(doi="10.1234/x", pdf_sha256="00" * 32)
    assert pid == "doi:10.1234/x"


def test_paper_id_normalises_inputs() -> None:
    """URL / prefix / version forms are normalised before kind tagging."""
    assert (
        make_paper_id(arxiv="https://arxiv.org/abs/2301.12345v3") == "arxiv:2301.12345"
    )
    assert make_paper_id(doi="https://doi.org/10.1000/Test") == "doi:10.1000/test"


def test_paper_id_all_empty_raises() -> None:
    with pytest.raises(ValueError):
        make_paper_id()
    with pytest.raises(ValueError):
        make_paper_id(arxiv="", doi="", pdf_sha256="")
    with pytest.raises(ValueError):
        make_paper_id(arxiv=None, doi=None, pdf_sha256=None)


# ---------------------------------------------------------------------------
# make_finding_paper_id
# ---------------------------------------------------------------------------


def test_finding_paper_id_shape() -> None:
    pid = make_finding_paper_id(
        "2.4 kV held for 30 s on Si/SiO2 MOSCAPs",
        {"electrode": "Cu", "ambient": "N2"},
        "ab12c3",
    )
    assert pid.startswith("finding:")
    assert len(pid) == len("finding:") + 64  # sha256 hex
    assert re.fullmatch(r"finding:[0-9a-f]{64}", pid)


def test_finding_paper_id_deterministic() -> None:
    args = (
        "2.4 kV held for 30 s on Si/SiO2 MOSCAPs",
        {"electrode": "Cu", "ambient": "N2"},
        "ab12c3",
    )
    assert make_finding_paper_id(*args) == make_finding_paper_id(*args)


def test_finding_paper_id_body_canonicalised() -> None:
    """Whitespace + case differences in body collapse to one pub_id."""
    a = make_finding_paper_id("2.4 kV  held  for 30 s", {}, "ab12c3")
    b = make_finding_paper_id("2.4 KV held for 30 s", {}, "ab12c3")
    c = make_finding_paper_id("   2.4 kV held for 30 s   ", {}, "ab12c3")
    assert a == b == c


def test_finding_paper_id_scope_key_order_irrelevant() -> None:
    """Same scope under different key orderings hashes the same."""
    a = make_finding_paper_id("x", {"electrode": "Cu", "ambient": "N2"}, "ab12c3")
    b = make_finding_paper_id("x", {"ambient": "N2", "electrode": "Cu"}, "ab12c3")
    assert a == b


def test_finding_paper_id_scope_none_equals_empty() -> None:
    """``scope=None`` and ``scope={}`` produce the same id."""
    assert make_finding_paper_id("x", None, "ab12c3") == make_finding_paper_id(
        "x", {}, "ab12c3"
    )


def test_finding_paper_id_scope_distinguishes_setups() -> None:
    """Different setups → different findings (the load-bearing case)."""
    cu = make_finding_paper_id("2.4 kV", {"electrode": "Cu"}, "ab12c3")
    ag = make_finding_paper_id("2.4 kV", {"electrode": "Ag"}, "ab12c3")
    assert cu != ag


def test_finding_paper_id_cite_distinguishes_chains() -> None:
    """Same claim cited from a different paper → different finding."""
    a = make_finding_paper_id("2.4 kV", {}, "ab12c3")
    b = make_finding_paper_id("2.4 kV", {}, "de45f6")
    assert a != b


def test_finding_paper_id_cite_case_insensitive() -> None:
    """``pub_id`` handles are lowercase by convention; defensive fold."""
    a = make_finding_paper_id("2.4 kV", {}, "ab12c3")
    b = make_finding_paper_id("2.4 kV", {}, "AB12C3")
    c = make_finding_paper_id("2.4 kV", {}, "  ab12c3 ")
    assert a == b == c


def test_finding_paper_id_empty_body_raises() -> None:
    with pytest.raises(ValueError):
        make_finding_paper_id("", {}, "ab12c3")
    with pytest.raises(ValueError):
        make_finding_paper_id("   ", {}, "ab12c3")


def test_finding_paper_id_empty_cite_raises() -> None:
    with pytest.raises(ValueError):
        make_finding_paper_id("x", {}, "")
    with pytest.raises(ValueError):
        make_finding_paper_id("x", {}, "   ")


def test_finding_paper_id_threads_through_pub_id() -> None:
    """The whole point: make_pub_id over a finding paper_id yields a
    standard 6-char base32 handle, same as any other ref."""
    pid = make_finding_paper_id(
        "2.4 kV held for 30 s on Si/SiO2", {"electrode": "Cu"}, "ab12c3"
    )
    pub = make_pub_id(pid)
    assert len(pub) == 6
    assert re.fullmatch(r"[a-z2-7]+", pub)


# Regression pin — locks the formula so future refactors can't silently
# churn paper_ids and orphan every previously-minted finding. Value is
# computed at test author time from the (locked) algorithm; update only
# when changing the formula via a new ADR (which would invalidate every
# finding pub_id ever issued).
def test_finding_paper_id_regression() -> None:
    pid = make_finding_paper_id(
        "2.4 kV held for 30 s on Si/SiO2 MOSCAPs",
        {"electrode": "Cu", "ambient": "N2"},
        "ab12c3",
    )
    # Computed from the locked algorithm:
    #   body_canonical  = "2.4 kv held for 30 s on si/sio2 moscaps"
    #   scope_canonical = '{"ambient":"N2","electrode":"Cu"}'
    #   cite_canonical  = "ab12c3"
    #   key             = "finding|body=...|scope=...|cite=ab12c3"
    #   digest          = sha256(key).hexdigest()
    assert pid == (
        "finding:3850419c0261b98839e8c748b004e1820b8a3e105a3e6860fc1623341f1eb394"
    )


# ---------------------------------------------------------------------------
# make_pub_id
# ---------------------------------------------------------------------------


def test_pub_id_length_and_charset() -> None:
    pub = make_pub_id("arxiv:2301.12345")
    assert len(pub) == 6
    # base32 lowercase: digits 2-7, letters a-z.
    assert re.fullmatch(r"[a-z2-7]+", pub)


def test_pub_id_deterministic() -> None:
    a = make_pub_id("arxiv:2301.12345")
    b = make_pub_id("arxiv:2301.12345")
    assert a == b


def test_pub_id_distinct_inputs_yield_distinct_outputs() -> None:
    """Birthday-paradox tiny: 6-char base32 has 32^6 ≈ 1B values; with
    a handful of random inputs collisions should be vanishingly rare."""
    pubs = {
        make_pub_id(p)
        for p in [
            "arxiv:2301.12345",
            "arxiv:2301.12346",
            "doi:10.1000/foo",
            "doi:10.1000/bar",
            "sha256:" + "0" * 64,
            "sha256:" + "1" * 64,
        ]
    }
    assert len(pubs) == 6


def test_pub_id_empty_raises() -> None:
    with pytest.raises(ValueError):
        make_pub_id("")


# Regression pins — locked algorithm → locked output. Update only when
# changing the formula via a new ADR (which would invalidate every
# pub_id ever issued).
@pytest.mark.parametrize(
    "paper_id, expected",
    [
        ("arxiv:2301.12345", "bj6n5d"),
        ("doi:10.1000/test", "v6yilt"),
        ("sha256:" + "0" * 64, "4gi3yg"),
    ],
)
def test_pub_id_regression(paper_id: str, expected: str) -> None:
    assert make_pub_id(paper_id) == expected


# ---------------------------------------------------------------------------
# make_cite_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authors, year, expected",
    [
        # Single author, various string shapes
        (["Miller"], 2023, "miller23"),
        (["Miller, John"], 2023, "miller23"),
        (["John Miller"], 2023, "miller23"),
        (["Miller, John A."], 2023, "miller23"),
        # Glued initials
        (["A.Clark"], 2023, "clark23"),
        (["A.B.Clark"], 2023, "clark23"),
        # Diacritics: NFKD + ASCII-only → "muller"
        (["Müller"], 2023, "muller23"),
        (["Müller, Hans"], 2023, "muller23"),
        # Hyphenated surname: ASCII strip removes the hyphen.
        (["Crocco-Galéas"], 2024, "croccogaleas24"),
        # CrossRef / Semantic Scholar dict shape
        ([{"family": "Miller", "given": "John"}], 2023, "miller23"),
        ([{"family": "Müller"}], 2023, "muller23"),
        # S2-ish "name" key fallback (no "family" key)
        ([{"name": "Miller"}], 2023, "miller23"),
        # CrossRef "last" key alias
        ([{"last": "Miller"}], 2023, "miller23"),
    ],
)
def test_cite_key_basic(authors: list, year: int, expected: str) -> None:
    assert make_cite_key(authors, year) == expected


def test_cite_key_year_handling() -> None:
    # 2-digit year: 2023 → "23", 1999 → "99", 2003 → "03"
    assert make_cite_key(["Miller"], 2023) == "miller23"
    assert make_cite_key(["Miller"], 1999) == "miller99"
    assert make_cite_key(["Miller"], 2003) == "miller03"
    assert make_cite_key(["Miller"], 2030) == "miller30"
    # Missing year → "00"
    assert make_cite_key(["Miller"], None) == "miller00"
    # Pre-1900 papers: still mod 100
    assert make_cite_key(["Darwin"], 1859) == "darwin59"


def test_cite_key_missing_authors() -> None:
    assert make_cite_key([], 2023) == "anon23"
    assert make_cite_key(None, 2023) == "anon23"
    assert make_cite_key([""], 2023) == "anon23"
    assert make_cite_key([{}], 2023) == "anon23"


def test_cite_key_collision_progression() -> None:
    """Empty taken → no suffix. Each subsequent collision bumps by one."""
    assert make_cite_key(["Miller"], 2023, taken=set()) == "miller23"
    assert make_cite_key(["Miller"], 2023, taken={"miller23"}) == "miller23a"
    assert (
        make_cite_key(["Miller"], 2023, taken={"miller23", "miller23a"}) == "miller23b"
    )
    # Sparse taken (a is taken but base is free) → return base, not "a".
    assert make_cite_key(["Miller"], 2023, taken={"miller23a"}) == "miller23"


def test_cite_key_fills_letter_z() -> None:
    base = "miller23"
    taken = {base + letter for letter in string.ascii_lowercase[:25]}
    taken.add(base)
    # 25 letters (a..y) + base taken → next free is "z"
    assert make_cite_key(["Miller"], 2023, taken=taken) == base + "z"


def test_cite_key_rolls_past_z_to_two_letters() -> None:
    """After a..z are exhausted the suffix rolls to aa, ab, … (no overflow)."""
    base = "miller23"
    taken = {base} | {base + letter for letter in string.ascii_lowercase}
    # 26 single letters + base all taken → roll to the first two-letter suffix
    assert make_cite_key(["Miller"], 2023, taken=taken) == base + "aa"
    # aa taken → ab
    assert make_cite_key(["Miller"], 2023, taken=taken | {base + "aa"}) == base + "ab"
    # a-prefixed two-letter suffixes (aa..az) all taken → roll to ba
    through_az = taken | {base + "a" + c for c in string.ascii_lowercase}
    assert make_cite_key(["Miller"], 2023, taken=through_az) == base + "ba"


def test_cite_key_deterministic_under_same_inputs() -> None:
    # Same authors / year / taken → same result.
    a = make_cite_key(["Miller"], 2023, taken={"miller23"})
    b = make_cite_key(["Miller"], 2023, taken={"miller23"})
    assert a == b == "miller23a"


def test_cite_key_only_first_author_matters() -> None:
    a = make_cite_key(["Miller", "Doe", "Roe"], 2023)
    b = make_cite_key(["Miller"], 2023)
    assert a == b


def test_cite_key_charset_ascii_lower() -> None:
    """Output is always lowercase ASCII letters + digits."""
    samples = [
        make_cite_key(["Müller"], 2023),
        make_cite_key(["李"], 2023),  # Chinese surname → may fold to "anon"
        make_cite_key(["O'Brien"], 2023),
        make_cite_key(["McDonald"], 2023),
    ]
    for s in samples:
        assert re.fullmatch(r"[a-z0-9]+", s), f"non-ascii-lower in {s!r}"


# ---------------------------------------------------------------------------
# gr521515 dry-run mint bugs — the shapes the prod sweep produced
# ---------------------------------------------------------------------------


def test_cite_key_corporate_author_uses_next_person() -> None:
    """gr521515 dry-run: a collective first author minted its own token
    as the 'surname' ('Parkinson Study Group' → ``group00``, 'Neuralynx,
    Inc.' → ``neuralynx12``). The mint walks past it to the next person
    author."""
    # The real stored rows behind group00 / neuralynx12:
    group_first = [
        {"family": "Parkinson Study Group", "openalex_author_id": "A5023314663"},
        {"given": "J.", "family": "Smith"},
    ]
    assert make_cite_key(group_first, 2000) == "smith00"
    # "Neuralynx, Inc." was parsed into given/family — the collective
    # token can sit in *given*, so detection looks at the whole entry.
    inc_first = [
        {"given": "Inc.", "family": "Neuralynx"},
        {"given": "A.", "family": "Doe"},
    ]
    assert make_cite_key(inc_first, 2012) == "doe12"
    assert make_cite_key(["Neuralynx, Inc.", "Doe, A."], 2012) == "doe12"


def test_cite_key_all_collective_byline_uses_org_word() -> None:
    """gr523665: an all-collective byline keys on the first significant
    word of the org name — the old ``anon`` fallback left the paper
    junk-keyed and skipped by the sweep forever."""
    assert make_cite_key([{"family": "Parkinson Study Group"}], 2000) == ("parkinson00")
    assert make_cite_key([{"given": "Inc.", "family": "Neuralynx"}], 2012) == (
        "neuralynx12"
    )
    assert make_cite_key(["LHCb Collaboration"], 2020) == "lhcb20"
    # Articles/connectives/legal forms are not significant words; an
    # org name reduced to them alone still falls back to ``anon``.
    assert make_cite_key(["The Royal Society"], 2020) == "royal20"
    assert make_cite_key(["The Study Group"], 2020) == "anon20"


def test_cite_key_cyrillic_author_transliterates() -> None:
    """gr523665: pa497788 ("Батманова, А.Н.", 2025) was swept as
    "corporate/collective only" — a Cyrillic person surname folds to
    nothing under NFKD. It romanises first, then keys."""
    assert make_cite_key(["Батманова, А.Н."], 2025) == "batmanova25"
    assert make_cite_key([{"family": "Батманова", "given": "А.Н."}], 2025) == (
        "batmanova25"
    )
    # Western order + Cyrillic initials peel after romanisation.
    assert make_cite_key(["Иванов И.В."], 2026) == "ivanov26"
    assert make_cite_key(["Щукин, П."], 2024) == "shchukin24"
    # Scripts outside the table still fold to the ``anon`` placeholder.
    assert make_cite_key(["李"], 2023) == "anon23"


def test_cite_key_latin_special_author_transliterates() -> None:
    """gr523730: letters NFKD does not decompose (ł, ø, đ, ß, æ, œ,
    þ, ı) dropped under the bare fold — pa212196 (Kołożyn-Krajewska)
    minted ``koozynkrajewska26``. They romanise mint-side via
    ``_transliterate``, the same seat as Cyrillic."""
    assert make_cite_key(["Kołożyn-Krajewska"], 2026) == "kolozynkrajewska26"
    assert make_cite_key(["Łukasiewicz"], 2026) == "lukasiewicz26"
    assert make_cite_key([{"family": "Łukasiewicz", "given": "Jan"}], 2026) == (
        "lukasiewicz26"
    )
    assert make_cite_key(["Søren"], 2026) == "soren26"
    # The rest of the table.
    assert make_cite_key(["Đoković"], 2026) == "dokovic26"
    assert make_cite_key(["Weiß"], 2026) == "weiss26"
    assert make_cite_key(["Ævarsdóttir"], 2026) == "aevarsdottir26"
    assert make_cite_key(["Œuvre"], 2026) == "oeuvre26"
    assert make_cite_key(["Þórsson"], 2026) == "thorsson26"
    assert make_cite_key(["Yılmaz"], 2026) == "yilmaz26"
    # Regression: ordinary Latin authors key unchanged.
    assert make_cite_key(["Miller"], 2023) == "miller23"
    assert make_cite_key(["Müller"], 2023) == "muller23"
    assert make_cite_key(["Crocco-Galéas"], 2024) == "croccogaleas24"


def test_cite_key_surname_then_initials_form() -> None:
    """gr521515 dry-run: ``{"name": "Mula A"}`` (surname first, initials
    trailing) minted ``a26avk`` — a trailing single-letter / dotted-
    initial token is an initial, not the surname."""
    mula = [{"name": "Mula A"}, {"name": "Yan Bai"}, {"name": "Yulin Wang"}]
    assert make_cite_key(mula, 2026) == "mula26"
    assert make_cite_key(["Mula A.V."], 2026) == "mula26"
    assert make_cite_key(["Mula A V"], 2026) == "mula26"


def test_cite_key_particle_family_used_whole() -> None:
    """gr521515 dry-run: ``{"family": "de Sa"}`` minted ``sa98`` —
    nobiliary particles belong to the surname — and ``{"family":
    "Fidler Mis"}`` minted ``mis26`` — a parsed family field is used
    whole, spaces removed."""
    desa = [
        {"given": "Virginia R.", "family": "de Sa"},
        {"given": "Geoffrey E.", "family": "Hinton"},
    ]
    assert make_cite_key(desa, 1998) == "desa98"
    assert make_cite_key([{"given": "Nataša", "family": "Fidler Mis"}], 2026) == (
        "fidlermis26"
    )
    # Free-text Western order joins the lowercase particles too.
    assert make_cite_key(["Jan van der Berg"], 2020) == "vanderberg20"
    assert make_cite_key(["John von Neumann"], 1945) == "vonneumann45"


def test_cite_key_pubmed_caps_initials() -> None:
    """Same bad-key class as ``a26avk``: PubMed's unpunctuated all-caps
    initials (``Mula AV``) trail the surname. A trailing run of 1-3
    ASCII capitals after a mixed-case head token is initials."""
    assert make_cite_key(["Mula AV"], 2026) == "mula26"
    assert make_cite_key(["Smith AB"], 2026) == "smith26"
    assert make_cite_key(["Smith JK"], 2026) == "smith26"
    assert make_cite_key(["Smith ABC"], 2026) == "smith26"


def test_cite_key_caps_token_can_stay_surname() -> None:
    """A short mixed-case surname is never an initial — ``Wu``/``Li``/
    ``Sa``/``Ng`` stay the surname; a single dotted/bare letter still
    peels even off an all-caps surname (``SMITH J``)."""
    assert make_cite_key(["Ming Wu"], 2026) == "wu26"
    assert make_cite_key(["Yan Li"], 2026) == "li26"
    assert make_cite_key(["Maria Sa"], 2026) == "sa26"
    assert make_cite_key(["Anthony Ng"], 2026) == "ng26"
    assert make_cite_key(["SMITH J"], 2026) == "smith26"


def test_cite_key_generational_suffixes() -> None:
    """A trailing generational suffix is not the surname either —
    ``Jr``/``Jr.``/``Sr``/``II``/``III``/``IV`` peel like initials."""
    assert make_cite_key(["Smith Jr"], 2026) == "smith26"
    assert make_cite_key(["Smith Jr."], 2026) == "smith26"
    assert make_cite_key(["Smith Sr"], 2026) == "smith26"
    assert make_cite_key(["Smith II"], 2026) == "smith26"
    assert make_cite_key(["Smith III"], 2026) == "smith26"
    assert make_cite_key(["Smith IV"], 2026) == "smith26"


def test_cite_key_paren_honorifics() -> None:
    """gr524004: a parenthesised honorific tail — ``(Dr)``, ``(Prof)``,
    ``(PhD)`` — is not the surname; ``"Wu Shaoteng (Dr)"`` minted the
    ``dr00`` junk key that broke the merge path's slug lookup."""
    assert make_cite_key(["Wu Shaoteng (Dr)"], 2026) == "shaoteng26"
    assert make_cite_key(["Wu Shaoteng (Dr)"], None) == "shaoteng00"
    assert make_cite_key(["Jane Doe (Prof)"], 2026) == "doe26"
    assert make_cite_key(["Jane Doe (PhD)"], 2026) == "doe26"
    assert make_cite_key(["Jane Doe (Ph.D.)"], 2026) == "doe26"
    # Comma form and the parsed-family shape drop the same tail.
    assert make_cite_key(["Doe, Jane (Dr)"], 2026) == "doe26"
    assert make_cite_key([{"family": "Doe (Dr)", "given": "Jane"}], 2026) == ("doe26")
    # A parenthesised token that isn't an honorific is kept — it can be
    # the surname itself.
    assert make_cite_key(["John (Smith)"], 2026) == "smith26"


def test_cite_key_paren_honorifics_non_latin() -> None:
    """The honorific fold runs after romanisation, so a Cyrillic
    credential tail — ``"(Др)"`` transliterates to ``"(Dr)"`` — drops
    the same way instead of minting as the surname."""
    assert make_cite_key(["Иванов (Др)"], 2026) == "ivanov26"
    assert make_cite_key([{"family": "Иванов (Др)"}], 2026) == "ivanov26"


def test_cite_key_capitalized_particles() -> None:
    """The family field folds ``"De La Cruz"`` whole — the free-string
    form now matches it: a *capitalized* particle joins the surname
    when another name token follows it (a trailing particle-shaped
    token is the surname itself)."""
    assert make_cite_key(["Van Der Berg"], 2026) == "vanderberg26"
    assert make_cite_key(["Maria De La Cruz"], 2026) == "delacruz26"
    # Lowercase particles join as before.
    assert make_cite_key(["Ludwig van Beethoven"], 2026) == "vanbeethoven26"
    # Spaced Mac/Mc surnames fold like the glued forms.
    assert make_cite_key(["Mac Donald"], 2026) == "macdonald26"
    assert make_cite_key(["Mc Donald"], 2026) == "mcdonald26"
    # A capitalized particle can be a given name — tolerated.
    assert make_cite_key(["Le Nguyen"], 2026) == "lenguyen26"


# ---------------------------------------------------------------------------
# make_node_id
# ---------------------------------------------------------------------------


def test_node_id_length_and_charset() -> None:
    n = make_node_id("arxiv:2301.12345", 3, 7)
    assert len(n) == 8
    assert re.fullmatch(r"[a-z2-7]+", n)


def test_node_id_deterministic() -> None:
    a = make_node_id("arxiv:2301.12345", 3, 7)
    b = make_node_id("arxiv:2301.12345", 3, 7)
    assert a == b


def test_node_id_input_sensitivity() -> None:
    """Different inputs → different node_ids (high probability)."""
    ids = {
        make_node_id("arxiv:2301.12345", 3, 7),
        make_node_id("arxiv:2301.12345", 3, 8),  # different block_index
        make_node_id("arxiv:2301.12345", 4, 7),  # different page
        make_node_id("arxiv:2301.12346", 3, 7),  # different paper_id
        make_node_id("arxiv:2301.12345", None, 7),  # page=None
    }
    assert len(ids) == 5


def test_node_id_page_none_handled() -> None:
    n = make_node_id("arxiv:2301.12345", None, 0)
    assert len(n) == 8


def test_node_id_empty_paper_id_raises() -> None:
    with pytest.raises(ValueError):
        make_node_id("", 0, 0)


@pytest.mark.parametrize(
    "paper_id, page, block_index, expected",
    [
        ("arxiv:2301.12345", 3, 7, "b5usdz3q"),
        ("arxiv:2301.12345", None, 0, "zeekxnz6"),
    ],
)
def test_node_id_regression(
    paper_id: str, page: int | None, block_index: int, expected: str
) -> None:
    assert make_node_id(paper_id, page, block_index) == expected


class TestPlaceholderTitle:
    def test_blank_and_sentinel_are_placeholders(self) -> None:
        assert is_placeholder_title(None)
        assert is_placeholder_title("")
        assert is_placeholder_title("   ")
        assert is_placeholder_title(PLACEHOLDER_TITLE)
        assert is_placeholder_title(f"  {PLACEHOLDER_TITLE}  ")

    def test_a_real_title_is_not(self) -> None:
        assert not is_placeholder_title("Attention Is All You Need")
        # Close, but a real (if odd) title - the check is exact, not fuzzy.
        assert not is_placeholder_title("(no title) - a memoir")
