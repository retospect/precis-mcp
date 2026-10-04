"""The claim page's gate panel shows ``term-coverage`` (a warning) at the
approve step, before anything is frozen. DB-backed."""

from __future__ import annotations

from typing import Any

from precis_web.nanopub_render import hub_context
from tests.test_nanopub_gates_mint import _seed_hub, _seed_paper


def test_approve_view_carries_the_warning_before_anything_is_frozen(
    store: Any,
) -> None:
    paper, chunk, _sha = _seed_paper(store)
    hub = _seed_hub(
        store, "TEM shows MOFs can be anisotropic up to 400:1.", paper, chunk
    )
    ctx = hub_context(store, hub, embedder=None)
    assert ctx is not None and ctx["state"] == "unminted"
    cov = [i for i in ctx["preflight"] if i.check == "term-coverage"]
    assert len(cov) == 1 and cov[0].blocking is False
    row = next(g for g in ctx["gates"]["preflight"] if g["name"] == "term-coverage")
    assert row["status"] == "note" and "TEM" in row["message"]
