"""Tests for the ``precis service`` CLI — parser surface + the ``seed``
subcommand (§L control cutover: the deploy-time, never-clobbers-a-console-
override sibling of ``prio``).
"""

from __future__ import annotations

import argparse

from precis.cli.main import _build_parser
from precis.cli.service import _cmd_prio, _cmd_seed
from precis.workers.service_config import list_service_config, set_service_prio

# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


def test_seed_subcommand_registered():
    parser = _build_parser()
    args = parser.parse_args(["service", "seed", "melchior", "classify", "5"])
    assert args.cmd == "service"
    assert args.service_cmd == "seed"
    assert args.host == "melchior"
    assert args.service == "classify"
    assert args.prio == 5
    assert args.actor is None


def test_seed_accepts_wildcard_host_and_actor():
    parser = _build_parser()
    args = parser.parse_args(
        ["service", "seed", "*", "job_claude_docker", "5", "--actor", "deploy"]
    )
    assert args.host == "*"
    assert args.actor == "deploy"


def test_existing_subcommands_still_parse():
    parser = _build_parser()
    for argv in (
        ["service", "list"],
        ["service", "prio", "melchior", "classify", "0"],
        ["service", "clear", "melchior", "classify"],
    ):
        args = parser.parse_args(argv)
        assert args.cmd == "service"


# ---------------------------------------------------------------------------
# _cmd_seed behaviour (real DB)
# ---------------------------------------------------------------------------


def _ns(**overrides) -> argparse.Namespace:
    defaults = dict(host="melchior", service="classify", prio=5, actor=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_seed_inserts_when_absent(store, capsys) -> None:
    _cmd_seed(store, _ns())
    out = capsys.readouterr().out
    assert "seeded" in out
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    assert rows[("melchior", "classify")]["prio"] == 5


def test_cmd_seed_leaves_a_console_override_untouched(store, capsys) -> None:
    set_service_prio(store, "melchior", "classify", 0, actor="console")
    _cmd_seed(store, _ns(prio=5, actor="deploy"))
    out = capsys.readouterr().out
    assert "already has a row" in out
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    assert rows[("melchior", "classify")]["prio"] == 0
    assert rows[("melchior", "classify")]["actor"] == "console"


def test_cmd_prio_still_upserts_unconditionally(store, capsys) -> None:
    """Contrast case: the operator-facing ``prio`` command DOES clobber —
    only ``seed`` is the never-clobber deploy path."""
    _cmd_seed(store, _ns(prio=5, actor="deploy"))
    _cmd_prio(store, _ns(prio=0, actor="console"))
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    assert rows[("melchior", "classify")]["prio"] == 0
    assert rows[("melchior", "classify")]["actor"] == "console"
