"""gr574952 / gr574990: the memory detail page is a person's view of the
agent's ``get`` body — the ``# memory <id>: <title>`` header, the
``tags:`` line and the gripe hint are the agent's frame (h1, chips and
nothing on the page), and the record id reads as the universal handle
with a copy button."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.runtime import PrecisRuntime
from precis.runtime.handle_deref import MEMORY_GRIPE_HINT
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.refs import _strip_memory_chrome

pytestmark = pytest.mark.db


@pytest.fixture
def client(runtime_with_store: PrecisRuntime) -> TestClient:
    app = create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


def _put_memory(runtime: PrecisRuntime, text: str, **extra: object) -> int:
    out = runtime.dispatch("put", {"kind": "memory", "text": text, **extra})
    m = re.search(r"\bme(\d+)\b", out)
    assert m, out
    return int(m.group(1))


def test_memory_detail_drops_the_agent_frame_and_shows_the_handle(
    client: TestClient, runtime_with_store: PrecisRuntime
) -> None:
    mid = _put_memory(
        runtime_with_store,
        "Programme root body\n\nSecond paragraph names me1.",
        title="Programme root title",
        tags=["topic:frame-test"],
    )
    page = client.get(f"/refs/memory/{mid}")
    assert page.status_code == 200
    # the title lives in the h1 (and <title>), not echoed as the body's
    # first line
    assert f"# memory {mid}" not in page.text
    # the prose stays
    assert "Programme root body" in page.text
    assert "Second paragraph names" in page.text
    # tags are chips, not a ``tags:`` text line; the hint is agent-only
    assert "tags: " not in page.text
    assert MEMORY_GRIPE_HINT[:24] not in page.text
    # the handle with its copy button, the numeric id in the hover title
    assert f'data-copy="me{mid}"' in page.text
    assert f'title="#{mid}"' in page.text


def test_strip_memory_chrome_only_touches_a_framed_body() -> None:
    framed = (
        "# memory 42: A title\n\nprose line\n\ntags: SPACE:repo-dev section:gotchas\n\n"
        "Links:\n{a\tb}\nx\ty\n" + MEMORY_GRIPE_HINT
    )
    assert _strip_memory_chrome(framed, 42) == "prose line\n\nLinks:\n{a\tb}\nx\ty"
    # a sub-kind header (lemma/inference) is a frame too
    assert _strip_memory_chrome("# lemma 42\n\nclaim", 42) == "claim"
    # no header: not a framed render, returned as is
    assert _strip_memory_chrome("prose only\ntags: x", 42) == "prose only\ntags: x"
    assert _strip_memory_chrome("# memory 43: other\n\nbody", 42).startswith(
        "# memory 43"
    )
