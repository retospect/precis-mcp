"""The audio-export renderer drives a TTS synth seam per segment.

Uses a fake synth (no model), so it exercises the stitching + per-segment
voice routing without a TTS toolchain. numpy + soundfile come with the [tts]
extra, so skip cleanly where absent (host / pre-[tts] gate image)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("numpy")
pytest.importorskip("soundfile")

import numpy as np

from precis.export.audio import export_audio


@dataclass
class _Chunk:
    chunk_kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


class _Store:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def reading_order(self, _ref_id: int) -> list[_Chunk]:
        return self._chunks


class _Ref:
    id = 7
    kind = "draft"
    meta: dict[str, Any] = {}


class _FakeSynth:
    """Records the (text, voice, lang) it's asked for; returns silence."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def synthesize(self, text: str, *, voice: str, lang: str) -> tuple[Any, int]:
        self.calls.append((text, voice, lang))
        return np.zeros(2400, dtype=np.float32), 24000  # 0.1s


def _patch_guard(monkeypatch):
    # guard_exportable rejects non-exportable kinds; drafts pass, but our _Ref
    # is a stub — neutralise the guard for the unit test.
    import precis.export as ex

    monkeypatch.setattr(ex, "guard_exportable", lambda _ref: None)


def test_export_writes_wav_and_routes_voice_per_segment(tmp_path, monkeypatch):
    _patch_guard(monkeypatch)
    store = _Store(
        [
            _Chunk("heading", "Intro"),
            _Chunk("paragraph", "Plain part."),
            _Chunk("paragraph", "UK part.", {"voice": "bf_emma", "lang": "en-gb"}),
        ]
    )
    synth = _FakeSynth()
    out = tmp_path / "draft.wav"
    res = export_audio(
        store, _Ref(), target_path=out, synth=synth, default_voice="af_heart"
    )
    assert out.is_file() and res.segments == 3
    # Each segment routed to its voice — the UK one overrides the default.
    voices = [v for (_t, v, _l) in synth.calls]
    assert voices == ["af_heart", "af_heart", "bf_emma"]
    assert synth.calls[2][2] == "en-gb"  # lang routed too


def test_empty_draft_raises(tmp_path, monkeypatch):
    _patch_guard(monkeypatch)
    store = _Store([_Chunk("figure", "just a figure")])
    with pytest.raises(ValueError):
        export_audio(store, _Ref(), target_path=tmp_path / "x.wav", synth=_FakeSynth())
