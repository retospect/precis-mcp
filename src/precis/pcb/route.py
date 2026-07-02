"""Rent the autorouter — Freerouting headless (ADR 0042 §6, §9, §13a).

We **own** the netlist + part-selection + placement IR and **rent** copper
routing. The v1 router is Freerouting run headless: a Specctra ``.dsn`` (from
:func:`precis.pcb.export.specctra_dsn`) goes in, a routed ``.ses`` session
comes out, to be back-imported into KiCad → gerbers for JLCPCB.

This module mirrors :mod:`precis.export.compile` (the LaTeX Tier-B wrapper): a
thin, deterministic, bounded subprocess that **returns a result instead of
raising**, gated on the binary so a host without it degrades cleanly (emit the
``.dsn``, skip the ``.ses``) rather than crashing. The binary is found via
``PRECIS_FREEROUTING_JAR`` (a path to ``freerouting.jar``) or
``PRECIS_FREEROUTING_BIN`` (a wrapper/stub on PATH — tests inject a stub like
``PRECIS_CLAUDE_BIN``); ``java`` is assumed on PATH for the jar form.

:func:`place_route_round_trip` is the §9 hand-off: place → ``.dsn`` → route →
on an incomplete route, re-place (more annealing iters / a fresh seed) and
re-route, bounded by ``max_passes``. The placer minimises the *crossing*
objective the router ultimately pays for, so a re-place is a real second
chance, not a coin flip.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _jar() -> str | None:
    return os.environ.get("PRECIS_FREEROUTING_JAR")


def _bin() -> str | None:
    return os.environ.get("PRECIS_FREEROUTING_BIN")


def have_freerouting() -> bool:
    """True when a Freerouting backend is resolvable — a stub/wrapper on
    ``PRECIS_FREEROUTING_BIN``, or a ``PRECIS_FREEROUTING_JAR`` + ``java``."""
    b = _bin()
    if b and shutil.which(b):
        return True
    jar = _jar()
    return bool(jar and Path(jar).exists() and shutil.which("java"))


@dataclass
class RouteResult:
    """Outcome of one Freerouting pass."""

    ok: bool
    ses: Path | None
    returncode: int
    log_tail: str
    unrouted: int | None = None  # nets/airwires Freerouting could not complete
    skipped: bool = False  # no backend → not attempted


def _cmd(dsn: Path, ses: Path) -> list[str]:
    b = _bin()
    if b and shutil.which(b):
        return [b, "-de", str(dsn), "-do", str(ses)]
    jar = _jar()
    # Freerouting 1.x headless batch CLI: -de design in, -do session out,
    # -mp 0 = route to completion then stop. -Djava.awt.headless=true keeps the
    # 1.9.0 AWT classes from needing a display on the daemon hosts. (2.x reworked
    # this command line — the ansible precis_eda role pins 1.9.0 to match.)
    return [
        "java",
        "-Djava.awt.headless=true",
        "-jar",
        str(jar),
        "-de",
        str(dsn),
        "-do",
        str(ses),
        "-mp",
        "0",
    ]


_UNROUTED_RE = re.compile(r"(\d+)\s+(?:unrouted|incomplete|open)", re.IGNORECASE)


def _parse_unrouted(text: str) -> int | None:
    """Best-effort pull of the unrouted-connection count from Freerouting's
    stdout (it prints e.g. ``0 incomplete connections``)."""
    last = None
    for m in _UNROUTED_RE.finditer(text or ""):
        last = int(m.group(1))
    return last


def route_dsn(
    dsn: Path | str, *, ses: Path | str | None = None, timeout_s: int | None = None
) -> RouteResult:
    """Route a Specctra ``.dsn`` to a ``.ses`` session via Freerouting headless.

    Never raises on a routing failure (that's ``ok=False`` + the log tail);
    ``skipped=True`` when no backend is installed. Bounded by a wall-clock cap
    (``PRECIS_FREEROUTING_TIMEOUT_S``, default 300s)."""
    dsn = Path(dsn)
    out = Path(ses) if ses else dsn.with_suffix(".ses")
    if not have_freerouting():
        log.warning("route_dsn: no Freerouting backend; skipping (.dsn emitted)")
        return RouteResult(
            ok=False,
            ses=None,
            returncode=-1,
            log_tail="Freerouting not installed (set PRECIS_FREEROUTING_JAR)",
            skipped=True,
        )
    if timeout_s is None:
        timeout_s = int(os.environ.get("PRECIS_FREEROUTING_TIMEOUT_S", "300"))
    cmd = _cmd(dsn, out)
    log.info("route_dsn: %s (timeout=%ds)", " ".join(cmd), timeout_s)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return RouteResult(
            ok=False,
            ses=None,
            returncode=-1,
            log_tail=f"Freerouting timed out after {timeout_s}s",
        )
    blob = (proc.stdout or "") + (proc.stderr or "")
    unrouted = _parse_unrouted(blob)
    ok = proc.returncode == 0 and out.exists() and (unrouted in (None, 0))
    return RouteResult(
        ok=ok,
        ses=out if out.exists() else None,
        returncode=proc.returncode,
        log_tail=blob[-2000:],
        unrouted=unrouted,
    )


@dataclass
class RoundTripResult:
    """Outcome of the bounded place↔route loop (ADR 0042 §9)."""

    ok: bool
    passes: int
    dsn: Path
    route: RouteResult
    history: list[dict[str, Any]]


def place_route_round_trip(
    model_fn: Callable[[], dict[str, Any]],
    place_fn: Callable[[int, int], dict[str, Any]],
    dsn_fn: Callable[[dict[str, Any]], str],
    out_dir: Path | str,
    *,
    max_passes: int = 3,
    base_iters: int = 1500,
    name: str = "design",
) -> RoundTripResult:
    """The §9 place↔route hand-off, bounded.

    Each pass: ``place_fn(iters, seed)`` re-places (the placer minimises the
    crossing objective the router pays for), ``model_fn()`` reloads the placed
    IR, ``dsn_fn(model)`` writes the ``.dsn``, then Freerouting routes it. On an
    incomplete route we escalate (more iters, a fresh seed) and retry, up to
    ``max_passes``. Returns the best/last attempt. Degrades to a single
    ``.dsn``-only pass when no router is installed."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # The .dsn write lives inside the pass loop; zero passes would return a
    # dsn path that was never written. Callers can't ask for less than one.
    max_passes = max(1, int(max_passes))
    history: list[dict[str, Any]] = []
    last_route = RouteResult(
        ok=False, ses=None, returncode=-1, log_tail="not attempted", skipped=True
    )
    dsn_path = out_dir / f"{name}.dsn"
    for p in range(max_passes):
        iters = base_iters * (p + 1)
        placed = place_fn(iters, p)
        model = model_fn()
        dsn_path = out_dir / f"{name}.dsn"
        dsn_path.write_text(dsn_fn(model))
        route = route_dsn(dsn_path, ses=out_dir / f"{name}.ses")
        last_route = route
        history.append(
            {
                "pass": p,
                "iters": iters,
                "seed": p,
                "crossings_after": placed.get("crossings_after"),
                "routed_ok": route.ok,
                "unrouted": route.unrouted,
                "skipped": route.skipped,
            }
        )
        if route.ok or route.skipped:
            break  # done, or no router to iterate against
    return RoundTripResult(
        ok=last_route.ok,
        passes=len(history),
        dsn=dsn_path,
        route=last_route,
        history=history,
    )


__all__ = [
    "RoundTripResult",
    "RouteResult",
    "have_freerouting",
    "place_route_round_trip",
    "route_dsn",
]
