"""safe_fetch boundary probe (td574934 A4, gr596928).

The SSRF guard (:mod:`precis.utils.safe_fetch`) is the boundary every
agent-supplied URL crosses. The probe asserts it still refuses:

* private / loopback / link-local / IPv6-mapped literal targets
  (:func:`resolve_pinned_ip` — the standalone pre-check);
* a hostname resolving to a private record (the DNS-rebinding shape,
  with ``getaddrinfo`` stubbed so the probe needs no network);
* a redirect to an internal target — the first hop is served by a stub
  ``client.send``; the second goes through the REAL pinning backend,
  which must refuse the link-local literal at connect;
* an unguarded client (no pinning transport) — ``safe_get`` must refuse
  it rather than fetch unguarded.

Contract: exit 0 pass, 1 boundary breach (stdout = the finding), else
cannot-run (exit 2 here). ``argv[1]`` names the tree root (default: cwd).
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

_LITERALS: tuple[tuple[str, str], ...] = (
    ("private", "http://10.9.9.9/"),
    ("private", "http://172.16.9.9/"),
    ("loopback", "http://127.0.0.1/"),
    ("link-local", "http://169.254.169.254/latest/meta-data"),
    ("v6 loopback", "http://[::1]/"),
    ("v6 link-local", "http://[fe80::1]/"),
    ("v6 unique-local", "http://[fc00::1]/"),
    ("v4-mapped private", "http://[::ffff:10.0.0.1]/"),
    ("v4-mapped loopback", "http://[::ffff:127.0.0.1]/"),
)


def _fake_gai(ip: str):
    def _gai(host: str, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _gai


def check(root: Path) -> list[str]:
    """Every finding; empty = pass."""
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    import httpx

    from precis.utils import safe_fetch as sf

    findings: list[str] = []

    for label, url in _LITERALS:
        try:
            sf.resolve_pinned_ip(url)
        except sf.SsrfBlocked:
            continue
        except Exception as exc:
            findings.append(
                f"{label} target {url} raised {type(exc).__name__}: {exc} "
                "(expected SsrfBlocked)"
            )
        else:
            findings.append(f"{label} target not refused: {url}")

    # A hostname resolving to a private record — the rebinding shape.
    real_gai = socket.getaddrinfo
    try:
        socket.getaddrinfo = _fake_gai("10.9.9.9")
        try:
            sf.resolve_pinned_ip("http://probe.internal/")
        except sf.SsrfBlocked:
            pass
        else:
            findings.append("hostname resolving to a private record was not refused")
        # …and a public record still classifies (no fetch — resolve only).
        socket.getaddrinfo = _fake_gai("93.184.216.34")
        try:
            pinned = sf.resolve_pinned_ip("http://probe.public/")
        except Exception as exc:
            findings.append(
                f"a public hostname was refused: {type(exc).__name__}: {exc}"
            )
        else:
            if pinned != "93.184.216.34":
                findings.append(
                    f"a public hostname pinned to {pinned!r}, not the resolved record"
                )
    finally:
        socket.getaddrinfo = real_gai

    # Redirect to an internal target: the first hop is a canned 302 (no
    # network); the second goes through the real pinning backend, which
    # must refuse the link-local literal at connect.
    try:
        client = httpx.Client(
            transport=sf.pinning_transport(), follow_redirects=False, timeout=5.0
        )
    except Exception as exc:
        findings.append(f"cannot build a pinning client: {exc}")
        client = None
    if client is not None:
        try:
            real_send = client.send

            def _fake_send(request, **kw):
                if request.url.host == "probe.test":
                    return httpx.Response(
                        302,
                        headers={"location": "http://169.254.169.254/x"},
                        request=request,
                    )
                return real_send(request, **kw)

            client.send = _fake_send  # type: ignore[method-assign]
            try:
                sf.safe_get(client, "http://probe.test/start")
            except sf.SsrfBlocked:
                pass
            except Exception as exc:
                findings.append(
                    f"a redirect to a link-local target raised "
                    f"{type(exc).__name__}: {exc} (expected SsrfBlocked)"
                )
            else:
                findings.append("safe_get followed a redirect to a link-local target")
            # An unguarded client must be refused outright.
            plain = httpx.Client()
            try:
                sf.safe_get(plain, "http://93.184.216.34/")
            except sf.SsrfBlocked:
                pass
            except Exception as exc:
                findings.append(
                    f"an unguarded client raised {type(exc).__name__}: {exc} "
                    "(expected SsrfBlocked)"
                )
            else:
                findings.append("safe_get accepted an unguarded client")
            finally:
                plain.close()
        finally:
            client.close()

    return findings


def main(argv: list[str] | None = None) -> int:
    """Probe entry: ``[tree_root]``; 0 pass, 1 breach, 2 cannot-run."""
    argv = list(sys.argv if argv is None else argv)
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    try:
        findings = check(root)
    except Exception as exc:
        print(f"cannot-run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
