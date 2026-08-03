"""Tests for the ``precis service`` CLI — parser surface + the ``seed``
subcommand (§L control cutover: the deploy-time, never-clobbers-a-console-
override sibling of ``prio``).
"""

from __future__ import annotations

import argparse

import pytest

from precis.cli.main import _build_parser
from precis.cli.service import _cmd_prio, _cmd_release, _cmd_reserve, _cmd_seed
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


def test_reserve_release_subcommands_registered():
    parser = _build_parser()
    args = parser.parse_args(
        ["service", "reserve", "--host", "melchior", "--hours", "2"]
    )
    assert args.service_cmd == "reserve"
    assert args.host == "melchior"
    assert args.hours == 2.0
    assert args.all is False

    args2 = parser.parse_args(["service", "release", "--all"])
    assert args2.service_cmd == "release"
    assert args2.all is True


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


# ---------------------------------------------------------------------------
# reserve / release (§B-2)
# ---------------------------------------------------------------------------


def _reserve_ns(**overrides):
    defaults = dict(host="melchior", all=False, hours=4.0, actor=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _release_ns(**overrides):
    defaults = dict(host="melchior", all=False)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_reserve_creates_row_with_expiry(store, capsys) -> None:
    _cmd_reserve(store, _reserve_ns())
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    row = rows[("melchior", "reserve")]
    assert row["prio"] == 1
    assert row["expires_at"] is not None
    assert "reserved until" in capsys.readouterr().out


def test_cmd_reserve_all_flag_uses_wildcard(store, capsys) -> None:
    _cmd_reserve(store, _reserve_ns(all=True))
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    assert ("*", "reserve") in rows


def test_cmd_release_removes_row(store, capsys) -> None:
    _cmd_reserve(store, _reserve_ns())
    _cmd_release(store, _release_ns())
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    assert ("melchior", "reserve") not in rows
    assert "released reserve" in capsys.readouterr().out


def test_cmd_release_no_row_reports_cleanly(store, capsys) -> None:
    _cmd_release(store, _release_ns())
    assert "no reserve row" in capsys.readouterr().out


def test_cmd_reserve_hours_bounds_enforced(store) -> None:
    with pytest.raises(ValueError):
        _cmd_reserve(store, _reserve_ns(hours=0))
    with pytest.raises(ValueError):
        _cmd_reserve(store, _reserve_ns(hours=200))


def test_generic_verbs_refuse_reserve_pseudo_service(store, capsys) -> None:
    """The generic verbs must not touch the ``reserve`` pseudo-service —
    ``set_service_prio``'s UPSERT doesn't set ``expires_at``, so a
    ``service prio <host> reserve N`` would mint an inert row (or mutate
    ``prio`` on a live reserve without touching what gates it). One door:
    ``service reserve`` / ``release``."""
    from precis.cli.service import _cmd_clear, _cmd_model

    for cmd, ns in (
        (_cmd_prio, _ns(service="reserve", prio=1)),
        (_cmd_seed, _ns(service="reserve", prio=1)),
        (_cmd_model, _ns(service="reserve", model="m", clear=False)),
        (_cmd_clear, _ns(service="reserve")),
    ):
        with pytest.raises(SystemExit) as exc:
            cmd(store, ns)
        assert exc.value.code == 2
        assert "pseudo-service" in capsys.readouterr().err
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    assert ("melchior", "reserve") not in rows
