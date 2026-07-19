"""Optional CPU-limit flags for spawned job containers (nice-all-jobs).

:func:`container_limit_flags` is the shared helper spliced into every
``docker``/``podman run`` argv builder (struct_relax, alphafold, tts, the
sandbox agent executor); absent env must be a strict no-op (backward compat),
so this proves the helper AND the two easiest-to-assert pure builders it feeds.
"""

from __future__ import annotations

from precis.utils.container_limits import container_limit_flags
from precis.workers.job_types import struct_relax
from precis_bio.alphafold import build_fold_argv


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
