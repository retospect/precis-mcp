"""Integration tests for ``precis.sim.ingest`` (AC #3, sim-harness slice 1).

A fixture sim repo (a ``docs/findings.md`` + an ``out/pareto.csv``) is
projected into a fake ``PRECIS_ROOT`` and driven through the real
prose-ingest walker (``handler._ensure_ingested`` — never the
create-only ``put()``): findings land as a ``markdown`` ref, the CSV as
a ``plaintext`` ref (renamed ``.txt`` on disk), each stamped with the
producing git SHA in ``meta``. A second run over unchanged files must
be a true no-op — same ref ids, same block counts, nothing re-inserted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.sim.ingest import ingest_sim
from precis.sim.manifest import SimManifest
from precis.sim.registry import SimEntry
from precis.store import Store


def _git(path: Path, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *cmd], cwd=path, check=True, capture_output=True, text=True
    )


def _init_git_repo(path: Path) -> str:
    """git-init *path*, commit everything in it, return the HEAD sha."""
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed fixture sim")
    result = _git(path, "rev-parse", "HEAD")
    return result.stdout.strip()


@pytest.fixture
def sim_repo(tmp_path: Path) -> Path:
    """A minimal fixture sim repo: prose findings + a Pareto CSV."""
    repo = tmp_path / "fixture-sim"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "findings.md").write_text(
        "# Findings\n\nThe pareto front improved 12% over baseline.\n",
        encoding="utf-8",
    )
    (repo / "out").mkdir()
    (repo / "out" / "pareto.csv").write_text(
        "mass_kg,drag_n\n1.0,0.5\n2.0,0.4\n", encoding="utf-8"
    )
    return repo


@pytest.fixture
def sim_manifest() -> SimManifest:
    return SimManifest(
        run="python run.py",
        outputs=("docs/findings.md", "out/pareto.csv"),
        verify=(),
        writeup="fixture-writeup",
    )


def _entry(sim_repo: Path) -> SimEntry:
    return SimEntry(
        slug="fixture-sim",
        path=sim_repo,
        git_remote=None,
        manifest=Path("precis.sim.yaml"),
        quest=None,
    )


# ── first run: correct kinds + provenance ───────────────────────────


def test_ingest_sim_creates_markdown_and_plaintext_refs(
    store: Store,
    hub: Hub,
    tmp_path: Path,
    sim_repo: Path,
    sim_manifest: SimManifest,
) -> None:
    sha = _init_git_repo(sim_repo)
    root = tmp_path / "precis_root"
    root.mkdir()

    outcome = ingest_sim(
        slug="fixture-sim",
        entry=_entry(sim_repo),
        manifest=sim_manifest,
        root=root,
        hub=hub,
        store=store,
    )

    assert outcome.failed == 0
    assert outcome.skipped == 0
    assert outcome.ingested == 2

    md_ref = store.get_ref(kind="markdown", id="sim--fixture-sim--docs--findings")
    assert md_ref is not None
    assert md_ref.meta["sim_slug"] == "fixture-sim"
    assert md_ref.meta["sim_git_sha"] == sha
    assert store.count_blocks(md_ref.id) > 0

    txt_ref = store.get_ref(kind="plaintext", id="sim--fixture-sim--out--pareto")
    assert txt_ref is not None
    assert txt_ref.meta["sim_slug"] == "fixture-sim"
    assert txt_ref.meta["sim_git_sha"] == sha
    assert store.count_blocks(txt_ref.id) > 0

    # Projected onto disk under PRECIS_ROOT/sim/<slug>/ — CSV normalized
    # to .txt (PlaintextHandler's extension set omits .csv), markdown
    # keeps its extension.
    # Subpath preserved under sim/<slug>/ (not flattened to basename), so
    # same-named outputs in different subdirs can't collide.
    assert (root / "sim" / "fixture-sim" / "docs" / "findings.md").exists()
    assert (root / "sim" / "fixture-sim" / "out" / "pareto.txt").exists()
    assert not (root / "sim" / "fixture-sim" / "out" / "pareto.csv").exists()


def test_ingest_sim_tolerates_non_git_dir(
    store: Store, hub: Hub, tmp_path: Path, sim_repo: Path, sim_manifest: SimManifest
) -> None:
    """No ``.git`` in the sim checkout — SHA is omitted, not a crash."""
    root = tmp_path / "precis_root"
    root.mkdir()

    outcome = ingest_sim(
        slug="fixture-sim",
        entry=_entry(sim_repo),
        manifest=sim_manifest,
        root=root,
        hub=hub,
        store=store,
    )

    assert outcome.failed == 0
    assert outcome.ingested == 2
    md_ref = store.get_ref(kind="markdown", id="sim--fixture-sim--docs--findings")
    assert md_ref is not None
    assert md_ref.meta["sim_slug"] == "fixture-sim"
    assert "sim_git_sha" not in md_ref.meta


def test_ingest_sim_skips_binary_plots(
    store: Store, hub: Hub, tmp_path: Path, sim_repo: Path
) -> None:
    (sim_repo / "out" / "pareto.png").write_bytes(b"\x89PNG\r\n fake")
    manifest = SimManifest(
        run="python run.py",
        outputs=("docs/findings.md", "out/pareto.png"),
        verify=(),
        writeup="fixture-writeup",
    )
    root = tmp_path / "precis_root"
    root.mkdir()

    outcome = ingest_sim(
        slug="fixture-sim",
        entry=_entry(sim_repo),
        manifest=manifest,
        root=root,
        hub=hub,
        store=store,
    )

    assert outcome.ingested == 1
    assert outcome.skipped == 1
    assert outcome.failed == 0
    assert any("binary plot" in m for m in outcome.messages)
    assert not (root / "sim" / "fixture-sim" / "pareto.png").exists()


# ── second run: true no-op (AC #3) ──────────────────────────────────


def test_ingest_sim_second_run_over_unchanged_files_is_a_no_op(
    store: Store,
    hub: Hub,
    tmp_path: Path,
    sim_repo: Path,
    sim_manifest: SimManifest,
) -> None:
    _init_git_repo(sim_repo)
    root = tmp_path / "precis_root"
    root.mkdir()
    entry = _entry(sim_repo)

    first = ingest_sim(
        slug="fixture-sim",
        entry=entry,
        manifest=sim_manifest,
        root=root,
        hub=hub,
        store=store,
    )
    assert first.ingested == 2
    assert first.failed == 0

    md_ref = store.get_ref(kind="markdown", id="sim--fixture-sim--docs--findings")
    txt_ref = store.get_ref(kind="plaintext", id="sim--fixture-sim--out--pareto")
    assert md_ref is not None
    assert txt_ref is not None
    md_id_before, txt_id_before = md_ref.id, txt_ref.id
    md_blocks_before = store.count_blocks(md_ref.id)
    txt_blocks_before = store.count_blocks(txt_ref.id)

    second = ingest_sim(
        slug="fixture-sim",
        entry=entry,
        manifest=sim_manifest,
        root=root,
        hub=hub,
        store=store,
    )

    # Zero new rows: no new ingest, everything reported skipped/no-op.
    assert second.ingested == 0
    assert second.failed == 0
    assert second.skipped == 2

    md_ref_after = store.get_ref(kind="markdown", id="sim--fixture-sim--docs--findings")
    txt_ref_after = store.get_ref(kind="plaintext", id="sim--fixture-sim--out--pareto")
    assert md_ref_after is not None
    assert txt_ref_after is not None
    # Same underlying ref rows (not soft-deleted + re-created).
    assert md_ref_after.id == md_id_before
    assert txt_ref_after.id == txt_id_before
    # Same block counts — nothing was re-chunked/re-inserted.
    assert store.count_blocks(md_ref_after.id) == md_blocks_before
    assert store.count_blocks(txt_ref_after.id) == txt_blocks_before
    # Provenance survives the no-op run.
    assert md_ref_after.meta.get("sim_slug") == "fixture-sim"
    assert txt_ref_after.meta.get("sim_slug") == "fixture-sim"


def test_ingest_sim_force_reingests_unchanged_files(
    store: Store,
    hub: Hub,
    tmp_path: Path,
    sim_repo: Path,
    sim_manifest: SimManifest,
) -> None:
    root = tmp_path / "precis_root"
    root.mkdir()
    entry = _entry(sim_repo)

    ingest_sim(
        slug="fixture-sim",
        entry=entry,
        manifest=sim_manifest,
        root=root,
        hub=hub,
        store=store,
    )
    forced = ingest_sim(
        slug="fixture-sim",
        entry=entry,
        manifest=sim_manifest,
        root=root,
        hub=hub,
        store=store,
        force=True,
    )
    assert forced.ingested == 2
    assert forced.skipped == 0


def test_ingest_sim_same_basename_in_different_subdirs_no_collision(
    store: Store, hub: Hub, tmp_path: Path
) -> None:
    """Two outputs sharing a basename across subdirs must ingest as two
    distinct refs — the destination preserves the subpath, it doesn't flatten
    to the basename and clobber (F3)."""
    repo = tmp_path / "fixture-sim"
    (repo / "case1").mkdir(parents=True)
    (repo / "case2").mkdir(parents=True)
    (repo / "case1" / "findings.md").write_text(
        "# Case 1\n\nCase one improved 12%.\n", encoding="utf-8"
    )
    (repo / "case2" / "findings.md").write_text(
        "# Case 2\n\nCase two improved 8%.\n", encoding="utf-8"
    )
    manifest = SimManifest(
        run="python run.py",
        outputs=("case1/findings.md", "case2/findings.md"),
        verify=(),
        writeup="fixture-writeup",
    )
    root = tmp_path / "precis_root"
    root.mkdir()

    outcome = ingest_sim(
        slug="fixture-sim",
        entry=_entry(repo),
        manifest=manifest,
        root=root,
        hub=hub,
        store=store,
    )

    assert outcome.failed == 0
    assert outcome.ingested == 2  # both, not one clobbering the other
    c1 = store.get_ref(kind="markdown", id="sim--fixture-sim--case1--findings")
    c2 = store.get_ref(kind="markdown", id="sim--fixture-sim--case2--findings")
    assert c1 is not None and c2 is not None
    assert c1.id != c2.id
    assert (root / "sim" / "fixture-sim" / "case1" / "findings.md").exists()
    assert (root / "sim" / "fixture-sim" / "case2" / "findings.md").exists()
