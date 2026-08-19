"""The "Published claim artifacts" export appendix: a cited claim hub
whose nanopub publish row is minted (signed/anchored/published) gets one
end-matter entry — frozen AIDA sentence + trusty URI + status — in both
exporters; unminted hubs leave the export byte-identical. DB-backed via
the ``hub`` fixture; signing keys come from env (no vault, no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import docx as docx_lib

from precis.dispatch import Hub
from precis.export import docx, latex
from precis.export._nanopub_appendix import SECTION_TITLE
from precis.handlers.draft import DraftHandler
from precis.utils import handle_registry
from tests.test_export_latex_trust import _new_project
from tests.test_nanopub_gates_mint import _seed_hub, _seed_paper
from tests.test_nanopub_preflight import _anchor, _signed_hub


def _draft_citing(hub: Hub, *, slug: str, text: str) -> Any:
    draft = DraftHandler(hub=hub)
    pid = _new_project(hub)
    draft.put(id=slug, title="T", project=pid)
    draft.put(id=slug, chunk_kind="paragraph", text=text, at={"last": True})
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return ref


def _export_tex(hub: Hub, ref: Any, tmp_path: Path, name: str) -> str:
    out = tmp_path / name
    latex.export_draft(hub.live_store, ref, target_dir=out)
    return (out / "main.tex").read_text(encoding="utf-8")


def _export_docx_text(hub: Hub, ref: Any, tmp_path: Path, name: str) -> str:
    out = tmp_path / f"{name}.docx"
    docx.export_docx(hub.live_store, ref, target_path=out)
    return "\n".join(p.text for p in docx_lib.Document(str(out)).paragraphs)


def test_signed_hub_cite_gets_appendix_entry_latex(
    hub: Hub, tmp_path: Path, monkeypatch: Any
) -> None:
    sentence = "DFT shows a minted nanobud claim holds."
    claim_hub, row = _signed_hub(hub.live_store, monkeypatch, sentence)
    handle = handle_registry.format_handle("finding", claim_hub)
    ref = _draft_citing(hub, slug="dminted", text=f"Claim [{handle}] holds.")

    tex = _export_tex(hub, ref, tmp_path, "minted")

    assert SECTION_TITLE in tex
    assert sentence in tex
    assert str(row.trusty_uri) in tex
    # Signed-but-unpublished renders distinguishably from published.
    assert "under embargo" in tex
    assert "published " not in tex.split(SECTION_TITLE, 1)[1]


def test_unminted_hub_produces_no_appendix_section(hub: Hub, tmp_path: Path) -> None:
    paper, chunk, _sha = _seed_paper(hub.live_store)
    claim_hub = _seed_hub(hub.live_store, "Never minted.", paper, chunk)
    handle = handle_registry.format_handle("finding", claim_hub)
    ref = _draft_citing(hub, slug="dunminted", text=f"Claim [{handle}] holds.")

    tex = _export_tex(hub, ref, tmp_path, "unminted")

    assert SECTION_TITLE not in tex


def test_hub_cited_twice_yields_one_entry(
    hub: Hub, tmp_path: Path, monkeypatch: Any
) -> None:
    claim_hub, row = _signed_hub(
        hub.live_store, monkeypatch, "DFT shows the twice-cited claim holds."
    )
    handle = handle_registry.format_handle("finding", claim_hub)
    ref = _draft_citing(
        hub, slug="dtwice", text=f"First [{handle}] and again [{handle}]."
    )

    tex = _export_tex(hub, ref, tmp_path, "twice")

    assert tex.count(str(row.trusty_uri)) == 1


def test_published_entry_shows_published_date(
    hub: Hub, tmp_path: Path, monkeypatch: Any
) -> None:
    claim_hub, row = _signed_hub(
        hub.live_store, monkeypatch, "DFT shows a public claim holds."
    )
    _anchor(hub.live_store, row)
    assert hub.live_store.nanopub_record_published(
        row.id, registry_url="test://registry"
    )
    handle = handle_registry.format_handle("finding", claim_hub)
    ref = _draft_citing(hub, slug="dpub", text=f"Claim [{handle}] holds.")

    tex = _export_tex(hub, ref, tmp_path, "pub")

    appendix = tex.split(SECTION_TITLE, 1)[1]
    assert "published " in appendix
    assert "under embargo" not in appendix


def test_docx_appendix_mirrors_latex(
    hub: Hub, tmp_path: Path, monkeypatch: Any
) -> None:
    sentence = "DFT shows a minted docx claim holds."
    claim_hub, row = _signed_hub(hub.live_store, monkeypatch, sentence)
    handle = handle_registry.format_handle("finding", claim_hub)
    ref = _draft_citing(hub, slug="ddocx", text=f"Claim [{handle}] holds.")

    text = _export_docx_text(hub, ref, tmp_path, "minted")

    assert SECTION_TITLE in text
    assert sentence in text
    assert str(row.trusty_uri) in text
    assert "under embargo" in text


def test_docx_unminted_has_no_section(hub: Hub, tmp_path: Path) -> None:
    paper, chunk, _sha = _seed_paper(hub.live_store)
    claim_hub = _seed_hub(hub.live_store, "Never minted docx.", paper, chunk)
    handle = handle_registry.format_handle("finding", claim_hub)
    ref = _draft_citing(hub, slug="ddocxun", text=f"Claim [{handle}] holds.")

    text = _export_docx_text(hub, ref, tmp_path, "unminted")

    assert SECTION_TITLE not in text
