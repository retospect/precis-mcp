"""gr228594 — the glyph_health corpus backfill walks held PDFs, flags only
``suspected`` records, never writes on dry-run and never touches chunks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from precis.ingest.glyph_backfill import backfill_glyph_health
from precis.ingest.glyph_health import glyph_suspected, summarize
from precis.store._pdf_ops import GlyphBackfillRef


class _FakeStore:
    def __init__(self, refs: list[GlyphBackfillRef]) -> None:
        self._refs = refs
        self.stamped: dict[int, dict[str, Any]] = {}

    def refs_for_glyph_backfill(
        self, *, after_ref_id: int = 0, limit: int
    ) -> list[GlyphBackfillRef]:
        return [r for r in self._refs if r.ref_id > after_ref_id][:limit]

    def stamp_ref_meta(self, ref_id: int, updates: dict[str, Any]) -> None:
        self.stamped[ref_id] = updates


def _ref(ref_id: int, storage_path: str = "") -> GlyphBackfillRef:
    return GlyphBackfillRef(ref_id, "0" * 64, storage_path, ())


# Fonts shaped like the two documented scars and a clean text font.
_MODE_A = {  # pa494: Adv* font whose /ToUnicode echoes its own glyph name
    "basefont": "KKLGAD+AdvP7DA6",
    "tounicode": {0x6D: "m"},
    "differences": {0x6D: "m"},
    "has_tounicode": True,
    "has_encoding": True,
}
_CLEAN = {
    "basefont": "ABCDEF+Times-Roman",
    "tounicode": {0x6D: "m"},
    "differences": {},
    "has_tounicode": True,
    "has_encoding": True,
}


def _record(*, fonts: list[dict[str, Any]], c0: int = 0) -> dict[str, Any]:
    return summarize(
        fonts=fonts,
        c0_controls=c0,
        greek_chars=0,
        micron_without_sign=0,
        orphan_spans=0,
    )


def _pdf(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.4")
    return str(p)


def test_flags_scars_only_and_writes_on_apply(tmp_path: Path) -> None:
    scar_c0 = _pdf(tmp_path, "pa47024.pdf")  # mode (b) residue: C0 survived
    clean = _pdf(tmp_path, "clean.pdf")
    store = _FakeStore([_ref(1, scar_c0), _ref(2, clean), _ref(3, "/nowhere.pdf")])
    records = {
        scar_c0: _record(fonts=[], c0=6),
        clean: _record(fonts=[_CLEAN]),
    }

    def analyze(path: Path, _text: str) -> dict[str, Any]:
        return records[str(path)]

    dry = backfill_glyph_health(
        store, (tmp_path,), analyze=analyze, body_text=lambda *_: ""
    )
    assert (dry.scanned, dry.flagged, dry.clean, dry.no_pdf) == (3, [1], 1, 1)
    assert dry.written == 0 and store.stamped == {}

    done = backfill_glyph_health(
        store, (tmp_path,), apply=True, analyze=analyze, body_text=lambda *_: ""
    )
    assert done.written == 1 and set(store.stamped) == {1}
    assert glyph_suspected({"glyph_health": store.stamped[1]["glyph_health"]})
    assert done.last_ref_id == 3


def test_mode_a_without_residue_is_flagged(tmp_path: Path) -> None:
    """pa494 shape: lying ToUnicode, zero C0 residue — still flagged."""
    pdf = _pdf(tmp_path, "pa494.pdf")
    store = _FakeStore([_ref(7, pdf)])
    res = backfill_glyph_health(
        store,
        (tmp_path,),
        analyze=lambda *_: _record(fonts=[_MODE_A], c0=0),
        body_text=lambda *_: "",
    )
    assert res.flagged == [7]


def test_resume_cursor_limit_and_errors(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path, "x.pdf")
    store = _FakeStore([_ref(i, pdf) for i in (1, 2, 3)])

    def analyze(_p: Path, _t: str) -> dict[str, Any]:
        raise RuntimeError("boom")

    res = backfill_glyph_health(
        store, (tmp_path,), after_ref_id=1, limit=1, analyze=analyze
    )
    assert res.scanned == 1 and res.errored == 1 and res.last_ref_id == 2

    # An unparseable PDF yields only weak text signals: not a verdict.
    res = backfill_glyph_health(
        store,
        (tmp_path,),
        analyze=lambda *_: {"suspected": True, "font_analysis_error": "x"},
        body_text=lambda *_: "",
    )
    assert res.flagged == [] and res.errored == 3
