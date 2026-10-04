"""Synthetic offline OpenSSH unlock, vault boundary and lifecycle checks."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from precis.remote import credentials
from precis.remote.credentials import CredentialRefs, vault_ssh_session
from precis.remote.ssh import RemoteError, SshProfile, bounded_process

PHRASE = "Synthetic-Test-Only-Passphrase42!"


@pytest.fixture
def short_scratch() -> Iterator[Path]:
    parent = Path.cwd() / ".scratch" / "ss"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s-", dir=parent) as directory:
        yield Path(directory)


@pytest.fixture
def key_material(tmp_path: Path) -> tuple[str, SshProfile]:
    path = tmp_path / "synthetic"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-a", "1", "-N", PHRASE, "-f", str(path)],
        check=True,
        capture_output=True,
    )
    public = " ".join(path.with_suffix(".pub").read_text().split()[:2])
    return path.read_text(), SshProfile(
        "fixture.invalid", 8822, "fixture", (public,), "synthetic"
    )


def test_real_agent_unlock_ttl_and_cleanup(
    short_scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    key_material: tuple[str, SshProfile],
    caplog: Any,
) -> None:
    key, profile = key_material
    monkeypatch.setattr(
        credentials,
        "require_vault_secret",
        lambda name, *, store: key if name == "KEY" else PHRASE,
    )
    root = short_scratch
    with vault_ssh_session(
        cast(Any, None),
        CredentialRefs("KEY", "PHRASE"),
        profile,
        scratch_root=root,
        identity_lifetime_s=1,
    ) as session:
        assert not (session.scratch / "encrypted").exists()
        assert (session.scratch.stat().st_mode & 0o777) == 0o700
        assert (session.scratch / "identity.pub").stat().st_mode & 0o777 == 0o600
        assert PHRASE not in repr(session._env) + repr(session._argv)
        listed = bounded_process(
            ["ssh-add", "-l"], env=session._env, scratch=session.scratch, timeout_s=3
        )
        assert listed.returncode == 0
        time.sleep(1.1)
        expired = bounded_process(
            ["ssh-add", "-l"], env=session._env, scratch=session.scratch, timeout_s=3
        )
        assert expired.returncode == 1
    assert list(root.iterdir()) == []
    assert PHRASE not in caplog.text and key not in caplog.text


@pytest.mark.parametrize("phrase", ["WRONG_SENTINEL", "", "line\nbreak"])
def test_failed_unlock_cleans_everything(
    short_scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    key_material: tuple[str, SshProfile],
    phrase: str,
) -> None:
    key, profile = key_material
    monkeypatch.setattr(
        credentials,
        "require_vault_secret",
        lambda name, *, store: key if name == "KEY" else phrase,
    )
    root = short_scratch
    with pytest.raises(RemoteError) as failure:
        with vault_ssh_session(
            cast(Any, None), CredentialRefs("KEY", "PHRASE"), profile, scratch_root=root
        ):
            pytest.fail("Invalid passphrase accepted")
    assert str(failure.value) in {
        "unlock_failed",
        "passphrase_invalid",
        "transport_timeout",
    }
    assert phrase not in str(failure.value) if phrase else True
    assert list(root.iterdir()) == []


def test_caller_exception_still_reaps_agent(
    short_scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    key_material: tuple[str, SshProfile],
) -> None:
    key, profile = key_material
    monkeypatch.setattr(
        credentials,
        "require_vault_secret",
        lambda name, *, store: key if name == "KEY" else PHRASE,
    )
    root = short_scratch
    with pytest.raises(RuntimeError, match="caller"):
        with vault_ssh_session(
            cast(Any, None), CredentialRefs("KEY", "PHRASE"), profile, scratch_root=root
        ) as session:
            socket_path = Path(session._env["SSH_AUTH_SOCK"])
            raise RuntimeError("caller")
    assert not socket_path.exists() and list(root.iterdir()) == []


def test_key_framing_and_crlf(key_material: tuple[str, SshProfile]) -> None:
    key, _ = key_material
    encrypted, public = credentials._encrypted_public_key(key.replace("\n", "\r\n"))
    assert b"\r" not in encrypted and public.startswith(b"ssh-ed25519 ")
    for bad in [
        "not-a-key",
        key + key,
        key.replace("BEGIN", "OTHER"),
        key[:80],
        "\x00" + key,
    ]:
        with pytest.raises(RemoteError, match="credential_format"):
            credentials._encrypted_public_key(bad)


def test_scratch_symlinks_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(actual)
    with pytest.raises(RemoteError, match="scratch_invalid"):
        credentials.reap_stale_scratch(link)
    assert actual.is_dir()


def test_stale_cleanup_respects_active_lock(tmp_path: Path) -> None:
    import fcntl

    root = tmp_path / "sessions"
    root.mkdir(mode=0o700)
    stale = root / "ssh-stale"
    stale.mkdir(mode=0o700)
    (stale / "lease").touch(mode=0o600)
    active = root / "ssh-active"
    active.mkdir(mode=0o700)
    fd = os.open(active / "lease", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert credentials.reap_stale_scratch(root) == 1
        assert active.exists() and not stale.exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize("unencrypted", [False, True])
def test_loopback_pins_and_auth(
    tmp_path: Path,
    short_scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    key_material: tuple[str, SshProfile],
    unencrypted: bool,
) -> None:
    """Real synthetic server: cryptographic pin and authentication acceptance."""
    import getpass
    import shutil
    import socket

    daemon = shutil.which("sshd") or "/usr/sbin/sshd"
    host_key = tmp_path / "host"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
        check=True,
        capture_output=True,
    )
    host_public = " ".join(host_key.with_suffix(".pub").read_text().split()[:2])
    key, client_profile = key_material
    refs = CredentialRefs("KEY", "PHRASE")
    if unencrypted:
        key_path = tmp_path / "unencrypted-client"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
            check=True,
            capture_output=True,
        )
        key = key_path.read_text()
        client_public = " ".join(key_path.with_suffix(".pub").read_text().split()[:2])
        client_profile = SshProfile(
            "fixture.invalid", 22, "fixture", (client_public,), "synthetic"
        )
        refs = CredentialRefs("KEY", encryption="unencrypted")
    authorized = tmp_path / "authorized_keys"
    authorized.write_text(client_profile.host_keys[0] + "\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = tmp_path / "sshd_config"
    config.write_text(
        f"ListenAddress 127.0.0.1\nPort {port}\nHostKey {host_key}\n"
        f"PidFile {tmp_path / 'sshd.pid'}\nAuthorizedKeysFile {authorized}\n"
        "StrictModes no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "UsePAM no\nPermitRootLogin yes\nLogLevel ERROR\n"
    )
    monkeypatch.setattr(
        credentials,
        "require_vault_secret",
        lambda name, *, store: key if name == "KEY" else PHRASE,
    )
    log_path = tmp_path / "sshd.log"
    with log_path.open("wb") as log:
        server = subprocess.Popen(
            [daemon, "-D", "-e", "-f", str(config)], stdout=log, stderr=log
        )
        try:
            deadline = time.monotonic() + 3
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        pytest.fail(
                            "Synthetic sshd gate unavailable: " + log_path.read_text()
                        )
                    time.sleep(0.02)
            good = SshProfile(
                "127.0.0.1", port, getpass.getuser(), (host_public,), "synthetic"
            )
            with vault_ssh_session(
                cast(Any, None),
                refs,
                good,
                scratch_root=short_scratch,
            ) as session:
                assert session.probe() == "authenticated"
            wrong = SshProfile(
                "127.0.0.1",
                port,
                getpass.getuser(),
                client_profile.host_keys,
                "synthetic",
            )
            with vault_ssh_session(
                cast(Any, None),
                refs,
                wrong,
                scratch_root=short_scratch,
            ) as session:
                with pytest.raises(RemoteError, match="pin_mismatch"):
                    session.probe()
        finally:
            server.terminate()
            server.wait(timeout=3)


def test_unencrypted_vault_load_never_writes_private_key(
    short_scratch: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: Any,
) -> None:
    path = tmp_path / "synthetic-unencrypted"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    key = path.read_text()
    public = " ".join(path.with_suffix(".pub").read_text().split()[:2])
    calls = []

    def reveal(name: str, *, store: Any) -> str:
        calls.append(name)
        return key

    monkeypatch.setattr(credentials, "require_vault_secret", reveal)
    from precis.remote import ssh

    def no_tempfile(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Sensitive input used a file")

    monkeypatch.setattr(ssh.tempfile, "TemporaryFile", no_tempfile)
    profile = SshProfile("fixture.invalid", 22, "fixture", (public,), "synthetic")
    with vault_ssh_session(
        cast(Any, None),
        CredentialRefs("KEY", encryption="unencrypted"),
        profile,
        scratch_root=short_scratch,
        identity_lifetime_s=1,
    ) as session:
        assert calls == ["KEY"]
        assert not (session.scratch / "encrypted").exists()
        assert not (session.scratch / "askpass").exists()
        assert key not in repr(session._env) + repr(session._argv)
        for file in session.scratch.iterdir():
            if file.is_file():
                assert key.encode() not in file.read_bytes()
        listed = subprocess.run(
            ["ssh-add", "-l"], env=session._env, capture_output=True, timeout=3
        )
        assert listed.returncode == 0
        time.sleep(1.1)
        listed = subprocess.run(
            ["ssh-add", "-l"], env=session._env, capture_output=True, timeout=3
        )
        assert listed.returncode == 1
    assert not list(short_scratch.iterdir())
    assert key not in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nPRIVATE_SENTINEL\n-----END PGP PRIVATE KEY BLOCK-----",
        "-----BEGIN RSA PRIVATE KEY-----\nPRIVATE_SENTINEL\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_unsupported_format_is_redacted_before_agent(
    short_scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    key_material: tuple[str, SshProfile],
    value: str,
    caplog: Any,
) -> None:
    monkeypatch.setattr(
        credentials, "require_vault_secret", lambda name, *, store: value
    )

    def never_agent(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Unsupported format started agent")

    monkeypatch.setattr(credentials.subprocess, "Popen", never_agent)
    with pytest.raises(RemoteError, match="credential_format") as error:
        with vault_ssh_session(
            cast(Any, None),
            CredentialRefs("KEY", encryption="unencrypted"),
            key_material[1],
            scratch_root=short_scratch,
        ):
            pytest.fail("Unsupported format accepted")
    assert "PRIVATE_SENTINEL" not in caplog.text + str(error.value)
    assert not list(short_scratch.iterdir())


def test_encrypted_key_does_not_silently_become_no_passphrase(
    key_material: tuple[str, SshProfile],
) -> None:
    with pytest.raises(RemoteError, match="credential_format"):
        credentials._key_envelope(key_material[0], encrypted=False)
