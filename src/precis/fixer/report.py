"""Fixer reporting — management by exception.

Three tiers:

* ``OK`` — clean ship: **silent** on the loud channel (log only).
* ``FIXED`` — hit trouble, fix-forwarded, now green: **one-line** note.
* ``NEEDS_YOU`` — gate red / couldn't verify / bubbled: **full, loud.**

The durable record (an ``agentlog`` + a gripe comment) is written on
*every* run incl. greens — that half is the caller's job and is
deferred at the MVP; this module owns the *push* (loud/one-line) side,
which goes to the wired Discord #news channel via a webhook.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger("precis.fixer")


class ReportStatus(StrEnum):
    OK = "ok"
    FIXED = "fixed"
    NEEDS_YOU = "needs_you"


@dataclass(frozen=True)
class Report:
    status: ReportStatus
    title: str
    detail: str

    def one_line(self) -> str:
        mark = {"ok": "✓", "fixed": "⚠", "needs_you": "✗"}[self.status.value]
        return f"{mark} {self.title} — {self.detail.splitlines()[0]}"


def _should_push(status: ReportStatus) -> bool:
    """OK is silent on the loud channel; only exceptions get pushed."""
    return status is not ReportStatus.OK


def _post_discord(webhook: str, content: str) -> None:
    data = json.dumps({"content": content}).encode("utf-8")
    from precis.utils.http import POLICIES, call_with_backoff, polite_user_agent
    from precis.utils.rate_limit import RateLimited

    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": polite_user_agent()},
    )
    try:
        # discord.com is a third party: backoff on 429/5xx, Retry-After honoured.
        call_with_backoff(
            lambda: urllib.request.urlopen(req, timeout=15).close(),
            service="discord",
            policy=POLICIES["discord"],
            url=webhook,
        )
    except (urllib.error.URLError, OSError, RateLimited) as exc:
        log.warning("fixer: discord post failed: %s", exc)


def emit_report(report: Report, discord_webhook: str | None) -> None:
    """Log always; push to Discord only for exceptions."""
    log.info("%s", report.one_line())
    if not _should_push(report.status):
        return  # clean ship: silent on #news
    if report.status is ReportStatus.NEEDS_YOU:
        body = f"{report.one_line()}\n```\n{report.detail}\n```"
    else:  # FIXED — one-line
        body = report.one_line()
    if discord_webhook:
        _post_discord(discord_webhook, body)
    else:
        log.warning(
            "fixer: no PRECIS_FIXER_DISCORD_WEBHOOK; would have pushed:\n%s", body
        )
