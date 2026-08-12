"""The TTS render driver — container staging + backend dispatch. The container
path is pure (fake podman), so it runs without a TTS toolchain."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from precis.draft.narrate import NarrationSegment
from precis.tts.render import ContainerRenderError, render_episode, render_via_container

# The fake podman helpers below parse a "<host-path>:/work/out"-style bind
# mount by splitting on the first ':' — Windows host paths carry their own
# drive-letter colon (e.g. "C:\\...\\tmp:/work/out"), so the split lands on
# the wrong separator and mangles the path.
_needs_posix_mount_paths = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake-podman mount-arg split on ':' collides with the Windows"
    " drive-letter colon in the host path",
)

_SEGS = [
    NarrationSegment("Hello.", "af_heart", "en-us", "para"),
    NarrationSegment("你好", "zf_xiaoxiao", "cmn", "para"),
]

_SEGS_WITH_GAP = [
    NarrationSegment("Hello.", "af_heart", "en-us", "para", gap_after=1.5),
    NarrationSegment("你好", "zf_xiaoxiao", "cmn", "para"),
]


def _fake_podman(cmd, **kwargs):
    # find the -v <outdir>:/work/out mount, drop a render there
    outdir = next(Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/out"))
    indir = next(Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/in:ro"))
    # the worker staged the voice-score for the container to read
    payload = json.loads((indir / "segments.json").read_text())
    assert [s["lang"] for s in payload["segments"]] == ["en-us", "cmn"]
    (outdir / "out.mp3").write_bytes(b"mp3-bytes")
    (outdir / "result.json").write_text(json.dumps({"segments": 2, "duration_s": 3.2}))


@_needs_posix_mount_paths
def test_render_via_container_stages_runs_and_copies(tmp_path):
    out = tmp_path / "ep.mp3"
    result = render_via_container(_SEGS, out, image="precis-tts:test", run=_fake_podman)
    assert out.read_bytes() == b"mp3-bytes"
    assert result == {"segments": 2, "duration_s": 3.2, "audio_path": str(out)}


@_needs_posix_mount_paths
def test_render_via_container_tolerates_legacy_m4a_image(tmp_path):
    # An older, un-rebuilt precis-tts image still writes out.m4a. The read-back
    # must publish it as m4a (matching bytes/mime) rather than dark-holing it.
    def _old_image(cmd, **kw):
        outdir = next(Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/out"))
        (outdir / "out.m4a").write_bytes(b"m4a-bytes")

    out = tmp_path / "ep.mp3"  # caller asked for mp3
    result = render_via_container(_SEGS, out, image="x", run=_old_image)
    written = Path(result["audio_path"])
    assert written == tmp_path / "ep.m4a"  # suffix follows what was produced
    assert written.read_bytes() == b"m4a-bytes"
    assert not out.exists()


@_needs_posix_mount_paths
def test_render_episode_dispatches_to_container(tmp_path):
    out = tmp_path / "ep.mp3"
    result = render_episode(_SEGS, out, image="precis-tts:test", run=_fake_podman)
    assert out.is_file() and result["segments"] == 2
    assert result["audio_path"] == str(out)


@_needs_posix_mount_paths
def test_render_via_container_serializes_gap_after_per_segment(tmp_path):
    captured_payload = {}

    def _capture_podman(cmd, **kwargs):
        indir = next(
            Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/in:ro")
        )
        outdir = next(Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/out"))
        captured_payload.update(json.loads((indir / "segments.json").read_text()))
        (outdir / "out.mp3").write_bytes(b"mp3-bytes")

    render_via_container(
        _SEGS_WITH_GAP,
        tmp_path / "ep.mp3",
        image="precis-tts:test",
        run=_capture_podman,
    )
    assert "pause_s" not in captured_payload  # no top-level scalar pause any more
    segs = captured_payload["segments"]
    assert segs[0]["gap_after"] == 1.5
    assert segs[1]["gap_after"] is None


@_needs_posix_mount_paths
def test_render_episode_forwards_gap_after_to_container(tmp_path):
    captured = {}

    def _run(cmd, **kw):
        indir = next(
            Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/in:ro")
        )
        outdir = next(Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/out"))
        captured.update(json.loads((indir / "segments.json").read_text()))
        (outdir / "out.mp3").write_bytes(b"mp3-bytes")

    render_episode(_SEGS_WITH_GAP, tmp_path / "ep.mp3", image="x", run=_run)
    assert "pause_s" not in captured
    assert captured["segments"][0]["gap_after"] == 1.5


@_needs_posix_mount_paths
def test_render_via_container_bounds_the_run_with_a_timeout(tmp_path):
    captured = {}

    def _run(cmd, **kw):
        captured.update(kw)
        outdir = next(Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/out"))
        (outdir / "out.mp3").write_bytes(b"x")

    render_via_container(_SEGS, tmp_path / "e.mp3", image="x", timeout=42, run=_run)
    assert captured.get("timeout") == 42  # a hung render can't block forever


def test_render_episode_no_backend_raises(tmp_path):
    with pytest.raises(RuntimeError, match="no TTS backend"):
        render_episode(_SEGS, tmp_path / "ep.mp3")


def test_render_via_container_includes_stderr_tail_on_failure(tmp_path):
    # A container failure must surface the real traceback, not just "exit
    # status 1" — the process log otherwise strands it inside the (--rm'd)
    # container.
    def _boom(cmd, **kw):
        raise subprocess.CalledProcessError(
            1, cmd, output="stdout noise", stderr="x" * 3000 + "TAIL MARKER"
        )

    with pytest.raises(RuntimeError) as excinfo:
        render_via_container(_SEGS, tmp_path / "ep.mp3", image="x", run=_boom)
    msg = str(excinfo.value)
    assert "TAIL MARKER" in msg  # the tail, not the head, of stderr is kept
    assert "1" in msg  # return code


def test_render_via_container_raises_container_render_error_with_returncode(tmp_path):
    # gr204287: the raised error must be the typed ContainerRenderError (not a
    # bare RuntimeError) with .returncode threaded through, so a caller (e.g.
    # cast_audio) can tell a killed container (143) apart from a real failure.
    def _boom(cmd, **kw):
        raise subprocess.CalledProcessError(143, cmd, output="", stderr="killed")

    with pytest.raises(ContainerRenderError) as excinfo:
        render_via_container(_SEGS, tmp_path / "ep.mp3", image="x", run=_boom)
    assert excinfo.value.returncode == 143
    assert "143" in str(excinfo.value)


def test_render_via_container_falls_back_to_stdout_when_stderr_empty(tmp_path):
    def _boom(cmd, **kw):
        raise subprocess.CalledProcessError(
            1, cmd, output="stdout tail here", stderr=""
        )

    with pytest.raises(RuntimeError, match="stdout tail here"):
        render_via_container(_SEGS, tmp_path / "ep.mp3", image="x", run=_boom)


def test_render_via_container_missing_output_raises(tmp_path):
    def _noop(cmd, **kw):  # container "succeeds" but writes nothing
        return None

    with pytest.raises(RuntimeError, match="no out.mp3"):
        render_via_container(_SEGS, tmp_path / "ep.mp3", image="x", run=_noop)


def test_render_episode_in_process(tmp_path):
    # in-process path needs numpy/soundfile (the [tts] extra); skip where absent.
    pytest.importorskip("numpy")
    pytest.importorskip("soundfile")
    import numpy as np

    class _FakeSynth:
        def synthesize(self, text, *, voice, lang):
            return np.zeros(2400, dtype=np.float32), 24000

    def _fake_encode(wav, out):
        import shutil

        shutil.copyfile(wav, out)

    out = tmp_path / "ep.m4a"
    result = render_episode(_SEGS, out, synth=_FakeSynth(), encode=_fake_encode)
    assert out.is_file() and result["segments"] == 2
