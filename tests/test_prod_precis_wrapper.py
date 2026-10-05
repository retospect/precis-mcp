"""The production CLI wrapper forwards configuration using synthetic files only."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "provided,file_url,expected",
    [
        (None, "https://fixture.invalid/v1", "https://fixture.invalid/v1"),
        (
            "https://override.invalid/v1",
            "https://fixture.invalid/v1",
            "https://override.invalid/v1",
        ),
        (None, None, "unset"),
    ],
)
def test_cloud_url_child_environment(
    tmp_path: Path, provided: str | None, file_url: str | None, expected: str
) -> None:
    secret_dir = tmp_path / "synthetic-home" / ".secrets" / "pw"
    secret_dir.mkdir(parents=True)
    (secret_dir / "PRECIS_DATABASE_URL").write_text(
        "postgresql://fixture@host.docker.internal:1/fixture\n"
    )
    if file_url is not None:
        (secret_dir / "PRECIS_LLM_BASE_URL").write_text(file_url + "\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "url=${PRECIS_LLM_BASE_URL-unset}" "dsn=${PRECIS_DATABASE_URL-unset}" "args=$*"\n'
    )
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(secret_dir.parents[1]),
        "PATH": str(fake_bin) + ":" + os.environ["PATH"],
    }
    env.pop("PRECIS_LLM_BASE_URL", None)
    if provided is not None:
        env["PRECIS_LLM_BASE_URL"] = provided
    wrapper = Path(__file__).resolve().parents[1] / "scripts" / "prod-precis"
    result = subprocess.run(
        ["bash", str(wrapper), "tools", "get", "--kind", "gripe", "--id", "fixture"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert f"url={expected}\n" in result.stdout
    assert "dsn=postgresql://fixture@127.0.0.1:1/fixture\n" in result.stdout
    assert "args=run precis tools get --kind gripe --id fixture" in result.stdout
    assert result.stderr == ""
    assert expected not in result.args
