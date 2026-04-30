"""Tests for the Phase 1 visibility API — mask, visible_kinds, startup warnings.

Covers ``set_kinds_mask`` / ``clear_kinds_mask`` / ``get_kinds_mask``,
``visible_kinds(verb)``, ``resolve_alias``, and the ``STARTUP_WARNINGS``
accumulator including the ``requires``-env auto-warning path.
"""

from __future__ import annotations

import pytest

from precis.protocol import VERBS, Handler, KindSpec, Plugin
from precis.registry import (
    ALIASES,
    KINDS,
    PLUGINS,
    SCHEMES,
    STARTUP_WARNINGS,
    add_startup_warning,
    clear_kinds_mask,
    clear_startup_warnings,
    get_kinds_mask,
    register_plugin,
    resolve_alias,
    set_kinds_mask,
    visible_kinds,
)


class _VisHandler(Handler):
    """Minimal handler used only for visibility wiring."""

    def read(self, path, selector, view, subview, query, summarize, depth, page):
        return ""


# ---------------------------------------------------------------------------
# Shared fixture — registers three known kinds and tears them down cleanly
# ---------------------------------------------------------------------------


@pytest.fixture
def three_kinds(monkeypatch):
    """Register three canonical kinds and one alias for the test duration."""
    specs = {
        "alpha": Plugin(
            name="vis-alpha",
            handler_cls=_VisHandler,
            schemes=["alpha"],
            kinds=[KindSpec(name="alpha", description="Alpha resources")],
        ),
        "beta": Plugin(
            name="vis-beta",
            handler_cls=_VisHandler,
            schemes=["beta"],
            kinds=[
                KindSpec(
                    name="beta",
                    description="Beta resources",
                    aliases=["b-legacy"],
                )
            ],
        ),
        "gamma": Plugin(
            name="vis-gamma",
            handler_cls=_VisHandler,
            schemes=["gamma"],
            kinds=[
                KindSpec(
                    name="gamma",
                    description="Gamma resources — needs API key",
                    requires=["VIS_GAMMA_KEY"],
                )
            ],
        ),
    }
    for plugin in specs.values():
        register_plugin(plugin)
    clear_kinds_mask()
    clear_startup_warnings()
    yield specs
    # Teardown: pop everything we added so other tests see a clean registry.
    for name in ("alpha", "beta", "gamma"):
        KINDS.pop(name, None)
        SCHEMES.pop(name, None)
    ALIASES.pop("b-legacy", None)
    for p in ("vis-alpha", "vis-beta", "vis-gamma"):
        PLUGINS.pop(p, None)
    clear_kinds_mask()
    clear_startup_warnings()


# ---------------------------------------------------------------------------
# set_kinds_mask / get_kinds_mask / clear_kinds_mask
# ---------------------------------------------------------------------------


class TestMaskAccessors:
    def test_initially_no_mask(self):
        clear_kinds_mask()
        assert get_kinds_mask() is None

    def test_set_and_get_returns_copy(self):
        set_kinds_mask({"paper": frozenset({"get"})})
        got = get_kinds_mask()
        assert got == {"paper": frozenset({"get"})}
        # Mutating the returned copy must not affect internal state.
        got["fruit"] = frozenset({"get"})
        assert "fruit" not in (get_kinds_mask() or {})
        clear_kinds_mask()

    def test_set_none_is_equivalent_to_clear(self):
        set_kinds_mask({"paper": frozenset({"get"})})
        set_kinds_mask(None)
        assert get_kinds_mask() is None

    def test_clear_resets_state(self):
        set_kinds_mask({"paper": VERBS})
        clear_kinds_mask()
        assert get_kinds_mask() is None


# ---------------------------------------------------------------------------
# visible_kinds — core filtering logic
# ---------------------------------------------------------------------------


class TestVisibleKinds:
    def test_unknown_verb_raises(self, three_kinds):
        with pytest.raises(ValueError, match="unknown verb"):
            visible_kinds("fetch")

    def test_no_mask_and_env_set_returns_all_three(self, three_kinds, monkeypatch):
        monkeypatch.setenv("VIS_GAMMA_KEY", "present")
        names = [k.spec.name for k in visible_kinds("get")]
        # All three of our fixture kinds must be present.  Other built-in
        # kinds may also appear; we only assert the subset.
        assert {"alpha", "beta", "gamma"}.issubset(set(names))

    def test_result_is_sorted_by_kind_name(self, three_kinds, monkeypatch):
        monkeypatch.setenv("VIS_GAMMA_KEY", "present")
        names = [k.spec.name for k in visible_kinds("get")]
        assert names == sorted(names)

    def test_missing_required_env_hides_kind(self, three_kinds, monkeypatch):
        monkeypatch.delenv("VIS_GAMMA_KEY", raising=False)
        names = {k.spec.name for k in visible_kinds("get")}
        assert "alpha" in names
        assert "beta" in names
        assert "gamma" not in names  # env unmet → hidden

    def test_missing_env_produces_one_startup_warning(self, three_kinds, monkeypatch):
        monkeypatch.delenv("VIS_GAMMA_KEY", raising=False)
        visible_kinds("get")
        visible_kinds("get")  # second call should not add a duplicate
        matching = [w for w in STARTUP_WARNINGS if "gamma" in w]
        assert len(matching) == 1
        assert "VIS_GAMMA_KEY" in matching[0]

    def test_mask_whitelists_bare_kind_with_all_verbs(self, three_kinds, monkeypatch):
        # Layered semantics post-mcp-critic-M2:
        #   1. mask: whitelist the kind for all four verbs
        #   2. requires-env: VIS_GAMMA_KEY present (n/a for alpha)
        #   3. **verb-capability filter**: hide for verbs the handler
        #      can't actually do.  ``_VisHandler`` is read-only with
        #      no ``move_nodes`` override, so even an explicit
        #      ``alpha[put,move]`` mask cannot manufacture a write
        #      surface that doesn't exist.  The mask grants
        #      *visibility*; it does not grant *capability*.
        monkeypatch.setenv("VIS_GAMMA_KEY", "present")
        set_kinds_mask({"alpha": VERBS})
        for verb in ("search", "get"):
            names = {k.spec.name for k in visible_kinds(verb)}
            assert names == {"alpha"}, (
                f"{verb}: read verbs always universal for visible kinds"
            )
        for verb in ("put", "move"):
            names = {k.spec.name for k in visible_kinds(verb)}
            assert names == set(), (
                f"{verb}: _VisHandler doesn't implement it; mask alone "
                "cannot manufacture the capability"
            )

    def test_mask_whitelists_verb_subset(self, three_kinds, monkeypatch):
        monkeypatch.setenv("VIS_GAMMA_KEY", "present")
        set_kinds_mask({"alpha": frozenset({"get", "search"})})
        # alpha visible for get and search, hidden for put and move.
        assert {k.spec.name for k in visible_kinds("get")} == {"alpha"}
        assert {k.spec.name for k in visible_kinds("search")} == {"alpha"}
        assert visible_kinds("put") == []
        assert visible_kinds("move") == []

    def test_mask_hides_kinds_not_listed(self, three_kinds, monkeypatch):
        monkeypatch.setenv("VIS_GAMMA_KEY", "present")
        set_kinds_mask({"alpha": VERBS})
        names = {k.spec.name for k in visible_kinds("get")}
        # Only 'alpha' is whitelisted; beta and gamma are hidden despite
        # being registered.
        assert "beta" not in names
        assert "gamma" not in names

    def test_unknown_kind_in_mask_is_silently_ignored(self, three_kinds, monkeypatch):
        monkeypatch.setenv("VIS_GAMMA_KEY", "present")
        # 'fruitbat' is not registered — mask entry is a no-op at this
        # layer; the parser is responsible for warning about it.
        set_kinds_mask({"fruitbat": VERBS, "alpha": VERBS})
        names = {k.spec.name for k in visible_kinds("get")}
        assert names == {"alpha"}

    def test_env_gating_applies_on_top_of_mask(self, three_kinds, monkeypatch):
        monkeypatch.delenv("VIS_GAMMA_KEY", raising=False)
        set_kinds_mask({"gamma": VERBS})
        # Mask says "show gamma", but env is missing → still hidden.
        assert visible_kinds("get") == []

    def test_empty_verb_whitelist_per_spec_hides_kind_for_that_verb(
        self, three_kinds, monkeypatch
    ):
        monkeypatch.setenv("VIS_GAMMA_KEY", "present")
        # A mask entry can legitimately be frozenset() — parser rejects
        # empty brackets, but programmatic callers could construct one.
        # We treat it as "kind visible for no verbs".
        set_kinds_mask({"alpha": frozenset()})
        for verb in ("search", "get", "put", "move"):
            assert visible_kinds(verb) == []


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------


class TestResolveAlias:
    def test_canonical_name_passes_through(self, three_kinds):
        assert resolve_alias("alpha") == "alpha"

    def test_registered_alias_redirects(self, three_kinds):
        # 'b-legacy' is an alias of 'beta' (see fixture).
        assert resolve_alias("b-legacy") == "beta"

    def test_unknown_name_returns_itself(self, three_kinds):
        assert resolve_alias("fruitbat") == "fruitbat"

    def test_alias_target_is_itself_canonical(self, three_kinds):
        target = resolve_alias("b-legacy")
        assert target in KINDS
        assert resolve_alias(target) == target


# ---------------------------------------------------------------------------
# STARTUP_WARNINGS + add_startup_warning
# ---------------------------------------------------------------------------


class TestStartupWarnings:
    def test_add_appends(self):
        clear_startup_warnings()
        add_startup_warning("kind 'alpha' is disabled")
        assert "kind 'alpha' is disabled" in STARTUP_WARNINGS
        clear_startup_warnings()

    def test_duplicate_is_deduped_on_append(self):
        clear_startup_warnings()
        add_startup_warning("dup message")
        add_startup_warning("dup message")
        add_startup_warning("dup message")
        assert STARTUP_WARNINGS.count("dup message") == 1
        clear_startup_warnings()

    def test_empty_message_is_ignored(self):
        clear_startup_warnings()
        add_startup_warning("")
        assert STARTUP_WARNINGS == []

    def test_clear_drops_all(self):
        clear_startup_warnings()
        add_startup_warning("a")
        add_startup_warning("b")
        assert len(STARTUP_WARNINGS) == 2
        clear_startup_warnings()
        assert STARTUP_WARNINGS == []

    def test_order_is_preserved(self):
        clear_startup_warnings()
        add_startup_warning("first")
        add_startup_warning("second")
        add_startup_warning("third")
        assert STARTUP_WARNINGS == ["first", "second", "third"]
        clear_startup_warnings()


# ---------------------------------------------------------------------------
# Env-warning idempotence across visible_kinds calls for different verbs
# ---------------------------------------------------------------------------


class TestEnvWarningIdempotence:
    def test_multiple_verbs_do_not_multiply_the_warning(self, three_kinds, monkeypatch):
        monkeypatch.delenv("VIS_GAMMA_KEY", raising=False)
        for verb in ("search", "get", "put", "move"):
            visible_kinds(verb)
        matching = [w for w in STARTUP_WARNINGS if "gamma" in w]
        assert len(matching) == 1


# ===========================================================================
# Regression suite — 2026-04-25 mcp-critic review (v3 A3)
# ===========================================================================


# ---------------------------------------------------------------------------
# visible_schemes filters env-gated kinds
# ---------------------------------------------------------------------------


class TestVisibleSchemes:
    """``visible_schemes`` exposes only the schemes whose plugin has
    at least one visible kind (env-requires satisfied + mask-allowed).

    The unfiltered ``SCHEMES`` dict used to leak into ``KIND_UNKNOWN``
    error responses, surfacing ``rmk`` (needs ``REMARKABLE_TOKEN``)
    and ``math`` (needs ``WOLFRAM_APP_ID``) as if they were
    invocable.  Review 2026-04-25 mcp-critic finding A3.
    """

    def test_returns_set_of_scheme_strings(self):
        from precis.registry import visible_schemes

        out = visible_schemes()
        assert isinstance(out, set)
        assert all(isinstance(s, str) for s in out)

    def test_paper_scheme_always_visible(self):
        # ``paper`` has no env-requires; must be in every visible
        # build of the package that registers the papers plugin.
        from precis.registry import _discover, visible_schemes

        _discover()
        out = visible_schemes()
        assert "paper" in out

    def test_rmk_hidden_when_no_remarkable_token(self, monkeypatch):
        # Strip the env so the requires gate fails, then ask the
        # registry for visible schemes.
        from precis.registry import _ENV_WARNED, _discover, visible_schemes

        monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
        # _env_satisfied caches its "warned" decision per process —
        # clear so the test's view is fresh.
        _ENV_WARNED.discard("rmk")
        _discover()
        assert "rmk" not in visible_schemes()

    def test_kind_unknown_options_omits_hidden_scheme(self, monkeypatch):
        # Resolve a non-existent scheme and confirm the error envelope
        # lists only visible schemes.  ``rmk`` is hidden when its
        # token is unset; the options enum must reflect that.
        from precis.protocol import PrecisError
        from precis.registry import _ENV_WARNED, _discover, resolve

        monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
        _ENV_WARNED.discard("rmk")
        _discover()
        with pytest.raises(PrecisError) as excinfo:
            resolve("definitely-not-a-scheme", "")
        # Options is the second-positional kwarg; pull from .options.
        opts = excinfo.value.options or []
        assert "rmk" not in opts
        # And the visible schemes do appear (paper is always present).
        assert "paper" in opts
