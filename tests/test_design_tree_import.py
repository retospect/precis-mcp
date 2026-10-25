"""``precis conventions import --manifest design-tree``: finding refs,
tags, the SPEC edge set, and the shared importer guarantees — on the dev
``store`` fixture only, never prod."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from precis.cli import _build_parser
from precis.cli.conventions import ManifestError, import_conventions
from precis.store import Store

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "design_tree"
RULES_FIXTURE = Path(__file__).parent / "fixtures" / "conventions" / "rules.yaml"
TAPPED = "join/service/threaded/clearance-tapped"


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


def _by_key(store: Store) -> dict[str, int]:
    return {
        m["dtree"]["id"]: int(r) for r, m, _ in _importer_refs(store) if "dtree" in m
    }


def _edges(store: Store) -> list[tuple[int, int, str, dict[str, Any]]]:
    with store.tx() as conn:
        return [
            (int(s), int(d), str(rel), meta or {})
            for s, d, rel, meta in conn.execute(
                "SELECT src_ref_id, dst_ref_id, relation, meta FROM links "
                "WHERE meta->>'source'='conventions-import' ORDER BY link_id"
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


def _body(store: Store, ref_id: int) -> str:
    with store.tx() as conn:
        rows = conn.execute(
            "SELECT ord, chunk_kind, text FROM chunks WHERE ref_id=%s", (ref_id,)
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(0, "finding_body")]
    return str(rows[0][2])


def test_creates_finding_refs_tags_edges(store: Store) -> None:
    rep = import_conventions(store, FIXTURE_DIR)
    assert (rep.created, rep.edges_added) == (31, 34)
    ids = _by_key(store)
    assert len(ids) == 31
    node = ids[TAPPED]
    meta = next(m for r, m, _ in _importer_refs(store) if r == node)
    assert meta["dtree"] == {
        "id": TAPPED,
        "role": "leaf",
        "domain": "mech",
        "scale": "macro",
        "coverage": "deep",
    }
    assert _tags(store, node) >= {
        "SPACE:design-conventions",
        "dt:leaf",
        "domain:mech",
    }
    rule = ids[f"{TAPPED}#c1"]
    rule_meta = next(m for r, m, _ in _importer_refs(store) if r == rule)
    assert rule_meta["dtree"]["role"] == "rule"
    assert _tags(store, rule) >= {"SPACE:design-conventions", "dt:rule"}
    assert "thread engagement" in _body(store, rule)
    assert _body(store, node).endswith(f"Source: {TAPPED} (join.yaml)")

    edge_triples = {(s, d, rel) for s, d, rel, _ in _edges(store)}
    assert (node, ids["join/service/threaded"], "specialises") in edge_triples
    assert (node, ids["join/frequent"], "specialises") in edge_triples
    assert (ids[f"{TAPPED}#i1"], node, "qualifies") in edge_triples
    assert (ids[f"{TAPPED}#f1"], ids[f"{TAPPED}#i1"], "fixes") in edge_triples
    assert (rule, node, "part-of") in edge_triples
    assert (
        ids["enf:src/precis_se/fasten.py::grip"],
        rule,
        "tests",
    ) in edge_triples
    src_url = ids["src:https://example.invalid/machinery-handbook"]
    assert (rule, src_url, "cites") in edge_triples
    assert (node, src_url, "cites") in edge_triples
    as_meta = {
        (s, d): m.get("as") for s, d, rel, m in _edges(store) if rel == "related-to"
    }
    assert as_meta[(node, ids["axis/locking"])] == "axis"
    assert as_meta[(node, ids["failure/galvanic-corrosion"])] == "mode"
    assert as_meta[(node, ids["principle/retention-all-dof"])] == "principle"
    assert as_meta[(node, ids["process/desktop-fdm"])] == "process"
    assert as_meta[(node, ids["system/t-slot-20"])] == "system"
    assert as_meta[(node, ids["join/service/pinned"])] is None
    assert (ids["join/frequent"], ids["close"], "related-to") in edge_triples


def test_second_run_creates_nothing(store: Store) -> None:
    import_conventions(store, FIXTURE_DIR)
    before = _snapshot(store)
    rep = import_conventions(store, FIXTURE_DIR)
    assert (rep.created, rep.edges_added, rep.updated, rep.unchanged) == (
        0,
        0,
        0,
        31,
    )
    assert _snapshot(store) == before


def test_dry_run_writes_nothing(store: Store) -> None:
    before = _snapshot(store)
    rep = import_conventions(store, FIXTURE_DIR, dry_run=True)
    assert (rep.created, rep.edges_added) == (31, 34)
    assert _snapshot(store) == before


def _edited(tmp_path: Path) -> Path:
    """A variant fixture: one node's summary changed, one link dropped,
    one registry node removed (-> orphaned)."""
    out = tmp_path / "design_tree"
    out.mkdir()
    for src in FIXTURE_DIR.glob("*.yaml"):
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
        if src.name == "join.yaml":
            tapped = data["join"]["children"]["service"]["children"]["threaded"][
                "children"
            ]["clearance-tapped"]
            tapped["summary"] = "rewritten summary"
            tapped["links"].remove("system/t-slot-20")
        if src.name == "library.yaml":
            del data["criterion/separable"]
        (out / src.name).write_text(yaml.safe_dump(data), encoding="utf-8")
    return out


def test_sync_rewrites_removes_edge_never_retires(store: Store, tmp_path: Path) -> None:
    import_conventions(store, FIXTURE_DIR)
    ids = _by_key(store)
    node = ids[TAPPED]
    rep = import_conventions(store, _edited(tmp_path), sync=True)
    assert rep.updated == 1
    assert rep.edges_removed == 1
    assert rep.created == 0
    assert rep.edges_added == 0
    assert "rewritten summary" in _body(store, node)
    assert (node, ids["system/t-slot-20"], "related-to") not in {
        (s, d, rel) for s, d, rel, _ in _edges(store)
    }
    assert [o.split(" ", 1)[1] for o in rep.orphaned] == ["criterion/separable"]
    assert all(ret is None for _, _, ret in _importer_refs(store))
    assert len(_importer_refs(store)) == 31


def test_sync_converges_meta_tags_and_edge_meta(store: Store, tmp_path: Path) -> None:
    """--sync converges meta.dtree, the dt: tag and link meta {as}:
    join/frequent moves method -> leaf, and axis/locking's role change
    drops the 'as' meta on the related-to edge into it."""
    import_conventions(store, FIXTURE_DIR)
    ids = _by_key(store)
    frequent = ids["join/frequent"]

    out = tmp_path / "design_tree"
    out.mkdir()
    for src in FIXTURE_DIR.glob("*.yaml"):
        data = yaml.safe_load(src.read_text(encoding="utf-8"))
        if src.name == "join.yaml":
            data["join"]["children"]["frequent"]["role"] = "leaf"
        if src.name == "library.yaml":
            data["axis/locking"]["role"] = "leaf"
        (out / src.name).write_text(yaml.safe_dump(data), encoding="utf-8")

    rep = import_conventions(store, out, sync=True)
    meta = next(m for r, m, _ in _importer_refs(store) if r == frequent)
    assert meta["dtree"]["role"] == "leaf"
    tags = _tags(store, frequent)
    assert "dt:leaf" in tags
    assert "dt:method" not in tags
    # 'as' meta follows the target's new role (leaf maps to none)
    as_meta = {
        (s, d): m.get("as") for s, d, rel, m in _edges(store) if rel == "related-to"
    }
    assert as_meta[(ids[TAPPED], ids["axis/locking"])] is None
    assert any(
        line.startswith(f"edge-meta me{ids[TAPPED]} -related-to->")
        for line in rep.lines
    )
    # the edge still exists, just re-minted with empty meta
    assert (ids[TAPPED], ids["axis/locking"], "related-to") in {
        (s, d, rel) for s, d, rel, _ in _edges(store)
    }


def test_retired_ref_not_reminted(store: Store) -> None:
    import_conventions(store, FIXTURE_DIR)
    ids = _by_key(store)
    edges_before = _edges(store)
    store.retire_ref(ids[TAPPED])
    rep = import_conventions(store, FIXTURE_DIR, sync=True)
    assert rep.skipped_retired == 1
    assert (rep.created, rep.edges_added, rep.edges_removed) == (0, 0, 0)
    assert f"skipped-retired me{ids[TAPPED]} {TAPPED}" in rep.lines
    # existing edges to/from the retired ref are left alone, none added
    assert _edges(store) == edges_before


def test_lane_mismatch_refused(store: Store) -> None:
    with pytest.raises(ManifestError, match="design-conventions"):
        import_conventions(store, FIXTURE_DIR, kind="memory")
    with pytest.raises(ManifestError, match="repo-dev"):
        import_conventions(
            store, RULES_FIXTURE, kind="finding", space="design-conventions"
        )


def test_missing_packaged_dir(store: Store, tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="no such manifest"):
        import_conventions(store, tmp_path / "empty")
    empty = tmp_path / "dtree"
    empty.mkdir()
    with pytest.raises(ManifestError, match="no \\*.yaml"):
        import_conventions(store, empty)
    # the packaged literal resolves to data/design-tree, absent until S1
    with pytest.raises(ManifestError, match="no \\*.yaml"):
        import_conventions(store, "design-tree")


def test_cli_registered() -> None:
    args = _build_parser().parse_args(
        [
            "conventions",
            "import",
            "--manifest",
            "design-tree",
            "--space",
            "design-conventions",
            "--kind",
            "finding",
            "--dry-run",
        ]
    )
    assert (args.manifest, args.space, args.kind, args.dry_run) == (
        "design-tree",
        "design-conventions",
        "finding",
        True,
    )


def test_dtree_import_does_not_touch_repo_dev_lane(store: Store) -> None:
    """Both lanes in one DB: a rules.yaml --sync must not reap the dtree
    lane's importer edges (they share meta.source)."""
    import_conventions(store, FIXTURE_DIR)
    dtree_edges = {(s, d, rel) for s, d, rel, _ in _edges(store)}
    import_conventions(store, RULES_FIXTURE, sync=True)
    after = {(s, d, rel) for s, d, rel, _ in _edges(store)}
    assert dtree_edges <= after
