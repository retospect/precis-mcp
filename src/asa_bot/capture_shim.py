"""HTTP shim that Claude Code hooks call to capture the assistant turn.

Stop hook script POSTs JSON:
  {
    "conv_slug": "discord/G/C/T",
    "text": "<assistant body>",
    "msg_id": "claude:<session_id>:<turn>",
    "stop_reason": "end_turn",
    "input_tokens": ...,
    "output_tokens": ...,
    "cache_read_tokens": ...,
    "cache_creation_tokens": ...,
    "duration_ms": ...
  }

asa_bot relays via its long-lived precis MCP client.

If precis is down (network blip, mid-restart), the request lands in
a fallback JSONL file. On precis health recovery the bot replays
the JSONL.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from asa_bot.config import CaptureConfig
from asa_bot.precis_client import PrecisClient

log = logging.getLogger(__name__)


class CaptureShim:
    def __init__(
        self,
        cfg: CaptureConfig,
        precis: PrecisClient,
    ) -> None:
        self._cfg = cfg
        self._precis = precis
        self._fallback_path = Path(cfg.fallback_jsonl)
        self._fallback_path.parent.mkdir(parents=True, exist_ok=True)

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/capture", self._handle_capture)
        app.router.add_get("/health", self._handle_health)
        return app

    async def _handle_capture(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        conv_slug = data.get("conv_slug")
        text = data.get("text")
        if not conv_slug or not text:
            return web.json_response({"error": "missing conv_slug or text"}, status=400)
        try:
            await self._write_to_precis(conv_slug, data)
            return web.json_response({"ok": True})
        except Exception as e:
            log.warning("capture failed, falling back to JSONL: %s", e)
            self._append_fallback(data)
            return web.json_response({"ok": False, "fallback": True}, status=202)

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _write_to_precis(self, conv_slug: str, data: dict[str, Any]) -> None:
        meta: dict[str, Any] = {}
        for k in (
            "stop_reason",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "duration_ms",
        ):
            if data.get(k) is not None:
                meta[k] = data[k]
        meta["ts"] = data.get("ts") or datetime.now(tz=UTC).isoformat()
        args = {
            "kind": "conv",
            "id": conv_slug,
            "text": data["text"],
            "author": data.get("author") or "asa",
            "meta": meta,
        }
        msg_id = data.get("msg_id")
        if msg_id:
            args["msg_id"] = msg_id
        await self._precis.call_tool("put", args)

    def _append_fallback(self, data: dict[str, Any]) -> None:
        record = dict(data)
        record["fallback_ts"] = datetime.now(tz=UTC).isoformat()
        with self._fallback_path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

    async def replay_fallback(self) -> int:
        """On startup / recovery, replay JSONL into precis.

        Returns the count replayed. Lines that fail again are kept;
        successful lines are removed. Safe to call any time.
        """
        if not self._fallback_path.exists():
            return 0
        remaining: list[str] = []
        replayed = 0
        with self._fallback_path.open() as fh:
            lines = fh.readlines()
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = rec.get("conv_slug")
            if not slug:
                continue
            try:
                await self._write_to_precis(slug, rec)
                replayed += 1
            except Exception:
                remaining.append(line)
        with self._fallback_path.open("w") as fh:
            fh.writelines(remaining)
        if replayed:
            log.info("capture-shim replayed %d fallback records", replayed)
        return replayed


async def start_shim(
    cfg: CaptureConfig, precis: PrecisClient
) -> tuple[CaptureShim, web.AppRunner]:
    shim = CaptureShim(cfg, precis)
    runner = web.AppRunner(shim.make_app())
    await runner.setup()
    site = web.TCPSite(runner, cfg.listen_host, cfg.listen_port)
    await site.start()
    log.info("capture shim listening on %s:%d", cfg.listen_host, cfg.listen_port)
    return shim, runner
