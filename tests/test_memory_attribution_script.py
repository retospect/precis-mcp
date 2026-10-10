"""scripts/memory-attribution-audit — dry run changes nothing, --apply tags the flagged."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store, Tag
from tests.conftest import id_of

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "_memory_attribution_audit.py"
)
_TAG = Tag.closed("AUDIT", "ungrounded-number")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_memory_attribution_audit", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seeded(hub: Hub, store: Store) -> dict[str, int]:
    """One bad, one good, one citation-free memory; the bad one is untagged
    afterwards, as a pre-gate row would be."""
    h = MemoryHandler(hub=hub)
    src = id_of(h.put(text="source: nothing numeric").body)
    good_src = id_of(h.put(text="source: scale is about 10 nm").body)
    bad = id_of(h.put(text=f"it is 5 nm per memory:{src}.").body)
    good = id_of(h.put(text=f"it is 10 nm per memory:{good_src}.").body)
    plain = id_of(h.put(text="a free-standing 5 nm remark").body)
    store.remove_tag(bad, _TAG)  # simulate a memory written before the gate
    assert not store.has_tag(bad, "AUDIT", "ungrounded-number")
    return {"bad": bad, "good": good, "plain": plain}


def test_dry_run_reports_and_changes_nothing(
    store: Store, seeded: dict[str, int]
) -> None:
    mod = _load()
    out = mod.audit(store, apply=False)
    assert seeded["bad"] in out["flagged_ids"]
    assert seeded["good"] not in out["flagged_ids"]
    assert seeded["plain"] not in out["flagged_ids"]
    assert out["applied"] == 0
    assert out["by_cite_kind"].get("memory", 0) >= 1
    assert f'me{seeded["bad"]}  "5 nm"  memory:' in "\n".join(out["sample"])
    assert not store.has_tag(seeded["bad"], "AUDIT", "ungrounded-number")


def test_apply_tags_exactly_the_flagged_rows(
    store: Store, seeded: dict[str, int]
) -> None:
    mod = _load()
    out = mod.audit(store, apply=True)
    assert out["applied"] == out["flagged"] == len(out["flagged_ids"])
    for rid in out["flagged_ids"]:
        assert store.has_tag(rid, "AUDIT", "ungrounded-number")
    assert seeded["bad"] in out["flagged_ids"]
    assert not store.has_tag(seeded["good"], "AUDIT", "ungrounded-number")
    assert not store.has_tag(seeded["plain"], "AUDIT", "ungrounded-number")
    # Body untouched.
    h_body = store.chunks.list_chunks_for_ref(seeded["bad"])[0].text
    assert h_body.startswith("it is 5 nm per memory:")


def test_cite_kind_buckets() -> None:
    mod = _load()
    assert mod.cite_kind("websearch:170350") == "websearch"
    assert mod.cite_kind("pc995663") == "pc"
    assert mod.cite_kind("[pa12]") == "pa"
    assert mod.cite_kind("futrell25") == "paper-key"


def test_clear_stale_lifts_only_the_system_tag_the_gate_no_longer_raises(
    store: Store, seeded: dict[str, int]
) -> None:
    # A grounded memory tagged by an earlier gate round, and a citation-free
    # memory an agent tagged by hand.
    store.add_tag(seeded["good"], _TAG, set_by="system")
    store.add_tag(seeded["plain"], _TAG, set_by="agent")
    mod = _load()
    out = mod.audit(store, apply=True, clear_stale=True)
    assert seeded["bad"] in out["flagged_ids"]
    assert out["cleared"] == 1
    assert store.has_tag(seeded["bad"], "AUDIT", "ungrounded-number")
    assert not store.has_tag(seeded["good"], "AUDIT", "ungrounded-number")
    assert store.has_tag(seeded["plain"], "AUDIT", "ungrounded-number")
    # Without --clear-stale nothing is lifted.
    store.add_tag(seeded["good"], _TAG, set_by="system")
    out = mod.audit(store, apply=True)
    assert out["cleared"] == 0
    assert store.has_tag(seeded["good"], "AUDIT", "ungrounded-number")
