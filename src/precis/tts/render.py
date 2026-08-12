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
from precis.utils.container_limits import container_limit_flags

log = logging.getLogger(__name__)


#: Container exit codes that mean the render was **collaterally killed** — a
#: graceful signal from outside the render, not a broken draft: 143 = 128+SIGTERM
#: (15). A worker restart (a deploy, a jetsam cull, a manual bounce) SIGTERMs the
#: worker's cgroup, which reaches the in-flight ``docker/podman run`` child, so the
#: container exits 143 — this is the collateral case the cast_audio backoff must
#: not escalate on (see ``precis.workers.cast_audio._stamp_failure``). ``subprocess``
#: can also surface SIGTERM as the negative ``-15``, so accept both encodings.
#:
#: **SIGKILL (137 / -9) is deliberately excluded**: it is ambiguous — a cgroup
#: OOM-killer kills a genuinely-oversized render with the *same* 137 a hard bounce
#: produces, and nothing bounds a single draft's memory (``container_limit_flags``
#: sets CPU only). Since the observed prod restart-kills are all 143 (SIGTERM,
#: zero 137s), treating 137 as a genuine failure is the safe default: an OOM-
#: looping draft correctly backs off to the hourly ceiling instead of respinning a
#: container every ~2 minutes for 48h.
SIGNAL_KILL_RETURNCODES = frozenset({143, -15})


class ContainerRenderError(RuntimeError):
    """A ``precis-tts`` container run exited non-zero.

    Carries the container's ``returncode`` so a caller can distinguish a *killed*
    render (:data:`SIGNAL_KILL_RETURNCODES`) from a genuine render failure (exit
    1 with a traceback in ``stderr``). The message keeps the historical
    ``precis-tts container exited N: <stderr>`` shape so existing log-greps and
    the captured container stderr are unchanged.
    """

    def __init__(self, returncode: int | None, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"precis-tts container exited {returncode}: {stderr}")


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
    output. Each segment's own ``gap_after`` (a content/markup property, not a
    synth knob) rides along in the payload so the stitch — which runs *inside*
    the container — can honour a per-segment silence override; there is no
    top-level scalar pause any more (an older, un-rebuilt image just sees
    ``gap_after: null`` and keeps its own kind-based default)."""
    payload = {
        "segments": [
            {
                "text": s.text,
                "voice": s.voice,
                "lang": s.lang,
                "kind": s.kind,
                "gap_after": s.gap_after,
            }
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
        argv = [container_cmd, "run", "--rm"]
        argv += container_limit_flags()
        argv += [
            "-v",
            f"{indir}:/work/in:ro",
            "-v",
            f"{outdir}:/work/out",
            image,
        ]
        try:
            run(
                argv,
                check=True,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            # Capture what the container printed — without this the daemon
            # log only ever shows "exit status 1" and the real traceback is
            # stranded inside the (already-removed, --rm) container.
            stderr = (getattr(exc, "stderr", "") or "")[-2000:]
            if not stderr:
                stderr = (getattr(exc, "stdout", "") or "")[-2000:]
            raise ContainerRenderError(exc.returncode, stderr) from exc
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
    that, not the requested path (the container path may have produced m4a).
    Inter-segment silence is a per-segment ``NarrationSegment.gap_after``
    (a content property, not a synth knob) — this driver carries no scalar
    pause of its own; it just forwards ``segments`` unchanged to whichever
    backend stitches."""
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


__all__ = [
    "SIGNAL_KILL_RETURNCODES",
    "ContainerRenderError",
    "render_episode",
    "render_via_container",
]
