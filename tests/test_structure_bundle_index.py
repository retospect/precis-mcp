"""The QMOF / CoRE MOF bundle index — build, read, and its refusals. Pure.

Bundles are built in ``tmp_path`` from the recorded fixtures under
``tests/fixtures/structure/mof/``. No network, no DB.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from precis.structure.importers import bundle_index as bi

FIXTURES = Path(__file__).parent / "fixtures" / "structure" / "mof"
CORE_CIF = (FIXTURES / "core_mof_ABAVIJ_clean.cif").read_text(encoding="utf-8")
QMOF_CIF = (FIXTURES / "qmof_synthetic.cif").read_text(encoding="utf-8")


# ── bundle index ──────────────────────────────────────────────────────


def _qmof_zip(path: Path) -> Path:
    records = [
        {
            "qmof_id": "qmof-0000001",
            "name": "ZNFORM_FSR",
            "outputs": {"pbe": {"energy_total": -123.4}},
        }
    ]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("qmof_database/qmof.json", json.dumps(records))
        zf.writestr("qmof_database/relaxed_structures/ZNFORM_FSR.cif", QMOF_CIF)
        zf.writestr("qmof_database/README.txt", "not a cif")
    return path


def test_qmof_zip_index_maps_name_to_qmof_id_with_energy(tmp_path: Path) -> None:
    bundle = _qmof_zip(tmp_path / "qmof.zip")
    doc = bi.build_index("qmof", bundle)
    assert list(doc["entries"]) == ["qmof-0000001"]
    assert doc["entries"]["qmof-0000001"]["energy"] == pytest.approx(-123.4)
    bi.write_index(doc, tmp_path)
    raw = bi.load_record("qmof", "qmof-0000001", tmp_path)
    assert raw is not None
    assert raw["energy"] == pytest.approx(-123.4)
    assert raw["cif_text"] == QMOF_CIF
    # bundle under the index dir is stored relative, so a remount resolves
    stored = json.loads((tmp_path / "qmof.json").read_text(encoding="utf-8"))
    assert stored["bundle"] == "qmof.zip"


def test_core_mof_directory_index_and_unknown_id(tmp_path: Path) -> None:
    bundle = tmp_path / "core"
    (bundle / "CR" / "ASR").mkdir(parents=True)
    (bundle / "CR" / "ASR" / "ABAVIJ_clean.cif").write_text(CORE_CIF, encoding="utf-8")
    index_dir = tmp_path / "idx"
    bi.write_index(bi.build_index("core-mof", bundle), index_dir)
    raw = bi.load_record("core-mof", "ABAVIJ_clean", index_dir)
    assert raw is not None
    assert "energy" not in raw
    assert raw["cif_text"] == CORE_CIF
    assert bi.load_record("core-mof", "NOPE", index_dir) is None


def test_missing_index_names_the_build_command(tmp_path: Path) -> None:
    with pytest.raises(bi.BundleIndexError) as exc:
        bi.load_record("core-mof", "ABAVIJ_clean", tmp_path)
    assert "precis structure index-bundle core-mof" in exc.value.next


def test_replaced_bundle_under_a_stale_index_is_refused(tmp_path: Path) -> None:
    bundle = _qmof_zip(tmp_path / "qmof.zip")
    bi.write_index(bi.build_index("qmof", bundle), tmp_path)
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("padding.txt", "x" * 1000)
        zf.writestr("qmof_database/relaxed_structures/ZNFORM_FSR.cif", QMOF_CIF)
    with pytest.raises(bi.BundleIndexError, match="no longer matches"):
        bi.load_record("qmof", "qmof-0000001", tmp_path)


def test_corrupt_index_is_a_handled_refusal(tmp_path: Path) -> None:
    (tmp_path / "core-mof.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(bi.BundleIndexError, match="unreadable") as exc:
        bi.load_record("core-mof", "ABAVIJ_clean", tmp_path)
    assert "index-bundle core-mof" in exc.value.next


def test_nested_archive_is_refused_at_build(tmp_path: Path) -> None:
    bundle = tmp_path / "b.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("inner.zip", b"PK")
    with pytest.raises(bi.BundleIndexError, match="nested"):
        bi.build_index("core-mof", bundle)


def test_unknown_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(bi.BundleIndexError) as exc:
        bi.build_index("catalysis-hub", tmp_path)
    assert "qmof" in exc.value.next
