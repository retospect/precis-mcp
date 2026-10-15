"""Zoom-out totality and behaviour (web-graph-navigation slice 7).

``utils/refeye.py::UP_RELATIONS`` maps every kind to the node a focus
hangs on by its family's "up" relation, or to a disabled rule with its
reason. Pinned here (AC 7): every kind in the kind registry has an entry
(a missing kind is a failure, never a silent default), every step names a
registered relation, and the focus page renders zoom-out as a live link
or disabled with that entry's reason; the fixture paper → ``corroborates``
→ finding → ``serves`` → quest walks retreat back to the paper and
zoom-out up to the quest. Also: degree tie-break with the chooser, the
todo tree's virtual ``parent`` step and the "no live link" reason.
"""

from __future__ import annotations

import re

import pytest

from precis.store.store import Store
from precis.utils import handle_registry, kind_facts
from precis.utils.refeye import UP_RELATIONS, resolve_up


def test_every_registered_kind_has_an_up_entry() -> None:
    declared = {s.kind for s in kind_facts.all_declared_specs()}
    missing = declared - set(UP_RELATIONS)
    assert not missing, (
        f"kind(s) {sorted(missing)} have no UP_RELATIONS entry — add their up "
        "relation or a disabled UpRule with the reason (utils/refeye.py)"
    )


def test_every_entry_is_live_xor_disabled_with_reason() -> None:
    for kind, rule in UP_RELATIONS.items():
        assert bool(rule.steps) != bool(rule.reason), kind
        for _rel, direction in rule.steps:
            assert direction in ("out", "in"), kind


def test_up_steps_name_registered_relations(store: Store) -> None:
    valid = store.valid_relations() | {"parent"}
    used = {rel for rule in UP_RELATIONS.values() for rel, _ in rule.steps}
    assert used <= valid, sorted(used - valid)


def _ref(store: Store, kind: str, title: str, parent_id: int | None = None) -> int:
    if kind == "paper":
        return store.insert_ref(
            kind="paper",
            slug=f"{title.replace(' ', '')}2026",
            title=title,
            provider="manual",
            meta={},
        ).id
    return store.insert_ref(kind=kind, slug=None, title=title, parent_id=parent_id).id


def test_degree_tie_break_and_chooser(store: Store) -> None:
    fi = _ref(store, "finding", "a claim")
    small, big = _ref(store, "quest", "small goal"), _ref(store, "quest", "big goal")
    store.add_link(src_ref_id=fi, dst_ref_id=small, relation="serves")
    store.add_link(src_ref_id=fi, dst_ref_id=big, relation="serves")
    for i in range(3):
        p = _ref(store, "paper", f"p{i}")
        store.add_link(src_ref_id=p, dst_ref_id=big, relation="serves")
    up = resolve_up(store, "finding", fi)
    assert up["target"].id == big and up["rel"] == "serves"
    assert [r.id for r in up["others"]] == [small] and up["n_others"] == 1


def test_todo_parent_and_no_link_reason(store: Store) -> None:
    root = _ref(store, "todo", "root")
    child = _ref(store, "todo", "child", parent_id=root)
    assert resolve_up(store, "todo", child, parent_id=root)["target"].id == root
    lone = resolve_up(store, "todo", root, parent_id=None)
    assert lone["target"] is None and "parent (out)" in lone["reason"]
    gr = resolve_up(store, "gripe", 1)
    assert gr["reason"] == "no container relation registered for gripe"


@pytest.fixture
def graph_client(runtime_with_store, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_retreat_to_paper_and_zoom_out_to_quest(graph_client, store: Store) -> None:
    paper = _ref(store, "paper", "evidence")
    fi = _ref(store, "finding", "the hub")
    qu = _ref(store, "quest", "the goal")
    store.add_link(src_ref_id=paper, dst_ref_id=fi, relation="corroborates")
    store.add_link(src_ref_id=fi, dst_ref_id=qu, relation="serves")

    r = graph_client.get(f"/graph/paper/{paper}")
    # the paper's own zoom-out is the hub it corroborates
    assert f'data-zoom-out="{handle_registry.format_handle("finding", fi)}"' in r.text
    r = graph_client.get(f"/graph/finding/{fi}")
    assert r.status_code == 200
    retreat = re.search(r'<a href="([^"]+)"[^>]*aria-label="retreat"', r.text)
    assert retreat and retreat.group(1) == f"/graph/paper/{paper}"
    zoom = re.search(r'<a href="([^"]+)"[^>]*data-zoom-out="([^"]+)"', r.text)
    assert zoom
    assert zoom.group(1) == f"/graph/quest/{qu}"
    assert zoom.group(2) == handle_registry.format_handle("quest", qu)


def test_disabled_kind_renders_its_reason(graph_client, store: Store) -> None:
    an = _ref(store, "anki", "a card")
    r = graph_client.get(f"/graph/anki/{an}")
    assert r.status_code == 200
    assert 'aria-disabled="true"' in r.text
    assert "no container relation registered for anki" in r.text
    assert "data-zoom-out=" not in r.text


def test_eye_page_up_link_reads_the_same_table(graph_client, store: Store) -> None:
    """``/eye/<handle>`` shows one "up" link from ``resolve_up``, keeping
    the rung and ring filter; the chooser lists the runner-up."""
    fi = _ref(store, "finding", "the hub")
    qu, alt = _ref(store, "quest", "the goal"), _ref(store, "quest", "side goal")
    store.add_link(src_ref_id=fi, dst_ref_id=qu, relation="serves")
    store.add_link(src_ref_id=fi, dst_ref_id=alt, relation="serves")
    store.add_link(
        src_ref_id=_ref(store, "paper", "p"), dst_ref_id=qu, relation="serves"
    )
    fi_h = handle_registry.format_handle("finding", fi)
    qu_h = handle_registry.format_handle("quest", qu)
    r = graph_client.get(f"/eye/{fi_h}?extent=fisheye&kind=quest")
    assert r.status_code == 200
    up = re.search(r'<a href="([^"]+)"[^>]*data-zoom-out="([^"]+)"', r.text)
    assert up and up.group(2) == qu_h
    assert up.group(1).replace("&amp;", "&") == f"/eye/{qu_h}?extent=fisheye&kind=quest"
    alt_h = handle_registry.format_handle("quest", alt)
    assert f'data-zoom-alt="{alt_h}"' in r.text


def test_eye_page_up_link_disabled_with_reason(graph_client, store: Store) -> None:
    an = _ref(store, "anki", "a card")
    r = graph_client.get(f"/eye/{handle_registry.format_handle('anki', an)}")
    assert r.status_code == 200
    assert 'aria-disabled="true"' in r.text
    assert "no container relation registered for anki" in r.text
    assert "data-zoom-out=" not in r.text
    td = _ref(store, "todo", "orphan")
    r = graph_client.get(f"/eye/{handle_registry.format_handle('todo', td)}")
    assert "no live parent (out) link from this todo" in r.text
