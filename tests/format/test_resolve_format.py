"""Unit tests for :func:`precis.cli._common.resolve_format`.

The CLI ``--format`` flag is purely a precedence problem: explicit
flag value > TTY default > pipe default. The tests pin all three
branches plus the override-default knobs.
"""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import contextmanager

import pytest

from precis.cli._common import (
    add_format_argument,
    resolve_format,
)


@contextmanager
def _stdout(*, isatty: bool):
    """Replace ``sys.stdout`` with a stream that lies about TTY-ness.

    `resolve_format` reads `sys.stdout.isatty()`; the standard
    `pytest.capsys` capture stream lies the other way (`isatty()`
    returns False), so we patch in our own controllable stream.
    """

    class _Stream(io.StringIO):
        def isatty(self) -> bool:  # type: ignore[override]
            return isatty

    saved = sys.stdout
    sys.stdout = _Stream()
    try:
        yield
    finally:
        sys.stdout = saved


class TestAddFormatArgument:
    def test_flag_registered_with_choices(self):
        p = argparse.ArgumentParser()
        add_format_argument(p)
        args = p.parse_args(["--format", "toon"])
        assert args.format == "toon"

    def test_default_is_none(self):
        p = argparse.ArgumentParser()
        add_format_argument(p)
        args = p.parse_args([])
        # `None` is the explicit "no override" sentinel so
        # `resolve_format` can pick the contextual default.
        assert args.format is None

    def test_rejects_unknown_format(self):
        p = argparse.ArgumentParser()
        add_format_argument(p)
        with pytest.raises(SystemExit):
            p.parse_args(["--format", "yaml"])


class TestResolveFormat:
    def _ns(self, fmt: str | None) -> argparse.Namespace:
        return argparse.Namespace(format=fmt)

    def test_explicit_flag_wins_on_tty(self):
        with _stdout(isatty=True):
            assert resolve_format(self._ns("toon")) == "toon"

    def test_explicit_flag_wins_on_pipe(self):
        with _stdout(isatty=False):
            assert resolve_format(self._ns("table")) == "table"

    def test_no_flag_tty_defaults_to_table(self):
        with _stdout(isatty=True):
            assert resolve_format(self._ns(None)) == "table"

    def test_no_flag_pipe_defaults_to_toon(self):
        with _stdout(isatty=False):
            assert resolve_format(self._ns(None)) == "toon"

    def test_override_default_tty(self):
        with _stdout(isatty=True):
            assert resolve_format(self._ns(None), default_tty="toon") == "toon"

    def test_override_default_pipe(self):
        with _stdout(isatty=False):
            assert resolve_format(self._ns(None), default_pipe="json") == "json"

    def test_namespace_without_format_attribute_defaults(self):
        # A subcommand that forgot to call `add_format_argument`
        # should not crash; we degrade to the contextual default.
        ns = argparse.Namespace()  # no `format` attribute
        with _stdout(isatty=False):
            assert resolve_format(ns) == "toon"
