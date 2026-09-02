"""The claim page's provenance DAG — the compound papers row.

Regression pin for the fi211522 report (2026-08-28): a compound hub's DAG
rendered zero papers — grounding lives on its conjunct atoms by the
compound-shape gate, but ``_graph`` only read the hub's own evidence.
(The report's other half — dead clicks after the workbench's innerHTML
swap — is pinned by test_nanopub_routes.py's swapped-fragment-scripts test.)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis_web import nanopub_render


def _src(ref_id: int, role: str = "corroborates") -> SimpleNamespace:
    return SimpleNamespace(
        ref_id=ref_id,
        kind="paper",
        title=f"paper {ref_id}",
        year=2008,
        doi=None,
        pdf_sha256=None,
        role=role,
        via="inbound",
    )


def _bundle(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "hub_ref_id": 99,
        "sentence": "compound sentence",
        "sources": [],
        "contradicts": [],
        "conjunct_atoms": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


class _Store:
    def nanopub_publish_row(self, ref_id: int) -> None:
        return None


class TestCompoundPapersRow:
    def test_atom_evidence_populates_papers_row(self, monkeypatch) -> None:
        # fi211522's shape: hub has no evidence of its own; each atom is
        # corroborated by the same paper. The papers row must show that
        # paper ONCE, with an edge to each atom it grounds — and none to
        # the hub.
        from precis.nanopub import evidence as ev

        atom_bundles = {
            1: _bundle(hub_ref_id=1, sources=[_src(563)]),
            2: _bundle(hub_ref_id=2, sources=[_src(563)]),
        }
        monkeypatch.setattr(
            ev, "load_bundle", lambda store, hub_id: atom_bundles[hub_id]
        )
        bundle = _bundle(conjunct_atoms=[(1, "atom one"), (2, "atom two")])

        g = nanopub_render._graph(_Store(), bundle, None, "compound", blocked=False)

        paper_nodes = [n for n in g["nodes"] if n["id"] == "pc563"]
        assert len(paper_nodes) == 1
        paper_edges = [e for e in g["edges"] if e["src"] == "pc563"]
        assert {e["dst"] for e in paper_edges} == {"fi1", "fi2"}

    def test_hub_own_evidence_keeps_hub_edge(self, monkeypatch) -> None:
        from precis.nanopub import evidence as ev

        monkeypatch.setattr(
            ev,
            "load_bundle",
            lambda store, hub_id: _bundle(hub_ref_id=hub_id, sources=[_src(563)]),
        )
        # The hub carries its own paper too — that edge must survive the
        # atom aggregation (both edges when the same paper grounds both).
        bundle = _bundle(sources=[_src(563)], conjunct_atoms=[(1, "atom one")])

        g = nanopub_render._graph(_Store(), bundle, None, "compound", blocked=False)

        assert len([n for n in g["nodes"] if n["id"] == "pc563"]) == 1
        assert {e["dst"] for e in g["edges"] if e["src"] == "pc563"} == {
            "hub",
            "fi1",
        }

    def test_atomic_claim_unchanged(self) -> None:
        # No atoms: the papers row is the hub's own evidence, edges to hub.
        bundle = _bundle(sources=[_src(7, role="establishes")])
        g = nanopub_render._graph(_Store(), bundle, None, "claim", blocked=False)
        assert [n["id"] for n in g["nodes"] if n["cls"].startswith("paper")] == ["pc7"]
        assert {e["dst"] for e in g["edges"] if e["src"] == "pc7"} == {"hub"}
