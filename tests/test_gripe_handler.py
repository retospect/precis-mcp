"""Tests for the gripe handler — agent feedback log.

Covers:
- Reading an empty log
- Appending entries (with and without tags)
- /recent ordering (newest first)
- /all ordering (oldest first, every entry)
- Mode tolerance — server's default mode='replace' accepted as append
- Destructive modes (delete/move) rejected
- Empty text rejected
- Round-trip through server.get / server.put
- Round-trip through stats() — gripe shows up in kinds-by-verb

No DB dependency — uses PRECIS_GRIPE_PATH to redirect to a tmp file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis import server
from precis.handlers.gripe import (
    GripeHandler,
    _append_gripe,
    _gripe_path,
    _read_entries,
)
from precis.protocol import ErrorCode, PrecisError


@pytest.fixture
def tmp_gripe_log(tmp_path, monkeypatch):
    """Redirect the gripe log to a tmp file via the env var."""
    log_path = tmp_path / "gripes.md"
    monkeypatch.setenv("PRECIS_GRIPE_PATH", str(log_path))
    return log_path


# ---------------------------------------------------------------------------
# Storage primitives
# ---------------------------------------------------------------------------


class TestStoragePrimitives:
    def test_path_honours_env_var(self, tmp_gripe_log):
        assert _gripe_path() == tmp_gripe_log

    def test_read_entries_on_missing_file_returns_empty(self, tmp_gripe_log):
        assert _read_entries(tmp_gripe_log) == []

    def test_append_then_read_round_trip(self, tmp_gripe_log):
        ts = _append_gripe("first gripe", tags=["ingestion"])
        entries = _read_entries(tmp_gripe_log)
        assert len(entries) == 1
        assert entries[0]["text"] == "first gripe"
        assert entries[0]["tags"] == ["ingestion"]
        assert entries[0]["ts"] == ts

    def test_append_without_tags(self, tmp_gripe_log):
        _append_gripe("no tags here")
        entries = _read_entries(tmp_gripe_log)
        assert entries[0]["tags"] == []

    def test_multiple_appends_preserve_order(self, tmp_gripe_log):
        _append_gripe("first")
        _append_gripe("second")
        _append_gripe("third")
        entries = _read_entries(tmp_gripe_log)
        assert [e["text"] for e in entries] == ["first", "second", "third"]

    def test_multiline_text_preserved(self, tmp_gripe_log):
        _append_gripe("line one\nline two\nline three", tags=["multiline"])
        entries = _read_entries(tmp_gripe_log)
        assert entries[0]["text"] == "line one\nline two\nline three"


# ---------------------------------------------------------------------------
# Handler — direct read / put
# ---------------------------------------------------------------------------


class TestHandlerRead:
    def test_empty_log_renders_friendly_hint(self, tmp_gripe_log):
        h = GripeHandler()
        out = h.read()
        assert "gripe log is empty" in out
        # Renders the path so the agent knows where the file lives.
        assert str(tmp_gripe_log) in out
        # Empty hint points at the put recipe.
        assert "put(type='gripe'" in out

    def test_recent_returns_newest_first(self, tmp_gripe_log):
        _append_gripe("oldest")
        _append_gripe("middle")
        _append_gripe("newest")
        h = GripeHandler()
        out = h.read(view="recent")
        # Find the index of each text in the output.
        i_old = out.index("oldest")
        i_mid = out.index("middle")
        i_new = out.index("newest")
        # Newest first → newest index < middle index < oldest index.
        assert i_new < i_mid < i_old

    def test_all_returns_oldest_first(self, tmp_gripe_log):
        _append_gripe("oldest")
        _append_gripe("middle")
        _append_gripe("newest")
        h = GripeHandler()
        out = h.read(view="all")
        i_old = out.index("oldest")
        i_mid = out.index("middle")
        i_new = out.index("newest")
        assert i_old < i_mid < i_new

    def test_recent_caps_at_20(self, tmp_gripe_log):
        for i in range(25):
            _append_gripe(f"gripe-{i:02d}")
        h = GripeHandler()
        out = h.read(view="recent")
        # The newest 20 are visible; the 5 oldest should not be.
        for i in range(5, 25):
            assert f"gripe-{i:02d}" in out
        for i in range(5):
            assert f"gripe-{i:02d}" not in out
        # Footer mentions the older entries.
        assert "5 older entries" in out


class TestHandlerWrite:
    def test_bare_append_works(self, tmp_gripe_log):
        h = GripeHandler()
        out = h.put(text="first gripe")
        assert "logged at" in out
        assert "first gripe" in out
        # File was created and contains the entry.
        entries = _read_entries(tmp_gripe_log)
        assert len(entries) == 1
        assert entries[0]["text"] == "first gripe"

    def test_replace_mode_treated_as_append(self, tmp_gripe_log):
        # The server's put() defaults to mode='replace' — for an
        # append-only log we collapse the distinction so a bare
        # ``put(type='gripe', text='…')`` (no explicit mode) doesn't
        # throw MODE_UNSUPPORTED.  Regression for the original P6d bug.
        h = GripeHandler()
        out = h.put(text="hello", mode="replace")
        assert "ERROR" not in out
        assert "logged at" in out
        assert _read_entries(tmp_gripe_log)[0]["text"] == "hello"

    def test_destructive_modes_rejected(self, tmp_gripe_log):
        h = GripeHandler()
        for bad_mode in ("delete", "move"):
            with pytest.raises(PrecisError) as exc:
                h.put(text="x", mode=bad_mode)
            assert exc.value.code == ErrorCode.MODE_UNSUPPORTED

    def test_empty_text_rejected(self, tmp_gripe_log):
        h = GripeHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(text="")
        assert exc.value.code == ErrorCode.PARAM_INVALID
        with pytest.raises(PrecisError):
            h.put(text="   ")  # whitespace-only

    def test_tags_round_trip(self, tmp_gripe_log):
        h = GripeHandler()
        h.put(text="tagged gripe", tags=["ingestion", "urgent"])
        entries = _read_entries(tmp_gripe_log)
        assert entries[0]["tags"] == ["ingestion", "urgent"]

    def test_invalid_tags_type_rejected(self, tmp_gripe_log):
        h = GripeHandler()
        with pytest.raises(PrecisError) as exc:
            h.put(text="x", tags="not-a-list")  # type: ignore[arg-type]
        assert exc.value.code == ErrorCode.PARAM_INVALID


# ---------------------------------------------------------------------------
# Server-level integration
# ---------------------------------------------------------------------------


class TestServerIntegration:
    def test_documented_recipe_works(self, tmp_gripe_log):
        # The exact phrasing the error envelopes have been suggesting
        # for months: ``put(type='gripe', text='…')``.  Before P6d this
        # produced ``ERROR [kind_unknown]: unknown scheme 'gripe'``,
        # which made the docs lie.  This is the regression test.
        out = server.put(
            id="",
            type="gripe",
            text="search said wang2020›38 but get returned 'No blocks in range'",
        )
        assert "ERROR" not in out
        assert "logged at" in out
        assert "[cost: free]" in out

    def test_recent_view_through_server(self, tmp_gripe_log):
        server.put(id="", type="gripe", text="entry one")
        server.put(id="", type="gripe", text="entry two")
        out = server.get(id="gripe:/recent", type="gripe")
        assert "entry one" in out
        assert "entry two" in out
        # Newest first — entry two appears before entry one.
        assert out.index("entry two") < out.index("entry one")

    def test_gripe_kind_appears_in_stats(self):
        # The new kind must surface in the kinds-by-verb listing so
        # agents can discover it via stats().
        out = server.stats()
        assert "gripe" in out

    def test_gripe_kind_visible_to_visible_kinds(self):
        from precis.registry import visible_kinds, _discover

        _discover()
        kinds_get = {k.spec.name for k in visible_kinds("get")}
        kinds_put = {k.spec.name for k in visible_kinds("put")}
        assert "gripe" in kinds_get
        assert "gripe" in kinds_put
