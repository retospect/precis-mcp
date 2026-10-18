"""CIF import and the QMOF / CoRE MOF adapters — pure.

Recorded fixtures under ``tests/fixtures/structure/mof/`` (README there);
no network: the URL fetch runs against a ``safe_get`` double. No DB.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

pytest.importorskip("ase")

from precis.structure.importers import get_adapter, mof
from precis.structure.importers.cif import (
    MAX_CIF_ATOMS,
    MAX_CIF_BYTES,
    CifRefused,
    cif_to_scene,
    fetch_cif_url,
)
from precis.utils import safe_fetch

FIXTURES = Path(__file__).parent / "fixtures" / "structure" / "mof"
CORE_CIF = (FIXTURES / "core_mof_ABAVIJ_clean.cif").read_text(encoding="utf-8")
QMOF_CIF = (FIXTURES / "qmof_synthetic.cif").read_text(encoding="utf-8")


def _p1_cif(n_atoms: int) -> str:
    rows = "\n".join(
        f"C{i} C {(i % 97) / 97:.5f} {(i // 97 % 97) / 97:.5f} {(i // 9409) / 9:.5f}"
        for i in range(n_atoms)
    )
    return (
        "data_big\n_cell_length_a 60\n_cell_length_b 60\n_cell_length_c 60\n"
        "_cell_angle_alpha 90\n_cell_angle_beta 90\n_cell_angle_gamma 90\n"
        "loop_\n_symmetry_equiv_pos_as_xyz\n x,y,z\n"
        "loop_\n_atom_site_label\n_atom_site_type_symbol\n"
        "_atom_site_fract_x\n_atom_site_fract_y\n_atom_site_fract_z\n" + rows + "\n"
    )


# ── cif_to_scene ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "natoms", "elements"),
    [
        (CORE_CIF, 108, {"Co", "C", "H", "N", "O"}),
        (QMOF_CIF, 9, {"Zn", "C", "H", "O"}),
    ],
    ids=["core-mof", "qmof"],
)
def test_fixture_parses_to_a_3d_cell(text: str, natoms: int, elements: set) -> None:
    scene = cif_to_scene(text)
    assert len(scene.atoms) == natoms
    assert set(scene.composition()) == elements
    assert scene.cell.pbc == (True, True, True)
    assert abs(float(np.linalg.det(scene.cell.lattice))) > 100.0


def test_cif_over_the_atom_cap_is_refused_with_next() -> None:
    with pytest.raises(CifRefused) as exc:
        cif_to_scene(_p1_cif(MAX_CIF_ATOMS + 1))
    assert "atom cap" in str(exc.value)
    assert exc.value.next


def test_symmetry_expansion_estimate_refuses_before_ase_expands() -> None:
    # 2,600 sites x 2 symmetry operations estimate past the cap.
    text = _p1_cif(2600).replace(" x,y,z\n", " x,y,z\n -x,-y,-z\n")
    with pytest.raises(CifRefused) as exc:
        cif_to_scene(text)
    assert "symmetry" in str(exc.value)


def test_space_group_name_without_symops_is_bounded_by_its_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 400 sites in F m -3 m (192 operations) and no symop loop: ASE would
    # expand from the name alone, so the estimate must use the group order.
    from ase.io.cif import CIFBlock

    text = (
        _p1_cif(400)
        .replace("loop_\n_symmetry_equiv_pos_as_xyz\n x,y,z\n", "")
        .replace("data_big\n", "data_big\n_symmetry_space_group_name_H-M 'F m -3 m'\n")
    )

    def must_not_expand(self: Any, *a: Any, **kw: Any) -> Any:
        raise AssertionError("refusal must come before ASE expands the cell")

    monkeypatch.setattr(CIFBlock, "get_atoms", must_not_expand)
    with pytest.raises(CifRefused) as exc:
        cif_to_scene(text)
    assert "400 sites x 192" in str(exc.value)


def test_uppercase_cif_tags_are_recognised() -> None:
    scene = cif_to_scene(QMOF_CIF.replace("_cell_length_a", "_CELL_LENGTH_A"))
    assert len(scene.atoms) == 9


def test_oversized_text_is_refused_before_parse() -> None:
    with pytest.raises(CifRefused) as exc:
        cif_to_scene("data_x\n_cell_length_a 1\n#" + "x" * MAX_CIF_BYTES)
    assert "byte cap" in str(exc.value)
    assert exc.value.next


@pytest.mark.parametrize(
    "body",
    ["hello world", '{"cell": {"a": 1}}', "data_x\n_cell_length_a 5\n"],
    ids=["prose", "json", "cif-without-sites"],
)
def test_non_cif_body_is_refused_with_next(body: str) -> None:
    with pytest.raises(CifRefused) as exc:
        cif_to_scene(body)
    assert exc.value.next


# ── adapters ──────────────────────────────────────────────────────────


def test_core_mof_record_imports_with_no_energy() -> None:
    scene, run, eid = get_adapter("core-mof")(
        {"id": "ABAVIJ_clean", "cif_text": CORE_CIF, "energy": -5.0}
    )
    assert (eid.dataset, eid.config_id) == ("core-mof", "ABAVIJ_clean")
    assert run.energy is None
    assert run.provenance == "external"
    assert run.method["source"] == "core-mof-2024"
    assert run.method["geometry"] == "experimental, solvent-removed"
    assert len(scene.atoms) == 108


def test_qmof_record_carries_energy_and_method_fingerprint() -> None:
    _scene, run, eid = get_adapter("qmof")(
        {"id": "qmof-0000001", "cif_text": QMOF_CIF, "energy": -123.4}
    )
    assert (eid.dataset, eid.config_id) == ("qmof", "qmof-0000001")
    assert run.energy == pytest.approx(-123.4)
    assert run.provenance == "external"
    assert run.method["functional"] == "PBE-D3(BJ)"
    assert run.method["dataset_doi"] == mof.QMOF_DOI


def test_qmof_record_without_energy_is_geometry_only() -> None:
    _scene, run, _eid = mof.qmof_adapter({"id": "qmof-x", "cif_text": QMOF_CIF})
    assert run.energy is None


def test_adapter_refuses_a_record_without_cif() -> None:
    with pytest.raises(ValueError, match="cif_text"):
        mof.core_mof_adapter({"id": "X"})


# ── URL fetch through safe_get ────────────────────────────────────────


def _fake_response(body: bytes, status: int = 200) -> Any:
    return SimpleNamespace(
        status_code=status, content=body, headers={"Content-Length": str(len(body))}
    )


def test_url_fetch_goes_through_safe_get(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
        calls.append(url)
        return _fake_response(CORE_CIF.encode("utf-8"))

    monkeypatch.setattr(safe_fetch, "safe_get", fake_safe_get)
    text = fetch_cif_url("https://example.org/ABAVIJ_clean.cif")
    assert calls == ["https://example.org/ABAVIJ_clean.cif"]
    assert text == CORE_CIF


def test_url_fetch_refuses_an_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        safe_fetch,
        "safe_get",
        lambda client, url, /, **kw: _fake_response(b"x" * (MAX_CIF_BYTES + 1)),
    )
    with pytest.raises(CifRefused, match="byte cap"):
        fetch_cif_url("https://example.org/big.cif")


def test_url_fetch_maps_ssrf_block_to_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(client: Any, url: str, /, **kw: Any) -> Any:
        raise safe_fetch.SsrfBlocked("private address")

    monkeypatch.setattr(safe_fetch, "safe_get", blocked)
    with pytest.raises(CifRefused, match="URL refused") as exc:
        fetch_cif_url("http://internal.example/x.cif")
    assert exc.value.next


# ── MPContribs (QMOF, no index) ───────────────────────────────────────


def test_mpcontribs_fetch_reads_contribution_then_structure_cif(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.utils import http

    seen: list[tuple[str, dict]] = []

    def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
        seen.append((url, kw.get("params") or {}))
        payload: dict[str, Any]
        if url.endswith("/contributions/"):
            payload = {"data": [{"identifier": "qmof-1", "structures": [{"id": "s9"}]}]}
        else:
            payload = {"cif": QMOF_CIF}
        return SimpleNamespace(
            json=lambda: payload, raise_for_status=lambda: None, status_code=200
        )

    monkeypatch.setattr(safe_fetch, "safe_get", fake_safe_get)
    monkeypatch.setattr(http, "with_backoff", lambda send, **kw: send())
    raw = mof.fetch_qmof_mpcontribs("qmof-1", "k")
    assert raw is not None
    assert raw["cif_text"] == QMOF_CIF
    assert raw["energy"] is None
    assert seen[0][1]["identifier"] == "qmof-1"
    assert seen[1][0].endswith("/structures/s9/")
