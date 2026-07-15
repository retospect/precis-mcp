"""Render a voice-score to an m4a — via the precis-tts container or in-process.

The producer (``briefing_audio`` / ``precis draft audio``) builds narration
segments, then calls :func:`render_episode`, which picks a backend:

- **container** (``image`` set) — stage ``segments.json``, one-shot ``podman run
  precis-tts``, read back ``out.m4a``. The worker needs **no** ``[tts]`` extra;
  this is the cluster path (docker/tts/README.md).
- **in-process** (``synth`` given) — ``synthesize_text`` → WAV → ffmpeg m4a. The
  local / manual path on a host that has the ``[tts]`` extra (spark's kokoro-venv).

Both return ``{"segments": int, "duration_s": float}``.
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

log = logging.getLogger(__name__)


def _ffmpeg_m4a(wav: Path, out: Path) -> None:
    """Transcode a WAV to AAC ``.m4a`` (the podcast enclosure format)."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(wav),
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(out),
        ],
        check=True,
    )


def render_via_container(
    segments: Sequence[Any],
    out_m4a: str | Path,
    *,
    image: str,
    speed: float = 1.0,
    container_cmd: str = "podman",
    scratch_dir: str | Path | None = None,
    timeout: float | None = 600,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Render ``segments`` to ``out_m4a`` via a one-shot ``podman run`` of the
    precis-tts image. Stages ``segments.json`` in a scratch ``in/`` dir, mounts it
    read-only + an ``out/`` dir, and copies the produced ``out.m4a`` to
    ``out_m4a``. ``run`` is injectable for tests. ``timeout`` bounds the container
    (a hung render — e.g. a stalled model/dict fetch — must not block the worker
    tick forever; on expiry ``subprocess.TimeoutExpired`` propagates and the
    caller backs the job off). Raises on a non-zero run or a missing output."""
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
        produced = outdir / "out.m4a"
        if not produced.is_file():
            raise RuntimeError(f"precis-tts produced no out.m4a in {outdir}")
        shutil.copyfile(produced, out_m4a)
        result_path = outdir / "result.json"
        if result_path.is_file():
            return dict(json.loads(result_path.read_text(encoding="utf-8")))
        return {}
    finally:
        if scratch_dir is None:
            shutil.rmtree(base, ignore_errors=True)


def render_episode(
    segments: Sequence[Any],
    out_m4a: str | Path,
    *,
    image: str | None = None,
    synth: Any | None = None,
    speed: float = 1.0,
    scratch_dir: str | Path | None = None,
    container_cmd: str = "podman",
    timeout: float | None = 600,
    encode: Callable[[Path, Path], None] = _ffmpeg_m4a,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Render ``segments`` to ``out_m4a``, container-first.

    ``image`` set → container path (worker, no ``[tts]`` needed). Else ``synth``
    given → in-process ``synthesize_text`` + ``encode`` (WAV→m4a). Neither → a
    ``RuntimeError`` (no backend). ``encode`` is injectable so a test can skip
    ffmpeg."""
    out = Path(out_m4a)
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
    return {"segments": res.segments, "duration_s": res.duration_s}


__all__ = ["render_episode", "render_via_container"]
