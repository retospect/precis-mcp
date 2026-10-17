"""``precis conventions import``: refs, tags, edges, idempotence, dry-run, sync."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from precis.cli import _build_parser
from precis.cli.conventions import ManifestError, import_conventions
from precis.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "conventions" / "rules.yaml"


def _snapshot(store: Store) -> dict[str, list[Any]]:
    with store.tx() as conn:
        return {
            t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
            for t in ("refs", "chunks", "links", "ref_tags")
        }


def _importer_refs(store: Store) -> list[tuple[int, dict[str, Any], Any]]:
    with store.tx() as conn:
        return [
            (int(r), m, ret)
            for r, m, ret in conn.execute(
                "SELECT ref_id, meta, retired_at FROM refs "
                "WHERE meta->>'source'='conventions-import' ORDER BY ref_id"
            ).fetchall()
        ]


def _edges(store: Store) -> list[tuple[int, int]]:
    with store.tx() as conn:
        return [
            (int(s), int(d))
            for s, d in conn.execute(
                "SELECT src_ref_id, dst_ref_id FROM links WHERE relation='tests' "
                "AND meta->>'source'='conventions-import' ORDER BY link_id"
            ).fetchall()
        ]


def _tags(store: Store, ref_id: int) -> set[str]:
    with store.tx() as conn:
        return {
            f"{ns}:{v}" if ns not in ("OPEN", "FLAG") else v
            for ns, v in conn.execute(
                "SELECT t.namespace, t.value FROM ref_tags rt JOIN tags t "
                "ON t.tag_id=rt.tag_id WHERE rt.ref_id=%s",
                (ref_id,),
            ).fetchall()
        }


def _by_key(store: Store) -> dict[str, int]:
    out: dict[str, int] = {}
    for rid, meta, _ in _importer_refs(store):
        out[meta.get("convention_rule") or meta["convention_enforcer"]] = rid
    return out


def _body(store: Store, ref_id: int) -> str:
    with store.tx() as conn:
        rows = conn.execute(
            "SELECT ord, chunk_kind, text FROM chunks WHERE ref_id=%s", (ref_id,)
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(0, "finding_body")]
    return str(rows[0][2])


def _title(store: Store, ref_id: int) -> str:
    with store.tx() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id=%s", (ref_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_creates_refs_tags_edges(store: Store) -> None:
    rep = import_conventions(store, FIXTURE)
    assert (rep.created, rep.edges_added, rep.unchanged) == (5, 3, 0)
    ids = _by_key(store)
    assert set(ids) == {
        "invariants/forward-only-migrations",
        "invariants/body-chunks-append-only",
        "invariants/no-enforcer",
        "tests/test_skill_size.py",
        "ruff:PLW1514",
    }
    rule = ids["invariants/forward-only-migrations"]
    meta = next(m for r, m, _ in _importer_refs(store) if r == rule)
    assert meta["convention_file"] == "docs/conventions/invariants.md"
    assert meta["convention_section"] == "Forward-only migrations"
    assert _tags(store, rule) >= {"SPACE:repo-dev", "convention"}
    enf = ids["tests/test_skill_size.py"]
    assert _tags(store, enf) >= {"SPACE:repo-dev", "enforcer"}
    assert _body(store, rule).endswith(
        "\n\nSource: docs/conventions/invariants.md § Forward-only migrations"
    )
    assert "size gate clause one" in _body(store, enf)
    assert "size gate clause two" in _body(store, enf)
    # direction: enforcer -> rule
    assert sorted(_edges(store)) == sorted(
        [
            (enf, rule),
            (enf, ids["invariants/body-chunks-append-only"]),
            (ids["ruff:PLW1514"], rule),
        ]
    )
    assert f"edge fi{enf} -tests-> fi{rule}" in rep.lines
    assert f"created fi{rule} invariants/forward-only-migrations" in rep.lines


def test_second_run_creates_nothing(store: Store) -> None:
    import_conventions(store, FIXTURE)
    before = _snapshot(store)
    rep = import_conventions(store, FIXTURE)
    assert (rep.created, rep.edges_added, rep.updated, rep.unchanged) == (0, 0, 0, 5)
    assert _snapshot(store) == before


def test_dry_run_writes_nothing(store: Store) -> None:
    before = _snapshot(store)
    rep = import_conventions(store, FIXTURE, dry_run=True)
    assert (rep.created, rep.edges_added) == (5, 3)
    assert _snapshot(store) == before
    # dry-run sync over an existing import also writes nothing
    import_conventions(store, FIXTURE)
    before = _snapshot(store)
    import_conventions(store, FIXTURE, dry_run=True, sync=True)
    assert _snapshot(store) == before


def _edited(tmp_path: Path) -> Path:
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    data[0]["title"] = "New title."
    data[0]["body"] = "New body."
    data[0]["enforcers"] = [data[0]["enforcers"][0]]  # drops the ruff edge
    del data[2]  # absent from the manifest -> orphaned
    out = tmp_path / "rules.yaml"
    out.write_text(yaml.safe_dump(data), encoding="utf-8")
    return out


def test_default_mode_leaves_existing_refs_alone(store: Store, tmp_path: Path) -> None:
    import_conventions(store, FIXTURE)
    before = _snapshot(store)
    rep = import_conventions(store, _edited(tmp_path))
    assert (rep.updated, rep.edges_removed, rep.created) == (0, 0, 0)
    assert len(rep.orphaned) == 2
    assert _snapshot(store) == before


def test_sync_rewrites_removes_edge_never_retires(store: Store, tmp_path: Path) -> None:
    import_conventions(store, FIXTURE)
    ids = _by_key(store)
    rule = ids["invariants/forward-only-migrations"]
    rep = import_conventions(store, _edited(tmp_path), sync=True)
    assert (rep.updated, rep.edges_removed, rep.created, rep.edges_added) == (
        1,
        1,
        0,
        0,
    )
    assert _title(store, rule) == "New title."
    assert _body(store, rule).startswith("New body.\n\nSource:")
    assert (ids["ruff:PLW1514"], rule) not in _edges(store)
    assert len(_edges(store)) == 2
    # orphans (rule + the now-unreferenced ruff enforcer) are listed, not retired
    assert sorted(o.split(" ", 1)[1] for o in rep.orphaned) == [
        "invariants/no-enforcer",
        "ruff:PLW1514",
    ]
    assert all(ret is None for _, _, ret in _importer_refs(store))
    assert len(_importer_refs(store)) == 5


def test_manifest_validation(store: Store, tmp_path: Path) -> None:
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    before = _snapshot(store)
    bad: dict[str, list[dict[str, Any]]] = {
        "dup": [data[0], data[0]],
        "missing-key": [{"id": "x", "file": "docs/conventions/invariants.md"}],
        "enforcer-path": [
            {**data[2], "enforcers": [{"path": "tests/nope.py", "what": "w"}]}
        ],
    }
    for name, entries in bad.items():
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(entries), encoding="utf-8")
        with pytest.raises(ManifestError):
            import_conventions(store, path)
    assert _snapshot(store) == before


def test_cli_registered() -> None:
    args = _build_parser().parse_args(["conventions", "import", "--dry-run", "--sync"])
    assert (args.dry_run, args.sync, args.manifest) == (True, True, None)


def test_retired_ref_not_reminted_and_duplicates_untouched(store: Store) -> None:
    import_conventions(store, FIXTURE)
    ids = _by_key(store)
    rule = ids["invariants/forward-only-migrations"]
    store.retire_ref(rule)
    edges_before = _edges(store)
    rep = import_conventions(store, FIXTURE, sync=True)
    assert (rep.created, rep.edges_added, rep.edges_removed) == (0, 0, 0)
    assert rep.skipped_retired == 1
    assert f"skipped-retired fi{rule} invariants/forward-only-migrations" in rep.lines
    assert len(_importer_refs(store)) == 5
    assert _edges(store) == edges_before

    # two live duplicates of one key: reported, untouched, never retired
    other = ids["invariants/no-enforcer"]
    with store.tx() as conn:
        dup = conn.execute(
            "INSERT INTO refs (kind, title, meta) SELECT kind, title, meta "
            "FROM refs WHERE ref_id=%s RETURNING ref_id",
            (other,),
        ).fetchone()
    assert dup is not None
    before = _snapshot(store)
    rep = import_conventions(store, FIXTURE, sync=True)
    assert rep.duplicates == 1
    assert f"duplicate invariants/no-enforcer: fi{other}, fi{dup[0]}" in rep.lines
    assert rep.created == 0
    assert _snapshot(store) == before
