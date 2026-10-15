"""Tests for the template reaction classifier (``_rxn_classify``)."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _rxn_classify as rc
from precis.handlers.rxn import RxnHandler

pytest.importorskip("rdkit")


@pytest.mark.parametrize(
    ("rxn", "expect"),
    [
        # amide coupling, acid + amine, with coupling agents in the middle
        (
            "CC(=O)O.NCc1ccccc1>CN(C)C=O.CCN=C=NCCCN(C)C>CC(=O)NCc1ccccc1",
            "RXNO:0000357",
        ),
        # amide from acyl chloride
        ("O=C(Cl)c1ccccc1.CNC>>CN(C)C(=O)c1ccccc1", "RXNO:0000357"),
        # Fischer-type esterification
        ("CC(=O)O.OCC>>CC(=O)OCC.O", "RXNO:0000360"),
        # Boc protection: the carbamate parent is dropped for the child
        (
            "NCc1ccccc1.CC(C)(C)OC(=O)OC(=O)OC(C)(C)C>>CC(C)(C)OC(=O)NCc1ccccc1",
            "RXNO:0000079",
        ),
        # Boc deprotection
        ("CC(C)(C)OC(=O)NCc1ccccc1>OC(=O)C(F)(F)F>NCc1ccccc1", "RXNO:0000326"),
        # Suzuki-Miyaura
        ("Brc1ccccc1.OB(O)c1ccc(C)cc1>[Pd]>Cc1ccc(-c2ccccc2)cc1", "RXNO:0000140"),
        # Buchwald-Hartwig
        ("Brc1ccccc1.C1COCCN1>[Pd]>c1ccc(N2CCOCC2)cc1", "RXNO:0000192"),
        # SNAr on an aryl fluoride
        (
            "O=[N+]([O-])c1ccc(F)cc1.C1CCNCC1>>O=[N+]([O-])c1ccc(N2CCCCC2)cc1",
            "MOP:0000791",
        ),
        # Sonogashira
        ("Ic1ccccc1.C#CCO>>OCC#Cc1ccccc1", "RXNO:0000137"),
        # reductive amination
        ("O=Cc1ccccc1.NC1CC1>[BH3-]C#N.[Na+]>c1ccc(CNC2CC2)cc1", "RXNO:0000335"),
        # Williamson ether
        ("Oc1ccccc1.BrCC>>CCOc1ccccc1", "RXNO:0000090"),
        # nitro reduction
        ("O=[N+]([O-])c1ccccc1>[Pd].[H][H]>Nc1ccccc1", "RXNO:0000337"),
        # secondary alcohol oxidation
        ("CC(O)c1ccccc1>>CC(=O)c1ccccc1", "MOP:0000574"),
        # ketone reduction
        ("CC(=O)c1ccccc1>[BH4-].[Na+]>CC(O)c1ccccc1", "MOP:0000580"),
        # Diels-Alder
        ("C=CC=C.C=CC(=O)OC>>COC(=O)C1CC=CCC1", "RXNO:0000006"),
        # sulfonamide
        ("Cc1ccc(S(=O)(=O)Cl)cc1.NCC>>CCNS(=O)(=O)c1ccc(C)cc1", "MOP:0002524"),
        # ester hydrolysis
        ("CCOC(=O)c1ccccc1>[OH-].[Na+]>O=C(O)c1ccccc1", "MOP:0000619"),
        # Grignard addition
        ("O=Cc1ccccc1.C[Mg]Br>>CC(O)c1ccccc1", "RXNO:0000014"),
    ],
)
def test_template_reproduces_product(rxn: str, expect: str) -> None:
    got = rc.classify(rxn)
    assert got.best is not None, got.note
    assert got.best.id == expect, got.render()
    assert got.best.name == rc.class_name(expect)


def test_boc_protection_is_unambiguous() -> None:
    got = rc.classify(
        "NCc1ccccc1.CC(C)(C)OC(=O)OC(=O)OC(C)(C)C>>CC(C)(C)OC(=O)NCc1ccccc1"
    )
    assert got.best is not None
    assert got.best.confidence == pytest.approx(0.9)
    assert got.alternatives == []


def test_unprecedented_transformation_is_unclassified() -> None:
    # A ring contraction no template describes: refuse, do not guess.
    got = rc.classify("C1CCCCC1=O>>O=C(O)C1CCCC1")
    assert got.best is None
    assert got.to_meta() == {}
    assert "unclassified" in got.render()


def test_reactant_match_without_product_does_not_count() -> None:
    # Acid + amine present, but the product is not the amide.
    got = rc.classify("CC(=O)O.NCc1ccccc1>>CCO")
    assert got.best is None or got.best.id != "RXNO:0000357"


def test_malformed_reaction_raises() -> None:
    with pytest.raises(ValueError):
        rc.classify("CCO")
    with pytest.raises(ValueError):
        rc.classify("CC(=O)O.notasmiles>>CC")


def test_every_table_class_is_a_live_rxno_id() -> None:
    names = rc.rxno_names()
    for cid in rc.class_ids():
        assert names.get(cid), cid


def test_meta_shape() -> None:
    meta = rc.classify("CC(=O)O.OCC>>CC(=O)OCC.O").to_meta()
    assert meta["reaction_class"] == "RXNO:0000360"
    assert meta["class_name"] == "O-acylation to ester"
    assert meta["class_source"] == rc.CLASSIFIER_VERSION
    assert 0 < meta["class_confidence"] <= 1


# ── rxn handler integration ──────────────────────────────────────────


def _rxn(store: Any) -> RxnHandler:
    return RxnHandler(hub=Hub(store=store))


def test_put_without_class_is_auto_classified(store: Any) -> None:
    h = _rxn(store)
    body = h.put(id="etoac", rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC.O").body
    assert "class: RXNO:0000360 O-acylation to ester (confidence 0.9" in body
    meta = store.get_ref(kind="rxn", id="etoac").meta
    assert meta["reaction_class"] == "RXNO:0000360"
    assert meta["class_source"] == rc.CLASSIFIER_VERSION
    page = h.get(id="etoac").body
    assert "class: RXNO:0000360 O-acylation to ester" in page
    assert "O-acylation to ester" in h.get().body


def test_caller_class_wins_and_survives_a_smiles_update(store: Any) -> None:
    h = _rxn(store)
    h.put(
        id="etoac", rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC.O", reaction_class="RXNO:0000167"
    )
    h.put(id="etoac", rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC")
    meta = store.get_ref(kind="rxn", id="etoac").meta
    assert meta["reaction_class"] == "RXNO:0000167"
    assert meta["class_source"] == "caller"
    assert "Fischer-Speier esterification (given by caller)" in h.get(id="etoac").body


def test_unknown_caller_class_is_noted(store: Any) -> None:
    body = (
        _rxn(store)
        .put(
            id="x", rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC.O", reaction_class="RXNO:9999999"
        )
        .body
    )
    assert "not an RXNO/MOP id" in body


def test_new_smiles_clears_a_stale_classifier_label(store: Any) -> None:
    h = _rxn(store)
    h.put(id="r", rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC.O")
    body = h.put(id="r", rxn_smiles="C1CCCCC1=O>>O=C(O)C1CCCC1").body
    assert "unclassified" in body
    assert not store.get_ref(kind="rxn", id="r").meta.get("reaction_class")


def test_view_classify_by_smiles_and_by_ref(store: Any) -> None:
    h = _rxn(store)
    by_q = h.get(
        view="classify", q="Brc1ccccc1.OB(O)c1ccccc1>>c1ccc(-c2ccccc2)cc1"
    ).body
    assert "RXNO:0000140 Suzuki-Miyaura coupling" in by_q
    h.put(
        id="etoac", rxn_smiles="CC(=O)O.OCC>>CC(=O)OCC.O", reaction_class="RXNO:0000167"
    )
    by_id = h.get(id="etoac", view="classify").body
    assert "RXNO:0000360" in by_id
    assert "stored class: RXNO:0000167" in by_id
    with pytest.raises(BadInput):
        h.get(view="classify")


# ── route steps ──────────────────────────────────────────────────────


def test_classify_route_labels_steps_and_renders() -> None:
    from precis_chem.classify import classify_route
    from precis_chem.ir import RouteGraph, RouteStep

    graph = RouteGraph(
        target="CC(=O)NCc1ccccc1",
        engine="stub",
        engine_version="0",
        steps=[
            RouteStep(
                id=1, product="CC(=O)NCc1ccccc1", reactants=["CC(=O)O", "NCc1ccccc1"]
            ),
            RouteStep(id=2, product="C1CCCCC1", reactants=["c1ccccc1"]),
        ],
    )
    out = classify_route(graph)
    assert out.steps[0].reaction_class == "RXNO:0000357"
    assert out.steps[1].reaction_class is None
    text = out.render()
    assert "class: RXNO:0000357 N-acylation to amide (0.9)" in text
    again = RouteGraph.from_json(out.to_json())
    assert again.steps[0].class_name == "N-acylation to amide"
