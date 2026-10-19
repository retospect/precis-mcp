"""``precis pcb-bench`` passes its arguments through to the bench untouched.
No database: the bench is stubbed or refuses before it would connect."""

from __future__ import annotations

import argparse

import pytest

from precis.cli import pcb_bench
from precis.pcb import bench


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    pcb_bench.add_parser(parser.add_subparsers(dest="cmd"))
    return parser.parse_args(argv)


def test_arguments_and_exit_code_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 3

    monkeypatch.setattr(bench, "main", fake_main)
    ns = _parse(["pcb-bench", "sanity", "tb-s1", "--upload", "--version"])
    with pytest.raises(SystemExit) as exc:
        pcb_bench.run(ns)
    assert seen == [["sanity", "tb-s1", "--upload", "--version"]]
    assert exc.value.code == 3


def test_bench_usage_error_surfaces_as_exit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        pcb_bench.run(_parse(["pcb-bench", "bogus"]))
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err
