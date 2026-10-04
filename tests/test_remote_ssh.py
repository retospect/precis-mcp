"""Bounded transport and config isolation, using synthetic local processes."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

from precis.remote.ssh import RemoteError, SshProfile, bounded_process


def profile() -> SshProfile:
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32
    return SshProfile(
        "fixture.invalid",
        8822,
        "fixture",
        ("ssh-ed25519 " + base64.b64encode(blob).decode(),),
        "synthetic",
    )


def test_profile_rejects_missing_or_invalid_pins() -> None:
    for keys in [(), ("ssh-ed25519 not-base64",), ("ssh-ed25519 eA==",)]:
        with pytest.raises(RemoteError):
            SshProfile("fixture.invalid", 22, "fixture", keys, "synthetic")
    with pytest.raises(RemoteError, match="profile_invalid"):
        SshProfile(
            "-oProxyCommand=bad", 22, "fixture", profile().host_keys, "synthetic"
        )


def test_transport_timeout_and_output_cap(tmp_path: Path) -> None:
    with pytest.raises(RemoteError, match="transport_timeout"):
        bounded_process(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            env={},
            scratch=tmp_path,
            timeout_s=0.05,
        )
    with pytest.raises(RemoteError, match="output_limit"):
        bounded_process(
            [sys.executable, "-c", "print('x'*10000)"],
            env={},
            scratch=tmp_path,
            timeout_s=3,
            max_output=10,
        )
    result = bounded_process(
        [
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read()); print('err',file=sys.stderr)",
        ],
        env={},
        scratch=tmp_path,
        timeout_s=3,
        input_data=b"payload",
    )
    assert (
        result.returncode == 0
        and b"payload" in result.stdout
        and b"err" in result.stderr
    )
    assert "payload" not in repr(result)


def test_sensitive_stdin_is_bounded_without_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.remote import ssh

    def no_file(*args: object, **kwargs: object) -> None:
        pytest.fail("Sensitive payload written to file")

    monkeypatch.setattr(ssh.tempfile, "TemporaryFile", no_file)
    result = bounded_process(
        [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"],
        env={},
        scratch=tmp_path,
        timeout_s=3,
        input_data=b"x" * 1000000,
        sensitive_input=True,
    )
    assert result.stdout.strip() == b"1000000"
    with pytest.raises(RemoteError, match="transport_timeout"):
        bounded_process(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            env={},
            scratch=tmp_path,
            timeout_s=0.05,
            input_data=b"x" * 1000000,
            sensitive_input=True,
        )
    assert not list(tmp_path.iterdir())
