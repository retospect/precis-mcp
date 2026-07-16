"""The one WAV→MP3 encode — a single source of truth for the podcast enclosure
format, shared by every producer path.

Three flows stitch a WAV then encode it: the in-process render driver
(:mod:`precis.tts.render`), the precis-tts container entrypoint
(``docker/tts/precis-tts-run``, running *inside* the container so the worker
venv needs no ffmpeg), and the manual ``precis draft audio`` CLI. They run in
different processes/environments, but the codec + bitrate must stay identical,
so they all call :func:`encode_mp3` rather than each spelling out ffmpeg.

MP3 (libmp3lame) is deliberate: the one audio format that plays everywhere,
incl. Apple Podcasts / Safari / iOS, so a shared enclosure or a copied file
just works. Stdlib-only (subprocess), so this imports cleanly in the container.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Enclosure bitrate. 128k libmp3lame is transparent for spoken voice while
#: keeping a ~45-min meditation cast a comfortable size over Tailscale.
MP3_BITRATE = "128k"


def encode_mp3(wav: Path, out: Path, *, bitrate: str = MP3_BITRATE) -> None:
    """Transcode a WAV to MP3 at ``out`` (``ffmpeg`` on PATH). Raises on a
    non-zero ffmpeg exit."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(wav),
            "-c:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            str(out),
        ],
        check=True,
    )


__all__ = ["MP3_BITRATE", "encode_mp3"]
