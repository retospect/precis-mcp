"""Triage classifies memory gripes (gr657257): a plain gripe whose first
line leads with a ``me<id>:`` handle — or whose verdict names a memory
the text carries — becomes ``project:memory`` and links the memory
``raises-concern-about``; a gripe citing a memory as context stays
ordinary."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.utils.llm.router import LlmResult
from precis.workers import gripe_triage
from tests.workers.test_gripe_triage import (
    _comments,
    _gripe,
    _index,
    _light,
    _llm,
    _open_tag_values,
    _verdict,
)

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _no_release_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "PRECIS_TRIAGE_DEPLOYED_HISTORY", "/nonexistent-deployed-history"
    )


def _run(store: Store, llm: object) -> None:
    gripe_triage.run_gripe_triage_pass(
        store,
        llm=llm,
        notify=False,
        threads=_index(),
        repo=Path("/nonexistent-triage-test-repo"),
    )


def _memory(store: Store, title: str = "Export format") -> int:
    return int(store.insert_ref(kind="memory", slug=None, title=title).id)


def _concerns(store: Store, gid: int) -> set[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'raises-concern-about'",
            (gid,),
        ).fetchall()
    return {int(r[0]) for r in rows}


def test_one_line_memory_gripe_is_tagged_linked_and_routed(store: Store) -> None:
    _light(store)
    mem = _memory(store)
    gid = _gripe(store, f"me{mem}: still says CSV export; the code writes JSON")
    _run(store, _llm({"verdicts": [_verdict(gid, project="precis")]}))
    tags = _open_tag_values(store, gid)
    assert "project:memory" in tags and "project:precis" not in tags
    assert _concerns(store, gid) == {mem}
    comments = _comments(store, gid)
    assert any(c.startswith(f"triage: memory gripe on me{mem}") for c in comments)
    assert any(c.startswith("project: precis -> memory") for c in comments)


def test_context_citation_stays_an_ordinary_gripe(store: Store) -> None:
    _light(store)
    mem = _memory(store)
    gid = _gripe(
        store,
        "search drops the tags filter",
        body=f"repro: the route I followed is recorded in me{mem}",
    )
    _run(store, _llm({"verdicts": [_verdict(gid, project="precis", memory=None)]}))
    assert "project:precis" in _open_tag_values(store, gid)
    assert _concerns(store, gid) == set()


def test_model_named_memory_must_appear_in_the_gripe(store: Store) -> None:
    _light(store)
    named = _memory(store)
    other = _memory(store, "Unrelated")
    gid = _gripe(store, "hub memory is too big", body=f"me{named} runs 9k chars")
    invented = _gripe(store, "some other complaint")
    _run(
        store,
        _llm(
            {
                "verdicts": [
                    _verdict(gid, fix="memory", memory=f"me{named}"),
                    _verdict(invented, fix="memory", memory=f"me{other}"),
                ]
            }
        ),
    )
    assert "project:memory" in _open_tag_values(store, gid)
    assert _concerns(store, gid) == {named}
    assert "project:memory" not in _open_tag_values(store, invented)
    assert _concerns(store, invented) == set()


@pytest.mark.parametrize("fix", ["docs", "code", "skill", "infra"])
def test_artifact_fix_citing_a_memory_keeps_the_repo_project(
    store: Store, fix: str
) -> None:
    """gr666194: a ``fix:docs``/``fix:code`` gripe citing a memory as the
    source of missing content keeps ``project:<repo>`` — the handle is
    evidence, not the target. ``project:memory`` would strand it: the
    gardener only edits memories and the factory excludes the tag."""
    _light(store)
    mem = _memory(store)
    gid = _gripe(
        store,
        "docs/runbooks/dgx-bringup.md is missing the wiring detail",
        body=f"the missing detail lives only in me{mem} — copy it into the runbook",
    )
    _run(
        store,
        _llm({"verdicts": [_verdict(gid, fix=fix, memory=f"me{mem}")]}),
    )
    tags = _open_tag_values(store, gid)
    assert "project:precis" in tags and "project:memory" not in tags
    assert f"fix:{fix}" in tags
    assert _concerns(store, gid) == set()
    assert not any(c.startswith("triage: memory gripe") for c in _comments(store, gid))


@pytest.mark.parametrize("fix", ["memory", "spec"])
def test_non_artifact_fix_honours_the_memory_field(store: Store, fix: str) -> None:
    """The ``memory`` field still routes under a non-artifact fix: the
    verdict's own class names no repo change, so the text's ``me<id>``
    marks a memory complaint (gr666194)."""
    _light(store)
    mem = _memory(store)
    gid = _gripe(store, "hub memory is too big", body=f"me{mem} runs 9k chars")
    _run(
        store,
        _llm({"verdicts": [_verdict(gid, fix=fix, memory=f"me{mem}")]}),
    )
    tags = _open_tag_values(store, gid)
    assert "project:memory" in tags and f"fix:{fix}" in tags
    assert _concerns(store, gid) == {mem}


@pytest.mark.parametrize("fix", ["docs", "code"])
def test_model_picked_memory_project_under_artifact_fix_is_vetoed(
    store: Store, fix: str
) -> None:
    """gr666194/td666796: the artifact-fix veto also guards the model's
    own ``project`` pick, not only its ``memory`` field — a verdict of
    ``fix=docs, project=memory, memory=me<id>`` otherwise strands the
    repo fix in front of the gardener exactly as before."""
    _light(store)
    mem = _memory(store)
    gid = _gripe(
        store,
        "docs/runbooks/dgx-bringup.md is missing the wiring detail",
        body=f"the missing detail lives only in me{mem} — copy it into the runbook",
    )
    _run(
        store,
        _llm(
            {"verdicts": [_verdict(gid, fix=fix, project="memory", memory=f"me{mem}")]}
        ),
    )
    tags = _open_tag_values(store, gid)
    assert "project:precis" in tags and "project:memory" not in tags
    assert f"fix:{fix}" in tags
    assert _concerns(store, gid) == set()
    comments = _comments(store, gid)
    assert not any(c.startswith("triage: memory gripe") for c in comments)
    assert not any(c.startswith("project:") for c in comments)


def test_model_picked_memory_project_stands_under_a_memory_fix(
    store: Store,
) -> None:
    """The veto is scoped to repo fixes: a ``fix:memory`` verdict's
    ``project=memory`` still routes to the gardener even when the gripe
    names no handle — the ask itself is memory work (gr666194)."""
    _light(store)
    gid = _gripe(store, "memory recall keeps surfacing a retired claim")
    _run(
        store,
        _llm({"verdicts": [_verdict(gid, fix="memory", project="memory")]}),
    )
    tags = _open_tag_values(store, gid)
    assert "project:memory" in tags and "fix:memory" in tags
    assert _concerns(store, gid) == set()
    assert any(c.startswith("project: precis -> memory") for c in _comments(store, gid))


def test_retired_memory_and_filer_project_stay_ordinary(store: Store) -> None:
    _light(store)
    gone = _memory(store)
    store.retire_ref(gone)
    live = _memory(store)
    retired_gid = _gripe(store, f"me{gone}: stale")
    filer_gid = _gripe(store, f"me{live}: wrong, but filed against infra")
    store.add_tag(filer_gid, Tag.open("project:infra"), set_by="agent")
    _run(
        store,
        _llm({"verdicts": [_verdict(retired_gid), _verdict(filer_gid)]}),
    )
    assert "project:memory" not in _open_tag_values(store, retired_gid)
    assert _concerns(store, retired_gid) == set()
    assert "project:infra" in _open_tag_values(store, filer_gid)
    assert _concerns(store, filer_gid) == set()


def test_filer_set_memory_project_still_gets_the_link(store: Store) -> None:
    _light(store)
    mem = _memory(store)
    gid = _gripe(store, f"me{mem}: contradicts its hub")
    store.add_tag(gid, Tag.open("project:memory"), set_by="agent")
    _run(store, _llm({"verdicts": [_verdict(gid, project="memory")]}))
    assert _concerns(store, gid) == {mem}
    assert not any(c.startswith("project:") for c in _comments(store, gid))


def test_prompt_asks_for_the_memory_field(store: Store) -> None:
    _light(store)
    gid = _gripe(store, "anything")
    prompts: list[str] = []

    def spy(prompt: str) -> LlmResult:
        prompts.append(prompt)
        return _llm({"verdicts": [_verdict(gid)]})(prompt)

    _run(store, spy)
    assert '"memory": "<me<id> the gripe complains about, else null>"' in prompts[0]
