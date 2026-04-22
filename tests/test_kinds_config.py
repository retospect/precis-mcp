"""Tests for ``PRECIS_KINDS`` parser (§13, §10.1 fatal matrix)."""

from __future__ import annotations

import pytest

from precis.kinds_config import ConfigError, load_from_env, parse_precis_kinds
from precis.protocol import VERBS

# ---------------------------------------------------------------------------
# No-filter sentinel
# ---------------------------------------------------------------------------


class TestNoFilter:
    def test_none_returns_none(self):
        assert parse_precis_kinds(None) is None  # type: ignore[arg-type]

    def test_empty_returns_none(self):
        assert parse_precis_kinds("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_precis_kinds("   \t\n  ") is None


# ---------------------------------------------------------------------------
# Bare kinds → all verbs
# ---------------------------------------------------------------------------


class TestBareKinds:
    def test_single_bare_kind_gets_all_verbs(self):
        mask = parse_precis_kinds("paper")
        assert mask == {"paper": VERBS}

    def test_multiple_bare_kinds(self):
        mask = parse_precis_kinds("paper,memory,web")
        assert set(mask) == {"paper", "memory", "web"}
        for verbs in mask.values():
            assert verbs == VERBS

    def test_whitespace_around_kinds_is_tolerated(self):
        mask = parse_precis_kinds("  paper  ,  memory  ,  web  ")
        assert set(mask) == {"paper", "memory", "web"}


# ---------------------------------------------------------------------------
# Bracketed verb whitelists
# ---------------------------------------------------------------------------


class TestBracketed:
    def test_single_verb(self):
        mask = parse_precis_kinds("paper[get]")
        assert mask == {"paper": frozenset({"get"})}

    def test_multiple_verbs(self):
        mask = parse_precis_kinds("paper[get,search]")
        assert mask == {"paper": frozenset({"get", "search"})}

    def test_all_four_verbs_spelled_out(self):
        mask = parse_precis_kinds("paper[search,get,put,move]")
        assert mask == {"paper": frozenset({"search", "get", "put", "move"})}

    def test_mix_bare_and_bracketed(self):
        mask = parse_precis_kinds("paper,memory,doc[get,search]")
        assert mask["paper"] == VERBS
        assert mask["memory"] == VERBS
        assert mask["doc"] == frozenset({"get", "search"})

    def test_whitespace_inside_brackets_is_tolerated(self):
        mask = parse_precis_kinds("paper[ get , search ]")
        assert mask == {"paper": frozenset({"get", "search"})}


# ---------------------------------------------------------------------------
# Fatal error paths (§10.1)
# ---------------------------------------------------------------------------


class TestFatalPaths:
    def test_unknown_verb_in_brackets(self):
        with pytest.raises(ConfigError, match="unknown verb 'fetch'"):
            parse_precis_kinds("paper[fetch]")

    def test_unknown_verb_message_lists_allowed_set(self):
        with pytest.raises(ConfigError) as exc:
            parse_precis_kinds("paper[fetch]")
        msg = str(exc.value)
        # Must enumerate the four allowed verbs so the operator knows what to fix.
        for verb in ("search", "get", "put", "move"):
            assert verb in msg

    def test_empty_brackets(self):
        with pytest.raises(ConfigError, match="empty brackets"):
            parse_precis_kinds("paper[]")

    def test_stray_comma_inside_brackets(self):
        with pytest.raises(ConfigError, match="stray comma"):
            parse_precis_kinds("paper[get,,search]")

    def test_trailing_comma_inside_brackets(self):
        with pytest.raises(ConfigError, match="stray comma"):
            parse_precis_kinds("paper[get,]")

    def test_duplicate_kind(self):
        with pytest.raises(ConfigError, match="listed more than once"):
            parse_precis_kinds("paper,memory,paper")

    def test_duplicate_with_different_verb_masks_still_fatal(self):
        # Different brackets don't rescue duplicate names.
        with pytest.raises(ConfigError, match="listed more than once"):
            parse_precis_kinds("paper[get],paper[search]")

    def test_alias_in_config_is_fatal(self):
        aliases = {"wolfram": "math", "perplexity": "web"}
        with pytest.raises(ConfigError, match="alias 'wolfram'"):
            parse_precis_kinds("paper,wolfram", aliases=aliases)

    def test_alias_error_mentions_canonical_name(self):
        aliases = {"wolfram": "math"}
        with pytest.raises(ConfigError) as exc:
            parse_precis_kinds("wolfram", aliases=aliases)
        msg = str(exc.value)
        assert "wolfram" in msg
        assert "math" in msg

    def test_leading_comma_is_fatal(self):
        with pytest.raises(ConfigError, match="stray comma"):
            parse_precis_kinds(",paper")

    def test_trailing_comma_is_fatal(self):
        with pytest.raises(ConfigError, match="stray comma"):
            parse_precis_kinds("paper,")

    def test_doubled_comma_is_fatal(self):
        with pytest.raises(ConfigError, match="stray comma"):
            parse_precis_kinds("paper,,memory")

    def test_nested_brackets_are_fatal(self):
        with pytest.raises(ConfigError, match="nested"):
            parse_precis_kinds("paper[get[fancy]]")

    def test_unclosed_bracket_is_fatal(self):
        with pytest.raises(ConfigError, match=r"\['"):
            parse_precis_kinds("paper[get,search")

    def test_unopened_bracket_is_fatal(self):
        with pytest.raises(ConfigError, match=r"\]'"):
            parse_precis_kinds("paper]get]")

    def test_kind_name_with_colon_is_malformed(self):
        # A common mistake: copying a URI into the config.
        with pytest.raises(ConfigError, match="malformed"):
            parse_precis_kinds("paper:wang2020state")


# ---------------------------------------------------------------------------
# Unknown kind → non-fatal warning
# ---------------------------------------------------------------------------


class TestUnknownKindWarning:
    def test_unknown_kind_is_skipped_and_warned(self):
        warnings: list[str] = []
        mask = parse_precis_kinds(
            "paper,fruitbat",
            known_kinds={"paper"},
            warnings_out=warnings,
        )
        # fruitbat dropped, paper kept.
        assert mask == {"paper": VERBS}
        # Exactly one warning mentioning the missing kind.
        assert len(warnings) == 1
        assert "fruitbat" in warnings[0]

    def test_unknown_kind_with_brackets_still_warns(self):
        warnings: list[str] = []
        mask = parse_precis_kinds(
            "fruitbat[get]",
            known_kinds={"paper"},
            warnings_out=warnings,
        )
        assert mask == {}
        assert len(warnings) == 1

    def test_multiple_unknown_kinds_each_warn(self):
        warnings: list[str] = []
        mask = parse_precis_kinds(
            "fruitbat,platypus",
            known_kinds={"paper"},
            warnings_out=warnings,
        )
        assert mask == {}
        assert len(warnings) == 2

    def test_no_known_kinds_set_means_accept_all(self):
        # When the caller doesn't pass known_kinds, the parser is
        # grammar-only — every kind is accepted as-is for later filtering.
        mask = parse_precis_kinds("paper,fruitbat")
        assert set(mask) == {"paper", "fruitbat"}


# ---------------------------------------------------------------------------
# load_from_env wrapper
# ---------------------------------------------------------------------------


class TestLoadFromEnv:
    def test_env_unset_returns_none(self):
        assert load_from_env(env={}) is None

    def test_env_empty_returns_none(self):
        assert load_from_env(env={"PRECIS_KINDS": ""}) is None

    def test_env_populated_is_parsed(self):
        mask = load_from_env(env={"PRECIS_KINDS": "paper,memory[get]"})
        assert mask is not None
        assert mask["paper"] == VERBS
        assert mask["memory"] == frozenset({"get"})

    def test_env_alias_fatal_propagates(self):
        with pytest.raises(ConfigError, match="alias"):
            load_from_env(
                env={"PRECIS_KINDS": "wolfram"},
                aliases={"wolfram": "math"},
            )

    def test_env_warnings_captured(self):
        warnings: list[str] = []
        load_from_env(
            env={"PRECIS_KINDS": "paper,fruitbat"},
            known_kinds={"paper"},
            warnings_out=warnings,
        )
        assert len(warnings) == 1
        assert "fruitbat" in warnings[0]


# ---------------------------------------------------------------------------
# Frozen return semantics
# ---------------------------------------------------------------------------


class TestReturnShape:
    def test_verb_sets_are_frozen(self):
        mask = parse_precis_kinds("paper")
        assert isinstance(mask["paper"], frozenset)

    def test_bracketed_verb_sets_are_frozen(self):
        mask = parse_precis_kinds("paper[get,search]")
        assert isinstance(mask["paper"], frozenset)

    def test_result_is_plain_dict_for_assignability(self):
        # A plain dict, so callers can still do `mask[new_kind] = ...`
        # during tests.  The inner frozensets are the invariant part.
        mask = parse_precis_kinds("paper")
        assert isinstance(mask, dict)
