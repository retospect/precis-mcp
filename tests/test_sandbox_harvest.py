"""sandbox_run harvest (design §"Harvest → DB + NAS") — the ``/work/out``
→ folder + content-addressed tarball projection.

Covers the buildable substrate against a real (test) Postgres, tmp_path
roots for both ``PRECIS_ROOT`` (the plaintext projection) and
``PRECIS_SANDBOX_ARTIFACT_ROOT`` (the tarball store) — no live host, no
podman needed (this module never shells out to a container runtime).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis.workers.executors import _sandbox_harvest as harvest

pytestmark = pytest.mark.db


# ── helpers ────────────────────────────────────────────────────────


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _parent_id(store: Store, ref_id: int) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT parent_id FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def _mk_job(store: Store, *, parent_id: int | None = None) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="sandbox_run job",
        meta={"executor": "claude_docker", "job_type": "sandbox_run"},
        parent_id=parent_id,
    )
    return int(ref.id)


# ── content-addressed tarball ────────────────────────────────────


class TestArtifactStore:
    def test_artifact_root_default_mirrors_corpus_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PRECIS_SANDBOX_ARTIFACT_ROOT", raising=False)
        assert harvest.artifact_root() == Path.home() / "work"

    def test_artifact_root_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRECIS_SANDBOX_ARTIFACT_ROOT", "/mnt/nas")
        assert harvest.artifact_root() == Path("/mnt/nas")

    def test_artifact_key_shape(self) -> None:
        assert harvest.artifact_key("abc123") == "sandbox-artifacts/abc123.tar.gz"

    def test_build_tarball_round_trips_and_verifies(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "main.py").write_text("print('hi')\n")
        (out_dir / "nested").mkdir()
        (out_dir / "nested" / "data.txt").write_text("hello\n")

        art_root = tmp_path / "artifacts"
        artifact = harvest.build_tarball(out_dir, root=art_root)

        assert set(artifact) == {"sha256", "size", "key"}
        assert artifact["key"] == harvest.artifact_key(artifact["sha256"])
        assert artifact["size"] > 0
        dest = art_root / artifact["key"]
        assert dest.is_file()
        assert dest.stat().st_size == artifact["size"]

        assert harvest.verify_artifact(artifact["sha256"], root=art_root) is True
        assert harvest.verify_artifact("0" * 64, root=art_root) is False

    def test_verify_artifact_missing_file(self, tmp_path: Path) -> None:
        assert harvest.verify_artifact("deadbeef", root=tmp_path) is False

    def test_symlinked_out_of_tree_file_not_dereferenced_in_tarball(
        self, tmp_path: Path
    ) -> None:
        # Finding 1's "tarball extraction side untouched": tarfile's
        # default (``dereference=False``) already stores a symlink as a
        # SYMTYPE entry (the link target PATH only, never its content),
        # and the "data" extraction filter (``stage_run_artifact``'s
        # ``filter="data"``) refuses to extract a symlink pointing outside
        # the destination — so this side of the harvest was already safe,
        # unaffected by the ``_iter_out_files``/``project_out`` fix.
        import tarfile

        secret = tmp_path / "outside" / "secret.txt"
        secret.parent.mkdir(parents=True)
        secret.write_text("TOP SECRET HOST CONTENT\n")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "main.py").write_text("print('hi')\n")
        (out_dir / "leak").symlink_to(secret)

        art_root = tmp_path / "artifacts"
        artifact = harvest.build_tarball(out_dir, root=art_root)
        tar_path = art_root / artifact["key"]

        with tarfile.open(tar_path, mode="r:gz") as tar:
            member = tar.getmember("./leak")
            assert member.issym()  # a link entry, never the file's content
            assert member.linkname == str(secret)  # the path only, no data

        # Extracting with the "data" filter (stage_run_artifact's own
        # filter) refuses the out-of-tree symlink outright rather than
        # silently materializing it.
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        with tarfile.open(tar_path, mode="r:gz") as tar:
            with pytest.raises(tarfile.FilterError):
                tar.extractall(extract_dir, filter="data")


# ── RUN.json ─────────────────────────────────────────────────────


class TestRunJson:
    def test_missing_returns_none(self, tmp_path: Path) -> None:
        assert harvest.parse_run_json(tmp_path) is None

    def test_valid_object_parsed(self, tmp_path: Path) -> None:
        (tmp_path / "RUN.json").write_text(
            '{"cmd": "python main.py", "inputs": [], "outputs": [], '
            '"image": "code-task:abc"}'
        )
        recipe = harvest.parse_run_json(tmp_path)
        assert recipe is not None
        assert recipe["cmd"] == "python main.py"

    def test_invalid_json_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "RUN.json").write_text("{not json")
        assert harvest.parse_run_json(tmp_path) is None

    def test_non_object_json_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "RUN.json").write_text("[1, 2, 3]")
        assert harvest.parse_run_json(tmp_path) is None


# ── /work/out → plaintext DB projection ────────────────────────────


class TestProjectOut:
    def test_projects_text_skips_binary_and_oversized(
        self, hub_no_embedder: Hub, store: Store, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "work" / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "main.py").write_text("print('hi')\n")
        (out_dir / "README.txt").write_text("hello\n")
        nested = out_dir / "tests"
        nested.mkdir()
        (nested / "test_main.py").write_text("def test_x(): pass\n")
        (out_dir / "blob.bin").write_bytes(b"\x00\x01\x02binary")
        big = out_dir / "huge.txt"
        big.write_bytes(b"x" * (harvest.MAX_HARVEST_FILE_BYTES + 1))

        dest_root = tmp_path / "PRECIS_ROOT"
        dest_root.mkdir()

        ref_ids, n_skipped, messages = harvest.project_out(
            hub_no_embedder,
            out_dir=out_dir,
            dest_root=dest_root,
            harvest_subdir="sandbox/sandbox-1",
        )

        assert len(ref_ids) == 3  # main.py, README.txt, tests/test_main.py
        assert n_skipped == 2  # blob.bin (binary), huge.txt (size cap)
        assert any("binary" in m for m in messages)
        assert any("cap" in m for m in messages)

        # Physically landed under PRECIS_ROOT/sandbox/sandbox-1/… — dots
        # collapsed and lowercased (see _dest_name's docstring: the
        # PlaintextHandler slug<->path mapping requires exactly one real
        # extension, all-lowercase, to round-trip).
        assert (dest_root / "sandbox" / "sandbox-1" / "main-py.txt").is_file()
        assert (dest_root / "sandbox" / "sandbox-1" / "readme.txt").is_file()
        assert (
            dest_root / "sandbox" / "sandbox-1" / "tests" / "test_main-py.txt"
        ).is_file()

        # Each ref is a real, disk-backed plaintext ref — re-reading via a
        # fresh PlaintextHandler over the same root must NOT soft-delete it
        # (the whole point of materializing on disk instead of a phantom
        # DB-only row).
        from precis.handlers.plaintext import PlaintextHandler

        handler2 = PlaintextHandler(hub=hub_no_embedder, root=dest_root)
        for ref_id in ref_ids:
            ref = store.get_ref(kind="plaintext", id=ref_id)
            assert ref is not None
            assert ref.slug is not None
            reingested = handler2.ensure_ingested(ref.slug, force=False)
            assert reingested is not None
            assert reingested.id == ref_id

    def test_symlink_to_out_of_tree_file_is_not_projected(
        self, hub_no_embedder: Hub, tmp_path: Path
    ) -> None:
        # Finding 1 (CRITICAL — symlink exfil): a build writing
        # ``ln -s ~/.pgpass out/leak`` must not have the HOST file's
        # contents dereferenced and projected into a DB-visible ref.
        secret = tmp_path / "outside" / "secret.txt"
        secret.parent.mkdir(parents=True)
        secret.write_text("TOP SECRET HOST CONTENT\n")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "main.py").write_text("print('hi')\n")
        (out_dir / "leak").symlink_to(secret)

        dest_root = tmp_path / "root"
        dest_root.mkdir()
        ref_ids, n_skipped, messages = harvest.project_out(
            hub_no_embedder,
            out_dir=out_dir,
            dest_root=dest_root,
            harvest_subdir="sandbox/x",
        )

        # Only main.py projected; the symlink is skipped, never read.
        assert len(ref_ids) == 1
        assert n_skipped == 1
        assert any("symlink" in m for m in messages)

        # No file anywhere under dest_root contains the secret content.
        for p in dest_root.rglob("*"):
            if p.is_file():
                assert "TOP SECRET" not in p.read_text(errors="ignore")

    def test_symlinked_directory_is_not_descended(
        self, hub_no_embedder: Hub, tmp_path: Path
    ) -> None:
        real_dir = tmp_path / "real_elsewhere"
        real_dir.mkdir()
        (real_dir / "host_only.txt").write_text("host-only content\n")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "main.py").write_text("print('hi')\n")
        (out_dir / "linked_dir").symlink_to(real_dir, target_is_directory=True)

        dest_root = tmp_path / "root"
        dest_root.mkdir()
        ref_ids, n_skipped, messages = harvest.project_out(
            hub_no_embedder,
            out_dir=out_dir,
            dest_root=dest_root,
            harvest_subdir="sandbox/x",
        )

        assert len(ref_ids) == 1  # only main.py
        assert n_skipped == 1  # the symlinked dir itself, not descended
        assert any("symlink" in m for m in messages)
        # The linked directory's content never lands anywhere under
        # dest_root.
        for p in dest_root.rglob("*"):
            if p.is_file():
                assert "host-only" not in p.read_text(errors="ignore")

    def test_regular_file_still_projects(
        self, hub_no_embedder: Hub, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "main.py").write_text("print('hi')\n")

        dest_root = tmp_path / "root"
        dest_root.mkdir()
        ref_ids, n_skipped, _messages = harvest.project_out(
            hub_no_embedder,
            out_dir=out_dir,
            dest_root=dest_root,
            harvest_subdir="sandbox/x",
        )
        assert len(ref_ids) == 1
        assert n_skipped == 0

    def test_no_collision_between_same_stem_different_source_ext(
        self, hub_no_embedder: Hub, tmp_path: Path
    ) -> None:
        # main.py and main.json both get a ".txt" appended — must not
        # collapse onto the same slug (see _slugify_dest_path docstring).
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "main.py").write_text("a = 1\n")
        (out_dir / "main.json").write_text("{}\n")

        dest_root = tmp_path / "root"
        dest_root.mkdir()
        ref_ids, n_skipped, _messages = harvest.project_out(
            hub_no_embedder,
            out_dir=out_dir,
            dest_root=dest_root,
            harvest_subdir="sandbox/x",
        )
        assert n_skipped == 0
        assert len(ref_ids) == 2
        assert len(set(ref_ids)) == 2


# ── harvest_out orchestration ───────────────────────────────────────


class TestHarvestOut:
    def test_empty_out_dir_yields_empty_result(
        self, store: Store, tmp_path: Path
    ) -> None:
        jid = _mk_job(store)
        work_dir = tmp_path / "work"
        (work_dir / "out").mkdir(parents=True)  # empty
        result = harvest.harvest_out(
            store,
            job_ref_id=jid,
            container_name="sandbox-x",
            work_dir=work_dir,
            image="code-task:latest",
            model="claude-opus",
            artifact_store_root=tmp_path / "artifacts",
        )
        assert result.empty
        assert result.folder_ref_id is None
        assert "empty" in harvest.summarize(result)

    def test_full_harvest_mints_folder_projects_files_and_tarball(
        self, store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        precis_root = tmp_path / "PRECIS_ROOT"
        precis_root.mkdir()
        art_root = tmp_path / "artifacts"

        parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
        jid = _mk_job(store, parent_id=parent.id)

        work_dir = tmp_path / "work" / "sandbox-1"
        out_dir = work_dir / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "main.py").write_text("print('hi')\n")
        (out_dir / "RUN.json").write_text(
            '{"cmd": "python main.py", "inputs": [], "outputs": [], '
            '"image": "code-task:abc"}'
        )

        result = harvest.harvest_out(
            store,
            job_ref_id=jid,
            container_name="sandbox-1",
            work_dir=work_dir,
            image="code-task:abc",
            model="claude-opus-4-7",
            root=precis_root,
            artifact_store_root=art_root,
        )

        assert result.folder_ref_id is not None
        assert result.n_projected == 2  # main.py + RUN.json, both projected
        assert result.artifact is not None
        assert result.run_recipe is not None
        assert result.run_recipe["cmd"] == "python main.py"

        folder_meta = _meta(store, result.folder_ref_id)
        assert folder_meta["image"] == "code-task:abc"
        assert folder_meta["artifact"]["sha256"] == result.artifact["sha256"]
        assert folder_meta["run_recipe"]["cmd"] == "python main.py"

        # job -> folder link (design: harvest folder is derived-from the job)
        with store.pool.connection() as conn:
            link_rows = conn.execute(
                "SELECT src_ref_id FROM links "
                "WHERE relation = 'derived-from' AND dst_ref_id = %s",
                (jid,),
            ).fetchall()
        assert any(r[0] == result.folder_ref_id for r in link_rows)

        # Job meta carries artifact + recipe + folder pointer too.
        jmeta = _meta(store, jid)
        assert jmeta["artifact"]["sha256"] == result.artifact["sha256"]
        assert jmeta["harvest_folder_id"] == result.folder_ref_id
        assert jmeta["run_recipe"]["cmd"] == "python main.py"

        # Files parented under the folder.
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id FROM refs WHERE parent_id = %s", (result.folder_ref_id,)
            ).fetchall()
        assert len(rows) == 2

        # Tarball verifies round-trip.
        assert harvest.verify_artifact(result.artifact["sha256"], root=art_root)

        summary = harvest.summarize(result)
        assert "harvested folder:" in summary
        assert "RUN.json recipe parsed" in summary

    def test_second_build_supersedes_first_via_owning_todo(
        self, store: Store, tmp_path: Path
    ) -> None:
        precis_root = tmp_path / "PRECIS_ROOT"
        precis_root.mkdir()
        art_root = tmp_path / "artifacts"

        parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})

        def _build(container: str) -> harvest.HarvestResult:
            jid = _mk_job(store, parent_id=parent.id)
            work_dir = tmp_path / "work" / container
            out_dir = work_dir / "out"
            out_dir.mkdir(parents=True)
            (out_dir / "main.py").write_text(f"print({container!r})\n")
            return harvest.harvest_out(
                store,
                job_ref_id=jid,
                container_name=container,
                work_dir=work_dir,
                image="code-task:latest",
                model="claude-opus",
                root=precis_root,
                artifact_store_root=art_root,
            )

        first = _build("sandbox-1")
        second = _build("sandbox-2")

        assert first.folder_ref_id != second.folder_ref_id
        with store.pool.connection() as conn:
            link_rows = conn.execute(
                "SELECT dst_ref_id FROM links "
                "WHERE relation = 'supersedes' AND src_ref_id = %s",
                (second.folder_ref_id,),
            ).fetchall()
        assert any(r[0] == first.folder_ref_id for r in link_rows)

    def test_precis_root_unset_still_harvests_tarball_only(
        self, store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("PRECIS_ROOT", raising=False)
        jid = _mk_job(store)
        work_dir = tmp_path / "work"
        out_dir = work_dir / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "main.py").write_text("print('hi')\n")

        result = harvest.harvest_out(
            store,
            job_ref_id=jid,
            container_name="sandbox-x",
            work_dir=work_dir,
            image="code-task:latest",
            model="claude-opus",
            root=None,
            artifact_store_root=tmp_path / "artifacts",
        )
        assert result.folder_ref_id is not None
        assert result.n_projected == 0
        assert result.artifact is not None
        assert any("PRECIS_ROOT not set" in m for m in result.messages)

    def test_extra_derived_from_links_run_folder_to_build_folder(
        self, store: Store, tmp_path: Path
    ) -> None:
        precis_root = tmp_path / "PRECIS_ROOT"
        precis_root.mkdir()
        art_root = tmp_path / "artifacts"

        build_jid = _mk_job(store)
        build_work = tmp_path / "work" / "sandbox-build"
        (build_work / "out").mkdir(parents=True)
        (build_work / "out" / "main.py").write_text("print('hi')\n")
        build_result = harvest.harvest_out(
            store,
            job_ref_id=build_jid,
            container_name="sandbox-build",
            work_dir=build_work,
            image="code-task:abc",
            model="claude-opus",
            root=precis_root,
            artifact_store_root=art_root,
        )
        assert build_result.folder_ref_id is not None

        run_jid = _mk_job(store)
        run_work = tmp_path / "work" / "sandbox-run"
        (run_work / "out").mkdir(parents=True)
        (run_work / "out" / "RESULT.md").write_text("42\n")
        run_result = harvest.harvest_out(
            store,
            job_ref_id=run_jid,
            container_name="sandbox-run",
            work_dir=run_work,
            image="code-task:abc",
            model="",
            root=precis_root,
            artifact_store_root=art_root,
            extra_derived_from=build_result.folder_ref_id,
        )
        assert run_result.folder_ref_id is not None

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT dst_ref_id FROM links "
                "WHERE relation = 'derived-from' AND src_ref_id = %s",
                (run_result.folder_ref_id,),
            ).fetchall()
        dsts = {r[0] for r in rows}
        assert run_jid in dsts
        assert build_result.folder_ref_id in dsts
        run_folder_meta = _meta(store, run_result.folder_ref_id)
        assert run_folder_meta["run_of_folder_id"] == build_result.folder_ref_id


# ── mode:run staging — the inverse: fetch a build's tarball back out ──


class TestStageRunArtifact:
    def test_stages_from_tarball(self, store: Store, tmp_path: Path) -> None:
        precis_root = tmp_path / "PRECIS_ROOT"
        precis_root.mkdir()
        art_root = tmp_path / "artifacts"

        jid = _mk_job(store)
        work_dir = tmp_path / "work" / "sandbox-build"
        out_dir = work_dir / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "main.py").write_text("print('hi')\n")
        (out_dir / "RUN.json").write_text(
            '{"cmd": "python main.py", "inputs": [], "outputs": [], '
            '"image": "code-task:abc"}'
        )
        result = harvest.harvest_out(
            store,
            job_ref_id=jid,
            container_name="sandbox-build",
            work_dir=work_dir,
            image="code-task:abc",
            model="claude-opus",
            root=precis_root,
            artifact_store_root=art_root,
        )
        assert result.folder_ref_id is not None

        dest_dir = tmp_path / "run-work"
        folder_meta = harvest.stage_run_artifact(
            store,
            folder_id=result.folder_ref_id,
            dest_dir=dest_dir,
            artifact_store_root=art_root,
        )
        assert (dest_dir / "main.py").read_text() == "print('hi')\n"
        assert (dest_dir / "RUN.json").is_file()
        assert folder_meta["run_recipe"]["cmd"] == "python main.py"

    def test_missing_folder_raises(self, store: Store, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            harvest.stage_run_artifact(
                store, folder_id=999999, dest_dir=tmp_path / "work"
            )

    def test_falls_back_to_plaintext_refs_when_tarball_missing(
        self, store: Store, tmp_path: Path
    ) -> None:
        precis_root = tmp_path / "PRECIS_ROOT"
        precis_root.mkdir()
        art_root = tmp_path / "artifacts"

        jid = _mk_job(store)
        work_dir = tmp_path / "work" / "sandbox-build"
        out_dir = work_dir / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "main.py").write_text("print('hi')\n")
        result = harvest.harvest_out(
            store,
            job_ref_id=jid,
            container_name="sandbox-build",
            work_dir=work_dir,
            image="code-task:abc",
            model="claude-opus",
            root=precis_root,
            artifact_store_root=art_root,
        )
        assert result.folder_ref_id is not None
        assert result.artifact is not None

        # Simulate a NAS hiccup / manual GC: delete the stored tarball so
        # verify_artifact fails and staging must fall back.
        tar_path = harvest.artifact_path(result.artifact["sha256"], root=art_root)
        tar_path.unlink()

        dest_dir = tmp_path / "run-work"
        harvest.stage_run_artifact(
            store,
            folder_id=result.folder_ref_id,
            dest_dir=dest_dir,
            artifact_store_root=art_root,
            root=precis_root,
        )
        # Reconstructed from the plaintext ref's stamped harvest_orig_path
        # — the ORIGINAL name, not the on-disk projection rewrite
        # (main.py, not main-py.txt).
        assert (dest_dir / "main.py").read_text() == "print('hi')\n"

    def test_no_tarball_and_no_plaintext_refs_raises(
        self, store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("PRECIS_ROOT", raising=False)
        jid = _mk_job(store)
        work_dir = tmp_path / "work"
        out_dir = work_dir / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "main.py").write_text("print('hi')\n")
        # No PRECIS_ROOT (root=None) → no plaintext projection at all,
        # only the tarball.
        result = harvest.harvest_out(
            store,
            job_ref_id=jid,
            container_name="sandbox-x",
            work_dir=work_dir,
            image="code-task:latest",
            model="claude-opus",
            root=None,
            artifact_store_root=tmp_path / "artifacts",
        )
        assert result.folder_ref_id is not None
        assert result.artifact is not None
        tar_path = harvest.artifact_path(
            result.artifact["sha256"], root=tmp_path / "artifacts"
        )
        tar_path.unlink()

        with pytest.raises(ValueError, match="nothing to stage"):
            harvest.stage_run_artifact(
                store,
                folder_id=result.folder_ref_id,
                dest_dir=tmp_path / "run-work",
                artifact_store_root=tmp_path / "artifacts",
                root=None,
            )
