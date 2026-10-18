"""The agent-facing CIF ``put`` on the structure kind, against the test DB.

CIF text, a bare CIF body, a URL (through a ``safe_get`` double; no
network), and a named-source import that lands read-only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("ase")

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.structure import StructureHandler
from precis.structure.importers import mof
from precis.structure.importers.cif import MAX_CIF_ATOMS
from precis.utils import safe_fetch

FIXTURES = Path(__file__).parent / "fixtures" / "structure" / "mof"
CORE_CIF = (FIXTURES / "core_mof_ABAVIJ_clean.cif").read_text(encoding="utf-8")
QMOF_CIF = (FIXTURES / "qmof_synthetic.cif").read_text(encoding="utf-8")


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def _external_runs(store, ref_id: int) -> list[tuple]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT provenance, energy, method FROM struct_runs "
            "WHERE ref_id = %s AND provenance = 'external'",
            (ref_id,),
        ).fetchall()


# ── put ───────────────────────────────────────────────────────────────


def test_cif_put_creates_an_ordinary_editable_design(structure) -> None:
    resp = structure.put(id="abavij", args={"cif": CORE_CIF})
    assert "created" in resp.body
    ref = structure.store.get_ref(kind="structure", id="abavij")
    assert ref is not None
    scene, _ = structure.store.structure_load(ref.id)
    assert len(scene.atoms) == 108
    assert scene.cell.pbc == (True, True, True)
    assert _external_runs(structure.store, ref.id) == []


def test_bare_cif_text_body_is_accepted(structure) -> None:
    structure.put(id="znform", text=QMOF_CIF, args={"description": "Zn formate"})
    ref = structure.store.get_ref(kind="structure", id="znform")
    assert ref is not None
    assert (ref.meta or {}).get("description") == "Zn formate"


def test_non_cif_body_is_refused_with_next(structure) -> None:
    with pytest.raises(BadInput) as exc:
        structure.put(id="junk", args={"cif": "this is not a crystal file"})
    assert exc.value.next


def test_cif_over_the_atom_cap_is_refused_with_next(structure) -> None:
    rows = "\n".join(
        f"C{i} C {(i % 97) / 97:.5f} {(i // 97 % 97) / 97:.5f} {(i // 9409) / 9:.5f}"
        for i in range(MAX_CIF_ATOMS + 1)
    )
    big = (
        "data_big\n_cell_length_a 60\n_cell_length_b 60\n_cell_length_c 60\n"
        "_cell_angle_alpha 90\n_cell_angle_beta 90\n_cell_angle_gamma 90\n"
        "loop_\n_atom_site_label\n_atom_site_type_symbol\n"
        "_atom_site_fract_x\n_atom_site_fract_y\n_atom_site_fract_z\n" + rows + "\n"
    )
    with pytest.raises(BadInput) as exc:
        structure.put(id="big", args={"cif": big})
    assert "atom cap" in exc.value.cause
    assert exc.value.next
    assert structure.store.get_ref(kind="structure", id="big") is None


def test_url_cif_put_goes_through_safe_get(
    structure, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_safe_get(client: Any, url: str, /, **kw: Any) -> Any:
        calls.append(url)
        body = CORE_CIF.encode("utf-8")
        return SimpleNamespace(
            status_code=200, content=body, headers={"Content-Length": str(len(body))}
        )

    monkeypatch.setattr(safe_fetch, "safe_get", fake_safe_get)
    structure.put(id="from-url", args={"cif_url": "https://example.org/a.cif"})
    assert calls == ["https://example.org/a.cif"]
    assert structure.store.get_ref(kind="structure", id="from-url") is not None


def test_url_put_refuses_a_non_http_scheme(structure) -> None:
    with pytest.raises(BadInput) as exc:
        structure.put(id="f", args={"cif_url": "file:///etc/passwd"})
    assert exc.value.next


def test_named_source_put_imports_read_only_and_never_overwrites(structure) -> None:
    args = {"cif": CORE_CIF, "source": "core-mof", "config_id": "ABAVIJ_clean"}
    first = structure.put(args=args)
    assert "no energy" in first.body
    with pytest.raises(BadInput, match="already imported") as again:
        structure.put(args={**args, "cif": QMOF_CIF})
    assert again.value.next
    ref_id = structure.store.find_ref_by_identifier(
        "core-mof", "ABAVIJ_clean", kind="structure"
    )
    assert ref_id is not None
    runs = _external_runs(structure.store, ref_id)
    assert len(runs) == 1
    provenance, energy, method = runs[0]
    assert (provenance, energy) == ("external", None)
    assert method["source"] == "core-mof-2024"
    assert method["geometry_supplied_by"] == "put"
    scene, _ = structure.store.structure_load(ref_id)
    assert len(scene.atoms) == 108  # the refused second put left the atoms alone
    ref = structure.store.get_ref(kind="structure", id=ref_id)
    with pytest.raises(BadInput, match="imported reference"):
        structure.put(id=str(ref.slug), args={"cif": CORE_CIF})


def test_named_source_put_refuses_id_and_unknown_source(structure) -> None:
    with pytest.raises(BadInput, match="picks its own slug"):
        structure.put(
            id="x", args={"cif": CORE_CIF, "source": "core-mof", "config_id": "A"}
        )
    with pytest.raises(BadInput) as exc:
        structure.put(
            args={"cif": CORE_CIF, "source": "catalysis-hub", "config_id": "A"}
        )
    assert exc.value.options == list(mof.CIF_ADAPTERS)
