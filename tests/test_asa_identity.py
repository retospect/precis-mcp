"""Discord author -> precis account handle (:mod:`asa_bot.identity`)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from asa_bot import identity


def _author(name="ada", disc="0", uid=123456789012345678):
    return SimpleNamespace(name=name, discriminator=disc, id=uid)


def test_display_handle_legacy_and_new_style():
    assert identity.display_handle(_author()) == "ada"
    assert identity.display_handle(_author(disc="1234")) == "ada#1234"


def test_mapped_author_gets_account_abbrev(monkeypatch):
    seen = []

    async def fake(dsn, uid):
        seen.append((dsn, uid))
        return "rs"

    monkeypatch.setattr(identity, "resolve_account_abbrev", fake)
    h = asyncio.run(identity.resolve_author_handle("dsn", _author()))
    assert h == "rs"
    assert seen == [("dsn", 123456789012345678)]


def test_unmapped_author_keeps_display_name(monkeypatch):
    async def fake(dsn, uid):
        return None

    monkeypatch.setattr(identity, "resolve_account_abbrev", fake)
    assert asyncio.run(identity.resolve_author_handle("dsn", _author(disc="9"))) == (
        "ada#9"
    )


def test_no_dsn_returns_none_without_connecting():
    assert asyncio.run(identity.resolve_account_abbrev("", 1)) is None


def test_db_failure_is_swallowed(monkeypatch):
    import psycopg

    async def boom(*a, **k):
        raise OSError("db down")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", boom)
    assert asyncio.run(identity.resolve_account_abbrev("postgres://x", 5)) is None


def test_resolve_speaker_returns_the_abbrev_only_when_mapped(monkeypatch):
    async def mapped(dsn, uid):
        return "rs"

    async def unmapped(dsn, uid):
        return None

    monkeypatch.setattr(identity, "resolve_account_abbrev", mapped)
    assert asyncio.run(identity.resolve_speaker("dsn", _author())) == ("rs", "rs")
    monkeypatch.setattr(identity, "resolve_account_abbrev", unmapped)
    # A display name that happens to equal an abbrev never becomes the account.
    assert asyncio.run(identity.resolve_speaker("dsn", _author(name="rs"))) == (
        "rs",
        None,
    )
