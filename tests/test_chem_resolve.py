"""Tests for the ``chem`` kind's name → SMILES resolver.

Every upstream call replays a fixture from ``tests/fixtures/chem/name_resolve``
(see its README for which are recorded vs hand-built); an unmapped URL fails
the test, so nothing reaches the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Upstream
from precis.handlers import _chem_resolve as cr
from precis.handlers.chem import ChemHandler

rdkit = pytest.importorskip("rdkit")

FIX = Path(__file__).parent / "fixtures" / "chem" / "name_resolve"

ASPIRIN_KEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _opsin(q: str) -> str:
    return cr.OPSIN_URL.format(name=cr._q(q))


def _pc_name(q: str) -> str:
    return cr.PUBCHEM_CIDS_URL.format(name=cr._q(q))


def _pc_props(cids: str) -> str:
    return cr.PUBCHEM_PROPS_URL.format(cids=cids)


def _cir(q: str) -> str:
    return cr.CIR_URL.format(name=cr._q(q), rep="smiles")


class Replay:
    """URL → (status, body). Records calls; unmapped URLs fail loudly."""

    def __init__(self, routes: dict[str, tuple[int, str] | Exception]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, str]:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected network call: {url}")
        got = self.routes[url]
        if isinstance(got, Exception):
            raise got
        return got


NOT_FOUND = (404, '{"Fault":{"Code":"PUGREST.NotFound"}}')


def _aspirin_routes() -> dict[str, tuple[int, str] | Exception]:
    return {
        _opsin("aspirin"): (404, _fx("opsin_failure.json")),
        _pc_name("aspirin"): (200, _fx("pubchem_name_aspirin.json")),
        _pc_props("2244"): (200, _fx("pubchem_cid_2244.json")),
        _cir("aspirin"): (200, "CC(=O)Oc1ccccc1C(O)=O\n"),
    }


# ── query classification ─────────────────────────────────────────────


def test_cas_checksum() -> None:
    assert cr.cas_checksum_ok("50-78-2")  # aspirin
    assert cr.cas_checksum_ok("64-17-5")  # ethanol
    assert cr.cas_checksum_ok("7732-18-5")  # water
    assert not cr.cas_checksum_ok("50-78-3")
    assert cr.classify_query("64-17-5") == "cas"
    assert cr.classify_query("dcm") == "abbreviation"
    assert cr.classify_query("aspirin") == "name"


def test_abbreviation_table_smiles_all_parse() -> None:
    from rdkit import Chem

    for abbrev, readings in cr.abbreviation_table().items():
        for name, smi in readings:
            assert Chem.MolFromSmiles(smi) is not None, (abbrev, name, smi)


# ── tiers ────────────────────────────────────────────────────────────


def test_abbreviation_resolves_offline() -> None:
    fetch = Replay({})
    res = cr.resolve("DCM", fetch)
    assert res.status == "resolved"
    assert res.answer is not None
    assert res.answer.smiles == "ClCCl"
    assert res.answer.inchikey == "YMWUJEATGCHHMB-UHFFFAOYSA-N"
    assert res.answer.source == "precis abbreviation table"
    assert fetch.calls == []


def test_ambiguous_abbreviation_refused_with_candidates() -> None:
    res = cr.resolve("TEA", Replay({}))
    assert res.status == "ambiguous"
    assert res.answer is None
    assert {c.name for c in res.candidates} == {"triethylamine", "triethanolamine"}


def test_opsin_hit_short_circuits() -> None:
    q = "2-acetyloxybenzoic acid"
    fetch = Replay({_opsin(q): (200, _fx("opsin_2-acetyloxybenzoic_acid.json"))})
    res = cr.resolve(q, fetch)
    assert res.status == "resolved"
    assert res.answer is not None
    assert res.answer.source == "OPSIN"
    assert res.answer.inchikey == ASPIRIN_KEY
    assert res.answer.smiles == "CC(=O)Oc1ccccc1C(=O)O"
    assert len(fetch.calls) == 1


def test_trivial_name_via_pubchem_corroborated_by_cir() -> None:
    res = cr.resolve("aspirin", Replay(_aspirin_routes()))
    assert res.status == "resolved"
    ans = res.answer
    assert ans is not None
    assert ans.source == "PubChem CID 2244"
    assert ans.source_ref == "https://pubchem.ncbi.nlm.nih.gov/compound/2244"
    assert ans.inchikey == ASPIRIN_KEY
    assert ans.smiles == "CC(=O)Oc1ccccc1C(=O)O"
    assert "CIR agrees" in res.notes
    assert [t for t, _ in res.tiers] == ["OPSIN", "PubChem", "CIR"]


def test_cas_skips_opsin() -> None:
    fetch = Replay(
        {
            _pc_name("64-17-5"): (200, _fx("pubchem_name_64-17-5.json")),
            _pc_props("702"): (200, _fx("pubchem_cid_702.json")),
            _cir("64-17-5"): (200, "CCO\n"),
        }
    )
    res = cr.resolve("64-17-5", fetch)
    assert res.status == "resolved"
    assert res.answer is not None
    assert res.answer.smiles == "CCO"
    assert ("OPSIN", "skipped (CAS number)") in res.tiers


def test_pubchem_cir_disagreement_is_ambiguous() -> None:
    # PubChem silently picks 1,1-dichloroethane for the bare name.
    fetch = Replay(
        {
            _opsin("dichloroethane"): (404, _fx("opsin_failure.json")),
            _pc_name("dichloroethane"): (200, _fx("pubchem_name_dichloroethane.json")),
            _pc_props("6365"): (200, _fx("pubchem_cid_6365.json")),
            _cir("dichloroethane"): (200, "ClCCCl\n"),
        }
    )
    res = cr.resolve("dichloroethane", fetch)
    assert res.status == "ambiguous"
    smiles = {c.smiles for c in res.candidates}
    assert smiles == {"CC(Cl)Cl", "ClCCCl"}


def test_pubchem_multi_cid_is_ambiguous() -> None:
    q = "dichloroethanes"
    fetch = Replay(
        {
            _opsin(q): (404, _fx("opsin_failure.json")),
            _pc_name(q): (200, '{"IdentifierList":{"CID":[6365,11]}}'),
            _pc_props("6365,11"): (200, _fx("pubchem_cid_6365_11.json")),
            _cir(q): NOT_FOUND,
        }
    )
    res = cr.resolve(q, fetch)
    assert res.status == "ambiguous"
    assert {c.name for c in res.candidates} == {
        "1,1-Dichloroethane",
        "1,2-Dichloroethane",
    }


def test_not_found_everywhere() -> None:
    q = "notachemicalzzq"
    fetch = Replay(
        {
            _opsin(q): (404, _fx("opsin_failure.json")),
            _pc_name(q): (404, _fx("pubchem_name_notachemicalzzq.json")),
            _cir(q): (404, "Page not found (404)"),
        }
    )
    res = cr.resolve(q, fetch)
    assert res.status == "not_found"
    assert res.candidates == []


def test_fault_without_answer_is_fault_not_miss() -> None:
    q = "notachemicalzzq"
    fetch = Replay(
        {
            _opsin(q): (404, _fx("opsin_failure.json")),
            _pc_name(q): (503, "busy"),
            _cir(q): TimeoutError("read timed out"),
        }
    )
    res = cr.resolve(q, fetch)
    assert res.status == "fault"
    assert any(o.startswith("fault") for _, o in res.tiers)


def test_cir_fault_does_not_block_pubchem_answer() -> None:
    routes = _aspirin_routes()
    routes[_cir("aspirin")] = TimeoutError("slow")
    res = cr.resolve("aspirin", Replay(routes))
    assert res.status == "resolved"
    assert "not corroborated: CIR gave no answer" in res.notes


# ── handler (cache flow) ─────────────────────────────────────────────


@pytest.fixture
def chem(hub: Hub) -> ChemHandler:
    h = ChemHandler(hub=hub)
    return h


def test_handler_resolves_and_caches(chem: ChemHandler) -> None:
    fetch = Replay(_aspirin_routes())
    chem.fetch = fetch
    body = chem.get(id="aspirin").body
    assert "smiles:   CC(=O)Oc1ccccc1C(=O)O" in body
    assert f"inchikey: {ASPIRIN_KEY}" in body
    assert "source:   PubChem CID 2244" in body
    n = len(fetch.calls)
    again = chem.get(id="aspirin").body
    assert "PubChem CID 2244" in again
    assert len(fetch.calls) == n  # served from cache


def test_handler_refusal_lists_candidates(chem: ChemHandler) -> None:
    chem.fetch = Replay({})
    body = chem.get(id="TEA").body
    assert "REFUSED: ambiguous" in body
    assert "triethylamine" in body and "triethanolamine" in body


def test_handler_fault_raises_and_is_not_cached(chem: ChemHandler) -> None:
    q = "notachemicalzzq"
    chem.fetch = Replay(
        {
            _opsin(q): (404, _fx("opsin_failure.json")),
            _pc_name(q): (503, "busy"),
            _cir(q): (502, "bad gateway"),
        }
    )
    with pytest.raises(Upstream):
        chem.get(id=q)
    chem.fetch = Replay(
        {
            _opsin(q): (404, _fx("opsin_failure.json")),
            _pc_name(q): (404, _fx("pubchem_name_notachemicalzzq.json")),
            _cir(q): (404, "nope"),
        }
    )
    assert "not found by any source" in chem.get(id=q).body


def test_handler_rejects_bad_cas(chem: ChemHandler) -> None:
    chem.fetch = Replay({})
    with pytest.raises(BadInput, match="check digit"):
        chem.get(id="50-78-3")
