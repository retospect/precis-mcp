"""In-container ``precis-dft-run gpaw-relax`` entrypoint.

The actual GPAW relax needs the image; here we test what's reachable
without GPAW: the pure params→GPAW-kwargs mapping, the CLI routing,
and the failure path (GPAW absent ⇒ a recorded failure + rc=1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ase.build import bulk

import precis_dft._container.cli as cli
import precis_dft._container.gpaw_relax as gpaw_relax
from precis_dft._container.gpaw_relax import gpaw_kwargs_from_params, run_cli
from precis_dft.structures import canonical_poscar


class TestGpawKwargsFromParams:
    def test_maps_dft_block(self) -> None:
        kw = gpaw_kwargs_from_params(
            {
                "dft": {
                    "functional": "PBE",
                    "h": 0.16,
                    "spin_polarized": False,
                    "kpts": {"density": 4.0, "gamma_centered": False},
                    "smearing": {"method": "gauss", "width_ev": 0.1},
                }
            }
        )
        assert kw["xc"] == "PBE"
        assert kw["mode"] == "lcao"
        assert kw["h"] == 0.16
        assert kw["spinpol"] is False
        assert kw["kpts"] == {"density": 4.0, "gamma": False}
        assert kw["occupations"] == {"name": "gauss", "width": 0.1}

    def test_defaults_when_dft_absent(self) -> None:
        kw = gpaw_kwargs_from_params({})
        assert kw["xc"] == "RPBE"
        assert kw["kpts"]["density"] == 6.0
        assert kw["occupations"]["name"] == "fermi-dirac"

    def test_pw_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode='lcao' only"):
            gpaw_kwargs_from_params({"dft": {"mode": "pw"}})


class TestRunCliFailurePath:
    """Without GPAW installed, run_cli must record the failure and
    return 1 rather than raising — the host side relies on both."""

    def _stage(self, in_dir: Path) -> None:
        in_dir.mkdir(parents=True, exist_ok=True)
        (in_dir / "POSCAR").write_text(
            canonical_poscar(bulk("Pt", "fcc", a=3.92)), encoding="utf-8"
        )
        (in_dir / "params.json").write_text(
            json.dumps({"dft": {"functional": "RPBE"}}), encoding="utf-8"
        )

    def test_records_failure_and_returns_1(self, tmp_path: Path) -> None:
        in_dir, out_dir = tmp_path / "in", tmp_path / "out"
        self._stage(in_dir)
        rc = run_cli(str(in_dir), str(out_dir))
        assert rc == 1
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        assert result["ok"] is False
        assert "error" in result and "traceback" in result

    def test_bad_mode_is_recorded(self, tmp_path: Path) -> None:
        in_dir, out_dir = tmp_path / "in", tmp_path / "out"
        in_dir.mkdir(parents=True)
        (in_dir / "POSCAR").write_text(
            canonical_poscar(bulk("Pt", "fcc", a=3.92)), encoding="utf-8"
        )
        (in_dir / "params.json").write_text(
            json.dumps({"dft": {"mode": "pw"}}), encoding="utf-8"
        )
        rc = run_cli(str(in_dir), str(out_dir))
        assert rc == 1
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        assert "mode='lcao' only" in result["error"]


class TestRankGating:
    """Under ``mpirun -np N`` every rank runs ``run_cli``; only rank 0 may
    touch ``result.json`` (N writers on one file is a torn file at best)."""

    def _stage(self, in_dir: Path) -> None:
        in_dir.mkdir(parents=True, exist_ok=True)
        (in_dir / "POSCAR").write_text(
            canonical_poscar(bulk("Pt", "fcc", a=3.92)), encoding="utf-8"
        )
        (in_dir / "params.json").write_text(json.dumps({"dft": {}}), encoding="utf-8")

    def test_a_non_zero_rank_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        in_dir, out_dir = tmp_path / "in", tmp_path / "out"
        self._stage(in_dir)

        class _Rank3:
            rank = 3
            size = 4

        monkeypatch.setattr(gpaw_relax, "_world", lambda: _Rank3())
        rc = run_cli(str(in_dir), str(out_dir))
        assert rc == 1  # GPAW absent here — the point is the file, not the rc
        assert not (out_dir / "result.json").exists()

    def test_rank_zero_records_the_rank_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A serial image and an under-ranked ``mpirun`` are indistinguishable
        from the timings alone, so the count goes in the record."""
        in_dir, out_dir = tmp_path / "in", tmp_path / "out"
        self._stage(in_dir)

        class _Rank0:
            rank = 0
            size = 4

        monkeypatch.setattr(gpaw_relax, "_world", lambda: _Rank0())
        run_cli(str(in_dir), str(out_dir))
        assert (
            json.loads((out_dir / "result.json").read_text(encoding="utf-8"))["ranks"]
            == 4
        )

    def test_defaults_to_serial_without_gpaw(self) -> None:
        world = gpaw_relax._world()
        assert world.rank == 0 and world.size >= 1


class TestCli:
    def test_routes_gpaw_relax(self, tmp_path: Path) -> None:
        in_dir, out_dir = tmp_path / "in", tmp_path / "out"
        in_dir.mkdir(parents=True)
        (in_dir / "POSCAR").write_text(
            canonical_poscar(bulk("Pt", "fcc", a=3.92)), encoding="utf-8"
        )
        (in_dir / "params.json").write_text(json.dumps({"dft": {}}), encoding="utf-8")
        # Routes to run_cli; GPAW absent ⇒ rc=1 (failure recorded).
        rc = cli.main(["gpaw-relax", "--in", str(in_dir), "--out", str(out_dir)])
        assert rc == 1
        assert (out_dir / "result.json").exists()

    def test_unknown_subcommand_exits(self) -> None:
        with pytest.raises(SystemExit):
            cli.main(["not-a-command"])
