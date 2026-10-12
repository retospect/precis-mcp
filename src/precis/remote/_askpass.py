"""Internal one-shot OpenSSH askpass helper; no vault or logging imports."""

from __future__ import annotations

import os
import socket
import sys


def main() -> int:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(os.environ["PRECIS_ASKPASS_SOCKET"])
            chunks = bytearray()
            while data := client.recv(4096):
                chunks.extend(data)
                if len(chunks) > 4096:
                    return 1
        if not chunks:
            return 1
        sys.stdout.buffer.write(chunks)
        sys.stdout.buffer.flush()
        return 0
    except (OSError, KeyError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
