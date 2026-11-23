"""The generated "AI contributions" back-matter section (gr725421).

The export adds the section every time: an author-written heading is
used as written; otherwise the section is generated from the writing
models on record — the ``chunk_events.source.writer`` stamps landed at
write time, ``touched``-linked ``agentlog`` runs, and the minters and
later content editors on cited ``TAPROOT:claim`` hubs — plus precis
(cited whole through the renderers' citation machinery) and its
background models, and it always ends in a marked line asking the
human authors to state what they checked. One line per model: a model
with several roles gets one line naming each.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from precis.export import _ai_contributions as ac
from precis.export import latex, walker
from precis.export._back_matter import ordered_blocks
from precis.store.revision_context import revision_context
from precis.store.store import Store


def _chunk(kind: str, text: str, depth: int = 0, dc: str = "dc1") -> Any:
    return SimpleNamespace(chunk_kind=kind, text=text, depth=depth, dc=dc)


def test_authored_heading_is_detected() -> None:
    assert not walker.has_ai_contributions(
        [_chunk("heading", "Methods"), _chunk("paragraph", "x")], "Draft title"
    )
    assert walker.has_ai_contributions(
        [
            _chunk("heading", "Methods"),
            _chunk("heading", " AI  Contributions ", dc="dc2"),
        ],
        "Draft title",
    )
    # A nested heading of the same name does not count as the section.
    assert not walker.has_ai_contributions(
        [
            _chunk("heading", "Appendix"),
            _chunk("heading", "AI contributions", 1, "dc2"),
        ],
        "Draft title",
    )


def test_a_draft_titled_ai_contributions_still_generates() -> None:
    """The seeded title chunk is skipped by both renderers — a draft
    *titled* "AI contributions" is not an authored section."""
    chunks = [
        _chunk("heading", "AI contributions", 0, "dc1"),
        _chunk("heading", "Methods", 1, "dc2"),
        _chunk("heading", "Results", 1, "dc3"),
    ]
    assert not walker.has_ai_contributions(chunks, "AI contributions")
    numbers = walker.number_xrefs(
        chunks, "AI contributions", equation_of=lambda _: None
    )
    assert numbers.numbered_headings == {"dc2", "dc3"}
    assert numbers.labels["dc2"] == ("Section", "1")
    assert numbers.labels["dc3"] == ("Section", "2")


def test_authored_section_nested_under_the_title_counts() -> None:
    """When the body is nested under the seeded title, both renderers
    lift depth-1 headings to top-level sections — an authored "AI
    contributions" there counts (gr725421 review)."""
    chunks = [
        _chunk("heading", "Draft title", 0, "dc1"),
        _chunk("heading", "Methods", 1, "dc2"),
        _chunk("heading", "AI contributions", 1, "dc3"),
    ]
    assert walker.has_ai_contributions(chunks, "Draft title")
    assert walker.back_matter_handles(chunks, "dc1") == {"dc3"}
    # A deeper subsection of the same name does not.
    chunks[2] = _chunk("heading", "AI contributions", 2, "dc3")
    assert not walker.has_ai_contributions(chunks, "Draft title")


def test_back_matter_order_carries_the_block() -> None:
    assert "ai_contributions" in [
        name for name, _ in ordered_blocks({"ai_contributions": "x"})
    ]
    names = [
        name
        for name, _ in ordered_blocks(
            {"references": "r", "ai_contributions": "a", "published": "p"}
        )
    ]
    assert names == ["references", "ai_contributions", "published"]


def test_compose_lines_rolls_up_models() -> None:
    lines = ac.compose_lines(
        [
            ac.ModelWrites(
                "claude-opus-5-5",
                12,
                datetime.date(2026, 11, 18),
                datetime.date(2026, 11, 18),
                frozenset({"created", "edited"}),
            )
        ],
        3,  # unrecorded_writes
        {"openai/gpt-oss-120b": 2},  # hub_minters
        {"meta-llama-4": 1},  # hub_editors
        1,  # unmodelled_hubs
        [("openai/gpt-oss-120b", 42)],  # summarisers
        "openai/gpt-oss-120b",  # also the embedder
        run_models=[
            ac.ModelWrites(
                "claude-sonnet-5",
                2,
                datetime.date(2026, 11, 19),
                datetime.date(2026, 11, 20),
                frozenset(),
            )
        ],
        unrecorded_runs=1,
    )
    texts = [e.text for e in lines]
    assert texts[0].startswith("claude-opus-5-5: drafted and edited")
    assert "(12 writes, on 2026-11-18)" in texts[0]
    assert any(
        "3 further chunk writes carry no writing-model record" in t for t in texts
    )
    # A run-only model is "used in" the runs: models_used covers work
    # anywhere in the run and touched also covers moves — "edited the
    # manuscript" would claim more than the record shows.
    assert any(
        "claude-sonnet-5: used in 2 logged agent runs that touched this draft" in t
        for t in texts
    )
    assert any("1 logged agent run" in t and "no model record" in t for t in texts)
    shared = [t for t in texts if t.startswith("openai/gpt-oss-120b:")]
    assert len(shared) == 1
    assert "minted 2 claim records this manuscript cites" in shared[0]
    assert "42 calls in the last 30 days" in shared[0]
    assert "computed the vectors behind semantic search" in shared[0]
    assert any(
        "meta-llama-4: edited 1 claim record this manuscript cites" in t for t in texts
    )
    assert any("1 cited claim record" in t and "not recorded" in t for t in texts)
    assert any("precis-mcp" in t and "10.5281/zenodo.22863181" in t for t in texts)
    # Exactly the last line is marked for the authors.
    assert lines[-1].marked and "human authors" in lines[-1].text
    assert not any(e.marked for e in lines[:-1])


def test_compose_lines_cites_the_software_record() -> None:
    """``precis_cite`` drops the prose DOI and carries the cite key for
    the renderer's own citation machinery."""
    lines = ac.compose_lines([], 0, {}, {}, 0, [], None, precis_cite="stamm26precis")
    precis = next(e for e in lines if e.text.startswith("precis-mcp"))
    assert precis.cite == "stamm26precis"
    assert "10.5281" not in precis.text
    assert "manuscript-preparation system" in precis.text


def test_compose_lines_says_so_when_nothing_is_recorded() -> None:
    lines = ac.compose_lines([], 0, {}, {}, 0, [], None)
    texts = [e.text for e in lines]
    assert any("No writing model is on record" in t for t in texts)
    assert any("No passage-summarisation model is recorded" in t for t in texts)
    assert lines[-1].marked


def _draft(store: Store, name: str) -> Any:
    proj = store.insert_ref(kind="todo", slug=None, title=f"proj {name}")
    ref, _title = store.drafts.create_draft(
        name=name, title=f"Title {name}", project_ref_id=proj.id
    )
    return ref


def _chunk_sources(store: Store, ref_id: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT e.source FROM chunk_events e "
            "JOIN chunks c ON c.chunk_id = e.chunk_id "
            "WHERE c.ref_id = %s ORDER BY e.event_id",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _revision(store: Store, ref_id: int, event: str, model: str) -> None:
    """One revision row carrying ``writer.model`` — the GUC path (the
    ``precis_revisions_writer`` trigger stamps it; a literal ``writer``
    column value is overwritten)."""
    with revision_context("test", writer={"model": model}):
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO revisions (target_kind, target_id, event, actor, "
                "reason, prev_state) VALUES ('ref', %s, %s, 'agent', 'test', "
                "'{}'::jsonb)",
                (ref_id, event),
            )


def test_chunk_writes_carry_the_writer_stamp(store: Store) -> None:
    """Every draft chunk write records the active context's writer on the
    event — an MCP-session edit leaves no agentlog run (gr725421)."""
    ref = _draft(store, "wstamp")
    with revision_context("add", writer={"model": "claude-opus-5-5"}):
        store.drafts.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text="body")
    sources = _chunk_sources(store, ref.id)
    assert sources and sources[-1]["writer"]["model"] == "claude-opus-5-5"
    assert sources[-1]["reason"] == "add"


def test_entries_rollup_reads_the_chunk_events(store: Store) -> None:
    ref = _draft(store, "wrollup")
    with revision_context("add", writer={"model": "claude-opus-5-5"}):
        store.drafts.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text="body")
    entries = ac.ai_contribution_entries(store, ref.id)
    texts = [e.text for e in entries]
    assert any(t.startswith("claude-opus-5-5: drafted") for t in texts)
    assert any("precis-mcp" in t for t in texts)
    assert entries[-1].marked


def test_fork_credits_copying_and_later_writing_separately(store: Store) -> None:
    ref = _draft(store, "forksource")
    with revision_context("add", writer={"model": "source-model"}):
        store.drafts.add_chunks(
            ref_id=ref.id, chunk_kind="paragraph", text="source body"
        )
    paragraph = next(
        c for c in store.drafts.reading_order(ref.id) if c.text == "source body"
    )
    with revision_context("edit", writer={"model": "source-editor"}):
        store.drafts.edit_text(paragraph.handle, "body copied at fork")
    project = store.insert_ref(kind="todo", slug=None, title="Target project")
    with revision_context("test fork", writer={"model": "fork-model"}):
        fork = store.drafts.fork_draft(ref.id, project.id, new_slug="forktarget")
    with revision_context("edit", writer={"model": "late-source-editor"}):
        store.drafts.edit_text(paragraph.handle, "body edited after fork")
    with revision_context("add", writer={"model": "writing-model"}):
        store.drafts.add_chunks(ref_id=fork.id, chunk_kind="paragraph", text="new body")

    texts = [e.text for e in ac.ai_contribution_entries(store, fork.id)]
    assert any(t.startswith("fork-model: copied the manuscript text") for t in texts)
    assert not any(t.startswith("fork-model: drafted") for t in texts)
    assert any(t.startswith("writing-model: drafted") for t in texts)
    assert any(t.startswith("source-model: drafted") for t in texts)
    assert any(t.startswith("source-editor: edited") for t in texts)
    assert not any(t.startswith("late-source-editor:") for t in texts)

    next_project = store.insert_ref(kind="todo", slug=None, title="Next project")
    next_fork = store.drafts.fork_draft(fork.id, next_project.id, new_slug="secondfork")
    models, _ = ac._chunk_writers(store, next_fork.id)
    source = next(m for m in models if m.model == "source-model")
    assert source.writes == 1
    assert {m.model for m in models} == {
        "source-model",
        "source-editor",
        "fork-model",
        "writing-model",
    }


def test_summariser_usage_includes_escalation_and_failed_calls(store: Store) -> None:
    ref = _draft(store, "summary_usage")
    with store.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO llm_call_log (source, tier, transport, model, tools_needed, errored, ts)
            VALUES
              ('llm_summarize', 'frontier', 'claude_agent', 'shared-model', false, false,
               now() - interval '3 days'),
              ('llm_summarize:escalate', 'frontier', 'claude_agent', 'shared-model',
               false, false, now() - interval '1 day'),
              ('llm_summarize:escalate', 'frontier', 'claude_agent', 'failed-model',
               false, true, now()),
              ('unrelated', 'frontier', 'claude_agent', 'failed-model', false, false, now())
            """
        )
    models, spans = ac._summariser_models(store)
    assert dict(models) == {"shared-model": 2, "failed-model": 1}
    first, last = spans["shared-model"]
    assert (last - first).days == 2
    texts = [entry.text for entry in ac.ai_contribution_entries(store, ref.id)]
    failed = next(t for t in texts if t.startswith("failed-model:"))
    assert "recorded in the passage-summarisation call log (1 call" in failed
    assert any("The call log records attempts" in t for t in texts)


def test_entries_credit_agentlog_runs_linked_to_the_draft(store: Store) -> None:
    """A ``claude -p`` worker's run ``touched``-links the draft — the
    roll-up credits its model even with no chunk-event stamp."""
    ref = _draft(store, "runs")
    run = store.insert_ref(
        kind="agentlog",
        slug=None,
        title="run",
        meta={"model": "opus", "model_actual": "claude-opus-5-5"},
    )
    bare = store.insert_ref(kind="agentlog", slug=None, title="run2", meta={})
    store.add_link(
        src_ref_id=run.id, dst_ref_id=ref.id, relation="touched", set_by="agent"
    )
    store.add_link(
        src_ref_id=bare.id, dst_ref_id=ref.id, relation="touched", set_by="agent"
    )
    entries = ac.ai_contribution_entries(store, ref.id)
    texts = [e.text for e in entries]
    assert any(
        t.startswith(
            "claude-opus-5-5: used in 1 logged agent run that touched this draft"
        )
        for t in texts
    )
    assert any("1 logged agent run" in t and "no model record" in t for t in texts)


def test_hub_writers_only_claim_hubs_and_minters_vs_editors(
    store: Store,
) -> None:
    """Only ``TAPROOT:claim`` findings roll up — an ordinary cited
    finding is prose evidence, not a claim hub (gr725421 review). The
    ``meta.filed_by`` model minted the hub and counts apart from a
    writer on a later content revision; a writer on a
    ``retired``/``trust-recorded`` revision reviewed the claim — it did
    not draft it."""
    from precis.taproot.canon import CanonicalClaim
    from precis.taproot.hub import mint_hub

    ref = _draft(store, "hubw")
    hub_id = mint_hub(
        store, CanonicalClaim(sentence="Pd/C couples aryl halides.", scope={})
    )
    bare_hub = mint_hub(
        store, CanonicalClaim(sentence="Cu fails the same coupling.", scope={})
    )
    plain = store.insert_ref(kind="finding", slug=None, title="ordinary finding")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = ANY(%s)",
            ('{"filed_by": {"model": "filer-model"}}', [hub_id, plain.id]),
        )
    for fid in (hub_id, plain.id, bare_hub):
        store.add_link(src_ref_id=ref.id, dst_ref_id=fid, relation="cites")
    _revision(store, hub_id, "edited", "drafter-model")
    _revision(store, hub_id, "retired", "reviewer-model")
    _revision(store, plain.id, "edited", "plain-model")
    _revision(store, bare_hub, "retired", "reviewer-model")
    minters, editors, unmodelled = ac._hub_writers(store, ref.id)
    assert minters == {"filer-model": 1}
    assert editors == {"drafter-model": 1}
    # The plain finding never enters the roll-up; the bare hub carries
    # no model in either place, so it lands in the "not recorded" count.
    assert unmodelled == 1


def test_software_record_cites_through_the_machinery(
    store: Store, tmp_path: Path
) -> None:
    """The install's own software record is cited whole through the
    bibliography — the DOI finds the record, its ``pa`` handle resolves
    the cite key, and the rendered section emits ``\\cite``."""
    ref = _draft(store, "softrec")
    rec = store.insert_ref(
        kind="paper",
        slug="stamm26precis",
        title="precis-mcp",
        meta={"record_type": "software"},
    )
    store.insert_ref_identifiers(rec.id, [("doi", ac.PRECIS_SOFTWARE_DOI, "manual")])
    assert ac.software_record_cite_key(store) == "stamm26precis"
    entries = ac.ai_contribution_entries(store, ref.id)
    precis = next(e for e in entries if e.text.startswith("precis-mcp"))
    assert precis.cite == "stamm26precis"
    assert "10.5281" not in precis.text
    out = latex.build_ai_contributions_section(store, ref.id)
    assert "\\cite{stamm26precis}" in out
    result = latex.export_draft(store, ref, target_dir=tmp_path, remarkable=True)
    main = result.main_tex.read_text(encoding="utf-8")
    assert "claim records.\\footnote{" in main
    assert "~\\cite{stamm26precis}}" in main
    assert "\x02" not in main
    assert "{stamm26precis," in result.bib.read_text(encoding="utf-8")


def test_latex_section_marks_the_author_line(store: Store) -> None:
    ref = _draft(store, "wtex")
    out = latex.build_ai_contributions_section(store, ref.id)
    assert out.startswith("\\section*{AI contributions}")
    assert "\\textcolor{red}{" in out
    assert "10.5281/zenodo.22863181" in out


def test_assemble_document_places_the_section() -> None:
    out = latex.assemble_document(
        title="T",
        author_block="\\author{a}",
        body="body",
        acronyms="",
        ai_contributions_section="\\section*{AI contributions}\nline",
    )
    assert "\\section*{AI contributions}" in out
    assert out.index("\\printglossaries") < out.index("AI contributions")


def test_docx_ctx_defaults_to_no_draft() -> None:
    from precis.export.docx import _Ctx

    ctx = _Ctx(store=None, known_handles=set())
    assert ctx.draft_ref_id == 0 and not ctx.has_ai_contributions


def test_agentlog_run_credits_every_model_used(store: Store) -> None:
    ref = _draft(store, "multirun")
    run = store.insert_ref(
        kind="agentlog",
        slug=None,
        title="run",
        meta={
            "model_actual": "claude-opus-5-5",
            "models_used": ["claude-opus-5-5", "claude-haiku-5-5"],
        },
    )
    store.add_link(
        src_ref_id=run.id, dst_ref_id=ref.id, relation="touched", set_by="agent"
    )
    models, unrecorded = ac._agentlog_runs(store, ref.id)
    assert {m.model for m in models} == {"claude-opus-5-5", "claude-haiku-5-5"}
    assert unrecorded == 0


def test_same_model_in_chunks_and_runs_prints_one_line() -> None:
    d = datetime.date(2026, 11, 18)
    lines = ac.compose_lines(
        [ac.ModelWrites("m1", 4, d, d, frozenset({"edited"}))],
        0,
        {},
        {},
        0,
        [("openai/gpt-oss-120b", 5)],
        None,
        run_models=[ac.ModelWrites("m1", 2, d, d, frozenset())],
        summariser_spans={"openai/gpt-oss-120b": (d, d)},
    )
    texts = [e.text for e in lines]
    assert sum(t.startswith("m1:") for t in texts) == 1
    assert "used in 2 logged agent runs" in texts[0]
    assert any("5 calls in the last 30 days, on 2026-11-18" in t for t in texts)


def test_one_line_per_model_across_writes_runs_and_hubs() -> None:
    """gr725421 review: a model appearing in the draft's chunk writes,
    its logged runs and the cited hubs' revisions gets ONE line whose
    clauses name each role — not a line per source."""
    d = datetime.date(2026, 11, 18)
    lines = ac.compose_lines(
        [ac.ModelWrites("m1", 4, d, d, frozenset({"edited"}))],
        0,
        {"m1": 1, "m2": 2},  # hub_minters
        {"m1": 2},  # hub_editors
        0,
        [],
        None,
        run_models=[ac.ModelWrites("m1", 3, d, d, frozenset())],
    )
    texts = [e.text for e in lines]
    m1 = [t for t in texts if t.startswith("m1:")]
    assert len(m1) == 1
    assert "edited the manuscript text (4 writes" in m1[0]
    assert "used in 3 logged agent runs" in m1[0]
    assert "minted 1 and edited 2 claim records this manuscript cites" in m1[0]
    assert any(
        t.startswith("m2: minted 2 claim records this manuscript cites") for t in texts
    )


def test_subtree_hygiene_reads_the_whole_draft(hub: Any) -> None:
    """gr725421 review: a ``view='hygiene'`` report scoped to one
    heading's subtree still checks the whole draft for an authored "AI
    contributions" section — a report on Methods must not say the
    export will generate one when the author supplied it elsewhere."""
    from precis.handlers.draft import DraftHandler

    draft = DraftHandler(hub=hub)
    proj = hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft.put(id="nt", title="T", project=proj)
    draft.put(id="nt", chunk_kind="heading", text="Methods", at={"last": True})
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    order = hub.live_store.drafts.reading_order(ref.id)
    methods_dc = next(c.dc for c in order if c.text == "Methods")
    flag = 'no "AI contributions" section'
    assert flag in draft.get(id=methods_dc, view="hygiene").body
    draft.put(id="nt", chunk_kind="heading", text="AI contributions", at={"last": True})
    assert flag not in draft.get(id=methods_dc, view="hygiene").body


def test_metadata_only_events_are_not_text_edits(store: Store) -> None:
    ref = _draft(store, "metaonly")
    with revision_context("add", writer={"model": "claude-opus-5-5"}):
        store.drafts.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text="body")
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s", (ref.id,)
        ).fetchone()
        assert row is not None
        cid = row[0]
        conn.execute(
            "INSERT INTO chunk_events (chunk_id, event_kind, source) "
            'VALUES (%s, \'edited\', \'{"reason": "set-style", '
            '"writer": {"model": "claude-haiku-5-5"}}\')',
            (cid,),
        )
    models, _ = ac._chunk_writers(store, ref.id)
    assert [m.model for m in models] == ["claude-opus-5-5"]
