"""Tests for ``PRECIS_MD_ROOTS`` parsing and hub wiring.

Mirrors ``tests/test_python_config_wire.py``. Covers:
- ``parse_md_roots`` — thin wrapper over the shared
  ``precis.handlers._roots.parse_alias_roots`` (already exercised
  from the python side); a couple of smoke cases confirm the wrapper
  passes ``env_var='PRECIS_MD_ROOTS'`` through correctly.
- ``boot(...)`` instantiates ``MdHandler`` only when at least one
  valid root parses, regardless of whether a store is present.
- The handler is hidden (deferred, with reason) when no roots are
  configured, and when every entry is malformed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from precis.dispatch import Hub, boot
from precis.handlers.md import MdHandler, parse_md_roots

# ---------------------------------------------------------------------------
# parse_md_roots
# ---------------------------------------------------------------------------


def test_parse_returns_empty_for_none() -> None:
    assert parse_md_roots(None) == {}


def test_parse_returns_empty_for_empty_string() -> None:
    assert parse_md_roots("") == {}
    assert parse_md_roots("   ") == {}


def test_parse_single_entry(tmp_path: Path) -> None:
    raw = f"docs:{tmp_path}"
    out = parse_md_roots(raw)
    assert out == {"docs": tmp_path.resolve()}


def test_parse_multiple_entries(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    raw = f"a:{a},b:{b}"
    out = parse_md_roots(raw)
    assert out == {"a": a.resolve(), "b": b.resolve()}


def test_parse_skips_entry_missing_colon(tmp_path: Path, caplog) -> None:
    raw = f"junk-no-colon,r:{tmp_path}"
    with caplog.at_level(logging.WARNING):
        out = parse_md_roots(raw)
    assert out == {"r": tmp_path.resolve()}
    assert any(
        "PRECIS_MD_ROOTS" in r.message and "missing ':'" in r.message
        for r in caplog.records
    )


def test_parse_skips_nonexistent_path(tmp_path: Path, caplog) -> None:
    raw = f"good:{tmp_path},bad:{tmp_path}/no-such-dir"
    with caplog.at_level(logging.WARNING):
        out = parse_md_roots(raw)
    assert out == {"good": tmp_path.resolve()}
    assert any("PRECIS_MD_ROOTS" in r.message for r in caplog.records)


def test_parse_first_alias_wins_on_duplicates(tmp_path: Path, caplog) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    raw = f"r:{a},r:{b}"
    with caplog.at_level(logging.WARNING):
        out = parse_md_roots(raw)
    assert out == {"r": a.resolve()}
    assert any("duplicate alias" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# boot() integration
# ---------------------------------------------------------------------------


def test_md_handler_hidden_when_no_roots() -> None:
    r = boot()
    assert "md" not in r.kinds
    assert "md" in r.loadabilities
    assert r.loadabilities["md"].loaded is False
    assert r.loadabilities["md"].reason == "missing PRECIS_MD_ROOTS"


def test_md_handler_hidden_when_all_entries_invalid(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        r = boot(md_roots="bogus,no-colon-either")
    assert "md" not in r.kinds
    assert r.loadabilities["md"].reason == "PRECIS_MD_ROOTS parsed empty"


def test_md_handler_present_when_one_root_valid(tmp_path: Path) -> None:
    r = boot(md_roots=f"r:{tmp_path}")
    assert "md" in r.kinds
    h = r.handler_for("md")
    assert isinstance(h, MdHandler)
    assert h.roots == {"r": tmp_path.resolve()}


def test_md_handler_present_without_store(tmp_path: Path) -> None:
    """md doesn't depend on a store; it should appear even when
    boot() is called with store=None."""
    r = boot(store=None, md_roots=f"r:{tmp_path}")
    assert "md" in r.kinds


def test_md_handler_present_with_multiple_valid_roots(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    r = boot(md_roots=f"a:{a},b:{b}")
    h = r.handler_for("md")
    assert isinstance(h, MdHandler)
    assert set(h.roots) == {"a", "b"}


def test_md_handler_no_embedder_without_store(tmp_path: Path) -> None:
    """Storeless boot has no embedder; the md handler degrades to
    lexical-only rather than erroring."""
    r = boot(store=None, md_roots=f"r:{tmp_path}")
    h = r.handler_for("md")
    assert isinstance(h, MdHandler)
    assert h.embedder is None
    assert h.vector_cache is None


# ---------------------------------------------------------------------------
# Smoke: end-to-end dispatch table construction
# ---------------------------------------------------------------------------


def test_dispatch_resolves_md_kind(tmp_path: Path) -> None:
    r = boot(md_roots=f"r:{tmp_path}")
    assert "md" in r.kinds
    assert isinstance(r, Hub)
    assert r.get("md", "get") is not None
    assert r.get("md", "search") is not None
    h = r.handler_for("md")
    assert isinstance(h, MdHandler)


def test_config_field_default_is_none() -> None:
    from precis.config import PrecisConfig

    cfg = PrecisConfig()
    assert cfg.md_roots is None
