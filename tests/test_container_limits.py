"""Optional CPU-limit flags for spawned job containers (nice-all-jobs).

:func:`container_limit_flags` is the shared helper spliced into every
``docker``/``podman run`` argv builder (struct_relax, alphafold, tts, the
sandbox agent executor); absent env must be a strict no-op (backward compat),
so this proves the helper AND the two easiest-to-assert pure builders it feeds.
"""

from __future__ import annotations

from precis.utils.container_limits import container_limit_flags, container_user_flags
from precis.workers.job_types import struct_relax
from precis_bio.alphafold import build_fold_argv
from precis_chem.aizynth import build_aizynth_argv
from precis_chem.jobs import _build_normalizer_argv


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
