"""gr543776 job artifact store (store/job_artifacts.py) and the seed
scratch harvest (precis_pathway.seed_job._harvest_scratch /
_reap_scratch): a killed seed's finished measurements land as
``meta.artifacts`` entries before the scratch dir is reaped."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

import pytest

from precis.store import job_artifacts


class _FakeConn:
    """``conn.execute`` stand-in recording the meta-append params so the
    FS/spool paths run without a database."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, params))
        return None


def _root(path: Path) -> Path:
    """An accepted artifact root: exists AND sentinel-marked (a plain tmp
    dir is never a mount)."""
    path.mkdir()
    (path / job_artifacts.SENTINEL).touch()
    return path


def test_write_artifact_records_entry_and_round_trips(tmp_path: Path) -> None:
    conn = _FakeConn()
    root = _root(tmp_path / "nas")
    data = b"extxyz-bytes"

    entry = job_artifacts.write_artifact(conn, 42, "band-CO.extxyz", data, root=root)

    assert entry["name"] == "band-CO.extxyz"
    assert entry["bytes"] == len(data)
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert entry["path"] == "job-artifacts/42/band-CO.extxyz"
    assert "spooled" not in entry
    assert (root / entry["path"]).read_bytes() == data
    assert len(conn.calls) == 1 and "jsonb_set" in conn.calls[0][0]
    assert job_artifacts.read_artifact(entry, root=root) == data
    import stat as _stat

    assert _stat.S_IMODE((root / entry["path"]).stat().st_mode) == 0o644


def test_same_name_different_bytes_never_overwrites(tmp_path: Path) -> None:
    conn = _FakeConn()
    root = _root(tmp_path / "nas")
    e1 = job_artifacts.write_artifact(conn, 7, "b.extxyz", b"one", root=root)
    e2 = job_artifacts.write_artifact(conn, 7, "b.extxyz", b"two", root=root)
    e3 = job_artifacts.write_artifact(conn, 7, "b.extxyz", b"one", root=root)
    assert e1["path"] != e2["path"]
    assert e3["path"] == e1["path"]  # identical content dedupes
    assert job_artifacts.read_artifact(e2, root=root) == b"two"


def test_unwritable_root_spools_and_drain_moves(tmp_path: Path) -> None:
    conn = _FakeConn()
    missing_root = tmp_path / "not-mounted"
    spool = tmp_path / "spool"

    entry = job_artifacts.write_artifact(
        conn, 9, "ts-s1.extxyz", b"saddle", root=missing_root, spool=spool
    )
    assert entry["spooled"] is True
    assert not missing_root.exists()  # an absent mountpoint is never created
    assert (spool / entry["path"]).read_bytes() == b"saddle"
    # read resolves through the spool fallback
    assert (
        job_artifacts.read_artifact(entry, root=missing_root, spool=spool) == b"saddle"
    )

    missing_root.mkdir()  # the mount dir appears but is not sentinel-marked
    assert job_artifacts.drain_spool(9, root=missing_root, spool=spool) == 0
    (missing_root / job_artifacts.SENTINEL).touch()  # marked = usable root
    assert job_artifacts.drain_spool(9, root=missing_root, spool=spool) == 1
    assert (missing_root / entry["path"]).read_bytes() == b"saddle"
    assert not (spool / entry["path"]).exists()


def test_unsentined_root_spools_instead_of_shadowing(tmp_path: Path) -> None:
    """An empty unmounted mountpoint is a directory — but it is not the
    NAS. Without the sentinel the write must fall back to the spool."""
    conn = _FakeConn()
    bare = tmp_path / "mountpoint"
    bare.mkdir()  # exists, no sentinel, not a mount
    spool = tmp_path / "spool"
    entry = job_artifacts.write_artifact(
        conn, 11, "b.extxyz", b"x", root=bare, spool=spool
    )
    assert entry["spooled"] is True
    assert not (bare / "job-artifacts").exists()
    assert (spool / entry["path"]).is_file()


def test_drain_conflict_keeps_spool_and_read_still_resolves(tmp_path: Path) -> None:
    conn = _FakeConn()
    root = tmp_path / "nas"
    spool = tmp_path / "spool"
    entry = job_artifacts.write_artifact(
        conn, 12, "c.extxyz", b"spooled-bytes", root=root, spool=spool
    )
    assert entry["spooled"] is True
    _root(root)
    # a DIFFERENT artifact already occupies the recorded path on the root
    (root / entry["path"]).parent.mkdir(parents=True, exist_ok=True)
    (root / entry["path"]).write_bytes(b"other-bytes")
    assert job_artifacts.drain_spool(12, root=root, spool=spool) == 0
    # the spool file stayed, and the recorded path resolves to ITS bytes
    assert (spool / entry["path"]).is_file()
    assert (
        job_artifacts.read_artifact(entry, root=root, spool=spool) == b"spooled-bytes"
    )


def test_read_artifact_verifies_sha256(tmp_path: Path) -> None:
    conn = _FakeConn()
    root = _root(tmp_path / "nas")
    entry = job_artifacts.write_artifact(conn, 3, "b.extxyz", b"data", root=root)
    (root / entry["path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        job_artifacts.read_artifact(entry, root=root)


# ── DB-backed: the meta append and the scratch harvest ──────────────────


def _mk_job(store: Any) -> int:
    from precis.store.types import Tag

    ref = store.insert_ref(kind="job", slug=None, title="artifact test", meta={})
    store.add_tag(ref.id, Tag.parse_strict("STATUS:running"), set_by="agent")
    return int(ref.id)


def _artifacts(store: Any, ref_id: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'artifacts' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return list(row[0] or []) if row else []


def test_write_artifact_appends_meta_artifacts(store: Any, tmp_path: Path) -> None:
    ref_id = _mk_job(store)
    root = _root(tmp_path / "nas")
    with store.pool.connection() as conn:
        e1 = job_artifacts.write_artifact(conn, ref_id, "a.extxyz", b"a", root=root)
        e2 = job_artifacts.write_artifact(conn, ref_id, "b.json", b"{}", root=root)
        conn.commit()
    got = _artifacts(store, ref_id)
    assert [a["name"] for a in got] == ["a.extxyz", "b.json"]
    assert got[0]["sha256"] == e1["sha256"] and got[1]["path"] == e2["path"]


def test_meta_artifacts_dedupes_by_name_and_sha(store: Any, tmp_path: Path) -> None:
    """A retried write of the same payload must not append a duplicate
    entry; a same-name write with different bytes records its own."""
    ref_id = _mk_job(store)
    root = _root(tmp_path / "nas")
    with store.pool.connection() as conn:
        job_artifacts.write_artifact(conn, ref_id, "a.extxyz", b"a", root=root)
        job_artifacts.write_artifact(conn, ref_id, "a.extxyz", b"a", root=root)
        job_artifacts.write_artifact(conn, ref_id, "a.extxyz", b"other", root=root)
        conn.commit()
    got = _artifacts(store, ref_id)
    assert [a["name"] for a in got] == ["a.extxyz", "a.extxyz"]
    assert len({a["sha256"] for a in got}) == 2


def test_reap_scratch_harvests_failed_seed_checkpoints(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed seed's checkpoints are landed as artifacts BEFORE the
    scratch dir is deleted — the failed-seed half of gr543776."""
    from precis.workers.executors import _common, claude_inproc
    from precis_pathway import seed_job

    ref_id = _mk_job(store)
    meta = {"executor": "ssh_node", "params": {}}
    ctx = claude_inproc._build_dispatch_context(store, ref_id, "t", meta)
    _common.set_status(store, ref_id, "failed")
    ctx = claude_inproc._build_dispatch_context(store, ref_id, "t", meta)

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    scratch = temp_root / "autocatpath-seed-x1"
    ckpt = scratch / "checkpoints"
    ckpt.mkdir(parents=True)
    (ckpt / "band-s1.extxyz").write_bytes(b"band")
    (ckpt / "relax-R.extxyz").write_bytes(b"relax")
    (ckpt / "band-s1.extxyz.tmp").write_bytes(b"torn")  # never harvested
    root = _root(tmp_path / "nas")
    monkeypatch.setenv("PRECIS_JOB_ARTIFACT_ROOT", str(root))

    seed_job._reap_scratch(ctx, {"dir": str(scratch)}, failed=True)

    names = {a["name"] for a in _artifacts(store, ref_id)}
    assert names == {"band-s1.extxyz", "relax-R.extxyz"}
    assert (root / f"job-artifacts/{ref_id}/band-s1.extxyz").is_file()
    assert not scratch.exists()  # reaped after the harvest


def test_reap_scratch_keeps_dir_when_harvest_fails(
    store: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A checkpoint file that cannot be landed must keep the scratch dir
    — deleting it would discard a measurement's only copy."""
    from precis.store import job_artifacts as ja
    from precis.workers.executors import _common, claude_inproc
    from precis_pathway import seed_job

    ref_id = _mk_job(store)
    meta = {"executor": "ssh_node", "params": {}}
    _common.set_status(store, ref_id, "failed")
    ctx = claude_inproc._build_dispatch_context(store, ref_id, "t", meta)

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    scratch = temp_root / "autocatpath-seed-x2"
    ckpt = scratch / "checkpoints"
    ckpt.mkdir(parents=True)
    (ckpt / "band-s1.extxyz").write_bytes(b"band")
    root = _root(tmp_path / "nas")
    monkeypatch.setenv("PRECIS_JOB_ARTIFACT_ROOT", str(root))

    real_write = ja.write_artifact

    def _boom(conn: Any, rid: int, name: str, data: bytes, **kw: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(ja, "write_artifact", _boom)

    seed_job._reap_scratch(ctx, {"dir": str(scratch)}, failed=True)

    assert scratch.exists()  # kept, not deleted
    assert "not" in caplog.text and "harvested" in caplog.text
    assert not _artifacts(store, ref_id)

    monkeypatch.setattr(ja, "write_artifact", real_write)
    seed_job._reap_scratch(ctx, {"dir": str(scratch)}, failed=True)
    assert not scratch.exists()  # a clean re-reap finishes the job
    assert {a["name"] for a in _artifacts(store, ref_id)} == {"band-s1.extxyz"}


def test_harvest_skips_names_already_recorded(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The succeeded path records the envelope's artifacts; the reap's
    disk harvest must not append duplicates for the same names."""
    from precis.workers.executors import claude_inproc
    from precis_pathway import seed_job

    ref_id = _mk_job(store)
    root = _root(tmp_path / "nas")
    monkeypatch.setenv("PRECIS_JOB_ARTIFACT_ROOT", str(root))
    with store.pool.connection() as conn:
        job_artifacts.write_artifact(conn, ref_id, "band-s1.extxyz", b"band", root=root)
        conn.commit()

    ckpt = tmp_path / "autocatpath-seed-y" / "checkpoints"
    ckpt.mkdir(parents=True)
    (ckpt / "band-s1.extxyz").write_bytes(b"band")
    (ckpt / "ts-s1.extxyz").write_bytes(b"ts")

    ctx = claude_inproc._build_dispatch_context(store, ref_id, "t", {})
    n, missed = seed_job._harvest_scratch(ctx, {"dir": str(ckpt.parent)})

    assert n == 1 and missed == []
    names = [a["name"] for a in _artifacts(store, ref_id)]
    assert names == ["band-s1.extxyz", "ts-s1.extxyz"]


def test_record_artifacts_never_fails_the_job(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed artifact write warns and continues — the run's result
    already landed, so retention must never fail the job (gr543776)."""
    from precis.store import job_artifacts as ja
    from precis.workers.executors import claude_inproc
    from precis_pathway import seed_job

    ref_id = _mk_job(store)
    root = _root(tmp_path / "nas")
    monkeypatch.setenv("PRECIS_JOB_ARTIFACT_ROOT", str(root))
    monkeypatch.setenv("PRECIS_JOB_ARTIFACT_SPOOL", str(tmp_path / "spool"))
    ctx = claude_inproc._build_dispatch_context(store, ref_id, "t", {})

    real_write = ja.write_artifact

    def _flaky(conn: Any, rid: int, name: str, data: bytes, **kw: Any) -> Any:
        if name == "bad.extxyz":
            raise OSError("disk full")
        return real_write(conn, rid, name, data, **kw)

    monkeypatch.setattr(ja, "write_artifact", _flaky)

    n = seed_job._record_artifacts(
        ctx, {"artifacts": {"bad.extxyz": "x", "good.json": "{}"}}
    )

    assert n == 1
    assert [a["name"] for a in _artifacts(store, ref_id)] == ["good.json"]
