"""Concurrency load harness for the precis MCP surface.

Fans N concurrent MCP clients at a live ``precis serve`` and ramps N until
something degrades, recording the two facts that tell the walls apart.

Two independent walls present identically as "calls hang" (see
``docs/backlog/mcp-concurrency-load-test.md``):

1. the anyio worker pool — verbs are sync callables, so FastMCP runs each on
   a worker thread; enough concurrent thread-parking calls starve the pool
   and *every* call queues, including ones that touch nothing;
2. Postgres connections — the dev DB has no pgbouncer in front of it.

The discriminator is a **canary**: ``get(kind='skill')`` is served from files
under ``src/precis/data/skills/`` and touches neither the DB nor the
embedder. If canary latency climbs in lockstep with load, the queue is for a
*thread*. If the canary stays flat while DB-backed verbs slow, the queue is
for a *connection*. The canary rides the same server as everything else, so
it sees the thread pool; the connection sampler talks to Postgres directly,
so it does not. Neither measurement can mask the other.

Two arms beyond the single-process ramp:

* ``--servers K`` starts K ``precis serve`` processes and spreads the workers
  across them round-robin. Client-side spreading stands in for a balancer;
  it is enough to show whether throughput scales with processes, which is
  the claim the GIL verdict makes.
* ``--url URL`` targets an already-running server instead of starting one.
  The harness cannot see that server's CPU or its Postgres, so the verdict
  reports the canary curve and throughput and leaves GIL-versus-pool to a
  host-side reading. This is the mode for Reto's prod run; it refuses to
  start without ``--live-target`` so it cannot load a live server by
  accident.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Concurrency steps. Ends well past the ~24 sessions the serving plan wants
# so the curve shows a plateau rather than stopping at the target.
DEFAULT_RAMP = [1, 2, 4, 8, 12, 16, 24, 32]

# The verb mix. Each entry is (label, tool, args, parks_thread) — the flag
# records the *prediction*, so the report can say whether it held.
VERB_MIX: list[tuple[str, str, dict[str, Any], bool]] = [
    # Canary: file-backed, no DB, no embedder. Any latency it gains is
    # queueing for a worker thread.
    ("canary:skill", "get", {"kind": "skill", "id": "precis-overview"}, False),
    # DB-only: a lexical search does no embedding.
    ("search:lexical", "search", {"q": "fullerene", "mode": "lexical", "k": 5}, False),
    # DB + embedder: the predicted thread-parker.
    ("search:semantic", "search", {"q": "carbon nanotube junction", "k": 5}, True),
    # DB-only, kind-scoped lexical search: a different handler path from the
    # cross-kind fan-out above. (The old get(kind='paper', view='list') call
    # became BadInput when bare-q get turned into a search.)
    (
        "search:paper",
        "search",
        {"kind": "paper", "q": "carbon", "mode": "lexical", "k": 5},
        False,
    ),
]

CANARY = VERB_MIX[0]


@dataclass
class Call:
    step: int
    label: str
    ms: float
    error: str | None


@dataclass
class Sample:
    step: int
    t: float
    pg_conns: int
    pg_active: int
    canary_ms: float | None
    canary_error: str | None
    # CPU fractions (1.0 == one core saturated). These are the third
    # discriminator, and the one that separates a server-side lock from
    # plain CPU saturation — and from a saturated harness, which produces
    # the same rising-latency signature as a server-side wall.
    srv_cpu: float = -1.0
    harness_cpu: float = -1.0


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    i = (len(ys) - 1) * p
    lo = int(i)
    hi = min(lo + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (i - lo)


def assign_url(urls: list[str], worker_id: int) -> str:
    """Round-robin a worker onto one of the servers."""
    return urls[worker_id % len(urls)]


def per_process_cpu(total: float, n_servers: int) -> float:
    """Server CPU per process; negative stays negative (unobservable)."""
    if total < 0 or n_servers < 1:
        return total
    return total / n_servers


class CpuMeter:
    """CPU fraction for a pid, from /proc, between successive reads.

    A value near 0 while latency climbs is the thread-pool/lock signature
    (memory `mcp-fleet-concurrency-limit`: "calls hang, serve at 0% CPU in
    state S"). A value near ``nproc`` is plain saturation. They need telling
    apart before any ceiling from this harness is quotable.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.ticks = os.sysconf("SC_CLK_TCK")
        self._last: tuple[float, float] | None = None

    def read(self) -> float:
        try:
            raw = Path(f"/proc/{self.pid}/stat").read_text(encoding="utf-8")
            # comm can contain spaces and parens; everything after the last
            # ") " is positional.
            fields = raw[raw.rindex(") ") + 2 :].split()
            # utime = field 14, stime = 15 (1-based, incl. pid and comm)
            busy = (int(fields[11]) + int(fields[12])) / self.ticks
        except Exception:
            return -1.0
        now = time.monotonic()
        prev = self._last
        self._last = (now, busy)
        if prev is None or now <= prev[0]:
            return -1.0
        return (busy - prev[1]) / (now - prev[0])


class MultiCpuMeter:
    """Summed CPU fraction over several server pids.

    Any pid that cannot be read makes the sum unobservable (-1) rather than
    silently low, since a low reading is itself a verdict (the pool/lock
    signature).
    """

    def __init__(self, pids: list[int]) -> None:
        self.meters = [CpuMeter(p) for p in pids]

    def read(self) -> float:
        vals = [m.read() for m in self.meters]
        if not vals or any(v < 0 for v in vals):
            return -1.0
        return sum(vals)


def tool_error(result: Any) -> str | None:
    """Map an MCP result onto an error label.

    The runtime returns its own ``[error:Kind]`` envelope as *content* with
    ``isError`` set, so a transport-level success can still be a failure.
    """
    if getattr(result, "isError", False):
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", "") or ""
            if text.startswith("[error:"):
                return text[7:].split("]", 1)[0]
        return "isError"
    return None


# --------------------------------------------------------------------------
# server lifecycle
# --------------------------------------------------------------------------


def start_server(port: int, token: str, log_path: Path) -> subprocess.Popen[bytes]:
    """Launch ``precis serve`` on a network transport.

    stdio would serialise the harness behind a single pipe, which is the one
    thing that must not be a variable here.
    """
    log = log_path.open("wb")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "precis.cli.main",
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            # ``--token=`` form, and hex rather than urlsafe: a
            # base64url token can begin with "-", which argparse reads as
            # a flag and the server then refuses to start.
            f"--token={token}",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )


async def wait_ready(url: str, token: str, timeout_s: float = 120.0) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            async with streamablehttp_client(url, headers=headers) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return
        except Exception as exc:
            last = exc
            await asyncio.sleep(1.0)
    raise RuntimeError(f"server did not become ready in {timeout_s}s: {last!r}")


# --------------------------------------------------------------------------
# load generation
# --------------------------------------------------------------------------


async def worker(
    url: str,
    token: str,
    step: int,
    worker_id: int,
    stop: asyncio.Event,
    out: list[Call],
) -> None:
    """One simulated agent session: its own MCP session, calls in a loop.

    Each worker holds a separate session for the whole step, which is what an
    agent does — reconnecting per call would measure connection setup instead
    of the wall.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with streamablehttp_client(url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                i = worker_id
                while not stop.is_set():
                    label, tool, args, _ = VERB_MIX[i % len(VERB_MIX)]
                    i += 1
                    t0 = time.perf_counter()
                    try:
                        res = await session.call_tool(tool, args)
                        err = tool_error(res)
                    except Exception as exc:
                        err = type(exc).__name__
                    out.append(
                        Call(step, label, (time.perf_counter() - t0) * 1000.0, err)
                    )
    except Exception as exc:
        out.append(Call(step, "session:setup", 0.0, type(exc).__name__))


async def sampler(
    url: str,
    token: str,
    dsn: str | None,
    step: int,
    stop: asyncio.Event,
    out: list[Sample],
    srv_pids: list[int] | None = None,
) -> None:
    """Record the two discriminators every 500 ms.

    The canary goes through the server (so it queues for a thread); the
    connection count goes straight to Postgres (so it does not).
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    conn = None
    if dsn:
        with contextlib.suppress(Exception):
            import psycopg

            conn = psycopg.connect(dsn, autocommit=True)

    srv_meter = MultiCpuMeter(srv_pids) if srv_pids else None
    own_meter = CpuMeter(os.getpid())
    if srv_meter:
        srv_meter.read()
    own_meter.read()

    headers = {"Authorization": f"Bearer {token}"}
    t_start = time.monotonic()
    try:
        async with streamablehttp_client(url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                _, tool, args, _ = CANARY
                while not stop.is_set():
                    n_all = n_active = -1
                    if conn is not None:
                        with contextlib.suppress(Exception), conn.cursor() as cur:
                            cur.execute(
                                "SELECT count(*), "
                                "count(*) FILTER (WHERE state = 'active') "
                                "FROM pg_stat_activity "
                                "WHERE datname = current_database() "
                                "AND pid <> pg_backend_pid()"
                            )
                            row = cur.fetchone()
                            if row:
                                n_all, n_active = int(row[0]), int(row[1])

                    t0 = time.perf_counter()
                    c_ms: float | None = None
                    c_err: str | None = None
                    try:
                        res = await session.call_tool(tool, args)
                        c_err = tool_error(res)
                        c_ms = (time.perf_counter() - t0) * 1000.0
                    except Exception as exc:
                        c_err = type(exc).__name__
                        c_ms = (time.perf_counter() - t0) * 1000.0

                    out.append(
                        Sample(
                            step,
                            time.monotonic() - t_start,
                            n_all,
                            n_active,
                            c_ms,
                            c_err,
                            srv_meter.read() if srv_meter else -1.0,
                            own_meter.read(),
                        )
                    )
                    await asyncio.sleep(0.5)
    except Exception:
        pass
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


async def run_step(
    urls: list[str],
    token: str,
    dsn: str | None,
    n: int,
    duration: float,
    srv_pids: list[int] | None = None,
) -> tuple[list[Call], list[Sample]]:
    calls: list[Call] = []
    samples: list[Sample] = []
    stop = asyncio.Event()

    tasks = [
        asyncio.create_task(worker(assign_url(urls, i), token, n, i, stop, calls))
        for i in range(n)
    ]
    # The canary rides server 0. With K servers it sees one process's queue,
    # which is the latency a session pinned to that process would get.
    tasks.append(
        asyncio.create_task(sampler(urls[0], token, dsn, n, stop, samples, srv_pids))
    )

    await asyncio.sleep(duration)
    stop.set()
    # Generous but bounded: a genuinely wedged pool must show up as a
    # cancelled worker rather than hanging the whole ramp.
    _done, pending = await asyncio.wait(tasks, timeout=60.0)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return calls, samples


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def max_connections(dsn: str | None) -> int | None:
    if not dsn:
        return None
    try:
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as c, c.cursor() as cur:
            cur.execute("SHOW max_connections")
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception:
        return None


def verdict(
    ramp: list[int],
    calls: list[Call],
    samples: list[Sample],
    pg_max: int | None,
    n_servers: int = 1,
    ncpu: int | None = None,
) -> list[str]:
    """Name the wall, or say plainly that none was reached.

    Silence is not success here: a ramp that never degraded has to say so, or
    a too-short run reads like a clean bill of health.
    """
    out: list[str] = []

    def canary_p95(step: int) -> float:
        xs = [
            s.canary_ms for s in samples if s.step == step and s.canary_ms is not None
        ]
        return pct([x for x in xs if x is not None], 0.95)

    base = next((canary_p95(s) for s in ramp if canary_p95(s) == canary_p95(s)), None)
    worst_step = ramp[-1]
    worst = canary_p95(worst_step)

    conn_peak = max((s.pg_conns for s in samples if s.pg_conns >= 0), default=-1)
    conn_errors = sum(
        1
        for c in calls
        if c.error
        and any(
            k in c.error.lower()
            for k in ("toomany", "operational", "connection", "admin_shutdown")
        )
    )
    err_rate = (sum(1 for c in calls if c.error) / len(calls)) if calls else 0.0

    if base and worst == worst and base > 0:
        ratio = worst / base
        out.append(
            f"canary p95 {base:.0f} ms at N={ramp[0]} -> {worst:.0f} ms at "
            f"N={worst_step} ({ratio:.1f}x)"
        )
        if ratio >= 5.0:
            srv = [
                s.srv_cpu for s in samples if s.step == worst_step and s.srv_cpu >= 0
            ]
            own = [
                s.harness_cpu
                for s in samples
                if s.step == worst_step and s.harness_cpu >= 0
            ]
            srv_med = statistics.median(srv) if srv else -1.0
            own_med = statistics.median(own) if own else -1.0
            ncpu = ncpu or os.cpu_count() or 1
            proc_med = per_process_cpu(srv_med, n_servers)
            srv_txt = (
                f"server CPU {srv_med:.2f} over {n_servers} process(es)"
                if srv_med >= 0
                else "server CPU not observable"
            )
            out.append(
                f"at N={worst_step}: {srv_txt}, harness CPU {own_med:.2f} "
                f"(of {ncpu} cores here)"
            )
            # A saturated harness produces the same rising-latency curve as a
            # server-side wall, so it has to be excluded by name before any
            # ceiling here is quotable.
            if own_med >= 0.85 and own_med > srv_med:
                out.append(
                    "INCONCLUSIVE — the HARNESS is the bottleneck, not the "
                    "server. All load generators share one event loop in one "
                    "process; re-run with the load split across processes "
                    "before reading any ceiling off this."
                )
            elif srv_med < 0:
                # External target: no /proc view of the server. Saying "pool"
                # here would be the fall-through, not a measurement.
                out.append(
                    "WALL NOT NAMED — the canary queues with load, so there is "
                    "one shared queue, but the server's CPU is not observable "
                    "from here (external --url target). GIL versus thread pool "
                    "needs the host-side reading: ~1 core busy per process is "
                    "the GIL, ~0 is the pool or a lock."
                )
            elif srv_med >= 0.85 * ncpu:
                out.append(
                    "WALL = server CPU. Genuinely compute-bound across the "
                    "whole box, so the lever is fewer/cheaper calls or more "
                    "cores, not a bigger thread pool."
                )
            elif proc_med >= 0.7:
                # ~1 core busy on a many-core box is the GIL, not a shortage.
                # Worth separating, because it inverts the remedy: the pool
                # is not starved of threads, the threads cannot run at once.
                out.append(
                    f"WALL = single-threaded execution (GIL). Each server "
                    f"process holds ~{proc_med:.1f} cores of {ncpu} and will "
                    f"not climb past it "
                    f"however many callers arrive. Threads are not the "
                    f"shortage — they cannot run in parallel for CPU-bound "
                    f"work, so raising the anyio pool changes nothing. The "
                    f"levers are process-level parallelism (several `precis "
                    f"serve` processes behind a balancer) or moving the hot "
                    f"work out of Python."
                )
            else:
                out.append(
                    "WALL = anyio worker pool or a lock inside it. The canary "
                    "touches neither the DB nor the embedder and the server is "
                    "NOT CPU-bound, so the time is spent waiting, not working "
                    "— the signature in memory `mcp-fleet-concurrency-limit`."
                )
        else:
            out.append(
                "Worker pool NOT the binding constraint over this ramp — the "
                "canary stayed flat, so calls were not queueing for a thread."
            )

    if pg_max is not None and conn_peak >= 0:
        out.append(f"peak Postgres connections {conn_peak} of max_connections {pg_max}")
        if conn_peak >= 0.9 * pg_max or conn_errors:
            out.append(
                "WALL = Postgres connections. Note the dev DB has no pgbouncer; "
                "prod does, so this ceiling is the harness's, not prod's."
            )

    out.append(f"overall error rate {err_rate:.1%} over {len(calls)} calls")
    # A verb that fails even alone is a stale VERB_MIX entry, not load: its
    # latency column measures the error path and its errors inflate the rate.
    first = [c for c in calls if c.step == ramp[0]]
    for label in sorted({c.label for c in first}):
        mine = [c for c in first if c.label == label]
        if all(c.error for c in mine):
            out.append(
                f"VERB MIX BROKEN — {label} fails at N={ramp[0]} "
                f"({mine[0].error}); fix VERB_MIX before reading its column."
            )
    if not any(v.startswith("WALL") for v in out):
        out.append(
            f"NO WALL REACHED. Either the ramp is too short or the ceiling "
            f"is above N={worst_step} — extend --ramp before quoting this as "
            f"a safe number."
        )
    return out


def render(
    ramp: list[int],
    calls: list[Call],
    samples: list[Sample],
    pg_max: int | None,
    duration: float,
    n_servers: int = 1,
    target: str = "local",
) -> str:
    lines: list[str] = []
    lines.append("# MCP concurrency load test")
    lines.append("")
    lines.append(
        f"Run {datetime.now(UTC).isoformat()} · {duration:.0f}s per step · "
        f"target {target} · {n_servers} server process(es)"
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    for v in verdict(ramp, calls, samples, pg_max, n_servers):
        lines.append(f"- {v}")
    lines.append("")
    lines.append("## Latency by verb and concurrency (ms)")
    lines.append("")
    lines.append("| N | verb | calls | p50 | p95 | p99 | err |")
    lines.append("|---|---|---|---|---|---|---|")
    for n in ramp:
        for label, *_ in VERB_MIX:
            xs = [c.ms for c in calls if c.step == n and c.label == label]
            if not xs:
                continue
            errs = sum(1 for c in calls if c.step == n and c.label == label and c.error)
            lines.append(
                f"| {n} | {label} | {len(xs)} | {pct(xs, 0.5):.0f} | "
                f"{pct(xs, 0.95):.0f} | {pct(xs, 0.99):.0f} | "
                f"{errs / len(xs):.1%} |"
            )
    lines.append("")
    lines.append("## Discriminators")
    lines.append("")
    lines.append(
        "| N | canary p95 | pg conns | server CPU | harness CPU | throughput/s |"
    )
    lines.append("|---|---|---|---|---|---|")

    def med(xs: list[float]) -> str:
        return f"{statistics.median(xs):.2f}" if xs else "—"

    for n in ramp:
        cx = [s.canary_ms for s in samples if s.step == n and s.canary_ms is not None]
        conns = [s.pg_conns for s in samples if s.step == n and s.pg_conns >= 0]
        srv = [s.srv_cpu for s in samples if s.step == n and s.srv_cpu >= 0]
        own = [s.harness_cpu for s in samples if s.step == n and s.harness_cpu >= 0]
        total = sum(1 for c in calls if c.step == n)
        lines.append(
            f"| {n} | {pct([x for x in cx if x is not None], 0.95):.0f} | "
            f"{max(conns) if conns else '—'} | {med(srv)} | {med(own)} | "
            f"{total / duration:.1f} |"
        )
    lines.append("")
    lines.append(f"CPU is a fraction of one core; the container has {os.cpu_count()}.")
    if pg_max is not None:
        lines.append(f"max_connections = {pg_max}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------


async def amain(args: argparse.Namespace) -> int:
    ramp = [int(x) for x in args.ramp.split(",")]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    procs: list[subprocess.Popen[bytes]] = []
    if args.url:
        # External target: its CPU and Postgres are not ours to sample.
        token = os.environ["PRECIS_LOADTEST_TOKEN"]
        urls = [args.url]
        dsn = None
        n_servers = 1
        target = args.url
    else:
        token = secrets.token_hex(24)
        n_servers = args.servers
        urls = [f"http://127.0.0.1:{args.port + k}/mcp" for k in range(n_servers)]
        dsn = os.environ.get("PRECIS_DATABASE_URL")
        target = "local"
        for k in range(n_servers):
            log = out_dir / f"{stamp}-server{k}.log"
            procs.append(start_server(args.port + k, token, log))
    pg_max = max_connections(dsn)

    calls: list[Call] = []
    samples: list[Sample] = []
    try:
        for u in urls:
            await wait_ready(u, token)
        print(f"servers up: {', '.join(urls)}", flush=True)
        pids = [p.pid for p in procs] or None
        for n in ramp:
            if any(p.poll() is not None for p in procs):
                print(f"a server died before N={n}; see {out_dir}", file=sys.stderr)
                break
            print(f"  step N={n} for {args.duration}s …", flush=True)
            c, s = await run_step(urls, token, dsn, n, args.duration, pids)
            calls.extend(c)
            samples.extend(s)
            errs = sum(1 for x in c if x.error)
            print(f"    {len(c)} calls, {errs} errors", flush=True)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            with contextlib.suppress(Exception):
                p.wait(timeout=20)

    md = render(ramp, calls, samples, pg_max, args.duration, n_servers, target)
    (out_dir / f"{stamp}-report.md").write_text(md, encoding="utf-8")
    (out_dir / f"{stamp}-raw.json").write_text(
        json.dumps(
            {
                "ramp": ramp,
                "duration_s": args.duration,
                "target": target,
                "n_servers": n_servers,
                "pg_max_connections": pg_max,
                "calls": [asdict(c) for c in calls],
                "samples": [asdict(s) for s in samples],
            }
        ),
        encoding="utf-8",
    )
    print(md)
    print(f"report: {out_dir / f'{stamp}-report.md'}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ramp", default=",".join(str(x) for x in DEFAULT_RAMP))
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--servers",
        type=int,
        default=1,
        help="start K serve processes on consecutive ports (multi-process arm)",
    )
    p.add_argument(
        "--url",
        help="target an already-running server; token from PRECIS_LOADTEST_TOKEN",
    )
    p.add_argument(
        "--live-target",
        action="store_true",
        help="required with --url: confirms you mean to load a live server",
    )
    p.add_argument("--out", default="scripts/mcp_loadtest/out")
    args = p.parse_args(argv)
    if args.servers < 1:
        p.error("--servers must be >= 1")
    if args.live_target and not args.url:
        p.error("--live-target only applies to --url")
    if args.url:
        if not args.live_target:
            p.error("--url loads a live server; add --live-target to confirm")
        if args.servers != 1:
            p.error("--servers starts local processes; it does not apply to --url")
        if not os.environ.get("PRECIS_LOADTEST_TOKEN"):
            p.error("--url needs the bearer token in PRECIS_LOADTEST_TOKEN")
    return args


def main() -> int:
    return asyncio.run(amain(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
