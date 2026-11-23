"""Verbatim claim-reuse advisory (gr693801) — a claim hub's canonical
sentence carried verbatim by this draft AND another live draft is a
self-plagiarism flag, surfaced as an export-time ``job_event`` advisory.
Detection is text-level: a paraphrasing cite never trips it, an uncited
paste does."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.draft import lint as _draft_lint
from precis.handlers.draft import DraftHandler
from precis.store import Tag
from precis.store.revision_context import revision_context
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.utils import handle_registry
from precis.workers.job_types import get_job_type

SENTENCE = "Graphene has a tensile strength of 130 GPa."
OTHER_SENTENCE = "Nickel foam electrodes reduce overpotential in alkaline OER."


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _mk_draft(hub: Hub, draft: DraftHandler, slug: str) -> int:
    # One draft per project — ``create_draft`` refuses a second.
    draft.put(id=slug, title="T", project=_proj(hub))
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return ref.id


def _add_paragraph(draft: DraftHandler, slug: str, text: str) -> None:
    draft.put(id=slug, chunk_kind="paragraph", text=text, at={"last": True})


def _mint_hub(hub: Hub, sentence: str = SENTENCE) -> int:
    return mint_hub(hub.live_store, CanonicalClaim(sentence=sentence, scope={}))


def test_reuse_flagged_naming_the_other_draft(hub: Hub, draft: DraftHandler) -> None:
    hub_id = _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    _add_paragraph(draft, "older", f"We showed {SENTENCE}")

    rows = _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt")
    assert len(rows) == 1
    row = rows[0]
    assert row.hub_ref_id == hub_id
    assert row.title == SENTENCE
    assert row.other_draft == "older"
    assert row.dc.startswith("dc") and row.other_dc.startswith("dc")


def test_first_manuscript_alone_does_not_flag(hub: Hub, draft: DraftHandler) -> None:
    """Not a first-paper problem: one draft carrying the canonical sentence
    verbatim is the claim's home, not reuse."""
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    assert _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt") == []


def test_citing_without_verbatim_text_does_not_flag(
    hub: Hub, draft: DraftHandler
) -> None:
    """The risk is the same PRINTED wording in two papers: this draft
    paraphrases (cites the hub, never quotes its sentence), so the other
    draft's verbatim carry is not two-manuscript reuse."""
    hub_id = _mint_hub(hub)
    fi = handle_registry.format_handle("finding", hub_id)
    _mk_draft(hub, draft, "nt")
    _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", f"as shown in [{fi}], the effect holds")
    _add_paragraph(draft, "older", f"We showed {SENTENCE}")
    assert _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt") == []


def test_verbatim_here_but_only_cited_elsewhere_does_not_flag(
    hub: Hub, draft: DraftHandler
) -> None:
    """Mirror image: this draft carries the sentence verbatim, the other
    draft only cites the hub — one verbatim manuscript, no reuse."""
    hub_id = _mint_hub(hub)
    fi = handle_registry.format_handle("finding", hub_id)
    _mk_draft(hub, draft, "nt")
    _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    _add_paragraph(draft, "older", f"as shown in [{fi}], the effect holds")
    assert _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt") == []


def test_uncited_verbatim_paste_still_flags(hub: Hub, draft: DraftHandler) -> None:
    """Detection is by the sentence text, not the cites edge: a draft that
    pastes the canonical wording WITHOUT a [fi<id>] mark is still verbatim
    reuse — unattributed, if anything worse."""
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    _add_paragraph(draft, "older", f"Unmarked: {SENTENCE}")
    rows = _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt")
    assert len(rows) == 1 and rows[0].other_draft == "older"


def test_whitespace_folded_verbatim_still_flags(hub: Hub, draft: DraftHandler) -> None:
    """A line wrap inside the other draft's sentence is still the same
    wording verbatim."""
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    wrapped = SENTENCE.replace("tensile strength", "tensile\nstrength")
    _add_paragraph(draft, "older", f"We showed {wrapped}")
    rows = _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt")
    assert len(rows) == 1


def test_reworded_sentence_does_not_flag(hub: Hub, draft: DraftHandler) -> None:
    """Verbatim is verbatim: a near-miss paraphrase in the other draft is
    the DESIRED outcome (vary phrasing), not a flag."""
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    _add_paragraph(
        draft, "older", "We measured graphene's tensile strength at 130 GPa."
    )
    assert _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt") == []


def test_code_chunk_carry_does_not_flag(hub: Hub, draft: DraftHandler) -> None:
    """A ``code`` chunk's payload is literal source — gr592769's rule that
    no prose check reads inside it, on either side of the match."""
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    draft.put(id="older", chunk_kind="code", text=SENTENCE, at={"last": True})
    assert _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt") == []


def test_retired_other_draft_does_not_flag(hub: Hub, draft: DraftHandler) -> None:
    """A retired draft is not a manuscript in play."""
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    other_id = _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    _add_paragraph(draft, "older", f"We showed {SENTENCE}")
    hub.live_store.drafts.retire_draft(other_id)
    assert _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt") == []


def test_two_other_drafts_list_both(hub: Hub, draft: DraftHandler) -> None:
    """One row per (hub, other draft) — a hub carried verbatim by TWO
    other manuscripts warns about each."""
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    for slug in ("older-a", "older-b"):
        _mk_draft(hub, draft, slug)
        _add_paragraph(draft, slug, f"We showed {SENTENCE}")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    rows = _draft_lint.find_verbatim_claim_reuse(hub.live_store, "nt")
    assert sorted(r.other_draft for r in rows) == ["older-a", "older-b"]


@pytest.mark.parametrize("private_kind", ["draft", "finding"])
def test_reuse_respects_secret_read_fence(
    hub: Hub, draft: DraftHandler, private_kind: str
) -> None:
    hub_id = _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    other_id = _mk_draft(hub, draft, "older")
    for slug in ("nt", "older"):
        _add_paragraph(draft, slug, SENTENCE)
    store = hub.live_store
    private_id = other_id if private_kind == "draft" else hub_id
    store.add_tag(private_id, Tag.open("user_secret:alice"))

    with revision_context(actor="user:bob"):
        assert _draft_lint.find_verbatim_claim_reuse(store, "nt") == []
    with revision_context(actor="user:alice"):
        (reuse,) = _draft_lint.find_verbatim_claim_reuse(store, "nt")
        assert reuse.hub_ref_id == hub_id and reuse.other_draft == "older"


def test_chunks_outside_reading_tree_do_not_flag(hub: Hub, draft: DraftHandler) -> None:
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    other_id = _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", SENTENCE)
    store = hub.live_store
    with store.drafts.tx() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, handle) "
            "VALUES (%s, 10, 'paragraph', %s, 'unpositioned')",
            (other_id, SENTENCE),
        )
    assert _draft_lint.find_verbatim_claim_reuse(store, "nt") == []


def test_cli_pdf_export_reports_advisory_on_stderr(
    hub: Hub,
    draft: DraftHandler,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from precis.cli import _build_parser
    from precis.cli import draft as cli_draft
    from precis.export import compile as pdf_compile

    _mint_hub(hub)
    for slug in ("nt", "older"):
        _mk_draft(hub, draft, slug)
        _add_paragraph(draft, slug, SENTENCE)

    store = hub.live_store
    monkeypatch.setattr(cli_draft, "resolve_dsn", lambda database_url: "unused")
    monkeypatch.setattr(cli_draft.Store, "connect", lambda dsn: store)
    monkeypatch.setattr(store, "close", lambda: None)
    monkeypatch.setattr(
        pdf_compile,
        "compile_pdf",
        lambda path: SimpleNamespace(skipped=False, ok=True, pdf=path / "main.pdf"),
    )
    args = _build_parser().parse_args(
        ["draft", "export", "nt", "--pdf", "--out", str(tmp_path)]
    )
    cli_draft._run_export(args)
    stderr = capsys.readouterr().err
    assert "possible self-plagiarism" in stderr
    assert "draft 'older'" in stderr
    assert "compiled" in stderr


def test_export_dispatch_reports_advisory_without_failing(
    hub: Hub, draft: DraftHandler, monkeypatch: Any
) -> None:
    """End to end: the draft_export job emits the advisory as a job_event
    and the export itself succeeds — detection plus guidance, never a
    block."""
    import precis.export.compile as _compile

    monkeypatch.setattr(_compile, "have_latexmk", lambda: False)
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    _mk_draft(hub, draft, "older")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")
    _add_paragraph(draft, "older", f"We showed {SENTENCE}")

    spec = get_job_type("draft_export")
    assert spec is not None and spec.dispatch is not None

    from tests.test_draft_export_job import _FakeCtx

    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": "nt"}})
    spec.dispatch(ctx, spec)
    assert not ctx.failures, ctx.failures
    events = [t for k, t in ctx.events if k == "job_event"]
    assert any(
        "verbatim" in t and "self-plagiarism" in t and "'older'" in t for t in events
    ), events


def test_export_dispatch_clean_when_no_reuse(
    hub: Hub, draft: DraftHandler, monkeypatch: Any
) -> None:
    import precis.export.compile as _compile

    monkeypatch.setattr(_compile, "have_latexmk", lambda: False)
    _mint_hub(hub)
    _mk_draft(hub, draft, "nt")
    _add_paragraph(draft, "nt", f"As established, {SENTENCE}")

    spec = get_job_type("draft_export")
    assert spec is not None and spec.dispatch is not None

    from tests.test_draft_export_job import _FakeCtx

    ctx = _FakeCtx(store=hub.store, meta={"params": {"draft": "nt"}})
    spec.dispatch(ctx, spec)
    assert not ctx.failures, ctx.failures
    events = [t for k, t in ctx.events if k == "job_event"]
    assert not any("self-plagiarism" in t for t in events), events
