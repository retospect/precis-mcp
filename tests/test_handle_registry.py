"""ADR 0036 handle registry — totality + computed-format guards.

The totality test is the point: a new persistent-ref kind added without a
2-char code fails here, so coverage is enforced by CI, not manual review
(``news``/``message``/``cron`` all slipped through manual lists).

Handles are *computed*, not stored: ``<2-char code><decimal pk>`` (e.g.
``pa5``, ``pc10``, ``tg42``). No alphabet, no minter — :func:`format_handle`
encodes, :func:`parse` decodes.
"""

from __future__ import annotations

import re

import pytest

from precis.utils import handle_registry as hr

# Mirror of the addressable persistent-ref kinds registered in
# ``dispatch.boot()`` (providers web/youtube/wikipedia/semanticscholar/
# perplexity-* and stateless tools calc/math/provenance/random are
# addressed by URL/query/compute, not handles). ``random`` is a stateless
# generator — no row, no handle. TODO: derive from the live hub registry
# so this can never drift, once a lightweight kind-list accessor exists
# that doesn't boot the heavy handler graph.
EXPECTED_PERSISTENT_KINDS = frozenset(
    {
        "paper",
        "patent",
        "cfp",
        "news",
        "draft",
        "conv",
        "pres",
        "markdown",
        "plaintext",
        "tex",
        "python",
        "orcid",
        "memory",
        "oracle",
        "finding",
        "citation",
        "flashcard",
        "todo",
        "job",
        "alert",
        "agentlog",
        "cron",
        "message",
        "gripe",
        "skill",
        "tag",
        "cad",
        "structure",
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


def test_format_handle_is_code_plus_decimal_pk() -> None:
    assert hr.format_handle("paper", 5) == "pa5"
    assert hr.format_handle("memory", 4217) == "me4217"
    assert hr.format_handle("paper", 10, chunk=True) == "pc10"
    assert hr.format_handle("tag", 42) == "tg42"


def test_try_format_is_none_for_codeless_kind_or_id() -> None:
    assert hr.try_format("websearch", 5) is None  # provider, no code
    assert hr.try_format("paper", None) is None
    assert hr.try_format("paper", 5) == "pa5"


def test_round_trip_code_lookup() -> None:
    for kind, code in hr.KIND_CODES.items():
        assert hr.code_for_kind(kind) == code
        assert hr.kind_for_code(code) == (kind, False)
    for kind, code in hr.CHUNK_CODES.items():
        assert hr.code_for_kind(kind, chunk=True) == code
        assert hr.kind_for_code(code) == (kind, True)


def test_parse_decodes_refs_backed_decimal_handles() -> None:
    assert hr.parse("pa5") == ("paper", False, 5)
    assert hr.parse("me4217") == ("memory", False, 4217)
    assert hr.parse("pc10") == ("paper", True, 10)
    # Prefix is case-folded; the decimal body has no case.
    assert hr.parse("ME5") == ("memory", False, 5)


def test_parse_rejects_file_backed_and_other_table_codes() -> None:
    # skill/python are file-backed, tag lives in its own table — they
    # carry codes for registry completeness but aren't decimal handles
    # ``parse`` resolves (they keep their kind+id addressing).
    assert hr.parse("sktoc") is None
    assert hr.parse("pysome.module") is None
    assert hr.parse("tg42") is None


def test_parse_rejects_junk() -> None:
    assert hr.parse("miller23") is None  # legacy slug
    assert hr.parse("zz5") is None  # unknown code
    assert hr.parse("pa") is None  # no body
    assert hr.parse("paxyz") is None  # non-digit body
    assert hr.parse("¶YP377G") is None  # legacy draft sigil handle


def test_is_well_formed_tracks_parse() -> None:
    assert hr.is_well_formed("pa5")
    assert hr.is_well_formed("pc10")
    assert not hr.is_well_formed("miller23")
    assert not hr.is_well_formed("tg42")
    assert not hr.is_well_formed("zz5")


def test_normalize_folds_prefix_case_only() -> None:
    assert hr.normalize("ME5") == "me5"
    assert hr.normalize("  pa42 ") == "pa42"
    # idempotent
    assert hr.normalize(hr.normalize("CO123")) == hr.normalize("CO123")


def test_unknown_kind_and_code_raise() -> None:
    with pytest.raises(KeyError):
        hr.code_for_kind("websearch")  # a provider, not a record
    with pytest.raises(KeyError):
        hr.kind_for_code("zz")


# --- relative navigation grammar (ADR 0036) -----------------------------


def test_parse_relative_step() -> None:
    assert hr.parse_relative("pc10+1") == ("paper", True, 10, ("step", 1))
    assert hr.parse_relative("pc10-3") == ("paper", True, 10, ("step", -3))
    assert hr.parse_relative("pc10++") == ("paper", True, 10, ("step", 1))
    assert hr.parse_relative("pc10--") == ("paper", True, 10, ("step", -1))


def test_parse_relative_zero_step_is_identity() -> None:
    """A ``±0`` step is a redundant no-op but resolves to the chunk itself
    (idempotent / liberal-in-what-we-accept), mirroring the ``-0..0`` span."""
    assert hr.parse_relative("pc10+0") == ("paper", True, 10, ("step", 0))
    assert hr.parse_relative("pc10-0") == ("paper", True, 10, ("step", 0))


def test_parse_relative_ancestor() -> None:
    assert hr.parse_relative("dc4^") == ("draft", True, 4, ("ancestor", 1))
    assert hr.parse_relative("dc4^^") == ("draft", True, 4, ("ancestor", 2))
    assert hr.parse_relative("dc4^3") == ("draft", True, 4, ("ancestor", 3))


def test_parse_relative_span() -> None:
    assert hr.parse_relative("pc10-2..3") == ("paper", True, 10, ("span", -2, 3))
    assert hr.parse_relative("pc10+1..4") == ("paper", True, 10, ("span", 1, 4))


def test_parse_relative_rejects_non_relative_and_junk() -> None:
    assert hr.parse_relative("pc10") is None  # absolute, no operator
    assert hr.parse_relative("me5+1") is None  # record code, not a chunk
    assert hr.parse_relative("miller23+1") is None  # legacy slug
    assert hr.parse_relative("pc10+x") is None  # malformed operator


# --- plugin-contributed codes (ADR 0036 — handle_codes entry point) ------


def _fake_eps(monkeypatch: pytest.MonkeyPatch, **codes: dict) -> None:
    """Reset the lazy plugin state and inject one fake handle-code plugin."""
    import types

    mod = types.SimpleNamespace(
        RECORD_CODES=codes.get("records", {}),
        CHUNK_CODES=codes.get("chunks", {}),
    )
    ep = types.SimpleNamespace(name="fake_plugin", load=lambda: mod)
    monkeypatch.setattr(hr, "entry_points", lambda group: [ep])
    monkeypatch.setattr(hr, "_plugins_loaded", False)
    monkeypatch.setattr(hr, "_plugin_kind_codes", {})
    monkeypatch.setattr(hr, "_plugin_chunk_codes", {})


def test_plugin_codes_merge_into_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_eps(
        monkeypatch,
        records={"service": "sv", "payment": "pm"},
        chunks={"payment": "pb"},
    )
    assert hr.code_for_kind("service") == "sv"
    assert hr.code_for_kind("payment", chunk=True) == "pb"
    assert hr.format_handle("payment", 5) == "pm5"
    assert hr.kind_for_code("sv") == ("service", False)
    # plugin codes resolve as refs-backed decimal handles
    assert hr.parse("pm5") == ("payment", False, 5)
    assert hr.parse("pb7") == ("payment", True, 7)
    assert hr.is_well_formed("sv12")
    # built-ins still work alongside
    assert hr.parse("pa5") == ("paper", False, 5)


def test_plugin_code_colliding_with_builtin_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_eps(monkeypatch, records={"evil": "pa"})  # 'pa' is paper
    with pytest.raises(KeyError):
        hr.code_for_kind("evil")
    assert hr.kind_for_code("pa") == ("paper", False)  # built-in wins


def test_plugin_codes_do_not_change_builtin_totality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The built-in SSOT (and its totality test) is unaffected by plugins.
    _fake_eps(monkeypatch, records={"service": "sv"})
    hr.code_for_kind("service")  # trigger load
    assert set(hr.KIND_CODES) == EXPECTED_PERSISTENT_KINDS
