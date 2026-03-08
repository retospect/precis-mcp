"""Tests for grep.py — pattern parsing and matching."""

import pytest

from precis.grep import GrepPattern, parse_grep


class TestParseGrep:
    def test_plain_substring(self):
        p = parse_grep("wibble")
        assert p.matches("contains wibble here")
        assert p.matches("WIBBLE uppercase")
        assert not p.matches("no match")

    def test_case_sensitive_slash(self):
        p = parse_grep("/Wibble/")
        assert p.matches("contains Wibble here")
        assert not p.matches("contains wibble here")

    def test_regex_case_insensitive(self):
        p = parse_grep("/wib{2}le/i")
        assert p.matches("wibble")
        assert p.matches("WIBBLE")
        assert not p.matches("wible")

    def test_regex_case_sensitive(self):
        p = parse_grep("/Wib{2}le/")
        assert p.matches("Wibble")
        assert not p.matches("wibble")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            parse_grep("")

    def test_invalid_regex_raises(self):
        with pytest.raises(ValueError, match="Invalid regex"):
            parse_grep("/[invalid/")

    def test_special_chars_escaped_in_plain(self):
        p = parse_grep("F1=0.93")
        assert p.matches("F1=0.93 score")
        assert not p.matches("F1=0X93")  # dot should be literal
