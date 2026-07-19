"""Unit tests for the per-host capability self-probe (slice 6b).

Pure, no DB: the vocabulary derives from the registry, each probe returns
present / absent / unknown from mocked tooling, and ``probe_host_resources``
never lets a broken probe escape (heartbeat liveness must survive it).
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from precis.workers import capability_probe as cap

# Env overrides that would shadow the real probes — clear them per test.
_OVERRIDE_ENVS = (
    "PRECIS_GPU_COUNT",
    "PRECIS_PODMAN_SLOTS",
    "PRECIS_TTS_SLOTS",
    "PRECIS_TTS_IMAGE",
)


@pytest.fixture(autouse=True)
def _clear_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OVERRIDE_ENVS:
        monkeypatch.delenv(name, raising=False)


def _which(present: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in present else None


# ── vocabulary ──────────────────────────────────────────────────────────


def test_vocabulary_derives_from_registry() -> None:
    """The evaluated set is exactly the union of every ``requires`` token."""
    vocab = cap.capability_vocabulary()
    # The capabilities services declare today.
    assert {"gpu", "podman", "tts"} <= vocab
    # Every probe key is a real capability some service requires (no orphans).
    assert set(cap._PROBES) <= vocab


# ── container runtime (podman / docker / OrbStack) ──────────────────────


def _no_container_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("PRECIS_CONTAINER_BIN", "PRECIS_PODMAN_BIN", "PRECIS_PODMAN_SLOTS"):
        monkeypatch.delenv(v, raising=False)


def test_container_runtime_prefers_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_container_env(monkeypatch)
    monkeypatch.setattr(cap.shutil, "which", _which({"podman", "docker"}))
    assert cap.container_runtime() == "podman"


def test_container_runtime_falls_back_to_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_container_env(monkeypatch)
    monkeypatch.setattr(cap.shutil, "which", _which({"docker"}))
    assert cap.container_runtime() == "docker"


def test_container_runtime_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_container_env(monkeypatch)
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    assert cap.container_runtime() is None


def test_container_runtime_explicit_abspath_off_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """OrbStack's docker often isn't on a daemon's PATH — a full path in
    PRECIS_CONTAINER_BIN is found via existence, not ``which``."""
    binp = tmp_path / "docker"
    binp.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PRECIS_CONTAINER_BIN", str(binp))
    monkeypatch.setattr(cap.shutil, "which", _which(set()))  # not on PATH
    assert cap.container_runtime() == str(binp)


def test_probe_podman_counts_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_container_env(monkeypatch)
    monkeypatch.setattr(cap.shutil, "which", _which({"docker"}))
    assert cap._probe_podman() == 2


def test_probe_podman_zero_without_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_container_env(monkeypatch)
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    assert cap._probe_podman() == 0


# ── gpu ─────────────────────────────────────────────────────────────────


def test_gpu_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_GPU_COUNT", "3")
    assert cap._probe_gpu() == 3


def test_gpu_absent_without_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``nvidia-smi`` on PATH ⇒ definitively 0 (every Mac node)."""
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    assert cap._probe_gpu() == 0


def test_gpu_counts_nvidia_smi_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which({"nvidia-smi"}))
    out = "GPU 0: NVIDIA A100 (UUID: x)\nGPU 1: NVIDIA A100 (UUID: y)\n"
    monkeypatch.setattr(
        cap.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=out),
    )
    assert cap._probe_gpu() == 2


def test_gpu_unknown_when_nvidia_smi_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binary present but the call blows up ⇒ None (leave the row alone)."""
    monkeypatch.setattr(cap.shutil, "which", _which({"nvidia-smi"}))

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=10)

    monkeypatch.setattr(cap.subprocess, "run", _boom)
    assert cap._probe_gpu() is None


def test_gpu_unknown_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which({"nvidia-smi"}))
    monkeypatch.setattr(
        cap.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=9, stdout=""),
    )
    assert cap._probe_gpu() is None


# ── podman ──────────────────────────────────────────────────────────────


def test_podman_present_default_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which({"podman"}))
    assert cap._probe_podman() == 2


def test_podman_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    assert cap._probe_podman() == 0


def test_podman_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_PODMAN_SLOTS", "5")
    monkeypatch.setattr(cap.shutil, "which", _which(set()))  # override still wins
    assert cap._probe_podman() == 5


# ── tts ─────────────────────────────────────────────────────────────────


def test_tts_container_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PRECIS_TTS_IMAGE`` + podman ⇒ present (the cluster path)."""
    monkeypatch.setenv("PRECIS_TTS_IMAGE", "precis-tts:latest")
    monkeypatch.setattr(cap.shutil, "which", _which({"podman"}))
    assert cap._probe_tts() == 1


def test_tts_image_without_podman_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_TTS_IMAGE", "precis-tts:latest")
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    monkeypatch.setattr(cap, "find_spec", lambda name: None)
    assert cap._probe_tts() == 0


def test_tts_local_extra_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """No image, but the ``[tts]`` extra (``kokoro_onnx``) is importable."""
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    monkeypatch.setattr(
        cap, "find_spec", lambda name: object() if name == "kokoro_onnx" else None
    )
    assert cap._probe_tts() == 1


def test_tts_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    monkeypatch.setattr(cap, "find_spec", lambda name: None)
    assert cap._probe_tts() == 0


# ── probe_host_resources: the safe aggregate ────────────────────────────


def test_probe_host_resources_covers_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    monkeypatch.setattr(cap, "find_spec", lambda name: None)
    result = cap.probe_host_resources()
    assert set(result) == set(cap.capability_vocabulary())
    # a bare host: no nvidia-smi, no podman, no tts → all definitively absent
    assert result["gpu"] == 0
    assert result["podman"] == 0
    assert result["tts"] == 0


def test_probe_host_resources_unknown_for_missing_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required capability with no registered probe ⇒ None (untouched)."""
    monkeypatch.setattr(cap, "capability_vocabulary", lambda: frozenset({"mystery"}))
    result = cap.probe_host_resources()
    assert result == {"mystery": None}


def test_probe_host_resources_swallows_probe_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that raises must be downgraded to unknown, not propagate."""

    def _raiser() -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(cap, "capability_vocabulary", lambda: frozenset({"gpu"}))
    monkeypatch.setitem(cap._PROBES, "gpu", _raiser)
    result = cap.probe_host_resources()  # must not raise
    assert result == {"gpu": None}


# ── 6d-deferred: soft memory-pressure signal ─────────────────────────────


def test_resource_kind_mem_is_soft() -> None:
    assert cap.resource_kind("mem") == "soft"
    assert cap.resource_kind("gpu") == "hard"


def test_soft_signal_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MEM_PRESSURE_FREE", "0")
    assert cap.probe_soft_signals() == {"mem": 0}


def test_soft_signal_override_clamped_to_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_MEM_PRESSURE_FREE", "9")
    assert cap.probe_soft_signals()["mem"] == cap.mem_capacity()


# ── the container_agent verified-capability soft gauge ───────────────────────


def _patch_container(
    monkeypatch: pytest.MonkeyPatch, *, enabled: bool, capable: bool | Exception
) -> None:
    from precis.workers.executors import agent_container

    monkeypatch.setattr(agent_container, "container_agent_enabled", lambda: enabled)

    def _cap(*a, **k):
        if isinstance(capable, Exception):
            raise capable
        return capable

    monkeypatch.setattr(agent_container, "container_capability_ok", _cap)


def test_container_agent_omitted_when_not_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that never opted in advertises no container_agent row at all —
    silence there is correct (the gauge means 'you asked; here's whether it
    works'). ``mem`` is still reported."""
    monkeypatch.setenv("PRECIS_MEM_PRESSURE_FREE", "2")
    _patch_container(monkeypatch, enabled=False, capable=True)
    assert cap.probe_soft_signals() == {"mem": 2}


def test_container_agent_verified_reports_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MEM_PRESSURE_FREE", "2")
    _patch_container(monkeypatch, enabled=True, capable=True)
    sig = cap.probe_soft_signals()
    assert sig["container_agent"] == cap.soft_capacity("container_agent")
    assert cap.soft_capacity("container_agent") == 1


def test_container_agent_degraded_reports_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opted in but the runtime/image/token probe fails ⇒ 0 (degraded), so the
    console renders it red instead of the host silently running in-proc."""
    monkeypatch.setenv("PRECIS_MEM_PRESSURE_FREE", "2")
    _patch_container(monkeypatch, enabled=True, capable=False)
    assert cap.probe_soft_signals()["container_agent"] == 0


def test_container_agent_probe_error_reads_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising capability probe on an opted-in host is fail-*visible*: 0, not
    omitted — an unverifiable opted-in host must not read as healthy."""
    monkeypatch.setenv("PRECIS_MEM_PRESSURE_FREE", "2")
    _patch_container(monkeypatch, enabled=True, capable=RuntimeError("boom"))
    assert cap.probe_soft_signals()["container_agent"] == 0


def test_container_agent_kind_is_soft() -> None:
    assert cap.resource_kind("container_agent") == "soft"


def test_soft_capacity_unknown_defaults_to_one() -> None:
    assert cap.soft_capacity("frobnicator") == 1


def test_retractable_soft_signals_covers_container_not_mem() -> None:
    """container_agent must be retracted on opt-out (absence = definitive);
    mem must NOT be (absence = unmeasurable → leave)."""
    assert "container_agent" in cap.RETRACTABLE_SOFT_SIGNALS
    assert "mem" not in cap.RETRACTABLE_SOFT_SIGNALS
