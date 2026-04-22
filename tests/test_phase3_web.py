"""Phase 3 — Perplexity Sonar handlers (web, think, research).

No network — every test mocks either the whole ``_call_sonar`` path
(when we want to exercise the read() + formatter pipeline) or
``httpx.post`` (when we want to cover the HTTP layer itself).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from precis import server
from precis.handlers.web import (
    ResearchHandler,
    ThinkHandler,
    WebHandler,
    _attribution,
    _format_response,
    _WebBase,
)
from precis.protocol import ErrorCode, PrecisError
from precis.registry import (
    KINDS,
    SCHEMES,
    visible_kinds,
)

# ---------------------------------------------------------------------------
# Mode subclasses — class-attribute sanity
# ---------------------------------------------------------------------------


class TestModeAttributes:
    def test_web_uses_sonar_model(self):
        assert WebHandler._MODEL == "sonar"
        assert WebHandler.scheme == "web"

    def test_think_uses_reasoning_pro(self):
        assert ThinkHandler._MODEL == "sonar-reasoning-pro"
        assert ThinkHandler.scheme == "think"

    def test_research_uses_deep_research(self):
        assert ResearchHandler._MODEL == "sonar-deep-research"
        assert ResearchHandler.scheme == "research"

    def test_timeouts_ascend_by_mode(self):
        # Deeper analysis → longer allowed wait.
        assert WebHandler._TIMEOUT < ThinkHandler._TIMEOUT
        assert ThinkHandler._TIMEOUT < ResearchHandler._TIMEOUT

    def test_all_three_are_read_only(self):
        for cls in (WebHandler, ThinkHandler, ResearchHandler):
            assert cls.writable is False
            assert cls.views == set()


# ---------------------------------------------------------------------------
# Attribution footer — ToS requirements
# ---------------------------------------------------------------------------


class TestPerplexityAttribution:
    """Perplexity's ToS prohibits silent embedding and mandates
    disclosure of AI use in any public output.  These tests lock in
    the required footer shape.
    """

    def test_attribution_names_perplexity(self):
        out = _attribution("sonar")
        assert "Perplexity AI" in out
        assert "perplexity.ai" in out

    def test_attribution_names_model(self):
        assert "sonar" in _attribution("sonar")
        assert "sonar-reasoning-pro" in _attribution("sonar-reasoning-pro")
        assert "sonar-deep-research" in _attribution("sonar-deep-research")

    def test_attribution_links_to_tos(self):
        out = _attribution("sonar")
        assert "terms-of-service" in out.lower()

    def test_attribution_warns_not_primary_source(self):
        out = _attribution("sonar")
        assert "not a primary source" in out.lower()

    def test_attribution_tells_user_to_verify_citations(self):
        out = _attribution("sonar")
        assert "verify" in out.lower()

    def test_attribution_discloses_commercial_restriction(self):
        out = _attribution("sonar")
        # ToS restricts Standard/Pro to non-commercial use.
        assert "non-commercial" in out.lower() or "commercial" in out.lower()

    def test_attribution_on_successful_response(self):
        data = {
            "choices": [{"message": {"content": "Anthropic is an AI company."}}],
            "citations": ["https://anthropic.com/"],
        }
        out = _format_response(data, "sonar")
        assert "Perplexity AI" in out

    def test_attribution_on_empty_choices(self):
        out = _format_response({"choices": []}, "sonar")
        assert "Perplexity AI" in out

    def test_attribution_on_empty_content(self):
        data = {"choices": [{"message": {"content": ""}}]}
        out = _format_response(data, "sonar-reasoning-pro")
        assert "Perplexity AI" in out
        # Footer mentions the specific model.
        assert "sonar-reasoning-pro" in out


# ---------------------------------------------------------------------------
# _format_response — shape + citation passthrough
# ---------------------------------------------------------------------------


class TestFormatResponse:
    def test_content_surfaced_verbatim(self):
        data = {
            "choices": [{"message": {"content": "The answer is 42."}}],
            "citations": [],
        }
        out = _format_response(data, "sonar")
        assert "The answer is 42." in out

    def test_citations_numbered_and_listed(self):
        data = {
            "choices": [{"message": {"content": "Per [1] and [2]."}}],
            "citations": [
                "https://arxiv.org/abs/2401.12345",
                "https://nature.com/articles/s41586-024-00000-0",
            ],
        }
        out = _format_response(data, "sonar")
        # Inline citation markers from the content are preserved.
        assert "[1]" in out
        assert "[2]" in out
        # Sources list renders each URL with its index.
        assert "Sources:" in out
        assert "[1] https://arxiv.org/abs/2401.12345" in out
        assert "[2] https://nature.com/articles/s41586-024-00000-0" in out

    def test_no_sources_section_when_no_citations(self):
        data = {"choices": [{"message": {"content": "hi"}}], "citations": []}
        out = _format_response(data, "sonar")
        assert "Sources:" not in out

    def test_strips_surrounding_whitespace(self):
        data = {
            "choices": [{"message": {"content": "\n\n answer \n\n"}}],
            "citations": [],
        }
        out = _format_response(data, "sonar")
        # Leading/trailing whitespace on content stripped, but footer follows.
        assert out.split("\n---")[0].strip() == "answer"

    def test_null_fields_tolerated(self):
        # Perplexity has been known to send null citations; formatter
        # must not crash.
        data = {"choices": [{"message": {"content": "x"}}], "citations": None}
        out = _format_response(data, "sonar")
        assert "x" in out

    def test_empty_message_renders_placeholder(self):
        data = {"choices": [{"message": {"content": ""}}]}
        out = _format_response(data, "sonar")
        assert "empty answer" in out.lower()


# ---------------------------------------------------------------------------
# HTTP layer — httpx errors map to structured PrecisError codes
# ---------------------------------------------------------------------------


def _mock_sonar_response(content: str = "ok", citations=None) -> MagicMock:
    """Build a httpx-style successful response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "citations": citations or [],
    }
    return resp


class TestCallSonar:
    def test_missing_env_raises_kind_unavailable(self, monkeypatch):
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        h = WebHandler()
        with pytest.raises(PrecisError) as exc:
            h._call_sonar("hi")
        assert exc.value.code == ErrorCode.KIND_UNAVAILABLE

    def test_sends_correct_model_and_query(self, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "stub")
        mock_resp = _mock_sonar_response()
        with patch("httpx.post", return_value=mock_resp) as post:
            h = ThinkHandler()
            h._call_sonar("what is X")

        args, kwargs = post.call_args
        assert kwargs["json"]["model"] == "sonar-reasoning-pro"
        assert kwargs["json"]["messages"][0]["content"] == "what is X"
        assert kwargs["json"]["return_citations"] is True
        assert kwargs["timeout"] == ThinkHandler._TIMEOUT

    def test_auth_header_carries_bearer_token(self, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key-abc")
        mock_resp = _mock_sonar_response()
        with patch("httpx.post", return_value=mock_resp) as post:
            WebHandler()._call_sonar("q")
        assert (
            post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key-abc"
        )

    def test_http_401_raises_denied(self, monkeypatch):
        import httpx

        monkeypatch.setenv("PERPLEXITY_API_KEY", "bad-key")
        mock_resp = MagicMock(status_code=401, text="unauthorized")
        err = httpx.HTTPStatusError("401", request=MagicMock(), response=mock_resp)
        mock_resp.raise_for_status = MagicMock(side_effect=err)
        with patch("httpx.post", return_value=mock_resp):
            with pytest.raises(PrecisError) as exc:
                WebHandler()._call_sonar("q")
        assert exc.value.code == ErrorCode.DENIED

    def test_http_429_raises_rate_limited(self, monkeypatch):
        import httpx

        monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
        mock_resp = MagicMock(status_code=429, text="too many requests")
        err = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_resp)
        mock_resp.raise_for_status = MagicMock(side_effect=err)
        with patch("httpx.post", return_value=mock_resp):
            with pytest.raises(PrecisError) as exc:
                WebHandler()._call_sonar("q")
        assert exc.value.code == ErrorCode.RATE_LIMITED

    def test_http_500_raises_upstream_error(self, monkeypatch):
        import httpx

        monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
        mock_resp = MagicMock(status_code=500, text="boom")
        err = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_resp)
        mock_resp.raise_for_status = MagicMock(side_effect=err)
        with patch("httpx.post", return_value=mock_resp):
            with pytest.raises(PrecisError) as exc:
                WebHandler()._call_sonar("q")
        assert exc.value.code == ErrorCode.UPSTREAM_ERROR

    def test_timeout_raises_timeout(self, monkeypatch):
        import httpx

        monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
        with patch("httpx.post", side_effect=httpx.TimeoutException("slow")):
            with pytest.raises(PrecisError) as exc:
                WebHandler()._call_sonar("q")
        assert exc.value.code == ErrorCode.TIMEOUT

    def test_generic_transport_error_raises_upstream(self, monkeypatch):
        import httpx

        monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
        with patch("httpx.post", side_effect=httpx.ConnectError("no net")):
            with pytest.raises(PrecisError) as exc:
                WebHandler()._call_sonar("q")
        assert exc.value.code == ErrorCode.UPSTREAM_ERROR


# ---------------------------------------------------------------------------
# read() end-to-end — handler → _call_sonar → _format_response
# ---------------------------------------------------------------------------


class TestRead:
    def _stub_call(self, content="ok", citations=None):
        return {
            "choices": [{"message": {"content": content}}],
            "citations": citations or [],
        }

    @pytest.fixture
    def read_kwargs(self):
        """Minimal valid kwargs bundle for Handler.read."""
        return dict(
            selector=None,
            view=None,
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )

    def test_empty_path_returns_landing(self, read_kwargs):
        h = WebHandler()
        out = h.read(path="", **read_kwargs)
        # Landing shape: scheme header, usage block, model name, no API call.
        assert "web:" in out
        assert "Usage:" in out
        assert "sonar" in out
        assert "<question>" in out

    def test_whitespace_only_path_returns_landing(self, read_kwargs):
        h = WebHandler()
        out = h.read(path="   \n  ", **read_kwargs)
        assert "Usage:" in out
        assert "web:" in out

    def test_landing_does_not_call_sonar(self, read_kwargs, monkeypatch):
        # Guard: bare-landing must be free — no HTTP call, no key lookup.
        h = WebHandler()
        called: list[str] = []
        monkeypatch.setattr(
            h, "_call_sonar", lambda q: called.append(q) or {"choices": []}
        )
        h.read(path="", **read_kwargs)
        assert called == []

    def test_think_landing_mentions_its_model(self, read_kwargs):
        out = ThinkHandler().read(path="", **read_kwargs)
        assert "think:" in out
        assert "sonar-reasoning-pro" in out

    def test_research_landing_mentions_its_model(self, read_kwargs):
        out = ResearchHandler().read(path="", **read_kwargs)
        assert "research:" in out
        assert "sonar-deep-research" in out

    def test_read_absorbs_top_k_kwarg(self, read_kwargs):
        # BUG-I regression — the server-side ``search()`` dispatcher
        # routes Perplexity-backed kinds through ``tools.read`` which
        # forwards ``top_k`` as kwargs.  ``_WebBase.read`` must accept
        # (and ignore) the argument rather than raising TypeError.
        # Guard the landing path so no HTTP call is made.
        h = WebHandler()
        out = h.read(path="", top_k=5, **read_kwargs)
        assert "Usage:" in out  # landing view, not an error

    def test_read_absorbs_arbitrary_unknown_kwargs(self, read_kwargs):
        # Defensive: future dispatcher kwargs shouldn't break _WebBase
        # either.  Covers research + think alongside web to confirm
        # the absorption is in the shared base.
        for cls in (WebHandler, ThinkHandler, ResearchHandler):
            out = cls().read(
                path="", top_k=10, some_future_kwarg="x", **read_kwargs
            )
            assert "Usage:" in out

    def test_landing_cost_of_is_free(self):
        # cost_of must report free for a bare call so agents don't
        # think the landing cost them anything.
        from precis.protocol import CallContext

        h = WebHandler()
        for args in (
            {"id": "web:"},
            {"id": ""},
            {"id": "web:", "grep": "", "query": ""},
            {"id": "web:   "},
        ):
            ctx = CallContext(kind="web", verb="get", args=args)
            assert h.cost_of(ctx) == "free", args

    def test_cost_of_not_free_when_question_present(self):
        from precis.protocol import CallContext

        h = WebHandler()
        ctx = CallContext(kind="web", verb="get", args={"id": "web:hello"})
        assert h.cost_of(ctx) is None

    def test_query_fallback_when_path_empty(self, read_kwargs, monkeypatch):
        # When id='' and query='X', handler treats query as the question.
        h = WebHandler()
        monkeypatch.setattr(h, "_call_sonar", lambda q: self._stub_call(f"echo: {q}"))
        read_kwargs["query"] = "fallback question"
        out = h.read(path="", **read_kwargs)
        assert "echo: fallback question" in out

    def test_successful_call_includes_content_and_attribution(
        self, read_kwargs, monkeypatch
    ):
        h = WebHandler()
        monkeypatch.setattr(
            h,
            "_call_sonar",
            lambda q: self._stub_call(
                "42 is the answer.", citations=["https://example.com/"]
            ),
        )
        out = h.read(path="the question", **read_kwargs)
        assert "42 is the answer." in out
        assert "Sources:" in out
        assert "https://example.com/" in out
        assert "Perplexity AI" in out

    def test_think_handler_uses_right_model(self, read_kwargs, monkeypatch):
        h = ThinkHandler()
        received = {}

        def fake_call(q):
            received["query"] = q
            received["model"] = h._MODEL
            return self._stub_call("reasoned answer")

        monkeypatch.setattr(h, "_call_sonar", fake_call)
        out = h.read(path="complex question", **read_kwargs)
        assert received["model"] == "sonar-reasoning-pro"
        assert "sonar-reasoning-pro" in out  # attribution names model

    def test_research_handler_uses_right_model(self, read_kwargs, monkeypatch):
        h = ResearchHandler()
        received = {}
        monkeypatch.setattr(
            h,
            "_call_sonar",
            lambda q: (received.update({"model": h._MODEL}), self._stub_call("r"))[1],
        )
        out = h.read(path="deep question", **read_kwargs)
        assert received["model"] == "sonar-deep-research"
        assert "sonar-deep-research" in out


# ---------------------------------------------------------------------------
# Registry / visibility — env gating
# ---------------------------------------------------------------------------


class TestRegistration:
    @classmethod
    def setup_class(cls):
        import precis.registry as reg

        reg._discover()

    def test_all_three_kinds_registered(self):
        # httpx is in [external], which is installed in the test env.
        assert "web" in KINDS
        assert "think" in KINDS
        assert "research" in KINDS
        assert "web" in SCHEMES
        assert "think" in SCHEMES
        assert "research" in SCHEMES

    def test_all_three_hidden_without_api_key(self, monkeypatch):
        import precis.registry as reg

        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        for kind in ("web", "think", "research"):
            reg._ENV_WARNED.discard(kind)
        names = {k.spec.name for k in visible_kinds("get")}
        assert "web" not in names
        assert "think" not in names
        assert "research" not in names

    def test_all_three_visible_with_api_key(self, monkeypatch):
        monkeypatch.setenv("PERPLEXITY_API_KEY", "stub-for-visibility")
        names = {k.spec.name for k in visible_kinds("get")}
        assert "web" in names
        assert "think" in names
        assert "research" in names

    def test_cost_hints_ordered_by_depth(self):
        # web < think < research cost — lock in the relative ordering
        # via the cost_hint strings.  The agent reads these to choose
        # the cheapest mode that answers the question.
        web_spec = KINDS["web"].spec
        think_spec = KINDS["think"].spec
        research_spec = KINDS["research"].spec
        assert "0.001" in web_spec.cost_hint
        assert "0.005" in think_spec.cost_hint
        assert "0.50" in research_spec.cost_hint

    def test_each_kind_declares_perplexity_env(self):
        for kind in ("web", "think", "research"):
            assert "PERPLEXITY_API_KEY" in KINDS[kind].spec.requires


# ---------------------------------------------------------------------------
# Server URI dispatch
# ---------------------------------------------------------------------------


class TestServerDispatch:
    def test_type_web_builds_web_uri(self):
        assert server._to_uri("what is X", kind="web") == "web:what is X"

    def test_type_think_builds_think_uri(self):
        assert server._to_uri("analyse Y", kind="think") == "think:analyse Y"

    def test_type_research_builds_research_uri(self):
        assert (
            server._to_uri("investigate Z", kind="research") == "research:investigate Z"
        )

    def test_explicit_scheme_prefix_preserved(self):
        assert server._to_uri("web:hello") == "web:hello"
        assert server._to_uri("think:hello") == "think:hello"
        assert server._to_uri("research:hello") == "research:hello"


# ---------------------------------------------------------------------------
# Base-class contract — subclasses MUST set _MODEL
# ---------------------------------------------------------------------------


class TestBaseContract:
    def test_base_has_empty_model_placeholder(self):
        # _WebBase itself is abstract-ish; _MODEL defaults to empty
        # string so accidentally registering _WebBase directly fails
        # loudly rather than silently querying a non-existent model.
        assert _WebBase._MODEL == ""
