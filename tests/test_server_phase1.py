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

    def test_identifier_scheme_as_kind_routes_as_scheme(self):
        # ``doi`` is registered as a URI scheme on PaperHandler (alongside
        # ``paper``, ``arxiv``, ``pmid``, etc.) so agents can address a
        # DOI as ``doi:10.x/y``.  The LLM-facing KindSpec.aliases entries
        # were retired in Apr 2026 — ``doi`` is no longer a ``type=``
        # synonym for ``paper``, but it is still a valid scheme.  Confirm
        # the scheme side still works and that no alias slips back in.
        assert "doi" not in ALIASES
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

    def test_fatal_alias_in_env_exits_two(self, capsys, monkeypatch):
        # Every real KindSpec.aliases was retired — inject a synthetic
        # alias so the fatal "alias in config" branch can still be
        # exercised against any future regression that adds one back.
        monkeypatch.setitem(ALIASES, "fakealias", "paper")
        with pytest.raises(SystemExit) as exc:
            server._load_kinds_mask(env={"PRECIS_KINDS": "fakealias"})
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "alias" in captured.err.lower()
        assert "fakealias" in captured.err

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


# ---------------------------------------------------------------------------
# Strict no-type default — ambiguous calls error instead of silently routing
# to the paper corpus.  Guards the §6.3 / §15.2 smoke-test regression.
# ---------------------------------------------------------------------------


class TestAmbiguousKindErrors:
    def test_search_without_type_or_scope_errors(self):
        out = server.search(query="MOF")
        assert "ERROR [kind_unknown]" in out
        assert "cause:" in out
        assert "options:" in out
        # The error must name a kind the caller could re-issue with.
        assert "paper" in out
        # And it must tell them exactly what to do next.
        assert "type=" in out

    def test_search_with_scope_still_works(self, monkeypatch):
        # Scope disambiguates the call — no error expected.  Patch
        # ``tools.read`` so the test doesn't need a real store.
        captured: dict[str, str] = {}

        def fake_read(uri: str, **kwargs):
            captured["uri"] = uri
            return "ok"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.search(query="MOF", scope="wang2020state")
        assert "ERROR [kind_unknown]" not in out
        assert captured["uri"].startswith("paper:")

    def test_search_with_explicit_type_still_works(self, monkeypatch):
        captured: dict[str, str] = {}

        def fake_read(uri: str, **kwargs):
            captured["uri"] = uri
            return "ok"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.search(query="MOF", type="paper")
        assert "ERROR [kind_unknown]" not in out
        assert captured["uri"] == "paper:"

    def test_get_with_only_grep_errors(self):
        out = server.get(grep="MOF")
        assert "ERROR [kind_unknown]" in out
        assert "grep=" in out
        assert "options:" in out

    def test_get_with_bare_slug_errors(self):
        # BUG-C regression — a bare alphanumeric slug with no ``type=``
        # and no scheme prefix used to silently auto-route to ``paper:``.
        # That default was retired for parity with ``search()`` and
        # ``put()``.  Caller must now say ``type='paper'`` or use an
        # explicit prefix (``paper:…``, ``doi:…``, etc.).
        out = server.get(id="wang2020state")
        assert "ERROR [kind_unknown]" in out
        assert "options:" in out
        # The error must name a kind the caller could re-issue with.
        assert "paper" in out
        # And it must tell them exactly what to do next.
        assert "type=" in out or "scheme" in out

    def test_dispatch_unknown_kind_emits_structured_envelope(self):
        # BUG-E regression — the legacy ``_dispatch`` raw-fallback path
        # (taken when the kind isn't in KINDS, e.g. the retired ``conv``
        # alias) used to emit ``!! ERROR PrecisError: …`` which doesn't
        # match the structured ``ERROR [<code>]:`` envelope every other
        # path uses.  Now both paths share the same shape so agents
        # have a single error format to parse.
        out = server.get(id="/recent", type="conv")
        # Must be the structured envelope, not the legacy raw form.
        assert "ERROR [" in out
        assert "!! ERROR PrecisError" not in out
        # The cause line carries the original exception info so the
        # diagnostic content is preserved.
        assert "cause:" in out

    def test_dispatch_precis_error_preserves_options_and_next(self, monkeypatch):
        # A handler raising PrecisError with options= / next= must have
        # those preserved when the raw-fallback path catches them.
        from precis.protocol import ErrorCode as _EC
        from precis.protocol import PrecisError as _PE

        def exploding_read(uri, **kwargs):
            raise _PE(
                _EC.ID_NOT_FOUND,
                cause="nope",
                options=["a", "b"],
                next="try get(id='a')",
            )

        monkeypatch.setattr(server.tools, "read", exploding_read)
        # Force the raw-fallback path by targeting a scheme that
        # registers (so dispatch attempts it) but whose handler raises.
        # ``paper:wang2020state`` with a mocked read that raises reaches
        # the Result-pipeline path; to hit the raw-fallback we need the
        # kind to be missing from KINDS.  Simulate by popping it.
        from precis.registry import KINDS

        saved = KINDS.pop("paper", None)
        try:
            out = server.get(id="paper:wang2020state")
        finally:
            if saved is not None:
                KINDS["paper"] = saved
        assert "ERROR [id_not_found]" in out
        assert "nope" in out
        assert "a, b" in out  # options rendered as comma-separated
        assert "try get(id='a')" in out

    def test_get_with_type_paper_still_works(self, monkeypatch):
        # Explicit ``type='paper'`` disambiguates the bare slug.
        captured: dict[str, str] = {}

        def fake_read(uri: str, **kwargs):
            captured["uri"] = uri
            return "ok"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.get(id="wang2020state", type="paper")
        assert "ERROR [kind_unknown]" not in out
        assert captured["uri"].startswith("paper:")

    def test_get_with_scheme_prefix_still_works(self, monkeypatch):
        # ``paper:wang2020state`` carries its own routing hint — no
        # KIND_UNKNOWN, no ``type=`` required.
        captured: dict[str, str] = {}

        def fake_read(uri: str, **kwargs):
            captured["uri"] = uri
            return "ok"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.get(id="paper:wang2020state")
        assert "ERROR [kind_unknown]" not in out
        assert captured["uri"] == "paper:wang2020state"

    def test_get_with_bare_doi_still_works(self, monkeypatch):
        # Bare DOI classifies confidently — no type= needed.
        captured: dict[str, str] = {}

        def fake_read(uri: str, **kwargs):
            captured["uri"] = uri
            return "ok"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.get(id="10.1021/jacs.2c01234")
        assert "ERROR [kind_unknown]" not in out
        assert captured["uri"].startswith("doi:")

    def test_get_with_file_extension_still_works(self, monkeypatch):
        # File-extension ids classify to the ``file:`` scheme — no
        # type= needed, no KIND_UNKNOWN.
        captured: dict[str, str] = {}

        def fake_read(uri: str, **kwargs):
            captured["uri"] = uri
            return "ok"

        monkeypatch.setattr(server.tools, "read", fake_read)
        out = server.get(id="report.docx")
        assert "ERROR [kind_unknown]" not in out
        assert captured["uri"].startswith("file:")

    def test_put_without_id_or_type_errors(self):
        out = server.put(id="", text="foo", mode="append")
        assert "ERROR [kind_unknown]" in out
        assert "options:" in out
        assert "type=" in out

    def test_put_with_explicit_type_still_works(self, monkeypatch):
        captured: dict[str, str] = {}

        def fake_put(uri: str, **kwargs):
            captured["uri"] = uri
            return "ok"

        monkeypatch.setattr(server.tools, "put", fake_put)
        out = server.put(id="", type="memory", text="x", mode="append")
        assert "ERROR [kind_unknown]" not in out
        assert captured["uri"] == "memory:"

    def test_put_forwards_tags_when_set(self, monkeypatch):
        captured: dict[str, object] = {}

        def fake_put(uri: str, **kwargs):
            captured.update(kwargs)
            captured["uri"] = uri
            return "ok"

        monkeypatch.setattr(server.tools, "put", fake_put)
        server.put(
            id="",
            type="todo",
            text="buy milk",
            mode="append",
            tags=["shopping", "home"],
        )
        assert captured.get("tags") == ["shopping", "home"]

    def test_put_omits_tags_when_not_set(self, monkeypatch):
        # When the caller doesn't pass ``tags=``, the kwarg must not
        # appear in the forwarded call — otherwise handlers that reject
        # unknown kwargs via ``extract_kwargs`` would error on every
        # put() call that didn't happen to mention tags.
        captured: dict[str, object] = {}

        def fake_put(uri: str, **kwargs):
            captured.update(kwargs)
            captured["uri"] = uri
            return "ok"

        monkeypatch.setattr(server.tools, "put", fake_put)
        server.put(id="", type="memory", text="x", mode="append")
        assert "tags" not in captured
