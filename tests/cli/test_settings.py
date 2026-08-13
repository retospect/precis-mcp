"""Unit tests for ``precis settings`` (:mod:`precis.cli.settings`).

No real DB: uses the same ``_FakeConn``/``_FakeStore`` app_settings fakes
``tests/test_settings.py`` exercises the resolver layer with, driving the
CLI's private ``_list``/``_get``/``_set`` helpers directly (``run()`` itself
just does ``Store.connect`` + dispatch, not worth re-testing) and
``coerce_for_write`` — the type-validation gate shared with the /settings
web route.
"""

from __future__ import annotations

import io
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, cast

import pytest

from precis import settings as psettings
from precis.cli import settings as cli_settings
from tests.test_settings import _FakeConn, _FakeStore


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    psettings.bind_store(None)
    psettings.invalidate()
    yield
    psettings.bind_store(None)
    psettings.invalidate()


# ── coerce_for_write ─────────────────────────────────────────────────────


def test_coerce_bool_accepts_common_spellings() -> None:
    entry = psettings.SettingSpec(
        key="k", type="bool", env_var=None, default=False, doc=""
    )
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        assert cli_settings.coerce_for_write(entry, truthy) is True
    for falsy in ("0", "false", "FALSE", "no", "off"):
        assert cli_settings.coerce_for_write(entry, falsy) is False


def test_coerce_bool_rejects_junk() -> None:
    entry = psettings.SettingSpec(
        key="k", type="bool", env_var=None, default=False, doc=""
    )
    with pytest.raises(ValueError, match="not a valid bool"):
        cli_settings.coerce_for_write(entry, "maybe")


def test_coerce_float_and_int() -> None:
    fentry = psettings.SettingSpec(
        key="f", type="float", env_var=None, default=0.0, doc=""
    )
    assert cli_settings.coerce_for_write(fentry, "3.5") == 3.5
    with pytest.raises(ValueError, match="not a valid float"):
        cli_settings.coerce_for_write(fentry, "abc")

    ientry = psettings.SettingSpec(key="i", type="int", env_var=None, default=0, doc="")
    assert cli_settings.coerce_for_write(ientry, "7") == 7
    with pytest.raises(ValueError, match="not a valid int"):
        cli_settings.coerce_for_write(ientry, "7.5")


def test_coerce_str_passes_through() -> None:
    entry = psettings.SettingSpec(
        key="s", type="str", env_var=None, default=None, doc=""
    )
    assert cli_settings.coerce_for_write(entry, "hello world") == "hello world"


# ── argparse wiring ──────────────────────────────────────────────────────


def test_add_parser_registers_four_subcommands() -> None:
    import argparse

    top = argparse.ArgumentParser()
    sub = top.add_subparsers(dest="cmd")
    cli_settings.add_parser(sub)

    for argv, expect_key, expect_value in (
        (["settings", "list"], None, None),
        (["settings", "get", "budget.hourly_usd"], "key", "budget.hourly_usd"),
        (["settings", "set", "budget.hourly_usd", "3"], "key", "budget.hourly_usd"),
        (["settings", "clear", "budget.hourly_usd"], "key", "budget.hourly_usd"),
    ):
        args = top.parse_args(argv)
        assert args.cmd == "settings"
        if expect_key:
            assert getattr(args, expect_key) == expect_value


# ── list / get / set / clear (against the app_settings fakes) ───────────


def test_list_shows_all_registry_keys(capsys: pytest.CaptureFixture) -> None:
    store = cast("Any", _FakeStore(_FakeConn(value=None)))
    cli_settings._list(store, psettings)
    out = capsys.readouterr().out
    for key in psettings.REGISTRY:
        assert key in out


def test_set_refuses_unregistered_key() -> None:
    store = cast("Any", _FakeStore(_FakeConn(value=None)))
    args = Namespace(key="totally.unregistered.key", value="x")
    buf = io.StringIO()
    with redirect_stderr(buf), pytest.raises(SystemExit) as exc:
        cli_settings._set(args, store, psettings)
    assert exc.value.code == 2
    msg = buf.getvalue()
    assert "unregistered" in msg
    assert "precis.settings.REGISTRY" in msg


def test_set_validates_type_before_write() -> None:
    conn = _FakeConn(value=None)
    store = cast("Any", _FakeStore(conn))
    args = Namespace(key="budget.hourly_usd", value="not-a-float")
    buf = io.StringIO()
    with redirect_stderr(buf), pytest.raises(SystemExit) as exc:
        cli_settings._set(args, store, psettings)
    assert exc.value.code == 2
    assert "not a valid float" in buf.getvalue()
    # The bad value never reached the store — no INSERT was issued.
    assert not any("INSERT" in s.upper() for s in conn.sql)
    assert conn.value is None


def test_set_then_get_resolves_at_db_layer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("PRECIS_BUDGET_HOURLY_USD", raising=False)
    conn = _FakeConn(value=None)
    store = cast("Any", _FakeStore(conn))
    cli_settings._set(Namespace(key="budget.hourly_usd", value="7.5"), store, psettings)
    out = capsys.readouterr().out
    assert "set budget.hourly_usd = 7.5" in out

    cli_settings._get(Namespace(key="budget.hourly_usd"), store, psettings)
    out = capsys.readouterr().out
    assert "layer=db" in out
    assert "7.5" in out


def test_clear_reverts_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_BUDGET_HOURLY_USD", raising=False)
    conn = _FakeConn(value="7.5")
    store = cast("Any", _FakeStore(conn))
    assert psettings.get_float("budget.hourly_usd", store=store) == 7.5
    psettings.clear_setting("budget.hourly_usd", store=store)
    assert (
        psettings.get_float("budget.hourly_usd", store=store) == 5.0
    )  # compiled default


def test_run_dispatches_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run()`` itself: patch ``Store.connect`` so no real DB is touched."""
    from precis.store import Store

    conn = _FakeConn(value="7.5")
    store = cast("Any", _FakeStore(conn))
    monkeypatch.setattr(Store, "connect", classmethod(lambda cls, dsn: store))
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    args = Namespace(
        settings_cmd="clear", key="budget.hourly_usd", database_url="postgresql://x"
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_settings.run(args)
    assert "cleared budget.hourly_usd" in buf.getvalue()
    assert conn.value is None
