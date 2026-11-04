"""Gripe filing identity and trust render without guessing a model."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")

from precis.runtime.trust_verb import TrustFooter
from precis.store import Tag
from precis_web.routes import gripes


def test_gripe_origin_and_trust_line(runtime, client, monkeypatch) -> None:
    runtime.store.gripes[0].meta = {
        "filed_by": {
            "actor": "test-agent",
            "client": "test-client/2",
            "session_id": "session-0123456789-abcdef",
            "model": "test-model",
            "model_source": "declared",
            "source": "test-tick",
        }
    }
    monkeypatch.setattr(
        gripes,
        "trust_footer",
        lambda *a: TrustFooter("gripe", 96, "generated · hunch", None, "unused"),
    )

    response = client.get("/gripes/96")

    assert response.status_code == 200
    assert "test-agent" in response.text
    assert "client test-client/2" in response.text
    assert "session-0123456…</span>" in response.text
    assert "session-0123456789-abcdef" not in response.text
    assert "model test-model (declared)" in response.text
    assert "source test-tick" in response.text
    assert "trust: generated · hunch" in response.text


@pytest.mark.parametrize(
    "meta", [None, {}, {"filed_by": None}, {"filed_by": {}}, {"filed_by": "old"}]
)
def test_legacy_origin_uses_readonly_tags(runtime, client, monkeypatch, meta) -> None:
    runtime.store.gripes[0].meta = meta
    monkeypatch.setattr(
        runtime.store,
        "tags_for",
        lambda *a: [Tag.open("agent:read-only-run"), Tag.open("source:agent_ro")],
    )
    response = client.get("/gripes/96")
    assert response.status_code == 200
    assert "agent:read-only-run" in response.text
    assert "source:agent_ro" in response.text
    assert "model unknown" in response.text


def test_client_name_does_not_imply_a_model(runtime, client) -> None:
    runtime.store.gripes[0].meta = {"filed_by": {"client": "claude-code/1"}}
    response = client.get("/gripes/96")
    assert response.status_code == 200
    assert "client claude-code/1" in response.text
    assert "model unknown" in response.text


def test_origin_escapes_html(runtime, client) -> None:
    runtime.store.gripes[0].meta = {
        "filed_by": {"client": "<script>origin</script>", "source": "<b>source</b>"}
    }
    response = client.get("/gripes/96")
    assert "<script>origin</script>" not in response.text
    assert "&lt;script&gt;origin&lt;/script&gt;" in response.text
    assert "&lt;b&gt;source&lt;/b&gt;" in response.text


def test_agentlog_resolves_model_after_filing(runtime, client, monkeypatch) -> None:
    runtime.store.gripes[0].meta = {"filed_by": {"agentlog_id": 123}}
    monkeypatch.setattr(gripes, "agentlog_model", lambda *a: "resolved-model")
    response = client.get("/gripes/96")
    assert "model resolved-model (agentlog)" in response.text


def test_missing_origin_renders_unknown_without_breaking_title(runtime, client) -> None:
    response = client.get("/gripes/96")
    assert response.status_code == 200
    assert "client unknown" in response.text
    assert "model unknown" in response.text
    assert re.search(r"<h1\b[^>]*>the paper slug NotFound", response.text)
