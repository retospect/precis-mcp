"""IR types + adapter registry. Pure unit tests, no DB/network."""

from __future__ import annotations

import pytest

from precis.structure.importers import (
    ExternalId,
    ExternalRun,
    get_adapter,
    register_adapter,
)


def test_external_id_is_the_dataset_config_id_pair() -> None:
    eid = ExternalId(dataset="aqcat25", config_id="pd-111-oh-0042")
    assert eid.dataset == "aqcat25"
    assert eid.config_id == "pd-111-oh-0042"
    # frozen — usable as a dict/set key for idempotent collapse
    assert hash(eid) == hash(ExternalId(dataset="aqcat25", config_id="pd-111-oh-0042"))


def test_external_run_fields_and_defaults() -> None:
    run = ExternalRun(
        energy=-12.34,
        max_force=0.012,
        final_geometry={"positions": [[0, 0, 0]]},
        method={
            "functional": "PBE",
            "cutoff_eV": 500,
            "kmesh": [4, 4, 1],
            "spin": "polarized",
            "pseudopotentials": "GPAW-PAW",
            "dataset_doi": "10.1234/aqcat25",
        },
    )
    assert run.energy == -12.34
    assert run.max_force == 0.012
    assert run.final_geometry == {"positions": [[0, 0, 0]]}
    assert run.method["functional"] == "PBE"
    assert run.provenance == "external"  # default, never masquerades as computed


def test_external_run_allows_none_force_and_geometry() -> None:
    run = ExternalRun(energy=-1.0, max_force=None, final_geometry=None, method={})
    assert run.max_force is None
    assert run.final_geometry is None


def test_register_and_get_adapter_round_trip() -> None:
    def _fake_adapter(raw: object) -> tuple[object, ExternalRun, ExternalId]:
        return (
            raw,
            ExternalRun(energy=0.0, max_force=None, final_geometry=None, method={}),
            ExternalId(dataset="fake-source", config_id=str(raw)),
        )

    register_adapter("fake-source", _fake_adapter)
    try:
        fn = get_adapter("fake-source")
        scene, run, eid = fn("raw-record-1")
        assert scene == "raw-record-1"
        assert isinstance(run, ExternalRun)
        assert eid == ExternalId(dataset="fake-source", config_id="raw-record-1")
    finally:
        # keep the module-level registry clean for other tests in the session
        from precis.structure.importers import _ADAPTERS

        _ADAPTERS.pop("fake-source", None)


def test_get_adapter_unknown_name_raises_with_known_names() -> None:
    register_adapter("known-source", lambda raw: None)  # type: ignore[arg-type]
    try:
        with pytest.raises(ValueError, match="known-source"):
            get_adapter("nope")
    finally:
        from precis.structure.importers import _ADAPTERS

        _ADAPTERS.pop("known-source", None)
