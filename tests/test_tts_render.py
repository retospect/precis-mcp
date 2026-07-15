"""The TTS render driver — container staging + backend dispatch. The container
path is pure (fake podman), so it runs without a TTS toolchain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from precis.draft.narrate import NarrationSegment
from precis.tts.render import render_episode, render_via_container

_SEGS = [
    NarrationSegment("Hello.", "af_heart", "en-us", "para"),
    NarrationSegment("你好", "zf_xiaoxiao", "cmn", "para"),
]


def _fake_podman(cmd, **kwargs):
    # find the -v <outdir>:/work/out mount, drop a render there
    outdir = next(Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/out"))
    indir = next(Path(a.split(":", 1)[0]) for a in cmd if a.endswith(":/work/in:ro"))
    # the worker staged the voice-score for the container to read
    payload = json.loads((indir / "segments.json").read_text())
    assert [s["lang"] for s in payload["segments"]] == ["en-us", "cmn"]
    (outdir / "out.m4a").write_bytes(b"m4a-bytes")
    (outdir / "result.json").write_text(json.dumps({"segments": 2, "duration_s": 3.2}))


def test_render_via_container_stages_runs_and_copies(tmp_path):
    out = tmp_path / "ep.m4a"
    result = render_via_container(_SEGS, out, image="precis-tts:test", run=_fake_podman)
    assert out.read_bytes() == b"m4a-bytes"
    assert result == {"segments": 2, "duration_s": 3.2}


def test_render_episode_dispatches_to_container(tmp_path):
    out = tmp_path / "ep.m4a"
    result = render_episode(_SEGS, out, image="precis-tts:test", run=_fake_podman)
    assert out.is_file() and result["segments"] == 2


def test_render_episode_no_backend_raises(tmp_path):
    with pytest.raises(RuntimeError, match="no TTS backend"):
        render_episode(_SEGS, tmp_path / "ep.m4a")


def test_render_via_container_missing_output_raises(tmp_path):
    def _noop(cmd, **kw):  # container "succeeds" but writes nothing
        return None

    with pytest.raises(RuntimeError, match="no out.m4a"):
        render_via_container(_SEGS, tmp_path / "ep.m4a", image="x", run=_noop)


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
