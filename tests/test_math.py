"""Tests for `MathHandler` — Wolfram Alpha cache-backed kind.

The HTTP call is mocked at the `_run_query` seam so tests don't talk to
the real Wolfram API. Cache flow is delegated to `CacheBackedHandler`
(covered by `test_cache_base.py`); these tests focus on:

- canonicalization of math queries
- pod-flattening formatter (success / failure / timeout / no-text)
- attribution footer with deep-link + accessed-date
- ``WOLFRAM_APP_ID`` env gating
- KindSpec wiring
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import Upstream
from precis.handlers.math import MathHandler, _format_doc
from precis.store import Store

# ── synthetic Wolfram document fixtures ──────────────────────────────


class _Pod:
    """Mimics what xmltodict + Document.make produces for a single pod."""

    def __init__(self, title: str, plaintexts: list[str]) -> None:
        self._d = {
            "@title": title,
            "subpod": [{"plaintext": t} for t in plaintexts],
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)


class _Doc:
    """Mimics the parsed ``<queryresult>`` Document object."""

    def __init__(
        self,
        *,
        success: Any = True,
        timedout: Any = None,
        pods: list[_Pod] | None = None,
        didyoumeans: Any = None,
    ) -> None:
        # ``wolframalpha.Document`` decodes ``@success`` as a real bool
        # via ``xml_bool``, so production code sees True/False rather
        # than 'true'/'false'. Fixtures match that contract.
        self.success = success
        self.timedout = timedout
        self.pods = pods or []
        self.didyoumeans = didyoumeans


# ── handler fixture with patched _run_query ──────────────────────────


@pytest.fixture
def handler(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> MathHandler:
    """MathHandler with a mocked HTTP layer + WOLFRAM_APP_ID set."""
    monkeypatch.setenv("WOLFRAM_APP_ID", "TEST-APP-ID")
    return MathHandler(hub=hub)


def _patch_run_query(monkeypatch: pytest.MonkeyPatch, doc: Any) -> list[str]:
    """Replace `_run_query` with a stub that records calls. Returns the
    list of expressions the mocked function was asked for."""
    calls: list[str] = []

    def _stub(app_id: str, expression: str) -> Any:
        calls.append(expression)
        return doc

    monkeypatch.setattr("precis.handlers.math._run_query", _stub)
    return calls


# ── basic flow ────────────────────────────────────────────────────────


def test_successful_query_renders_pods(
    handler: MathHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _Doc(
        pods=[
            _Pod("Result", ["≈ 5.1 million people"]),
            _Pod("Source information", ["United Nations 2024 estimates"]),
        ]
    )
    _patch_run_query(monkeypatch, doc)

    resp = handler.get(q="population of Ireland")

    assert "## Result" in resp.body
    assert "5.1 million people" in resp.body
    assert "## Source information" in resp.body
    assert "Computed by Wolfram|Alpha" in resp.body
    # Per-query deep-link with the user's actual query (URL-encoded)
    assert "wolframalpha.com/input?i=population+of+ireland" in resp.body
    # Cost trailer
    assert "[cost: ~$0.0020]" in (resp.cost or "")


def test_second_call_hits_cache(
    handler: MathHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _Doc(pods=[_Pod("Result", ["42"])])
    calls = _patch_run_query(monkeypatch, doc)

    handler.get(q="meaning of life")
    resp2 = handler.get(q="meaning of life")

    assert calls == ["meaning of life"]  # only one upstream call
    assert "cached" in (resp2.cost or "")


def test_canonicalization_collapses_case_and_whitespace(
    handler: MathHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _Doc(pods=[_Pod("Result", ["x"])])
    calls = _patch_run_query(monkeypatch, doc)

    handler.get(q="Population of Ireland")
    handler.get(q="  population  of  IRELAND  ")
    handler.get(q="population of ireland")

    assert calls == ["population of ireland"]


# ── formatter cases ──────────────────────────────────────────────────


def test_format_success_with_single_subpod_dict() -> None:
    """xmltodict collapses single-child to dict (not list)."""

    class PodDict:
        def get(self, k, default=None):
            return {
                "@title": "Result",
                "subpod": {"plaintext": "single"},  # dict, not list
            }.get(k, default)

    doc = _Doc(pods=[PodDict()])
    out = _format_doc(doc, "x")
    assert "## Result" in out
    assert "single" in out


def test_format_success_no_displayable_text() -> None:
    """Pods with no plaintext (e.g. images-only) → friendly fallback."""
    doc = _Doc(pods=[_Pod("Result", [])])
    out = _format_doc(doc, "weird query")
    assert "no displayable text" in out
    assert "weird query" in out


def test_format_failure_with_did_you_mean() -> None:
    doc = _Doc(
        success=False,
        didyoumeans=[
            {"#text": "population of Ireland"},
            {"#text": "people in Ireland"},
        ],
    )
    out = _format_doc(doc, "popultion ireland")
    assert "Query failed" in out
    assert "Did you mean: population of Ireland, people in Ireland" in out


def test_format_failure_didyoumeans_dict_form() -> None:
    """``didyoumeans`` collapses to a dict when there's only one suggestion."""
    doc = _Doc(success=False, didyoumeans={"#text": "population of Ireland"})
    out = _format_doc(doc, "popultion ireland")
    assert "Did you mean: population of Ireland" in out


def test_format_timeout() -> None:
    """Wolfram returns ``<queryresult timedout="" numpods="0"/>`` with no
    success attribute when its solver times out internally."""
    doc = _Doc(success=None, timedout="20.001", pods=[])
    out = _format_doc(doc, "distance of the planets from the sun")
    assert "timed out internally" in out
    assert "more specific query" in out


def test_format_no_success_no_timeout() -> None:
    """Defensive: if Wolfram returns a shape we don't recognise."""
    doc = _Doc(success=None, timedout=None, pods=[])
    out = _format_doc(doc, "x")
    assert "no success status" in out


# ── env gating ───────────────────────────────────────────────────────


def test_missing_app_id_raises_upstream(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WOLFRAM_APP_ID", raising=False)
    h = MathHandler(hub=Hub(store=store))
    with pytest.raises(Upstream, match="WOLFRAM_APP_ID"):
        h.get(q="anything")


def test_kind_spec_declares_env_requirement() -> None:
    assert MathHandler.spec.requires_env == ("WOLFRAM_APP_ID",)


def test_kind_hidden_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WOLFRAM_APP_ID", raising=False)
    assert MathHandler.spec.is_available() is False


def test_kind_visible_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WOLFRAM_APP_ID", "anything")
    assert MathHandler.spec.is_available() is True


# ── upstream errors propagate cleanly ────────────────────────────────


def test_upstream_http_error_renders_as_upstream(
    handler: MathHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(app_id: str, expression: str) -> Any:
        raise RuntimeError("ECONNRESET")

    monkeypatch.setattr("precis.handlers.math._run_query", _boom)
    with pytest.raises(Upstream, match="Wolfram Alpha API error"):
        handler.get(q="anything")
