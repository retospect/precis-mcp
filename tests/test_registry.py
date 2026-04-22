"""Tests for the handler registry."""

import pytest

from precis.protocol import Handler, KindSpec, Plugin, PrecisError
from precis.registry import (
    ALIASES,
    FILE_TYPES,
    KINDS,
    PLUGINS,
    SCHEMES,
    RegistryError,
    _synthesise_kind_specs,
    register_file_type,
    register_plugin,
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
        with pytest.raises(PrecisError, match="unknown scheme"):
            resolve("nonexistent", "foo")

    def test_unknown_extension_raises(self):
        with pytest.raises(PrecisError, match="no handler"):
            resolve("file", "test.xyz")

    def test_retired_scheme_aliases_do_not_leak(self):
        # BUG-B regression — the legacy ``[project.entry-points
        # ."precis.schemes"]`` block in ``pyproject.toml`` used to
        # register ``fc`` as a scheme alias for FlashcardHandler.  After
        # the 2026-04-22 rename to the canonical ``flashcard:`` scheme,
        # that entry point must stay removed; otherwise the entry-point
        # loader at ``registry._discover`` re-adds it as an orphan (it
        # sees ``fc`` isn't claimed by the built-in plugin, which now
        # registers ``flashcard``, and back-fills SCHEMES).
        #
        # ``conv`` is also checked as a belt-and-braces guard against
        # anyone re-introducing the abbreviated alias in either the
        # entry points or the plugin registration.
        from precis.registry import SCHEMES, _discover

        _discover()
        assert "fc" not in SCHEMES, (
            "Legacy 'fc' scheme re-appeared in SCHEMES — check "
            "pyproject.toml [project.entry-points.'precis.schemes'] for "
            "an 'fc = …' line that survived the rename."
        )
        assert "conv" not in SCHEMES
        # Positive assertion — the canonical names must be present so
        # a misconfigured pyproject doesn't silently pass this test by
        # dropping both.
        assert "flashcard" in SCHEMES
        assert "conversation" in SCHEMES

    def test_resolve_returns_cached_instance(self):
        """Handlers are memoised per scheme so warm resources (pools, caches,
        parsed indexes) are reused across calls.  See the ``_SCHEME_INSTANCES``
        comment in ``registry.py`` for the rationale."""
        from precis.registry import _reset_instance_cache

        _reset_instance_cache()
        try:
            h1 = resolve("dummy", "a")
            h2 = resolve("dummy", "b")
            assert h1 is h2
        finally:
            _reset_instance_cache()


# ─── Plugin protocol v2: KINDS + ALIASES synthesis ──────────────────


class _FruitHandler(Handler):
    """Fresh-fruit descriptors for the hungry agent."""

    scheme = "fruit"

    def read(self, path, selector, view, subview, query, summarize, depth, page):
        return "banana"


class _FileOnlyHandler(Handler):
    """A file-only handler with no schemes."""

    scheme = "file"

    def read(self, path, selector, view, subview, query, summarize, depth, page):
        return "file content"


class _WebBundleHandler(Handler):
    """Fake 'multiple perplexity kinds' handler."""

    def read(self, path, selector, view, subview, query, summarize, depth, page):
        return "web"


class TestSynthesiseKindSpecs:
    """Default KindSpec synthesis for v1 plugins that don't declare kinds."""

    def test_single_scheme_plugin_gets_one_kindspec(self):
        p = Plugin(name="fruits", handler_cls=_FruitHandler, schemes=["fruit"])
        specs = _synthesise_kind_specs(p)
        assert len(specs) == 1
        assert specs[0].name == "fruit"
        assert specs[0].aliases == []

    def test_multi_scheme_plugin_first_scheme_is_canonical_rest_are_aliases(self):
        p = Plugin(
            name="papers",
            handler_cls=_FruitHandler,  # handler class doesn't matter here
            schemes=["paper", "doi", "arxiv"],
        )
        specs = _synthesise_kind_specs(p)
        assert len(specs) == 1
        assert specs[0].name == "paper"
        assert specs[0].aliases == ["doi", "arxiv"]

    def test_description_comes_from_handler_docstring_first_line(self):
        p = Plugin(name="fruits", handler_cls=_FruitHandler, schemes=["fruit"])
        specs = _synthesise_kind_specs(p)
        # Handler's docstring is "Fresh-fruit descriptors for the hungry agent."
        assert specs[0].description == ("Fresh-fruit descriptors for the hungry agent.")

    def test_description_fallback_when_handler_has_no_docstring(self):
        # A handler class with no docstring at all.
        class _MuteHandler(Handler):
            def read(
                self, path, selector, view, subview, query, summarize, depth, page
            ):
                return ""

        _MuteHandler.__doc__ = None  # ensure really empty
        p = Plugin(name="mute", handler_cls=_MuteHandler, schemes=["mute"])
        specs = _synthesise_kind_specs(p)
        assert specs[0].description == "mute resources"

    def test_file_only_plugin_synthesises_no_kinds(self):
        p = Plugin(
            name="word",
            handler_cls=_FileOnlyHandler,
            schemes=[],
            file_types=[".docx"],
        )
        specs = _synthesise_kind_specs(p)
        assert specs == []


# ─── Plugin registration populates KINDS + ALIASES ──────────────────


class TestRegisterPluginKinds:
    """End-to-end: register_plugin() fills KINDS and ALIASES."""

    def setup_method(self):
        self._stash_kinds: list[str] = []
        self._stash_aliases: list[str] = []
        self._stash_plugins: list[str] = []

    def teardown_method(self):
        for k in self._stash_kinds:
            KINDS.pop(k, None)
        for a in self._stash_aliases:
            ALIASES.pop(a, None)
        for p in self._stash_plugins:
            PLUGINS.pop(p, None)
        SCHEMES.pop("fruit", None)
        SCHEMES.pop("paper2", None)
        SCHEMES.pop("doi2", None)
        SCHEMES.pop("web2", None)
        SCHEMES.pop("think2", None)
        SCHEMES.pop("research2", None)

    def _stash(self, kinds: list[str], aliases: list[str], plugin: str) -> None:
        self._stash_kinds.extend(kinds)
        self._stash_aliases.extend(aliases)
        self._stash_plugins.append(plugin)

    def test_v1_plugin_without_declared_kinds_gets_default_spec(self):
        p = Plugin(
            name="fruits_default",
            handler_cls=_FruitHandler,
            schemes=["fruit"],
            # no kinds=  — synthesis should fill it in
        )
        register_plugin(p)
        self._stash(["fruit"], [], "fruits_default")

        assert "fruit" in KINDS
        assert KINDS["fruit"].plugin_name == "fruits_default"
        assert KINDS["fruit"].handler_cls is _FruitHandler
        assert KINDS["fruit"].spec.name == "fruit"

    def test_v1_plugin_with_alias_schemes_registers_aliases(self):
        p = Plugin(
            name="papers2",
            handler_cls=_FruitHandler,
            schemes=["paper2", "doi2"],
        )
        register_plugin(p)
        self._stash(["paper2"], ["doi2"], "papers2")

        assert "paper2" in KINDS
        assert KINDS["paper2"].spec.aliases == ["doi2"]
        # Alias resolves to the canonical kind name.
        assert ALIASES["doi2"] == "paper2"

    def test_v2_plugin_with_explicit_kinds_registers_each(self):
        p = Plugin(
            name="web_bundle",
            handler_cls=_WebBundleHandler,
            schemes=["web2", "think2", "research2"],
            kinds=[
                KindSpec(
                    name="web2",
                    description="Quick synthesis",
                    cost_hint="~$0.002/call",
                    requires=["PERPLEXITY_API_KEY"],
                ),
                KindSpec(
                    name="think2",
                    description="Multi-step reasoning",
                    cost_hint="~$0.01/call",
                    requires=["PERPLEXITY_API_KEY"],
                ),
                KindSpec(
                    name="research2",
                    description="Deep research",
                    cost_hint="~$0.10+/call",
                    requires=["PERPLEXITY_API_KEY"],
                ),
            ],
        )
        register_plugin(p)
        self._stash(["web2", "think2", "research2"], [], "web_bundle")

        assert "web2" in KINDS
        assert "think2" in KINDS
        assert "research2" in KINDS
        assert KINDS["web2"].spec.cost_hint == "~$0.002/call"
        assert KINDS["think2"].spec.requires == ["PERPLEXITY_API_KEY"]

    def test_kind_collision_is_fatal(self):
        p1 = Plugin(name="fruits_a", handler_cls=_FruitHandler, schemes=["fruit"])
        p2 = Plugin(name="fruits_b", handler_cls=_FruitHandler, schemes=["fruit"])
        register_plugin(p1)
        self._stash(["fruit"], [], "fruits_a")

        # §6.9 / §10.1: two plugins declaring the same kind is a fatal
        # invariant.  The registry raises before the second registration
        # can clobber the first.
        with pytest.raises(RegistryError) as exc:
            register_plugin(p2)
        # Second plugin never made it into the PLUGINS dict.
        assert "fruits_b" not in PLUGINS
        # First registration still holds.
        assert KINDS["fruit"].plugin_name == "fruits_a"
        # Error message names both plugins so the operator can fix config.
        msg = str(exc.value)
        assert "fruits_a" in msg and "fruits_b" in msg

    def test_alias_colliding_with_existing_kind_is_skipped(self, caplog):
        # Register 'fruit' as a kind.
        p1 = Plugin(name="fruits_c", handler_cls=_FruitHandler, schemes=["fruit"])
        register_plugin(p1)
        self._stash(["fruit"], [], "fruits_c")
        # Now register a plugin whose alias list includes 'fruit'.
        p2 = Plugin(
            name="bananas",
            handler_cls=_FruitHandler,
            schemes=["banana", "fruit"],  # 'fruit' as alias would clash with kind
        )
        with caplog.at_level("WARNING"):
            register_plugin(p2)
            self._stash(["banana"], [], "bananas")

        # Alias was skipped, canonical 'banana' still registered.
        assert "banana" in KINDS
        # 'fruit' is still the original kind, not an alias to 'banana'.
        assert KINDS["fruit"].plugin_name == "fruits_c"
        assert ALIASES.get("fruit") is None

    def test_protocol_version_mismatch_refuses_plugin(self, caplog):
        from precis.protocol import PLUGIN_PROTOCOL_VERSION  # noqa: F401

        p = Plugin(
            name="future_plugin",
            handler_cls=_FruitHandler,
            schemes=["futurefruit"],
            protocol_version="99",  # way ahead
        )
        with caplog.at_level("ERROR"):
            register_plugin(p)
        # Plugin was not registered.
        assert "future_plugin" not in PLUGINS
        assert "futurefruit" not in SCHEMES
        assert "futurefruit" not in KINDS
        # Error surfaced.
        assert any("protocol" in rec.message.lower() for rec in caplog.records)
