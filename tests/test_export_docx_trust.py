"""Trust surfaces — docx export marking (the trust-surfaces export marking, stage a). End-to-end via ``export_docx`` against real
Postgres (the ``hub`` fixture), mirroring ``tests/test_export_docx.py``'s
hub/finding coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler
from precis.handlers.finding import FindingHandler
from precis.handlers.todo import TodoHandler
from precis.store import BlockInsert, Store
from precis.store.types import Tag
from precis.utils import handle_registry

docx = pytest.importorskip("docx")  # python-docx (the `docx` extra)

from precis.export.docx import export_docx


def _seed_paper(store: Store, slug: str, title: str = "a paper") -> None:
    store.insert_ref(kind="paper", slug=slug, title=title, provider="manual")
    paper_ref = store.get_ref(kind="paper", id=slug)
    assert paper_ref is not None
    store.blocks.insert_blocks(
        paper_ref.id, [BlockInsert(pos=0, text="body", slug="b0")]
    )


def _new_project(hub: Hub) -> int:
    return int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )


def _finding(
    hub: Hub,
    *,
    cite_key: str,
    status: str,
    chain: list | None = None,
    dead_reason: str | None = None,
    override: dict | None = None,
) -> int:
    """A plain (non-hub) finding, promoted to ``status`` with the given
    chain/dead_reason/override meta."""
    _seed_paper(hub.live_store, cite_key)
    resp = FindingHandler(hub=hub).put(
        title="Pd/C catalyzes Suzuki coupling at RT",
        body="claim body",
        scope={},
        cited_in=cite_key,
    )
    ref_id = int(resp.body.split("id=")[1].split()[0].rstrip(",.()"))
    # So the cite resolves to ``cite_key`` (not the pub_id placeholder).
    meta_patch: dict = {"primary_cite_key": cite_key}
    if chain is not None:
        meta_patch["chain"] = chain
    if dead_reason is not None:
        meta_patch["dead_reason"] = dead_reason
    if override is not None:
        meta_patch["unacquirable_override"] = override
    hub.live_store.update_ref(ref_id, meta_patch=meta_patch)
    hub.live_store.add_tag(
        ref_id,
        Tag.closed("STATUS", status),
        set_by="chase",
        replace_prefix=True,
    )
    return ref_id


def _draft_citing(hub: Hub, *, slug: str, finding_ref_id: int) -> Any:
    draft = DraftHandler(hub=hub)
    pid = _new_project(hub)
    draft.put(id=slug, title="T", project=pid)
    handle = handle_registry.format_handle("finding", finding_ref_id)
    draft.put(
        id=slug,
        chunk_kind="paragraph",
        text=f"Claim [{handle}] holds.",
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return ref


def _export_text(store: Store, ref: Any, tmp_path: Path, name: str) -> str:
    out = tmp_path / f"{name}.docx"
    export_docx(store, ref, target_path=out)
    return "\n".join(p.text for p in docx.Document(str(out)).paragraphs)


# ── AC 6 — established/clean renders identically, no event row ─────────


def test_established_clean_renders_plain_cite_no_marks(
    hub: Hub, tmp_path: Path
) -> None:
    fid = _finding(hub, cite_key="ac6clean", status="established")
    ref = _draft_citing(hub, slug="dclean", finding_ref_id=fid)

    text = _export_text(hub.live_store, ref, tmp_path, "clean")

    assert "[1]" in text
    assert "unverified" not in text
    assert "UNSUPPORTED" not in text
    assert "Unverified claims" not in text
    assert hub.live_store.events_for(ref.id, event="export_override") == []


# ── regression — a malformed verification blob never aborts the export
# ("export always works", decided) ──────────────────────────────────────


def test_non_dict_verification_export_succeeds_unmarked(
    hub: Hub, tmp_path: Path
) -> None:
    fid = _finding(
        hub,
        cite_key="malformed",
        status="established",
        chain=[{"ref_id": 1, "ord": 0, "verification": "not-a-dict"}],
    )
    ref = _draft_citing(hub, slug="dmalformed", finding_ref_id=fid)

    text = _export_text(hub.live_store, ref, tmp_path, "malformed")  # must not raise

    assert "[1]" in text
    assert "unverified" not in text
    assert "UNSUPPORTED" not in text


# ── AC 1 — unverified mark + end-matter list (acquiring/tracing) ───────


def test_acquiring_finding_gets_unverified_mark_and_end_matter(
    hub: Hub, tmp_path: Path
) -> None:
    fid = _finding(hub, cite_key="ac1acq", status="acquiring")
    ref = _draft_citing(hub, slug="dacq", finding_ref_id=fid)

    text = _export_text(hub.live_store, ref, tmp_path, "acq")

    assert "[unverified: source pending]" in text
    assert "Unverified claims" in text
    assert "source pending" in text.split("Unverified claims", 1)[1]


# ── AC 4 — dead_chain(unacquirable) without override ───────────────────


def test_dead_chain_unacquirable_without_override_is_unverified_with_note(
    hub: Hub, tmp_path: Path
) -> None:
    fid = _finding(
        hub, cite_key="ac4dead", status="dead_chain", dead_reason="unacquirable"
    )
    ref = _draft_citing(hub, slug="ddead", finding_ref_id=fid)

    text = _export_text(hub.live_store, ref, tmp_path, "dead")

    assert "[unverified: no OA copy obtainable; hand-download queued]" in text
    assert "UNSUPPORTED" not in text


# ── AC 2 — unsupported renders louder, distinct from pending ───────────


def test_unsupported_renders_loud_distinct_mark(hub: Hub, tmp_path: Path) -> None:
    fid = _finding(
        hub,
        cite_key="ac2unsup",
        status="established",
        chain=[
            {
                "ref_id": 1,
                "ord": 0,
                "verification": {
                    "supports": "no",
                    "support_reason": "reports the opposite trend",
                },
            }
        ],
    )
    ref = _draft_citing(hub, slug="dunsup", finding_ref_id=fid)

    text = _export_text(hub.live_store, ref, tmp_path, "unsup")

    assert "UNSUPPORTED" in text
    assert "cited source does not back this claim" in text
    assert "source pending" not in text  # distinct from the pending mark


# ── AC 3 — override converts unverified→clean + ref_events; unsupported
# is never suppressed by an override ───────────────────────────────────


def test_override_renders_vouched_mark_and_records_ref_event(
    hub: Hub, tmp_path: Path
) -> None:
    fid = _finding(
        hub,
        cite_key="ac3over",
        status="dead_chain",
        dead_reason="unacquirable",
        override={
            "by": "agent",
            "at": "2026-08-04T00:00:00+00:00",
            "note": "print-only 1962 monograph",
        },
    )
    ref = _draft_citing(hub, slug="dover", finding_ref_id=fid)

    text = _export_text(hub.live_store, ref, tmp_path, "over")

    assert "[1]" in text
    # A legacy override folds to the CALM author-vouched mark, not clean and
    # not the loud unverified/unsupported — and never lands in the
    # "Unverified claims" problem list.
    assert "author-vouched" in text
    assert "print-only 1962 monograph" in text
    assert "unverified" not in text
    assert "UNSUPPORTED" not in text
    assert "Unverified claims" not in text

    events = hub.live_store.events_for(ref.id, event="export_override")
    assert len(events) == 1
    overridden = events[0].payload["overridden"]
    assert len(overridden) == 1
    assert overridden[0]["finding_ref_id"] == fid
    assert overridden[0]["note"] == "print-only 1962 monograph"
    assert overridden[0]["by"] == "agent"


def test_override_does_not_suppress_unsupported_mark(hub: Hub, tmp_path: Path) -> None:
    fid = _finding(
        hub,
        cite_key="ac3unsup",
        status="established",
        chain=[{"ref_id": 1, "ord": 0, "verification": {"supports": "no"}}],
        override={
            "by": "agent",
            "at": "2026-08-04T00:00:00+00:00",
            "note": "print-only 1962 monograph",
        },
    )
    ref = _draft_citing(hub, slug="dunsupov", finding_ref_id=fid)

    text = _export_text(hub.live_store, ref, tmp_path, "unsupov")

    assert "UNSUPPORTED" in text
    assert hub.live_store.events_for(ref.id, event="export_override") == []
