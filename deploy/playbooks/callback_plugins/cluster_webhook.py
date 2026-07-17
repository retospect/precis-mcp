# Ansible managed
"""Ansible callback plugin — posts play results to cluster webhook."""

from __future__ import annotations

import json
import os
import urllib.request

from ansible.plugins.callback import CallbackBase

WEBHOOK_URL = os.environ.get("CLUSTER_WEBHOOK_URL", "")


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "cluster_webhook"
    CALLBACK_NEEDS_WHITELIST = True

    def _post(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            self._display.warning(f"Cluster webhook failed: {e}")

    def v2_playbook_on_stats(self, stats):
        hosts = sorted(stats.processed.keys())
        summary = {}
        for h in hosts:
            s = stats.summarize(h)
            summary[h] = s

        has_failures = any(
            summary[h].get("failures", 0) > 0 or summary[h].get("unreachable", 0) > 0
            for h in hosts
        )

        self._post(
            {
                "event": "ansible_run",
                "status": "failed" if has_failures else "ok",
                "summary": summary,
            }
        )
