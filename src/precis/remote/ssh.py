"""Bounded OpenSSH transport; independent of vault and workload code."""

from __future__ import annotations

import base64
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


class RemoteError(RuntimeError):
    """Allowlisted failure code, never command output or private details."""


@dataclass(frozen=True)
class SshProfile:
    host: str
    port: int
    user: str
    host_keys: tuple[str, ...]
    pin_provenance: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", self.host):
            raise RemoteError("profile_invalid")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", self.user):
            raise RemoteError("profile_invalid")
        if not 1 <= self.port <= 65535 or not self.host_keys or not self.pin_provenance:
            raise RemoteError("pin_missing")
        for key in self.host_keys:
            parts = key.split()
            try:
                if len(parts) != 2 or parts[0] != "ssh-ed25519":
                    raise ValueError
                blob = base64.b64decode(parts[1], validate=True)
                if (
                    len(blob) != 51
                    or blob[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
                ):
                    raise ValueError
            except ValueError:
                raise RemoteError("pin_invalid") from None


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)


def bounded_process(
    argv: Sequence[str],
    *,
    env: dict[str, str],
    scratch: Path,
    timeout_s: float,
    input_data: bytes = b"",
    max_output: int = 16 * 1024 * 1024,
    sensitive_input: bool = False,
) -> CommandResult:
    """Drain both pipes with a combined byte cap; kill and reap on all failures."""
    if timeout_s <= 0 or max_output < 1:
        raise RemoteError("limits_invalid")
    with ExitStack() as stack:
        incoming: int | BinaryIO
        if sensitive_input:
            incoming = subprocess.PIPE
        else:
            input_file = stack.enter_context(tempfile.TemporaryFile(dir=scratch))
            input_file.write(input_data)
            input_file.seek(0)
            incoming = input_file
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=incoming,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError:
            raise RemoteError("transport_unavailable") from None
        output = [bytearray(), bytearray()]
        deadline = time.monotonic() + timeout_s
        try:
            with selectors.DefaultSelector() as selector:
                assert proc.stdout is not None and proc.stderr is not None
                selector.register(proc.stdout, selectors.EVENT_READ, 0)
                selector.register(proc.stderr, selectors.EVENT_READ, 1)
                offset = 0
                if sensitive_input:
                    assert proc.stdin is not None
                    os.set_blocking(proc.stdin.fileno(), False)
                    if input_data:
                        selector.register(proc.stdin, selectors.EVENT_WRITE, 2)
                    else:
                        proc.stdin.close()
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RemoteError("transport_timeout")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        if key.data == 2:
                            try:
                                offset += os.write(
                                    key.fd, input_data[offset : offset + 65536]
                                )
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                offset = len(input_data)
                            if offset == len(input_data):
                                selector.unregister(key.fileobj)
                                assert proc.stdin is not None
                                proc.stdin.close()
                            continue
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        output[key.data].extend(data)
                        if sum(map(len, output)) > max_output:
                            raise RemoteError("output_limit")
            proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            return CommandResult(proc.returncode, bytes(output[0]), bytes(output[1]))
        except subprocess.TimeoutExpired:
            raise RemoteError("transport_timeout") from None
        finally:
            # A leader can exit while descendants retain the output pipes.
            # The session owns the whole group, not just its leader PID.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


class SshSession:
    """An injected session with no private-key/passphrase attributes."""

    def __init__(
        self,
        profile: SshProfile,
        *,
        agent_socket: Path,
        identity: Path,
        known_hosts: Path,
        scratch: Path,
        env: dict[str, str],
        ssh_binary: str = "ssh",
    ) -> None:
        self.profile = profile
        self.scratch = scratch
        self._env = dict(env)
        self._argv = [
            ssh_binary,
            "-F",
            "/dev/null",
            "-p",
            str(profile.port),
            "-l",
            profile.user,
            "-o",
            f"IdentityAgent={agent_socket}",
            "-i",
            str(identity),
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            "VerifyHostKeyDNS=no",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPersist=no",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ClearAllForwardings=yes",
            "-T",
            profile.host,
        ]

    def run(
        self,
        remote_argv: Sequence[str],
        *,
        timeout_s: float = 30,
        input_data: bytes = b"",
        max_output: int = 16 * 1024 * 1024,
    ) -> CommandResult:
        import shlex

        if not remote_argv or any("\x00" in part for part in remote_argv):
            raise RemoteError("command_invalid")
        return bounded_process(
            [*self._argv, shlex.join(remote_argv)],
            env=self._env,
            scratch=self.scratch,
            timeout_s=timeout_s,
            input_data=input_data,
            max_output=max_output,
        )

    def probe(self) -> str:
        """Explicit read-only authentication; exposes only classified status."""
        result = self.run(["true"])
        if result.returncode == 0:
            return "authenticated"
        if (
            b"Host key verification failed" in result.stderr
            or b"HOST IDENTIFICATION" in result.stderr
        ):
            raise RemoteError("pin_mismatch")
        if b"Permission denied" in result.stderr:
            raise RemoteError("auth_failed")
        raise RemoteError("transport_unavailable")
