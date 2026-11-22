"""Unit tests for the per-host capability self-probe (slice 6b).

Pure, no DB: the vocabulary derives from the registry, each probe returns
present / absent / unknown from mocked tooling, and ``probe_host_resources``
never lets a broken probe escape (heartbeat liveness must survive it).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from precis.workers import capability_probe as cap

# Env overrides that would shadow the real probes — clear them per test.
_OVERRIDE_ENVS = (
    "PRECIS_GPU_COUNT",
    "PRECIS_PODMAN_SLOTS",
    "PRECIS_TTS_SLOTS",
    "PRECIS_TTS_IMAGE",
    "PRECIS_EMBEDDER_SLOTS",
    "PRECIS_EMBEDDER_URL",
    "PRECIS_CLAUDE_BIN",
    "PRECIS_FIX_WORK_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "PRECIS_SLICE_ROOT",
    "PRECIS_LATEXMK_SLOTS",
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
    assert {"gpu", "podman", "tts", "embedder"} <= vocab
    # Every probe key is a real capability some service requires (no orphans).
    assert set(cap._PROBES) <= vocab


def test_every_vocabulary_token_has_a_probe() -> None:
    """A ``requires`` token with no probe is permanently unknown — never
    advertised, so jobs gated on it can't be capability-routed anywhere.
    Adding a ``requires={"new"}`` service means adding a ``_PROBES["new"]``
    entry in the same change; this test is the forcing function."""
    missing = set(cap.capability_vocabulary()) - set(cap._PROBES)
    assert not missing, f"vocabulary tokens with no registered probe: {missing}"


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OrbStack's docker often isn't on a daemon's PATH — a full path in
    PRECIS_CONTAINER_BIN is found via existence, not ``which``."""
    binp = tmp_path / "docker"
    binp.write_text("#!/bin/sh\n", encoding="utf-8")
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


# ── embedder (§F cycle a) ─────────────────────────────────────────────


def test_embedder_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_EMBEDDER_SLOTS", "3")
    # Override bypasses the readyz probe entirely (mirrors gpu/podman/tts).
    monkeypatch.setattr(cap, "_embedder_ready", lambda url: False)
    assert cap._probe_embedder() == 3


def test_embedder_absent_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_EMBEDDER_URL", raising=False)
    assert cap._probe_embedder() == 0


def test_embedder_present_when_readyz_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_EMBEDDER_URL", "http://127.0.0.1:8181")
    monkeypatch.setattr(cap, "_embedder_ready", lambda url: True)
    assert cap._probe_embedder() == cap._EMBEDDER_DEFAULT_SLOTS


def test_embedder_unknown_when_configured_but_readyz_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL IS configured but the probe ATTEMPT fails (timeout,
    connection error, non-200) is UNKNOWN (None), mirroring gpu/tts — NOT
    absent (0). A busy daemon missing one 3s /readyz round-trip must not
    retract its resource_slots row (that would silently drop the claim
    gate exactly when the host is busiest)."""
    monkeypatch.setenv("PRECIS_EMBEDDER_URL", "http://127.0.0.1:8181")
    monkeypatch.setattr(cap, "_embedder_ready", lambda url: False)
    assert cap._probe_embedder() is None


def test_embedder_uses_first_endpoint_of_comma_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_EMBEDDER_URL", "http://127.0.0.1:8181,http://other:8181")
    seen: list[str] = []

    def _ready(url: str) -> bool:
        seen.append(url)
        return True

    monkeypatch.setattr(cap, "_embedder_ready", _ready)
    assert cap._probe_embedder() == cap._EMBEDDER_DEFAULT_SLOTS
    assert seen == ["http://127.0.0.1:8181"]


# ── probe_host_resources: the safe aggregate ────────────────────────────


def test_probe_host_resources_covers_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    monkeypatch.setattr(cap, "find_spec", lambda name: None)
    monkeypatch.setattr(cap, "_stored_claude_auth", lambda: False)
    result = cap.probe_host_resources()
    assert set(result) == set(cap.capability_vocabulary())
    # a bare host: no nvidia-smi, no podman, no tts, no embedder URL, no
    # git/claude/clone-work-dir/claude-auth → all definitively absent
    assert result["gpu"] == 0
    assert result["podman"] == 0
    assert result["tts"] == 0
    assert result["embedder"] == 0
    assert result["git"] == 0
    assert result["claude_bin"] == 0
    assert result["clones_dir"] == 0
    assert result["claude_config_mount"] == 0


def test_probe_host_resources_unknown_for_missing_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required capability with no registered probe ⇒ None (untouched)."""
    monkeypatch.setattr(cap, "capability_vocabulary", lambda: frozenset({"mystery"}))
    result = cap.probe_host_resources()
    assert result == {"mystery": None}


def test_missing_probe_warns_once_per_process(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The no-probe warning fires once per token per process, not once per
    heartbeat cycle (the log-spam half of capability-vocab-missing-probes)."""
    monkeypatch.setattr(cap, "capability_vocabulary", lambda: frozenset({"mystery2"}))
    monkeypatch.setattr(cap, "_WARNED_MISSING_PROBES", set())
    with caplog.at_level("WARNING", logger=cap.log.name):
        cap.probe_host_resources()
        cap.probe_host_resources()
    warned = [r for r in caplog.records if "no probe for required" in r.message]
    assert len(warned) == 1


# ── ADR-0048 sandbox-lane capability probes ─────────────────────────────


def test_probe_git_present_and_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which({"git"}))
    assert cap._probe_git() == 1
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    assert cap._probe_git() == 0


def test_probe_autocatpath_present_and_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr543909: the engine token reads the venv, never imports the engine."""
    monkeypatch.setattr(cap, "find_spec", lambda name: object())
    assert cap._probe_autocatpath() == 1
    monkeypatch.setattr(cap, "find_spec", lambda name: None)
    assert cap._probe_autocatpath() == 0

    def _boom(name: str) -> None:
        raise ValueError(name)

    monkeypatch.setattr(cap, "find_spec", _boom)
    assert cap._probe_autocatpath() == 0
    assert "autocatpath" in cap.capability_vocabulary()


def test_probe_has_mace_present_and_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr548810: the backend token reads the venv, never imports mace."""
    monkeypatch.setattr(cap, "find_spec", lambda name: object())
    assert cap._probe_has_mace() == 1
    monkeypatch.setattr(cap, "find_spec", lambda name: None)
    assert cap._probe_has_mace() == 0

    def _boom(name: str) -> None:
        raise ValueError(name)

    monkeypatch.setattr(cap, "find_spec", _boom)
    assert cap._probe_has_mace() == 0
    assert "has_mace" in cap.capability_vocabulary()


def test_vocabulary_includes_placement_tokens() -> None:
    """td543917: ``ServiceSpec.placement`` tokens are probed like ``requires``."""
    assert {"repo_checkout", "claude_bin", "git"} <= cap.capability_vocabulary()
    assert "repo_checkout" in cap._PROBES


def test_probe_repo_checkout_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    assert cap._probe_repo_checkout() == 1


def test_probe_repo_checkout_absent_without_dot_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path))
    assert cap._probe_repo_checkout() == 0
    monkeypatch.setenv("PRECIS_SLICE_ROOT", str(tmp_path / "missing"))
    assert cap._probe_repo_checkout() == 0


def test_probe_repo_checkout_absent_when_slice_root_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The jo543902 shape: a venv install with no checkout around it."""
    from precis.workers.job_types import run_slice

    def _no_checkout() -> None:
        raise RuntimeError("no git checkout around site-packages")

    monkeypatch.setattr(run_slice, "slice_root", _no_checkout)
    assert cap._probe_repo_checkout() == 0


def test_probe_repo_checkout_unknown_when_git_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.workers.job_types import run_slice

    def _hang() -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=300)

    monkeypatch.setattr(run_slice, "slice_root", _hang)
    assert cap._probe_repo_checkout() is None


def test_vocabulary_carries_per_repo_checkout_tokens() -> None:
    """Every repo profile has a ``repo_checkout_<name>`` token in the
    vocabulary with a probe; without it no host could ever advertise the
    token a second-repo job requires and that job would wait forever."""
    assert cap.SECOND_REPO_TOKENS
    assert "repo_checkout_catpath" in cap.SECOND_REPO_TOKENS
    assert cap.capability_vocabulary() >= cap.SECOND_REPO_TOKENS
    assert set(cap._PROBES) >= cap.SECOND_REPO_TOKENS


def test_probe_second_repo_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clone named by ``PRECIS_SLICE_REPOS`` with a ``.git`` advertises the
    token; breaking this strands every second-repo slice on a host that has
    the clone."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("PRECIS_SLICE_REPOS", json.dumps({"catpath": str(tmp_path)}))
    assert cap._PROBES["repo_checkout_catpath"]() == 1


def test_probe_second_repo_absent_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``PRECIS_SLICE_REPOS`` reads 0: a host without the clone must not
    claim a second-repo slice (it would fail at submit)."""
    monkeypatch.delenv("PRECIS_SLICE_REPOS", raising=False)
    assert cap._PROBES["repo_checkout_catpath"]() == 0


def test_probe_second_repo_absent_without_dot_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path that exists but holds no ``.git`` is not a clone."""
    monkeypatch.setenv("PRECIS_SLICE_REPOS", json.dumps({"catpath": str(tmp_path)}))
    assert cap._PROBES["repo_checkout_catpath"]() == 0
    monkeypatch.setenv(
        "PRECIS_SLICE_REPOS", json.dumps({"catpath": str(tmp_path / "missing")})
    )
    assert cap._PROBES["repo_checkout_catpath"]() == 0


def test_probe_claude_bin_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which({"claude"}))
    assert cap._probe_claude_bin() == 1


def test_probe_claude_bin_env_abspath_off_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A daemon PATH often misses claude — an absolute PRECIS_CLAUDE_BIN
    is found via existence + exec bit, the same read
    ``adapter_bin_present`` makes (gr676054: an override the host cannot
    exec must not advertise ``claude_bin``)."""
    binp = tmp_path / "claude"
    binp.write_text("#!/bin/sh\n", encoding="utf-8")
    binp.chmod(0o755)
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", str(binp))
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    assert cap._probe_claude_bin() == 1
    # Same path, exec bit dropped → absent, matching the pick filter.
    binp.chmod(0o644)
    assert cap._probe_claude_bin() == 0


def test_probe_claude_bin_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", _which(set()))
    assert cap._probe_claude_bin() == 0


def test_probe_clones_dir_env_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cap._probe_clones_dir() == 0  # autouse fixture cleared the env
    monkeypatch.setenv("PRECIS_FIX_WORK_DIR", "/tmp/fix-work")
    assert cap._probe_clones_dir() == 1


def test_probe_claude_config_env_token_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setattr(
        cap, "_stored_claude_auth", lambda: pytest.fail("must not consult the vault")
    )
    assert cap._probe_claude_config() == 1


def test_probe_claude_config_stored_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap, "_stored_claude_auth", lambda: True)
    assert cap._probe_claude_config() == 1
    monkeypatch.setattr(cap, "_stored_claude_auth", lambda: False)
    assert cap._probe_claude_config() == 0


@pytest.mark.parametrize("adapter", ["claude", "codex"])
@pytest.mark.parametrize(
    "binary, login", [(False, False), (False, True), (True, False), (True, True)]
)
def test_lane_tokens_require_binary_and_login(
    monkeypatch: pytest.MonkeyPatch, adapter: str, binary: bool, login: bool
) -> None:
    """gr701455: installed but logged-out CLIs cannot advertise a builder."""
    monkeypatch.setattr(cap, "claude_bin_present", lambda: False)
    monkeypatch.setattr(cap, "codex_bin_present", lambda: False)
    monkeypatch.setattr(cap, f"{adapter}_bin_present", lambda: binary)
    monkeypatch.setattr(cap, "claude_auth_present", lambda: login)
    monkeypatch.setattr(cap, "codex_login_present", lambda: login)
    monkeypatch.setattr(cap, "_probe_kimi_bin", lambda: 0)
    monkeypatch.setattr(cap, "_probe_devin_bin", lambda: 0)
    assert cap._PROBES[f"{adapter}_bin"]() == int(binary and login)
    assert cap._probe_builder() == int(binary and login)


def test_builder_floor_is_withheld_while_run_slice_is_suspended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's deploy-run units hold run_slice once the builder
    account takes the builds: their host stops advertising the ``builder``
    floor (build_queue_supply counts only hosts that carry it) while the
    lane-bin tokens other job types require stay."""
    monkeypatch.setattr(cap, "_probe_claude_lane", lambda: 1)
    monkeypatch.setattr(cap, "_probe_codex_bin", lambda: 1)
    monkeypatch.setattr(cap, "_probe_kimi_bin", lambda: 1)
    monkeypatch.setattr(cap, "_probe_devin_bin", lambda: 1)
    monkeypatch.delenv("PRECIS_SUSPENDED_JOB_TYPES", raising=False)
    assert cap._probe_builder() == 1
    monkeypatch.setenv("PRECIS_SUSPENDED_JOB_TYPES", "autocatpath_seed")
    assert cap._probe_builder() == 1, "another type's hold leaves the floor"
    monkeypatch.setenv("PRECIS_SUSPENDED_JOB_TYPES", "autocatpath_seed, run_slice")
    assert cap._probe_builder() == 0
    assert cap._probe_claude_lane() == 1


def test_claude_directory_without_credentials_is_not_a_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from precis import secrets

    monkeypatch.setattr(secrets, "get_secret", lambda name: None)
    monkeypatch.setattr(cap.sys, "platform", "linux")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert cap._stored_claude_auth() is False
    (tmp_path / ".credentials.json").write_text("{}", encoding="utf-8")
    assert cap._stored_claude_auth() is True


@pytest.mark.parametrize("returncode", [0, 44])
def test_claude_keychain_checks_presence_without_extracting_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int
) -> None:
    from precis import secrets

    monkeypatch.setattr(secrets, "get_secret", lambda name: None)
    monkeypatch.setattr(cap.sys, "platform", "darwin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    def probe(argv: list[str], **kwargs: object) -> SimpleNamespace:
        assert argv == [
            "security",
            "find-generic-password",
            "-s",
            "Claude Code-credentials",
        ]
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 5
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(cap.subprocess, "run", probe)
    assert cap._stored_claude_auth() is (returncode == 0)


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


# ── the upload_staging presence gauge (gr537770) ────────────────────────────


def test_upload_staging_reported_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host whose ``PRECIS_UPLOAD_STAGING`` resolves advertises the
    capability the ``paper_pull`` upload leg gates on; an unset (or
    inbox-nested) value reports nothing at all."""
    monkeypatch.delenv("PRECIS_UPLOAD_STAGING", raising=False)
    monkeypatch.delenv("PRECIS_WATCH_INBOX", raising=False)
    assert "upload_staging" not in cap.probe_soft_signals()
    staging = tmp_path / "staging"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(staging))
    monkeypatch.setenv("PRECIS_WATCH_INBOX", str(inbox))
    assert cap.probe_soft_signals()["upload_staging"] == 1
    # Inside the watched inbox reads as unconfigured — same predicate the
    # run path uses (pdf_upload.uploads_dir).
    monkeypatch.setenv("PRECIS_UPLOAD_STAGING", str(inbox / ".uploads"))
    assert "upload_staging" not in cap.probe_soft_signals()


def test_upload_staging_is_retractable() -> None:
    assert "upload_staging" in cap.RETRACTABLE_SOFT_SIGNALS


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


def test_claude_agent_capable_binary_or_verified_container_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr518089: the agent-lane verdict the worker's ``needs_claude="agent"``
    veto consumes — True with the binary, or without it only when the
    container lane is opted in AND verified; mirrors the router's
    claude_agent rescue (``_claude_rung_unavailable``)."""
    monkeypatch.setattr(cap, "claude_bin_present", lambda: True)
    _patch_container(monkeypatch, enabled=False, capable=False)
    assert cap.claude_agent_capable() is True  # the binary alone suffices

    monkeypatch.setattr(cap, "claude_bin_present", lambda: False)
    assert cap.claude_agent_capable() is False  # nothing to fall back on

    _patch_container(monkeypatch, enabled=True, capable=False)
    assert cap.claude_agent_capable() is False  # opted in but unverified

    _patch_container(monkeypatch, enabled=True, capable=True)
    assert cap.claude_agent_capable() is True  # the container lane carries it


# ── latexmk (draft_export TeX toolchain, gr592838) ──────────────────────


def test_latexmk_probe_follows_have_latexmk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe is the same predicate the dispatch gate reads — a host
    without ``latexmk`` advertises 0 so ``draft_export``'s placement
    holds its claims queued for a TeX-carrying host."""
    import precis.export.compile as compile_mod

    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)
    assert cap._probe_latexmk() == 1
    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: False)
    assert cap._probe_latexmk() == 0


def test_latexmk_probe_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PRECIS_LATEXMK_SLOTS`` overrides the probe — the operator's way to
    un-advertise TeX on a box whose toolchain is present but broken."""
    import precis.export.compile as compile_mod

    monkeypatch.setattr(compile_mod, "have_latexmk", lambda: True)
    monkeypatch.setenv("PRECIS_LATEXMK_SLOTS", "0")
    assert cap._probe_latexmk() == 0
    monkeypatch.setenv("PRECIS_LATEXMK_SLOTS", "1")
    assert cap._probe_latexmk() == 1


def test_latexmk_probe_is_registered() -> None:
    assert "latexmk" in cap._PROBES


# ── devin lane: binary + login, TTL-cached ──────────────────────────────


@pytest.fixture
def devin_probe(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The real devin login probe over fake seams: ``bin`` is whether the
    binary resolves, ``status`` the ``auth status`` answer, ``runs`` how
    many times ``auth status`` ran. The cache is dropped around each test."""
    from precis.workers.job_types import _slice_adapters as sa

    state = SimpleNamespace(bin=True, status="Logged in\n", rc=0, runs=0)

    def auth_status(_bin: str) -> subprocess.CompletedProcess[str]:
        state.runs += 1
        return subprocess.CompletedProcess([], state.rc, state.status, "")

    monkeypatch.setattr(sa, "adapter_bin_present", lambda name: state.bin)
    monkeypatch.setattr(sa, "devin_auth_status", auth_status)
    monkeypatch.setattr(sa, "_devin_login_cache", None)
    return state


@pytest.mark.parametrize(
    "binary, status, rc, token",
    [
        (True, "Logged in (via Devin)\n", 0, 1),
        (True, "Not logged in\n", 1, 0),
        (True, "Not logged in\n", 0, 0),
        (False, "Logged in\n", 0, 0),
    ],
)
def test_devin_bin_token_requires_binary_and_login(
    monkeypatch: pytest.MonkeyPatch,
    devin_probe: SimpleNamespace,
    binary: bool,
    status: str,
    rc: int,
    token: int,
) -> None:
    """A logged-out devin host must not advertise the lane: its preflight
    refusal would back the lane off for the whole fleet."""
    devin_probe.bin, devin_probe.status, devin_probe.rc = binary, status, rc
    for other in ("_probe_claude_lane", "_probe_codex_bin", "_probe_kimi_bin"):
        monkeypatch.setattr(cap, other, lambda: 0)
    assert cap._PROBES["devin_bin"]() == token
    assert cap._probe_builder() == token


def test_devin_bin_token_without_binary_never_runs_auth_status(
    devin_probe: SimpleNamespace,
) -> None:
    devin_probe.bin = False
    assert cap._probe_devin_bin() == 0
    assert devin_probe.runs == 0


def test_devin_login_probe_is_ttl_cached_and_bustable(
    devin_probe: SimpleNamespace,
) -> None:
    from precis.workers.job_types import _slice_adapters as sa

    assert cap.devin_login_present() is True
    assert cap.devin_login_present() is True
    assert devin_probe.runs == 1  # the second read was inside the TTL
    devin_probe.status, devin_probe.rc = "Not logged in\n", 1
    assert cap.devin_login_present() is True  # still the cached verdict
    sa._bust_devin_login_probe()
    assert cap.devin_login_present() is False
    assert devin_probe.runs == 2


def test_devin_login_probe_reprobes_after_the_ttl(
    devin_probe: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers.job_types import _slice_adapters as sa

    assert cap.devin_login_present() is True
    now = sa.time.monotonic()
    monkeypatch.setattr(
        sa.time, "monotonic", lambda: now + sa._DEVIN_LOGIN_PROBE_TTL_S + 1
    )
    assert cap.devin_login_present() is True
    assert devin_probe.runs == 2


def test_note_devin_logged_out_flips_the_cached_answer(
    devin_probe: SimpleNamespace,
) -> None:
    from precis.workers.job_types import _slice_adapters as sa

    assert cap.devin_login_present() is True
    sa.note_devin_logged_out()
    assert cap.devin_login_present() is False
    assert devin_probe.runs == 1  # recorded, not re-probed


@pytest.mark.parametrize(
    "stdout, rc, expected",
    [
        ("Logged in (via Devin)\n", 0, True),
        ("Not logged in\n", 0, False),
        ("Not logged in\n", 1, False),
        ("Logged in\n", 1, False),
        ("", 127, False),
    ],
)
def test_devin_status_logged_in_reading(stdout: str, rc: int, expected: bool) -> None:
    from precis.workers.job_types._slice_adapters import devin_status_logged_in

    status = subprocess.CompletedProcess([], rc, stdout, "")
    assert devin_status_logged_in(status) is expected


def test_darwin_mem_total_uses_absolute_sysctl_when_path_lacks_sbin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launchd PATH without /usr/sbin must not blank the host's mem_mb row."""
    seen: list[str] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append(argv[0])
        if argv[0] == "sysctl":
            raise FileNotFoundError("sysctl")
        assert argv == ["/usr/sbin/sysctl", "-n", "hw.memsize"]
        return SimpleNamespace(returncode=0, stdout="68719476736\n")

    monkeypatch.setattr(cap.subprocess, "run", run)
    assert cap._darwin_mem_total_mb() == 65536
    assert seen[0] == "/usr/sbin/sysctl"


def test_darwin_mem_total_falls_back_to_sysconf_when_sysctl_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no usable sysctl the page-count product still gives the total."""

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        raise FileNotFoundError(argv[0])

    values = {"SC_PHYS_PAGES": 4194304, "SC_PAGE_SIZE": 16384}
    monkeypatch.setattr(cap.subprocess, "run", run)
    monkeypatch.setattr(cap.os, "sysconf", lambda name: values[name])
    assert cap._darwin_mem_total_mb() == 65536


def test_darwin_mem_total_none_when_every_source_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown stays None (row left alone), never a made-up number."""

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="")

    def sysconf(name: str) -> int:
        raise ValueError(name)

    monkeypatch.setattr(cap.subprocess, "run", run)
    monkeypatch.setattr(cap.os, "sysconf", sysconf)
    assert cap._darwin_mem_total_mb() is None


# ── repo_checkout_<name> probe: malformed PRECIS_SLICE_REPOS (gr764103) ──


def test_repo_probe_warns_once_naming_env_and_repo_not_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A mangled PRECIS_SLICE_REPOS (the unquoted-unit symptom) still reads 0,
    and says why exactly once per process, without echoing the value."""
    cap._SLICE_REPOS_WARNED.clear()
    mangled = "{catpath:"
    monkeypatch.setenv("PRECIS_SLICE_REPOS", mangled)
    probe = cap._make_repo_probe("catpath")
    with caplog.at_level("WARNING", logger=cap.log.name):
        assert probe() == 0
        assert probe() == 0
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warns) == 1
    text = warns[0].getMessage()
    assert "PRECIS_SLICE_REPOS" in text and "catpath" in text
    assert mangled not in text
    cap._SLICE_REPOS_WARNED.clear()


def test_repo_probe_warning_skips_the_default_repo(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from precis.plans import lanes

    cap._SLICE_REPOS_WARNED.clear()
    monkeypatch.setenv("PRECIS_SLICE_REPOS", "{catpath:")
    with caplog.at_level("WARNING", logger=cap.log.name):
        cap._warn_once_if_slice_repos_malformed(
            lanes.DEFAULT_REPO, "PRECIS_SLICE_REPOS"
        )
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_repo_probe_stays_quiet_when_env_unset_or_valid(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    cap._SLICE_REPOS_WARNED.clear()
    probe = cap._make_repo_probe("catpath")
    with caplog.at_level("WARNING", logger=cap.log.name):
        monkeypatch.delenv("PRECIS_SLICE_REPOS", raising=False)
        assert probe() == 0
        # well-formed JSON, entry absent: the operator's mapping, not mangling
        monkeypatch.setenv("PRECIS_SLICE_REPOS", json.dumps({"other": str(tmp_path)}))
        assert probe() == 0
        # well-formed and present, but not a git checkout
        monkeypatch.setenv("PRECIS_SLICE_REPOS", json.dumps({"catpath": str(tmp_path)}))
        assert probe() == 0
        (tmp_path / ".git").mkdir()
        assert probe() == 1
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    cap._SLICE_REPOS_WARNED.clear()


def test_gate_image_probe_retracts_without_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr701559: a host missing either gate image advertises ``gate_image``
    as 0 (retract); both present is 1; an unaskable daemon is unknown."""
    monkeypatch.delenv(cap.GATE_PREFLIGHT_ENV, raising=False)
    present: dict[str, bool | None] = {}
    monkeypatch.setattr(cap, "_image_present", lambda image: present[image])
    present.update({"precis-mcp:premodels": True, "precis-dev": True})
    assert cap._probe_gate_image() == 1
    present["precis-dev"] = False
    assert cap._probe_gate_image() == 0
    present["precis-dev"] = None
    assert cap._probe_gate_image() is None


def test_gate_host_lack_names_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(cap.GATE_PREFLIGHT_ENV, raising=False)
    monkeypatch.setattr(cap, "_image_present", lambda image: image != "precis-dev")
    assert cap.gate_host_lack() == "image precis-dev"
    monkeypatch.setattr(cap, "_image_present", lambda image: True)
    monkeypatch.setattr(cap, "_probe_uv_bin", lambda: 0)
    assert cap.gate_host_lack() == "tool uv"
    monkeypatch.setattr(cap, "_probe_uv_bin", lambda: 1)
    assert cap.gate_host_lack() is None
    monkeypatch.setattr(cap, "_image_present", lambda image: None)
    assert cap.gate_host_lack() is None


def test_gate_tokens_gate_land_and_run_placement() -> None:
    from precis.workers.registry import SERVICES_BY_NAME

    for name in ("land_slice", "run_slice"):
        assert {"gate_image", "uv_bin"} <= SERVICES_BY_NAME[name].placement
