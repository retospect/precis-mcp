"""Precis-only audited vault→short-lived OpenSSH session bridge."""

from __future__ import annotations

import base64
import fcntl
import os
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from precis.secrets import require_vault_secret

from .ssh import RemoteError, SshProfile, SshSession, bounded_process

if TYPE_CHECKING:
    from precis.store import Store


@dataclass(frozen=True)
class CredentialRefs:
    key_ref: str
    passphrase_ref: str


def _encrypted_public_key(value: str) -> tuple[bytes, bytes]:
    """Validate framing only; all cryptographic operations remain OpenSSH's."""
    try:
        normalized = value.replace("\r\n", "\n").strip()
        lines = normalized.splitlines()
        if len(normalized) > 65536 or "\x00" in normalized or len(lines) < 3:
            raise ValueError
        if (
            lines[0] != "-----BEGIN OPENSSH PRIVATE KEY-----"
            or lines[-1] != "-----END OPENSSH PRIVATE KEY-----"
        ):
            raise ValueError
        blob = base64.b64decode("".join(lines[1:-1]), validate=True)
        if not blob.startswith(b"openssh-key-v1\x00"):
            raise ValueError
        cursor = 15

        def field() -> bytes:
            nonlocal cursor
            length = struct.unpack_from(">I", blob, cursor)[0]
            cursor += 4
            if length > len(blob) - cursor:
                raise ValueError
            answer = blob[cursor : cursor + length]
            cursor += length
            return answer

        cipher, kdf, options = field(), field(), field()
        if cipher != b"aes256-ctr" or kdf != b"bcrypt" or not options:
            raise ValueError
        count = struct.unpack_from(">I", blob, cursor)[0]
        cursor += 4
        if count != 1:
            raise ValueError
        public = field()
        encrypted = field()
        if cursor != len(blob) or not encrypted or len(public) != 51:
            raise ValueError
        if public[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
            raise ValueError
        return (normalized + "\n").encode("ascii"), b"ssh-ed25519 " + base64.b64encode(
            public
        ) + b"\n"
    except (ValueError, struct.error, UnicodeError):
        raise RemoteError("credential_format") from None


def _write_private(path: Path, content: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as out:
        out.write(content)


def _scratch_root(root: Path) -> Path:
    root = root.absolute()
    # OpenSSH expands percent/dollar tokens and parses path lists itself.
    if any(c in str(root) for c in "%$\n\r\t "):
        raise RemoteError("scratch_invalid")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if (
        root.is_symlink()
        or root.resolve() != root
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RemoteError("scratch_invalid")
    return root


def reap_stale_scratch(root: Path) -> int:
    """Reclaim only owned directories with an unlocked consumer lease; no PID kill."""
    root = _scratch_root(root)
    reaped = 0
    for child in root.glob("ssh-*"):
        if (
            child.is_symlink()
            or not child.is_dir()
            or child.stat().st_uid != os.getuid()
        ):
            continue
        try:
            fd = os.open(child / "lease", os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        try:
            shutil.rmtree(child)
            reaped += 1
        finally:
            os.close(fd)
    return reaped


@contextmanager
def vault_ssh_session(
    store: Store,
    refs: CredentialRefs,
    profile: SshProfile,
    *,
    scratch_root: Path,
    identity_lifetime_s: int = 60,
) -> Iterator[SshSession]:
    """Resolve only after pins/profile validation; no network until caller runs."""
    if (
        not 1 <= identity_lifetime_s <= 60
        or not refs.key_ref
        or not refs.passphrase_ref
    ):
        raise RemoteError("credential_refs_invalid")
    root = _scratch_root(scratch_root)
    reap_stale_scratch(root)
    encrypted, public = _encrypted_public_key(
        require_vault_secret(refs.key_ref, store=store)
    )
    phrase = require_vault_secret(refs.passphrase_ref, store=store)
    if (
        not phrase
        or len(phrase.encode()) > 4095
        or any(c in phrase for c in "\n\r\x00")
    ):
        raise RemoteError("passphrase_invalid")
    session_dir = Path(tempfile.mkdtemp(prefix="ssh-", dir=root))
    agent: subprocess.Popen[bytes] | None = None
    broker: socket.socket | None = None
    thread: threading.Thread | None = None
    lease: int | None = None
    stop = threading.Event()
    try:
        lease = os.open(
            session_dir / "lease", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
        )
        fcntl.flock(lease, fcntl.LOCK_EX)
        agent_socket = session_dir / "a"
        broker_socket = session_dir / "b"
        if len(os.fsencode(broker_socket)) > 100:
            raise RemoteError("scratch_invalid")
        key_path, identity, known_hosts = (
            session_dir / n for n in ("encrypted", "identity.pub", "known_hosts")
        )
        _write_private(key_path, encrypted)
        _write_private(identity, public)
        _write_private(
            known_hosts,
            "".join(
                f"[{profile.host}]:{profile.port} {key}\n" for key in profile.host_keys
            ).encode(),
        )
        askpass = session_dir / "askpass"
        _write_private(
            askpass,
            (
                "#!/bin/sh\nexec "
                + shlex.quote(sys.executable)
                + " "
                + shlex.quote(str(Path(__file__).with_name("_askpass.py")))
                + ' "$@"\n'
            ).encode(),
            0o700,
        )
        env = {"PATH": "/usr/bin:/bin", "LANG": "C", "SSH_AUTH_SOCK": str(agent_socket)}
        try:
            agent = subprocess.Popen(
                ["ssh-agent", "-D", "-a", str(agent_socket)],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            raise RemoteError("agent_unavailable") from None
        deadline = time.monotonic() + 5
        while not agent_socket.exists():
            if agent.poll() is not None or time.monotonic() >= deadline:
                raise RemoteError("agent_unavailable")
            time.sleep(0.01)
        broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        broker.bind(str(broker_socket))
        os.chmod(broker_socket, 0o600)
        broker.listen(1)
        broker.settimeout(0.1)

        def answer_once() -> None:
            assert broker is not None
            while not stop.is_set():
                try:
                    conn, _ = broker.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with conn:
                    conn.settimeout(1)
                    try:
                        conn.sendall(phrase.encode() + b"\n")
                    except OSError:
                        pass
                return

        thread = threading.Thread(target=answer_once, daemon=True)
        thread.start()
        unlock_env = {
            **env,
            "SSH_ASKPASS": str(askpass),
            "SSH_ASKPASS_REQUIRE": "force",
            "PRECIS_ASKPASS_SOCKET": str(broker_socket),
        }
        result = bounded_process(
            ["ssh-add", "-q", "-t", str(identity_lifetime_s), str(key_path)],
            env=unlock_env,
            scratch=session_dir,
            timeout_s=8,
            max_output=8192,
        )
        if result.returncode:
            raise RemoteError("unlock_failed")
        stop.set()
        broker.close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise RemoteError("cleanup_failed")
        key_path.unlink()
        phrase = ""
        del encrypted, unlock_env, result
        yield SshSession(
            profile,
            agent_socket=agent_socket,
            identity=identity,
            known_hosts=known_hosts,
            scratch=session_dir,
            env=env,
        )
    finally:
        stop.set()
        if broker:
            broker.close()
        if thread:
            thread.join(timeout=2)
        if agent:
            if agent.poll() is None:
                agent.terminate()
                try:
                    agent.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    agent.kill()
            agent.wait()
        if lease is not None:
            os.close(lease)
        shutil.rmtree(session_dir, ignore_errors=True)
