"""DFT deployment must deliver optional run controls without enabling MPI by default."""

from pathlib import Path

import jinja2
import yaml

_ROOT = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "dft"


def _render(**overrides: object) -> dict[str, str]:
    values = yaml.safe_load(
        (_ROOT / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    values.update(dft_node="spark", ansible_managed="test")
    values.update(overrides)
    source = (_ROOT / "templates" / "dft.env.j2").read_text(encoding="utf-8")
    rendered = (
        jinja2.Environment(undefined=jinja2.StrictUndefined)
        .from_string(source)
        .render(values)
    )
    return dict(
        line.split("=", 1)
        for line in rendered.splitlines()
        if line and not line.startswith("#")
    )


def test_defaults_preserve_serial_runtime_and_fleet_binding() -> None:
    env = _render()
    assert env["PRECIS_DFT_MPI_RANKS"] == "0"
    assert env["PRECIS_DFT_OMP_THREADS"] == "4"
    assert "PRECIS_DFT_CPUSET" not in env
    assert "PRECIS_DFT_PAW_DIR" not in env


def test_host_overrides_reach_the_worker_environment() -> None:
    env = _render(
        dft_mpi_ranks=8,
        dft_omp_threads=1,
        dft_cpuset="10-17",
        dft_paw_dir="/var/lib/precis/gpaw-setups",
    )
    assert env["PRECIS_DFT_MPI_RANKS"] == "8"
    assert env["PRECIS_DFT_OMP_THREADS"] == "1"
    assert env["PRECIS_DFT_CPUSET"] == "10-17"
    assert env["PRECIS_DFT_PAW_DIR"] == "/var/lib/precis/gpaw-setups"
