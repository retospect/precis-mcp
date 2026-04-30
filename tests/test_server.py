"""Server-layer tests — ``type=`` dispatch, ``_to_uri`` kind hint, stats(),
MCP tool signature, dispatch handler caching, separator helpers, multi-id
footer dedup, scheme-alias dispatch.

Originally Phase 1 (CHANGELOG); regression classes folded in from the
2026-04-25 mcp-critic review (v1+v2+v3) so server-level concerns live
together.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from precis import server
from precis.registry import (
    ALIASES,
    KINDS,
    _discover,
    _reset_instance_cache,
    clear_kinds_mask,
    clear_startup_warnings,
    resolve,
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
        # Phase 3/4/5c: env-gated kinds emit a one-shot warning when
        # their env is unset — math → WOLFRAM_APP_ID, web/think/
        # research → PERPLEXITY_API_KEY, rmk → REMARKABLE_TOKEN.
        # Stub every env var that any registered kind depends on, then
        # reset the dedup set so any prior warning from an earlier test
        # run is cleared.
        import precis.registry as reg

        monkeypatch.setenv("WOLFRAM_APP_ID", "test-stub")
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-stub")
        monkeypatch.setenv("REMARKABLE_TOKEN", "test-stub")
        reg._ENV_WARNED.clear()
        clear_startup_warnings()
        out = server.stats()
        assert "startup warnings: none" in out

    def test_stats_surfaces_scheme_aliases(self):
        # Phase 5 — without this section, an agent sees ``paper`` in
        # the kinds-by-verb listing and has no way to discover that
        # ``doi:``, ``arxiv:``, ``pmid:``, etc. resolve to the same
        # handler.  The error envelope (``ERROR [kind_unknown]``) only
        # lists canonical kinds, so stats() is the one place the full
        # URI vocabulary becomes visible.
        out = server.stats()
        assert "scheme aliases:" in out
        # Hard-coded check for the papers plugin's known aliases.
        # If the papers plugin schemes change, this assertion needs to
        # update — the test failure tells you exactly what changed.
        assert "paper" in out
        # All 6 alternate schemes registered for the papers plugin
        # must appear in the body.
        for alt in ("doi", "arxiv", "pmid", "pmcid", "issn"):
            assert alt in out, f"expected scheme alias {alt!r} in stats"

    def test_stats_alias_invariant_every_scheme_discoverable(self):
        # Stronger invariant: every URI scheme served by a registered
        # KIND must be discoverable from stats() — either as a canonical
        # kind in ``kinds by verb`` or as an alias in ``scheme aliases``.
        # An undiscoverable scheme is a documentation bug because the
        # agent cannot learn it exists from running the server.
        #
        # We iterate over KINDS (not raw SCHEMES) so this test is
        # immune to pollution from earlier tests that register fake
        # plugins and don't perfectly tear down their scheme entries.
        from precis.registry import KINDS, PLUGINS, _discover

        _discover()
        out = server.stats()
        missing: list[tuple[str, str]] = []
        for kind_name, kind in KINDS.items():
            plugin = PLUGINS.get(kind.plugin_name)
            if plugin is None:
                continue
            for scheme in plugin.schemes:
                if scheme not in out:
                    missing.append((kind_name, scheme))
        assert not missing, (
            f"kind/scheme pair(s) {missing!r} registered but absent "
            "from stats() output — agents cannot discover them"
        )


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
        # Single-line causes ride in the summary line (the dedup
        # branch in ``_format_error`` drops the redundant ``cause:``
        # line — mcp-critic finding M).  The cause TEXT must still
        # surface; the line label is optional.
        assert "type=" in out or "ambiguous" in out
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
        # The cause text is in the summary line — single-line causes
        # are dedup'd from the explicit ``cause:`` line (mcp-critic
        # finding M).  Diagnostic content survives via the summary.
        assert "unknown scheme" in out or "conv" in out

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


# ===========================================================================
# Regression suite — 2026-04-25 mcp-critic review (server-level concerns)
# ===========================================================================


# ---------------------------------------------------------------------------
# v1 fix 1 — MCP put(unlink=...) is reachable
# ---------------------------------------------------------------------------


class TestUnlinkOnMCPSurface:
    """The MCP ``put`` tool must expose every primitive ``tools.put``
    declares; otherwise the advertisement at ``tools/list`` is a lie
    (mcp-critic rule B1)."""

    def test_unlink_param_in_signature(self):
        sig = inspect.signature(server.put)
        assert (
            "unlink" in sig.parameters
        ), "server.put() is missing 'unlink=' — agents cannot remove links via MCP"

    def test_unlink_default_is_empty_string(self):
        # Default must be falsy so the parameter is optional and the
        # legacy zero-arg call shape (``put(id=..., text=..., mode=...)``)
        # keeps working unchanged.
        sig = inspect.signature(server.put)
        assert sig.parameters["unlink"].default == ""

    def test_unlink_dispatches_through_tools_put(self):
        # End-to-end: server.put(unlink=...) reaches tools.put(unlink=...).
        # Patch tools.put to capture the kwargs without hitting the store.
        with patch.object(server.tools, "put", return_value="ok") as mock_put:
            server.put(id="memory:x", unlink="memory:y:cites")
        assert mock_put.called
        kwargs = mock_put.call_args.kwargs
        assert kwargs["unlink"] == "memory:y:cites"

    def test_unlink_present_in_tool_docstring(self):
        # Description must explain the parameter — the MCP tool listing
        # is the agent's only documentation surface (rule B1 / B2).
        assert "unlink" in (server.put.__doc__ or "").lower()


# ---------------------------------------------------------------------------
# v1 fix 2 — _dispatch reuses the cached handler instance
# ---------------------------------------------------------------------------


class TestDispatchUsesCachedHandler:
    """Two calls to the same kind must reach the same handler instance.

    The previous code path constructed ``registered.handler_cls()`` on
    every dispatch, splitting the actual call (cached) from the
    ``hints()`` / ``cost_of()`` hooks (fresh).  With the v5.3 fix,
    ``_dispatch`` resolves through :func:`registry.resolve`, which
    returns the same memoised instance ``tools.read`` / ``tools.put``
    use.
    """

    def setup_method(self) -> None:
        _discover()
        _reset_instance_cache()

    def test_same_handler_instance_across_calls(self):
        # Use ``calc`` because it has no env requirements and no DB —
        # the cached handler is the simplest case.
        if "calc" not in KINDS:
            pytest.skip("calc kind not registered (no sympy installed)")
        h1 = resolve("calc", "")
        h2 = resolve("calc", "")
        assert h1 is h2

    def test_dispatch_resolves_cached_instance(self):
        # Force a cache miss → hit pattern by clearing then dispatching
        # through invoke_handler.  Capture the handler that gets passed
        # to ``invoke_handler`` and assert it equals the cached one.
        if "calc" not in KINDS:
            pytest.skip("calc kind not registered (no sympy installed)")

        seen: list[object] = []

        def _capture(kind, verb, handler, fn, *, args=None):
            seen.append(handler)
            from precis.protocol import Result

            return Result.ok("ok", kind=kind, cost="free")

        with patch.object(server, "invoke_handler", side_effect=_capture):
            server._dispatch("calc", "get", lambda: "ok", args={"id": "1+1"})
            server._dispatch("calc", "get", lambda: "ok", args={"id": "2+2"})

        # Both dispatches saw the same handler instance.
        assert len(seen) == 2
        assert seen[0] is seen[1]
        # And it's the same instance ``resolve`` returns.
        assert seen[0] is resolve("calc", "")


# ---------------------------------------------------------------------------
# v2 C5/E3 — comma-in-parens
# ---------------------------------------------------------------------------


class TestCommaInParensSplit:
    """``server._split_top_level_commas`` keeps commas inside
    ``()``/``[]``/``{}`` together.  Review 2026-04-25 finding C5/E3.
    """

    def test_no_parens_splits_normally(self):
        assert server._split_top_level_commas("a,b,c") == ["a", "b", "c"]

    def test_comma_inside_parens_is_preserved(self):
        assert server._split_top_level_commas("int(1,100)") == ["int(1,100)"]

    def test_mixed_top_and_inner_commas(self):
        # Top-level split lands between ``a(b,c)`` and ``d``; the inner
        # comma is part of the function argument list and survives.
        assert server._split_top_level_commas("a(b,c),d") == ["a(b,c)", "d"]

    def test_nested_parens(self):
        out = server._split_top_level_commas("Matrix([[1,2],[3,4]]),5")
        assert out == ["Matrix([[1,2],[3,4]])", "5"]

    def test_brackets_and_braces_count(self):
        assert server._split_top_level_commas("f({a,b}),g([c,d])") == [
            "f({a,b})",
            "g([c,d])",
        ]

    def test_whitespace_stripped_and_empty_dropped(self):
        assert server._split_top_level_commas(" a , , b ") == ["a", "b"]

    def test_unbalanced_parens_dont_raise(self):
        # Trailing unbalanced paren is treated as content; the URI
        # parser will surface the error downstream with its own
        # structured envelope.  We just don't crash here.
        out = server._split_top_level_commas("a(b,c")
        assert out == ["a(b,c"]


# ---------------------------------------------------------------------------
# v2 D7 — multi-id batch footer dedupe
# ---------------------------------------------------------------------------


class TestMultiIdFooterDedupe:
    """``_strip_inner_cost_footers`` collapses N per-chunk
    ``[cost: …]`` lines down to one trailing footer.  Review
    2026-04-25 finding D7.
    """

    def test_three_free_chunks_emit_one_trailing_footer(self):
        parts = [
            "chunk one body\n\n[cost: free]",
            "chunk two body\n\n[cost: free]",
            "chunk three body\n\n[cost: free]",
        ]
        out = server._strip_inner_cost_footers(parts)
        assert out.count("[cost: free]") == 1
        assert out.endswith("[cost: free]")
        # All three bodies survive
        assert "chunk one body" in out
        assert "chunk two body" in out
        assert "chunk three body" in out

    def test_paid_footer_wins_over_free(self):
        parts = [
            "free chunk\n\n[cost: free]",
            "paid chunk\n\n[cost: ~$0.005/call]",
        ]
        out = server._strip_inner_cost_footers(parts)
        assert "[cost: free]" not in out
        assert out.endswith("[cost: ~$0.005/call]")

    def test_no_inner_footers_passes_through(self):
        parts = ["body one", "body two"]
        out = server._strip_inner_cost_footers(parts)
        # Nothing to merge — separator + bodies, no synthetic footer.
        assert "[cost:" not in out
        assert "body one" in out
        assert "body two" in out

    def test_separator_preserved(self):
        parts = ["a\n\n[cost: free]", "b\n\n[cost: free]"]
        out = server._strip_inner_cost_footers(parts)
        assert "\n---\n" in out


# ---------------------------------------------------------------------------
# v2 D12 — cost-footer parity for scheme aliases
# ---------------------------------------------------------------------------


class TestSchemeAliasCostFooterParity:
    """Non-canonical scheme names of a multi-scheme single-kind plugin
    (``doi``, ``arxiv``, ``pmid``, ``pmcid``, ``isbn``, ``issn`` for
    the paper plugin) route through the same ``KINDS``-keyed
    ``Result``-wrapping path in ``_dispatch`` as the canonical
    ``paper:`` scheme, so every URI-form picks up the
    ``[cost: free]`` footer.  Review 2026-04-25 finding D12.

    Implementation: ``server._kind_from_uri`` resolves a URI scheme
    that's neither a kind nor an alias to the owning plugin's first
    ``KindSpec`` name.  This deliberately does **NOT** also rebind
    ``type='doi'`` — ``type=`` is the agent-facing kind enum and
    keeps its strict canonical-only policy (per Apr 2026 cleanup,
    locked in by ``TestToUriKindHint`` above).
    """

    def setup_method(self):
        _discover()

    def test_kind_from_doi_uri_returns_paper(self):
        assert server._kind_from_uri("doi:10.1021/nn800256d") == "paper"

    def test_kind_from_arxiv_uri_returns_paper(self):
        assert server._kind_from_uri("arxiv:2207.09327") == "paper"

    def test_kind_from_pmid_pmcid_isbn_issn_uris_returns_paper(self):
        # ``isbn:`` is a scheme on *both* the paper plugin (via
        # PaperHandler.schemes) and the book plugin.  The lookup
        # returns whichever plugin's handler matches the SCHEMES
        # mapping, which by registration order is the paper plugin.
        # Books still resolve via their own ``book:`` scheme.
        for scheme, fixture in (
            ("pmid", "pmid:12345678"),
            ("pmcid", "pmcid:PMC1234567"),
            ("issn", "issn:2049-3630"),
        ):
            assert server._kind_from_uri(fixture) == "paper", (
                f"{scheme}: should route to canonical paper kind"
            )

    def test_type_doi_is_still_rejected_as_kind(self):
        # The agent enum is canonical-only.  ``type='doi'`` must NOT
        # silently rewrite to ``type='paper'`` — that's the explicit
        # invariant in TestToUriKindHint above.
        assert "doi" not in ALIASES
        assert "doi" not in KINDS

    def test_canonical_paper_kind_is_not_an_alias(self):
        assert "paper" in KINDS
        assert "paper" not in ALIASES


# ---------------------------------------------------------------------------
# v2 E3 — visually-similar separator rejection
# ---------------------------------------------------------------------------


class TestLookalikeSeparatorRejection:
    """``server._check_lookalike_sep`` catches en-dashes, em-dashes,
    Unicode hyphens, etc. and points the agent at canonical ``~``.
    Review 2026-04-25 finding E3.
    """

    def test_endash_rejected(self):
        out = server._check_lookalike_sep("wu2008first\u201338")
        assert out is not None
        assert "ERROR [id_malformed]" in out
        assert "U+2013" in out  # the offending char is named
        assert "wu2008first~38" in out  # canonical fix in next:
        assert "[cost: free]" in out  # cost-footer parity

    def test_emdash_rejected(self):
        out = server._check_lookalike_sep("wu2008first\u201438")
        assert out is not None
        assert "U+2014" in out
        assert "wu2008first~38" in out

    def test_unicode_hyphen_rejected(self):
        out = server._check_lookalike_sep("wu2008first\u201038")
        assert out is not None
        assert "U+2010" in out

    def test_unicode_minus_rejected(self):
        out = server._check_lookalike_sep("wu2008first\u221238")
        assert out is not None
        assert "U+2212" in out

    def test_ascii_tilde_passes_through(self):
        # Canonical separator is fine.
        assert server._check_lookalike_sep("wu2008first~38") is None

    def test_no_separator_passes_through(self):
        # Bare slug, no separator at all.
        assert server._check_lookalike_sep("wu2008first") is None

    def test_legacy_u203a_still_silently_accepted(self):
        # ``›`` (U+203A) is the v5.x legacy separator and remains
        # accepted on input for back-compat (see
        # ``test_uri.TestSeparatorFlip``).  The lookalike check must
        # NOT flag it.
        assert server._check_lookalike_sep("wu2008first\u203a38") is None


# ---------------------------------------------------------------------------
# v3 D4 — lookalike separator repair collapses double tilde
# ---------------------------------------------------------------------------


class TestLookalikeRepairCollapsesAdjacentSeparators:
    """``_check_lookalike_sep`` substitutes the lookalike with ``~``,
    then collapses any resulting ``~~+`` runs.  Without the collapse
    a tilde-then-en-dash input (``~–5``) produced a double-tilde
    suggestion (``~~5``), which itself is malformed.

    Review 2026-04-25 mcp-critic finding D4 — the suggested fix in
    the error message must itself parse.
    """

    def test_tilde_then_endash_repairs_to_single_tilde(self):
        out = server._check_lookalike_sep("ni2024atomic~\u20135")
        assert out is not None
        # Suggested fix is canonical, not a double tilde.
        assert "ni2024atomic~5" in out
        assert "~~5" not in out

    def test_tilde_then_emdash_repairs_to_single_tilde(self):
        out = server._check_lookalike_sep("ni2024atomic~\u20145")
        assert out is not None
        assert "ni2024atomic~5" in out
        assert "~~5" not in out

    def test_isolated_endash_repairs_to_single_tilde(self):
        # No leading ~ — the lookalike alone replaces with ~.
        out = server._check_lookalike_sep("ni2024atomic\u20135")
        assert out is not None
        assert "ni2024atomic~5" in out
        assert "~~5" not in out


# ---------------------------------------------------------------------------
# v2 cross-fix smoke — scheme alias goes through Result wrapping
# ---------------------------------------------------------------------------


class TestSchemeAliasEndToEnd:
    """Belt-and-braces: ``get(id='doi:10.x/y')`` reaches
    ``_dispatch('paper', …)`` (via the auto-alias) and renders
    through ``Result.render()``, which means the cost footer is
    present.  Review 2026-04-25 finding D12.
    """

    def setup_method(self):
        _discover()

    def test_doi_get_dispatches_to_paper_kind(self):
        # Capture the kind that ``_dispatch`` is called with.
        seen: list[str] = []

        def fake_dispatch(kind, verb, call, args=None):
            seen.append(kind)
            # Mimic the success path of invoke_handler so the test
            # doesn't need a live store.
            return "OK\n\n[cost: free]"

        with patch.object(server, "_dispatch", side_effect=fake_dispatch):
            out = server.get(id="doi:10.1021/nn800256d")
        assert seen == ["paper"]
        assert "[cost: free]" in out

    def test_arxiv_get_dispatches_to_paper_kind(self):
        seen: list[str] = []

        def fake_dispatch(kind, verb, call, args=None):
            seen.append(kind)
            return "OK\n\n[cost: free]"

        with patch.object(server, "_dispatch", side_effect=fake_dispatch):
            server.get(id="arxiv:2207.09327")
        assert seen == ["paper"]
