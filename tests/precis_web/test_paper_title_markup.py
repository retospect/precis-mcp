"""gr515227 title views on the web surfaces — markup renders, injection
stays inert.

Stored titles are raw (publisher markup, LaTeX, and potentially hostile
HTML). The paper view + hover popover render the ``title_md`` view
through ``render_markdown``, which escapes first — a ``<script>`` /
``onerror=`` payload must come out as inert text, never live markup.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig

#: A stored title exercising every channel at once: an HTML injection
#: payload, an attribute-breakout attempt, publisher <sup> markup and a
#: literal (non-math) dollar.
_EVIL = (
    "Study <script>alert(1)</script> of <img src=x onerror=alert(2)> "
    'Pd<sup>II</sup> and "The $100 Laptop"'
)


def test_paper_view_title_is_md_and_injection_inert(client, runtime) -> None:
    runtime.store.papers[0].title = _EVIL  # id=10, slug "smith2024"
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    html = resp.text
    # Nothing executable survives: no live script element and no
    # unescaped img/event-handler payload anywhere on the page.
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror=alert(2)>" not in html
    # The Meta tab's title edit field still carries the RAW title — but
    # HTML-escaped inside the attribute, inert.
    assert "onerror=alert(2)&gt;" in html
    # The legitimate content renders: sup → math (empty-base repair pulls
    # Pd inside the $…$), and the $ stays a dollar (the \$ escape
    # post-pass), not a math delimiter.
    assert "$Pd^{\\mathrm{II}}$" in html
    assert "$100 Laptop" in html


@pytest.fixture
def web_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_preview_popover_title_injection_inert(web_client, store) -> None:
    rid = store.insert_ref(kind="paper", slug="evilprev", title=_EVIL, year=2020).id
    resp = web_client.get(f"/preview/paper/{rid}")
    assert resp.status_code == 200
    html = resp.text
    assert "<script>alert(1)</script>" not in html
    assert "onerror" not in html
    assert "$Pd^{\\mathrm{II}}$" in html


def test_entity_encoded_script_stays_text(client, runtime) -> None:
    """``&lt;script&gt;`` double-decodes inside ``title_md`` and parses as
    a real tag, which is *dropped* — the payload can only ever surface as
    the inert text node ``alert(9)``, never as a ``<script`` element."""
    runtime.store.papers[0].title = "Encoded &lt;script&gt;alert(9)&lt;/script&gt; tail"
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert "<script>alert(9)</script>" not in resp.text
    assert "alert(9)" in resp.text  # payload reduced to inert text


def test_preview_popover_plain_attrs_stay_escaped(web_client, store) -> None:
    """A title whose payload only works as an *attribute* (no tags) must
    stay escaped wherever the raw/plain title lands in an attribute."""
    rid = store.insert_ref(
        kind="paper",
        slug="evilattr",
        title='x" onmouseover="alert(1)',
        year=2020,
    ).id
    resp = web_client.get(f"/preview/paper/{rid}")
    assert resp.status_code == 200
    assert 'onmouseover="alert(1)"' not in resp.text
