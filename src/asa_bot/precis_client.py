"""Long-lived precis MCP subprocess client.

asa_bot keeps one ``precis serve`` subprocess running across its
lifetime, talks to it over stdio JSON-RPC. Same transport claude
uses for MCP. Background health-check restarts on crash.

Concurrent callers serialize through an asyncio.Lock so request/
response framing stays sane on the single pipe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

from asa_bot.config import PrecisConfig

log = logging.getLogger(__name__)


class PrecisClient:
    """JSON-RPC client over a long-lived precis MCP subprocess."""

    def __init__(self, cfg: PrecisConfig) -> None:
        self._cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._initialized = False
        # Set by _kill: returncode stays None until the loop reaps the
        # child, so it cannot tell the next request the process is dead.
        self._killed = False

    async def start(self) -> None:
        await self._spawn()
        # Kick a tools/list so we know the server actually came up.
        await self.request("tools/list", {})

    async def _spawn(self) -> None:
        env = dict(os.environ)
        env.update(self._cfg.env)
        if self._cfg.database_url:
            env.setdefault("PRECIS_DATABASE_URL", self._cfg.database_url)
        self._proc = await asyncio.create_subprocess_exec(
            *self._cfg.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._initialized = False
        self._killed = False
        log.info("precis MCP spawned pid=%s", self._proc.pid)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        # MCP requires an initialize handshake before any other calls.
        await self._send_recv(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "asa-bot", "version": "0.1.0"},
            },
        )
        await self._send_notification("notifications/initialized")
        self._initialized = True

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request, return the result or raise.

        Bounded by ``request_timeout_seconds``: the lock is held across the
        response read, so an unbounded read on a hung subprocess wedged
        every later call (gr479812). On timeout the subprocess is killed;
        the next request respawns it.
        """
        async with self._lock:
            await self._respawn_if_dead()
            try:
                return await asyncio.wait_for(
                    self._request_locked(method, params),
                    timeout=self._cfg.request_timeout_seconds,
                )
            except TimeoutError:
                log.error(
                    "precis MCP %s timed out after %ss — killing subprocess",
                    method,
                    self._cfg.request_timeout_seconds,
                )
                self._kill()
                raise TimeoutError(
                    f"precis MCP {method} timed out after "
                    f"{self._cfg.request_timeout_seconds:g}s"
                ) from None

    async def _request_locked(self, method: str, params: dict[str, Any]) -> Any:
        await self._ensure_initialized()
        return await self._send_recv(method, params)

    async def _respawn_if_dead(self) -> None:
        proc = self._proc
        if proc is not None and not self._killed and proc.returncode is None:
            return
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5)
        await self._spawn()

    def _kill(self) -> None:
        if self._proc is None:
            return
        self._killed = True
        with contextlib.suppress(ProcessLookupError):
            self._proc.kill()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Convenience: invoke an MCP tool, return its content text."""
        resp = await self.request("tools/call", {"name": name, "arguments": arguments})
        # MCP tool responses come back as {"content": [{"type": "text",
        # "text": "..."}], "isError": false}.
        content = resp.get("content") if isinstance(resp, dict) else None
        if not content:
            return ""
        chunks = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                chunks.append(c.get("text", ""))
        return "\n".join(chunks)

    async def _send_recv(self, method: str, params: dict[str, Any]) -> Any:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("precis MCP not started")
        self._next_id += 1
        req_id = self._next_id
        req = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        line = json.dumps(req) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()
        # Read until we get the matching id. precis MCP may emit
        # notifications interleaved; ignore those.
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                raise ConnectionError("precis MCP closed stdout")
            try:
                msg = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                log.warning("precis MCP non-JSON line: %r", raw[:200])
                continue
            if msg.get("id") != req_id:
                # Notification or response to a different request — skip.
                continue
            if "error" in msg:
                raise RuntimeError(f"precis MCP error: {msg['error']}")
            return msg.get("result")

    async def _send_notification(self, method: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        msg = {"jsonrpc": "2.0", "method": method, "params": {}}
        self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def ping(self) -> bool:
        """Liveness **and** DB-kind health.

        ``tools/list`` alone only proves the subprocess is answering the
        MCP handshake — a precis that came up *without a store* (e.g. it
        was spawned during a reboot boot-race while the DB was
        unreachable) still answers ``tools/list`` cleanly but reports
        ``unknown kind`` for every DB-backed kind. That degraded server
        wipes asa's memory/conv/gripe recall for as long as it lives, and
        a liveness-only check never restarts it. So we also probe a cheap
        store-backed read and treat a "kind unavailable" reply as
        unhealthy, which lets :func:`health_loop` respawn it.
        """
        try:
            await asyncio.wait_for(self.request("tools/list", {}), timeout=5)
            # gripe ``/open`` is a pure-SQL tag-filtered list view (no
            # embedder call) — the cheapest way to force the store path.
            blob = await asyncio.wait_for(
                self.call_tool("get", {"kind": "gripe", "id": "/open"}),
                timeout=10,
            )
        except Exception as e:
            log.warning("precis ping failed: %s", e)
            return False
        if "unknown kind" in blob or "no kinds in this build support" in blob:
            log.warning(
                "precis ping: DB kinds unavailable — degraded/storeless "
                "server, forcing restart: %.120s",
                blob.replace("\n", " "),
            )
            return False
        return True

    async def restart(self) -> None:
        """Kill + respawn. Used by the health-check loop.

        Kills *before* taking the lock: a request stuck reading a hung
        subprocess holds the lock, and the kill hands its read an EOF so it
        releases. Taking the lock first deadlocked restart behind that very
        request (gr479812).
        """
        old = self._proc
        self._kill()
        async with self._lock:
            if old is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(old.wait(), timeout=5)
            if self._proc is old:
                # Not already respawned by a request that got the lock first.
                self._proc = None
                await self._spawn()

    async def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (ProcessLookupError, TimeoutError):
            if self._proc is not None:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass


async def health_loop(client: PrecisClient, interval: int) -> None:
    """Background task: check precis health, restart on failure."""
    while True:
        await asyncio.sleep(interval)
        if not await client.ping():
            log.warning("precis MCP unhealthy — restarting")
            try:
                await client.restart()
            except Exception:
                log.exception("precis restart failed")
