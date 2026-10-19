"""``--upload`` of the sanity board: the slug guard and what lands.

Uses the test database (the ``store`` fixture) — never prod. Route tests
stay DB-free in ``test_pcb_sanity_board.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.pcb import sanity_board as sb
from precis.pcb import sanity_upload as up


@pytest.fixture(scope="module")
def straight() -> tuple[sb.SanityResult, sb.Built]:
    built = sb.build(sb.SPECS["straight"])
    return sb.route(sb.SPECS["straight"], built), built


def test_upload_writes_a_frozen_routed_board(
    store: Any, straight: tuple[sb.SanityResult, sb.Built]
) -> None:
    result, built = straight
    slug = up.upload(store, result, built)
    assert slug == "sanity-straight"
    ref = store.get_ref(kind="pcb", id=slug)
    graph = store.pcb_graph(int(ref.id))
    assert {i["refdes"]: i["fixed"] for i in graph["instances"]} == {
        r: "both" for r in ("J_T", "J_B", "J_L", "J_R")
    }
    assert len(graph["nets"]) == 2 * sb.PINS
    board_id = store.pcb_ensure_board(int(ref.id))
    kinds = [row["ctype"] for row in store.pcb_copper_list(board_id)]
    assert kinds.count("track") >= 2 * sb.PINS


def test_existing_slug_is_refused_and_left_untouched(
    store: Any, straight: tuple[sb.SanityResult, sb.Built]
) -> None:
    result, built = straight
    up.upload(store, result, built)
    ref = store.get_ref(kind="pcb", id="sanity-straight")
    before = store.pcb_graph(int(ref.id))
    with pytest.raises(up.UploadRefused, match="never replaced"):
        up.upload(store, result, built)
    assert store.pcb_graph(int(ref.id)) == before


def test_version_takes_the_next_free_suffix(
    store: Any, straight: tuple[sb.SanityResult, sb.Built]
) -> None:
    result, built = straight
    assert up.upload(store, result, built) == "sanity-straight"
    assert up.upload(store, result, built, version=True) == "sanity-straight-v2"
    assert up.upload(store, result, built, version=True) == "sanity-straight-v3"


def test_slug_outside_the_namespace_is_refused(store: Any) -> None:
    with pytest.raises(up.UploadRefused, match="namespace"):
        up.resolve_slug(store, "taz15", version=True)
