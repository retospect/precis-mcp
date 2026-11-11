"""Spec-root placement audit (gr631564).

A spec root — a memory tagged ``spec:<slug>`` + ``section:index`` — hangs
under exactly one part-of parent, a programme root directly under the
Specifications hub me568215; topical hubs relate but never parent
(precis-spec-help). The write path re-checks the root's part-of parents
after a put/link/tag write: a miss stamps the system-owned
``AUDIT:spec-root-parent`` flag and the ack carries an advisory; a clean
re-check clears the system flag (an author-set row is never touched).
The export manifest carries the ``spec:<slug>`` column (17) and every
live parent in column 12 — ``scripts/memory-lint`` check 15 reads both
offline.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

from precis.cli.memory import SECTIONS_MANIFEST, export_memory_nodes
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from precis.utils import memory_spec
from tests.conftest import id_of

_AUDIT = "AUDIT:spec-root-parent"
_BODY = "The evidence stays on the source ref.\nSource: memory:1"


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(tag) for tag in store.tags_for(ref_id)}


def _put(handler: MemoryHandler, title: str, **kw: Any) -> int:
    return id_of(handler.put(title=title, text=_BODY, **kw).body)


@pytest.fixture
def specs(handler: MemoryHandler, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """A Specifications hub with a programme root under it, plus an unrelated
    topical hub. The prod hub id (me568215) can't be a fixture's ref_id, so
    the constant is patched onto the row this fixture creates — the check
    reads the hub by ancestry, not by name."""
    hub_id = _put(handler, "Specifications", tags=["section:index"])
    programme = _put(
        handler,
        "Kinetics programme",
        tags=["section:index"],
        link=f"memory:{hub_id}",
        rel="part-of",
    )
    topical = _put(handler, "CAD/se hub", tags=["section:index"])
    monkeypatch.setattr(memory_spec, "SPECIFICATIONS_HUB_ID", hub_id)
    return {"hub": hub_id, "programme": programme, "topical": topical}


def test_put_spec_root_under_topical_hub_audits(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    ack = handler.put(
        title="Spec: routing: the purpose",
        text=_BODY,
        tags=["section:index", "spec:routing"],
        link=f"memory:{specs['topical']}",
        rel="part-of",
    )
    ref_id = id_of(ack.body)
    assert f"{_AUDIT}:" in ack.body
    assert _AUDIT in _tags(store, ref_id)


def test_put_spec_root_under_programme_is_clean(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    ack = handler.put(
        title="Spec: routing: the purpose",
        text=_BODY,
        tags=["section:index", "spec:routing"],
        link=f"memory:{specs['programme']}",
        rel="part-of",
    )
    ref_id = id_of(ack.body)
    assert f"{_AUDIT}:" not in ack.body
    assert _AUDIT not in _tags(store, ref_id)


def test_put_spec_root_directly_under_spec_hub_audits(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    """A spec root part-of me568215 itself is misplaced — it hangs under
    a programme root, not directly off the hub (reachability alone used
    to clear it)."""
    ack = handler.put(
        title="Spec: routing: the purpose",
        text=_BODY,
        tags=["section:index", "spec:routing"],
        link=f"memory:{specs['hub']}",
        rel="part-of",
    )
    ref_id = id_of(ack.body)
    assert f"{_AUDIT}:" in ack.body
    assert "the Specifications hub itself" in ack.body
    assert _AUDIT in _tags(store, ref_id)


def test_spec_root_under_another_spec_root_audits(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    """A spec root part-of another spec root violates: its parent's
    parent is the programme, not the hub — a spec nests sections, never
    other spec roots."""
    parent = _put(
        handler,
        "Spec: routing: the purpose",
        tags=["section:index", "spec:routing"],
        link=f"memory:{specs['programme']}",
        rel="part-of",
    )
    ack = handler.put(
        title="Spec: caching: the purpose",
        text=_BODY,
        tags=["section:index", "spec:caching"],
        link=f"memory:{parent}",
        rel="part-of",
    )
    ref_id = id_of(ack.body)
    assert f"{_AUDIT}:" in ack.body
    assert "not a programme root" in ack.body
    assert _AUDIT in _tags(store, ref_id)
    assert _AUDIT not in _tags(store, parent)


def test_second_topical_parent_audits(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    """A spec root under a programme root that picks up a second part-of
    edge to a topical hub violates — exactly one parent is the shape;
    the topical hub links related-to instead."""
    ref_id = _put(
        handler,
        "Spec: routing: the purpose",
        tags=["section:index", "spec:routing"],
        link=f"memory:{specs['programme']}",
        rel="part-of",
    )
    assert _AUDIT not in _tags(store, ref_id)

    ack = handler.link(id=ref_id, target=f"memory:{specs['topical']}", rel="part-of")
    assert f"{_AUDIT}:" in ack.body
    assert "has 2 part-of parents" in ack.body
    assert _AUDIT in _tags(store, ref_id)


def test_put_spec_root_without_parent_audits(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    ack = handler.put(
        title="Spec: routing: the purpose",
        text=_BODY,
        tags=["section:index", "spec:routing"],
    )
    ref_id = id_of(ack.body)
    assert f"{_AUDIT}:" in ack.body
    assert "has no part-of parent" in ack.body
    assert _AUDIT in _tags(store, ref_id)


def test_reparenting_through_link_clears_flag(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    ref_id = _put(
        handler,
        "Spec: routing: the purpose",
        tags=["section:index", "spec:routing"],
        link=f"memory:{specs['topical']}",
        rel="part-of",
    )
    assert _AUDIT in _tags(store, ref_id)

    # A second edge to the programme does not clear it: the topical-hub
    # edge is itself wrong — topical hubs relate, never parent.
    ack = handler.link(id=ref_id, target=f"memory:{specs['programme']}", rel="part-of")
    assert f"{_AUDIT}:" in ack.body
    assert _AUDIT in _tags(store, ref_id)

    ack = handler.link(
        id=ref_id,
        target=f"memory:{specs['topical']}",
        rel="part-of",
        mode="remove",
    )
    assert f"{_AUDIT}:" not in ack.body
    assert _AUDIT not in _tags(store, ref_id)


def test_tag_writes_make_and_unmake_the_spec_root(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    # A plain node part-of a topical hub is unremarkable …
    ref_id = _put(
        handler,
        "Routing notes",
        link=f"memory:{specs['topical']}",
        rel="part-of",
    )
    # … until spec tags make it a root — then the placement audit fires.
    ack = handler.tag(id=ref_id, add=["section:index", "spec:routing"])
    assert f"{_AUDIT}:" in ack.body
    assert _AUDIT in _tags(store, ref_id)
    # Removing the spec tag unmakes the root; the re-check clears the flag.
    ack = handler.tag(id=ref_id, remove=["spec:routing"])
    assert f"{_AUDIT}:" not in ack.body
    assert _AUDIT not in _tags(store, ref_id)


def test_clean_recheck_preserves_author_flag(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    ref_id = _put(
        handler,
        "Spec: routing: the purpose",
        tags=["section:index", "spec:routing"],
        link=f"memory:{specs['programme']}",
        rel="part-of",
    )
    handler.tag(id=ref_id, add=[_AUDIT])
    # Any link() write triggers a placement re-check; this one stays clean,
    # so only the system flag could clear — the author's row is untouched.
    handler.link(id=ref_id, target=f"memory:{specs['topical']}", rel="cites")
    assert _AUDIT in _tags(store, ref_id)
    with store.tx() as conn:
        row = conn.execute(
            "SELECT rt.set_by FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = 'AUDIT' "
            "AND t.value = 'spec-root-parent'",
            (ref_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "agent"


def test_spec_section_under_its_root_does_not_audit(
    handler: MemoryHandler, store: Store, specs: dict[str, int]
) -> None:
    root = _put(
        handler,
        "Spec: routing: the purpose",
        tags=["section:index", "spec:routing"],
        link=f"memory:{specs['programme']}",
        rel="part-of",
    )
    # A spec section carries spec:<slug> too but section:reference, not
    # section:index — it is not a root and never audits.
    ack = handler.put(
        title="Spec: routing: section 2",
        text=_BODY,
        tags=["section:reference", "spec:routing"],
        link=f"memory:{root}",
        rel="part-of",
    )
    ref_id = id_of(ack.body)
    assert f"{_AUDIT}:" not in ack.body
    assert _AUDIT not in _tags(store, ref_id)


def test_export_manifest_carries_spec_column(
    handler: MemoryHandler, store: Store, specs: dict[str, int], tmp_path: Path
) -> None:
    for rid in specs.values():
        handler.tag(id=rid, add=["SPACE:repo-dev"])
    spec = _put(
        handler,
        "Spec: routing: the purpose",
        tags=["section:index", "spec:routing", "SPACE:repo-dev"],
        link=f"memory:{specs['programme']}",
        rel="part-of",
    )
    plain = _put(
        handler,
        "A repo-dev note",
        tags=["section:reference", "SPACE:repo-dev"],
        link=f"memory:{specs['programme']}",
        rel="part-of",
    )
    dest = tmp_path / "nodes"
    export_memory_nodes(store, dest)
    rows = {
        r[0]: r
        for r in csv.reader(
            (dest / SECTIONS_MANIFEST).read_text(encoding="utf-8").splitlines(),
            delimiter="\t",
        )
        if r
    }
    assert all(len(r) == 17 for r in rows.values())
    assert rows[f"me{spec}"][16] == "spec:routing"
    assert rows[f"me{plain}"][16] == ""
    assert rows[f"me{specs['programme']}"][16] == ""


def test_export_manifest_parent_column_lists_every_parent(
    handler: MemoryHandler, store: Store, specs: dict[str, int], tmp_path: Path
) -> None:
    """Check 15 validates every part-of parent, so the export must not
    drop one: a spec root with a programme AND a topical parent exports
    both handles comma-separated (td632322 — the first-parent-only
    export hid an invalid second parent)."""
    for rid in specs.values():
        handler.tag(id=rid, add=["SPACE:repo-dev"])
    spec = _put(
        handler,
        "Spec: routing: the purpose",
        tags=["section:index", "spec:routing", "SPACE:repo-dev"],
        link=f"memory:{specs['programme']}",
        rel="part-of",
    )
    handler.link(id=spec, target=f"memory:{specs['topical']}", rel="part-of")
    dest = tmp_path / "nodes"
    export_memory_nodes(store, dest)
    rows = {
        r[0]: r
        for r in csv.reader(
            (dest / SECTIONS_MANIFEST).read_text(encoding="utf-8").splitlines(),
            delimiter="\t",
        )
        if r
    }
    assert set(rows[f"me{spec}"][11].split(",")) == {
        f"me{specs['programme']}",
        f"me{specs['topical']}",
    }
