"""Tests for :mod:`precis.jobs.checklist_sync` — the deploy-time sync of
shipped checklist definitions (docs/backlog/checklist-kind.md slice 1).

Modeled on ``tests/test_oracle_sync.py``'s shape: pure-function tests for
hashing/parsing, then integration tests against the real ``store``
fixture for the sync semantics (idempotence, retire-on-removal,
local-rows-untouched) the design doc's acceptance criteria calls out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.jobs.checklist_sync import (
    bundled_checklists_dir,
    check_drift,
    is_disabled_by_env,
    load_checklist_file,
    sync_all,
)
from precis.store import Store

_FIXTURE_YAML = """
name: {name}
items:
  - name: item-a
    phase: setup
    severity: blocking
    decidability: judgment
    prevents: "a fails silently"
  - name: item-b
    severity: advisory
    prevents: "b fails silently"
"""


def _write_fixture(tmp_path: Path, name: str = "sync-test-checklist") -> Path:
    (tmp_path / "fixture.yaml").write_text(
        _FIXTURE_YAML.format(name=name), encoding="utf-8"
    )
    return tmp_path


# ── pure parsing/hashing ─────────────────────────────────────────────


def test_load_checklist_file_parses_name_and_items(tmp_path: Path) -> None:
    d = _write_fixture(tmp_path)
    cf = load_checklist_file(d / "fixture.yaml")
    assert cf.name == "sync-test-checklist"
    assert len(cf.items) == 2
    assert cf.items[0]["name"] == "item-a"
    assert len(cf.sha256) == 64


def test_load_checklist_file_missing_name_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("items: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="name"):
        load_checklist_file(p)


def test_load_checklist_file_item_missing_name_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("name: x\nitems:\n  - prevents: y\n", encoding="utf-8")
    with pytest.raises(ValueError, match="name"):
        load_checklist_file(p)


def test_load_checklist_file_items_not_list_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("name: x\nitems: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="items"):
        load_checklist_file(p)


def test_bundled_checklists_dir_finds_the_fixture() -> None:
    d = bundled_checklists_dir()
    assert d is not None
    assert (d / "test-fixture-checklist.yaml").is_file()


def test_is_disabled_by_env_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_CHECKLIST_AUTO_SYNC", raising=False)
    assert is_disabled_by_env() is False


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_is_disabled_by_env_falsy_values_disable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PRECIS_CHECKLIST_AUTO_SYNC", value)
    assert is_disabled_by_env() is True


# ── integration: sync_all against the real store ────────────────────


def test_sync_all_first_run_inserts_revs(tmp_path: Path, store: Store) -> None:
    d = _write_fixture(tmp_path, "cl-first-run")
    result = sync_all(store, src_dir=d)
    assert result["status"] == "ok"
    [file_result] = result["files"]
    assert file_result["status"] == "synced"
    assert file_result["created"] == 2

    checklist = store.checklist_get("cl-first-run")
    assert checklist is not None
    assert checklist["origin"] == "shipped"
    item_a = store.checklist_item_current(checklist["id"], "item-a")
    assert item_a is not None
    assert item_a["rev"] == 1
    assert item_a["origin"] == "shipped"


def test_sync_all_second_run_is_noop(tmp_path: Path, store: Store) -> None:
    d = _write_fixture(tmp_path, "cl-second-run")
    sync_all(store, src_dir=d)
    checklist = store.checklist_get("cl-second-run")
    assert checklist is not None
    item_before = store.checklist_item_current(checklist["id"], "item-a")
    assert item_before is not None

    result2 = sync_all(store, src_dir=d)
    [file_result] = result2["files"]
    assert file_result["status"] == "up_to_date"

    item_after = store.checklist_item_current(checklist["id"], "item-a")
    assert item_after is not None
    assert item_after["rev"] == item_before["rev"] == 1


def test_sync_all_item_removed_from_file_is_retired(
    tmp_path: Path, store: Store
) -> None:
    d = _write_fixture(tmp_path, "cl-retire-run")
    sync_all(store, src_dir=d)
    checklist = store.checklist_get("cl-retire-run")
    assert checklist is not None
    assert store.checklist_item_current(checklist["id"], "item-b") is not None

    (d / "fixture.yaml").write_text(
        """
name: cl-retire-run
items:
  - name: item-a
    phase: setup
    severity: blocking
    decidability: judgment
    prevents: "a fails silently"
""",
        encoding="utf-8",
    )
    result = sync_all(store, src_dir=d, force=True)
    [file_result] = result["files"]
    assert file_result["retired"] == 1
    assert store.checklist_item_current(checklist["id"], "item-b") is None
    # item-a untouched by the removal of item-b
    assert store.checklist_item_current(checklist["id"], "item-a") is not None


def test_sync_all_content_change_adds_new_rev(tmp_path: Path, store: Store) -> None:
    d = _write_fixture(tmp_path, "cl-rev-bump")
    sync_all(store, src_dir=d)
    checklist = store.checklist_get("cl-rev-bump")
    assert checklist is not None

    (d / "fixture.yaml").write_text(
        """
name: cl-rev-bump
items:
  - name: item-a
    phase: setup
    severity: blocking
    decidability: judgment
    prevents: "a fails silently — CHANGED"
  - name: item-b
    severity: advisory
    prevents: "b fails silently"
""",
        encoding="utf-8",
    )
    result = sync_all(store, src_dir=d)
    [file_result] = result["files"]
    assert file_result["status"] == "synced"
    assert file_result["revved"] == 1
    item_a = store.checklist_item_current(checklist["id"], "item-a")
    assert item_a is not None
    assert item_a["rev"] == 2
    assert "CHANGED" in item_a["prevents"]


def test_sync_all_never_touches_local_items(tmp_path: Path, store: Store) -> None:
    d = _write_fixture(tmp_path, "cl-local-safe")
    sync_all(store, src_dir=d)
    checklist = store.checklist_get("cl-local-safe")
    assert checklist is not None
    store.checklist_item_add_rev(
        checklist_id=checklist["id"],
        name="local-only-item",
        rev=1,
        phase=None,
        severity="advisory",
        decidability="judgment",
        prevents="a board-specific concern",
        applies=None,
        body=None,
        origin="local",
    )

    # re-sync with item-b removed from the file — a local item with an
    # unrelated name must never be touched, and item-b (still 'shipped'
    # origin, absent from the file) must still be retired correctly.
    (d / "fixture.yaml").write_text(
        """
name: cl-local-safe
items:
  - name: item-a
    phase: setup
    severity: blocking
    decidability: judgment
    prevents: "a fails silently"
""",
        encoding="utf-8",
    )
    sync_all(store, src_dir=d, force=True)

    local_item = store.checklist_item_current(checklist["id"], "local-only-item")
    assert local_item is not None
    assert local_item["origin"] == "local"
    assert local_item["rev"] == 1


def test_sync_all_no_data_dir(tmp_path: Path, store: Store) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = sync_all(store, src_dir=empty)
    assert result["status"] == "no_data"


def test_sync_all_no_store() -> None:
    assert sync_all(None)["status"] == "no_store"


# ── drift check ──────────────────────────────────────────────────────


def test_check_drift_empty_after_sync(tmp_path: Path, store: Store) -> None:
    d = _write_fixture(tmp_path, "cl-drift-clean")
    sync_all(store, src_dir=d)
    assert check_drift(store, src_dir=d) == []


def test_check_drift_detects_hand_edited_row(tmp_path: Path, store: Store) -> None:
    d = _write_fixture(tmp_path, "cl-drift-dirty")
    sync_all(store, src_dir=d)
    checklist = store.checklist_get("cl-drift-dirty")
    assert checklist is not None
    with store.pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE checklist_items SET prevents = 'hand-edited in prod' "
                "WHERE checklist_id = %s AND name = 'item-a' AND retired_at IS NULL",
                (checklist["id"],),
            )
    findings = check_drift(store, src_dir=d)
    assert any(f["item"] == "item-a" for f in findings)


def test_check_drift_missing_checklist_dir_returns_empty(
    tmp_path: Path, store: Store
) -> None:
    missing = tmp_path / "does-not-exist"
    assert check_drift(store, src_dir=missing) == []
