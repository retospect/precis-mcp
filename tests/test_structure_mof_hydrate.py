"""The QMOF / CoRE MOF ``get`` lookups against the test DB:
``get(kind='structure', args={'source': 'qmof'|'core-mof', 'config_id': ...})``
reading one record through a bundle index built in ``tmp_path``. No
network: MPContribs runs against a ``fetch_qmof_mpcontribs`` double.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("ase")

from precis import secrets
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.structure import StructureHandler
from precis.structure.importers import mof

FIXTURES = Path(__file__).parent / "fixtures" / "structure" / "mof"
CORE_CIF = (FIXTURES / "core_mof_ABAVIJ_clean.cif").read_text(encoding="utf-8")
QMOF_CIF = (FIXTURES / "qmof_synthetic.cif").read_text(encoding="utf-8")


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


@pytest.fixture
def index_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "idx"
    monkeypatch.setenv("PRECIS_STRUCT_BUNDLE_DIR", str(d))
    return d


@pytest.fixture
def no_mp_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: None)


def _external_runs(store, ref_id: int) -> list[tuple]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT provenance, energy, method FROM struct_runs "
            "WHERE ref_id = %s AND provenance = 'external'",
            (ref_id,),
        ).fetchall()


def _index_core_bundle(tmp_path: Path, index_dir: Path) -> None:
    from precis.cli import structure as cli

    bundle = tmp_path / "core"
    bundle.mkdir()
    (bundle / "ABAVIJ_clean.cif").write_text(CORE_CIF, encoding="utf-8")
    cli.run(
        argparse.Namespace(
            structure_cmd="index-bundle",
            source="core-mof",
            path=bundle,
            index_dir=index_dir,
        )
    )


# ── get source='qmof'|'core-mof' ──────────────────────────────────────


def test_core_mof_lookup_imports_one_record_then_hits_the_cache(
    structure, tmp_path: Path, index_dir: Path
) -> None:
    _index_core_bundle(tmp_path, index_dir)
    args = {"source": "core-mof", "config_id": "ABAVIJ_clean"}
    first = structure.get(args=args)
    assert "hydrated" in first.body
    assert "no energy" in first.body
    assert "10.5281/zenodo.15055758" in first.body
    second = structure.get(args=args)
    assert "cached" in second.body
    ref_id = structure.store.find_ref_by_identifier(
        "core-mof", "ABAVIJ_clean", kind="structure"
    )
    assert ref_id is not None
    assert len(_external_runs(structure.store, ref_id)) == 1


def test_lookup_of_an_id_the_index_lacks_is_not_found(
    structure, tmp_path: Path, index_dir: Path
) -> None:
    _index_core_bundle(tmp_path, index_dir)
    with pytest.raises(NotFound) as exc:
        structure.get(args={"source": "core-mof", "config_id": "NOPE"})
    assert exc.value.next


def test_missing_index_refusal_names_the_build_command(
    structure, index_dir: Path, no_mp_key: None
) -> None:
    for source in ("core-mof", "qmof"):
        with pytest.raises(Unsupported) as exc:
            structure.get(args={"source": source, "config_id": "x"})
        assert f"precis structure index-bundle {source}" in str(exc.value.next)


def test_lookup_needs_config_id(structure, index_dir: Path) -> None:
    with pytest.raises(BadInput) as exc:
        structure.get(args={"source": "qmof"})
    assert exc.value.next


def test_qmof_without_index_uses_mpcontribs_when_the_key_is_set(
    structure, index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_fetch(qmof_id: str, api_key: str, **kw: Any) -> dict:
        calls.append((qmof_id, api_key))
        return {"id": qmof_id, "cif_text": QMOF_CIF, "energy": None}

    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "test-key")
    monkeypatch.setattr(mof, "fetch_qmof_mpcontribs", fake_fetch)
    resp = structure.get(args={"source": "qmof", "config_id": "qmof-abc"})
    assert calls == [("qmof-abc", "test-key")]
    assert "no energy" in resp.body
    assert "MPContribs" in resp.body


def test_qmof_index_wins_over_mpcontribs_and_carries_energy(
    structure, tmp_path: Path, index_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import zipfile

    from precis.cli import structure as cli

    bundle = tmp_path / "qmof.zip"
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr(
            "qmof.json",
            json.dumps(
                [
                    {
                        "qmof_id": "qmof-0000001",
                        "name": "ZNFORM_FSR",
                        "outputs": {"pbe": {"energy_total": -123.4}},
                    }
                ]
            ),
        )
        zf.writestr("relaxed_structures/ZNFORM_FSR.cif", QMOF_CIF)
    cli.run(
        argparse.Namespace(
            structure_cmd="index-bundle", source="qmof", path=bundle, index_dir=None
        )
    )

    def must_not_fetch(*a: Any, **kw: Any) -> dict:
        raise AssertionError("MPContribs must not be called when an index exists")

    monkeypatch.setattr(secrets, "get_secret", lambda name, **kw: "test-key")
    monkeypatch.setattr(mof, "fetch_qmof_mpcontribs", must_not_fetch)
    resp = structure.get(args={"source": "qmof", "config_id": "qmof-0000001"})
    assert "no energy" not in resp.body
    ref_id = structure.store.find_ref_by_identifier(
        "qmof", "qmof-0000001", kind="structure"
    )
    (_prov, energy, method) = _external_runs(structure.store, ref_id)[0]
    assert energy == pytest.approx(-123.4)
    assert method["functional"] == "PBE-D3(BJ)"
