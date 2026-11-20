"""``electronic_analysis`` job_type + the descriptor surface (gr592999).

Proves the seam end-to-end *without a cluster or GPAW*: a stubbed
:data:`RUNNER` writes a fake ``result.json`` beside the staged POSCAR,
the dispatch records a ``fidelity='electronic'`` ``struct_runs`` row
(descriptors in ``params``, Bader in ``charges``), stamps
``meta['last_electronic']``, and the handler's ``view='electronic'`` /
``mode='analyze'`` / the frontier's ``mechanism_evidence`` flag read
it back. Container-side helpers are tested as pure functions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from ase.build import bulk
from ase.parallel import broadcast as ase_broadcast

import precis_dft._container.cli as cli
import precis_dft._container.gpaw_analysis as gpaw_analysis
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.structure import StructureHandler, _analyze_request_sha
from precis.structure.export import _grouped, to_poscar
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import (
    _BUILTIN_JOB_TYPES,
    electronic_analysis,
    get_job_type,
    struct_relax,
)

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, True]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
            {"op": "add_atom", "element": "Ta", "frac": [0.5, 0.5, 0.5]},
        ],
    }
)


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def _poscar_labels(scene) -> list[str]:
    order, groups = _grouped(scene)
    return [a.label for el in order for a in groups[el]]


def _fake_ctx(
    store, params: dict[str, Any], *, reserved: dict[str, Any] | None = None
) -> tuple[DispatchContext, list]:
    events: list[tuple[str, Any]] = []
    meta: dict[str, Any] = {"params": params}
    if reserved is not None:
        meta["reserved"] = reserved
    ctx = DispatchContext(
        store=store,
        ref_id=999,
        title="electronic_analysis",
        meta=meta,
        set_status=lambda s: events.append(("status", s)),
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **kw: events.append(("meta", kw)),
        record_failure=lambda r, **kw: events.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=lambda: False,
    )
    return ctx, events


def _analysis_params(structure, ident: str = "pd_pair") -> dict[str, Any]:
    """The params ``_dispatch_analyze`` mints — built here directly."""
    ref = structure.store.get_ref(kind="structure", id=ident)
    scene, _ = structure.store.structure_load(ref.id)
    return {
        "structure_ref_id": ref.id,
        "on_version": structure.store.structure_version(ref.id),
        "structure_sha": "deadbeef",
        "request_sha": "reqdeadbeef",
        "order": _poscar_labels(scene),
        "poscar_labels": _poscar_labels(scene),
        "poscar": to_poscar(scene),
        "analysis": {
            "dopant": ["aTa1"],
            "bonds": [{"name": "Pd-Ta", "a": "el:Pd", "b": "el:Ta"}],
        },
        "target_node": "spruce",
    }


def _stub_analysis_runner() -> Any:
    """A RUNNER that writes the container contract's success envelope."""

    def runner(argv, *, node, in_dir, out_dir, timeout=None):
        result = {
            "ok": True,
            "scalars": {"E_tot": -12.34, "fermi": -1.23},
            "descriptors": {
                "bader": {
                    "status": "computed",
                    "charges": {"aPd1": 0.08, "aPd2": 0.08, "aTa1": -0.9},
                },
                "dband": {
                    "dopant": {
                        "status": "computed",
                        "atoms": ["aTa1"],
                        "center_ev": -1.85,
                        "width_ev": 2.1,
                        "d_states": 9.8,
                    },
                    "surface": {
                        "status": "computed",
                        "atoms": ["aPd1", "aPd2"],
                        "center_ev": -2.01,
                        "width_ev": 2.34,
                        "d_states": 11.1,
                    },
                },
                "pdos": {
                    "dopant": {
                        "status": "computed",
                        "channel": "d",
                        "energies_ev": [-2.0, -1.0, 0.0],
                        "d": [0.1, 0.4, 0.2],
                    }
                },
                "cohp": {
                    "Pd-Ta": {
                        "status": "computed",
                        "icohp_ev": -1.55,
                        "pairs": [[0, 2], [1, 2]],
                    }
                },
            },
            "functional": "RPBE",
            "gpaw_version": "test",
        }
        Path(out_dir, "result.json").write_text(json.dumps(result), encoding="utf-8")
        return 0, "done\n"

    return runner


def _stub_stager(tmp_path: Path) -> Any:
    def stager(ref_id: int) -> tuple[str, str]:
        in_dir, out_dir = tmp_path / "in", tmp_path / "out"
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        return str(in_dir), str(out_dir)

    return stager


def _no_stale_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        struct_relax, "_container_still_present", lambda *a, **kw: False
    )


# ── container CLI + pure helpers ─────────────────────────────────────


class TestContainerCli:
    def test_gpaw_analysis_subcommand_routes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_run_cli(in_dir: str, out_dir: str) -> int:
            seen["in"], seen["out"] = in_dir, out_dir
            return 0

        monkeypatch.setattr(gpaw_analysis, "run_cli", fake_run_cli)
        rc = cli.main(
            ["gpaw-analysis", "--in", str(tmp_path / "i"), "--out", str(tmp_path / "o")]
        )
        assert rc == 0
        assert seen == {"in": str(tmp_path / "i"), "out": str(tmp_path / "o")}

    def test_run_cli_records_failure_without_gpaw(self, tmp_path: Path) -> None:
        """GPAW absent here: the failure must still land in result.json
        parseably (the host-side contract), not as a bare traceback."""
        in_dir, out_dir = tmp_path / "in", tmp_path / "out"
        in_dir.mkdir(parents=True)
        from precis_dft.structures import canonical_poscar

        (in_dir / "POSCAR").write_text(
            canonical_poscar(bulk("Pt", "fcc", a=3.92)), encoding="utf-8"
        )
        (in_dir / "params.json").write_text(
            json.dumps({"analysis": {"dopant": ["el:Pt"]}}), encoding="utf-8"
        )
        rc = gpaw_analysis.run_cli(str(in_dir), str(out_dir))
        assert rc == 1
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        assert result["ok"] is False
        assert "error" in result and "traceback" in result


class TestContainerHelpers:
    def test_label_map_requires_row_count(self) -> None:
        atoms = bulk("Pd", "fcc", a=3.9) * (2, 1, 1)
        assert gpaw_analysis._label_map(atoms, ["aPd1", "aPd2"]) == {
            "aPd1": 0,
            "aPd2": 1,
        }
        assert gpaw_analysis._label_map(atoms, ["aPd1"]) == {}

    def test_resolve_atom_set_label_and_element(self) -> None:
        atoms = bulk("Pd", "fcc", a=3.9) * (2, 1, 1)
        label_map = {"aPd1": 0, "aPd2": 1}
        assert gpaw_analysis._resolve_atom_set("aPd2", atoms, label_map) == [1]
        assert gpaw_analysis._resolve_atom_set("el:Pd", atoms, label_map) == [0, 1]
        assert gpaw_analysis._resolve_atom_set("el:Ta", atoms, label_map) == []
        assert gpaw_analysis._resolve_atom_set("typo", atoms, label_map) == []

    def test_resolve_bond_pairs_within_cutoff(self) -> None:
        atoms = bulk("Pd", "fcc", a=3.9) * (2, 1, 1)
        pairs = gpaw_analysis._resolve_bond_pairs(
            {"a": "el:Pd", "b": "el:Pd"}, atoms, {}, 1.3
        )
        # two Pd within bonding distance of each other
        assert pairs
        assert all(len(p) == 2 and p[0] < p[1] for p in pairs)

    def test_resolve_bond_pairs_empty_set(self) -> None:
        atoms = bulk("Pd", "fcc", a=3.9)
        assert (
            gpaw_analysis._resolve_bond_pairs(
                {"a": "el:Ta", "b": "el:Pd"}, atoms, {}, 1.3
            )
            == []
        )

    def test_dband_moments_symmetric_curve(self) -> None:
        # A d-band centred at +2 eV: a triangular PDOS peaked there.
        energies = [x * 0.1 for x in range(-100, 101)]
        ldos = [max(0.0, 1.0 - abs(e - 2.0)) for e in energies]
        out = gpaw_analysis._dband_moments(energies, ldos, fermi=0.0)
        assert out["center_ev"] == pytest.approx(2.0, abs=0.05)
        assert out["width_ev"] > 0
        assert out["d_states"] > 0

    def test_dband_moments_rejects_empty_curve(self) -> None:
        with pytest.raises(RuntimeError):
            gpaw_analysis._dband_moments([0.0, 1.0], [0.0, 0.0], fermi=0.0)

    def test_downsample_caps_points(self) -> None:
        xs = [float(x) for x in range(2000)]
        ys = list(xs)
        xs2, ys2 = gpaw_analysis._downsample(xs, ys)
        assert len(xs2) <= gpaw_analysis._CURVE_MAX_POINTS
        assert xs2[0] == xs[0] and xs2[-1] == xs[-1]

    def test_parse_acf_charges(self, tmp_path: Path) -> None:
        acf = tmp_path / "ACF.dat"
        acf.write_text(
            "# HEADER\n"
            "    1  0.0  0.0  0.0  10.1234  0.1  1.0\n"
            "    2  0.1  0.1  0.1  10.8765  0.1  1.0\n",
            encoding="utf-8",
        )
        assert gpaw_analysis._parse_acf_charges(acf, 2) == [10.1234, 10.8765]
        # count mismatch → None, never a truncated map
        assert gpaw_analysis._parse_acf_charges(acf, 3) is None


# ── job registry + dispatch ──────────────────────────────────────────


class TestRegistry:
    def test_spec_registered(self) -> None:
        assert "electronic_analysis" in _BUILTIN_JOB_TYPES
        spec = get_job_type("electronic_analysis")
        assert spec is not None
        assert spec.name == "electronic_analysis"
        assert "ssh_node" in spec.compatible_executors
        assert spec.dispatch is not None

    def test_validate_rejects_missing_structure(self, store) -> None:
        spec = get_job_type("electronic_analysis")
        assert spec is not None and spec.validate is not None
        with pytest.raises(BadInput):
            spec.validate(store, {"structure_ref_id": 424242})

    def test_validate_rejects_malformed_ref(self, store) -> None:
        spec = get_job_type("electronic_analysis")
        assert spec is not None and spec.validate is not None
        with pytest.raises(BadInput):
            spec.validate(store, {})


class TestDispatch:
    def test_success_records_descriptor_run(
        self, structure, tmp_path, monkeypatch
    ) -> None:
        structure.put(id="pd_pair", text=_PD)
        params = _analysis_params(structure)
        _no_stale_container(monkeypatch)
        monkeypatch.setattr(electronic_analysis, "STAGER", _stub_stager(tmp_path))
        monkeypatch.setattr(electronic_analysis, "RUNNER", _stub_analysis_runner())
        ctx, events = _fake_ctx(structure.store, params)
        electronic_analysis._dispatch(ctx, electronic_analysis.SPEC)

        assert ("status", "succeeded") in events
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        runs = structure.store.structure_electronic_runs(ref.id)
        assert len(runs) == 1
        run = runs[0]
        assert run["fidelity"] == "electronic"
        assert run["converged"] is True
        # The Bader map lands in the charges column (migration 0084's
        # charge-carrying slot); the full descriptor payload rides params.
        assert run["charges"]["bader"]["aTa1"] == pytest.approx(-0.9)
        desc = run["params"]["descriptors"]
        assert desc["dband"]["dopant"]["center_ev"] == pytest.approx(-1.85)
        assert desc["cohp"]["Pd-Ta"]["icohp_ev"] == pytest.approx(-1.55)
        # Design meta stamp: the frontier's mechanism-evidence read.
        last = ref.meta.get("last_electronic")
        assert last is not None and last["run_id"] == run["id"]
        assert last["structure_sha"] == "deadbeef"
        assert last["request_sha"] == "reqdeadbeef"
        assert set(last["present"]) == {"bader", "dband", "pdos", "cohp"}

    def test_run_carries_no_energy(self, structure, tmp_path, monkeypatch) -> None:
        """Deliberate contract (module docstring): the frontier's relax-
        energy channel must never read a single-point row — ``energy``
        stays NULL, the audit E_tot lives under params."""
        structure.put(id="pd_pair", text=_PD)
        params = _analysis_params(structure)
        _no_stale_container(monkeypatch)
        monkeypatch.setattr(electronic_analysis, "STAGER", _stub_stager(tmp_path))
        monkeypatch.setattr(electronic_analysis, "RUNNER", _stub_analysis_runner())
        ctx, _events = _fake_ctx(structure.store, params)
        electronic_analysis._dispatch(ctx, electronic_analysis.SPEC)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        run = [
            r
            for r in structure.store.structure_runs(ref.id)
            if r["fidelity"] == "electronic"
        ][0]
        assert run["energy"] is None
        desc_run = structure.store.structure_electronic_runs(ref.id)[0]
        assert desc_run["params"]["E_tot"] == pytest.approx(-12.34)

    def test_partial_descriptors_still_land(
        self, structure, tmp_path, monkeypatch
    ) -> None:
        """An honest ``unavailable`` family (e.g. no bader binary) never
        sinks the run — what computed still stamps."""
        structure.put(id="pd_pair", text=_PD)
        params = _analysis_params(structure)

        def runner(argv, *, node, in_dir, out_dir, timeout=None):
            result = {
                "ok": True,
                "scalars": {"E_tot": -12.34, "fermi": -1.23},
                "descriptors": {
                    "bader": {
                        "status": "unavailable",
                        "reason": "no 'bader' binary in the image",
                    },
                    "dband": {
                        "dopant": {
                            "status": "computed",
                            "center_ev": -1.8,
                            "width_ev": 2.0,
                            "d_states": 9.7,
                        }
                    },
                    "pdos": {},
                    "cohp": {},
                },
                "functional": "RPBE",
            }
            Path(out_dir, "result.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            return 0, "done\n"

        _no_stale_container(monkeypatch)
        monkeypatch.setattr(electronic_analysis, "STAGER", _stub_stager(tmp_path))
        monkeypatch.setattr(electronic_analysis, "RUNNER", runner)
        ctx, events = _fake_ctx(structure.store, params)
        electronic_analysis._dispatch(ctx, electronic_analysis.SPEC)
        assert ("status", "succeeded") in events
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        last = ref.meta["last_electronic"]
        assert last["present"] == ["dband"]
        assert "bader" in last["absent"]

    def test_result_not_ok_records_failure(
        self, structure, tmp_path, monkeypatch
    ) -> None:
        structure.put(id="pd_pair", text=_PD)
        params = _analysis_params(structure)

        def runner(argv, *, node, in_dir, out_dir, timeout=None):
            Path(out_dir, "result.json").write_text(
                json.dumps({"ok": False, "error": "SCF did not converge"}),
                encoding="utf-8",
            )
            return 0, "bombed\n"

        _no_stale_container(monkeypatch)
        monkeypatch.setattr(electronic_analysis, "STAGER", _stub_stager(tmp_path))
        monkeypatch.setattr(electronic_analysis, "RUNNER", runner)
        ctx, events = _fake_ctx(structure.store, params)
        electronic_analysis._dispatch(ctx, electronic_analysis.SPEC)
        fails = [e for e in events if e[0] == "fail"]
        assert len(fails) == 1
        assert "SCF did not converge" in fails[0][1]["reason"]
        assert fails[0][1]["failure_class"] == "non-convergence"

    def test_missing_result_records_infra(
        self, structure, tmp_path, monkeypatch
    ) -> None:
        structure.put(id="pd_pair", text=_PD)
        params = _analysis_params(structure)
        _no_stale_container(monkeypatch)
        monkeypatch.setattr(electronic_analysis, "STAGER", _stub_stager(tmp_path))
        monkeypatch.setattr(
            electronic_analysis,
            "RUNNER",
            lambda argv, **kw: (1, "docker blew up"),
        )
        ctx, events = _fake_ctx(structure.store, params)
        electronic_analysis._dispatch(ctx, electronic_analysis.SPEC)
        fails = [e for e in events if e[0] == "fail"]
        assert len(fails) == 1
        assert fails[0][1]["failure_class"] == "infra"

    def test_no_target_node_fails_infra(self, structure, tmp_path, monkeypatch) -> None:
        structure.put(id="pd_pair", text=_PD)
        params = _analysis_params(structure)
        params.pop("target_node")
        monkeypatch.setattr(struct_relax, "_NODE", None)
        ctx, events = _fake_ctx(structure.store, params)
        electronic_analysis._dispatch(ctx, electronic_analysis.SPEC)
        fails = [e for e in events if e[0] == "fail"]
        assert len(fails) == 1
        assert fails[0][1]["failure_class"] == "infra"
        assert "PRECIS_DFT_NODE" in fails[0][1]["reason"]

    def test_gpaw_analysis_command_on_argv(
        self, structure, tmp_path, monkeypatch
    ) -> None:
        """The container invocation must name ``gpaw-analysis``, not the
        relax subcommand."""
        structure.put(id="pd_pair", text=_PD)
        params = _analysis_params(structure)
        seen: list[list[str]] = []

        def runner(argv, *, node, in_dir, out_dir, timeout=None):
            seen.append(list(argv))
            Path(out_dir, "result.json").write_text(
                json.dumps({"ok": True, "scalars": {"E_tot": -1.0}, "descriptors": {}}),
                encoding="utf-8",
            )
            return 0, ""

        _no_stale_container(monkeypatch)
        monkeypatch.setattr(electronic_analysis, "STAGER", _stub_stager(tmp_path))
        monkeypatch.setattr(electronic_analysis, "RUNNER", runner)
        ctx, _events = _fake_ctx(structure.store, params)
        electronic_analysis._dispatch(ctx, electronic_analysis.SPEC)
        assert seen and "gpaw-analysis" in seen[0]
        assert "gpaw-relax" not in seen[0]


# ── mint path + view ─────────────────────────────────────────────────


class TestMintPath:
    def test_analyze_mints_job(self, structure, store, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        resp = structure.put(
            id="pd_pair",
            mode="analyze",
            args={
                "dopant": ["aTa1"],
                "bonds": [{"name": "Pd-Ta", "a": "el:Pd", "b": "el:Ta"}],
            },
        )
        assert "electronic_analysis" in resp.body
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE kind='job' AND parent_id=%s",
                (ref.id,),
            ).fetchone()
        assert row is not None
        meta = row[0]
        assert meta["job_type"] == "electronic_analysis"
        p = meta["params"]
        assert p["structure_ref_id"] == ref.id
        assert p["target_node"] == "spruce"
        assert p["analysis"]["dopant"] == ["aTa1"]
        assert len(p["poscar_labels"]) == 3

    def test_analyze_in_flight_collapses(self, structure, store, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        args = {"dopant": ["aTa1"]}
        first = structure.put(id="pd_pair", mode="analyze", args=args)
        second = structure.put(id="pd_pair", mode="analyze", args=args)
        assert "already in flight" in second.body
        assert second.ref_id == first.ref_id

    def test_analyze_completed_run_dedupes(self, structure, store, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        scene, _ = structure.store.structure_load(ref.id)
        from precis.structure import cache as relax_cache

        sha = relax_cache.structure_sha(scene)
        analysis = {"dopant": ["aTa1"]}
        version = int(ref.meta.get("version", 0))
        structure.store.structure_stamp_electronic(
            ref.id,
            on_version=version,
            summary={
                "run_id": 77,
                "on_version": version,
                "structure_sha": sha,
                "request_sha": _analyze_request_sha(
                    analysis=analysis,
                    dft=None,
                    poscar_labels=_poscar_labels(scene),
                    version=version,
                ),
                "analysis": analysis,
                "present": ["dband"],
                "absent": [],
            },
        )
        resp = structure.put(id="pd_pair", mode="analyze", args=analysis)
        assert "already on file" in resp.body
        assert "77" in resp.body
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM refs WHERE kind='job' AND parent_id=%s",
                (ref.id,),
            ).fetchone()[0]
        assert n == 0

    def test_analyze_rejects_unknown_arg(self, structure, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        with pytest.raises(BadInput):
            structure.put(id="pd_pair", mode="analyze", args={"bogus": 1})

    def test_analyze_rejects_bad_bond(self, structure, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        with pytest.raises(BadInput):
            structure.put(id="pd_pair", mode="analyze", args={"bonds": [{"name": "x"}]})

    def test_analyze_requires_id(self, structure, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        with pytest.raises(BadInput):
            structure.put(mode="analyze", args={})


class TestElectronicView:
    def test_absent_renders_evidence_absent(self, structure, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        resp = structure.get(id="pd_pair", view="electronic")
        assert "mechanism evidence: absent" in resp.body
        assert "mode='analyze'" in resp.body

    def _stamp_run(self, structure, slug: str) -> None:
        ref = structure.store.get_ref(kind="structure", id=slug)
        structure.store.structure_record_run(
            ref.id,
            fidelity="electronic",
            on_version=int(ref.meta.get("version", 0)),
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=None,
            model="RPBE",
            params={
                "E_tot": -12.34,
                "fermi": -1.23,
                "descriptors": {
                    "bader": {"status": "computed", "charges": {"aTa1": -0.9}},
                    "dband": {
                        "dopant": {
                            "status": "computed",
                            "center_ev": -1.85,
                            "width_ev": 2.1,
                        }
                    },
                    "pdos": {},
                    "cohp": {"Pd-Ta": {"status": "computed", "icohp_ev": -1.55}},
                },
            },
            charges={"bader": {"aTa1": -0.9}},
        )

    def test_view_renders_descriptor_table(self, structure, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        self._stamp_run(structure, "pd_pair")
        resp = structure.get(id="pd_pair", view="electronic")
        assert "mechanism evidence: present" in resp.body
        assert "d-band centre dopant" in resp.body
        assert "-1.850" in resp.body
        assert "ICOHP Pd-Ta" in resp.body
        assert "bader aTa1" in resp.body

    def test_compare_renders_side_by_side(self, structure, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_a", text=_PD)
        structure.put(id="pd_b", text=_PD)
        self._stamp_run(structure, "pd_a")
        self._stamp_run(structure, "pd_b")
        resp = structure.get(id="pd_a", view="electronic", args={"other": "pd_b"})
        assert "electronic compare" in resp.body
        assert "pd_a" in resp.body and "pd_b" in resp.body
        assert "d-band centre dopant" in resp.body

    def test_compare_missing_side_names_it(self, structure, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_a", text=_PD)
        structure.put(id="pd_b", text=_PD)
        self._stamp_run(structure, "pd_a")
        resp = structure.get(id="pd_a", view="electronic", args={"other": "pd_b"})
        assert "pd_b" in resp.body
        assert "mode='analyze'" in resp.body

    def _stamp_pdos_run(self, structure, slug: str) -> None:
        ref = structure.store.get_ref(kind="structure", id=slug)
        structure.store.structure_record_run(
            ref.id,
            fidelity="electronic",
            on_version=int(ref.meta.get("version", 0)),
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=None,
            model="RPBE",
            params={
                "descriptors": {
                    "dband": {
                        "dopant": {
                            "status": "computed",
                            "center_ev": -1.85,
                            "width_ev": 2.1,
                        }
                    },
                    "pdos": {
                        "dopant": {
                            "status": "computed",
                            "channel": "d",
                            "energies_ev": [-2.0, -1.0, 0.0],
                            "d": [0.1, 0.4, 0.2],
                        }
                    },
                }
            },
        )

    def test_view_renders_pdos_summary(self, structure, monkeypatch) -> None:
        """The dopant PDOS is a stored curve; the table cell is its
        summary — point count, energy window, peak — never dropped."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        self._stamp_pdos_run(structure, "pd_pair")
        resp = structure.get(id="pd_pair", view="electronic")
        assert "pdos dopant" in resp.body
        assert "3 pts" in resp.body
        assert "peak -1.00 eV" in resp.body

    def test_view_renders_unavailable_with_reason(self, structure, monkeypatch) -> None:
        """An ``unavailable`` family renders its reason in the table —
        the honest-absence contract, not a silently dropped row."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        structure.store.structure_record_run(
            ref.id,
            fidelity="electronic",
            on_version=int(ref.meta.get("version", 0)),
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=None,
            model="RPBE",
            params={
                "descriptors": {
                    "bader": {
                        "status": "unavailable",
                        "reason": "no 'bader' binary in the image",
                    },
                    "dband": {
                        "dopant": {
                            "status": "computed",
                            "center_ev": -1.85,
                            "width_ev": 2.1,
                        }
                    },
                    "pdos": {
                        "dopant": {
                            "status": "unavailable",
                            "reason": "no LDOS returned",
                        }
                    },
                    "cohp": {},
                }
            },
        )
        resp = structure.get(id="pd_pair", view="electronic")
        assert "no 'bader' binary in the image" in resp.body
        assert "no LDOS returned" in resp.body
        assert "unavailable" in resp.body

    def test_compare_renders_unavailable_reason(self, structure, monkeypatch) -> None:
        """The compare table's union of descriptors must carry the
        unavailable row + reason on the side that reported it."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_a", text=_PD)
        structure.put(id="pd_b", text=_PD)
        ref_b = structure.store.get_ref(kind="structure", id="pd_b")
        structure.store.structure_record_run(
            ref_b.id,
            fidelity="electronic",
            on_version=int(ref_b.meta.get("version", 0)),
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=None,
            model="RPBE",
            params={
                "descriptors": {
                    "bader": {
                        "status": "unavailable",
                        "reason": "no 'bader' binary in the image",
                    },
                    "pdos": {
                        "dopant": {
                            "status": "computed",
                            "channel": "d",
                            "energies_ev": [-1.0, 0.0],
                            "d": [0.3, 0.1],
                        }
                    },
                }
            },
        )
        self._stamp_pdos_run(structure, "pd_a")
        resp = structure.get(id="pd_a", view="electronic", args={"other": "pd_b"})
        assert "no 'bader' binary in the image" in resp.body
        assert "pdos dopant" in resp.body


# ── frontier mechanism-evidence flag ────────────────────────────────


class TestMechanismEvidence:
    def test_flag_absent_without_runs(self, structure, store) -> None:
        from precis.quest.frontier import _candidate_from_structure

        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        c = _candidate_from_structure(store, ref)
        assert c.flags["mechanism_evidence"] == "absent"

    def test_flag_absent_with_bare_electronic_run(self, structure, store) -> None:
        """A converged electronic run that carried no descriptor evidence
        (every family unavailable, or a row written without the stamp)
        is NOT mechanism evidence — 'present' requires computed
        descriptors on the current geometry."""
        from precis.quest.frontier import _candidate_from_structure

        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        structure.store.structure_record_run(
            ref.id,
            fidelity="electronic",
            on_version=int(ref.meta.get("version", 0)),
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=None,
        )
        c = _candidate_from_structure(store, ref)
        assert c.flags["mechanism_evidence"] == "absent"

    def test_flag_present_with_charged_electronic_run(self, structure, store) -> None:
        """A converged electronic run on the CURRENT version whose
        ``charges`` column carries the Bader map is direct evidence —
        covers a stamp write lost to a version race."""
        from precis.quest.frontier import _candidate_from_structure

        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        structure.store.structure_record_run(
            ref.id,
            fidelity="electronic",
            on_version=int(ref.meta.get("version", 0)),
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=None,
            charges={"bader": {"aTa1": -0.9}},
        )
        c = _candidate_from_structure(store, ref)
        assert c.flags["mechanism_evidence"] == "present"
        # and it never lands in `measures` — evidence is a flag, not a rank
        assert "mechanism_evidence" not in c.measures

    def test_flag_absent_when_evidence_is_stale(self, structure, store) -> None:
        """A stamp/run describing a superseded geometry must not keep
        reporting 'present' after an edit bumps the version."""
        from precis.quest.frontier import _candidate_from_structure

        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        version = int(ref.meta.get("version", 0))
        structure.store.structure_record_run(
            ref.id,
            fidelity="electronic",
            on_version=version,
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=None,
            charges={"bader": {"aTa1": -0.9}},
        )
        structure.store.structure_stamp_electronic(
            ref.id,
            on_version=version,
            summary={
                "run_id": 5,
                "on_version": version,
                "present": ["bader"],
                "absent": [],
            },
        )
        # Re-put bumps the version (geometry edit) — the old evidence no
        # longer describes the live geometry.
        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        assert int(ref.meta.get("version", 0)) != version
        c = _candidate_from_structure(store, ref)
        assert c.flags["mechanism_evidence"] == "absent"

    def test_flag_present_via_meta_stamp(self, structure, store) -> None:
        """A structure whose runs list doesn't reach the old analysis row
        still reads the versioned last_electronic stamp."""
        from precis.quest.frontier import _candidate_from_structure

        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        structure.store.structure_stamp_electronic(
            ref.id,
            on_version=int(ref.meta.get("version", 0)),
            summary={
                "run_id": 5,
                "on_version": int(ref.meta.get("version", 0)),
                "present": ["dband"],
                "absent": [],
            },
        )
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        c = _candidate_from_structure(store, ref, runs=[])
        assert c.flags["mechanism_evidence"] == "present"

    def test_frontier_summary_line(self, store) -> None:
        from precis.quest import tick as quest_tick
        from precis.quest.frontier import Candidate, FrontierResult

        fr = FrontierResult(
            objectives=[("energy", "min")],
            frontier=[
                Candidate(
                    ref_id=1,
                    handle="st1",
                    name="pd111",
                    measures={"energy": -1.2},
                    converged=True,
                    flags={"mechanism_evidence": "present"},
                )
            ],
            dominated=[
                Candidate(
                    ref_id=2,
                    handle="st2",
                    name="pd111-ta",
                    measures={"energy": -1.0},
                    converged=True,
                    flags={"mechanism_evidence": "absent"},
                )
            ],
        )
        out = quest_tick._frontier_summary(store, 0, fr=fr)
        assert "st1" in out and "mechanism evidence: present" in out
        assert "st2" in out and "mechanism evidence: absent" in out


# ── container descriptor internals (review td687448) ────────────────


class _FakeSetups(list):
    """The ``wfs.setups`` contract ``_cohp_for_bond`` reads: indexable by
    atom into objects with ``.nao``, plus ``.M_a`` (first AO index per
    atom) and ``.nao`` (total) — exactly what ``Setups.__init__``
    populates on both GPAW flavours."""

    def __init__(self, nao_per_atom: list[int]) -> None:
        super().__init__(SimpleNamespace(nao=n) for n in nao_per_atom)
        self.M_a = np.concatenate(([0], np.cumsum(nao_per_atom[:-1])))
        self.nao = int(sum(nao_per_atom))


def _fake_lcao_calc(*, eps_n: list[float], C_nM: Any, atom_of_orb: list[int]) -> Any:
    """A stand-in for the GPAW LCAO surface ``_cohp_for_bond`` reads:
    ``wfs.setups.M_a`` + ``wfs.setups[a].nao`` (AO blocks, on both GPAW
    flavours) and ``wfs.kpt_u`` (Hartree eigenvalues, k-weighted ``f_n``,
    ``C_nM``, ``s``/``k``). The Hamiltonian is passed in separately —
    ``_lcao_hamiltonian_ev`` owns that fetch. ``eps_n`` is given in eV
    and stored in Hartree like GPAW."""

    from ase.units import Ha

    n_atoms = max(atom_of_orb) + 1
    nao_per_atom = [atom_of_orb.count(a) for a in range(n_atoms)]
    kpt = SimpleNamespace(
        s=0,
        k=0,
        q=0,
        weight=2.0,  # must NOT be applied again on top of f_n
        eps_n=np.asarray(eps_n, dtype=float) / Ha,
        f_n=np.ones(len(eps_n)),
        C_nM=np.asarray(C_nM),
    )
    wfs = SimpleNamespace(mode="lcao", setups=_FakeSetups(nao_per_atom), kpt_u=[kpt])
    return SimpleNamespace(wfs=wfs)


class TestCohpContraction:
    """``_cohp_for_bond``'s per-state pair term must contract
    ``c*_nμ H_μν c_nν`` over the two AO index sets — never over the band
    index (review td687448: the old ``nm,mn,nk->n`` conflated band and
    bond-orbital axes — it crashed on ordinary dimensions and computed
    the wrong contraction on coincident ones)."""

    def _manual_cohp(self, C: Any, H: Any, a_orbs: list[int], b_orbs: list[int]):
        C = np.asarray(C)
        H = np.asarray(H)
        return np.array(
            [
                -sum(
                    np.conj(C[n, m]) * H[m, k] * C[n, k] for m in a_orbs for k in b_orbs
                ).real
                for n in range(C.shape[0])
            ]
        )

    def test_matches_manual_contraction(self) -> None:
        # nao=4, atom0 -> orbs {0,1}, atom1 -> orbs {2,3}; nbands=3 so
        # the band count differs from both AO blocks.
        C = np.array(
            [
                [0.5 + 0.1j, 0.2 + 0.0j, 0.3 - 0.1j, 0.1 + 0.0j],
                [0.1 + 0.0j, 0.4 + 0.3j, 0.2 + 0.0j, 0.5 - 0.2j],
                [0.0 + 0.0j, 0.1 - 0.1j, 0.6 + 0.0j, 0.2 + 0.1j],
            ]
        )
        H = np.zeros((4, 4))
        for i in range(4):
            H[i, i] = -1.0 + 0.5 * i
        H[0, 2] = H[2, 0] = 0.8
        H[0, 3] = H[3, 0] = 0.1
        H[1, 2] = H[2, 1] = -0.4
        H[1, 3] = H[3, 1] = 0.3
        calc = _fake_lcao_calc(
            eps_n=[-3.0, -1.0, 2.0], C_nM=C, atom_of_orb=[0, 0, 1, 1]
        )
        out = gpaw_analysis._cohp_for_bond(
            calc, np.asarray(H)[None, None], [0, 1], fermi=0.0
        )
        assert out["status"] == "computed"
        expected = self._manual_cohp(C, H, [0, 1], [2, 3])
        eps = [-3.0, -1.0, 2.0]
        order = np.argsort(eps)
        expected_sorted = expected[order]
        assert out["cohp"] == pytest.approx(list(expected_sorted), abs=1e-6)
        # ICOHP at E_F: both occupied states (eps < fermi), f_n = 1
        # (kpt.weight=2 in the fake must not rescale it).
        assert out["icohp_ev"] == pytest.approx(
            float(expected_sorted[0] + expected_sorted[1]), abs=1e-6
        )
        # energies are reported relative to the Fermi level
        assert out["energies_ev"] == pytest.approx([-3.0, -1.0, 2.0], abs=1e-4)

    def test_coincident_dims_also_correct(self) -> None:
        """nbands == nb (the coincident-dimension case the old einsum
        silently mis-contracted) must still give the right term."""
        C = np.array(
            [
                [0.4 + 0.0j, 0.1j, 0.3, 0.2],
                [0.2, 0.5 + 0.1j, 0.1, 0.4 - 0.1j],
            ]
        )
        H = np.zeros((4, 4))
        H[0, 2] = H[2, 0] = 0.7
        H[1, 3] = H[3, 1] = -0.5
        calc = _fake_lcao_calc(eps_n=[-1.0, 1.0], C_nM=C, atom_of_orb=[0, 0, 1, 1])
        out = gpaw_analysis._cohp_for_bond(
            calc, np.asarray(H)[None, None], [0, 1], fermi=0.0
        )
        expected = self._manual_cohp(C, H, [0, 1], [2, 3])
        assert out["cohp"] == pytest.approx(
            list(expected[np.argsort([-1.0, 1.0])]), abs=1e-6
        )


def _array_channel_comms() -> tuple[Any, Any]:
    """A (rank0, rank1) pair sharing a mailbox over an ndarray-only
    ``broadcast`` channel — the contract GPAW's ``world.broadcast``
    actually has. ``ase.parallel.broadcast`` pickles the result dict
    onto it; a raw ``world.broadcast(dict, 0)`` trips the isinstance
    check here, which is exactly the mpirun failure this guards."""

    box: list[np.ndarray] = []

    class _Rank:
        def __init__(self, rank: int) -> None:
            self.rank = rank
            self.size = 2

        def broadcast(self, a: np.ndarray, root: int = 0) -> None:
            assert isinstance(a, np.ndarray), "the channel takes ndarrays only"
            if self.rank == root:
                box.append(a.copy())
            else:
                a[...] = box.pop(0)

    return _Rank(0), _Rank(1)


class TestBaderCharges:
    """``_bader_charges`` — the all-electron cube is partitioned AND
    integrated by ``bader``, so the net charge is Z − count (never a
    PAW valence count), and only rank 0 touches the shared files."""

    def _fake_calc(self):
        return SimpleNamespace(get_all_electron_density=lambda **kw: np.ones((4, 4, 4)))

    def _acf_writer(self, charges: list[float]):
        def fake_run(argv, **kw):
            assert "-ref" not in argv
            acf = Path(kw["cwd"]) / "ACF.dat"
            acf.write_text(
                "# header\n"
                + "".join(
                    f"    {i + 1}  0.0  0.0  0.0  {c:.4f}  0.1  1.0\n"
                    for i, c in enumerate(charges)
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return fake_run

    def test_net_charge_is_nuclear_minus_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        atoms = bulk("Pd", "sc", a=3.9) * (2, 1, 1)  # Z = 46
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bader")
        monkeypatch.setattr(subprocess, "run", self._acf_writer([45.5, 46.25]))
        out = gpaw_analysis._bader_charges(
            atoms, self._fake_calc(), tmp_path, ["aPd1", "aPd2"]
        )
        assert out["status"] == "computed"
        assert out["charges"]["aPd1"] == pytest.approx(0.5)
        assert out["charges"]["aPd2"] == pytest.approx(-0.25)
        assert (tmp_path / "density.cube").exists()

    def test_cube_density_written_in_atomic_units(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ASE writes the cube grid in Bohr and bader assumes the data is
        Bohr^-3, but ``get_all_electron_density`` returns Å^-3 — the
        data block must be scaled by Bohr³ or every integrated electron
        count (and so every net charge) is inflated by Bohr^-3."""
        from ase.io.cube import read_cube
        from ase.units import Bohr

        atoms = bulk("Pd", "sc", a=3.9)
        density = np.linspace(0.2, 1.8, 64).reshape(4, 4, 4)
        calc = SimpleNamespace(get_all_electron_density=lambda **kw: density)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bader")
        monkeypatch.setattr(subprocess, "run", self._acf_writer([46.0]))
        gpaw_analysis._bader_charges(atoms, calc, tmp_path, ["aPd1"])
        with (tmp_path / "density.cube").open(encoding="utf-8") as fh:
            cube = read_cube(fh)
        assert cube["data"] == pytest.approx(density * Bohr**3)
        # The physical check: data x voxel volume (in Bohr³, as the file
        # declares) still sums to the electron count the Å^-3 density
        # integrates to over the same voxels.
        voxel_bohr3 = abs(np.linalg.det(cube["spacing"])) / Bohr**3
        electrons = float(cube["data"].sum()) * voxel_bohr3
        assert electrons == pytest.approx(
            float(density.sum()) * abs(np.linalg.det(cube["spacing"]))
        )

    def test_rank0_only_touches_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under MPI every rank joins the collective density gather, but
        the cube, the subprocess and ACF.dat belong to rank 0 alone —
        the others decode rank 0's dict off the broadcast channel."""
        atoms = bulk("Pd", "sc", a=3.9)
        calls = {"density": 0, "run": 0}

        def _density(**kw):
            calls["density"] += 1
            return np.ones((4, 4, 4))

        calc = SimpleNamespace(get_all_electron_density=_density)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bader")

        def _boom(*a, **kw):
            calls["run"] += 1
            raise AssertionError("non-rank0 must never launch bader")

        monkeypatch.setattr(subprocess, "run", _boom)
        canned = {"status": "computed", "charges": {"aPd1": 0.5}}
        root, nonroot = _array_channel_comms()
        # Stage the wire bytes exactly as rank 0's real broadcast sends
        # them — the non-root run then decodes the same dict.
        ase_broadcast(canned, root=0, comm=root)
        monkeypatch.setattr(gpaw_analysis, "_world", lambda: nonroot)
        out = gpaw_analysis._bader_charges(atoms, calc, tmp_path, ["aPd1"])
        assert calls["density"] == 1  # collective gather still joined
        assert calls["run"] == 0
        assert not (tmp_path / "density.cube").exists()
        assert out == canned

    def test_rank0_broadcasts_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        atoms = bulk("Pd", "sc", a=3.9)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bader")
        monkeypatch.setattr(subprocess, "run", self._acf_writer([46.0]))
        root, nonroot = _array_channel_comms()
        monkeypatch.setattr(gpaw_analysis, "_world", lambda: root)
        out = gpaw_analysis._bader_charges(atoms, self._fake_calc(), tmp_path, ["aPd1"])
        assert out["charges"]["aPd1"] == pytest.approx(0.0)
        # A peer rank decodes the identical dict from the wire bytes —
        # proof the result rode the ndarray channel a real GPAW
        # communicator exposes, not an object-returning fake.
        assert ase_broadcast(None, root=0, comm=nonroot) == out

    def test_rank0_exception_broadcasts_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rank-0 Bader failure — here the subprocess timeout — must be
        caught, wrapped in the ``unavailable`` envelope and *broadcast*.
        Letting it raise leaves every peer rank blocked on the channel
        (review td687448)."""
        atoms = bulk("Pd", "sc", a=3.9)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bader")

        def _timeout(*a: Any, **kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=["bader"], timeout=3600)

        monkeypatch.setattr(subprocess, "run", _timeout)
        root, nonroot = _array_channel_comms()
        monkeypatch.setattr(gpaw_analysis, "_world", lambda: root)
        out = gpaw_analysis._bader_charges(atoms, self._fake_calc(), tmp_path, ["aPd1"])
        assert out["status"] == "unavailable"
        assert "timed out" in out["reason"]
        # The envelope rode the wire: a peer rank decodes the same dict
        # instead of blocking on the broadcast.
        assert ase_broadcast(None, root=0, comm=nonroot) == out

    def test_rank0_exception_single_rank_returns_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Serial run: a rank-0 failure (e.g. the cube write) still
        surfaces as the envelope, not a raised exception."""
        atoms = bulk("Pd", "sc", a=3.9)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bader")

        def _boom(*a: Any, **kw: Any) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(subprocess, "run", _boom)
        out = gpaw_analysis._bader_charges(atoms, self._fake_calc(), tmp_path, ["aPd1"])
        assert out["status"] == "unavailable"
        assert "OSError" in out["reason"]
        assert "disk full" in out["reason"]


# ── dft-block request identity (review td687448) ─────────────────────


class TestDftIdentity:
    def test_dft_block_changes_in_flight_identity(
        self, structure, store, monkeypatch
    ) -> None:
        """Same geometry + same descriptor spec but a different ``dft``
        block is different physics — it must mint its own job, not
        collapse onto the in-flight one."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        spec = {"dopant": ["aTa1"]}
        first = structure.put(
            id="pd_pair",
            mode="analyze",
            args={**spec, "dft": {"functional": "RPBE"}},
        )
        other = structure.put(
            id="pd_pair",
            mode="analyze",
            args={**spec, "dft": {"functional": "PBE"}},
        )
        assert "already in flight" not in other.body
        assert other.ref_id != first.ref_id
        same = structure.put(
            id="pd_pair",
            mode="analyze",
            args={**spec, "dft": {"functional": "RPBE"}},
        )
        assert "already in flight" in same.body
        assert same.ref_id == first.ref_id

    def test_completed_dedupe_matches_dft(self, structure, store, monkeypatch) -> None:
        """The ``last_electronic`` stamp records the ``dft`` block it was
        computed under; a re-request with different settings mints, an
        identical one dedupes."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        scene, _ = structure.store.structure_load(ref.id)
        from precis.structure import cache as relax_cache

        sha = relax_cache.structure_sha(scene)
        analysis = {"dopant": ["aTa1"]}
        version = int(ref.meta.get("version", 0))
        structure.store.structure_stamp_electronic(
            ref.id,
            on_version=version,
            summary={
                "run_id": 77,
                "on_version": version,
                "structure_sha": sha,
                "request_sha": _analyze_request_sha(
                    analysis=analysis,
                    dft={"functional": "PBE"},
                    poscar_labels=_poscar_labels(scene),
                    version=version,
                ),
                "analysis": analysis,
                "dft": {"functional": "PBE"},
                "present": ["dband"],
                "absent": [],
            },
        )
        resp = structure.put(
            id="pd_pair",
            mode="analyze",
            args={**analysis, "dft": {"functional": "RPBE"}},
        )
        assert "already on file" not in resp.body
        resp2 = structure.put(
            id="pd_pair",
            mode="analyze",
            args={**analysis, "dft": {"functional": "PBE"}},
        )
        assert "already on file" in resp2.body
        assert "77" in resp2.body


# ── version-scoped reuse (review td687448) ───────────────────────────


_PD_RELABELLED = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, True]},
        "ops": [
            {
                "op": "add_atom",
                "element": "Pd",
                "frac": [0.0, 0.0, 0.0],
                "label": "surf1",
            },
            {
                "op": "add_atom",
                "element": "Pd",
                "frac": [0.26, 0.0, 0.0],
                "label": "surf2",
            },
            {
                "op": "add_atom",
                "element": "Ta",
                "frac": [0.5, 0.5, 0.5],
                "label": "dop1",
            },
        ],
    }
)


class TestVersionScopedReuse:
    """gr592999 review td687448: ``structure_sha`` is label-free and a
    re-put bumps ``meta.version`` even on identical geometry, while the
    stamp, the view and the frontier only count evidence on the LIVE
    version. Reuse must therefore key on version + atom-label identity —
    otherwise reanalysis dedupes onto a run/job whose stamp can never
    land and ``mechanism_evidence`` stays absent forever."""

    def _job_on_versions(self, store, ref_id: int) -> list[int]:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT meta FROM refs WHERE kind='job' AND parent_id=%s",
                (ref_id,),
            ).fetchall()
        return sorted(int(r[0]["params"]["on_version"]) for r in rows)

    def test_request_sha_covers_version_and_labels(self) -> None:
        """The request identity is spec + dft + label map + version: a
        bump or a relabel alone changes it even with geometry and spec
        identical."""
        kwargs: dict[str, Any] = {
            "analysis": {"dopant": ["aTa1"]},
            "dft": None,
            "poscar_labels": ["aPd1", "aPd2", "aTa1"],
        }
        base = _analyze_request_sha(version=3, **kwargs)
        assert base == _analyze_request_sha(version=3, **kwargs)
        assert base != _analyze_request_sha(version=4, **kwargs)
        assert base != _analyze_request_sha(
            version=3, **{**kwargs, "poscar_labels": ["x1", "x2", "x3"]}
        )

    def test_version_bump_remints_in_flight(
        self, structure, store, monkeypatch
    ) -> None:
        """An identical-geometry re-put bumps the version; the next
        analyze must mint a fresh job for the live version instead of
        collapsing onto the superseded in-flight one (whose stamp would
        be a version-gated no-op)."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        args = {"dopant": ["aTa1"]}
        first = structure.put(id="pd_pair", mode="analyze", args=args)
        assert "dispatched" in first.body
        structure.put(id="pd_pair", text=_PD)  # identical geometry → v2
        second = structure.put(id="pd_pair", mode="analyze", args=args)
        assert "already in flight" not in second.body
        assert second.ref_id != first.ref_id
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        version = int(ref.meta.get("version", 0))
        assert self._job_on_versions(store, ref.id) == [version - 1, version]
        # …but a re-request at the live version still collapses honestly.
        third = structure.put(id="pd_pair", mode="analyze", args=args)
        assert "already in flight" in third.body
        assert third.ref_id == second.ref_id

    def test_version_bump_defeats_completed_dedupe(
        self, structure, store, monkeypatch
    ) -> None:
        """A ``last_electronic`` stamp from a superseded version must not
        answer a new request — the fresh analysis mints a job on the live
        version so evidence can actually land."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        scene, _ = structure.store.structure_load(ref.id)
        from precis.structure import cache as relax_cache

        analysis = {"dopant": ["aTa1"]}
        version = int(ref.meta.get("version", 0))
        structure.store.structure_stamp_electronic(
            ref.id,
            on_version=version,
            summary={
                "run_id": 77,
                "on_version": version,
                "structure_sha": relax_cache.structure_sha(scene),
                "request_sha": _analyze_request_sha(
                    analysis=analysis,
                    dft=None,
                    poscar_labels=_poscar_labels(scene),
                    version=version,
                ),
                "analysis": analysis,
                "present": ["dband"],
                "absent": [],
            },
        )
        structure.put(id="pd_pair", text=_PD)  # identical geometry → v2
        resp = structure.put(id="pd_pair", mode="analyze", args=analysis)
        assert "already on file" not in resp.body
        assert "dispatched" in resp.body
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        assert self._job_on_versions(store, ref.id) == [int(ref.meta.get("version", 0))]

    def test_relabelled_identical_geometry_remints(
        self, structure, store, monkeypatch
    ) -> None:
        """Same sha, different label map: selectors like ``'aTa1'``
        resolve against POSCAR row order, so a re-put that relabels the
        atoms is a different request even when the spec's selector string
        is element-scoped."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        args = {"dopant": ["el:Ta"]}  # label-free selector — spec identical
        first = structure.put(id="pd_pair", mode="analyze", args=args)
        structure.put(id="pd_pair", text=_PD_RELABELLED)
        second = structure.put(id="pd_pair", mode="analyze", args=args)
        assert "already in flight" not in second.body
        assert second.ref_id != first.ref_id

    def test_completed_dedupe_rejects_foreign_label_map(
        self, structure, store, monkeypatch
    ) -> None:
        """Same version, same sha, same spec — but the stamp's request
        identity names a different label map: it must not answer."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        scene, _ = structure.store.structure_load(ref.id)
        from precis.structure import cache as relax_cache

        analysis = {"dopant": ["el:Ta"]}
        version = int(ref.meta.get("version", 0))
        structure.store.structure_stamp_electronic(
            ref.id,
            on_version=version,
            summary={
                "run_id": 77,
                "on_version": version,
                "structure_sha": relax_cache.structure_sha(scene),
                "request_sha": _analyze_request_sha(
                    analysis=analysis,
                    dft=None,
                    poscar_labels=["x1", "x2", "x3"],
                    version=version,
                ),
                "analysis": analysis,
                "present": ["dband"],
                "absent": [],
            },
        )
        resp = structure.put(id="pd_pair", mode="analyze", args=analysis)
        assert "already on file" not in resp.body
        assert "dispatched" in resp.body


# ── honest evidence verdicts (review td687448) ───────────────────────


class TestEvidenceVerdict:
    def test_dispatch_all_unavailable_stays_absent(
        self, structure, tmp_path, monkeypatch
    ) -> None:
        """A converged single point whose every descriptor family came
        back ``unavailable`` is a finished run, NOT mechanism evidence."""
        structure.put(id="pd_pair", text=_PD)
        params = _analysis_params(structure)

        def runner(argv, *, node, in_dir, out_dir, timeout=None):
            result = {
                "ok": True,
                "scalars": {"E_tot": -12.34, "fermi": -1.23},
                "descriptors": {
                    "bader": {"status": "unavailable", "reason": "no binary"},
                    "dband": {
                        "dopant": {
                            "status": "unavailable",
                            "reason": "LCAO API drift",
                        }
                    },
                    "pdos": {},
                    "cohp": {},
                },
                "functional": "RPBE",
            }
            Path(out_dir, "result.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            return 0, "done\n"

        _no_stale_container(monkeypatch)
        monkeypatch.setattr(electronic_analysis, "STAGER", _stub_stager(tmp_path))
        monkeypatch.setattr(electronic_analysis, "RUNNER", runner)
        ctx, events = _fake_ctx(structure.store, params)
        electronic_analysis._dispatch(ctx, electronic_analysis.SPEC)
        assert ("status", "succeeded") in events
        metas = [kw for tag, kw in events if tag == "meta"]
        assert any(m.get("mechanism_evidence") == "absent" for m in metas)
        assert not any(m.get("mechanism_evidence") == "present" for m in metas)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        last = ref.meta["last_electronic"]
        assert last["present"] == []
        assert last["on_version"] == params["on_version"]
        assert last["dft"] == {}

    def test_view_stale_run_reports_absent(self, structure, monkeypatch) -> None:
        """A descriptor run describing a superseded geometry is stale —
        the view must not keep reporting 'present' after an edit."""
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        TestElectronicView()._stamp_run(structure, "pd_pair")
        # geometry edit bumps the version
        structure.put(id="pd_pair", text=_PD)
        resp = structure.get(id="pd_pair", view="electronic")
        assert "mechanism evidence: absent" in resp.body
        assert "stale" in resp.body

    def test_view_all_unavailable_reports_absent(self, structure, monkeypatch) -> None:
        monkeypatch.setenv("PRECIS_DFT_NODE", "spruce")
        structure.put(id="pd_pair", text=_PD)
        ref = structure.store.get_ref(kind="structure", id="pd_pair")
        structure.store.structure_record_run(
            ref.id,
            fidelity="electronic",
            on_version=int(ref.meta.get("version", 0)),
            converged=True,
            n_steps=0,
            max_disp=0.0,
            energy=None,
            model="RPBE",
            params={
                "descriptors": {
                    "bader": {"status": "unavailable", "reason": "no binary"},
                    "dband": {},
                    "pdos": {},
                    "cohp": {},
                }
            },
        )
        resp = structure.get(id="pd_pair", view="electronic")
        assert "mechanism evidence: absent" in resp.body
        assert "no descriptors" in resp.body


# ── sweeper container naming (review td687448) ───────────────────────


def _capture_kill(killed: list[tuple[int, str | None]]) -> Any:
    """A ``kill_container`` stand-in that records ``(ref_id, node)`` and
    reports verified-removed."""

    def _kill(rid: int, *, node: str | None = None, **kw: Any) -> bool:
        killed.append((rid, node))
        return True

    return _kill


class TestSweeperContainerRef:
    @pytest.mark.parametrize("job_type", ["struct_relax", "electronic_analysis"])
    def test_kills_container_by_structure_ref(self, monkeypatch, job_type: str) -> None:
        """The container is ``precis-job-<structure_ref_id>``, not the
        job id — a sweep that passed the job ref would leave the real
        container orphaned on the node."""
        from precis.workers import sweeper

        killed: list[tuple[int, str | None]] = []
        monkeypatch.setattr(struct_relax, "kill_container", _capture_kill(killed))
        sweeper._kill_job_container(
            999,
            {
                "job_type": job_type,
                "params": {"structure_ref_id": 42, "target_node": "spruce"},
            },
        )
        assert killed == [(42, "spruce")]

    def test_falls_back_to_job_ref_without_structure_ref(self, monkeypatch) -> None:
        """Params without ``structure_ref_id`` (a legacy job row) still
        attempt the job-id name rather than raising."""
        from precis.workers import sweeper

        killed: list[tuple[int, str | None]] = []
        monkeypatch.setattr(struct_relax, "kill_container", _capture_kill(killed))
        sweeper._kill_job_container(
            999, {"job_type": "struct_relax", "params": {"target_node": "spruce"}}
        )
        assert killed == [(999, "spruce")]


class TestCohpInterface:
    def test_unreadable_basis_map_is_an_error(self) -> None:
        """A calc without the LCAO basis interface raises (the caller
        records the bond ``unavailable``) rather than a wrong curve."""
        calc = SimpleNamespace(wfs=SimpleNamespace(kpt_u=[]))
        with pytest.raises(RuntimeError, match="basis map"):
            gpaw_analysis._cohp_for_bond(
                calc, np.zeros((1, 1, 4, 4)), [0, 1], fermi=0.0
            )

    def test_noncollinear_spin_is_an_error(self) -> None:
        """``kpt.s is None`` (a 4-component non-collinear run) has no
        H_skMM slot to index — honest raise, not a silent spin-0 read."""
        calc = _fake_lcao_calc(
            eps_n=[-1.0], C_nM=[[1.0, 0.0, 0.0, 0.0]], atom_of_orb=[0, 0, 1, 1]
        )
        calc.wfs.kpt_u[0].s = None
        with pytest.raises(RuntimeError, match="non-collinear"):
            gpaw_analysis._cohp_for_bond(
                calc, np.zeros((1, 1, 4, 4)), [0, 1], fermi=0.0
            )


class TestLcaoHamiltonian:
    """``_lcao_hamiltonian_ev`` — the fetch must go through the real
    interface: the ``gpaw.lcao.tools`` *module function*, which returns
    ``(H_skMM, S_kMM)`` in eV on world rank 0 and ``(None, None)`` on
    slaves — the array then rides the broadcast to every rank."""

    def _fake_tools(self, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
        """Inject ``gpaw.lcao.tools.get_lcao_hamiltonian`` without GPAW —
        sys.modules short-circuits the ``from``-import, so the fake
        honours the real call shape: ``(calc) -> (H_skMM, S_kMM)``."""
        import sys
        import types

        tools = types.ModuleType("gpaw.lcao.tools")
        setattr(tools, "get_lcao_hamiltonian", lambda calc: result)
        monkeypatch.setitem(sys.modules, "gpaw.lcao.tools", tools)

    def _serial_calc(self) -> Any:
        wfs = SimpleNamespace(mode="lcao", world=SimpleNamespace(rank=0, size=1))
        return SimpleNamespace(wfs=wfs)

    def test_returns_h_on_serial_rank0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        H = np.arange(8.0).reshape(1, 1, 2, 4)
        self._fake_tools(monkeypatch, (H, None))
        out = gpaw_analysis._lcao_hamiltonian_ev(self._serial_calc())
        assert out is H

    def test_slaves_pick_up_rank0s_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On MPI slaves the tool returns ``(None, None)`` — the
        Hamiltonian arrives over the broadcast, here the ndarray channel
        a real GPAW communicator exposes."""
        H = np.arange(8.0).reshape(1, 1, 2, 4)
        root, nonroot = _array_channel_comms()
        self._fake_tools(monkeypatch, (H, None))
        root_calc = SimpleNamespace(wfs=SimpleNamespace(mode="lcao", world=root))
        assert gpaw_analysis._lcao_hamiltonian_ev(root_calc) is H

        self._fake_tools(monkeypatch, (None, None))
        slave_calc = SimpleNamespace(wfs=SimpleNamespace(mode="lcao", world=nonroot))
        assert gpaw_analysis._lcao_hamiltonian_ev(slave_calc) == pytest.approx(H)

    def test_non_lcao_mode_raises(self) -> None:
        calc = SimpleNamespace(
            wfs=SimpleNamespace(mode="pw", world=SimpleNamespace(rank=0, size=1))
        )
        with pytest.raises(RuntimeError, match="LCAO"):
            gpaw_analysis._lcao_hamiltonian_ev(calc)

    def test_missing_gpaw_tools_raises(self) -> None:
        """No GPAW in this environment — the import failure must be the
        honest ``unavailable`` reason, not an ImportError."""
        with pytest.raises(RuntimeError, match="gpaw.lcao.tools"):
            gpaw_analysis._lcao_hamiltonian_ev(self._serial_calc())


class TestStateGather:
    """``_gather_states`` — under MPI each rank holds only its local
    slice of ``wfs.kpt_u``; the ICOHP integral needs the union. The
    comms come from :func:`_kpt_band_comms`: ``kptband_comm`` on legacy
    GPAW, ``bd.comm`` + ``kd.comm`` on the new ``FakeWFS``."""

    def test_single_comm_collects_peer_states(self) -> None:
        """One ``kptband_comm``-style comm: every rank contributes its
        slice exactly once."""
        root, nonroot = _array_channel_comms()
        # Stage rank 1's local slice on the wire (it sends as root=1),
        # then run rank 0's gather: r=0 sends its own, r=1 decodes it.
        ase_broadcast(([1.1], [2.1], [0.1]), root=1, comm=nonroot)
        eps, cohp, occ = gpaw_analysis._gather_states(([1.0], [2.0], [0.0]), [root])
        assert (eps, cohp, occ) == ([1.0, 1.1], [2.0, 2.1], [0.0, 0.1])

    def test_bd_kd_pair_collects_grid_once(self) -> None:
        """The new-API fallback — a ``bd.comm`` gather then a ``kd.comm``
        gather — yields the full k-point × band grid with no
        duplication."""
        bd_root, bd_peer = _array_channel_comms()
        kd_root, kd_peer = _array_channel_comms()
        # rank 0's local slice is one k-point's band block; rank 1
        # contributes its bd-peer slice and its kd-peer slice.
        ase_broadcast(([1.1], [2.1], [0.1]), root=1, comm=bd_peer)
        ase_broadcast(([1.0, 1.1], [2.0, 2.1], [0.0, 0.1]), root=1, comm=kd_peer)
        eps, cohp, occ = gpaw_analysis._gather_states(
            ([1.0], [2.0], [0.0]), [bd_root, kd_root]
        )
        assert eps == [1.0, 1.1, 1.0, 1.1]
        assert cohp == [2.0, 2.1, 2.0, 2.1]
        assert occ == [0.0, 0.1, 0.0, 0.1]

    def test_serial_is_a_noop(self) -> None:
        comm = SimpleNamespace(rank=0, size=1)
        states = ([1.0], [2.0], [0.5])
        assert gpaw_analysis._gather_states(states, [comm]) == states
        assert gpaw_analysis._gather_states(states, []) == states

    def test_kptband_preferred_over_bd_kd(self) -> None:
        kb = SimpleNamespace(rank=0, size=1)
        wfs = SimpleNamespace(
            kptband_comm=kb,
            bd=SimpleNamespace(comm=SimpleNamespace(rank=0, size=2)),
            kd=SimpleNamespace(comm=SimpleNamespace(rank=0, size=2)),
        )
        assert gpaw_analysis._kpt_band_comms(wfs) == [kb]
        wfs2 = SimpleNamespace(bd=wfs.bd, kd=wfs.kd)
        assert gpaw_analysis._kpt_band_comms(wfs2) == [
            wfs.bd.comm,
            wfs.kd.comm,
        ]


class TestLdosReader:
    """``_ldos_reader`` picks the LDOS surface the running GPAW actually
    has: ``get_orbital_ldos`` on the legacy calculator, ``calc.dos()`` →
    ``DOSCalculator.raw_pdos`` on the new ``ASECalculator``."""

    def test_legacy_path_delegates(self) -> None:
        calls: list[tuple] = []

        def _ldos(a: int, *, angular: str, spin: int, width: float):
            calls.append((a, angular, spin, width))
            return [0.0, 1.0], [1.0 + spin, 2.0 + spin]

        calc = SimpleNamespace(get_orbital_ldos=_ldos, get_number_of_spins=lambda: 2)
        reader = gpaw_analysis._ldos_reader(calc)
        e, w = gpaw_analysis._orbital_ldos(calc, reader, 5, "d")
        assert calls == [
            (5, "d", 0, gpaw_analysis._PDOS_WIDTH_EV),
            (5, "d", 1, gpaw_analysis._PDOS_WIDTH_EV),
        ]
        assert e == [0.0, 1.0]
        assert w == pytest.approx([1.0 + 2.0, 2.0 + 3.0])

    def test_new_api_path_uses_raw_pdos(self) -> None:
        """On the new ``ASECalculator`` there is no ``get_orbital_ldos``
        — the same projector weights fold through ``calc.dos()`` onto an
        absolute-eV grid (``shift_fermi_level=False``)."""
        calls: list[tuple] = []
        dos_kwargs: list[dict] = []

        class _FakeDos:
            eig_skn = np.array([[[-5.0, -1.0, 2.0]]])

            def get_energies(self, emin=None, emax=None, npoints=100):
                calls.append(("grid", emin, emax, npoints))
                return np.linspace(emin, emax, npoints)

            def raw_pdos(self, energies, a, l, m=None, spin=None, width=0.1):
                calls.append((a, l, spin, width))
                return np.zeros(len(energies)) + (a + 1) + spin

        fake_dos = _FakeDos()

        def _dos(**kw: object) -> _FakeDos:
            dos_kwargs.append(kw)
            return fake_dos

        calc = SimpleNamespace(dos=_dos, get_number_of_spins=lambda: 2)
        reader = gpaw_analysis._ldos_reader(calc)
        assert dos_kwargs == [{"shift_fermi_level": False}]
        e, w = gpaw_analysis._orbital_ldos(calc, reader, 3, "d")
        # 'd' → l=2, one call per spin, our smearing width.
        assert calls[1:] == [
            (3, 2, 0, gpaw_analysis._PDOS_WIDTH_EV),
            (3, 2, 1, gpaw_analysis._PDOS_WIDTH_EV),
        ]
        assert w == pytest.approx([4 + 0 + 4 + 1] * len(e))
        # The grid spans the eigenvalue range plus the smearing pad.
        assert min(e) <= -5.0 - gpaw_analysis._PDOS_WIDTH_EV
        assert max(e) >= 2.0 + gpaw_analysis._PDOS_WIDTH_EV

    def test_unknown_angular_raises_on_new_path(self) -> None:
        class _FakeDos:
            eig_skn = np.array([[[0.0, 1.0]]])

            def get_energies(self, emin=None, emax=None, npoints=100):
                return np.linspace(emin, emax, npoints)

        calc = SimpleNamespace(dos=lambda **kw: _FakeDos())
        reader = gpaw_analysis._ldos_reader(calc)
        with pytest.raises(RuntimeError, match="angular channel"):
            reader(0, "spdf", 0)

    def test_no_interface_raises(self) -> None:
        with pytest.raises(RuntimeError, match="no LDOS interface"):
            gpaw_analysis._ldos_reader(SimpleNamespace())
