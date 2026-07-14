"""Audio export — narrate a draft to a WAV via a pluggable TTS synth.

A renderer peer to ``export/docx.py`` / ``export/latex.py``: it drives the
narration *layer* (:mod:`precis.draft.narrate`) and a :class:`Synthesizer`
seam, so this module stays TTS-agnostic (testable with a fake synth) while the
real engine (Kokoro, :mod:`precis.tts.kokoro`) is injected at call time on a
host that has it. Per-segment ``voice``/``lang`` come from chunk meta, so a
mixed-voice / multilingual draft renders as one stitched track.

WAV out (numpy + soundfile); the m4a/mp3 for the podcast feed is a cheap
ffmpeg post-step the caller does — kept out of here so the export has no
ffmpeg dependency and the unit test needs no audio toolchain.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from precis.draft.narrate import NarrationSegment, render_narration


@runtime_checkable
class Synthesizer(Protocol):
    """TTS seam: text + voice + lang → (float32 samples, sample_rate)."""

    def synthesize(self, text: str, *, voice: str, lang: str) -> tuple[Any, int]: ...


@dataclass(frozen=True, slots=True)
class AudioResult:
    path: Path
    segments: int
    duration_s: float


def _stitch(
    segments: Sequence[NarrationSegment],
    synth: Synthesizer,
    *,
    pause_s: float,
    heading_pause_s: float,
) -> tuple[Any, int]:
    """Synthesize each segment and concatenate into one track with silence gaps
    (a longer breath after a heading). Returns ``(float32 samples, sample_rate)``.
    Shared by :func:`export_audio` (drafts) and :func:`synthesize_text` (prose)."""
    import numpy as np

    sr: int | None = None
    parts: list[Any] = []
    for seg in segments:
        samples, seg_sr = synth.synthesize(seg.text, voice=seg.voice, lang=seg.lang)
        sr = sr or seg_sr
        parts.append(np.asarray(samples, dtype=np.float32))
        gap = heading_pause_s if seg.kind == "heading" else pause_s
        parts.append(np.zeros(int((sr or seg_sr) * gap), dtype=np.float32))
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return audio, (sr or 1)


def synthesize_text(
    segments: Sequence[NarrationSegment],
    target_path: str | Path,
    *,
    synth: Synthesizer,
    pause_s: float = 0.45,
    heading_pause_s: float = 0.9,
) -> AudioResult:
    """Stitch pre-built narration ``segments`` into a WAV at ``target_path``.

    The non-draft producer entry (a draft goes through :func:`export_audio`,
    which builds its segments from chunk meta; a prose producer like the news
    briefing builds them via :func:`precis.draft.narrate.markdown_segments` and
    calls this). Both share :func:`_stitch`. Raises ``ValueError`` on empty
    input."""
    import soundfile as sf

    if not segments:
        raise ValueError("no narration segments to synthesize")
    audio, sr = _stitch(
        segments, synth, pause_s=pause_s, heading_pause_s=heading_pause_s
    )
    target = Path(target_path)
    sf.write(str(target), audio, sr)
    return AudioResult(path=target, segments=len(segments), duration_s=len(audio) / sr)


def export_audio(
    store: Any,
    ref: Any,
    *,
    target_path: str | Path,
    synth: Synthesizer,
    default_voice: str = "af_heart",
    default_lang: str = "en-us",
    lexicon: dict[str, str] | None = None,
    pause_s: float = 0.45,
    heading_pause_s: float = 0.9,
    max_segments: int | None = None,
) -> AudioResult:
    """Render ``ref`` (a draft) to a WAV at ``target_path``. Raises
    ``ValueError`` if the draft has nothing speakable. ``max_segments`` caps
    the narration (a cheap preview of a long draft)."""
    import soundfile as sf

    from precis.export import guard_exportable

    guard_exportable(ref)
    segments = render_narration(
        store,
        ref,
        default_voice=default_voice,
        default_lang=default_lang,
        lexicon=lexicon,
    )
    if max_segments is not None:
        segments = segments[:max_segments]
    if not segments:
        raise ValueError(f"draft {getattr(ref, 'id', '?')} has nothing to narrate")

    audio, sr = _stitch(
        segments, synth, pause_s=pause_s, heading_pause_s=heading_pause_s
    )
    target = Path(target_path)
    sf.write(str(target), audio, sr)
    return AudioResult(
        path=target, segments=len(segments), duration_s=len(audio) / (sr or 1)
    )
