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
    CiteKeyOverflow,
    make_cite_key,
    make_content_hash,
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


def test_cite_key_overflow() -> None:
    base = "miller23"
    taken = {base} | {base + letter for letter in string.ascii_lowercase}
    with pytest.raises(CiteKeyOverflow) as exc:
        make_cite_key(["Miller"], 2023, taken=taken)
    assert exc.value.base == base
    assert exc.value.taken == taken


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
