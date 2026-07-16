"""Render a voice-score to an mp3 — via the precis-tts container or in-process.

The producer (``briefing_audio`` / ``precis draft audio``) builds narration
segments, then calls :func:`render_episode`, which picks a backend:

- **container** (``image`` set) — stage ``segments.json``, one-shot ``podman run
  precis-tts``, read back the produced ``out.mp3``. The worker needs **no**
  ``[tts]`` extra; this is the cluster path (docker/tts/README.md).
- **in-process** (``synth`` given) — ``synthesize_text`` → WAV → ffmpeg mp3. The
  local / manual path on a host that has the ``[tts]`` extra (spark's kokoro-venv).

**MP3, not AAC/m4a** — mp3 is the one audio format that plays *everywhere*
(incl. Apple Podcasts / Safari / iOS), so a shared enclosure or a copied file
just works. The container read-back stays tolerant of an ``out.m4a`` (an older,
un-rebuilt precis-tts image) so a code deploy that outruns the image rebuild
(``playbooks/45-tts.yml``, separate from the main redeploy) keeps producing
episodes rather than failing — it just publishes them as m4a until the image
catches up.

Both return ``{"segments": int, "duration_s": float, "audio_path": str}`` —
``audio_path`` is the *actual* file written (its extension matches whatever the
backend produced), so the caller publishes the right bytes + mime.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from precis.tts.encode import encode_mp3

log = logging.getLogger(__name__)


def render_via_container(
    segments: Sequence[Any],
    out_audio: str | Path,
    *,
    image: str,
    speed: float = 1.0,
    container_cmd: str = "podman",
    scratch_dir: str | Path | None = None,
    timeout: float | None = 600,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Render ``segments`` to ``out_audio`` via a one-shot ``podman run`` of the
    precis-tts image. Stages ``segments.json`` in a scratch ``in/`` dir, mounts it
    read-only + an ``out/`` dir, and copies the produced audio (``out.mp3``, or
    ``out.m4a`` from an older image) to ``out_audio`` — rewriting the suffix to
    match what the image actually produced, and returning that real path under
    ``audio_path``. ``run`` is injectable for tests. ``timeout`` bounds the
    container (a hung render — e.g. a stalled model/dict fetch — must not block
    the worker tick forever; on expiry ``subprocess.TimeoutExpired`` propagates
    and the caller backs the job off). Raises on a non-zero run or missing
    output."""
    payload = {
        "segments": [
            {"text": s.text, "voice": s.voice, "lang": s.lang, "kind": s.kind}
            for s in segments
        ],
        "speed": speed,
    }
    base = (
        Path(scratch_dir)
        if scratch_dir
        else Path(tempfile.mkdtemp(prefix="precis-tts-"))
    )
    indir, outdir = base / "in", base / "out"
    indir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    (indir / "segments.json").write_text(json.dumps(payload), encoding="utf-8")
    try:
        run(
            [
                container_cmd,
                "run",
                "--rm",
                "-v",
                f"{indir}:/work/in:ro",
                "-v",
                f"{outdir}:/work/out",
                image,
            ],
            check=True,
            timeout=timeout,
        )
        # Prefer the mp3 the current image writes; fall back to an m4a from an
        # older, un-rebuilt image so a code deploy never dark-holes episodes.
        produced = next(
            (
                outdir / f"out{e}"
                for e in (".mp3", ".m4a")
                if (outdir / f"out{e}").is_file()
            ),
            None,
        )
        if produced is None:
            raise RuntimeError(f"precis-tts produced no out.mp3/out.m4a in {outdir}")
        final = Path(out_audio).with_suffix(produced.suffix)
        shutil.copyfile(produced, final)
        result: dict[str, Any] = {}
        result_path = outdir / "result.json"
        if result_path.is_file():
            result = dict(json.loads(result_path.read_text(encoding="utf-8")))
        result["audio_path"] = str(final)
        return result
    finally:
        if scratch_dir is None:
            shutil.rmtree(base, ignore_errors=True)


def render_episode(
    segments: Sequence[Any],
    out_audio: str | Path,
    *,
    image: str | None = None,
    synth: Any | None = None,
    speed: float = 1.0,
    scratch_dir: str | Path | None = None,
    container_cmd: str = "podman",
    timeout: float | None = 600,
    encode: Callable[[Path, Path], None] = encode_mp3,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Render ``segments`` to ``out_audio`` (an mp3), container-first.

    ``image`` set → container path (worker, no ``[tts]`` needed). Else ``synth``
    given → in-process ``synthesize_text`` + ``encode`` (WAV→mp3). Neither → a
    ``RuntimeError`` (no backend). ``encode`` is injectable so a test can skip
    ffmpeg. The returned ``audio_path`` is the file actually written — publish
    that, not the requested path (the container path may have produced m4a)."""
    out = Path(out_audio)
    out.parent.mkdir(parents=True, exist_ok=True)
    if image:
        return render_via_container(
            segments,
            out,
            image=image,
            speed=speed,
            container_cmd=container_cmd,
            scratch_dir=scratch_dir,
            timeout=timeout,
            run=run,
        )
    if synth is None:
        raise RuntimeError(
            "no TTS backend: set PRECIS_TTS_IMAGE (container) or pass a synth"
        )
    from precis.export.audio import synthesize_text

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "narration.wav"
        res = synthesize_text(segments, wav, synth=synth)
        encode(wav, out)
    return {
        "segments": res.segments,
        "duration_s": res.duration_s,
        "audio_path": str(out),
    }


__all__ = ["render_episode", "render_via_container"]
