"""Glyph-damaged-source caveat (gr228652): predicate + the three read
surfaces (paper view banner, finding view, draft cite lint)."""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers._draft_lint import glyph_cite_hint
from precis.handlers.finding import FindingHandler
from precis.handlers.paper import _paper_banner
from precis.ingest.glyph_health import glyph_caveat, glyph_suspected
from precis.utils import handle_registry

_BAD = {
    "glyph_health": {
        "suspected": True,
        "modes": ["a"],
        "suspect_fonts": ["KKLGAD+AdvP7DA6"],
    }
}
_CLEAN = {"glyph_health": {"suspected": False, "modes": [], "suspect_fonts": []}}


def _paper(store, slug: str, meta: dict):
    return store.insert_ref(kind="paper", slug=slug, title=f"T {slug}", meta=meta)


def test_predicate_and_caveat() -> None:
    assert glyph_suspected(_BAD)
    assert not glyph_suspected(_CLEAN)
    assert not glyph_suspected({})
    assert not glyph_suspected(None)
    assert not glyph_suspected({"glyph_health": "junk"})
    text = glyph_caveat(_BAD)
    assert "fonts: KKLGAD+AdvP7DA6" in text
    assert "modes: a" in text
    assert "Check numbers and units against the PDF" in text
    bare = glyph_caveat({"glyph_health": {"suspected": True}})
    assert bare.startswith("Text extraction may have dropped")
    assert "fonts:" not in bare
    assert glyph_caveat(_CLEAN) == ""


def test_paper_banner_only_when_suspected(store) -> None:
    bad = _paper(store, "bad24a", _BAD)
    clean = _paper(store, "clean24a", _CLEAN)
    banner = _paper_banner(bad)
    assert banner is not None and "GLYPH-DAMAGED SOURCE" in banner
    assert _paper_banner(clean) is None


def test_paper_banner_follows_retraction_banner(store) -> None:
    ref = _paper(store, "both24a", _BAD)
    store.set_retraction_status(ref.id, status="retracted", reason="x")
    banner = _paper_banner(store.get_ref(kind="paper", id="both24a"))
    assert banner is not None
    first, second = banner.split("\n")
    assert "RETRACT" in first.upper()
    assert "GLYPH" in second


def test_finding_view_lists_suspected_source_only(store) -> None:
    bad = _paper(store, "badsrc24a", _BAD)
    clean = _paper(store, "cleansrc24a", _CLEAN)
    finding = store.insert_ref(kind="finding", slug=None, title="claim", meta={})
    for p in (bad, clean):
        store.add_link(src_ref_id=finding.id, dst_ref_id=p.id, relation="related-to")
    h = FindingHandler(hub=Hub(store=store))
    out = h._render_one(store.get_ref(kind="finding", id=finding.id), [])
    assert "source caveats:" in out
    bad_h = handle_registry.format_handle("paper", bad.id)
    clean_h = handle_registry.format_handle("paper", clean.id)
    assert f"  {bad_h} — Text extraction may have dropped" in out
    assert clean_h not in out


def test_finding_view_skips_misattributed_source(store) -> None:
    # A misattributes link disowns the paper; it is not a source to caveat.
    bad = _paper(store, "disowned24a", _BAD)
    finding = store.insert_ref(kind="finding", slug=None, title="claim", meta={})
    store.add_link(src_ref_id=finding.id, dst_ref_id=bad.id, relation="misattributes")
    h = FindingHandler(hub=Hub(store=store))
    out = h._render_one(store.get_ref(kind="finding", id=finding.id), [])
    assert "source caveats:" not in out


def test_finding_view_primary_cite_key_source(store) -> None:
    bad = _paper(store, "primbad24a", _BAD)
    finding = store.insert_ref(
        kind="finding",
        slug=None,
        title="claim",
        meta={"primary_cite_key": "primbad24a"},
    )
    h = FindingHandler(hub=Hub(store=store))
    out = h._render_one(store.get_ref(kind="finding", id=finding.id), [])
    assert handle_registry.format_handle("paper", bad.id) in out
    assert "source caveats:" in out


def test_draft_lint_warns_for_suspected_cite_only(store) -> None:
    bad = _paper(store, "lintbad24a", _BAD)
    clean = _paper(store, "lintclean24a", _CLEAN)
    bad_h = handle_registry.format_handle("paper", bad.id)
    clean_h = handle_registry.format_handle("paper", clean.id)
    out = glyph_cite_hint(store, f"See [{bad_h}] and [{clean_h}].")
    assert f"cites {bad_h}, whose extracted text may have lost" in out
    assert clean_h not in out
    assert glyph_cite_hint(store, f"Only [{clean_h}].") == ""
    assert glyph_cite_hint(store, "no cites") == ""
