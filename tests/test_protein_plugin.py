"""precis-bio `protein` kind + `fold` job (ADR 0056, slice 4).

Covers the pure IR/engine layer (no DB, no GPU), the AF3 container plumbing
(input JSON / argv / output parser), the handler's inline slice-0 fold +
content-addressed cache hit, the compute-lane dispatch branch (mint a fold job
under the protein via `can_own_jobs`), the requester-blocking wiring, the
worker write-back (stub + stubbed AF3 container), and the dark-ship gate.

The test DB template carries only core migrations, so `protein_store` seeds the
plugin's `protein` kind directly (the plugin migration's idempotent INSERT).
The `fold` job_type is injected into the registry (no entry point at test time).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

import precis_bio
from precis.dispatch import Hub, _try
from precis.store import Store
from precis.store.types import Relation
from precis.workers import job_types as jt
from precis_bio import jobs as bio_jobs
from precis_bio.alphafold import (
    CONTAINER_MODELS,
    INPUT_FILE,
    build_af3_input,
    build_fold_argv,
    parse_af3_output,
)
from precis_bio.converge import BOX_PADDING, cif_to_scene, parse_atom_site
from precis_bio.engine import (
    FOLD_IMAGE_ENV,
    AlphaFold3Engine,
    StubFoldEngine,
    resolve_engine,
)
from precis_bio.ir import (
    MODE_DE_NOVO,
    ProteinFold,
    fold_cache_key,
    mean_plddt_from_cif,
    normalize_sequence,
    validate_sequence,
)
from precis_bio.jobs import FOLD_SPEC, run_fold
from precis_bio.protein import ProteinHandler

_BIO_MIGRATIONS_DIR = Path(precis_bio.__file__).parent / "migrations"

#: A short real sequence (insulin A chain) for the round-trip tests.
_INSULIN_A = "GIVEQCCTSICSLYQLENYCN"


@pytest.fixture
def protein_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The shared test store with every precis_bio migration seeded + the dark
    flag on (the `protein` kind + the has-fold-structure relation)."""
    monkeypatch.setenv("PRECIS_BIO_ENABLED", "1")
    with store.pool.connection() as c:
        for sql in sorted(_BIO_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return store


@pytest.fixture
def register_fold() -> Any:
    """Inject the `fold` job_type into the registry for the test."""
    jt._REGISTRY["fold"] = FOLD_SPEC
    yield
    jt._REGISTRY.pop("fold", None)


def _fake_af3_output(
    out_dir: Path, *, name: str = "insulin", plddt: float = 88.0
) -> None:
    """Write a minimal but realistic AF3 output tree into ``out_dir`` — a
    ``<name>/`` subdir with a model mmCIF (two Cα atoms at ``plddt``) + a
    summary-confidences JSON."""
    sub = out_dir / name
    sub.mkdir(parents=True, exist_ok=True)
    cif = (
        "data_"
        + name
        + "\nloop_\n"
        + "\n".join(
            "_atom_site." + c
            for c in (
                "group_PDB",
                "id",
                "type_symbol",
                "label_atom_id",
                "label_comp_id",
                "label_asym_id",
                "label_seq_id",
                "Cartn_x",
                "Cartn_y",
                "Cartn_z",
                "B_iso_or_equiv",
            )
        )
        + "\n"
        + f"ATOM 1 C CA GLY A 1 0.000 0.000 0.000 {plddt:.2f}\n"
        + f"ATOM 2 C CA ILE A 2 3.800 0.000 0.000 {plddt:.2f}\n"
    )
    (sub / f"{name}_model.cif").write_text(cif)
    (sub / f"{name}_summary_confidences.json").write_text(
        json.dumps({"ptm": 0.82, "iptm": 0.0, "ranking_score": 0.9, "has_clash": 0.0})
    )


# ─────────────────────────── pure IR / engine ───────────────────────────


def test_normalize_and_validate_sequence() -> None:
    assert normalize_sequence(" giv eq\ncc ") == "GIVEQCC"
    assert validate_sequence("giveqcc") == "GIVEQCC"
    with pytest.raises(ValueError, match="empty"):
        validate_sequence("   ")
    with pytest.raises(ValueError, match="invalid amino-acid"):
        validate_sequence("GIVEQ123")


def test_fold_cache_key_is_content_addressed() -> None:
    k1 = fold_cache_key(sequence="ACDE", engine="stub", engine_version="v1", seeds=[1])
    k2 = fold_cache_key(sequence="acde", engine="stub", engine_version="v1", seeds=[1])
    assert k1 == k2 and k1.startswith("fold:")  # normalized, stable
    assert k1 != fold_cache_key(sequence="ACDE", engine="stub", engine_version="v2")
    assert k1 != fold_cache_key(sequence="ACDF", engine="stub", engine_version="v1")
    assert k1 != fold_cache_key(
        sequence="ACDE", engine="stub", engine_version="v1", seeds=[2]
    )


def test_protein_fold_json_roundtrip_and_render() -> None:
    f = ProteinFold(
        name="p",
        sequence="ACDE",
        engine="stub",
        engine_version="v1",
        cif="data_x\n",
        plddt_mean=91.2,
        ptm=0.8,
        n_residues=4,
        seeds=[1],
    )
    again = ProteinFold.from_json(f.to_json())
    assert again == f
    rendered = f.render()
    assert "ACDE" in rendered and "pLDDT" in rendered and "very high" in rendered
    assert f.folded and "ACDE" in f.card_text()


def test_mean_plddt_from_cif() -> None:
    cif = (
        "data_x\nloop_\n_atom_site.group_PDB\n_atom_site.id\n"
        "_atom_site.label_atom_id\n_atom_site.B_iso_or_equiv\n"
        "ATOM 1 CA 80.0\nATOM 2 N 10.0\nATOM 3 CA 90.0\n"
    )
    assert mean_plddt_from_cif(cif) == 85.0  # CA only: (80+90)/2
    assert mean_plddt_from_cif("not a cif") is None
    assert mean_plddt_from_cif("") is None


def test_stub_engine_is_deterministic() -> None:
    a = StubFoldEngine().fold("ACDE")
    b = StubFoldEngine().fold("ACDE")
    assert a == b
    assert a.folded and a.engine == "stub" and a.mode == MODE_DE_NOVO
    assert a.plddt_mean == 50.0  # the stub CIF's constant B-factor
    assert a.provenance.get("engine") == "stub"


def test_resolve_engine() -> None:
    assert isinstance(resolve_engine("stub"), StubFoldEngine)
    assert isinstance(resolve_engine(None), StubFoldEngine)  # default
    af = resolve_engine("alphafold3")
    assert isinstance(af, AlphaFold3Engine) and af.is_container
    with pytest.raises(ValueError, match="unknown fold engine"):
        resolve_engine("nope")


def test_alphafold_engine_image_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FOLD_IMAGE_ENV, raising=False)
    assert AlphaFold3Engine().image == "alphafold3:ready"
    monkeypatch.setenv(FOLD_IMAGE_ENV, "alphafold3:pinned-sha")
    assert AlphaFold3Engine().image == "alphafold3:pinned-sha"


def test_alphafold_fold_raises_until_node() -> None:
    with pytest.raises(NotImplementedError, match="container engine"):
        AlphaFold3Engine().fold("ACDE")


def test_run_fold_from_params() -> None:
    f = run_fold({"sequence": "ACDE", "engine": "stub", "cache_key": "x"})
    assert isinstance(f, ProteinFold) and f.folded and f.n_residues == 4


# ─────────────────────────── AF3 container plumbing ───────────────────────────


def test_build_af3_input() -> None:
    doc = build_af3_input("insulin", "giveqcc", seeds=[1, 2])
    assert doc["name"] == "insulin" and doc["modelSeeds"] == [1, 2]
    assert doc["dialect"] == "alphafold3"
    prot = doc["sequences"][0]["protein"]
    assert prot["id"] == "A" and prot["sequence"] == "GIVEQCC"
    assert prot["templates"] == [] and prot["unpairedMsa"] == ""


def test_build_fold_argv() -> None:
    argv = build_fold_argv(
        ref_id=7,
        in_dir="/s/in",
        out_dir="/s/out",
        image="af3:sha",
        models_dir="/nas/models",
    )
    assert argv[:5] == ["docker", "run", "--rm", "--gpus", "all"]
    assert "--name" in argv and "precis-fold-7" in argv
    assert "af3:sha" in argv
    assert f"/s/in/{INPUT_FILE}:/input/protein.json:ro" in argv
    assert f"/nas/models:{CONTAINER_MODELS}:ro" in argv
    assert "/s/out:/output" in argv
    assert "--norun_data_pipeline" in argv  # de-novo mode
    # No XLA cache mount + no memory cap unless asked.
    assert "/root/.cache/xla_extension" not in " ".join(argv)
    assert "--memory" not in argv
    argv2 = build_fold_argv(
        ref_id=7,
        in_dir="/s/in",
        out_dir="/s/out",
        image="af3",
        models_dir="/m",
        xla_cache_dir="/host/xla",
        mem_limit="100g",
    )
    assert "/host/xla:/root/.cache/xla_extension" in argv2
    # Memory cap: --memory + an equal --memory-swap (no swap on top of the cap),
    # placed before the image so docker parses it as a run flag.
    assert "--memory" in argv2 and "--memory-swap" in argv2
    assert argv2[argv2.index("--memory") + 1] == "100g"
    assert argv2[argv2.index("--memory-swap") + 1] == "100g"
    assert argv2.index("--memory") < argv2.index("af3")


def test_parse_af3_output(tmp_path: Path) -> None:
    _fake_af3_output(tmp_path, name="insulin", plddt=88.0)
    f = parse_af3_output(
        str(tmp_path),
        name="insulin",
        sequence=_INSULIN_A,
        engine_version="af3-v3.0.1",
        seeds=[1],
    )
    assert f.folded and f.engine == "alphafold3"
    assert f.plddt_mean == 88.0 and f.ptm == 0.82
    assert f.ranking_score == 0.9
    assert f.n_residues == len(_INSULIN_A)
    assert f.provenance["model_cif"] == "insulin_model.cif"


def test_parse_af3_output_missing_is_no_model(tmp_path: Path) -> None:
    f = parse_af3_output(str(tmp_path), name="x", sequence="ACDE")
    assert not f.folded and f.cif == "" and f.plddt_mean is None


# ─────────────────────────── handler (inline) ───────────────────────────


def test_put_inline_folds_and_get_renders(protein_store: Store) -> None:
    """No fold node configured ⇒ the in-process stub runs inline (slice-0)."""
    h = ProteinHandler(hub=Hub(store=protein_store))
    resp = h.put(id="insulin-a", sequence=_INSULIN_A, engine="stub")
    assert "folded" in resp.body and "in-process" in resp.body

    ref = protein_store.get_ref(kind="protein", id="insulin-a")
    assert ref is not None
    meta = ref.meta or {}
    assert meta.get("status") == "folded"
    assert meta.get("fold", {}).get("sequence") == _INSULIN_A

    got = h.get(id="insulin-a")
    assert "residues" in got.body and str(len(_INSULIN_A)) in got.body

    # view='cif' returns the raw structure.
    cif = h.get(id="insulin-a", view="cif").body
    assert "_atom_site" in cif and "ATOM" in cif

    # The protein emitted an embeddable card_combined chunk (ord = -1).
    with protein_store.pool.connection() as c:
        n = c.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord = -1",
            (ref.id,),
        ).fetchone()[0]
    assert n == 1


def test_put_rejects_bad_sequence(protein_store: Store) -> None:
    h = ProteinHandler(hub=Hub(store=protein_store))
    with pytest.raises(Exception, match="invalid amino-acid"):
        h.put(id="bad", sequence="GIVEQ123", engine="stub")


def test_put_second_call_is_cache_hit(
    protein_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = ProteinHandler(hub=Hub(store=protein_store))
    h.put(id="p", sequence="ACDE", engine="stub")

    calls = {"n": 0}
    orig = StubFoldEngine.fold

    def _counting(self: Any, sequence: str, **kw: Any) -> Any:
        calls["n"] += 1
        return orig(self, sequence, **kw)

    monkeypatch.setattr(StubFoldEngine, "fold", _counting)
    resp = h.put(id="p", sequence="ACDE", engine="stub")
    assert "cache hit" in resp.body
    assert calls["n"] == 0  # zero recompute (ADR 0007)


def test_delete_soft_retires(protein_store: Store) -> None:
    h = ProteinHandler(hub=Hub(store=protein_store))
    h.put(id="p", sequence="ACDE", engine="stub")
    resp = h.delete(id="p")
    assert "retired" in resp.body
    assert protein_store.get_ref(kind="protein", id="p") is None


# ─────────────────────────── compute lane ───────────────────────────


def test_put_dispatches_job_when_fold_node_set(
    protein_store: Store,
    register_fold: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured fold node ⇒ mint a fold job parented on the protein
    (ADR 0044 compute lane, via `can_own_jobs`), not an inline fold."""
    monkeypatch.setenv("PRECIS_FOLD_NODE", "spark")
    hub = Hub(store=protein_store)
    h = _try(ProteinHandler, hub=hub)
    assert h is not None

    resp = h.put(id="myprot", sequence=_INSULIN_A, engine="alphafold3")
    assert "dispatched to spark" in resp.body

    ref = protein_store.get_ref(kind="protein", id="myprot")
    assert ref is not None
    with protein_store.pool.connection() as c:
        row = c.execute(
            "SELECT meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND deleted_at IS NULL",
            (ref.id,),
        ).fetchone()
    assert row is not None, "a fold job should parent on the protein"
    assert row[0].get("job_type") == "fold"
    assert row[0].get("executor") == "ssh_node"
    assert (row[0].get("params") or {}).get("target_node") == "spark"
    assert "folding" in h.get(id="myprot").body  # still pending


def test_requested_by_wires_blocking_todo(
    protein_store: Store,
    register_fold: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_FOLD_NODE", "spark")
    todo = protein_store.insert_ref(kind="todo", slug=None, title="fold the target")

    hub = Hub(store=protein_store)
    h = _try(ProteinHandler, hub=hub)
    assert h is not None
    h.put(id="tgt", sequence="ACDE", engine="alphafold3", requested_by=todo.id)

    reloaded = protein_store.get_ref(kind="todo", id=todo.id)
    assert (reloaded.meta or {}).get("auto_check", {}).get(
        "type"
    ) == "derived_job_succeeded"
    with protein_store.pool.connection() as c:
        rel = c.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s", (todo.id,)
        ).fetchone()
    assert rel is not None and rel[0] == "requested"


def test_worker_dispatch_writes_fold_back(protein_store: Store) -> None:
    """The `fold` job dispatch (what ssh_node runs) folds + writes back (stub)."""
    ref = protein_store.insert_ref(
        kind="protein",
        slug="landing",
        title="landing",
        meta={"sequence": "ACDE", "engine": "stub", "status": "folding"},
    )
    key = fold_cache_key(sequence="ACDE", engine="stub", engine_version="stub-v1")
    params = {
        "protein_ref_id": ref.id,
        "sequence": "ACDE",
        "engine": "stub",
        "engine_version": "stub-v1",
        "cache_key": key,
    }
    ctx = _FakeCtx(store=protein_store, params=params)
    bio_jobs._dispatch(ctx, FOLD_SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    landed = protein_store.get_ref(kind="protein", id="landing")
    assert (landed.meta or {}).get("status") == "folded"
    assert (landed.meta or {}).get("fold", {}).get("sequence") == "ACDE"


def test_worker_dispatch_container_missing_node_fails(protein_store: Store) -> None:
    """A container engine with no fold node fails the job cleanly."""
    ref = protein_store.insert_ref(
        kind="protein", slug="cont", title="cont", meta={"sequence": "ACDE"}
    )
    params = {
        "protein_ref_id": ref.id,
        "sequence": "ACDE",
        "engine": "alphafold3",
        "engine_version": "af3-v3.0.1",
        "cache_key": "fold:deadbeef",
        # no target_node
    }
    ctx = _FakeCtx(store=protein_store, params=params)
    bio_jobs._dispatch(ctx, FOLD_SPEC)
    assert ctx.status != "succeeded"
    assert ctx.failure is not None and "fold node" in ctx.failure


def test_worker_dispatch_container_missing_models_fails(
    protein_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bio_jobs, "_MODELS_DIR", "")
    ref = protein_store.insert_ref(
        kind="protein", slug="nomodels", title="nomodels", meta={"sequence": "ACDE"}
    )
    params = {
        "protein_ref_id": ref.id,
        "sequence": "ACDE",
        "engine": "alphafold3",
        "engine_version": "af3-v3.0.1",
        "cache_key": "fold:abc",
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=protein_store, params=params)
    bio_jobs._dispatch(ctx, FOLD_SPEC)
    assert ctx.status != "succeeded"
    assert ctx.failure is not None and "PRECIS_FOLD_MODELS_DIR" in ctx.failure


def test_container_dispatch_round_trip(
    protein_store: Store, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The AF3 container path — stubbed RUNNER/STAGER — folds + writes back
    without a GPU (the struct_relax hook seam)."""
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()

    def _stager(ref_id: int) -> tuple[str, str]:
        return str(in_dir), str(out_dir)

    seen: dict[str, Any] = {}

    def _runner(argv: list[str], *, node: str, timeout: Any = None) -> tuple[int, str]:
        # The input JSON was staged for the container.
        seen["input"] = json.loads((in_dir / INPUT_FILE).read_text())
        # Simulate AF3: drop an output tree into the bound out-dir.
        _fake_af3_output(out_dir, name="myprot", plddt=91.0)
        return 0, "af3 ok"

    monkeypatch.setattr(bio_jobs, "STAGER", _stager)
    monkeypatch.setattr(bio_jobs, "RUNNER", _runner)
    monkeypatch.setattr(bio_jobs, "_MODELS_DIR", "/nas/models")

    ref = protein_store.insert_ref(
        kind="protein", slug="myprot", title="myprot", meta={"sequence": _INSULIN_A}
    )
    key = fold_cache_key(
        sequence=_INSULIN_A, engine="alphafold3", engine_version="af3-v3.0.1"
    )
    params = {
        "protein_ref_id": ref.id,
        "name": "myprot",
        "sequence": _INSULIN_A,
        "engine": "alphafold3",
        "engine_version": "af3-v3.0.1",
        "cache_key": key,
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=protein_store, params=params)
    bio_jobs._dispatch(ctx, FOLD_SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    # The staged AF3 input carried the sequence.
    assert seen["input"]["sequences"][0]["protein"]["sequence"] == _INSULIN_A
    landed = protein_store.get_ref(kind="protein", id="myprot")
    fold = (landed.meta or {}).get("fold") or {}
    assert fold.get("engine") == "alphafold3"
    assert (landed.meta or {}).get("status") == "folded"
    assert fold["plddt_mean"] == 91.0 and fold["ptm"] == 0.82


def test_container_dispatch_no_model_fails(
    protein_store: Store, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container that exits 0 but drops no mmCIF fails the job cleanly."""
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()

    monkeypatch.setattr(bio_jobs, "STAGER", lambda rid: (str(in_dir), str(out_dir)))
    monkeypatch.setattr(
        bio_jobs, "RUNNER", lambda argv, *, node, timeout=None: (0, "empty")
    )
    monkeypatch.setattr(bio_jobs, "_MODELS_DIR", "/nas/models")

    ref = protein_store.insert_ref(
        kind="protein", slug="empty", title="empty", meta={"sequence": "ACDE"}
    )
    params = {
        "protein_ref_id": ref.id,
        "name": "empty",
        "sequence": "ACDE",
        "engine": "alphafold3",
        "engine_version": "af3-v3.0.1",
        "cache_key": "fold:empty",
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=protein_store, params=params)
    bio_jobs._dispatch(ctx, FOLD_SPEC)
    assert ctx.status != "succeeded"
    assert ctx.failure is not None and "no model mmCIF" in ctx.failure


# ─────────────────── structure convergence (slice 4c) ───────────────────


def _multi_atom_cif() -> str:
    """A small realistic mmCIF (4 atoms, mixed elements) for the converger."""
    header = "data_x\nloop_\n" + "\n".join(
        "_atom_site." + c
        for c in (
            "group_PDB",
            "id",
            "type_symbol",
            "label_atom_id",
            "Cartn_x",
            "Cartn_y",
            "Cartn_z",
            "B_iso_or_equiv",
        )
    )
    rows = [
        "ATOM 1 N N 0.000 0.000 0.000 80.0",
        "ATOM 2 C CA 1.500 0.000 0.000 82.0",
        "ATOM 3 C C 2.400 1.200 0.000 78.0",
        "ATOM 4 O O 3.600 1.100 0.000 75.0",
    ]
    return header + "\n" + "\n".join(rows) + "\n"


def test_parse_atom_site() -> None:
    atoms = parse_atom_site(_multi_atom_cif())
    assert len(atoms) == 4
    assert [a[0] for a in atoms] == ["N", "C", "C", "O"]  # type_symbol → element
    assert atoms[1] == ("C", 1.5, 0.0, 0.0)
    assert parse_atom_site("not a cif") == []


def test_cif_to_scene_non_periodic() -> None:
    scene = cif_to_scene(_multi_atom_cif())
    assert len(scene.atoms) == 4
    assert scene.cell.pbc == (False, False, False)  # molecule mode
    # Unique per-element labels: N1, C1, C2, O1.
    assert set(scene.atoms) == {"N1", "C1", "C2", "O1"}
    assert scene.label_hi == {"N": 1, "C": 2, "O": 1}
    # Every atom sits strictly inside the padded box (fractional in (0,1)).
    for atom in scene.atoms.values():
        assert all(0.0 < f < 1.0 for f in atom.frac)
    # Box spans the bbox + 2*padding on each axis (x: 0..3.6 → 3.6 + 30).
    assert scene.cell.lattice[0][0] == pytest.approx(3.6 + 2 * BOX_PADDING)
    # No bonds by default (detector gated off).
    assert scene.bonds == []


def test_cif_to_scene_detects_bonds_when_small() -> None:
    scene = cif_to_scene(_multi_atom_cif(), detect_bonds_max=100)
    # 4 atoms is under the cap → covalent bonds inferred (backbone N-CA-C-O).
    assert len(scene.bonds) >= 2
    assert all(b.provenance == "inferred" for b in scene.bonds)


def test_cif_to_scene_empty_raises() -> None:
    with pytest.raises(ValueError, match="no _atom_site atoms"):
        cif_to_scene("data_empty\n")


def test_view_structure_converges_and_links(protein_store: Store) -> None:
    """get(view='structure') projects the fold into a structure ref + links it."""
    h = ProteinHandler(hub=Hub(store=protein_store))
    h.put(id="insulin-a", sequence=_INSULIN_A, engine="stub")

    resp = h.get(id="insulin-a", view="structure")
    assert "structure 'insulin-a-fold'" in resp.body
    assert "/structure/insulin-a-fold" in resp.body

    # A structure ref now exists...
    sref = protein_store.get_ref(kind="structure", id="insulin-a-fold")
    assert sref is not None
    # ...linked from the protein via has-fold-structure (asymmetric plugin
    # relation — its inverse mirrors via the gripe-160213 DB-sourced rewrite).
    pref = protein_store.get_ref(kind="protein", id="insulin-a")
    # (Plugin relations aren't in the `Relation` literal — cast; valid at runtime.)
    out = protein_store.links_for(
        pref.id, relation=cast(Relation, "has-fold-structure")
    )
    assert any(l.dst_ref_id == sref.id for l in out)
    # The structure finds the protein back through the inverse.
    back = protein_store.links_for(
        sref.id, relation=cast(Relation, "fold-structure-of")
    )
    assert any(l.src_ref_id == pref.id for l in back)

    # Second call is a cache hit (no rebuild).
    assert "cached" in h.get(id="insulin-a", view="structure").body


def test_view_structure_no_model(protein_store: Store) -> None:
    """A protein whose fold has no cif reports it rather than erroring."""
    ref = protein_store.insert_ref(
        kind="protein",
        slug="nomodel",
        title="nomodel",
        meta={
            "sequence": "ACDE",
            "status": "folded",
            "fold": {"name": "nomodel", "sequence": "ACDE", "cif": ""},
        },
    )
    assert ref is not None
    h = ProteinHandler(hub=Hub(store=protein_store))
    assert "no structure model" in h.get(id="nomodel", view="structure").body


# ─────────────────────────── dark-ship gate ───────────────────────────


def test_dark_ship_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_BIO_ENABLED", raising=False)
    assert ProteinHandler.spec.requires_env == ("PRECIS_BIO_ENABLED",)
    assert ProteinHandler.spec.is_available() is False
    monkeypatch.setenv("PRECIS_BIO_ENABLED", "1")
    assert ProteinHandler.spec.is_available() is True
    assert ProteinHandler.spec.can_own_jobs is True


# ─────────────────────────── helpers ───────────────────────────


class _FakeCtx:
    """Minimal DispatchContext double for the fold dispatch."""

    def __init__(self, *, store: Store, params: dict[str, Any]) -> None:
        self.store = store
        self.meta = {"params": params}
        self.status: str | None = None
        self.failure: str | None = None
        self.chunks: list[tuple[str, str]] = []
        self.meta_updates: dict[str, Any] = {}

    def record_failure(self, reason: str) -> None:
        self.failure = reason
        self.status = "failed"

    def set_status(self, value: str) -> None:
        self.status = value

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **fields: Any) -> None:
        self.meta_updates.update(fields)
