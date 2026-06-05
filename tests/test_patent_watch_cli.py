"""Tests for the three patent-watch CLI subcommands.

Covers argparse parsing + the pure ``_parse_interval`` helper. The
DB-touching paths (``_run_watch_patents`` etc.) are exercised
indirectly via ``test_patent_watch_db.py`` and ``test_patent_watch.py``;
this file focuses on the parser surface so a broken flag definition
fails fast.
"""

from __future__ import annotations

import pytest

from precis.cli import _build_parser, _parse_interval

# ---------------------------------------------------------------------------
# _parse_interval — converts --every spec to seconds
# ---------------------------------------------------------------------------


class TestParseInterval:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("1h", 3_600),
            ("12h", 12 * 3_600),
            ("1d", 86_400),
            ("7d", 7 * 86_400),
            ("2w", 14 * 86_400),
            ("30", 30),  # bare seconds
            (" 7d ", 7 * 86_400),  # whitespace tolerated
            ("7D", 7 * 86_400),  # case-insensitive
        ],
    )
    def test_valid(self, spec: str, expected: int) -> None:
        assert _parse_interval(spec) == expected

    @pytest.mark.parametrize(
        "spec",
        [
            "",
            "   ",
            "abc",
            "1m",  # minutes not supported
            "1y",  # years not supported
            "0d",
            "-3h",
            "1.5d",
        ],
    )
    def test_invalid(self, spec: str) -> None:
        with pytest.raises(ValueError):
            _parse_interval(spec)


# ---------------------------------------------------------------------------
# watch-patents — argparse surface
# ---------------------------------------------------------------------------


class TestWatchPatentsParse:
    def test_minimal(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["jobs", "watch-patents", "cpc=B01J27/24", "--name", "my-watch"]
        )
        assert args.cmd == "jobs"
        assert args.job == "watch-patents"
        assert args.cql == "cpc=B01J27/24"
        assert args.name == "my-watch"
        assert args.every == "7d"
        assert args.max_per_pass is None
        assert args.delete is False

    def test_all_flags(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "jobs",
                "watch-patents",
                "cpc=B01J27/24",
                "--name",
                "auto",
                "--every",
                "1d",
                "--max-per-pass",
                "5",
                "--database-url",
                "postgresql://h/db",
            ]
        )
        assert args.max_per_pass == 5
        assert args.database_url == "postgresql://h/db"

    def test_delete_form(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["jobs", "watch-patents", "--name", "goner", "--delete"]
        )
        # cql positional is omitted; that's allowed because it's nargs='?'.
        assert args.cql is None
        assert args.delete is True
        assert args.name == "goner"

    def test_name_required(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["jobs", "watch-patents", "cpc=B01J27/24"])


# ---------------------------------------------------------------------------
# list-patent-watches
# ---------------------------------------------------------------------------


class TestListPatentWatchesParse:
    def test_minimal(self) -> None:
        args = _build_parser().parse_args(["jobs", "list-patent-watches"])
        assert args.job == "list-patent-watches"
        assert args.show_cql is False

    def test_show_cql(self) -> None:
        args = _build_parser().parse_args(["jobs", "list-patent-watches", "--show-cql"])
        assert args.show_cql is True


# ---------------------------------------------------------------------------
# run-patent-watches
# ---------------------------------------------------------------------------


class TestRunPatentWatchesParse:
    def test_minimal(self) -> None:
        args = _build_parser().parse_args(["jobs", "run-patent-watches"])
        assert args.job == "run-patent-watches"
        assert args.name is None
        assert args.dry_run is False
        assert args.fair_use_limit_gb is None

    def test_filter_to_one_watch(self) -> None:
        args = _build_parser().parse_args(
            ["jobs", "run-patent-watches", "--name", "catalysts"]
        )
        assert args.name == "catalysts"

    def test_dry_run_and_limit(self) -> None:
        args = _build_parser().parse_args(
            [
                "jobs",
                "run-patent-watches",
                "--dry-run",
                "--fair-use-limit-gb",
                "1.5",
            ]
        )
        assert args.dry_run is True
        assert args.fair_use_limit_gb == 1.5
