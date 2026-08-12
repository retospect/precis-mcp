"""Trust surfaces — LaTeX export marking (the trust-surfaces export marking, stage a). End-to-end via ``export_draft`` against real
Postgres (the ``hub`` fixture), mirroring ``tests/test_export_latex.py``'s
hub/finding coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from precis.dispatch import Hub
from precis.export import latex
from precis.handlers.draft import DraftHandler
from precis.handlers.finding import FindingHandler
from precis.handlers.todo import TodoHandler
from precis.store import BlockInsert, Store
from precis.store.types import Tag
from precis.utils import handle_registry


def _seed_paper(store: Store, slug: str, title: str = "a paper") -> None:
    store.insert_ref(kind="paper", slug=slug, title=title, provider="manual")
    paper_ref = store.get_ref(kind="paper", id=slug)
    assert paper_ref is not None
    store.insert_blocks(paper_ref.id, [BlockInsert(pos=0, text="body", slug="b0")])


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
    # So the cite resolves to ``cite_key`` (not the pub_id placeholder) —
    # mirrors the chase-snapshot pattern tests/test_finding.py uses.
    meta_patch: dict = {"primary_cite_key": cite_key}
    if chain is not None:
        meta_patch["chain"] = chain
    if dead_reason is not None:
        meta_patch["dead_reason"] = dead_reason
    if override is not None:
        meta_patch["unacquirable_override"] = override
    if meta_patch:
        hub.live_store.update_ref(ref_id, meta_patch=meta_patch)
    hub.live_store.add_tag(
        ref_id, Tag.closed("STATUS", status), set_by="chase", replace_prefix=True
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


def _export(store: Store, ref: Any, tmp_path: Path, name: str) -> str:
    out = tmp_path / name
    latex.export_draft(store, ref, target_dir=out)
    return (out / "main.tex").read_text(encoding="utf-8")


# ── AC 6 — established/clean renders identically, no event row ─────────


def test_established_clean_renders_plain_cite_no_marks(
    hub: Hub, tmp_path: Path
) -> None:
    fid = _finding(hub, cite_key="ac6clean", status="established")
    ref = _draft_citing(hub, slug="dclean", finding_ref_id=fid)

    tex = _export(hub.live_store, ref, tmp_path, "clean")

    assert "\\cite{ac6clean}" in tex
    assert "unverified" not in tex
    assert "UNSUPPORTED" not in tex
    assert "Unverified claims" not in tex
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

    tex = _export(hub.live_store, ref, tmp_path, "malformed")  # must not raise

    assert "\\cite{malformed}" in tex
    assert "unverified" not in tex
    assert "UNSUPPORTED" not in tex


# ── AC 1 — unverified mark + end-matter list (acquiring/tracing) ───────


def test_acquiring_finding_gets_unverified_mark_and_end_matter(
    hub: Hub, tmp_path: Path
) -> None:
    fid = _finding(hub, cite_key="ac1acq", status="acquiring")
    ref = _draft_citing(hub, slug="dacq", finding_ref_id=fid)

    tex = _export(hub.live_store, ref, tmp_path, "acq")

    assert "\\textsuperscript{?}" in tex
    assert "[unverified: source pending]" in tex
    assert "Unverified claims" in tex
    assert "source pending" in tex.split("Unverified claims", 1)[1]


# ── AC 4 — dead_chain(unacquirable) without override ───────────────────


def test_dead_chain_unacquirable_without_override_is_unverified_with_note(
    hub: Hub, tmp_path: Path
) -> None:
    fid = _finding(
        hub, cite_key="ac4dead", status="dead_chain", dead_reason="unacquirable"
    )
    ref = _draft_citing(hub, slug="ddead", finding_ref_id=fid)

    tex = _export(hub.live_store, ref, tmp_path, "dead")

    assert "[unverified: no OA copy obtainable; hand-download queued]" in tex
    assert "UNSUPPORTED" not in tex


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

    tex = _export(hub.live_store, ref, tmp_path, "unsup")

    assert "\\textbf{" in tex
    assert "UNSUPPORTED" in tex
    assert "cited source does not back this claim" in tex
    assert "\\textsuperscript{?}" not in tex  # distinct from the pending mark


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

    tex = _export(hub.live_store, ref, tmp_path, "over")

    assert "\\cite{ac3over}" in tex
    # Calm author-vouched mark, not clean and not the loud unverified /
    # unsupported — and never in the "Unverified claims" problem list.
    assert "author-vouched" in tex
    assert "print-only 1962 monograph" in tex
    assert "unverified" not in tex
    assert "UNSUPPORTED" not in tex
    assert "Unverified claims" not in tex

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

    tex = _export(hub.live_store, ref, tmp_path, "unsupov")

    assert "UNSUPPORTED" in tex
    # No override event — an unsupported render is never "the override
    # worked", so nothing should be recorded as overridden-clean.
    assert hub.live_store.events_for(ref.id, event="export_override") == []
