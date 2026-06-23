"""ADR 0036 handle registry — totality + format guards.

The totality test is the point: a new persistent-ref kind added without a
2-char code fails here, so coverage is enforced by CI, not manual review
(``news``/``message``/``cron`` all slipped through manual lists).
"""

from __future__ import annotations

import re

import pytest

from precis.utils import handle_registry as hr

# Mirror of the addressable persistent-ref kinds registered in
# ``dispatch.boot()`` (the 25 records; providers web/youtube/wikipedia/
# semanticscholar/perplexity-* and tools calc/math/provenance are
# addressed by URL/query/compute, not handles). TODO: derive from the
# live hub registry so this can never drift, once a lightweight kind-list
# accessor exists that doesn't boot the heavy handler graph.
EXPECTED_PERSISTENT_KINDS = frozenset(
    {
        "paper",
        "patent",
        "news",
        "draft",
        "conv",
        "pres",
        "markdown",
        "plaintext",
        "tex",
        "python",
        "memory",
        "oracle",
        "finding",
        "citation",
        "flashcard",
        "random",
        "todo",
        "job",
        "alert",
        "agentlog",
        "cron",
        "message",
        "gripe",
        "skill",
        "tag",
    }
)


def test_every_persistent_kind_has_a_record_code() -> None:
    assert set(hr.KIND_CODES) == EXPECTED_PERSISTENT_KINDS


def test_codes_are_two_lowercase_letters() -> None:
    pat = re.compile(r"^[a-z]{2}$")
    for code in (*hr.KIND_CODES.values(), *hr.CHUNK_CODES.values()):
        assert pat.match(code), code


def test_all_codes_globally_distinct() -> None:
    codes = [*hr.KIND_CODES.values(), *hr.CHUNK_CODES.values()]
    assert len(codes) == len(set(codes)), "duplicate handle code"


def test_chunk_codes_only_for_known_kinds() -> None:
    assert set(hr.CHUNK_CODES) <= set(hr.KIND_CODES)


def test_alphabet_is_crockford32() -> None:
    assert len(hr.CROCKFORD32) == 32
    assert "i" not in hr.CROCKFORD32 and "l" not in hr.CROCKFORD32
    assert "o" not in hr.CROCKFORD32 and "u" not in hr.CROCKFORD32


def test_mint_is_well_formed_and_typed() -> None:
    h = hr.mint("paper")
    assert hr.is_well_formed(h)
    assert len(h) == hr.HANDLE_LEN
    assert hr.kind_for_code(h[:2]) == ("paper", False)

    c = hr.mint("draft", chunk=True)
    assert hr.kind_for_code(c[:2]) == ("draft", True)


def test_round_trip_code_lookup() -> None:
    for kind, code in hr.KIND_CODES.items():
        assert hr.code_for_kind(kind) == code
        assert hr.kind_for_code(code) == (kind, False)
    for kind, code in hr.CHUNK_CODES.items():
        assert hr.code_for_kind(kind, chunk=True) == code
        assert hr.kind_for_code(code) == (kind, True)


def test_normalize_folds_case_and_body_confusables_but_not_prefix() -> None:
    # 'ci' (citation) prefix keeps its 'i'; body i/l -> 1, o -> 0.
    assert hr.normalize("CI4M8P1RZ") == "ci4m8p1rz"
    assert hr.normalize("paILO2345") == "pa1102345"
    # idempotent
    assert hr.normalize(hr.normalize("co0O0o123")) == hr.normalize("co0O0o123")


def test_is_well_formed_rejects_junk() -> None:
    assert not hr.is_well_formed("miller23")  # legacy slug
    assert not hr.is_well_formed("zz4m8p1rz")  # unknown code
    assert not hr.is_well_formed("pa4m8p1r")  # too short
    assert not hr.is_well_formed("pa4m8p1rzz")  # too long


def test_unknown_kind_and_code_raise() -> None:
    with pytest.raises(KeyError):
        hr.code_for_kind("websearch")  # a provider, not a record
    with pytest.raises(KeyError):
        hr.kind_for_code("zz")
