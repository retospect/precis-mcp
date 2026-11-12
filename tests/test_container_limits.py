"""Optional resource-limit flags for spawned job containers (nice-all-jobs,
gr595537 for the memory half).

:func:`container_limit_flags` is the shared helper spliced into every
``docker``/``podman run`` argv builder (struct_relax, alphafold, tts, the
sandbox agent executor); absent env must be a strict no-op (backward compat),
so this proves the helper AND the two easiest-to-assert pure builders it feeds.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from precis.utils.container_limits import (
    KILLED_FOR_MEMORY,
    container_limit_flags,
    container_memory_flags,
    container_user_flags,
    job_mem_limit_mb,
    mem_limit_preexec,
    memory_cap_killed,
)
from precis.workers.job_types import run_slice, struct_relax
from precis_bio.alphafold import build_fold_argv
from precis_chem.aizynth import build_aizynth_argv
from precis_chem.jobs import _build_normalizer_argv


@pytest.fixture(autouse=True)
def _clean_limit_env(monkeypatch):
    # The memory knobs must not leak in from a dev/daemon env — the
    # no-env-no-flag assertions above and below assume them unset.
    for var in (
        "PRECIS_JOB_MEM_LIMIT",
        "PRECIS_DFT_MEM_LIMIT",
        "PRECIS_FOLD_MEM_LIMIT",
    ):
        monkeypatch.delenv(var, raising=False)


def test_no_env_no_flags(monkeypatch):
    monkeypatch.delenv("PRECIS_JOB_CPUSET", raising=False)
    monkeypatch.delenv("PRECIS_JOB_CPU_SHARES", raising=False)
    assert container_limit_flags() == []


def test_cpuset_only(monkeypatch):
    monkeypatch.setenv("PRECIS_JOB_CPUSET", "2-19")
    monkeypatch.delenv("PRECIS_JOB_CPU_SHARES", raising=False)
    assert container_limit_flags() == ["--cpuset-cpus", "2-19"]


def test_both_flags(monkeypatch):
    monkeypatch.setenv("PRECIS_JOB_CPUSET", "2-19")
    monkeypatch.setenv("PRECIS_JOB_CPU_SHARES", "256")
    flags = container_limit_flags()
    assert flags == ["--cpuset-cpus", "2-19", "--cpu-shares", "256"]


def test_struct_relax_build_run_argv_carries_limits(monkeypatch):
    monkeypatch.setenv("PRECIS_JOB_CPUSET", "2-19")
    monkeypatch.setenv("PRECIS_JOB_CPU_SHARES", "256")
    argv = struct_relax.build_run_argv(ref_id=7, in_dir="/i", out_dir="/o")
    assert "--cpuset-cpus" in argv and "2-19" in argv
    assert "--cpu-shares" in argv and "256" in argv
    run_idx = argv.index("run")
    image_idx = argv.index("precis-dft:cpu")
    assert run_idx < argv.index("--cpuset-cpus") < image_idx


def test_struct_relax_build_run_argv_unset_is_unchanged(monkeypatch):
    monkeypatch.delenv("PRECIS_JOB_CPUSET", raising=False)
    monkeypatch.delenv("PRECIS_JOB_CPU_SHARES", raising=False)
    argv = struct_relax.build_run_argv(ref_id=7, in_dir="/i", out_dir="/o")
    assert "--cpuset-cpus" not in argv and "--cpu-shares" not in argv


def test_build_fold_argv_carries_limits(monkeypatch):
    monkeypatch.setenv("PRECIS_JOB_CPUSET", "2-19")
    monkeypatch.setenv("PRECIS_JOB_CPU_SHARES", "256")
    argv = build_fold_argv(
        ref_id=7,
        in_dir="/s/in",
        out_dir="/s/out",
        image="af3:sha",
        models_dir="/nas/models",
    )
    assert "--cpuset-cpus" in argv and "2-19" in argv
    assert "--cpu-shares" in argv and "256" in argv
    run_idx = argv.index("run")
    image_idx = argv.index("af3:sha")
    assert run_idx < argv.index("--cpuset-cpus") < image_idx


def test_build_fold_argv_unset_is_unchanged(monkeypatch):
    monkeypatch.delenv("PRECIS_JOB_CPUSET", raising=False)
    monkeypatch.delenv("PRECIS_JOB_CPU_SHARES", raising=False)
    argv = build_fold_argv(
        ref_id=7,
        in_dir="/s/in",
        out_dir="/s/out",
        image="af3:sha",
        models_dir="/nas/models",
    )
    assert "--cpuset-cpus" not in argv and "--cpu-shares" not in argv


# ── --user for share-writing science containers (cluster-fileserver-move) ──


def test_container_user_unset_is_no_flag(monkeypatch):
    monkeypatch.delenv("PRECIS_CONTAINER_USER", raising=False)
    assert container_user_flags() == []
    monkeypatch.setenv("PRECIS_CONTAINER_USER", "  ")
    assert container_user_flags() == []


def test_container_user_set(monkeypatch):
    monkeypatch.setenv("PRECIS_CONTAINER_USER", "1500:1500")
    assert container_user_flags() == ["--user", "1500:1500"]


def _user_before_image(argv: list[str], image: str) -> None:
    i = argv.index("--user")
    assert argv[i + 1] == "1500:1500"
    assert argv.index("run") < i < argv.index(image)


def test_every_science_builder_carries_the_user(monkeypatch):
    monkeypatch.setenv("PRECIS_CONTAINER_USER", "1500:1500")
    _user_before_image(
        struct_relax.build_run_argv(ref_id=7, in_dir="/i", out_dir="/o"),
        "precis-dft:cpu",
    )
    _user_before_image(
        build_fold_argv(
            ref_id=7, in_dir="/i", out_dir="/o", image="af3:sha", models_dir="/m"
        ),
        "af3:sha",
    )
    _user_before_image(
        build_aizynth_argv(
            ref_id=7, in_dir="/i", out_dir="/o", smiles="CCO", image="az:sha"
        ),
        "az:sha",
    )
    _user_before_image(
        _build_normalizer_argv(
            ref_id=7,
            in_dir="/i",
            out_dir="/o",
            input_format="az",
            engine="aizynth",
            engine_version="4",
            image="norm:sha",
            container_cmd="docker",
        ),
        "norm:sha",
    )


def test_science_builders_unset_pass_no_user(monkeypatch):
    monkeypatch.delenv("PRECIS_CONTAINER_USER", raising=False)
    assert "--user" not in struct_relax.build_run_argv(
        ref_id=7, in_dir="/i", out_dir="/o"
    )
    assert "--user" not in build_aizynth_argv(
        ref_id=7, in_dir="/i", out_dir="/o", smiles="CCO", image="az:sha"
    )


# ── per-task memory cap (gr595537) ────────────────────────────────────


def test_mem_no_source_no_flags():
    """Bare call, no env ⇒ uncapped, byte-identical (no-env-no-flag)."""
    assert job_mem_limit_mb() is None
    assert container_memory_flags() == []
    assert container_limit_flags() == []


def test_mem_env_size_string(monkeypatch):
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "8g")
    assert job_mem_limit_mb() == 8192.0
    assert container_memory_flags() == [
        "--memory",
        "8192m",
        "--memory-swap",
        "8192m",
    ]


def test_mem_env_size_spellings(monkeypatch):
    for raw, want in [
        ("512m", 512.0),
        ("40g", 40960.0),
        ("1.5g", 1536.0),
        ("1073741824", 1024.0),  # a bare number is bytes (docker's rule)
        ("4GB", 4e9 / (1024 * 1024)),  # the *b spellings are powers of ten
        ("8192k", 8.0),  # k = KiB = 1024 BYTES, not 1024 MiB
    ]:
        monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", raw)
        assert job_mem_limit_mb() == want


def test_mem_env_disable_wins_over_everything(monkeypatch):
    """The operator escape hatch: uncapped even with an estimate + a job
    knob."""
    for raw in ("0", "none", "off"):
        monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", raw)
        assert job_mem_limit_mb(4096) is None
        assert container_memory_flags(4096) == []
        assert container_memory_flags(job_limit="40g") == []


def test_mem_estimate_is_the_per_task_cap():
    """The footprint estimate (predicted peak + margin) is the per-task cap."""
    assert job_mem_limit_mb(2048) == 2048.0
    assert job_mem_limit_mb(2048.5) == 2048.5


def test_mem_env_is_a_ceiling(monkeypatch):
    """PRECIS_JOB_MEM_LIMIT clamps the estimate down and supplies the cap
    when none exists — never raises it."""
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "4g")
    assert job_mem_limit_mb() == 4096.0
    assert job_mem_limit_mb(8192) == 4096.0
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "16g")
    assert job_mem_limit_mb(2048) == 2048.0


def test_mem_env_unreadable_falls_back(monkeypatch):
    """A junk env value doesn't silently uncap — the estimate still applies."""
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "bogus")
    assert job_mem_limit_mb(8192) == 8192.0
    assert job_mem_limit_mb() is None


def test_science_builders_uncapped_without_env():
    """No-env-no-flag: the retrosynth/fold/dft containers emit no --memory
    when no operator knob is set (gr595537's opt-in contract)."""
    for argv in (
        build_aizynth_argv(
            ref_id=7, in_dir="/i", out_dir="/o", smiles="CCO", image="az:sha"
        ),
        _build_normalizer_argv(
            ref_id=7,
            in_dir="/i",
            out_dir="/o",
            input_format="az",
            engine="aizynth",
            engine_version="4",
            image="norm:sha",
            container_cmd="docker",
        ),
        build_fold_argv(
            ref_id=7, in_dir="/i", out_dir="/o", image="af3:sha", models_dir="/m"
        ),
        struct_relax.build_run_argv(ref_id=7, in_dir="/i", out_dir="/o"),
    ):
        assert "--memory" not in argv


def test_env_ceiling_reaches_the_builders(monkeypatch):
    """The operator ceiling lands as ONE --memory pair on every science
    builder."""
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "4g")
    for argv in (
        build_aizynth_argv(
            ref_id=7, in_dir="/i", out_dir="/o", smiles="CCO", image="az:sha"
        ),
        build_fold_argv(
            ref_id=7, in_dir="/i", out_dir="/o", image="af3:sha", models_dir="/m"
        ),
        struct_relax.build_run_argv(ref_id=7, in_dir="/i", out_dir="/o"),
    ):
        assert argv.count("--memory") == 1
        i = argv.index("--memory")
        assert argv[i + 1 : i + 4] == ["4096m", "--memory-swap", "4096m"]


def test_disable_removes_builder_cap(monkeypatch):
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "0")
    argv = build_aizynth_argv(
        ref_id=7, in_dir="/i", out_dir="/o", smiles="CCO", image="az:sha"
    )
    assert "--memory" not in argv


def _mem_pair(argv: list[str]) -> list[str]:
    """The ``--memory <m> --memory-swap <m>`` pair in an argv, or ``[]``."""
    if "--memory" not in argv:
        return []
    i = argv.index("--memory")
    return argv[i : i + 4]


def _fold_argv() -> list[str]:
    import os

    return build_fold_argv(
        ref_id=7,
        in_dir="/i",
        out_dir="/o",
        image="af3:sha",
        models_dir="/m",
        mem_limit=os.environ.get("PRECIS_FOLD_MEM_LIMIT"),
    )


def _dft_argv() -> list[str]:
    return struct_relax.build_run_argv(ref_id=7, in_dir="/i", out_dir="/o")


def test_job_limit_alone_emits_its_cap(monkeypatch):
    """A job knob with no fleet ceiling behaves exactly as before — its
    spelling verbatim, so the argv is byte-identical."""
    monkeypatch.setenv("PRECIS_DFT_MEM_LIMIT", "40g")
    monkeypatch.setenv("PRECIS_FOLD_MEM_LIMIT", "100g")
    assert _mem_pair(_dft_argv()) == ["--memory", "40g", "--memory-swap", "40g"]
    assert _mem_pair(_fold_argv()) == [
        "--memory",
        "100g",
        "--memory-swap",
        "100g",
    ]


def test_job_limit_merges_into_one_cap(monkeypatch):
    """gr595537 review: a job knob (PRECIS_DFT/FOLD_MEM_LIMIT) resolves
    WITH the shared ceiling — one --memory pair at the minimum, never a
    second flag appended later that would override the operator ceiling."""
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "8g")
    monkeypatch.setenv("PRECIS_DFT_MEM_LIMIT", "40g")
    monkeypatch.setenv("PRECIS_FOLD_MEM_LIMIT", "100g")
    for argv in (_dft_argv(), _fold_argv()):
        assert argv.count("--memory") == 1
        assert _mem_pair(argv) == ["--memory", "8192m", "--memory-swap", "8192m"]
    # A job knob tighter than the fleet ceiling still stands (verbatim).
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "200g")
    assert _mem_pair(_dft_argv())[1] == "40g"
    assert _mem_pair(_fold_argv())[1] == "100g"


def test_disable_beats_the_job_knob(monkeypatch):
    """Nothing overrides an explicit disable — not even a job knob."""
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "0")
    monkeypatch.setenv("PRECIS_DFT_MEM_LIMIT", "40g")
    assert "--memory" not in struct_relax.build_run_argv(
        ref_id=7, in_dir="/i", out_dir="/o"
    )


def test_unreadable_job_limit_passes_through_raw(monkeypatch):
    """A mis-set job knob stays loud: passed to docker verbatim (which
    rejects it at run time), not silently dropped."""
    monkeypatch.setenv("PRECIS_DFT_MEM_LIMIT", "bogus")
    argv = struct_relax.build_run_argv(ref_id=7, in_dir="/i", out_dir="/o")
    i = argv.index("--memory")
    assert argv[i + 1 : i + 4] == ["bogus", "--memory-swap", "bogus"]


def test_mem_limit_preexec_resolves(monkeypatch):
    """No cap source ⇒ None (Popen takes preexec_fn=None); a cap ⇒ callable."""
    assert mem_limit_preexec() is None
    assert callable(mem_limit_preexec(64))
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "1g")
    assert callable(mem_limit_preexec())


def test_run_slice_launch_passes_the_mem_preexec(monkeypatch, tmp_path):
    """gr595537 wiring: the bare slice child gets the cap as ``preexec_fn``
    — ``None`` when no cap resolves (the finding: the helper had no
    production caller)."""
    seen: dict = {}

    class _Proc:
        pid = 4242

    def fake_popen(_argv, **kw):
        seen.update(kw)
        return _Proc()

    monkeypatch.setattr(run_slice.subprocess, "Popen", fake_popen)
    kw = dict(
        cwd=tmp_path,
        stdin=None,
        log=tmp_path / "log",
        exit_file=tmp_path / "exit",
        env={},
    )
    monkeypatch.setenv("PRECIS_JOB_MEM_LIMIT", "1g")
    assert run_slice._launch(["echo", "hi"], **kw) == 4242
    assert callable(seen["preexec_fn"])
    monkeypatch.delenv("PRECIS_JOB_MEM_LIMIT")
    run_slice._launch(["echo", "hi"], **kw)
    assert seen["preexec_fn"] is None


@pytest.mark.skipif(os.name != "posix", reason="rlimits are POSIX-only")
def test_mem_limit_preexec_sets_rlimit_data():
    """The spawned child inherits RLIMIT_DATA at the resolved cap (the
    soft limit; the child reads its own to prove it landed)."""
    import resource

    if not hasattr(resource, "RLIMIT_DATA"):
        pytest.skip("platform lacks RLIMIT_DATA")
    # 512 MiB — comfortably above the interpreter's own startup footprint.
    cap = 512 * 1024 * 1024
    fn = mem_limit_preexec(512)
    assert fn is not None
    code = (
        "import resource, sys\n"
        "soft, _ = resource.getrlimit(resource.RLIMIT_DATA)\n"
        f"sys.exit(0 if soft == {cap} else 7)\n"
    )
    rc = subprocess.run(
        [sys.executable, "-c", code], preexec_fn=fn, check=False
    ).returncode
    assert rc == 0


@pytest.mark.skipif(os.name != "posix", reason="rlimits are POSIX-only")
def test_mem_limit_preexec_address_space_opt_in():
    """address_space=True adds RLIMIT_AS; off by default because CUDA/torch
    reserve huge virtual space never resident."""
    import resource

    if not hasattr(resource, "RLIMIT_AS"):
        pytest.skip("platform lacks RLIMIT_AS")
    # 64 GiB — RLIMIT_AS counts every shared-library mapping (the dyld
    # shared cache alone is several GiB on macOS), so the child interpreter
    # needs generous virtual headroom just to start.
    cap = 65536 * 1024 * 1024
    fn = mem_limit_preexec(65536, address_space=True)
    assert fn is not None
    code = (
        "import resource, sys\n"
        "soft, _ = resource.getrlimit(resource.RLIMIT_AS)\n"
        f"sys.exit(0 if soft == {cap} else 7)\n"
    )
    rc = subprocess.run(
        [sys.executable, "-c", code], preexec_fn=fn, check=False
    ).returncode
    assert rc == 0


def test_memory_cap_killed_signature():
    """137/-9 is the cap kill's signature; the OOMKilled flag is
    authoritative when the caller inspected the container."""
    assert memory_cap_killed(137)
    assert memory_cap_killed(-9)
    for rc in (0, 1, 137 - 1, 143, -15, None):
        assert not memory_cap_killed(rc)
    assert memory_cap_killed(0, oom_killed=True)
    assert not memory_cap_killed(137, oom_killed=False)
    assert KILLED_FOR_MEMORY == "killed-for-memory"
