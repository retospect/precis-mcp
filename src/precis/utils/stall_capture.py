"""Automatic bounded packet capture when a fetch stalls on connect (gr465931).

The fetcher host intermittently stalls the TLS handshake to single IPs of
``api.figshare.com`` / ``api.crossref.org`` / ``doi.org``. To tell a LAN/ISP
path problem from endpoint flapping we need a tcpdump *during* a stall; this
module takes one for us.

Hooked at the caller/exception boundary (``retry_transient``, si_discovery's
``_safe_fetch``, bib_parse's Crossref loop) — never inside ``safe_fetch``.

* **Off unless** ``PRECIS_STALL_CAPTURE_DIR`` names a directory.
* Only for the three watched hosts and only for connect-phase timeouts.
* Rate-limited per host (``MAX_PER_DAY`` per rolling 24 h, ``MIN_GAP_S``
  apart), state in ``<dir>/.stall-capture-state.json`` under an flock so
  sibling worker processes share the budget.
* The capture is a detached ``tcpdump`` (argument list, no shell): ~30 s,
  ``-c`` packet cap and 128-byte snaplen keep a file under ~10 MB.
* Missing ``tcpdump`` or missing capability degrades to a log line.

Deploy-side capability: ``docs/runbooks/stall-capture.md``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ENV_DIR = "PRECIS_STALL_CAPTURE_DIR"
ENV_IFACE = "PRECIS_STALL_CAPTURE_IFACE"
WATCHED_HOSTS = frozenset({"api.figshare.com", "api.crossref.org", "doi.org"})
MAX_PER_DAY = 4
MIN_GAP_S = 3600.0
CAPTURE_SECONDS = 30
MAX_PACKETS = 50000  # x (128 B snaplen + 16 B record header) < 10 MB
SNAPLEN = 128
_STATE_NAME = ".stall-capture-state.json"


def _is_connect_timeout(exc: BaseException) -> bool:
    """True if ``exc`` (or a chained cause) is a connect/TLS-handshake timeout."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ == "ConnectTimeout":
            return True
        if "handshake operation timed out" in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _host_of(host_or_url: str) -> str:
    h = urlparse(host_or_url).netloc if "//" in host_or_url else host_or_url
    return h.split(":")[0].lower()


def allow(state_path: Path, host: str, now: float) -> bool:
    """Rate-limit gate: True (and records ``now``) if a capture for ``host``
    is allowed — under ``MAX_PER_DAY`` in the last 24 h and ``MIN_GAP_S``
    after the previous one."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            state = json.loads(fh.read() or "{}")
        except ValueError:
            state = {}
        times = [t for t in state.get(host, []) if now - t < 86400.0]
        if len(times) >= MAX_PER_DAY or (times and now - max(times) < MIN_GAP_S):
            return False
        times.append(now)
        state[host] = times
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state))
        return True


def build_command(
    tcpdump: str, pcap: Path, addrs: list[str], iface: str = "any"
) -> list[str]:
    """The tcpdump argument list (never run through a shell)."""
    hosts = " or ".join(f"host {a}" for a in addrs)
    return [
        tcpdump, "-n", "-i", iface, "-s", str(SNAPLEN),
        "-c", str(MAX_PACKETS),
        "-G", str(CAPTURE_SECONDS), "-W", "1",
        "-w", str(pcap),
        f"tcp port 443 and ({hosts})",
    ]  # fmt: skip


def _reap(proc: subprocess.Popen, pcap: Path, errfile: Path) -> None:
    rc = proc.wait()
    if rc != 0:
        try:
            msg = errfile.read_text(encoding="utf-8", errors="replace").strip()[-300:]
        except OSError:
            msg = ""
        log.warning("stall_capture: tcpdump exited %s for %s: %s", rc, pcap, msg)
        return
    log.info("stall_capture: finished %s", pcap)
    try:
        errfile.unlink()
    except OSError:
        pass


def maybe_capture(
    exc: BaseException, host_or_url: str, *, now: float | None = None
) -> Path | None:
    """If enabled, ``exc`` is a connect timeout, ``host_or_url`` is a watched
    host and the rate limit allows: start a detached tcpdump and return the
    pcap path. Never raises, never blocks."""
    try:
        cap_dir = os.environ.get(ENV_DIR, "").strip()
        if not cap_dir:
            return None
        host = _host_of(host_or_url)
        if host not in WATCHED_HOSTS or not _is_connect_timeout(exc):
            return None
        tcpdump = shutil.which("tcpdump")
        if tcpdump is None:
            log.warning("stall_capture: tcpdump not installed; skipping %s", host)
            return None
        t = time.time() if now is None else now
        out = Path(cap_dir)
        if not allow(out / _STATE_NAME, host, t):
            return None
        addrs = sorted(
            {
                str(ai[4][0])
                for ai in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            }
        )
        stamp = datetime.fromtimestamp(t, UTC).strftime("%Y%m%dT%H%M%SZ")
        pcap = out / f"stall-{host}-{stamp}.pcap"
        cmd = build_command(
            tcpdump, pcap, addrs, os.environ.get(ENV_IFACE, "").strip() or "any"
        )
        errfile = pcap.with_suffix(".err")
        with open(errfile, "wb") as ef:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=ef,
                start_new_session=True,
            )
        log.warning(
            "stall_capture: %s on %s; capturing %ds to %s (addrs %s)",
            type(exc).__name__, host, CAPTURE_SECONDS, pcap, ",".join(addrs),
        )  # fmt: skip
        threading.Thread(target=_reap, args=(proc, pcap, errfile), daemon=True).start()
        return pcap
    except Exception as err:  # capture must never break a fetch
        log.warning("stall_capture: skipped for this stall: %r", err)
        return None
