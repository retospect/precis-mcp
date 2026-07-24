"""On-demand hydrate from an external catalyst DB (ADR 0053 T6).

``get(kind='structure', source='catalysis-hub', ...)`` is the "quest worker
pokes around and pulls real substrates" surface: first touch fetches +
imports a config as an ordinary (searchable, cited) ``structure`` ref; a
repeat lookup by ``config_id=`` is a cache hit — no refetch. Network is
stubbed via monkeypatching ``catalysis_hub.fetch_config`` against the
checked-in fixture; no real network in the gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import Unsupported
from precis.handlers.structure import StructureHandler
from precis.structure.importers import catalysis_hub

FIXTURE = Path(__file__).parent / "fixtures" / "catalysis" / "pd111_no.json"
_CONFIG_ID = "PdNO111top-a1b2c3"


def _load_raw() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


@pytest.fixture
def stub_fetch(monkeypatch):
    """Stub ``catalysis_hub.fetch_config`` to return the fixture record,
    tracking call count so tests can assert the by-id short-circuit."""
    calls: list[dict] = []

    def _fake_fetch_config(**kwargs):
        calls.append(kwargs)
        return [_load_raw()]

    monkeypatch.setattr(catalysis_hub, "fetch_config", _fake_fetch_config)
    return calls


def test_first_touch_hydrates_a_searchable_cited_structure(structure, stub_fetch):
    resp = structure.get(
        source="catalysis-hub",
        args={"surface_composition": "Pd", "facet": "111", "config_id": _CONFIG_ID},
    )
    assert len(stub_fetch) == 1
    assert "hydrated" in resp.body
    assert "Pd8N1O1" in resp.body or "Pd" in resp.body
    assert "catalysis-hub" in resp.body
    assert _CONFIG_ID in resp.body
    assert "10.1021/acscatal.9b02179" in resp.body  # doi footer

    # materialized as an ordinary structure ref: findable + carries an
    # external struct_runs row.
    ref = structure.store.get_ref(
        kind="structure", id="catalysis-hub-pdno111top-a1b2c3"
    )
    assert ref is not None
    runs = structure.store.structure_runs(ref.id)
    assert len(runs) == 1
    assert runs[0]["fidelity"] == "external"
    assert runs[0]["energy"] == pytest.approx(-1.85)


def test_repeat_call_by_config_id_is_a_cache_hit_no_refetch(structure, stub_fetch):
    args = {"surface_composition": "Pd", "facet": "111", "config_id": _CONFIG_ID}
    first = structure.get(source="catalysis-hub", args=args)
    assert len(stub_fetch) == 1

    second = structure.get(source="catalysis-hub", args=args)
    assert len(stub_fetch) == 1  # no refetch — the by-id short-circuit hit
    assert "cached" in second.body
    assert _CONFIG_ID in second.body

    ref1 = structure.store.get_ref(
        kind="structure", id="catalysis-hub-pdno111top-a1b2c3"
    )
    assert ref1 is not None
    # same design both times
    assert f"design {ref1.slug}" in first.body or str(ref1.slug) in first.body
    assert str(ref1.slug) in second.body


def test_config_id_miss_does_not_substitute_an_unrelated_record(structure, stub_fetch):
    # the stub returns config PdNO111top-a1b2c3; asking for a different id must
    # report the miss, never render the fetched (unrelated) record as if it
    # were the one requested (that could feed a wrong substrate to a quest).
    resp = structure.get(
        source="catalysis-hub",
        args={
            "surface_composition": "Pd",
            "facet": "111",
            "config_id": "not-a-real-id",
        },
    )
    assert len(stub_fetch) == 1  # it did fetch
    assert "not-a-real-id" in resp.body
    assert "no catalysis-hub config" in resp.body
    assert "hydrated" not in resp.body


def test_missing_import_extra_is_a_clean_unsupported(structure, monkeypatch):
    def _raise_unsupported(**_kwargs):
        raise catalysis_hub.CatalysisHubUnsupported(
            "Catalysis-Hub fetch needs httpx — pip install 'precis-mcp[import]'"
        )

    monkeypatch.setattr(catalysis_hub, "fetch_config", _raise_unsupported)

    with pytest.raises(Unsupported) as exc_info:
        structure.get(
            source="catalysis-hub",
            args={"surface_composition": "Pd", "facet": "111"},
        )
    assert "pip install" in str(exc_info.value.next or exc_info.value)
