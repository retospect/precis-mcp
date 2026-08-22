"""``precis users`` (:mod:`precis.cli.users`).

Drives the private helpers against the real ``store`` fixture — ``run()``
itself is only ``Store.connect`` + dispatch. The parser is exercised too,
because an ``add_parser`` that doesn't wire a flag is a --help lie that no
unit test on the helpers would catch.
"""

from __future__ import annotations

import argparse
import io
from argparse import Namespace
from contextlib import redirect_stdout

import pytest

from precis.cli import users as cli_users
from precis.store import Store
from precis.users import (
    ALGO_PEPPERED,
    ALGO_PLAIN,
    MIN_PASSWORD_LENGTH,
    verify_password,
)


def _parse(argv: list[str]) -> Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    cli_users.add_parser(sub)
    return parser.parse_args(argv)


def _add(store: Store, login: str = "reto", abbrev: str = "rs", **over: object) -> str:
    args = _parse(
        ["users", "add", login, "--abbrev", abbrev, "--password-stdin", "--no-pepper"]
    )
    for k, v in over.items():
        setattr(args, k, v)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_users._add(args, store)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _password_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_users.sys, "stdin", io.StringIO("hunter2-swordfish\n"))


# ── parser ───────────────────────────────────────────────────────────


def test_add_requires_abbrev() -> None:
    with pytest.raises(SystemExit):
        _parse(["users", "add", "reto"])


def test_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        _parse(["users"])


# ── passwords never come from argv ───────────────────────────────────


def test_no_subcommand_accepts_a_password_argument() -> None:
    """``ps`` and shell history both leak argv — if this ever starts
    parsing, the leak is back."""
    for cmd in (
        ["users", "add", "reto", "--abbrev", "rs"],
        ["users", "passwd", "reto"],
    ):
        with pytest.raises(SystemExit):
            _parse([*cmd, "--password", "hunter2-swordfish"])


def test_stdin_password_must_not_be_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_users.sys, "stdin", io.StringIO("   \n"))
    with pytest.raises(SystemExit):
        cli_users._read_password(Namespace(password_stdin=True))


def test_cli_enforces_the_same_length_floor_as_the_web_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy only the ``/account`` form applies isn't a policy."""
    monkeypatch.setattr(
        cli_users.sys, "stdin", io.StringIO("a" * (MIN_PASSWORD_LENGTH - 1) + "\n")
    )
    with pytest.raises(SystemExit):
        cli_users._read_password(Namespace(password_stdin=True))


# ── round trips ──────────────────────────────────────────────────────


def test_add_then_authenticate(store: Store) -> None:
    out = _add(store, name="Reto Stamm", email="reto@example.com")
    assert "created reto (rs)" in out
    assert ALGO_PLAIN in out

    found = store.get_web_user_credentials("reto")
    assert found is not None
    user, record = found
    assert user.full_name == "Reto Stamm"
    assert verify_password("hunter2-swordfish", record)


def test_add_uses_the_vault_pepper_by_default(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_WEB_PASSWORD_PEPPER", "the-pepper")
    args = _parse(["users", "add", "reto", "--abbrev", "rs", "--password-stdin"])
    with redirect_stdout(io.StringIO()):
        cli_users._add(args, store)
    found = store.get_web_user_credentials("reto")
    assert found is not None
    assert found[1].password_algo == ALGO_PEPPERED
    assert verify_password("hunter2-swordfish", found[1], pepper="the-pepper")


def test_passwd_changes_it(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    _add(store)
    monkeypatch.setattr(cli_users.sys, "stdin", io.StringIO("newpass-longer\n"))
    args = _parse(["users", "passwd", "reto", "--password-stdin", "--no-pepper"])
    with redirect_stdout(io.StringIO()):
        cli_users._passwd(args, store)
    found = store.get_web_user_credentials("reto")
    assert found is not None
    assert verify_password("newpass-longer", found[1])


def test_passwd_on_unknown_user_exits(store: Store) -> None:
    args = _parse(["users", "passwd", "nobody", "--password-stdin", "--no-pepper"])
    with pytest.raises(SystemExit):
        cli_users._passwd(args, store)


def test_list_says_what_to_do_when_empty(store: Store) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_users._list(store)
    assert "503" in buf.getvalue()


def test_list_renders_the_roster(store: Store) -> None:
    _add(store)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_users._list(store)
    line = buf.getvalue()
    assert "reto" in line and "rs" in line and "active" in line and "never" in line


def test_disable_enable(store: Store) -> None:
    _add(store)
    with redirect_stdout(io.StringIO()):
        cli_users._toggle(_parse(["users", "disable", "reto"]), store, disabled=True)
    assert store.count_web_users() == 0
    with redirect_stdout(io.StringIO()):
        cli_users._toggle(_parse(["users", "enable", "reto"]), store, disabled=False)
    assert store.count_web_users() == 1


def test_rm(store: Store) -> None:
    _add(store)
    with redirect_stdout(io.StringIO()):
        cli_users._rm(_parse(["users", "rm", "reto"]), store)
    assert store.get_web_user("reto") is None
    with pytest.raises(SystemExit):
        cli_users._rm(_parse(["users", "rm", "reto"]), store)


def test_feed_token_prints_a_subscribable_url(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(store)
    monkeypatch.setenv("PRECIS_PODCAST_BASE_URL", "https://host.ts.net")
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_users._feed_token(_parse(["users", "feed-token", "reto"]), store)
    out = buf.getvalue()
    assert "https://host.ts.net/podcast/feed.xml?t=" in out

    from precis.users import feed_token_digest

    token = out.split("?t=")[1].strip()
    assert store.get_web_user_by_feed_token(feed_token_digest(token)) is not None


def test_feed_token_clear_revokes(store: Store) -> None:
    _add(store)
    with redirect_stdout(io.StringIO()):
        cli_users._feed_token(_parse(["users", "feed-token", "reto"]), store)
    args = _parse(["users", "feed-token", "reto", "--clear"])
    with redirect_stdout(io.StringIO()):
        cli_users._feed_token(args, store)
    user = store.get_web_user("reto")
    assert user is not None
