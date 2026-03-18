"""Tests for the handler registry."""

import pytest

from precis.protocol import Handler, PrecisError
from precis.registry import (
    FILE_TYPES,
    SCHEMES,
    register_file_type,
    register_scheme,
    resolve,
)


# ─── Dummy handlers for testing ─────────────────────────────────────


class DummyHandler(Handler):
    scheme = "dummy"
    writable = False

    def read(self, path, selector, view, subview, query, summarize, depth, page):
        return f"dummy:read:{path}"


class DummyFileHandler(Handler):
    scheme = "file"
    writable = True

    def read(self, path, selector, view, subview, query, summarize, depth, page):
        return f"file:read:{path}"


# ─── Registration ───────────────────────────────────────────────────


class TestRegistration:
    def test_register_scheme(self):
        register_scheme("dummy", DummyHandler)
        assert SCHEMES["dummy"] is DummyHandler

    def test_register_file_type(self):
        register_file_type(".dummy", DummyFileHandler)
        assert FILE_TYPES[".dummy"] is DummyFileHandler

    def teardown_method(self):
        SCHEMES.pop("dummy", None)
        FILE_TYPES.pop(".dummy", None)


# ─── Resolution ─────────────────────────────────────────────────────


class TestResolve:
    def setup_method(self):
        register_scheme("dummy", DummyHandler)
        register_file_type(".dummy", DummyFileHandler)

    def teardown_method(self):
        SCHEMES.pop("dummy", None)
        FILE_TYPES.pop(".dummy", None)

    def test_resolve_scheme(self):
        handler = resolve("dummy", "something")
        assert isinstance(handler, DummyHandler)

    def test_resolve_file_type(self):
        handler = resolve("file", "test.dummy")
        assert isinstance(handler, DummyFileHandler)

    def test_unknown_scheme_raises(self):
        with pytest.raises(PrecisError, match="Unknown scheme"):
            resolve("nonexistent", "foo")

    def test_unknown_extension_raises(self):
        with pytest.raises(PrecisError, match="No handler"):
            resolve("file", "test.xyz")

    def test_resolve_returns_new_instance(self):
        h1 = resolve("dummy", "a")
        h2 = resolve("dummy", "b")
        assert h1 is not h2
