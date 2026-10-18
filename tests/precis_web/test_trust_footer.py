"""gr496226: the ``get`` trust footer is an MCP affordance — on the web
the raw call syntax becomes a console deep-link (the same consider call
prefilled for the operator) plus a link to the ref's fisheye page; the
stamp itself stays visible."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.console import _parse_args

pytestmark = pytest.mark.db


@pytest.fixture
def client(runtime_with_store: PrecisRuntime) -> TestClient:
    app = create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


def _put_memory(runtime: PrecisRuntime, text: str) -> int:
    out = runtime.dispatch("put", {"kind": "memory", "text": text})
    m = re.search(r"\bme(\d+)\b", out)
    assert m, out
    return int(m.group(1))


def _sha(store: Store, mid: int) -> str:
    state = store.trust_state("ref", mid)
    assert state is not None and state.sha
    return state.sha


def _console_href(page_text: str) -> str:
    m = re.search(r'href="(/console\?[^"]*)"', page_text)
    assert m, page_text
    return m.group(1).replace("&amp;", "&")


def test_detail_page_links_the_trust_footer_instead_of_printing_the_call(
    client: TestClient, runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(runtime_with_store, "a note worth considering")
    sha = _sha(store, mid)
    page = client.get(f"/refs/memory/{mid}")
    assert page.status_code == 200
    # the stamp + sha stay; the raw call text is gone (the console
    # href carries it urlencoded, so the python-call shape is the check)
    assert "trust: generated · hunch" in page.text
    assert sha in page.text
    assert "edit(kind=" not in page.text
    assert "mode='trust'" not in page.text
    # the console deep-link stages the same call (args= rides JSON)
    href = _console_href(page.text)
    args_text = parse_qs(urlparse(href).query)["args_text"][0]
    payload = _parse_args("edit", args_text)
    assert payload["kind"] == "memory"
    assert str(payload["id"]) == str(mid)
    assert payload["mode"] == "trust"
    assert payload["args"]["trust"]["expected_sha"] == sha
    assert payload["args"]["trust"]["confidence"] == "considered"
    # and the fisheye page stays one click away on the nav's eye link
    assert f'/eye/me{mid}"' in page.text


def test_detail_page_without_a_trust_line_renders_no_footer(
    client: TestClient, store: Store
) -> None:
    # job is an unconverted kind rendering through the same generic
    # template: no trust line appended, so no footer either.
    ref = store.insert_ref(kind="job", slug=None, title="some job", meta={})
    page = client.get(f"/refs/job/{ref.id}")
    assert page.status_code == 200
    assert "mark considered" not in page.text


def test_strip_trust_footer_leaves_a_body_without_the_line_alone(
    store: Store,
) -> None:
    from precis_web.trust_footer import strip_trust_footer

    body, ctx = strip_trust_footer(store, "memory", 1, "just text")
    assert body == "just text" and ctx is None
    body, ctx = strip_trust_footer(store, "memory", 1, "")
    assert body == "" and ctx is None
