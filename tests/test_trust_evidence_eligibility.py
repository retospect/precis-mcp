"""graph-node-trust-state slice 2a — the shared evidence-eligibility
predicate (`src/precis/taproot/eligibility.py`) and its three consumers:
taproot support counting, the nanopub mint gate, the draft cite nudge.

The pure half needs no DB; the consumer half reuses the hub/paper seeding of
`tests/test_nanopub_gates_mint.py`. No defined grounding is stored yet, so
the consumers are driven by monkeypatching `eligibility.edge_groundings`."""

from __future__ import annotations

import itertools
import typing
from typing import Any

import pytest

from precis.nanopub import evidence, gates
from precis.store.types import _INVERSE_RELATIONS, Relation
from precis.taproot import eligibility as el
from precis.taproot.hub import HUB_ROLES, attach_evidence
from precis.taproot.lookup import hubs_grounded_by_paper
from precis.taproot.seniority import derive_evidence, derive_evidence_bulk
from precis.utils import handle_registry
from tests.test_nanopub_gates_mint import _seed_hub, _seed_paper
from tests.test_taproot_hub import _seed_pathway
from tests.workers._helpers import seed_ref

G = el.Grounding
_DEFINED = frozenset({G.DEFINED})


# ── pure predicate ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("slug", "canon", "cls"),
    [
        ("specialises", "specialises", el.RelationClass.STRUCTURAL),
        ("generalises", "specialises", el.RelationClass.STRUCTURAL),
        ("part-of", "part-of", el.RelationClass.STRUCTURAL),
        ("contains", "part-of", el.RelationClass.STRUCTURAL),
        ("represented-by", "represents", el.RelationClass.STRUCTURAL),
        ("realizes", "realized-by", el.RelationClass.STRUCTURAL),
        ("used-in", "made-of", el.RelationClass.STRUCTURAL),
        ("qualified-by", "qualifies", el.RelationClass.STRUCTURAL),
        ("has-instance", "instance-of", el.RelationClass.STRUCTURAL),
        ("establishes", "establishes", el.RelationClass.EVIDENTIAL),
        ("corroborates", "corroborates", el.RelationClass.EVIDENTIAL),
        ("contradicts", "contradicts", el.RelationClass.EVIDENTIAL),
        ("contradicted-by", "contradicts", el.RelationClass.EVIDENTIAL),
        ("refines", "refines", el.RelationClass.EVIDENTIAL),
        ("motivated-by", "motivated-by", el.RelationClass.OTHER),
        ("cites", "cites", el.RelationClass.OTHER),
        ("no-such-slug", "no-such-slug", el.RelationClass.OTHER),
    ],
)
def test_relation_classing(slug: str, canon: str, cls: el.RelationClass) -> None:
    assert el.canonical_relation(slug) == canon
    assert el.relation_class(slug) is cls


def test_structural_edge_never_supports() -> None:
    for slug in ("specialises", "generalises", "part-of", "contains"):
        use = el.evidence_use(slug, {G.SOURCED})
        assert (use.eligible, use.carries_support, use.rule) == (
            False,
            False,
            el.RULE_NOT_EVIDENTIAL,
        )


@pytest.mark.parametrize("role", ["establishes", "corroborates"])
@pytest.mark.parametrize(
    "gs",
    [{G.NONE}, {G.SOURCED}, {G.VERIFIED}, {G.SOURCED, G.DEFINED}],
)
def test_support_stays_eligible(role: str, gs: set[G]) -> None:
    assert el.evidence_use(role, gs) == el.EvidenceUse(True, True, None)


def test_definition_only_refused() -> None:
    use = el.evidence_use("establishes", {G.DEFINED})
    assert use == el.EvidenceUse(False, False, el.RULE_DEFINITION_ONLY)
    assert el.is_definition_only({G.DEFINED})
    assert not el.is_definition_only(set())
    assert not el.is_definition_only({G.DEFINED, G.SOURCED})


@pytest.mark.parametrize("side", ["src_groundings", "dst_groundings"])
def test_definition_only_endpoint_disqualifies_verified_edge(side: str) -> None:
    use = el.evidence_use("corroborates", {G.VERIFIED}, **{side: {G.DEFINED}})
    assert use.rule == el.RULE_DEFINITION_ONLY and not use.eligible


def test_trusted_endpoint_does_not_upgrade_definition_only_edge() -> None:
    use = el.evidence_use(
        "corroborates",
        {G.DEFINED},
        src_groundings={G.VERIFIED},
        dst_groundings={G.VERIFIED},
    )
    assert use.rule == el.RULE_DEFINITION_ONLY


def test_refines_and_contradicts_do_not_carry_support() -> None:
    assert el.evidence_use("refines", {G.SOURCED}) == el.EvidenceUse(
        True, False, el.RULE_ADVISORY
    )
    for slug in ("contradicts", "contradicted-by"):
        assert el.evidence_use(slug, {G.SOURCED}) == el.EvidenceUse(
            True, False, el.RULE_COUNTER
        )


def test_motivated_by_is_not_evidential() -> None:
    use = el.evidence_use("motivated-by", {G.MOTIVATED})
    assert (use.eligible, use.rule) == (False, el.RULE_NOT_EVIDENTIAL)


def test_edge_groundings_derivation_never_defines() -> None:
    metas: list[dict[str, Any] | None] = [
        None,
        {},
        {"source_handle": "pc1"},
        {"verified_by": "x"},
        {"verified_by": "x", "verified_claim_sha": "abc"},
        {"defined": True, "grounding": "defined", "definition": "text"},
    ]
    for relation in ("establishes", "motivated-by", "specialises"):
        for chunk in (None, 5):
            for meta in metas:
                assert G.DEFINED not in el.edge_groundings(relation, chunk, meta)
    assert el.edge_groundings("establishes", None, None) == {G.NONE}
    assert el.edge_groundings("establishes", 5, None) == {G.SOURCED}
    assert el.edge_groundings(
        "establishes", None, {"verified_by": "x", "verified_claim_sha": "s"}
    ) == {G.VERIFIED}
    assert el.edge_groundings("motivated-by", None, {}) == {G.MOTIVATED}


def test_support_relations_pin_hub_roles() -> None:
    assert HUB_ROLES - {"contradicts"} == el.SUPPORT_RELATIONS


def test_classed_slugs_are_registered_relations() -> None:
    registered = set(typing.get_args(Relation))
    classed = el.STRUCTURAL_RELATIONS | el.EVIDENTIAL_RELATIONS
    inverses = {_INVERSE_RELATIONS[s] for s in classed if s in _INVERSE_RELATIONS}
    missing = (classed | inverses) - registered
    assert not missing, missing


def test_inverse_map_matches_db(store: Any) -> None:
    classed = el.STRUCTURAL_RELATIONS | el.EVIDENTIAL_RELATIONS
    for slug in classed:
        assert store.inverse_relation(slug) == _INVERSE_RELATIONS.get(slug), slug


# ── consumers ───────────────────────────────────────────────────────────────

_n = itertools.count(1)


def _hub_with_edge(store: Any) -> tuple[int, int, int]:
    """(hub, paper, chunk) with one pinned corroborates edge."""
    n = next(_n)
    paper, chunk, _sha = _seed_paper(store, title=f"Eligibility paper {n}")
    hub = _seed_hub(
        store, f"Eligibility claim {n} holds at room temperature.", paper, chunk
    )
    return hub, paper, chunk


def _defined(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(el, "edge_groundings", lambda *a, **k: _DEFINED)


def test_derive_evidence_rejects_definition_only(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, paper, _ = _hub_with_edge(store)
    base = derive_evidence(store, hub)
    assert [e.paper_ref_id for e in base.originators + base.corroborators] == [paper]
    assert base.ineligible == []

    _defined(monkeypatch)
    for ev in (derive_evidence(store, hub), derive_evidence_bulk(store, [hub])[hub]):
        assert ev.originators == [] and ev.corroborators == []
        assert ev.grounding == []
        assert [(i.src_ref_id, i.relation, i.rule) for i in ev.ineligible] == [
            (paper, "corroborates", el.RULE_DEFINITION_ONLY)
        ]


def test_mint_gate_rejects_definition_only(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, paper, chunk = _hub_with_edge(store)
    payload: dict[str, Any] = {"passages": [], "fields": {}}
    bundle = evidence.load_bundle(store, hub)
    assert bundle.ineligible_evidence == []
    base = gates.run_mint_gates(store, bundle, payload)
    assert "definition-only-evidence" not in {v.gate for v in base}

    _defined(monkeypatch)
    bundle = evidence.load_bundle(store, hub)
    hits = [
        v
        for v in gates.run_mint_gates(store, bundle, payload)
        if v.gate == "definition-only-evidence"
    ]
    assert len(hits) == 1
    assert el.DEFINITION_ONLY_MESSAGE in hits[0].message


def test_cite_nudge_rejects_definition_only(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, paper, _ = _hub_with_edge(store)
    got = hubs_grounded_by_paper(store, paper, require_pub_id=False)
    assert [h["hub_ref_id"] for h in got] == [hub]
    assert set(got[0]) == {"hub_ref_id", "pub_id", "claim", "role"}

    _defined(monkeypatch)
    assert hubs_grounded_by_paper(store, paper, require_pub_id=False) == []


# ── definition beside non-evidential groundings ─────────────────────────────


@pytest.mark.parametrize("other", [G.NONE, G.MOTIVATED])
def test_definition_with_non_evidential_grounding_is_definition_only(
    other: G,
) -> None:
    gs = {G.DEFINED, other}
    assert el.is_definition_only(gs)
    use = el.evidence_use("establishes", gs)
    assert (use.eligible, use.rule) == (False, el.RULE_DEFINITION_ONLY)
    for side in ("src_groundings", "dst_groundings"):
        use = el.evidence_use("establishes", {G.SOURCED}, **{side: gs})
        assert (use.eligible, use.rule) == (False, el.RULE_DEFINITION_ONLY)


def test_definition_with_verified_still_supports() -> None:
    gs = {G.DEFINED, G.VERIFIED}
    assert not el.is_definition_only(gs)
    assert el.evidence_use("establishes", gs) == el.EvidenceUse(True, True, None)


# ── pathway / patent sources ────────────────────────────────────────────────


def _definition_only_messages(store: Any, hub: int) -> list[str]:
    bundle = evidence.load_bundle(store, hub)
    payload: dict[str, Any] = {"passages": [], "fields": {}}
    return [
        v.message
        for v in gates.run_mint_gates(store, bundle, payload)
        if v.gate == "definition-only-evidence"
    ]


def test_pathway_edge_definition_only_is_ineligible(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = next(_n)
    hub, _paper, _chunk = _hub_with_edge(store)
    pathway = _seed_pathway(store, slug=f"elig-rx-{n}", content_key=f"sha-elig-{n}")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=pathway, role="corroborates")

    base = derive_evidence(store, hub)
    assert [c.pathway_ref_id for c in base.computed] == [pathway]
    assert base.ineligible == []

    _defined(monkeypatch)
    for ev in (derive_evidence(store, hub), derive_evidence_bulk(store, [hub])[hub]):
        assert ev.computed == []
        pw = [i for i in ev.ineligible if i.src_ref_id == pathway]
        assert [(i.src_kind, i.rule) for i in pw] == [
            ("pathway", el.RULE_DEFINITION_ONLY)
        ]

    handle = handle_registry.format_handle("pathway", pathway)
    msgs = _definition_only_messages(store, hub)
    assert any(handle in m for m in msgs), msgs


def test_patent_edge_definition_only_names_patent_handle(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = next(_n)
    hub, _paper, _chunk = _hub_with_edge(store)
    patent = seed_ref(store, title=f"EP elig patent {n}", kind="patent")
    attach_evidence(store, hub_ref_id=hub, paper_ref_id=patent, role="corroborates")

    _defined(monkeypatch)
    handle = handle_registry.format_handle("patent", patent)
    msgs = _definition_only_messages(store, hub)
    assert any(handle in m for m in msgs), msgs
