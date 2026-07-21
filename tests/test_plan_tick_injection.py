"""``plan_tick`` patent claims-digest refresh hook
(docs/design/patent-authoring-loop.md).

DB-free: the workspace / bound-draft / digest helpers are stubbed, so no store
and no ``claude`` binary. The §12 CLAUDE.md-injection lockdown tests retired
with the claude subprocess path — plan_tick now drives the precis verbs
in-process over the OSS ``tools=`` loop (see ``test_plan_tick_oss``).
"""

from __future__ import annotations

import pytest

import precis.workers.job_types.plan_tick as pt

# ── patent claims-digest refresh hook (docs/design/patent-authoring-loop.md) ──


def test_refresh_patent_digest_delegates_for_patent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.workers.patent_digest as pd
    import precis.workers.planner_prompt as pp
    from precis.utils.workspace import Workspace

    ws = Workspace(path="p", format="tex", entrypoint="main.tex", doc_type="patent")
    monkeypatch.setattr(pt, "_load_parent_workspace", lambda store, rid: ws)
    monkeypatch.setattr(pp, "bound_draft", lambda store, rid: ("frypat", "T", "tex"))

    class _Draft:
        id = 555

    class _Store:
        def get_ref(self, *, kind: str, id: object) -> _Draft:
            return _Draft()

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        pd,
        "refresh_claims_digest",
        lambda store, todo, draft, **kw: calls.append((todo, draft)),
    )
    pt._refresh_patent_claims_digest(_Store(), 999)
    assert calls == [(999, 555)]


def test_refresh_patent_digest_noop_for_non_patent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.workers.patent_digest as pd
    from precis.utils.workspace import Workspace

    ws = Workspace(path="p", format="tex", entrypoint="main.tex", doc_type="paper")
    monkeypatch.setattr(pt, "_load_parent_workspace", lambda store, rid: ws)
    calls: list[int] = []
    monkeypatch.setattr(pd, "refresh_claims_digest", lambda *a, **k: calls.append(1))
    pt._refresh_patent_claims_digest(object(), 999)
    assert calls == []
