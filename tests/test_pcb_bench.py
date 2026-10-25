"""gr513867 — ``python -m precis.pcb.bench <example-board>`` resolution.

The route itself is exercised by the bench (and the gripe's own runs);
these tests pin only the cheap contract: the name lookup and the usage
refusals, so a typo names what exists instead of falling through to the
sanity usage line.
"""

from __future__ import annotations

from precis.pcb import bench


def test_resolve_board_dir_exact_and_prefix() -> None:
    # "nano" is ambiguous between nano_usb_pd and nano_usb_pd_r2 — the
    # shortest match is the base board.
    assert bench._resolve_board_dir("nano") == "nano_usb_pd"
    assert bench._resolve_board_dir("nano_usb_pd_r2") == "nano_usb_pd_r2"
    assert bench._resolve_board_dir("sanity") == "sanity"


def test_resolve_board_dir_unknown_returns_none() -> None:
    assert bench._resolve_board_dir("definitely-not-a-board") is None


def test_main_unknown_board_refuses_with_dir_list(capsys) -> None:
    assert bench.main(["definitely-not-a-board"]) == 2
    assert "examples/boards" in capsys.readouterr().err


def test_main_no_args_shows_usage(capsys) -> None:
    assert bench.main([]) == 2
    assert "<example-board>" in capsys.readouterr().err


def test_main_unknown_flag_refuses(capsys) -> None:
    # The example-board path refuses before resolving or routing.
    assert bench.main(["nano", "--bogus-flag"]) == 2
    assert "bogus-flag" in capsys.readouterr().err
