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


def test_write_artifact_records_entry_and_round_trips(tmp_path: Path) -> None:
    conn = _FakeConn()
    root = tmp_path / "nas"
    root.mkdir()
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


def test_same_name_different_bytes_never_overwrites(tmp_path: Path) -> None:
    conn = _FakeConn()
    root = tmp_path / "nas"
    root.mkdir()
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

    missing_root.mkdir()  # the mount comes up
    assert job_artifacts.drain_spool(9, root=missing_root, spool=spool) == 1
    assert (missing_root / entry["path"]).read_bytes() == b"saddle"
    assert not (spool / entry["path"]).exists()


def test_read_artifact_verifies_sha256(tmp_path: Path) -> None:
    conn = _FakeConn()
    root = tmp_path / "nas"
    root.mkdir()
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
    root = tmp_path / "nas"
    root.mkdir()
    with store.pool.connection() as conn:
        e1 = job_artifacts.write_artifact(conn, ref_id, "a.extxyz", b"a", root=root)
        e2 = job_artifacts.write_artifact(conn, ref_id, "b.json", b"{}", root=root)
        conn.commit()
    got = _artifacts(store, ref_id)
    assert [a["name"] for a in got] == ["a.extxyz", "b.json"]
    assert got[0]["sha256"] == e1["sha256"] and got[1]["path"] == e2["path"]


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
    root = tmp_path / "nas"
    root.mkdir()
    monkeypatch.setenv("PRECIS_JOB_ARTIFACT_ROOT", str(root))

    seed_job._reap_scratch(ctx, {"dir": str(scratch)}, failed=True)

    names = {a["name"] for a in _artifacts(store, ref_id)}
    assert names == {"band-s1.extxyz", "relax-R.extxyz"}
    assert (root / f"job-artifacts/{ref_id}/band-s1.extxyz").is_file()
    assert not scratch.exists()  # reaped after the harvest


def test_harvest_skips_names_already_recorded(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The succeeded path records the envelope's artifacts; the reap's
    disk harvest must not append duplicates for the same names."""
    from precis.workers.executors import claude_inproc
    from precis_pathway import seed_job

    ref_id = _mk_job(store)
    root = tmp_path / "nas"
    root.mkdir()
    monkeypatch.setenv("PRECIS_JOB_ARTIFACT_ROOT", str(root))
    with store.pool.connection() as conn:
        job_artifacts.write_artifact(conn, ref_id, "band-s1.extxyz", b"band", root=root)
        conn.commit()

    ckpt = tmp_path / "autocatpath-seed-y" / "checkpoints"
    ckpt.mkdir(parents=True)
    (ckpt / "band-s1.extxyz").write_bytes(b"band")
    (ckpt / "ts-s1.extxyz").write_bytes(b"ts")

    ctx = claude_inproc._build_dispatch_context(store, ref_id, "t", {})
    n = seed_job._harvest_scratch(ctx, {"dir": str(ckpt.parent)})

    assert n == 1
    names = [a["name"] for a in _artifacts(store, ref_id)]
    assert names == ["band-s1.extxyz", "ts-s1.extxyz"]
