"""Shared fixtures/builders for the survey-extraction test suites
(``test_extract_survey.py`` library tests, ``test_survey_extract_job.py``
dispatch tests). Plain functions + a ``world`` builder — the two files
import what they need; nothing here runs as a test itself.
"""

from __future__ import annotations

from typing import Any

from precis.extract.proposals import vocab_entry
from precis.store import Store
from precis.taxonomy.nodes import slugify
from precis.utils.llm.router import LlmResult, Tier
from precis.workers.executors._context import DispatchContext
from precis.workers.executors._yield import Done, Yield
from tests.workers._helpers import seed_chunk, seed_ref

CHUNK1 = (
    "The Cu NWA catalyst reached 95% Faradaic efficiency for NH3 at -0.5 V "
    "vs RHE. Its total current density was 1000 mA cm-2 under the same "
    "conditions."
)
CHUNK2 = "No results are reported here; the section is purely descriptive."
CHUNK3 = (
    "The Pd membrane gave 60%NO3 selectivity. Adsorption energies for *NO "
    "ranged from -1.87 to -0.80 eV."
)


def make_taxon(
    store: Store,
    name: str,
    *,
    unit: str | None = None,
    required: list[str] | None = None,
    parent: int | None = None,
    aliases: list[str] | None = None,
) -> int:
    meta: dict[str, Any] = {
        "name": name,
        "norm_name": name.lower(),
        "slug": slugify(name),
        "definition": f"{name}.",
        "aliases": aliases or [],
        "status": "proposed",
    }
    if unit is not None:
        meta["canonical_unit"] = unit
    if required is not None:
        meta["required_conditions"] = required
    ref = store.insert_ref(kind="taxon", slug=None, title=name, meta=meta)
    if parent is not None:
        store.add_link(src_ref_id=ref.id, dst_ref_id=parent, relation="specialises")
    return ref.id


def ref_meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def survey_world(store: Store) -> dict[str, Any]:
    """The measurand tree, one fixture paper, three chunks, the vocab."""
    root = make_taxon(store, "measurand")
    w: dict[str, Any] = {"store": store, "root": root}
    w["fe"] = make_taxon(
        store,
        "faradaic-efficiency",
        unit="%",
        required=["product", "applied-potential"],
        parent=root,
    )
    w["jtot"] = make_taxon(store, "total-current-density", unit="A m-2", parent=root)
    w["ads"] = make_taxon(store, "adsorption-energy", unit="eV", parent=root)
    w["potential"] = make_taxon(
        store, "applied-electrode-potential", unit="V", parent=root
    )
    w["product"] = make_taxon(store, "reaction-product", parent=root)
    w["paper"] = seed_ref(store, title="A synthetic survey", kind="paper")
    w["c1"] = seed_chunk(store, ref_id=w["paper"], text=CHUNK1, ord=0)
    w["c2"] = seed_chunk(store, ref_id=w["paper"], text=CHUNK2, ord=1)
    w["c3"] = seed_chunk(store, ref_id=w["paper"], text=CHUNK3, ord=2)
    w["vocab"] = {
        rid: vocab_entry(rid, ref_meta(store, rid))
        for rid in (w["fe"], w["jtot"], w["ads"], w["potential"], w["product"])
    }
    return w


def fake_ctx(
    store: Store,
    meta: dict[str, Any],
    events: list | None = None,
    ref_id: int = 1,
) -> DispatchContext:
    out = events if events is not None else []
    return DispatchContext(
        store=store,
        ref_id=ref_id,
        title="survey_extract",
        meta=meta,
        set_status=lambda s: out.append(("status", s)),
        append_chunk=lambda k, t: out.append((k, t)),
        set_meta=lambda **kw: out.append(("meta", kw)),
        record_failure=lambda r, **kw: out.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=lambda: False,
    )


def llm_result(text: str, model: str = "test-local") -> LlmResult:
    return LlmResult(text=text, cost_usd=0.0, turns_used=1, model=model, tier=Tier.BIG)


def drive(ctx_meta: dict[str, Any], store: Store, spec: Any) -> tuple[Done, list]:
    """Run a coordinator dispatch through its yields until Done."""
    state: dict[str, Any] = {}
    events: list = []
    dispatch = spec.dispatch
    assert dispatch is not None
    for _ in range(200):
        meta = dict(ctx_meta)
        if state:
            meta["coordinator_state"] = state
        out = dispatch(fake_ctx(store, meta, events), spec)
        if isinstance(out, Done):
            return out, events
        assert isinstance(out, Yield)
        state = out.state
    raise AssertionError("dispatch never finished")


def fe_reply(**kw: Any) -> dict[str, Any]:
    """A reader reply carrying one anchored FE run."""
    result = {
        "subject": "Cu NWA",
        "measurand": "faradaic-efficiency",
        "literal": "95",
        "unit": "%",
        "anchor": "0",
        "attribution": "own_work",
        "inputs": [
            {
                "condition": "product",
                "measurand": "reaction-product",
                "literal": "NH3",
            },
            {
                "condition": "applied-potential",
                "measurand": "applied-electrode-potential",
                "literal": "-0.5",
                "unit": "V",
            },
        ],
    }
    result.update(kw)
    return {"results": [result]}


__all__ = [
    "CHUNK1",
    "CHUNK2",
    "CHUNK3",
    "drive",
    "fake_ctx",
    "fe_reply",
    "llm_result",
    "make_taxon",
    "ref_meta",
    "survey_world",
]
