"""Real-PG tests for the focus page's came-from slot, embedded peek cards
and keyboard map (web-graph-navigation items 8, 8b and 9).

Pinned here: a quest with 450 serving papers renders focus, came-from and
peek in one page whose peek payload stays inside the ~400 B/node budget;
the per-rel peek counts come from one ``link_rel_summary_for_refs`` call;
two successive refocuses keep the ring order of the nodes both rings
share, with the previous focus pinned to ring 1's top slot; a paper card
carries author/year/venue/abstract, a finding its provenance count, a
memory its section; and every key of the keyboard map reaches an anchor
or input on the rendered page (AC 9 — the alpine handler itself is
checked by hand after deploy).
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store.store import Store
from precis.store.types import Tag
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.graph_peek import PEEK_BYTES_PER_NODE


@pytest.fixture
def graph_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _h(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _ring(html_text: str) -> list[str]:
    """Node handles in ``data-ring`` order."""
    pairs = re.findall(r'data-peek="([^"]+)" data-ring="(\d+)"', html_text)
    return [h for h, _ in sorted(pairs, key=lambda p: int(p[1]))]


def _peeks(html_text: str) -> str:
    m = re.search(
        r'<div id="graph-peeks" hidden>(.*?)</div>\s*<script>', html_text, re.S
    )
    assert m, "no embedded peek set"
    return m.group(1)


def _card(html_text: str, handle: str) -> str:
    m = re.search(
        rf'<div data-peek-card="{re.escape(handle)}">(.*?)</div>',
        _peeks(html_text),
        re.S,
    )
    assert m, f"no peek card for {handle}"
    return m.group(1)


def test_hub_with_450_papers_renders_came_from_and_peek(
    graph_client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="the hub").id
    abstract = "word " * 200
    for i in range(450):
        p = store.insert_ref(
            kind="paper",
            slug=f"p{i}",
            title=f"paper {i}",
            authors=[{"name": "Curie, Marie"}, {"name": "Bohr, Niels"}],
            year=2001,
            meta={"journal": "J. Test", "abstract": abstract},
        ).id
        store.add_link(src_ref_id=p, dst_ref_id=quest, relation="serves")
    first = store.insert_ref(kind="paper", slug="entry", title="entry paper").id
    store.add_link(src_ref_id=first, dst_ref_id=quest, relation="serves")

    calls: list[int] = []
    real = store.link_rel_summary_for_refs

    def counting(ids: list[int]) -> dict[int, dict[tuple[str, str], int]]:
        calls.append(len(ids))
        return real(ids)

    monkeypatch.setattr(store, "link_rel_summary_for_refs", counting)

    assert graph_client.get(f"/graph/paper/{first}").status_code == 200
    calls.clear()
    r = graph_client.get(f"/graph/quest/{quest}")
    assert r.status_code == 200
    assert len(calls) == 1, "peek counts must come from one batched call"

    # came-from slot names the paper and the rel followed, stored direction
    slot = re.search(r'aria-label="came from">(.*?)</p>', r.text, re.S)
    assert slot and f'data-came-from="{_h("paper", first)}"' in slot.group(1)
    assert "→ serves" in slot.group(1)
    # ... and the paper holds ring 1's top slot
    assert _ring(r.text)[0] == _h("paper", first)

    peeks = _peeks(r.text)
    n_cards = peeks.count("data-peek-card=")
    assert 0 < n_cards <= 61
    # budget: abstracts drop before the text passes ~400 B per node
    text = re.sub(r"<[^>]+>", "", peeks)
    assert len(" ".join(text.split())) <= PEEK_BYTES_PER_NODE * n_cards * 1.1
    entry_card = _card(r.text, _h("paper", first))
    assert "entry paper" in entry_card and "→serves 1" in entry_card


def test_paper_finding_memory_cards(graph_client: TestClient, store: Store) -> None:
    focus = store.insert_ref(kind="memory", slug=None, title="centre").id
    paper = store.insert_ref(
        kind="paper",
        slug="pk",
        title="A <b>paper</b>",
        authors=[{"name": "Curie, Marie"}],
        year=1903,
        meta={"journal": "Nature", "abstract": "Radium glows. " * 40},
    ).id
    fi = store.insert_ref(kind="finding", slug=None, title="radium glows").id
    mem = store.insert_ref(kind="memory", slug=None, title="a note").id
    store.add_tag(mem, Tag.open("section:gotchas"))
    store.add_link(src_ref_id=paper, dst_ref_id=fi, relation="corroborates")
    store.add_link(src_ref_id=paper, dst_ref_id=focus, relation="related-to")
    store.add_link(src_ref_id=fi, dst_ref_id=focus, relation="related-to")
    store.add_link(src_ref_id=mem, dst_ref_id=focus, relation="related-to")

    r = graph_client.get(f"/graph/memory/{focus}")
    assert r.status_code == 200
    pc = _card(r.text, _h("paper", paper))
    assert "A &lt;b&gt;paper&lt;/b&gt;" in pc  # escaped
    assert "Curie" in pc and "1903" in pc and "Nature" in pc
    assert "Radium glows." in pc and "…" in pc  # abstract clipped
    assert "1 provenance link" in _card(r.text, _h("finding", fi))
    assert "section:gotchas" in _card(r.text, _h("memory", mem))
    # first visit: the slot is empty
    slot = re.search(r'aria-label="came from">(.*?)</p>', r.text, re.S)
    assert slot and "came from" not in slot.group(1)


def test_ring_order_stable_across_refocus(
    graph_client: TestClient, store: Store
) -> None:
    a = store.insert_ref(kind="memory", slug=None, title="a").id
    b = store.insert_ref(kind="memory", slug=None, title="b").id
    shared = [
        store.insert_ref(kind="memory", slug=None, title=f"s{i}").id for i in range(6)
    ]
    store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
    # link the shared nodes in opposite orders so store order would differ
    for s in shared:
        store.add_link(src_ref_id=a, dst_ref_id=s, relation="related-to")
    for s in reversed(shared):
        store.add_link(src_ref_id=b, dst_ref_id=s, relation="related-to")
    common = {_h("memory", s) for s in shared}

    ring_a = _ring(graph_client.get(f"/graph/memory/{a}?depth=1").text)
    ring_b = _ring(graph_client.get(f"/graph/memory/{b}?depth=1").text)
    assert ring_b[0] == _h("memory", a)  # came-from pinned to the top
    assert [h for h in ring_a if h in common] == [h for h in ring_b if h in common]


def test_keyboard_map_targets_are_on_the_page(
    graph_client: TestClient, store: Store
) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="goal").id
    paper = store.insert_ref(kind="paper", slug="kp", title="p").id
    fi = store.insert_ref(kind="finding", slug=None, title="claim").id
    store.add_link(src_ref_id=paper, dst_ref_id=fi, relation="corroborates")
    store.add_link(src_ref_id=fi, dst_ref_id=quest, relation="serves")

    graph_client.get(f"/graph/paper/{paper}")
    r = graph_client.get(f"/graph/finding/{fi}")
    page = r.text

    def target(key: str) -> str:
        for tag in re.findall(r"<(?:a|input)\b[^>]*>", page):
            m = re.search(r'data-key="([^"]+)"', tag)
            if m and key in m.group(1).split(" "):
                return tag
        raise AssertionError(f"no target for key {key!r}")

    assert f'href="/graph/paper/{paper}"' in target("u")  # retreat
    assert target("Backspace") == target("u")
    assert f'href="/graph/quest/{quest}"' in target("U")  # zoom out
    assert target("/").startswith("<input")  # search box
    assert f"/graph/finding/{fi}?depth=1" in target("1")
    assert f"/graph/finding/{fi}?depth=2" in target("2")
    # j/k + Enter: every ring node is a link carrying its ring index
    nodes = re.findall(r'<a href="([^"]+)" data-peek="[^"]+" data-ring="\d+"', page)
    assert sorted(nodes) == sorted([f"/graph/paper/{paper}", f"/graph/quest/{quest}"])
    # depth links keep the active filters
    r = graph_client.get(f"/graph/finding/{fi}?kinds=paper&depth=1")
    assert "kinds=paper&amp;depth=2" in r.text
