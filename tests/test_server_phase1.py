"""Phase 1 server-layer tests — type= dispatch, _to_uri kind hint, stats()."""

from __future__ import annotations

import pytest

from precis import server
from precis.registry import (
    ALIASES,
    clear_kinds_mask,
    clear_startup_warnings,
    set_kinds_mask,
)

# ---------------------------------------------------------------------------
# _to_uri — kind-hint path
# ---------------------------------------------------------------------------


class TestToUriKindHint:
    def test_kind_hint_stamps_scheme(self):
        assert server._to_uri("wang2020state", kind="paper") == "paper:wang2020state"

    def test_kind_hint_with_empty_id_returns_bare_scheme(self):
        assert server._to_uri("", kind="paper") == "paper:"

    def test_alias_kind_that_is_also_a_scheme_preserves_scheme(self):
        # Phase 5: 'doi' is both an alias of the 'paper' kind AND a
        # registered scheme on PaperHandler (so the handler can dispatch
        # on identifier type). When both apply, the scheme name wins at
        # URI level; the alias still routes to the canonical kind for
        # enum/masking purposes.
        assert ALIASES.get("doi") == "paper"
        assert "doi" in __import__("precis.registry", fromlist=["SCHEMES"]).SCHEMES
        assert server._to_uri("10.1021/x", kind="doi") == "doi:10.1021/x"

    def test_kind_hint_preserves_explicit_scheme_prefix(self):
        # If the id already carries a scheme prefix, the kind hint is a
        # suggestion; the existing prefix wins.
        out = server._to_uri("arxiv:2301.12345", kind="paper")
        assert out == "arxiv:2301.12345"

    def test_unknown_kind_hint_passes_through_as_scheme(self):
        # resolve_alias returns the name unchanged for unknown kinds.
        # Since the id has no scheme, we prepend the raw name.  The
        # downstream handler resolution is what ultimately errors.
        assert server._to_uri("foo", kind="fruitbat") == "fruitbat:foo"


# ---------------------------------------------------------------------------
# _to_uri — legacy (no kind) path still works
# ---------------------------------------------------------------------------


class TestToUriLegacy:
    def test_empty_id_defaults_to_paper(self):
        assert server._to_uri("") == "paper:"

    def test_bare_slug_defaults_to_paper(self):
        assert server._to_uri("wang2020state") == "paper:wang2020state"

    def test_file_extension_routes_to_file_scheme(self):
        assert server._to_uri("report.docx") == "file:report.docx"

    def test_bare_doi_pattern_routes_to_doi_scheme(self):
        assert server._to_uri("10.1021/jacs.2c01234") == "doi:10.1021/jacs.2c01234"

    def test_known_scheme_prefix_preserved(self):
        assert server._to_uri("doi:10.1021/x") == "doi:10.1021/x"


# ---------------------------------------------------------------------------
# _load_kinds_mask — PRECIS_KINDS startup loader
# ---------------------------------------------------------------------------


class TestLoadKindsMask:
    def teardown_method(self):
        clear_kinds_mask()
        clear_startup_warnings()

    def test_unset_env_leaves_mask_none(self):
        server._load_kinds_mask(env={})
        from precis.registry import get_kinds_mask

        assert get_kinds_mask() is None

    def test_valid_env_installs_mask(self):
        server._load_kinds_mask(env={"PRECIS_KINDS": "paper"})
        from precis.registry import get_kinds_mask

        got = get_kinds_mask()
        assert got is not None
        assert "paper" in got

    def test_fatal_alias_in_env_exits_two(self, capsys):
        # 'doi' is an alias of 'paper'; putting it in config is fatal.
        with pytest.raises(SystemExit) as exc:
            server._load_kinds_mask(env={"PRECIS_KINDS": "doi"})
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "alias" in captured.err.lower()
        assert "doi" in captured.err

    def test_fatal_unknown_verb_exits_two(self, capsys):
        with pytest.raises(SystemExit) as exc:
            server._load_kinds_mask(env={"PRECIS_KINDS": "paper[fetch]"})
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "verb" in captured.err.lower()

    def test_fatal_empty_brackets_exits_two(self, capsys):
        with pytest.raises(SystemExit) as exc:
            server._load_kinds_mask(env={"PRECIS_KINDS": "paper[]"})
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "empty" in captured.err.lower()

    def test_unknown_kind_is_non_fatal_warning(self):
        # 'fruitbat' isn't registered — should produce a warning, not exit.
        server._load_kinds_mask(env={"PRECIS_KINDS": "paper,fruitbat"})
        from precis.registry import STARTUP_WARNINGS, get_kinds_mask

        mask = get_kinds_mask()
        assert mask is not None
        # paper kept, fruitbat dropped.
        assert "paper" in mask
        assert "fruitbat" not in mask
        assert any("fruitbat" in w for w in STARTUP_WARNINGS)


# ---------------------------------------------------------------------------
# stats() tool
# ---------------------------------------------------------------------------


class TestStatsTool:
    def teardown_method(self):
        clear_kinds_mask()
        clear_startup_warnings()

    def test_stats_reports_unmasked_state(self):
        clear_kinds_mask()
        out = server.stats()
        assert "service: precis-mcp" in out
        assert "mask: unset" in out
        assert "kinds by verb:" in out
        # Every verb line is present.
        for verb in ("search", "get", "put", "move"):
            assert f"{verb:<6}" in out

    def test_stats_reports_mask_set(self):
        set_kinds_mask({"paper": frozenset({"get"})})
        out = server.stats()
        assert "mask: PRECIS_KINDS set" in out
        # Paper appears in the get line but not in put / move / search.
        lines = out.splitlines()
        get_line = next(line for line in lines if line.lstrip().startswith("get"))
        put_line = next(line for line in lines if line.lstrip().startswith("put"))
        assert "paper" in get_line
        assert "(none)" in put_line or "paper" not in put_line

    def test_stats_surfaces_startup_warnings(self):
        from precis.registry import add_startup_warning

        clear_startup_warnings()
        add_startup_warning("test warning: something is up")
        out = server.stats()
        assert "startup warnings:" in out
        assert "test warning" in out

    def test_stats_shows_no_warnings_when_empty(self, monkeypatch):
        # Phase 3/4: env-gated kinds (math → WOLFRAM_APP_ID, web/think/
        # research → PERPLEXITY_API_KEY) emit a one-shot warning when
        # their env is unset.  Stub every env var that any registered
        # kind depends on, then reset the dedup set so any prior
        # warning from an earlier test run is cleared.
        import precis.registry as reg

        monkeypatch.setenv("WOLFRAM_APP_ID", "test-stub")
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-stub")
        reg._ENV_WARNED.clear()
        clear_startup_warnings()
        out = server.stats()
        assert "startup warnings: none" in out


# ---------------------------------------------------------------------------
# Tool signatures — the `type=` kwarg is actually present on each tool
# ---------------------------------------------------------------------------


class TestToolSignatures:
    def test_search_accepts_type_kwarg(self):
        import inspect

        sig = inspect.signature(server.search)
        assert "type" in sig.parameters
        assert sig.parameters["type"].default == ""

    def test_get_accepts_type_kwarg(self):
        import inspect

        sig = inspect.signature(server.get)
        assert "type" in sig.parameters

    def test_put_accepts_type_kwarg(self):
        import inspect

        sig = inspect.signature(server.put)
        assert "type" in sig.parameters

    def test_move_accepts_type_kwarg(self):
        import inspect

        sig = inspect.signature(server.move)
        assert "type" in sig.parameters
