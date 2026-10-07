"""Synthetic file/graph coexistence: fidelity, identity and refused lost updates."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from precis.cli import _build_parser
from precis.cli.memory import ImportRefused, _created_id
from precis.cli.memory_mirror import export_mirror, import_mirror
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store


def _topic(
    body: str = "**Why:** synthetic.\n**How to apply:** tests only.\n",
    *,
    name: str = "Alpha",
) -> bytes:
    return (
        f"---\nname: {name}\ndescription: Synthetic fixture\nmetadata:\n  type: project\n  extra: [one, two]\ncustom: π\n---\n"
        + body
    ).encode()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / "alpha.md").write_bytes(
        _topic(
            "See [[beta]] and [[missing]].\n**Why:** tests.\n**How to apply:** carefully.\n"
        )
    )
    (root / "beta.md").write_bytes(_topic("[Alpha](alpha.md)\n", name="Beta"))
    (root / "MEMORY.md").write_bytes(b"# Index\n\n- [Alpha](alpha.md) -- exact index\n")
    return root


def _dsn(store: Store) -> str:
    dsn = store.pool.conninfo
    assert isinstance(dsn, str)
    return dsn


def _snapshot(store: Store) -> list[Any]:
    # Fresh connection, including after a refused/aborted writer transaction.
    with psycopg.connect(_dsn(store)) as conn:
        return [
            conn.execute(sql).fetchall()
            for sql in (
                "SELECT ref_id,title,meta,retired_at,updated_at FROM refs ORDER BY ref_id",
                "SELECT chunk_id,ref_id,text FROM chunks ORDER BY chunk_id",
                "SELECT link_id,src_ref_id,dst_ref_id,relation,meta FROM links ORDER BY link_id",
                "SELECT * FROM ref_events ORDER BY event_id",
            )
        ]


def test_unchanged_ids_chunks_events_and_exact_roundtrip(
    store: Store, source: Path, tmp_path: Path
) -> None:
    first = import_mirror(store, source, namespace="fixture")
    assert first.created == 3
    assert first.unresolved == [("alpha.md", "missing.md")]
    before = _snapshot(store)
    again = import_mirror(store, source, namespace="fixture")
    assert again.refs == first.refs
    assert (again.created, again.updated, again.unchanged) == (0, 0, 3)
    assert _snapshot(store) == before
    dest = tmp_path / "export"
    assert export_mirror(store, dest, namespace="fixture").created == 3
    assert {p.name: p.read_bytes() for p in dest.iterdir()} == {
        p.name: p.read_bytes() for p in source.iterdir()
    }
    # A second namespace imports the same bytes without conflating identities.
    other = import_mirror(store, dest, namespace="other")
    assert set(other.refs.values()).isdisjoint(first.refs.values())


def test_changed_body_links_forward_resolution_and_missing_retained(
    store: Store, source: Path
) -> None:
    first = import_mirror(store, source, namespace="fixture")
    alpha, beta = first.refs["alpha.md"], first.refs["beta.md"]
    store.add_link(
        src_ref_id=alpha, dst_ref_id=first.refs["MEMORY.md"], relation="related-to"
    )
    (source / "alpha.md").write_bytes(_topic("Changed: [[missing]].\n"))
    (source / "missing.md").write_bytes(_topic("Later target.\n", name="Missing"))
    report = import_mirror(store, source, namespace="fixture")
    assert report.refs["alpha.md"] == alpha
    assert report.created == 1 and report.updated == 1
    assert not report.unresolved
    links = store.links_for(alpha, direction="out", relation="related-to")
    assert {e.dst_ref_id for e in links} == {
        first.refs["MEMORY.md"],
        report.refs["missing.md"],
    }
    assert all(e.dst_ref_id != beta for e in links)
    (source / "beta.md").unlink()
    report = import_mirror(store, source, namespace="fixture")
    assert report.missing == ["beta.md"]
    assert store.get_ref(kind="memory", id=beta) is not None


@pytest.mark.parametrize("change", ["body", "title", "hook", "frontmatter", "edge"])
def test_graph_divergence_refuses_entire_import(
    store: Store, hub: Hub, source: Path, change: str
) -> None:
    first = import_mirror(store, source, namespace="fixture")
    alpha = first.refs["alpha.md"]
    handler = MemoryHandler(hub=hub)
    if change == "body":
        handler.edit(id=alpha, mode="replace", text="Native graph edit survives.")
    elif change == "title":
        ref = store.get_ref(kind="memory", id=alpha)
        assert ref is not None
        handler.edit(
            id=alpha, mode="replace", text=handler._body_text(ref), title="Native title"
        )
    elif change == "hook":
        handler.edit(id=alpha, meta={"hook": "Native hook"})
    elif change == "frontmatter":
        store.update_ref(alpha, meta_patch={"frontmatter": {"changed": True}})
    else:
        with store.tx() as conn:
            conn.execute(
                "UPDATE links SET meta=meta || %s WHERE src_ref_id=%s",
                (Jsonb({"note": "Native annotation"}), alpha),
            )
    before = _snapshot(store)
    (source / "beta.md").write_bytes(_topic("Changed other file.\n", name="Beta"))
    (source / "new.md").write_bytes(_topic())
    with pytest.raises(ImportRefused, match="graph changed"):
        import_mirror(store, source, namespace="fixture")
    assert _snapshot(store) == before


@pytest.mark.parametrize("direction", ["in", "out"])
def test_body_replacement_preserves_chunk_anchored_links_by_refusing(
    store: Store, source: Path, direction: str
) -> None:
    first = import_mirror(store, source, namespace="fixture")
    alpha, beta = first.refs["alpha.md"], first.refs["beta.md"]
    if direction == "out":
        store.add_link(src_ref_id=alpha, src_pos=0, dst_ref_id=beta)
    else:
        store.add_link(src_ref_id=beta, dst_ref_id=alpha, dst_pos=0)
    before = _snapshot(store)
    (source / "alpha.md").write_bytes(_topic("Changed body must refuse."))
    with pytest.raises(ImportRefused, match="chunk-anchored links"):
        import_mirror(store, source, namespace="fixture")
    assert _snapshot(store) == before


def test_file_change_during_import_rolls_back_everything(
    store: Store, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.cli import memory_mirror as mirror

    original = mirror._read_files
    calls = 0
    before = _snapshot(store)

    def mutate(path: Path) -> dict[str, bytes]:
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / "alpha.md").write_bytes(_topic("Concurrent file edit."))
        return original(path)

    monkeypatch.setattr(mirror, "_read_files", mutate)
    with pytest.raises(ImportRefused, match="files changed"):
        import_mirror(store, source, namespace="fixture")
    assert _snapshot(store) == before


def test_waiting_import_sees_committed_graph_edit(
    store: Store, hub: Hub, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.cli import memory_mirror as mirror

    first = import_mirror(store, source, namespace="fixture")
    alpha = first.refs["alpha.md"]
    original = mirror._nodes
    reached = threading.Event()

    def reached_nodes(*args: Any, **kwargs: Any) -> Any:
        reached.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(mirror, "_nodes", reached_nodes)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.tx() as conn:
            conn.execute(
                "SELECT 1 FROM refs WHERE ref_id=%s FOR NO KEY UPDATE", (alpha,)
            )
            future = executor.submit(import_mirror, store, source, namespace="fixture")
            assert reached.wait(10)
            assert not future.done()
            # Competing transaction owns the same body-write lock as MemoryHandler.
            store.chunks.replace_body_chunk(
                alpha, "Winner prose.", chunk_kind="memory_body", conn=conn
            )
        with pytest.raises(ImportRefused, match="graph changed"):
            future.result(timeout=15)
    ref = store.get_ref(kind="memory", id=alpha)
    assert ref is not None
    assert MemoryHandler(hub=hub)._body_text(ref) == "Winner prose."


def test_parallel_import_does_not_duplicate(store: Store, source: Path) -> None:
    barrier = threading.Barrier(2)

    def run() -> Any:
        barrier.wait(timeout=10)
        return import_mirror(store, source, namespace="fixture")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        a, b = [f.result(timeout=20) for f in futures]
    assert a.refs == b.refs
    assert a.created + b.created == 3


def test_import_needs_only_one_connection(store: Store, source: Path) -> None:
    small = Store.connect(_dsn(store), min_size=1, max_size=1)
    try:
        small.pool.timeout = 1
        first = import_mirror(small, source, namespace="small-pool")
        assert first.created == 3
        assert import_mirror(small, source, namespace="small-pool").unchanged == 3
    finally:
        small.close()


@pytest.mark.parametrize(
    "bad", ["duplicate-yaml", "type", "nested", "symlink", "case", "unsafe-link"]
)
def test_invalid_inputs_do_not_write(
    store: Store, source: Path, tmp_path: Path, bad: str
) -> None:
    if bad == "duplicate-yaml":
        (source / "alpha.md").write_bytes(
            _topic().replace(b"name: Alpha", b"name: Alpha\nname: Other")
        )
    elif bad == "type":
        (source / "alpha.md").write_bytes(
            _topic().replace(b"type: project", b"type: invalid")
        )
    elif bad == "nested":
        (source / "nested").mkdir()
    elif bad == "symlink":
        (source / "escape.md").symlink_to(tmp_path / "absent")
    elif bad == "case":
        # Canonical tests run on Linux, including this case-sensitive pair.
        (source / "Alpha.md").write_bytes(_topic())
    else:
        (source / "alpha.md").write_bytes(_topic("[[../escape]]"))
    before = _snapshot(store)
    with pytest.raises(ImportRefused):
        import_mirror(store, source, namespace="fixture")
    assert _snapshot(store) == before


def test_identity_collision_and_retirement_refuse(store: Store, source: Path) -> None:
    first = import_mirror(store, source, namespace="fixture")
    ref = store.get_ref(kind="memory", id=first.refs["alpha.md"])
    assert ref is not None
    duplicate = store.insert_ref(
        kind="memory", slug=None, title="Collision", meta=ref.meta
    )
    before = _snapshot(store)
    with pytest.raises(ImportRefused, match="duplicate/retired"):
        import_mirror(store, source, namespace="fixture")
    assert _snapshot(store) == before
    with store.tx() as conn:
        conn.execute("DELETE FROM refs WHERE ref_id=%s", (duplicate.id,))
        conn.execute("UPDATE refs SET retired_at=now() WHERE ref_id=%s", (ref.id,))
    with pytest.raises(ImportRefused, match="duplicate/retired"):
        import_mirror(store, source, namespace="fixture")


def test_export_native_body_edit_and_metadata_refusal(
    store: Store, hub: Hub, source: Path, tmp_path: Path
) -> None:
    first = import_mirror(store, source, namespace="fixture")
    alpha = first.refs["alpha.md"]
    MemoryHandler(hub=hub).edit(id=alpha, mode="replace", text="Changed graph body.\n")
    dest = tmp_path / "out"
    export_mirror(store, dest, namespace="fixture")
    assert (dest / "alpha.md").read_bytes().endswith(b"---\nChanged graph body.\n")
    with pytest.raises(ImportRefused, match="destination exists"):
        export_mirror(store, dest, namespace="fixture")
    store.update_ref(alpha, meta_patch={"frontmatter": {"different": "metadata"}})
    with pytest.raises(ImportRefused, match="metadata differs"):
        export_mirror(store, tmp_path / "refused", namespace="fixture")
    assert not (tmp_path / "refused").exists()


def test_large_byte_faithful_roundtrip(store: Store, tmp_path: Path) -> None:
    source = tmp_path / "many"
    source.mkdir()
    index = "# Synthetic index\n" + "description π for test only.\n" * 600
    (source / "MEMORY.md").write_bytes(index.encode())
    assert len(index.encode()) >= 15000
    for i in range(120):
        raw = _topic(
            f"[[topic-{(i + 1) % 120}]]\n**Why:** π.\n**How to apply:** preserve.\n",
            name=f"Topic {i}",
        )
        if i % 2:
            raw = raw.replace(b"\n", b"\r\n")
        if i % 3:
            raw = raw.rstrip(b"\r\n")
        (source / f"topic-{i}.md").write_bytes(raw)
    first = import_mirror(store, source, namespace="large")
    assert first.created == 121 and not first.unresolved
    dest = tmp_path / "copy"
    assert export_mirror(store, dest, namespace="large").created == 121
    assert {p.name: p.read_bytes() for p in dest.iterdir()} == {
        p.name: p.read_bytes() for p in source.iterdir()
    }


def test_cli_dispatch_and_refusal(
    store: Store,
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from precis.cli.memory import run

    monkeypatch.setattr(Store, "connect", lambda _: store)
    monkeypatch.setattr(store, "close", lambda: None)
    for mode, path in [("import", source), ("export", tmp_path / "out")]:
        args = _build_parser().parse_args(
            [
                "memory",
                "mirror",
                mode,
                str(path),
                "--namespace",
                "cli",
                "--database-url",
                _dsn(store),
            ]
        )
        run(args)
    assert "exported 3 files" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="destination exists"):
        run(args)


def _live_title_ids(store: Store, title: str) -> list[int]:
    with psycopg.connect(_dsn(store)) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE kind='memory' AND title=%s "
                "AND retired_at IS NULL AND NOT (meta ? 'file_mirror') ORDER BY ref_id",
                (title,),
            ).fetchall()
        ]


def _plant_legacy(hub: Hub, title: str) -> int:
    resp = MemoryHandler(hub=hub).put(
        text="Legacy one-shot body.\n", title=title, tags=["SPACE:repo-dev"]
    )
    return _created_id(resp)


def _unplant(store: Store, *ids: int) -> None:
    """Retire planted nodes before the test ends. Per-test TRUNCATE runs at the
    *next* DB-fixture test's start, and the acceptance test takes no DB fixture,
    so a live planted title would otherwise leak into its import and trip the
    legacy refusal (CI shard 6, 2026-10-07)."""
    for rid in ids:
        store.retire_ref(rid)


def test_export_reports_unexported_native_and_other_namespace(
    store: Store, hub: Hub, source: Path, tmp_path: Path
) -> None:
    first = import_mirror(store, source, namespace="fixture")
    native = _plant_legacy(hub, "Native note")
    dest = tmp_path / "out"
    report = export_mirror(store, dest, namespace="fixture")
    assert report.unexported == [f"me{native}"]
    assert report.created == 3 and sorted(p.name for p in dest.iterdir()) == [
        "MEMORY.md",
        "alpha.md",
        "beta.md",
    ]
    assert import_mirror(store, source, namespace="fixture").unexported == []
    other = import_mirror(store, source, namespace="other")
    again = export_mirror(store, tmp_path / "out2", namespace="other")
    assert again.unexported == sorted(
        [f"me{native}", *(f"me{r}" for r in first.refs.values())],
        key=lambda h: int(h[2:]),
    )
    assert set(other.refs.values()).isdisjoint(first.refs.values())
    _unplant(store, native)


def test_legacy_refuse_retire_keep(
    store: Store, hub: Hub, source: Path, tmp_path: Path
) -> None:
    legacy = _plant_legacy(hub, "Alpha")  # same as alpha.md's name:
    before = _snapshot(store)
    with pytest.raises(ImportRefused, match=rf"me{legacy} 'Alpha'"):
        import_mirror(store, source, namespace="fixture")
    assert _snapshot(store) == before  # refused: nothing written
    with pytest.raises(ImportRefused, match="legacy must be"):
        import_mirror(store, source, namespace="fixture", legacy="bogus")

    report = import_mirror(store, source, namespace="fixture", legacy="retire")
    assert report.retired == [f"me{legacy}"] and report.created == 3
    with psycopg.connect(_dsn(store)) as conn:
        row = conn.execute(
            "SELECT retired_at FROM refs WHERE ref_id=%s", (legacy,)
        ).fetchone()
    assert row is not None and row[0] is not None  # soft-deleted, still present
    assert _live_title_ids(store, "Alpha") == []
    assert import_mirror(store, source, namespace="fixture").retired == []

    kept = _plant_legacy(hub, "Beta")
    report = import_mirror(store, source, namespace="second", legacy="keep")
    assert report.created == 3 and report.retired == []
    assert _live_title_ids(store, "Beta") == [kept]
    _unplant(store, kept)


def test_legacy_modes_leave_121_file_export_byte_identical(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    from tests.test_memory_mirror_acceptance import _fixture

    src = tmp_path / "synthetic"
    files, _meta, _edges = _fixture(src)
    title = "Synthetic project topic 002"
    legacy = _plant_legacy(hub, title)
    with pytest.raises(ImportRefused, match="legacy nodes"):
        import_mirror(store, src, namespace="a")
    kept: list[int] = []
    for ns, mode in (("b", "retire"), ("c", "keep")):
        if mode == "keep":
            kept.append(_plant_legacy(hub, title))
        report = import_mirror(store, src, namespace=ns, legacy=mode)
        assert report.created == 121
        assert report.retired == ([f"me{legacy}"] if mode == "retire" else [])
        dest = tmp_path / f"export-{ns}"
        export_mirror(store, dest, namespace=ns)
        assert {p.name: p.read_bytes() for p in dest.iterdir()} == files
    _unplant(store, *kept)


def test_cli_legacy_arg_and_unexported_stderr(
    store: Store,
    hub: Hub,
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from precis.cli.memory import run

    parse = _build_parser().parse_args
    base = ["memory", "mirror", "import", str(source), "--namespace", "n"]
    assert parse(base).legacy == "refuse"
    assert parse([*base, "--legacy", "retire"]).legacy == "retire"
    with pytest.raises(SystemExit):
        parse([*base, "--legacy", "bogus"])
    monkeypatch.setattr(Store, "connect", lambda _: store)
    monkeypatch.setattr(store, "close", lambda: None)
    legacy = _plant_legacy(hub, "Alpha")
    with pytest.raises(SystemExit, match="legacy nodes"):
        run(parse([*base, "--database-url", _dsn(store)]))
    run(parse([*base, "--legacy", "retire", "--database-url", _dsn(store)]))
    assert f"me{legacy}" in capsys.readouterr().out  # retired list in the JSON report
    native = _plant_legacy(hub, "Native note")
    out = tmp_path / "out"
    run(
        parse(
            ["memory", "mirror", "export", str(out), "--namespace", "n"]
            + ["--database-url", _dsn(store)]
        )
    )
    captured = capsys.readouterr()
    assert captured.out.strip() == "exported 3 files"
    assert f"me{native}" in captured.err
    _unplant(store, native)
