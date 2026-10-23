"""``_relax_ml`` at block scale (gr506395): the optimiser follows the atom
count, progress reaches the log, and the MLIP's device is reported with a
warning on a CPU fallback. EMT stands in for the MLIP (no MACE needed)."""

from __future__ import annotations

import importlib
import logging
from typing import Any

import pytest

from precis.structure import RelaxUnsupported, Scene, apply_ops, relax
from precis.structure.cell import Cell
from precis.structure.relax import BFGS_MAX_ATOMS, ml_device_check, ml_optimizer

pytest.importorskip("ase")

# ``precis.structure.relax`` the module (the package re-exports the function
# under the same name).
relax_mod = importlib.import_module("precis.structure.relax")


def _two_cu() -> Scene:
    scene = Scene(cell=Cell.from_lengths_angles(10, 10, 10, pbc=(True, True, True)))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Cu", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Cu", "frac": [0.24, 0.0, 0.0]},
        ],
    )
    return scene


def _emt_mlip(monkeypatch: pytest.MonkeyPatch) -> None:
    from ase.calculators.emt import EMT

    monkeypatch.setattr(relax_mod, "_ml_calculator", lambda model, **_kw: EMT())


def test_ml_optimizer_by_atom_count() -> None:
    from ase.optimize import BFGS, LBFGS

    assert ml_optimizer(1) is BFGS
    assert ml_optimizer(BFGS_MAX_ATOMS) is BFGS
    assert ml_optimizer(BFGS_MAX_ATOMS + 1) is LBFGS
    assert ml_optimizer(5105) is LBFGS


def test_small_relax_stays_on_bfgs(monkeypatch, caplog) -> None:
    _emt_mlip(monkeypatch)
    caplog.set_level(logging.INFO, logger="precis.structure.relax")
    relax(_two_cu(), fidelity="ml", steps=20)
    assert "optimizer BFGS" in caplog.text


def test_relax_above_threshold_uses_lbfgs_and_logs_progress(
    monkeypatch, caplog
) -> None:
    _emt_mlip(monkeypatch)
    monkeypatch.setattr(relax_mod, "BFGS_MAX_ATOMS", 1)
    monkeypatch.setattr(relax_mod, "PROGRESS_EVERY", 1)
    caplog.set_level(logging.INFO, logger="precis.structure.relax")
    res = relax(_two_cu(), fidelity="ml", steps=20)
    assert "optimizer LBFGS" in caplog.text
    assert res.n_steps >= 1
    assert "relax[ml]: step 1/20 on 2 atoms" in caplog.text
    assert "s elapsed" in caplog.text


class _Param:
    def __init__(self, device: str) -> None:
        self.device = device


class _Model:
    def __init__(self, device: str) -> None:
        self._device = device

    def parameters(self) -> Any:
        return iter([_Param(self._device)])


class _Calc:
    def __init__(self, device: str) -> None:
        self.models = [_Model(device)]


def test_ml_device_check_warns_when_cuda_requested_but_on_cpu(monkeypatch) -> None:
    monkeypatch.setattr(relax_mod, "_ml_calculator", lambda m, **_k: _Calc("cpu"))
    monkeypatch.setattr("shutil.which", lambda _name: None)
    report = ml_device_check("mace_mp", cuda_requested=True)
    assert report is not None
    assert report["device"] == "cpu"
    assert "CUDA was requested" in report["warning"]


def test_ml_device_check_warns_on_gpu_host_without_cuda(monkeypatch) -> None:
    monkeypatch.setattr(relax_mod, "_ml_calculator", lambda m, **_k: _Calc("cpu"))
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/nvidia-smi")
    report = ml_device_check("mace_mp")
    assert report is not None and report["gpu_present"] is True
    assert "runs on CPU" in report["warning"]


def test_ml_device_check_quiet_on_cuda(monkeypatch) -> None:
    monkeypatch.setattr(relax_mod, "_ml_calculator", lambda m, **_k: _Calc("cuda:0"))
    report = ml_device_check("mace_mp", cuda_requested=True)
    assert report is not None
    assert report["device"] == "cuda:0"
    assert "warning" not in report


def test_ml_device_check_absent_backend_is_none(monkeypatch) -> None:
    def _absent(_m: str, **_k: Any) -> Any:
        raise RelaxUnsupported("no mace")

    monkeypatch.setattr(relax_mod, "_ml_calculator", _absent)
    assert ml_device_check("mace_mp") is None
