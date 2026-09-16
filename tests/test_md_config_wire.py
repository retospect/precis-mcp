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
from urllib.error import URLError

import pytest

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


class _DeadRemoteEmbedder:
    """Stands in for ``RemoteEmbedder`` when the embedder service is
    down. ``.model`` is an HTTP call (``embedder.py:705 _call`` ->
    ``_urllib_transport``); a bounced service surfaces that as
    ``urllib.error.URLError`` (an ``OSError`` subclass). ``model_calls``
    counts ``.model`` accesses so tests can assert exactly when (if
    ever) ``MdHandler`` probes it."""

    dim = 1024

    def __init__(self) -> None:
        self.model_calls = 0

    @property
    def model(self) -> str:
        self.model_calls += 1
        raise URLError("Connection refused")

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise URLError("Connection refused")

    def embed_one(self, text: str) -> list[float]:
        raise URLError("Connection refused")

    def is_ready(self) -> bool:
        return False

    def warmup(self) -> None:
        return None

    def unload(self) -> None:
        return None


class _CountingEmbedder:
    """A *working* embedder shaped like ``_DeadRemoteEmbedder`` —
    ``.model`` is a property so tests can count probes — used to prove
    the lazy resolution still happens exactly once and is cached."""

    dim = 3

    def __init__(self) -> None:
        self.model_calls = 0

    @property
    def model(self) -> str:
        self.model_calls += 1
        return "working-embedder"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        return None

    def unload(self) -> None:
        return None


def test_md_handler_survives_dead_remote_embedder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (observed in prod): ``MdHandler.__init__`` used to
    build its vector cache off ``self.embedder.model`` at construction
    time — for a REMOTE embedder that's an HTTP call, so a bounced
    embedder service raised ``URLError`` (an ``OSError``) straight out
    of ``boot()`` and killed the whole MCP server at startup.

    Fixed at the source (gr341576): the model/dim probe is now lazy
    (see ``MdHandler.vector_cache``), so ``md`` boots and STAYS
    registered even with a dead embedder — dispatch.py's ``OSError``
    safety net (``test_dispatch.py::test_try_swallows_os_error``)
    remains as a generic backstop for any other handler, but MdHandler
    no longer needs it. Only a search that actually needs embeddings
    degrades — gracefully, to lexical-only — on first use."""
    r = boot(
        store=None,
        embedder=_DeadRemoteEmbedder(),
        md_roots=f"r:{tmp_path}",
    )
    assert isinstance(r, Hub)
    assert "md" in r.kinds
    assert r.loadabilities["md"].loaded is True
    assert "calc" in r.kinds

    h = r.handler_for("md")
    assert isinstance(h, MdHandler)
    with caplog.at_level(logging.WARNING, logger="precis.handlers.md"):
        resp = h.search(q="anything")
    assert resp.body  # doesn't raise; degrades instead
    assert any(
        "degrading to lexical-only search" in rec.message
        and "Connection refused" in rec.message
        for rec in caplog.records
    )


def test_md_handler_dead_embedder_no_network_at_construction(tmp_path: Path) -> None:
    """gr341576 (remaining half): constructing ``MdHandler`` must not
    touch the network at all — model/dim resolution is deferred to
    first real use, never done in ``__init__``."""
    embedder = _DeadRemoteEmbedder()
    handler = MdHandler(hub=Hub(embedder=embedder), roots={"r": tmp_path})
    assert embedder.model_calls == 0
    assert handler.embedder is embedder


def test_md_handler_dead_embedder_degrades_on_first_search(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """First embedding-needing use (``search``) triggers the deferred
    probe; a dead embedder degrades that search to lexical-only rather
    than raising, and the probe is attempted at most once — a second
    search doesn't retry the dead network."""
    (tmp_path / "a.md").write_text("# Heading\n\nsome body text\n", encoding="utf-8")
    embedder = _DeadRemoteEmbedder()
    handler = MdHandler(hub=Hub(embedder=embedder), roots={"r": tmp_path})
    assert embedder.model_calls == 0

    with caplog.at_level(logging.WARNING, logger="precis.handlers.md"):
        handler.search(q="body")
    assert embedder.model_calls == 1
    assert handler.vector_cache is None
    assert any(
        "degrading to lexical-only search" in rec.message
        and "Connection refused" in rec.message
        for rec in caplog.records
    )

    handler.search(q="body")
    assert embedder.model_calls == 1


def test_md_handler_working_embedder_probes_once_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A working embedder still resolves ``vector_cache`` lazily — the
    probe fires on first access, and every later access reuses the
    same cached instance rather than re-reading ``.model``/``.dim``."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    md_root = tmp_path / "root"
    md_root.mkdir()
    embedder = _CountingEmbedder()
    handler = MdHandler(hub=Hub(embedder=embedder), roots={"r": md_root})
    assert embedder.model_calls == 0

    cache1 = handler.vector_cache
    assert cache1 is not None
    assert embedder.model_calls == 1

    cache2 = handler.vector_cache
    assert cache2 is cache1
    assert embedder.model_calls == 1


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
