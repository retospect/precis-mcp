"""Alertmanager webhook receiver: Prometheus alerts into the ops channel.

Prometheus evaluates ``deploy/roles/monitoring/templates/alert_rules.yml.j2``,
Alertmanager groups what fires, and its one ``webhook_configs`` receiver POSTs
here. Each alert group becomes one ``queue_ops_message`` (the same path
``notify_critical_alert`` and ``health_digest`` use), so asa_bot posts it to
the channel ``PRECIS_OPS_ALERT_TARGET`` names. No Discord webhook, no new
secret (Reto, 2026-10-04; thread monitors-that-go-quiet, Do-next 1).

**Auth.** There is none on the wire: the server binds loopback by default and
the monitoring role runs it beside Alertmanager, so only that host's processes
can post. Do not bind it to a routable address.

**Failure direction.** Anything that stops a message being queued answers
non-2xx, and Alertmanager retries a non-2xx webhook. 503 = no
``PRECIS_OPS_ALERT_TARGET`` (queueing would be a silent no-op, which must not
read as delivered); 500 = the DB write failed; 400 = a body that is not an
Alertmanager group. A repeat of an already-queued group inside
:data:`DEDUPE_TTL_S` is acknowledged 200 without a second message. Accepted
trade-off: while Postgres or asa_bot is down these alerts do not arrive,
PostgresDown included.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from precis.alerts import _ops_alert_target, queue_ops_message

log = logging.getLogger(__name__)

#: Identical (groupKey, status, alert set) inside this window is one message.
#: Shorter than any sane ``repeat_interval``, so scheduled reminders still go.
DEDUPE_TTL_S = 600.0
#: Alerts spelled out in one message; the rest are counted.
MAX_ALERTS_LISTED = 20
#: A webhook body above this is not an Alertmanager group.
MAX_BODY_BYTES = 1_000_000

_SEVERITY_ICON = {"critical": "\U0001f6a8", "warning": "⚠️"}


class BadPayload(ValueError):
    """The body is not an Alertmanager webhook group (answered 400)."""


#: One alert line's cap, so a huge annotation cannot fan out into many messages.
MAX_LINE_CHARS = 300
#: Socket timeout on a request handler, so a stalled client frees its thread.
REQUEST_TIMEOUT_S = 10


def _safe(text: object) -> str:
    """Defang Discord mentions: a zero-width space after every ``@``."""
    return str(text).replace("@", "@\u200b")


def _alert_line(alert: dict[str, Any]) -> str:
    labels = alert.get("labels") or {}
    ann = alert.get("annotations") or {}
    name = labels.get("alertname", "alert")
    inst = labels.get("instance", "")
    text = ann.get("summary") or ann.get("description") or ""
    head = f"{_SEVERITY_ICON.get(labels.get('severity', ''), '-')} {name}"
    if inst and inst not in text:
        head += f" @ {inst}"
    line = f"{head}: {text}" if text else head
    return _safe(line)[:MAX_LINE_CHARS]


def format_group(payload: Any) -> tuple[str, str, str]:
    """Return ``(title, body, dedupe_key)`` for one webhook group.

    Raises :class:`BadPayload` on anything that is not a v4 group with at
    least one alert.
    """
    if not isinstance(payload, dict):
        raise BadPayload("body is not a JSON object")
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        raise BadPayload("no alerts in group")
    if not all(isinstance(a, dict) for a in alerts):
        raise BadPayload("alerts must be objects")
    status = str(payload.get("status") or "firing").lower()
    firing = status != "resolved"
    names = sorted(
        {str((a.get("labels") or {}).get("alertname", "alert")) for a in alerts}
    )
    label = "FIRING" if firing else "RESOLVED"
    icon = "\U0001f525" if firing else "✅"
    title = _safe(f"{icon} {label}: {', '.join(names)} ({len(alerts)})")[:200]
    lines = [_alert_line(a) for a in alerts[:MAX_ALERTS_LISTED]]
    if len(alerts) > MAX_ALERTS_LISTED:
        lines.append(f"... +{len(alerts) - MAX_ALERTS_LISTED} more")
    body = "\n".join([title, *lines])
    ident = json.dumps(
        [
            payload.get("groupKey", ""),
            status,
            sorted(f"{a.get('fingerprint', '')}:{a.get('status', '')}" for a in alerts),
        ]
    )
    return title, body, hashlib.sha256(ident.encode("utf-8")).hexdigest()


class Deduper:
    """Remembers recently queued group keys (in-process, thread-safe)."""

    def __init__(
        self, ttl_s: float = DEDUPE_TTL_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self._ttl}
            return key in self._seen

    def mark(self, key: str) -> None:
        with self._lock:
            self._seen[key] = self._clock()


def handle_payload(store: Any, payload: Any, dedupe: Deduper) -> tuple[int, str]:
    """Queue one webhook group; return ``(http_status, reason)``."""
    try:
        title, body, key = format_group(payload)
    except BadPayload as exc:
        return 400, str(exc)
    if dedupe.seen(key):
        return 200, "duplicate group, already queued"
    if not _ops_alert_target().strip():
        return 503, "PRECIS_OPS_ALERT_TARGET unset: nothing would be delivered"
    try:
        queue_ops_message(
            store, title, body, reason=f"prometheus {key[:12]}", raise_on_error=True
        )
    except Exception:
        log.exception("alertmanager receiver: queue failed")
        return 500, "queueing the ops message failed"
    dedupe.mark(key)
    return 200, "queued"


def make_server(store: Any, host: str, port: int) -> ThreadingHTTPServer:
    """Build (not start) the receiver HTTP server."""
    dedupe = Deduper()

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S

        def _reply(self, code: int, text: str) -> None:
            data = (text + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

        def do_POST(self) -> None:
            if self.path not in ("/", "/alerts"):
                self._reply(404, "not found")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY_BYTES:
                self._reply(400, "missing or oversized body")
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._reply(400, "body is not JSON")
                return
            self._reply(*handle_payload(store, payload, dedupe))

        def log_message(self, format: str, *args: Any) -> None:
            log.info("alertmanager receiver: " + format, *args)

    return ThreadingHTTPServer((host, port), Handler)


def serve(store: Any, host: str, port: int) -> None:
    """Run the receiver until interrupted."""
    server = make_server(store, host, port)
    log.info("alertmanager receiver listening on %s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


__all__ = [
    "BadPayload",
    "Deduper",
    "format_group",
    "handle_payload",
    "make_server",
    "serve",
]
