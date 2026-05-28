"""Tests for ``PRECIS_PYTHON_ROOTS`` parsing and hub wiring.

Covers:
- ``parse_python_roots`` parser corner cases (empty, malformed,
  duplicates, non-dir, whitespace).
- ``builtins(...)`` instantiates ``PythonHandler`` only when at least
  one valid root parses, regardless of whether a store is present.
- The handler is hidden when no roots are configured.
"""

from __future__ import annotations

import logging
from pathlib import Path

from precis.dispatch import Hub, boot
from precis.handlers.python import PythonHandler, parse_python_roots

# ---------------------------------------------------------------------------
# parse_python_roots
# ---------------------------------------------------------------------------


def test_parse_returns_empty_for_none() -> None:
    assert parse_python_roots(None) == {}


def test_parse_returns_empty_for_empty_string() -> None:
    assert parse_python_roots("") == {}
    assert parse_python_roots("   ") == {}


def test_parse_single_entry(tmp_path: Path) -> None:
    raw = f"r:{tmp_path}"
    out = parse_python_roots(raw)
    assert out == {"r": tmp_path.resolve()}


def test_parse_multiple_entries(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    raw = f"a:{a},b:{b}"
    out = parse_python_roots(raw)
    assert out == {"a": a.resolve(), "b": b.resolve()}


def test_parse_strips_surrounding_whitespace(tmp_path: Path) -> None:
    raw = f"  r  :  {tmp_path}  "
    out = parse_python_roots(raw)
    assert out == {"r": tmp_path.resolve()}


def test_parse_skips_entry_missing_colon(tmp_path: Path, caplog) -> None:
    raw = f"junk-no-colon,r:{tmp_path}"
    with caplog.at_level(logging.WARNING):
        out = parse_python_roots(raw)
    assert out == {"r": tmp_path.resolve()}
    assert any("missing ':'" in r.message for r in caplog.records)


def test_parse_skips_empty_alias_or_path(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        out = parse_python_roots(f":{tmp_path},r:")
    assert out == {}
    assert any("empty alias or path" in r.message for r in caplog.records)


def test_parse_skips_nonexistent_path(tmp_path: Path, caplog) -> None:
    raw = f"good:{tmp_path},bad:{tmp_path}/no-such-dir"
    with caplog.at_level(logging.WARNING):
        out = parse_python_roots(raw)
    assert out == {"good": tmp_path.resolve()}
    assert any("not a directory" in r.message for r in caplog.records)


def test_parse_first_alias_wins_on_duplicates(tmp_path: Path, caplog) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    raw = f"r:{a},r:{b}"
    with caplog.at_level(logging.WARNING):
        out = parse_python_roots(raw)
    assert out == {"r": a.resolve()}
    assert any("duplicate alias" in r.message for r in caplog.records)


def test_parse_expands_tilde(tmp_path: Path, monkeypatch) -> None:
    """``~`` in paths is expanded against the platform's home env var
    so the env var can use the same shorthand the user types in their
    shell. POSIX reads ``$HOME``; Windows reads ``%USERPROFILE%``.
    Set both so the test passes on every platform.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    out = parse_python_roots("r:~")
    assert out == {"r": tmp_path.resolve()}


def test_parse_ignores_blank_entries(tmp_path: Path) -> None:
    """Trailing/double commas don't break the parse."""
    raw = f",,r:{tmp_path},,"
    out = parse_python_roots(raw)
    assert out == {"r": tmp_path.resolve()}


# ---------------------------------------------------------------------------
# boot() integration
# ---------------------------------------------------------------------------


def test_python_handler_hidden_when_no_roots() -> None:
    """No PRECIS_PYTHON_ROOTS → no python kind, regardless of store."""
    r = boot()
    assert "python" not in r.kinds


def test_python_handler_hidden_when_all_entries_invalid(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        r = boot(python_roots="bogus,no-colon-either")
    assert "python" not in r.kinds


def test_python_handler_present_when_one_root_valid(tmp_path: Path) -> None:
    r = boot(python_roots=f"r:{tmp_path}")
    assert "python" in r.kinds
    h = r.handler_for("python")
    assert isinstance(h, PythonHandler)
    assert h.roots == {"r": tmp_path.resolve()}


def test_python_handler_present_without_store(tmp_path: Path) -> None:
    """Python kind doesn't depend on a store; it should appear even
    when boot() is called with store=None."""
    r = boot(store=None, python_roots=f"r:{tmp_path}")
    assert "python" in r.kinds


def test_python_handler_present_with_multiple_valid_roots(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    r = boot(python_roots=f"a:{a},b:{b}")
    h = r.handler_for("python")
    assert isinstance(h, PythonHandler)
    assert set(h.roots) == {"a", "b"}


# ---------------------------------------------------------------------------
# Smoke: end-to-end dispatch table construction
# ---------------------------------------------------------------------------


def test_dispatch_resolves_python_kind(tmp_path: Path) -> None:
    """``boot()`` populates ``(python, get, None)`` so the runtime
    dispatch table can route ``get(kind='python', ...)`` calls."""
    r = boot(python_roots=f"r:{tmp_path}")
    assert "python" in r.kinds
    assert isinstance(r, Hub)
    assert r.get("python", "get") is not None
    h = r.handler_for("python")
    assert isinstance(h, PythonHandler)


def test_config_field_default_is_none() -> None:
    """`PrecisConfig.python_roots` defaults to None — kind hidden by default."""
    from precis.config import PrecisConfig

    cfg = PrecisConfig()
    assert cfg.python_roots is None
