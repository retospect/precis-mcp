"""Tests for the ``chem`` kind's name → SMILES resolver.

Every upstream call replays a fixture from ``tests/fixtures/chem/name_resolve``
(see its README for which are recorded vs hand-built); an unmapped URL fails
the test, so nothing reaches the network.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

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
    assert "uncorroborated: CIR unavailable" in res.notes


def test_cir_miss_note_is_distinct_from_unavailable() -> None:
    # A CIR 404 is an answer ("no such record"), not an outage.
    routes = _aspirin_routes()
    routes[_cir("aspirin")] = NOT_FOUND
    res = cr.resolve("aspirin", Replay(routes))
    assert res.status == "resolved"
    assert "not corroborated: CIR gave no answer" in res.notes


def test_cir_disabled_never_calls_it() -> None:
    fetch = Replay(_aspirin_routes())
    res = cr.resolve("aspirin", fetch, cir_enabled=False)
    assert res.status == "resolved"
    assert res.answer is not None
    assert res.answer.source == "PubChem CID 2244"
    assert ("CIR", "skipped (disabled)") in res.tiers
    assert not any("cactus" in url for url in fetch.calls)
    assert "uncorroborated: CIR unavailable" in res.notes


def _locant_probe_routes() -> dict[str, tuple[int, str] | Exception]:
    """dichloroethane refusal sweep: OPSIN resolves the in-range locant
    variants (2,2- parses to the 1,1- structure) and 404s the rest."""
    fail = _fx("opsin_1,3-dichloroethane.json")
    return {
        _opsin("dichloroethane"): (404, _fx("opsin_failure.json")),
        _pc_name("dichloroethane"): (200, _fx("pubchem_name_dichloroethane.json")),
        _pc_props("6365"): (200, _fx("pubchem_cid_6365.json")),
        _opsin("1,2-Dichloroethane"): (200, _fx("opsin_1,2-dichloroethane.json")),
        _opsin("2,2-Dichloroethane"): (200, _fx("opsin_2,2-dichloroethane.json")),
        _opsin("1,3-Dichloroethane"): (404, fail),
        _opsin("2,3-Dichloroethane"): (404, fail),
        _opsin("3,3-Dichloroethane"): (404, fail),
    }


def test_locant_refusal_lists_every_isomer() -> None:
    res = cr.resolve(
        "dichloroethane", Replay(_locant_probe_routes()), cir_enabled=False
    )
    assert res.status == "ambiguous"
    assert [c.name for c in res.candidates] == [
        "1,1-Dichloroethane",
        "1,2-Dichloroethane",
    ]
    assert {c.smiles for c in res.candidates} == {"CC(Cl)Cl", "ClCCCl"}
    assert any("substituent positions" in n for n in res.notes)
    assert any(t == "OPSIN-locants" for t, _ in res.tiers)


def test_locant_enumeration_skipped_when_opsin_disabled() -> None:
    fetch = Replay(
        {
            _pc_name("dichloroethane"): (200, _fx("pubchem_name_dichloroethane.json")),
            _pc_props("6365"): (200, _fx("pubchem_cid_6365.json")),
        }
    )
    res = cr.resolve("dichloroethane", fetch, cir_enabled=False, opsin_enabled=False)
    assert res.status == "ambiguous"
    assert [c.name for c in res.candidates] == ["1,1-Dichloroethane"]
    assert not any("ebi.ac.uk" in url for url in fetch.calls)
    assert any("not enumerated" in n for n in res.notes)


def test_locant_enumeration_aborts_on_opsin_fault() -> None:
    routes = _locant_probe_routes()
    routes[_opsin("1,2-Dichloroethane")] = TimeoutError("handshake timed out")
    res = cr.resolve("dichloroethane", Replay(routes), cir_enabled=False)
    assert res.status == "ambiguous"
    assert [c.name for c in res.candidates] == ["1,1-Dichloroethane"]
    assert any("could not enumerate" in n for n in res.notes)


def test_locant_refusal_stays_fast_offline() -> None:
    # The ~9.9s post-PubChem stall was the lazy `from rdkit import Chem`
    # in _rdkit_canonical; it now warms at module import. Warm it here so
    # the bound covers the request path, not the one-time import.
    cr._rdkit_canonical("CC(Cl)Cl")
    t0 = time.monotonic()
    res = cr.resolve(
        "dichloroethane", Replay(_locant_probe_routes()), cir_enabled=False
    )
    elapsed = time.monotonic() - t0
    assert res.status == "ambiguous"
    assert len(res.candidates) == 2
    assert elapsed < 2.0


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


# ── r38 dogfood: CIR unreachable from prod ───────────────────────────


@pytest.mark.parametrize(
    ("query", "title", "expect"),
    [
        ("dichloroethane", "1,1-Dichloroethane", True),
        ("propanol", "1-Propanol", True),
        ("Dichloro ethane", "1,2-Dichloroethane", True),
        ("aspirin", "Aspirin", False),
        ("glucose", "D-Glucose", False),
        ("isopropanol", "Isopropyl Alcohol", False),
        ("1,1-dichloroethane", "1,1-Dichloroethane", False),
    ],
)
def test_underspecified_locants(query: str, title: str, expect: bool) -> None:
    assert cr.underspecified_locants(query, title) is expect


def _dce_cir_down() -> Replay:
    # Prod at r38: CIR fails the TLS handshake, PubChem picks the 1,1-isomer.
    routes = _locant_probe_routes()
    routes[_cir("dichloroethane")] = TimeoutError("handshake timed out")
    return Replay(routes)


def test_underspecified_name_refused_when_cir_is_down() -> None:
    res = cr.resolve("dichloroethane", _dce_cir_down())
    assert res.status == "ambiguous"
    assert [c.name for c in res.candidates] == [
        "1,1-Dichloroethane",
        "1,2-Dichloroethane",
    ]
    assert any("substituent positions" in n for n in res.notes)


def test_pubchem_miss_with_cir_down_is_not_found() -> None:
    q = "notachemicalzzq"
    fetch = Replay(
        {
            _opsin(q): (404, _fx("opsin_failure.json")),
            _pc_name(q): (404, _fx("pubchem_name_notachemicalzzq.json")),
            _cir(q): TimeoutError("handshake timed out"),
        }
    )
    res = cr.resolve(q, fetch)
    assert res.status == "not_found"
    assert "CIR was unreachable; OPSIN and PubChem found nothing" in res.notes


def test_cir_leg_uses_its_own_fetcher() -> None:
    main = Replay({k: v for k, v in _aspirin_routes().items() if "cactus" not in k})
    cir = Replay({_cir("aspirin"): (200, "CC(=O)Oc1ccccc1C(O)=O\n")})
    res = cr.resolve("aspirin", main, cir)
    assert res.status == "resolved"
    assert cir.calls == [_cir("aspirin")]


def test_opsin_leg_uses_its_own_fetcher() -> None:
    main = Replay({k: v for k, v in _aspirin_routes().items() if "ebi.ac.uk" not in k})
    opsin = Replay({_opsin("aspirin"): (404, _fx("opsin_failure.json"))})
    res = cr.resolve("aspirin", main, opsin_fetch=opsin)
    assert res.status == "resolved"
    assert opsin.calls == [_opsin("aspirin")]


def test_opsin_disabled_skips_to_pubchem() -> None:
    fetch = Replay(_aspirin_routes())
    res = cr.resolve("aspirin", fetch, opsin_enabled=False)
    assert res.status == "resolved"
    assert res.answer is not None
    assert res.answer.source == "PubChem CID 2244"
    assert ("OPSIN", "skipped (disabled)") in res.tiers
    assert not any("ebi.ac.uk" in url for url in fetch.calls)


def test_handler_opsin_off_via_env_never_calls_it(
    chem: ChemHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = Replay(_aspirin_routes())
    chem.fetch = fetch
    monkeypatch.setenv("PRECIS_CHEM_OPSIN_ENABLED", "0")
    body = chem.get(id="aspirin").body
    assert "OPSIN: skipped (disabled)" in body
    assert not any("ebi.ac.uk" in url for url in fetch.calls)


def test_handler_renders_underspecified_refusal(chem: ChemHandler) -> None:
    chem.fetch = _dce_cir_down()
    body = chem.get(id="dichloroethane").body
    assert "REFUSED: ambiguous, 2 readings" in body
    assert "1. 1,1-Dichloroethane: CC(Cl)Cl" in body
    assert "2. 1,2-Dichloroethane: ClCCCl" in body


def test_handler_cir_off_via_env_never_calls_it(
    chem: ChemHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = Replay(_aspirin_routes())
    chem.fetch = fetch
    monkeypatch.setenv("PRECIS_CHEM_CIR_ENABLED", "0")
    body = chem.get(id="aspirin").body
    assert "uncorroborated: CIR unavailable" in body
    assert "CIR: skipped (disabled)" in body
    assert not any("cactus" in url for url in fetch.calls)


def test_short_legs_keep_their_8s_timeout(
    chem: ChemHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[dict[str, Any]] = []

    def fake_http_fetch(**kwargs: Any) -> cr.Fetch:
        made.append(kwargs)
        return Replay(_aspirin_routes())

    monkeypatch.delenv("PRECIS_CHEM_CIR_ENABLED", raising=False)
    monkeypatch.delenv("PRECIS_CHEM_OPSIN_ENABLED", raising=False)
    monkeypatch.setattr("precis.handlers.chem.http_fetch", fake_http_fetch)
    chem.get(id="aspirin")
    assert made == [
        {},
        {"timeout_s": 8.0, "attempts": 1},
        {"timeout_s": 8.0, "attempts": 1},
    ]


def test_handler_cir_off_builds_no_cir_fetcher(
    chem: ChemHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[dict[str, Any]] = []

    def fake_http_fetch(**kwargs: Any) -> cr.Fetch:
        made.append(kwargs)
        return Replay(_aspirin_routes())

    monkeypatch.setenv("PRECIS_CHEM_CIR_ENABLED", "0")
    monkeypatch.delenv("PRECIS_CHEM_OPSIN_ENABLED", raising=False)
    monkeypatch.setattr("precis.handlers.chem.http_fetch", fake_http_fetch)
    chem.get(id="aspirin")
    assert made == [{}, {"timeout_s": 8.0, "attempts": 1}]


def test_handler_opsin_off_builds_no_opsin_fetcher(
    chem: ChemHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[dict[str, Any]] = []

    def fake_http_fetch(**kwargs: Any) -> cr.Fetch:
        made.append(kwargs)
        return Replay(_aspirin_routes())

    monkeypatch.delenv("PRECIS_CHEM_CIR_ENABLED", raising=False)
    monkeypatch.setenv("PRECIS_CHEM_OPSIN_ENABLED", "0")
    monkeypatch.setattr("precis.handlers.chem.http_fetch", fake_http_fetch)
    chem.get(id="aspirin")
    assert made == [{}, {"timeout_s": 8.0, "attempts": 1}]
